"""Rubrik skor, kalkulator, dan detektor flag. Deterministik, tanpa LLM.

SEMUA bobot dan ambang di sini adalah ASUMSI awal. Kalibrasi dengan kasus nyata
(wawancara penjual) sebelum dipakai di luar demo.
"""
from __future__ import annotations

from datetime import datetime

# (item_id, bobot, critical, deskripsi)
CHECKLISTS = {
    "DAMAGED": [
        ("PACK_VIDEO_CONDITION", 35, True, "Video packing memperlihatkan kondisi barang sebelum disegel"),
        ("WAYBILL_VISIBLE_IN_PACKING", 20, False, "Resi terlihat bersama barang di video packing"),
        ("HANDOVER_PHOTO", 15, False, "Foto kemasan saat diserahkan ke kurir"),
        ("WAYBILL_RECORD", 10, False, "Data resi tersedia"),
        ("CHAT_LOG", 10, False, "Riwayat chat tersedia"),
        ("TRACKING_DELIVERED", 10, False, "Tracking mencatat status terkirim"),
    ],
    "NOT_AS_DESCRIBED": [
        ("PACK_VIDEO_VARIANT", 30, True, "Video packing memperlihatkan varian yang dikirim"),
        ("ORDER_RECORD_VARIANT", 20, True, "Catatan pesanan menunjukkan varian yang dipesan"),
        ("LISTING_SNAPSHOT", 20, False, "Tangkapan layar listing (varian/harga saat dibeli)"),
        ("CHAT_LOG", 20, False, "Riwayat chat tersedia"),
        ("WAYBILL_LABEL", 10, False, "Label/data resi tersedia"),
    ],
    "RETURN_EMPTY_OR_INCOMPLETE": [
        ("PACK_VIDEO_COMPLETENESS", 30, True, "Video packing memperlihatkan kelengkapan isi"),
        ("RETURN_RECEIPT_VIDEO", 25, True, "Video penerimaan paket retur"),
        ("WEIGHT_COMPARISON", 30, False, "Berat paket kirim vs berat paket retur"),
        ("TRACKING_RETURN", 10, False, "Tracking mencatat alur retur"),
        ("CHAT_LOG", 5, False, "Riwayat chat tersedia"),
    ],
    "NOT_RECEIVED": [
        ("TRACKING_DELIVERED", 40, True, "Tracking mencatat status terkirim"),
        ("DELIVERY_TIMESTAMP_CONSISTENT", 20, False, "Urutan waktu tracking konsisten"),
        ("CHAT_LOG", 15, False, "Riwayat chat tersedia"),
        ("HANDOVER_PROOF", 15, False, "Bukti serah-terima kurir"),
        ("WAYBILL_RECORD", 10, False, "Data resi tersedia"),
    ],
}

# item -> tipe bukti yang relevan
ITEM_TYPE = {
    "PACK_VIDEO_CONDITION": "PACKING_VIDEO", "WAYBILL_VISIBLE_IN_PACKING": "PACKING_VIDEO",
    "PACK_VIDEO_VARIANT": "PACKING_VIDEO", "PACK_VIDEO_COMPLETENESS": "PACKING_VIDEO",
    "HANDOVER_PHOTO": "HANDOVER_PHOTO", "HANDOVER_PROOF": "HANDOVER_PHOTO",
    "WAYBILL_RECORD": "WAYBILL", "WAYBILL_LABEL": "WAYBILL", "WEIGHT_COMPARISON": "WAYBILL",
    "CHAT_LOG": "CHAT_LOG",
    "TRACKING_DELIVERED": "TRACKING", "DELIVERY_TIMESTAMP_CONSISTENT": "TRACKING", "TRACKING_RETURN": "TRACKING",
    "LISTING_SNAPSHOT": "LISTING_SNAPSHOT", "ORDER_RECORD_VARIANT": "ORDER_RECORD",
    "RETURN_RECEIPT_VIDEO": "RETURN_RECEIPT_VIDEO",
}

# item yang menuntut klaim tentang ISI video -> butuh pernyataan penjual (isi video tidak dibaca di MVP)
VIDEO_CLAIM_ITEMS = {
    "PACK_VIDEO_CONDITION", "WAYBILL_VISIBLE_IN_PACKING", "PACK_VIDEO_VARIANT",
    "PACK_VIDEO_COMPLETENESS", "RETURN_RECEIPT_VIDEO",
}

VAL = {"MET": 1.0, "PARTIAL": 0.5, "NOT_MET": 0.0, "UNKNOWN": 0.0}
PENALTY = {"HIGH": 15, "MEDIUM": 7}
STRONG_MIN, MEDIUM_MIN = 75, 50
BUFFER_MIN = 30  # filing_buffer_minutes (ASUMSI demo)


def needed_types(case_type):
    return {ITEM_TYPE[i[0]] for i in CHECKLISTS[case_type]}


def _obtainable(item_id, evidence):
    typ = ITEM_TYPE[item_id]
    if any(e["type"] == typ and e["status"] == "COLLECTED" for e in evidence):
        return False
    for e in evidence:
        if e["type"] != typ:
            continue
        if e["status"] in ("FAILED", "NEEDS_CONFIRMATION"):
            return True
        if e["status"] == "MISSING" and e.get("recoverable"):
            return True
    return False


