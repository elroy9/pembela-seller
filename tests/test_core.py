"""Tes keselamatan & konsistensi inti PEMBELA SELLER. Jalankan: python3 tests/test_core.py"""
import copy
import json
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "datagen"))
sys.path.insert(0, str(ROOT / "eval"))
warnings.filterwarnings("ignore")

import generate_dummy  # noqa: E402
import run_eval  # noqa: E402
from pembela import rubric  # noqa: E402
from pembela.approval import GateError, ScriptedApprover  # noqa: E402
from pembela.backends import BaselineBackend  # noqa: E402
from pembela.evidence import collect  # noqa: E402
from pembela.pipeline import AuditLog, run_case  # noqa: E402
from pembela.robot_http import file_appeal  # noqa: E402
from pembela.sessions import FlaskSession  # noqa: E402
from pembela.validator import draft_hash, validate_draft  # noqa: E402

# kasus tetap (lihat build_specs): DSP-001 DAMAGED STRONG_A (kalimat mudah), DSP-005 WEAK_COLLECT, DSP-006 WEAK_NO
STRONG, WEAK_COLLECT, WEAK_NO = "DSP-001", "DSP-005", "DSP-006"


class Env:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        generate_dummy.generate(self.dir)
        self.mm = run_eval.load_app(self.dir / "cases.json")
        self.s = FlaskSession(self.mm.app, self.mm.USER, self.mm.PASSWORD, self.mm.API_KEY)
        self.media = self.dir / "media"
        self.truth = {t["case_id"]: t for t in json.loads((self.dir / "ground_truth.json").read_text("utf-8"))}

    def run(self, cid, approver=None, **kw):
        n = int(cid.split("-")[1])
        a = AuditLog()
        r = run_case(self.s, BaselineBackend(), approver or ScriptedApprover(), cid, self.media, n, a, **kw)
        return r, a

    def status(self, cid):
        return self.s.get_json("/api/disputes/%s" % cid)[1]["status"]

    def drafted(self, cid):
        """Jalankan sampai draf siap, lalu kembalikan (appeal, evidence, order id) tanpa mengajukan."""
        r, _ = self.run(cid, ScriptedApprover({cid: "none"}))
        assert r["final_state"] == "ESCALATED_TO_HUMAN" and "approval belum" in r["reason"], r["reason"]
        return r


