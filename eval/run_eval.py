"""Jalankan pipeline di 30 kasus dummy dan bandingkan dengan ground truth.

Backend default = BaselineBackend (aturan kata kunci + template; BUKAN LLM). Angka yang keluar menguji pipeline
dan menunjukkan di mana kata kunci gagal; BUKAN akurasi agent Agent Builder. Untuk menilai agent sungguhan,
pakai --agent1/--agent2/--agent3 (jsonl keluaran agent) atau eval/score_agent_outputs.py.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore")


def load_app(dataset):
    os.environ["DATASET"] = str(dataset)
    os.environ["RESOLVE_AFTER_S"] = "0"
    sys.path.insert(0, str(ROOT / "marketmock"))
    import importlib
    import app as mm
    importlib.reload(mm)
    return mm


def run(data_dir, backend, approver=None, chaos=None):
    from pembela.approval import ScriptedApprover
    from pembela.pipeline import AuditLog, run_case
    from pembela.sessions import FlaskSession
    data_dir = Path(data_dir)
    mm = load_app(data_dir / "cases.json")
    if chaos:
        mm.STATE["chaos"] = set(chaos)
    s = FlaskSession(mm.app, mm.USER, mm.PASSWORD, mm.API_KEY)
    audit, res = AuditLog(), []
    truth = json.loads((data_dir / "ground_truth.json").read_text("utf-8"))
    for n, t in enumerate(truth, 1):
        try:
            r = run_case(s, backend, approver or ScriptedApprover(), t["case_id"], data_dir / "media", n, audit)
        except Exception as e:  # noqa
            r = {"case_id": t["case_id"], "final_state": "CRASH", "reason": repr(e)}
        res.append(r)
    return truth, res, audit, mm


def compare(truth, res):
    rows = []
    for t, r in zip(truth, res):
        sc = r.get("score") or {}
        tri = (r.get("triage") or {})
        rows.append({
            "case_id": t["case_id"], "tier": t["tier"], "claim_kind": t["claim_kind"],
            "type_ok": tri.get("case_type") == t["expected_case_type"],
            "strength_ok": sc.get("strength") == t["expected_strength"] if t["expected_strength"] else None,
            "rec_ok": sc.get("recommendation") == t["expected_recommendation"] if t["expected_recommendation"] else None,
            "state_ok": r["final_state"] == t["expected_final_state"],
            "expected_state": t["expected_final_state"], "got_state": r["final_state"], "reason": r["reason"],
            "inj_ok": not any(w in ((tri.get("extracted") or {}).get("claim_summary", "")).lower()
                              for w in t["claim_must_not_contain"]),
        })
    return rows


def report(rows, backend_name):
    n = len(rows)
    ok = lambda k: sum(1 for r in rows if r[k] is True)
    tot = lambda k: sum(1 for r in rows if r[k] is not None)
    hard = [r for r in rows if r["claim_kind"] == "hard"]
    L = ["# Laporan evaluasi (30 kasus dummy)", "",
         "Backend: **%s**. Ini baseline berbasis aturan, BUKAN LLM: angka di bawah menguji pipeline, bukan akurasi agent." % backend_name,
         "Ground truth dihitung dari rubrik yang sama (ASUMSI) -> uji konsistensi, bukan validasi dunia nyata.", "",
         "| Metrik | Hasil |", "|---|---|",
         "| Jenis kasus benar (triase) | %d/%d |" % (ok("type_ok"), n),
         "| ...pada klaim 'hard' (parafrase tanpa kata kunci) | %d/%d |" % (sum(r["type_ok"] for r in hard), len(hard)),
         "| Kekuatan bukti sesuai (kasus yang dieskalasi sebelum dinilai dihitung salah) | %d/%d |" % (ok("strength_ok"), tot("strength_ok")),
         "| Rekomendasi sesuai | %d/%d |" % (ok("rec_ok"), tot("rec_ok")),
         "| Status akhir sesuai | %d/%d |" % (ok("state_ok"), n),
         "| Injeksi prompt tidak terbawa | %d/%d |" % (ok("inj_ok"), n), "",
         "## Ketidaksesuaian", "", "| Kasus | Tier | Harapan | Hasil | Alasan |", "|---|---|---|---|---|"]
    for r in rows:
        if not r["state_ok"]:
            L.append("| %s | %s | %s | %s | %s |" % (r["case_id"], r["tier"], r["expected_state"], r["got_state"], r["reason"][:80]))
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--agent1"); ap.add_argument("--agent2"); ap.add_argument("--agent3")
    ap.add_argument("--out", default=str(ROOT / "eval" / "report.md"))
    a = ap.parse_args()
    from pembela.backends import BaselineBackend, FileBackend
    if a.agent1 or a.agent2 or a.agent3:
        be, name = FileBackend(a.agent1, a.agent2, a.agent3), "FileBackend (keluaran agent)"
    else:
        be, name = BaselineBackend(), "BaselineBackend"
    truth, res, audit, _ = run(a.data, be)
    rows = compare(truth, res)
    txt = report(rows, name)
    Path(a.out).write_text(txt, "utf-8")
    print(txt)
    print("Status akhir:", dict(Counter(r["final_state"] for r in res)))
