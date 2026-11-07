"""Demo konsol satu kasus: python3 demo.py DSP-001 [--auto]   (persetujuan manusia ditanya lewat konsol)
Opsional server nyata: jalankan `DATASET=data/cases.json python3 marketmock/app.py`, lalu tambah --url http://127.0.0.1:5000
"""
import argparse, os, sys, warnings
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "eval"))
warnings.filterwarnings("ignore")
import run_eval
from pembela.approval import ScriptedApprover
from pembela.backends import BaselineBackend
from pembela.pipeline import AuditLog, run_case
from pembela.robot_http import render_appeal_text
from pembela.sessions import FlaskSession, HttpSession


class ConsoleApprover:
    def decide(self, req):
        print("\n=== DRAF BANDING (perlu persetujuanmu) ===")
        print(render_appeal_text(req["appeal"]))
        print("\nSkor: %(score)s (%(strength)s) | sisa waktu: %(m)s menit | hash: %(h)s" % {
            **req["score"], "m": req["minutes_left"], "h": req["draft_hash"][:12]})
        for f in req["flags"]:
            print("FLAG %s: %s" % (f["severity"], f["note"]))
        print("Catatan: hasil banding TIDAK dapat dipastikan.")
        if input("Ajukan sekarang? [y/N] ").strip().lower() == "y":
            return ScriptedApprover().decide(req)
        return ScriptedApprover({req["case_id"]: "reject"}).decide(req)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("case"); ap.add_argument("--auto", action="store_true"); ap.add_argument("--url")
    a = ap.parse_args()
    data = ROOT / "data"
    if a.url:
        import requests
        s = HttpSession(a.url, "seller_demo", "demo123", "demo-api-key")
    else:
        mm = run_eval.load_app(data / "cases.json")
        s = FlaskSession(mm.app, mm.USER, mm.PASSWORD, mm.API_KEY)
    n = int(a.case.split("-")[1]); audit = AuditLog()
    r = run_case(s, BaselineBackend(), ScriptedApprover() if a.auto else ConsoleApprover(), a.case, data / "media", n, audit)
    print("\n>>> STATUS AKHIR:", r["final_state"], "-", r["reason"])
    for row in audit.rows:
        print(" ", row["ts"][11:19], row["event"])