class GateTests(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.r = self.e.drafted(STRONG)
        out = BaselineBackend().draft  # draf dibuat ulang lewat pipeline pada approval nyata di bawah

    def tearDown(self):
        self.e.tmp.cleanup()

    def _material(self):
        """Ambil appeal yang dihasilkan pipeline dengan menangkap permintaan approval."""
        cap = {}

        class Cap:
            def decide(s, req):
                cap["req"] = req
                return None
        self.e.mm.seed()
        run_case(self.e.s, BaselineBackend(), Cap(), STRONG, self.e.media, 1, AuditLog())
        return cap["req"], self.r["evidence"]

    def _approval(self, req, **kw):
        a = ScriptedApprover().decide(req)
        a.update(kw)
        return a

    def test_no_approval_blocked(self):
        req, ev = self._material()
        with self.assertRaises(GateError):
            file_appeal(self.e.s, STRONG, "DAMAGED", req["appeal"], None, ev, self.e.media, 400)
        self.assertEqual(self.e.status(STRONG), "OPEN")

    def test_reject_decision_blocked(self):
        req, ev = self._material()
        a = ScriptedApprover({STRONG: "reject"}).decide(req)
        with self.assertRaises(GateError):
            file_appeal(self.e.s, STRONG, "DAMAGED", req["appeal"], a, ev, self.e.media, 400)

    def test_hash_mismatch_blocked(self):
        req, ev = self._material()
        a = self._approval(req)
        changed = copy.deepcopy(req["appeal"])
        changed["requests"][0]["text"] += " Tambahan diam-diam."
        with self.assertRaises(GateError):
            file_appeal(self.e.s, STRONG, "DAMAGED", changed, a, ev, self.e.media, 400)
        self.assertEqual(self.e.status(STRONG), "OPEN")

    def test_tampered_evidence_blocked(self):
        req, ev = self._material()
        a = self._approval(req)
        f = next(e for e in ev if e["type"] == "PACKING_VIDEO")
        (self.e.media / f["file_uri"]).write_bytes(b"DIUBAH")
        with self.assertRaises(GateError) as c:
            file_appeal(self.e.s, STRONG, "DAMAGED", req["appeal"], a, ev, self.e.media, 400)
        self.assertIn("integritas", str(c.exception))
        self.assertEqual(self.e.status(STRONG), "OPEN")

    def test_deadline_buffer_blocked(self):
        req, ev = self._material()
        with self.assertRaises(GateError):
            file_appeal(self.e.s, STRONG, "DAMAGED", req["appeal"], self._approval(req), ev, self.e.media, 29)

    def test_valid_files_once_then_blocked(self):
        req, ev = self._material()
        a = self._approval(req)
        out = file_appeal(self.e.s, STRONG, "DAMAGED", req["appeal"], a, ev, self.e.media, 400)
        self.assertTrue(out["submission_ref"].startswith("SUB-"))
        with self.assertRaises(GateError):  # tidak boleh ada pengajuan ganda
            file_appeal(self.e.s, STRONG, "DAMAGED", req["appeal"], a, ev, self.e.media, 400)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.tmp.cleanup()

    def test_case_already_filed_outside_system_is_recognised(self):
        self.e.s.post_appeal(STRONG, "goods_ok_before_shipping", "Banding manual langsung di Seller Center", [("a.txt", b"x")])
        r, _ = self.e.run(STRONG)
        self.assertEqual(r["final_state"], "ALREADY_FILED")
        self.assertNotIn("score", r)   # tidak dianalisis sama sekali

    def test_no_decision_means_not_filed(self):
        r, _ = self.e.run(STRONG, ScriptedApprover({STRONG: "none"}))
        self.assertEqual(r["final_state"], "ESCALATED_TO_HUMAN")
        self.assertEqual(self.e.status(STRONG), "OPEN")

    def test_reject_closes_without_filing(self):
        r, _ = self.e.run(STRONG, ScriptedApprover({STRONG: "reject"}))
        self.assertEqual(r["final_state"], "CLOSED_NO_APPEAL")
        self.assertEqual(self.e.status(STRONG), "OPEN")

    def test_bad_edit_blocked_by_validator(self):
        r, a = self.e.run(STRONG, ScriptedApprover({STRONG: "edit_bad"}))
        self.assertEqual(r["final_state"], "ESCALATED_TO_HUMAN")
        self.assertEqual(self.e.status(STRONG), "OPEN")
        self.assertTrue(any(x["event"] == "EDIT_REJECTED_BY_VALIDATOR" for x in a.rows))

    def test_good_edit_needs_new_approval_then_files(self):
        r, a = self.e.run(STRONG, ScriptedApprover({STRONG: ["edit_ok", "approve"]}))
        self.assertEqual(r["final_state"], "OUTCOME_RECORDED")
        self.assertEqual(sum(1 for x in a.rows if x["event"] == "APPROVAL_REQUESTED"), 2)

    def test_timeout_retry_files_exactly_once(self):
        self.e.mm.STATE["chaos"] = {"timeout"}
        r, a = self.e.run(STRONG)
        self.assertEqual(r["final_state"], "OUTCOME_RECORDED")
        self.assertEqual(r["filing"]["attempts"], 2)
        self.assertEqual(len(list((self.e.mm.UPLOADS / STRONG).glob("*"))) > 0, True)

    def test_collect_more_and_no_appeal(self):
        self.assertEqual(self.e.run(WEAK_COLLECT)[0]["final_state"], "AWAITING_MORE_EVIDENCE")
        self.assertEqual(self.e.run(WEAK_NO)[0]["final_state"], "CLOSED_NO_APPEAL")
        self.assertEqual(self.e.status(WEAK_COLLECT), "OPEN")

    def test_injection_text_not_propagated(self):
        r, _ = self.e.run("DSP-027")
        self.assertNotIn("abaikan", r["triage"]["extracted"]["claim_summary"].lower())
        self.assertEqual(r["final_state"], "CLOSED_NO_APPEAL")

    def test_no_deadline_escalates_never_defaults(self):
        r, _ = self.e.run("DSP-026")
        self.assertEqual(r["final_state"], "ESCALATED_TO_HUMAN")
        self.assertIn("tenggat", r["reason"])

    def test_near_deadline_escalates(self):
        r, _ = self.e.run("DSP-030")
        self.assertEqual(r["final_state"], "ESCALATED_TO_HUMAN")
        self.assertEqual(self.e.status("DSP-030"), "OPEN")

    def test_tracking_503_is_failed_not_missing(self):
        r, _ = self.e.run("DSP-021")  # NOT_RECEIVED MEDIUM_A: tidak ada faults -> baseline
        self.assertEqual(r["final_state"], "OUTCOME_RECORDED")
        r2, _ = self.e.run("DSP-023")  # NOT_RECEIVED WEAK_COLLECT: tracking 503
        ev = {e["type"]: e for e in r2["evidence"]}
        self.assertEqual(ev["TRACKING"]["status"], "FAILED")
        self.assertTrue(ev["TRACKING"]["recoverable"])
        self.assertEqual(r2["final_state"], "AWAITING_MORE_EVIDENCE")

    def test_all_non_hard_cases_match_ground_truth(self):
        truth, res, _, _ = run_eval.run(self.e.dir, BaselineBackend())
        bad = [(t["case_id"], t["expected_final_state"], r["final_state"], r["reason"])
               for t, r in zip(truth, res) if t["claim_kind"] != "hard" and r["final_state"] != t["expected_final_state"]]
        self.assertEqual(bad, [])

    def test_hard_claims_expose_keyword_baseline_limit(self):
        truth, res, _, _ = run_eval.run(self.e.dir, BaselineBackend())
        hard = [r for t, r in zip(truth, res) if t["claim_kind"] == "hard" and t["case_id"] != "DSP-028"]
        self.assertTrue(all(r["triage"]["case_type"] == "UNKNOWN" for r in hard if "triage" in r))


class CrossOrderTests(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        from pembela.evidence import build_hash_index
        self.build = build_hash_index

    def tearDown(self):
        self.e.tmp.cleanup()

    def _run(self, cid):
        n = int(cid.split("-")[1])
        return run_case(self.e.s, BaselineBackend(), ScriptedApprover({cid: "none"}), cid, self.e.media, n, AuditLog(),
                        hash_index=self.build(self.e.media))

    def test_unique_files_no_cross_flag(self):
        r = self._run(STRONG)
        self.assertNotIn("DUPLICATE_ACROSS_ORDERS", [f["code"] for f in r["flags"]])

    def test_same_video_on_other_order_is_flagged_and_lowers_score(self):
        base = self._run(STRONG)["score"]["score"]
        oid1 = self.e.mm.DISPUTES[STRONG]["order_id"]
        oid2 = self.e.mm.DISPUTES["DSP-002"]["order_id"]
        import shutil
        shutil.copy(self.e.media / oid1 / "packing_video.mp4", self.e.media / oid2 / "packing_video.mp4")
        r = self._run(STRONG)
        self.assertIn("DUPLICATE_ACROSS_ORDERS", [f["code"] for f in r["flags"]])
        self.assertEqual(base - r["score"]["score"], 15)
        self.assertIn(oid2, [f["note"] for f in r["flags"] if f["code"] == "DUPLICATE_ACROSS_ORDERS"][0])


class EvidenceFlagTests(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.oid = json.loads((self.e.dir / "cases.json").read_text("utf-8"))[0]["order_id"]

    def tearDown(self):
        self.e.tmp.cleanup()

    def _flags(self):
        self.e.mm.seed()
        ev = collect(self.e.s, STRONG, self.oid, "DAMAGED", self.e.media, 1)
        return rubric.detect_flags(ev), ev

    def test_clean_case_has_no_flags(self):
        self.assertEqual(self._flags()[0], [])

    def test_packing_video_after_shipment_is_high_flag(self):
        mp = self.e.media / self.oid / "meta.json"
        m = json.loads(mp.read_text("utf-8"))
        m["files"]["packing_video.mp4"]["captured_at"] = "2026-12-31T10:00:00+07:00"
        mp.write_text(json.dumps(m), "utf-8")
        flags, _ = self._flags()
        self.assertEqual([f["code"] for f in flags], ["PACKING_VIDEO_AFTER_SHIPMENT"])
        self.assertEqual(flags[0]["severity"], "HIGH")

    def test_foreign_order_file_is_excluded_and_flagged(self):
        (self.e.media / self.oid / "packing_MM-999999-0001.mp4").write_bytes(b"x")
        flags, ev = self._flags()
        self.assertIn("EVIDENCE_FOR_OTHER_ORDER", [f["code"] for f in flags])
        self.assertEqual(sum(1 for e in ev if e["type"] == "PACKING_VIDEO" and e["status"] == "COLLECTED"), 1)

    def test_duplicate_hash_flagged(self):
        d = self.e.media / self.oid
        (d / "handover_photo.jpg").write_bytes((d / "packing_video.mp4").read_bytes())
        self.assertIn("DUPLICATE_FILE_HASH", [f["code"] for f in self._flags()[0]])

    def test_originals_never_modified(self):
        import hashlib
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.e.media / self.oid).iterdir()}
        self._flags()
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.e.media / self.oid).iterdir()}
        self.assertEqual(before, after)


