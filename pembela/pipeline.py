"""Orkestrasi satu kasus end-to-end (stand-in untuk alur Maestro).

triage -> kumpul bukti -> flag -> review -> skor (kode) -> [draf -> validasi -> hash -> approval] -> ajukan -> hasil
Semua keputusan akhir dicatat di audit log. Tidak ada pengajuan tanpa approval manusia (lihat robot_http).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from . import rubric
from .approval import GateError
from .evidence import agent_view, collect
from .robot_http import RobotError, file_appeal
from .validator import draft_hash, validate_draft, validate_review, validate_schema

WIB = timezone(timedelta(hours=7))
EV_LABEL = {"WAYBILL": "Data resi", "TRACKING": "Riwayat pelacakan", "CHAT_LOG": "Riwayat chat",
            "ORDER_RECORD": "Catatan pesanan", "PACKING_VIDEO": "Video packing", "HANDOVER_PHOTO": "Foto serah-terima",
            "LISTING_SNAPSHOT": "Tangkapan layar listing", "RETURN_RECEIPT_VIDEO": "Video penerimaan retur"}
MAX_EDITS = 3
OUTCOME_STATES = ("RESOLVED_WON", "RESOLVED_LOST")


class AuditLog:
    def __init__(self):
        self.rows = []

    def add(self, case_id, event, **kw):
        self.rows.append({"ts": datetime.now(WIB).isoformat(timespec="seconds"),
                          "case_id": case_id, "event": event, **kw})


def _minutes_left(deadline_at):
    if not deadline_at:
        return None
    return int((datetime.fromisoformat(deadline_at) - datetime.now(WIB)).total_seconds() // 60)


def _result(case_id, state, reason, **kw):
    return {"case_id": case_id, "final_state": state, "reason": reason, **kw}


def run_case(session, backend, approver, case_id, media_root, case_no, audit=None,
             poll_s=0.0, poll_tries=1, hash_index=None):
    audit = audit or AuditLog()
    log = lambda ev, **kw: audit.add(case_id, ev, **kw)
    code, d = session.get_json("/api/disputes/%s" % case_id)
    if code != 200:
        log("INTAKE_FAILED", http=code)
        return _result(case_id, "ESCALATED_TO_HUMAN", "gagal membaca sengketa (HTTP %s)" % code)
    if d["status"] != "OPEN":   # sudah diajukan (mis. manual di luar sistem) atau sudah lewat: jangan dianalisis
        state = "EXPIRED" if d["status"] == "EXPIRED" else "ALREADY_FILED"
        log("NOT_OPEN", status=d["status"])
        return _result(case_id, state, "status sengketa di Seller Center: %s" % d["status"])
    minutes_left = _minutes_left(d["deadline_at"])
    track_code, tr = session.get_json("/api/disputes/%s/evidence/tracking" % case_id)
    tracking = (tr or {}).get("tracking", []) if track_code == 200 else []
    stat = {e["status"] for e in tracking}

    # ---- agent 1 -------------------------------------------------------------------------
    a1_in = {"case_id": case_id, "platform": "MarketMock", "order_id": d["order_id"],
             "order_value_idr": d["order_value_idr"], "buyer_claim_text": d["buyer_claim_text"],
             "deadline_at": d["deadline_at"],
             "context": {"product": d.get("product"),
                         "has_return_flow": any(s.startswith("RETURN_") for s in stat),
                         "tracking_has_delivered_event": "DELIVERED" in stat}}
    errs = validate_schema(a1_in, "agent1_input")
    if errs:
        raise ValueError(errs)
    a1 = backend.triage(a1_in)
    errs = validate_schema(a1, "agent1_output")
    if errs:
        log("TRIAGE_INVALID", errors=errs)
        return _result(case_id, "ESCALATED_TO_HUMAN", "keluaran triase tidak valid")
    log("TRIAGE", case_type=a1["case_type"], confidence=a1["confidence"], route=a1["route"])
    # kode menegakkan aturan eskalasi, bukan hanya agent
    if d["deadline_at"] is None:
        return _result(case_id, "ESCALATED_TO_HUMAN", "tenggat tidak diketahui", triage=a1)
    if a1["route"] == "ESCALATE" or a1["case_type"] == "UNKNOWN" or a1["confidence"] < 0.6:
        return _result(case_id, "ESCALATED_TO_HUMAN", "triase tidak yakin (jenis kasus tidak jelas)", triage=a1)
    if minutes_left is not None and minutes_left <= 0:
        return _result(case_id, "EXPIRED", "tenggat sudah lewat", triage=a1)
    ctype = a1["case_type"]

    # ---- bukti ---------------------------------------------------------------------------
    ev = collect(session, case_id, d["order_id"], ctype, media_root, case_no)
    flags = rubric.merge_flags(rubric.detect_flags(ev), rubric.cross_order_flags(ev, hash_index, d["order_id"]))
    log("EVIDENCE", statuses={e["type"]: e["status"] for e in ev}, flags=[f["code"] for f in flags])
    checklist = rubric.checklist_for(ctype, ev)
    if minutes_left is not None and minutes_left < rubric.BUFFER_MIN:
        return _result(case_id, "ESCALATED_TO_HUMAN",
                       "sisa waktu %d menit < buffer %d menit" % (minutes_left, rubric.BUFFER_MIN),
                       triage=a1, evidence=ev)

    # ---- agent 2 -------------------------------------------------------------------------
    a2_in = {"case_id": case_id, "case_type": ctype, "claim_summary": a1["extracted"]["claim_summary"],
             "minutes_left": minutes_left, "evidence": [agent_view(e) for e in ev], "checklist": checklist}
    errs = validate_schema(a2_in, "agent2_input")
    if errs:
        raise ValueError(errs)
    a2 = backend.review(a2_in)
    errs = validate_review(a2, ctype, ev)
    if errs:
        log("REVIEW_INVALID", errors=errs)
        return _result(case_id, "ESCALATED_TO_HUMAN", "keluaran peninjau tidak valid: " + errs[0],
                       triage=a1, evidence=ev)
    allflags = rubric.merge_flags(flags, a2["consistency_flags"])
    statuses = {i["item_id"]: i["status"] for i in a2["items"]}
    sc = rubric.score(checklist, statuses, allflags, minutes_left)
    log("SCORE", **sc, agent_proposal=a2["recommendation_proposal"])
    base = dict(triage=a1, evidence=ev, score=sc, flags=allflags, items=a2["items"])
    if sc["recommendation"] == "DO_NOT_APPEAL":
        return _result(case_id, "CLOSED_NO_APPEAL", "bukti tidak cukup dan tidak dapat dilengkapi", **base)
    if sc["recommendation"] == "COLLECT_MORE":
        return _result(case_id, "AWAITING_MORE_EVIDENCE", "ada bukti yang dapat dilengkapi: %s" % ", ".join(sc["failed_items"]),
                       missing=a2["missing_evidence"], **base)

    # ---- agent 3 + validasi + persetujuan -----------------------------------------------
    crit = [c["item_id"] for c in checklist if c["critical"]]
    a3_ev = []
    for e in ev:
        v = agent_view(e)
        v["label"] = EV_LABEL[e["type"]]
        v["events"] = [{"ts": x["ts"], "label": "Status tracking: " + x["status"]}
                       for x in e["extracted"].get("events", [])]
        a3_ev.append(v)
    a3_in = {"case_id": case_id, "case_type": ctype,
             "order": {"order_id": d["order_id"], "product": d.get("product"), "order_value_idr": d["order_value_idr"]},
             "buyer_claim_summary": a1["extracted"]["claim_summary"], "evidence": a3_ev,
             "review": {"items": a2["items"], "strength": sc["strength"], "recommendation": sc["recommendation"],
                        "critical_items": crit, "flags": allflags},
             "requested_outcome_options": ["pengembalian dana kepada penjual"]}
    errs = validate_schema(a3_in, "agent3_input")
    if errs:
        raise ValueError(errs)
    a3 = backend.draft(a3_in)
    errs = validate_draft(a3, ev)
    if errs:
        log("DRAFT_INVALID", errors=errs)
        return _result(case_id, "ESCALATED_TO_HUMAN", "draf gagal validasi: " + errs[0], **base)
    if a3["status"] == "REFUSED":
        return _result(case_id, "ESCALATED_TO_HUMAN", "agent menolak menyusun draf: " + (a3["refusal_reason"] or ""), **base)
    appeal = a3["appeal"]

    approval = None
    for attempt in range(MAX_EDITS + 1):
        req = {"case_id": case_id, "appeal": appeal, "draft_hash": draft_hash(appeal),
               "score": sc, "flags": allflags, "minutes_left": _minutes_left(d["deadline_at"])}
        log("APPROVAL_REQUESTED", draft_hash=req["draft_hash"])
        dec = approver.decide(req)
        if dec is None:
            return _result(case_id, "ESCALATED_TO_HUMAN", "tidak ada keputusan penjual (approval belum diberikan)", **base)
        log("APPROVAL_DECISION", decision=dec["decision"], by=dec["decided_by"])
        if dec["decision"] == "REJECT":
            return _result(case_id, "CLOSED_NO_APPEAL", "penjual menolak draf", **base)
        if dec["decision"] == "EDIT":
            cand = dec["edited_appeal"]
            errs = validate_draft({"case_id": case_id, "status": "DRAFTED", "refusal_reason": None, "appeal": cand}, ev)
            if errs:
                log("EDIT_REJECTED_BY_VALIDATOR", errors=errs)
                return _result(case_id, "ESCALATED_TO_HUMAN", "hasil edit gagal validasi: " + errs[0], **base)
            appeal = cand  # edit lolos validasi -> minta approval ulang untuk hash baru
            continue
        approval = dec
        break
    else:
        return _result(case_id, "ESCALATED_TO_HUMAN", "terlalu banyak putaran edit", **base)

    # ---- pengajuan -----------------------------------------------------------------------
    try:
        filed = file_appeal(session, case_id, ctype, appeal, approval, ev, media_root,
                            _minutes_left(d["deadline_at"]))
    except GateError as e:
        log("GATE_BLOCKED", reason=str(e))
        return _result(case_id, "ESCALATED_TO_HUMAN", "gerbang memblokir pengajuan: %s" % e, **base)
    except RobotError as e:
        log("FILING_FAILED", reason=str(e))
        return _result(case_id, "ESCALATED_TO_HUMAN", "robot gagal mengajukan: %s" % e, **base)
    log("FILED", **filed)

    # ---- hasil ---------------------------------------------------------------------------
    outcome = None
    for _ in range(poll_tries):
        c, det = session.get_json("/api/disputes/%s" % case_id)
        if c == 200 and det["status"] in OUTCOME_STATES:
            outcome = det["status"]
            break
        time.sleep(poll_s)
    log("OUTCOME", outcome=outcome)
    return _result(case_id, "OUTCOME_RECORDED" if outcome else "FILED_AWAITING_OUTCOME",
                   "diajukan: %s" % filed["submission_ref"], outcome=outcome, filing=filed,
                   approval_id=approval["approval_id"], **base)
