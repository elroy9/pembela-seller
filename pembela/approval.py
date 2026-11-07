"""Persetujuan manusia. Di produksi: task Action Center. Di sini: ScriptedApprover untuk uji."""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta, timezone

WIB = timezone(timedelta(hours=7))


class GateError(Exception):
    """Pengajuan DIBLOKIR oleh gerbang (BR-01). Tidak ada banding yang boleh lolos tanpa approval valid."""


class ScriptedApprover:
    """actions: {case_id: 'approve'|'reject'|'none'|'edit_ok'|'edit_bad' | [urutan aksi]}; default 'approve'."""

    def __init__(self, actions=None):
        self.actions = {k: (list(v) if isinstance(v, list) else [v]) for k, v in (actions or {}).items()}

    def decide(self, req):
        seq = self.actions.get(req["case_id"], ["approve"])
        act = seq.pop(0) if len(seq) > 1 else seq[0]
        if act == "none":
            return None
        base = {"approval_id": "APR-" + uuid.uuid4().hex[:8].upper(), "case_id": req["case_id"],
                "decided_by": "HUMAN:seller", "decided_at": datetime.now(WIB).isoformat(timespec="seconds")}
        if act == "reject":
            return {**base, "decision": "REJECT", "approved_draft_hash": None}
        if act == "approve":
            return {**base, "decision": "APPROVE", "approved_draft_hash": req["draft_hash"]}
        ap = copy.deepcopy(req["appeal"])
        if act == "edit_ok":
            ap["requests"].append({"kind": "request", "text": "Mohon kabari penjual bila ada bukti tambahan yang diperlukan."})
        elif act == "edit_bad":  # klaim tanpa rujukan bukti + tuduhan -> harus ditolak validator
            ap["summary"].append({"kind": "claim", "text": "Pembeli terbukti berbohong.", "evidence_ids": []})
        else:
            raise ValueError(act)
        return {**base, "decision": "EDIT", "approved_draft_hash": None, "edited_appeal": ap}