class ValidatorRubricTests(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        r, _ = self.e.run(STRONG, ScriptedApprover({STRONG: "none"}))
        self.ev = r["evidence"]
        cap = {}

        class Cap:
            def decide(s, req):
                cap["a"] = req["appeal"]
        self.e.mm.seed()
        run_case(self.e.s, BaselineBackend(), Cap(), STRONG, self.e.media, 1, AuditLog())
        self.ap = cap["a"]

    def tearDown(self):
        self.e.tmp.cleanup()

    def wrap(self, ap):
        return {"case_id": STRONG, "status": "DRAFTED", "refusal_reason": None, "appeal": ap}

    def test_clean_draft_valid(self):
        self.assertEqual(validate_draft(self.wrap(self.ap), self.ev), [])

    def test_forbidden_word(self):
        ap = copy.deepcopy(self.ap)
        ap["summary"][0]["text"] += " Pembeli berbohong."
        self.assertTrue(any("terlarang" in x for x in validate_draft(self.wrap(ap), self.ev)))

    def test_invented_timestamp(self):
        ap = copy.deepcopy(self.ap)
        ap["timeline"].append({"ts": "2026-01-01T00:00:00+07:00", "event": "Karangan", "evidence_ids": [ap["evidence_list"][0]["evidence_id"]]})
        self.assertTrue(any("timestamp" in x for x in validate_draft(self.wrap(ap), self.ev)))

    def test_citation_to_unknown_evidence(self):
        ap = copy.deepcopy(self.ap)
        ap["summary"][0]["evidence_ids"] = ["EV-99-99"]
        self.assertTrue(validate_draft(self.wrap(ap), self.ev))

    def test_hash_changes_with_any_edit(self):
        ap2 = copy.deepcopy(self.ap)
        ap2["requests"][0]["text"] += "."
        self.assertNotEqual(draft_hash(self.ap), draft_hash(ap2))

    def test_critical_failure_caps_strength(self):
        cl = rubric.checklist_for("DAMAGED", [])
        st = {c["item_id"]: "MET" for c in cl}
        st["PACK_VIDEO_CONDITION"] = "NOT_MET"
        sc = rubric.score(cl, st, [], 400)
        self.assertEqual(sc["strength"], "WEAK")
        self.assertEqual(sc["recommendation"], "DO_NOT_APPEAL")

    def test_collect_more_needs_critical_obtainable(self):
        cl = rubric.checklist_for("NOT_RECEIVED", [])
        for c in cl:
            c["obtainable"] = c["item_id"] == "HANDOVER_PROOF"   # hanya item non-critical yang bisa dicari
        st = {c["item_id"]: "MET" for c in cl}
        st["TRACKING_DELIVERED"], st["HANDOVER_PROOF"] = "NOT_MET", "NOT_MET"
        self.assertEqual(rubric.score(cl, st, [], 400)["recommendation"], "DO_NOT_APPEAL")

    def test_high_flag_lowers_score(self):
        cl = rubric.checklist_for("DAMAGED", [])
        st = {c["item_id"]: "MET" for c in cl}
        a = rubric.score(cl, st, [], 400)["score"]
        b = rubric.score(cl, st, [{"severity": "HIGH"}], 400)["score"]
        self.assertEqual(a - b, 15)


class GeneratorTests(unittest.TestCase):
    def test_deterministic_and_complete(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            c1, t1 = generate_dummy.generate(a, 7)
            c2, t2 = generate_dummy.generate(b, 7)
            self.assertEqual(c1, c2)
            self.assertEqual(len(c1), 30)
            self.assertTrue(all("_truth" not in c and "expected_final_state" not in c for c in c1))
            self.assertEqual(len({c["order_id"] for c in c1}), 30)


if __name__ == "__main__":
    unittest.main(verbosity=1)