def checklist_for(case_type, evidence):
    return [
        {"item_id": i, "description": d, "weight": w, "critical": c, "obtainable": _obtainable(i, evidence)}
        for (i, w, c, d) in CHECKLISTS[case_type]
    ]


def _collectable(w, failed, crit_fail):
    """Mengumpulkan bukti hanya berguna bila SEMUA item critical yang gagal bisa diperoleh
    (melengkapi item non-critical tidak akan memperbaiki kegagalan critical)."""
    if crit_fail:
        return all(w[k]["obtainable"] for k in failed if w[k]["critical"])
    return any(w[k].get("obtainable") for k in failed)


def score(checklist, review_items, flags, minutes_left, buffer_min=BUFFER_MIN):
    """review_items: {item_id: status}; flags: list of {severity,...}."""
    w = {c["item_id"]: c for c in checklist}
    raw = sum(w[k]["weight"] * VAL[review_items.get(k, "UNKNOWN")] for k in w)
    raw -= sum(PENALTY.get(f["severity"], 0) for f in flags)
    raw = max(0, round(raw))
    failed = [k for k in w if review_items.get(k, "UNKNOWN") in ("NOT_MET", "UNKNOWN")]
    crit_fail = any(w[k]["critical"] for k in failed)
    strength = "STRONG" if raw >= STRONG_MIN else "MEDIUM" if raw >= MEDIUM_MIN else "WEAK"
    if crit_fail:
        strength = "WEAK"
    if strength == "STRONG" or (strength == "MEDIUM" and not crit_fail):
        rec = "APPEAL"
    elif _collectable(w, failed, crit_fail) and minutes_left is not None and minutes_left > buffer_min:
        rec = "COLLECT_MORE"
    else:
        rec = "DO_NOT_APPEAL"
    return {"score": raw, "strength": strength, "recommendation": rec,
            "failed_items": failed, "critical_failed": crit_fail}


def _ts(s):
    return datetime.fromisoformat(s) if s else None


def detect_flags(evidence):
    """Flag deterministik (tidak bergantung agent)."""
    flags = []
    by_type = {}
    for e in evidence:
        by_type.setdefault(e["type"], []).append(e)
    track = next((e for e in by_type.get("TRACKING", []) if e["status"] == "COLLECTED"), None)
    pick = _ts(track["extracted"].get("picked_up_at")) if track else None
    for v in by_type.get("PACKING_VIDEO", []):
        if v["status"] == "COLLECTED" and pick and v.get("captured_at"):
            if _ts(v["captured_at"]) > pick:
                ids = [v["evidence_id"]] + [track["evidence_id"]]
                flags.append({"code": "PACKING_VIDEO_AFTER_SHIPMENT", "severity": "HIGH", "evidence_ids": ids,
                              "note": "Waktu rekam video packing lebih baru dari waktu barang diserahkan ke kurir."})
    seen = {}
    for e in evidence:
        if e["status"] == "COLLECTED" and e.get("sha256") and e["type"] in (
                "PACKING_VIDEO", "HANDOVER_PHOTO", "LISTING_SNAPSHOT", "RETURN_RECEIPT_VIDEO"):
            seen.setdefault(e["sha256"], []).append(e["evidence_id"])
    for ids in seen.values():
        if len(ids) > 1:
            flags.append({"code": "DUPLICATE_FILE_HASH", "severity": "MEDIUM", "evidence_ids": sorted(ids),
                          "note": "Beberapa bukti berbeda memiliki isi berkas yang identik."})
    for e in evidence:
        foreign = (e.get("extracted") or {}).get("foreign_order_ids")
        if foreign:
            flags.append({"code": "EVIDENCE_FOR_OTHER_ORDER", "severity": "HIGH", "evidence_ids": [e["evidence_id"]],
                          "note": "Ada berkas di folder pesanan ini yang bernama nomor pesanan lain (dikecualikan)."})
    return flags


def cross_order_flags(evidence, hash_index, order_id):
    """Berkas yang isinya identik dengan berkas di pesanan LAIN -> flag HIGH (bukti sama dipakai untuk pesanan berbeda)."""
    flags = []
    for e in evidence:
        if e["status"] == "COLLECTED" and e.get("sha256") and hash_index:
            others = [o for o in hash_index.get(e["sha256"], []) if o.upper() != (order_id or "").upper()]
            if others:
                flags.append({"code": "DUPLICATE_ACROSS_ORDERS", "severity": "HIGH", "evidence_ids": [e["evidence_id"]],
                              "note": "Isi berkas ini identik dengan berkas pada pesanan lain (%s). Satu bukti tidak seharusnya dipakai untuk pesanan berbeda." % ", ".join(others)})
    return flags


def merge_flags(*lists):
    out, seen = [], set()
    for lst in lists:
        for f in lst:
            key = (f["code"], tuple(sorted(f["evidence_ids"])))
            if key not in seen:
                seen.add(key)
                out.append(f)
    return out
