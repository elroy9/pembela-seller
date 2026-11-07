"""Tes konsol web (alur persetujuan lewat HTTP). Jalankan: python3 tests/test_console.py
Dipisah dari test_core.py karena konsol memakai satu instance mock global."""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "console"))
import server  # noqa: E402

c = server.mm.app.test_client()
H = lambda t: re.search(r'name="hash" value="(\w+)"', t).group(1)
page = lambda p: c.get(p).get_data(as_text=True)


class ConsoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        c.post("/console/reset")
        c.post("/console/process-all")
        cls.initial = {k: sum(1 for v in server.STORE.values() if v["state"] == k) for k in server.STATES}

    def test_dashboard_counts_match_eval(self):
        got = self.initial  # diambil sebelum tes lain mengubah status
        self.assertEqual((got["PENDING_APPROVAL"], got["ESCALATED_TO_HUMAN"], got["AWAITING_MORE_EVIDENCE"], got["CLOSED_NO_APPEAL"]), (8, 12, 4, 6))
        self.assertEqual(c.get("/console/").status_code, 200)

    def test_dashboard_tiles_cover_all_cases(self):
        h = page("/console/")
        nums = [int(x) for x in re.findall(r'class="n">(\d+)', h)]
        self.assertEqual(nums[0], 30)
        self.assertEqual(sum(nums[1:]), 30)   # tiap kasus masuk tepat satu kartu (termasuk "Tenggat lewat")

    def test_all_case_pages_render(self):
        for cid in server._ids():
            self.assertEqual(c.get("/console/case/" + cid).status_code, 200, cid)

    def test_stale_hash_cannot_approve(self):
        old = H(page("/console/case/DSP-003"))
        c.post("/console/case/DSP-003/note", data={"note": "Mohon dipertimbangkan."})
        c.post("/console/case/DSP-003/approve", data={"hash": old})
        self.assertEqual(server.STORE["DSP-003"]["state"], "PENDING_APPROVAL")
        self.assertEqual(server._detail("DSP-003")["status"], "OPEN")

    def test_forbidden_note_rejected(self):
        before = server.STORE["DSP-007"]["req"]["draft_hash"]
        c.post("/console/case/DSP-007/note", data={"note": "Pembeli berbohong dan menipu."})
        self.assertEqual(server.STORE["DSP-007"]["req"]["draft_hash"], before)

    def test_approve_files_once(self):
        h = H(page("/console/case/DSP-009"))
        c.post("/console/case/DSP-009/approve", data={"hash": h})
        self.assertEqual(server.STORE["DSP-009"]["state"], "FILED")
        c.post("/console/case/DSP-009/approve", data={"hash": h})  # kedua kali: tidak ada draf menunggu
        self.assertEqual(len(list((server.mm.UPLOADS / "DSP-009").glob("*"))) > 0, True)

    def test_reject_never_files(self):
        c.post("/console/case/DSP-013/reject")
        self.assertEqual(server.STORE["DSP-013"]["state"], "CLOSED_NO_APPEAL")
        self.assertEqual(server._detail("DSP-013")["status"], "OPEN")

    def test_no_pending_no_approve_button(self):
        self.assertNotIn('id="go"', page("/console/case/DSP-005"))  # menunggu bukti
        self.assertNotIn('id="go"', page("/console/case/DSP-026"))  # tanpa tenggat -> eskalasi


import io


def upload(cid, role, when, name="x.mp4", data=b"BYTES-ASLI"):
    return c.post("/console/case/%s/upload" % cid, data={"role": role, "captured_at": when, "file": (io.BytesIO(data), name)},
                  content_type="multipart/form-data")


def attest(cid, *pairs):
    return c.post("/console/case/%s/attest" % cid, data={"att": list(pairs)})


class EvidenceFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        c.post("/console/reset")
        c.post("/console/process-all")

    def test_upload_then_attest_makes_case_appealable(self):
        self.assertEqual(server.STORE["DSP-005"]["state"], "AWAITING_MORE_EVIDENCE")
        upload("DSP-005", "PACKING_VIDEO", "2026-10-21T10:00")          # sebelum diserahkan ke kurir (21 Okt 15:00)
        self.assertEqual(server.STORE["DSP-005"]["state"], "AWAITING_MORE_EVIDENCE")   # belum ada pernyataan: PARTIAL, critical belum MET
        ev = next(e for e in server.STORE["DSP-005"]["res"]["evidence"] if e["type"] == "PACKING_VIDEO")
        self.assertEqual(ev["status"], "COLLECTED")
        attest("DSP-005", ev["evidence_id"] + ":PACK_VIDEO_CONDITION", ev["evidence_id"] + ":WAYBILL_VISIBLE_IN_PACKING")
        st = server.STORE["DSP-005"]
        self.assertEqual(st["state"], "PENDING_APPROVAL")
        self.assertEqual(st["res"]["score"]["recommendation"], "APPEAL")
        h = H(page("/console/case/DSP-005"))
        c.post("/console/case/DSP-005/approve", data={"hash": h})
        self.assertEqual(server.STORE["DSP-005"]["state"], "FILED")

    def test_video_after_shipment_flagged(self):
        upload("DSP-006", "PACKING_VIDEO", "2026-10-30T10:00")          # jauh setelah barang diserahkan
        ev = next(e for e in server.STORE["DSP-006"]["res"]["evidence"] if e["type"] == "PACKING_VIDEO")
        attest("DSP-006", ev["evidence_id"] + ":PACK_VIDEO_CONDITION", ev["evidence_id"] + ":WAYBILL_VISIBLE_IN_PACKING")
        self.assertIn("PACKING_VIDEO_AFTER_SHIPMENT", [f["code"] for f in server.STORE["DSP-006"]["res"]["flags"]])

    def test_withdrawing_attestation_lowers_score(self):
        before = server.STORE["DSP-001"]["res"]["score"]["score"]
        attest("DSP-001")      # tidak mencentang apa pun
        after = server.STORE["DSP-001"]["res"]["score"]["score"]
        self.assertLess(after, before)
        self.assertNotIn("menyatakan video", str(server.STORE["DSP-001"]["req"]["appeal"]["summary"]) if server.STORE["DSP-001"].get("req") else "")

    def test_bad_uploads_rejected(self):
        n = len(list((server.MEDIA / server.mm.DISPUTES["DSP-002"]["order_id"]).glob("*")))
        upload("DSP-002", "PACKING_VIDEO", "2026-10-21T10:00", name="virus.exe")
        upload("DSP-002", "BUKAN_ROLE", "2026-10-21T10:00")
        upload("DSP-002", "PACKING_VIDEO", "bukan-tanggal")
        self.assertEqual(n, len(list((server.MEDIA / server.mm.DISPUTES["DSP-002"]["order_id"]).glob("*"))))

    def test_ambiguous_media_choose_moves_not_deletes(self):
        cid = "DSP-029"
        ev = next(e for e in server.STORE[cid]["res"]["evidence"] if e["status"] == "NEEDS_CONFIRMATION")
        keep, other = ev["extracted"]["candidates"]
        c.post("/console/case/%s/choose" % cid, data={"evidence_id": ev["evidence_id"], "keep": keep})
        folder = server.MEDIA / server.mm.DISPUTES[cid]["order_id"]
        self.assertTrue((folder / "_tidak_dipakai" / other).exists())
        self.assertFalse(any(e["status"] == "NEEDS_CONFIRMATION" for e in server.STORE[cid]["res"]["evidence"]))

    def test_zz_reset_restores_original_media(self):
        c.post("/console/reset")
        self.assertFalse((server.MEDIA / server.mm.DISPUTES["DSP-005"]["order_id"] / "packing_video.mp4").exists())


class OutcomeAndDuplicateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        c.post("/console/reset")
        c.post("/console/process-all")
        server.mm.RESOLVE_AFTER_S = 0

    def _approve(self, cid):
        c.post("/console/case/%s/approve" % cid, data={"hash": H(page("/console/case/" + cid))})
        self.assertEqual(server.STORE[cid]["state"], "FILED")
        return server._detail(cid)["status"]

    def test_outcome_follows_score(self):
        strong, medium = server.STORE["DSP-019"]["res"]["score"]["score"], server.STORE["DSP-021"]["res"]["score"]["score"]
        self.assertGreaterEqual(strong, 75)
        self.assertLess(medium, 75)
        self.assertEqual(self._approve("DSP-019"), "RESOLVED_WON")
        self.assertEqual(self._approve("DSP-021"), "RESOLVED_LOST")

    def test_result_page_shows_appeal_text(self):
        code, html = server.S.get_page("/disputes/DSP-019/result")
        self.assertIn("RINGKASAN", html)
        self.assertIn("simulasi", html.lower())

    def test_same_video_on_two_orders_flagged_in_console(self):
        upload("DSP-017", "PACKING_VIDEO", "2026-10-22T10:00", data=b"VIDEO-YANG-SAMA")
        upload("DSP-018", "PACKING_VIDEO", "2026-10-22T10:00", data=b"VIDEO-YANG-SAMA")
        codes = [f["code"] for f in server.STORE["DSP-018"]["res"]["flags"]]
        self.assertIn("DUPLICATE_ACROSS_ORDERS", codes)
        self.assertIn("disimpan di komputermu", page("/console/case/DSP-018"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
