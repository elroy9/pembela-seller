"""Ekspor input agent 1/2/3 (satu JSON per baris) untuk ditempel ke Evaluation Set di Agent Builder.

Input agent 2/3 dibuat dari pipeline dengan backend baseline (bukti dan checklist nyata dari data dummy).
Hasilnya: eval/agent_inputs/agentN_inputs.jsonl  +  agentN_expected.jsonl (jawaban acuan dari ground truth).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "eval"))
import run_eval  # noqa: E402
from pembela.backends import BaselineBackend, RecordingBackend  # noqa: E402

class OracleTriage(BaselineBackend):
    """Hanya untuk ekspor: jenis kasus diambil dari ground truth agar input agent 2/3 tersedia untuk SEMUA kasus."""
    def __init__(self, truth):
        self.t = {t["case_id"]: t for t in truth}

    def triage(self, inp):
        o = super().triage(inp)
        et = self.t[inp["case_id"]]["expected_case_type"]
        o["case_type"], o["confidence"] = et, 0.95
        o["route"] = "CONTINUE" if et != "UNKNOWN" and inp["deadline_at"] else "ESCALATE"
        return o


if __name__ == "__main__":
    data = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data"
    truth0 = json.loads((data / "ground_truth.json").read_text("utf-8"))
    rec = RecordingBackend(OracleTriage(truth0))
    truth, res, _, _ = run_eval.run(data, rec)
    out = ROOT / "eval" / "agent_inputs"
    out.mkdir(exist_ok=True)
    tm = {t["case_id"]: t for t in truth}
    for name in ("agent1", "agent2", "agent3"):
        with open(out / ("%s_inputs.jsonl" % name), "w", encoding="utf-8") as f:
            for i in rec.rec[name]:
                f.write(json.dumps(i, ensure_ascii=False) + "\n")
    with open(out / "agent1_expected.jsonl", "w", encoding="utf-8") as f:
        for t in truth:
            f.write(json.dumps({"case_id": t["case_id"], "case_type": t["expected_case_type"],
                                "route": "CONTINUE" if t["expected_case_type"] != "UNKNOWN" else "ESCALATE"}) + "\n")
    with open(out / "agent2_expected.jsonl", "w", encoding="utf-8") as f:
        for i in rec.rec["agent2"]:
            t = tm[i["case_id"]]
            f.write(json.dumps({"case_id": i["case_id"], "items": t["expected_items"]}, ensure_ascii=False) + "\n")
    ex = out / "contoh_keluaran_baseline"
    ex.mkdir(exist_ok=True)
    for name in ("agent1", "agent2", "agent3"):
        with open(ex / ("%s_outputs.jsonl" % name), "w", encoding="utf-8") as f:
            for o in rec.out[name]:
                f.write(json.dumps(o, ensure_ascii=False) + "\n")
    print("Tertulis di", out, {k: len(v) for k, v in rec.rec.items()})
    print("Catatan: deadline_at/minutes_left di input relatif terhadap waktu ekspor; ekspor ulang bila sudah lama.")
