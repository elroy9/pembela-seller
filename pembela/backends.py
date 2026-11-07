"""Backend agent.

- BaselineBackend : stand-in deterministik (aturan + template). BUKAN LLM. Dipakai agar pipeline bisa
                    dijalankan dan diuji end-to-end tanpa tenant UiPath. Angkanya BUKAN akurasi agent LLM.
- FileBackend     : membaca keluaran agent sungguhan (jsonl hasil Agent Builder) per case_id.
- RecordingBackend: membungkus backend lain dan merekam input tiap agent (untuk ekspor evaluation set).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .rubric import ITEM_TYPE, VIDEO_CLAIM_ITEMS, score

_PAT = {
    "NOT_RECEIVED": re.compile(r"(tidak sampai|belum (datang|sampai|diterima|terima)|tidak diterima|belum nyampe)", re.I),
    "DAMAGED": re.compile(r"(pecah|rusak|cacat|penyok|retak|patah|hancur)", re.I),
    "NOT_AS_DESCRIBED": re.compile(r"(warna|varian|ukuran|tidak sesuai|salah kirim|beda|bukan yang)", re.I),
    "RETURN_EMPTY_OR_INCOMPLETE": re.compile(r"(retur|kosong|isi paket|dikirim balik|dikembalikan)", re.I),
}
_INJECTION = re.compile(r"(abaikan|ignore|sistem:|system:|instruksi)", re.I)


def _clean_claim(text):
    sents = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(s for s in sents if not _INJECTION.search(s))[:200]


class BaselineBackend:
    # ------------------------------------------------------------------ agent 1
    def triage(self, inp):
        claim = inp["buyer_claim_text"]
        ctx = inp["context"]
        hits = [t for t, p in _PAT.items() if p.search(claim)]
        if ctx["has_return_flow"] and "RETURN_EMPTY_OR_INCOMPLETE" in hits:
            hits = ["RETURN_EMPTY_OR_INCOMPLETE"]
        elif not ctx["has_return_flow"] and "RETURN_EMPTY_OR_INCOMPLETE" in hits:
            hits.remove("RETURN_EMPTY_OR_INCOMPLETE")
        missing = [k for k in ("order_id", "order_value_idr", "deadline_at") if inp.get(k) is None]
        if len(hits) == 1:
            ctype, conf = hits[0], 0.9
        elif len(hits) > 1:
            ctype, conf = "UNKNOWN", 0.4
        else:
            ctype, conf = "UNKNOWN", 0.3
        route = "CONTINUE" if ctype != "UNKNOWN" and not missing and conf >= 0.6 else "ESCALATE"
        return {
            "case_id": inp["case_id"], "case_type": ctype, "confidence": conf,
            "extracted": {"order_id": inp["order_id"], "order_value_idr": inp["order_value_idr"],
                          "deadline_at": inp["deadline_at"],
                          "claim_summary": "Pembeli menyatakan: " + _clean_claim(claim)},
            "missing_fields": missing, "route": route,
            "rationale": "Klasifikasi berbasis kata kunci pada teks klaim."[:240],
        }

    # ------------------------------------------------------------------ agent 2
    @staticmethod
    def item_status(item_id, evidence):
        typ = ITEM_TYPE[item_id]
        rel = [e for e in evidence if e["type"] == typ]
        col = [e for e in rel if e["status"] == "COLLECTED"]
        if not col:
            if any(e["status"] in ("FAILED", "NEEDS_CONFIRMATION") for e in rel):
                return "UNKNOWN", [], "Bukti belum dapat dipakai (gagal diambil atau perlu konfirmasi)."
            return "NOT_MET", [], "Bukti tidak tersedia."
        e = col[0]
        ids, ex = [e["evidence_id"]], e.get("extracted") or {}
        if item_id in VIDEO_CLAIM_ITEMS:
            if item_id in e.get("seller_attestations", []):
                return "MET", ids, "Pernyataan penjual atas isi video."
            return "PARTIAL", ids, "Berkas ada, isi belum dikonfirmasi penjual."
        if item_id == "WEIGHT_COMPARISON":
            s, r = ex.get("shipped_weight_kg"), ex.get("return_weight_kg")
            if not s or r is None:
                return "UNKNOWN", ids, "Data berat tidak lengkap."
            ratio = r / s
            st = "MET" if ratio < 0.7 else "PARTIAL" if ratio < 0.95 else "NOT_MET"
            return st, ids, "Rasio berat retur/kirim %.2f." % ratio
        if item_id == "TRACKING_DELIVERED":
            return ("MET" if ex.get("has_delivered") else "NOT_MET"), ids, "Status DELIVERED %s." % (
                "tercatat" if ex.get("has_delivered") else "tidak tercatat")
        if item_id == "DELIVERY_TIMESTAMP_CONSISTENT":
            c = ex.get("timestamps_consistent")
            return ("UNKNOWN" if c is None else "MET" if c else "NOT_MET"), ids, "Urutan waktu tracking."
        if item_id == "TRACKING_RETURN":
            return ("MET" if ex.get("has_return_events") else "PARTIAL"), ids, "Alur retur pada tracking."
        return "MET", ids, "Bukti tersedia."

    def review(self, inp):
        items = []
        for c in inp["checklist"]:
            st, ids, note = self.item_status(c["item_id"], inp["evidence"])
            items.append({"item_id": c["item_id"], "status": st, "evidence_ids": ids, "note": note[:200]})
        statuses = {i["item_id"]: i["status"] for i in items}
        sc = score(inp["checklist"], statuses, [], inp["minutes_left"])
        missing = [{"item_id": i["item_id"], "suggestion": "Cari/unggah ulang bukti yang sah dan nyata."}
                   for i in items if i["status"] in ("NOT_MET", "UNKNOWN")
                   and next(c for c in inp["checklist"] if c["item_id"] == i["item_id"])["obtainable"]]
        return {"case_id": inp["case_id"], "items": items, "consistency_flags": [],
                "missing_evidence": missing, "strength_proposal": sc["strength"],
                "recommendation_proposal": sc["recommendation"],
                "summary": "Skor usulan %d (%s)." % (sc["score"], sc["strength"])}

    # ------------------------------------------------------------------ agent 3
    _TEXT = {
        "PACK_VIDEO_CONDITION": {"MET": "Penjual menyatakan video packing ({ids}) memperlihatkan kondisi barang sebelum disegel.",
                                 "PARTIAL": "Terdapat video packing untuk pesanan ini ({ids}); isinya belum dikonfirmasi penjual."},
        "WAYBILL_VISIBLE_IN_PACKING": {"MET": "Penjual menyatakan resi terlihat bersama barang pada video packing ({ids})."},
        "PACK_VIDEO_VARIANT": {"MET": "Penjual menyatakan video packing ({ids}) memperlihatkan varian yang dikirim.",
                               "PARTIAL": "Terdapat video packing untuk pesanan ini ({ids}); isinya belum dikonfirmasi penjual."},
        "PACK_VIDEO_COMPLETENESS": {"MET": "Penjual menyatakan video packing ({ids}) memperlihatkan kelengkapan isi paket.",
                                    "PARTIAL": "Terdapat video packing untuk pesanan ini ({ids}); isinya belum dikonfirmasi penjual."},
        "RETURN_RECEIPT_VIDEO": {"MET": "Penjual menyatakan video penerimaan retur ({ids}) memperlihatkan kondisi isi paket retur.",
                                 "PARTIAL": "Terdapat video penerimaan retur ({ids}); isinya belum dikonfirmasi penjual."},
        "HANDOVER_PHOTO": {"MET": "Foto serah-terima ke kurir ({ids}) tersedia untuk pesanan ini."},
        "HANDOVER_PROOF": {"MET": "Bukti serah-terima kurir ({ids}) tersedia untuk pesanan ini."},
        "WAYBILL_RECORD": {"MET": "Data resi ({ids}) tercatat untuk pesanan ini."},
        "WAYBILL_LABEL": {"MET": "Data resi ({ids}) tercatat untuk pesanan ini."},
        "CHAT_LOG": {"MET": "Riwayat chat pesanan ({ids}) tersedia."},
        "TRACKING_DELIVERED": {"MET": "Riwayat tracking ({ids}) mencatat status DELIVERED."},
        "DELIVERY_TIMESTAMP_CONSISTENT": {"MET": "Urutan waktu pada riwayat tracking ({ids}) konsisten."},
        "TRACKING_RETURN": {"MET": "Riwayat tracking ({ids}) mencatat alur retur.",
                            "PARTIAL": "Riwayat tracking ({ids}) tersedia, tanpa catatan alur retur."},
        "WEIGHT_COMPARISON": {"MET": "Data resi ({ids}) mencatat berat paket retur jauh lebih ringan dari berat paket kirim.",
                              "PARTIAL": "Data resi ({ids}) mencatat berat paket retur sedikit lebih ringan dari berat paket kirim."},
        "LISTING_SNAPSHOT": {"MET": "Tangkapan layar listing ({ids}) tersedia."},
        "ORDER_RECORD_VARIANT": {"MET": "Catatan pesanan ({ids}) menunjukkan varian yang dipesan."},
    }

    def draft(self, inp):
        rev = inp["review"]
        st = {i["item_id"]: i for i in rev["items"]}
        crit_bad = [c for c in rev.get("critical_items", []) if st.get(c, {}).get("status") not in ("MET", "PARTIAL")]
        if rev["recommendation"] != "APPEAL":
            return self._refuse(inp, "Rekomendasi peninjau bukan APPEAL.")
        if crit_bad:
            return self._refuse(inp, "Item critical tidak terpenuhi: %s." % ", ".join(crit_bad))
        if not inp["evidence"]:
            return self._refuse(inp, "Tidak ada bukti pendukung di input.")
        summary, used = [], []
        for it in rev["items"]:
            tpl = self._TEXT.get(it["item_id"], {}).get(it["status"])
            if tpl and it["evidence_ids"]:
                summary.append({"kind": "claim", "text": tpl.format(ids=", ".join(it["evidence_ids"])),
                                "evidence_ids": it["evidence_ids"]})
                used += [e for e in it["evidence_ids"] if e not in used]
        if not summary:
            return self._refuse(inp, "Tidak ada klaim yang dapat dirujuk ke bukti.")
        label = {e["evidence_id"]: e["label"] for e in inp["evidence"]}
        timeline = []
        for e in inp["evidence"]:
            if e["evidence_id"] not in used:
                continue
            for ev in e.get("events", []):
                timeline.append({"ts": ev["ts"], "event": ev["label"], "evidence_ids": [e["evidence_id"]]})
            if e.get("captured_at") and e["type"] in ("PACKING_VIDEO", "HANDOVER_PHOTO", "LISTING_SNAPSHOT",
                                                       "RETURN_RECEIPT_VIDEO", "ORDER_RECORD"):
                timeline.append({"ts": e["captured_at"], "event": "%s dibuat/direkam%s" % (
                    e["label"], " (waktu menurut pernyataan penjual)" if e.get("captured_at_source") == "SELLER_DECLARED" else ""),
                                 "evidence_ids": [e["evidence_id"]]})
        timeline.sort(key=lambda t: t["ts"])
        opt = (inp.get("requested_outcome_options") or ["pengembalian dana kepada penjual"])[0]
        return {"case_id": inp["case_id"], "status": "DRAFTED", "refusal_reason": None, "appeal": {
            "summary": summary, "timeline": timeline,
            "evidence_list": [{"evidence_id": i, "label": label[i]} for i in used],
            "requests": [{"kind": "request", "text":
                          "Mohon tinjau ulang sengketa ini berdasarkan bukti terlampir dan putuskan sesuai bukti: %s." % opt}]}}

    @staticmethod
    def _refuse(inp, reason):
        return {"case_id": inp["case_id"], "status": "REFUSED", "refusal_reason": reason, "appeal": None}


class FileBackend:
    """Baca keluaran agent dari jsonl (satu objek JSON per baris, memuat case_id)."""

    def __init__(self, agent1=None, agent2=None, agent3=None):
        self.data = {}
        for name, p in (("agent1", agent1), ("agent2", agent2), ("agent3", agent3)):
            self.data[name] = {}
            if p:
                for line in Path(p).read_text("utf-8").splitlines():
                    if line.strip():
                        o = json.loads(line)
                        self.data[name][o["case_id"]] = o

    def _get(self, name, inp):
        try:
            return self.data[name][inp["case_id"]]
        except KeyError:
            raise KeyError("tidak ada keluaran %s untuk %s" % (name, inp["case_id"]))

    def triage(self, inp):
        return self._get("agent1", inp)

    def review(self, inp):
        return self._get("agent2", inp)

    def draft(self, inp):
        return self._get("agent3", inp)


class RecordingBackend:
    def __init__(self, inner):
        self.inner = inner
        self.rec = {"agent1": [], "agent2": [], "agent3": []}
        self.out = {"agent1": [], "agent2": [], "agent3": []}

    def _go(self, name, fn, inp):
        self.rec[name].append(inp)
        o = fn(inp)
        self.out[name].append(o)
        return o

    def triage(self, inp):
        return self._go("agent1", self.inner.triage, inp)

    def review(self, inp):
        return self._go("agent2", self.inner.review, inp)

    def draft(self, inp):
        return self._go("agent3", self.inner.draft, inp)
