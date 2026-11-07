"""Robot pengajuan banding (stand-in HTTP untuk `PS_Appeal_File`).

Gerbang BR-01: TIDAK ADA pengajuan tanpa approval valid yang hash-nya sama dengan draf yang dikirim.
Versi UiPath wajib meniru semua pemeriksaan di `file_appeal` sebelum membuka browser.
"""
from __future__ import annotations

import time
from pathlib import Path

from .approval import GateError
from .evidence import MEDIA_TYPES, read_file
from .rubric import BUFFER_MIN
from .validator import draft_hash
import hashlib

REASON = {
    "DAMAGED": "goods_ok_before_shipping",
    "NOT_AS_DESCRIBED": "claim_not_matching_evidence",
    "RETURN_EMPTY_OR_INCOMPLETE": "return_not_matching",
    "NOT_RECEIVED": "delivered_per_tracking",
}
FILED_STATES = ("APPEAL_FILED", "RESOLVED_WON", "RESOLVED_LOST")


class RobotError(Exception):
    pass


def render_appeal_text(appeal):
    lines = ["RINGKASAN"]
    lines += ["- %s [%s]" % (s["text"], ", ".join(s["evidence_ids"])) for s in appeal["summary"]]
    lines += ["", "KRONOLOGI"]
    lines += ["- %s: %s [%s]" % (t["ts"], t["event"], ", ".join(t["evidence_ids"])) for t in appeal["timeline"]]
    lines += ["", "DAFTAR BUKTI"]
    lines += ["- %s: %s" % (e["evidence_id"], e["label"]) for e in appeal["evidence_list"]]
    lines += ["", "PERMINTAAN"]
    lines += ["- %s" % r["text"] for r in appeal["requests"]]
    return "\n".join(lines)


def file_appeal(session, case_id, case_type, appeal, approval, evidence, media_root,
                minutes_left, buffer_min=BUFFER_MIN, max_attempts=2):
    # ---- gerbang (semua harus lolos) ---------------------------------------------------
    if not approval or approval.get("decision") != "APPROVE":
        raise GateError("approval tidak ada atau bukan APPROVE")
    if approval.get("approved_draft_hash") != draft_hash(appeal):
        raise GateError("hash draf berubah setelah approval")
    for e in evidence:
        if e["status"] == "COLLECTED" and e["type"] in MEDIA_TYPES:
            if hashlib.sha256(read_file(media_root, e)).hexdigest() != e["sha256"]:
                raise GateError("integritas bukti gagal: %s berubah sejak intake" % e["evidence_id"])
    if minutes_left is None or minutes_left < buffer_min:
        raise GateError("sisa waktu %s menit < filing_buffer %d menit" % (minutes_left, buffer_min))
    code, det = session.get_json("/api/disputes/%s" % case_id)
    if code != 200 or det["status"] != "OPEN":
        raise GateError("status sengketa %s, bukan OPEN" % (det or {}).get("status"))
    # ---- pengajuan ---------------------------------------------------------------------
    files = [(Path(e["file_uri"]).name, read_file(media_root, e))
             for e in evidence if e["status"] == "COLLECTED" and e["type"] in MEDIA_TYPES]
    if not files:
        # Seller Center mewajibkan >=1 lampiran. Bila tidak ada berkas media, lampirkan ekspor data pesanan
        # APA ADANYA dari bukti yang sudah terkumpul (tracking/resi/chat). Tidak mengarang isi apa pun.
        import json as _json
        dump = {e["evidence_id"]: {"type": e["type"], "extracted": e["extracted"]}
                for e in evidence if e["status"] == "COLLECTED"}
        files = [("ekspor_data_%s.txt" % case_id, _json.dumps(dump, ensure_ascii=False, indent=1).encode("utf-8"))]
    text = render_appeal_text(appeal)
    attempts = 0
    for attempts in range(1, max_attempts + 1):
        code, _ = session.post_appeal(case_id, REASON[case_type], text, files)
        # idempoten: selalu cek status sebelum mengulang agar tidak ada pengajuan ganda
        c2, det = session.get_json("/api/disputes/%s" % case_id)
        if c2 == 200 and det["status"] in FILED_STATES:
            ap = det["appeal"]
            return {"submission_ref": ap["submission_ref"], "filed_at": ap["filed_at"],
                    "attempts": attempts, "http_status": code}
        if code in (502, 503, 504):
            time.sleep(0.2)
            continue
        raise RobotError("pengajuan gagal, HTTP %s" % code)
    raise RobotError("pengajuan gagal setelah %d percobaan" % attempts)
