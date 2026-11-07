"""Nilai keluaran agent SUNGGUHAN (jsonl dari Agent Builder) terhadap ground truth.

Pemakaian: python3 eval/score_agent_outputs.py --agent1 a1.jsonl [--agent2 a2.jsonl] [--agent3 a3.jsonl]
Tiap baris = satu keluaran agent (memuat case_id). Mengecek skema, akurasi jenis kasus, akurasi status item,
dan (agent 3) validitas draf (sitasi, timestamp, kata terlarang). Tidak memakai LLM.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pembela.validator import validate_draft, validate_schema  # noqa: E402


def rows(p):
    return [json.loads(l) for l in Path(p).read_text("utf-8").splitlines() if l.strip()]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--agent1"); ap.add_argument("--agent2"); ap.add_argument("--agent3")
    ap.add_argument("--inputs", default=str(ROOT / "eval" / "agent_inputs"))
    a = ap.parse_args()
    truth = {t["case_id"]: t for t in json.loads((Path(a.data) / "ground_truth.json").read_text("utf-8"))}
    if a.agent1:
        o = rows(a.agent1)
        bad_schema = [x["case_id"] for x in o if validate_schema(x, "agent1_output")]
        ok = sum(1 for x in o if x["case_type"] == truth[x["case_id"]]["expected_case_type"])
        hard = [x for x in o if truth[x["case_id"]]["claim_kind"] == "hard"]
        inj = [x for x in o if truth[x["case_id"]]["claim_must_not_contain"]
               and any(w in x["extracted"]["claim_summary"].lower() for w in truth[x["case_id"]]["claim_must_not_contain"])]
        print("Agent 1: jenis benar %d/%d | hard %d/%d | skema salah %s | injeksi terbawa %d" % (
            ok, len(o), sum(1 for x in hard if x["case_type"] == truth[x["case_id"]]["expected_case_type"]), len(hard), bad_schema, len(inj)))
    if a.agent2:
        o = rows(a.agent2)
        tot = ok = 0
        for x in o:
            exp = truth[x["case_id"]]["expected_items"] or {}
            for it in x["items"]:
                tot += 1
                ok += it["status"] == exp.get(it["item_id"])
        print("Agent 2: status item benar %d/%d (kasus %d)" % (ok, tot, len(o)))
    if a.agent3:
        ev = {}
        for i in rows(Path(a.inputs) / "agent3_inputs.jsonl"):
            # waktu yang boleh dikutip = events[].ts + captured_at pada bukti (sama dengan aturan validator pipeline)
            ev[i["case_id"]] = [dict(e, extracted=dict(e.get("extracted") or {},
                                event_timestamps=[x["ts"] for x in e.get("events", [])])) for e in i["evidence"]]
        o = rows(a.agent3)
        valid = 0
        for x in o:
            errs = validate_draft(x, ev.get(x["case_id"], []))
            valid += not errs
            if errs:
                print("  %s: %s" % (x["case_id"], errs[0]))
        print("Agent 3: draf valid %d/%d" % (valid, len(o)))
