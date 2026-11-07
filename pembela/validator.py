"""Validator skema + sitasi + hash draf. Berjalan di LUAR agent (lapis pengaman utama)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from .rubric import CHECKLISTS

SCHEMAS = json.loads((Path(__file__).resolve().parent.parent / "agents" / "schemas.json").read_text("utf-8"))
FORBIDDEN = ["menipu", "penipu", "bohong", "berbohong", "pembohong", "memalsukan", "scam"]


def validate_schema(obj, name):
    v = Draft202012Validator(SCHEMAS[name])
    return ["%s: %s" % (".".join(str(p) for p in e.absolute_path) or "(root)", e.message[:160])
            for e in sorted(v.iter_errors(obj), key=lambda e: list(e.absolute_path))]


def _ev_map(evidence):
    return {e["evidence_id"]: e for e in evidence}


def validate_review(review, case_type, evidence):
    errs = validate_schema(review, "agent2_output")
    if errs:
        return errs
    ev = _ev_map(evidence)
    want = {i[0] for i in CHECKLISTS[case_type]}
    got = [i["item_id"] for i in review["items"]]
    if set(got) != want or len(got) != len(set(got)):
        errs.append("items: harus tepat satu entri untuk tiap item checklist %s" % sorted(want))
    for it in review["items"]:
        for eid in it["evidence_ids"]:
            if eid not in ev:
                errs.append("items.%s: evidence_id tidak dikenal %s" % (it["item_id"], eid))
            elif ev[eid]["status"] != "COLLECTED":
                errs.append("items.%s: bukti %s tidak berstatus COLLECTED" % (it["item_id"], eid))
        if it["status"] in ("MET", "PARTIAL") and not it["evidence_ids"]:
            errs.append("items.%s: status %s tanpa evidence_ids" % (it["item_id"], it["status"]))
    for f in review["consistency_flags"]:
        for eid in f["evidence_ids"]:
            if eid not in ev:
                errs.append("flag %s: evidence_id tidak dikenal %s" % (f["code"], eid))
    return errs


def _all_text(appeal):
    parts = [s["text"] for s in appeal["summary"]] + [t["event"] for t in appeal["timeline"]]
    parts += [e["label"] for e in appeal["evidence_list"]] + [r["text"] for r in appeal["requests"]]
    return "\n".join(parts)


def validate_draft(out, evidence):
    errs = validate_schema(out, "agent3_output")
    if errs:
        return errs
    if out["status"] == "REFUSED":
        if out["appeal"] is not None:
            errs.append("status REFUSED harus appeal=null")
        return errs
    ev = _ev_map(evidence)
    ap = out["appeal"]
    if not ap["summary"]:
        errs.append("summary kosong")
    for i, c in enumerate(ap["summary"]):
        for eid in c["evidence_ids"]:
            if eid not in ev or ev[eid]["status"] != "COLLECTED":
                errs.append("summary[%d]: rujukan bukti tidak valid %s" % (i, eid))
    for i, t in enumerate(ap["timeline"]):
        allowed = set()
        for eid in t["evidence_ids"]:
            if eid not in ev or ev[eid]["status"] != "COLLECTED":
                errs.append("timeline[%d]: rujukan bukti tidak valid %s" % (i, eid))
                continue
            allowed |= set((ev[eid].get("extracted") or {}).get("event_timestamps", []))
            if ev[eid].get("captured_at"):
                allowed.add(ev[eid]["captured_at"])
        if t["ts"] not in allowed:
            errs.append("timeline[%d]: timestamp %s tidak berasal dari bukti yang dirujuk" % (i, t["ts"]))
    for e in ap["evidence_list"]:
        if e["evidence_id"] not in ev or ev[e["evidence_id"]]["status"] != "COLLECTED":
            errs.append("evidence_list: bukti tidak valid %s" % e["evidence_id"])
    low = _all_text(ap).lower()
    for w in FORBIDDEN:
        if w in low:
            errs.append("kata terlarang pada draf: '%s'" % w)
    return errs


def draft_hash(appeal):
    canon = json.dumps(appeal, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()
