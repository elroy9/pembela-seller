"""Pengumpul bukti (stand-in untuk robot UiPath `PS_Evidence_Collect`).

Sumber: (1) API Seller Center tiruan, (2) folder media lokal (video/foto packing).
Aturan: berkas asli TIDAK diubah. Hanya hash dan metadata yang ditambahkan.
Isi video tidak dibaca di MVP; pernyataan penjual dibaca dari meta.json (seller_attestations).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .rubric import needed_types

MEDIA_EXT = {".mp4", ".jpg", ".jpeg", ".png", ".pdf"}
ORDER_RE = re.compile(r"MM-\d{6}-\d{4}", re.I)
ROLE_PATTERNS = [
    ("RETURN_RECEIPT_VIDEO", re.compile(r"(retur|return)", re.I)),
    ("HANDOVER_PHOTO", re.compile(r"(handover|serah)", re.I)),
    ("LISTING_SNAPSHOT", re.compile(r"(listing|snapshot)", re.I)),
    ("PACKING_VIDEO", re.compile(r"(pack|packing)", re.I)),
]
API_PART = {"WAYBILL": "waybill", "TRACKING": "tracking", "CHAT_LOG": "chat", "ORDER_RECORD": "listing"}
TYPE_ORDER = ["WAYBILL", "TRACKING", "CHAT_LOG", "ORDER_RECORD",
              "PACKING_VIDEO", "HANDOVER_PHOTO", "LISTING_SNAPSHOT", "RETURN_RECEIPT_VIDEO"]
MEDIA_TYPES = {"PACKING_VIDEO", "HANDOVER_PHOTO", "LISTING_SNAPSHOT", "RETURN_RECEIPT_VIDEO"}


def role_of(name):
    for role, pat in ROLE_PATTERNS:
        if pat.search(Path(name).stem):
            return role
    return None


def _find_files(root, order_id):
    files, foreign = [], []
    if not root.exists():
        return files, foreign
    for p in sorted(root.iterdir()):
        if p.is_dir() and p.name.lower() == order_id.lower():
            for f in sorted(p.iterdir()):
                if not (f.is_file() and f.suffix.lower() in MEDIA_EXT):
                    continue
                other = [m for m in ORDER_RE.findall(f.name) if m.lower() != order_id.lower()]
                if other:
                    foreign.append(f.name)
                else:
                    files.append(f)
        elif p.is_file() and p.suffix.lower() in MEDIA_EXT and order_id.lower() in p.name.lower():
            files.append(p)
    return files, foreign


def _load_meta(root, order_id):
    for p in (root / order_id / "meta.json", root / (order_id + ".meta.json")):
        if p.exists():
            return json.loads(p.read_text("utf-8"))
    return {"files": {}, "declared_unavailable": []}


def collect(session, dispute_id, order_id, case_type, media_root, case_no):
    root = Path(media_root)
    need = needed_types(case_type)
    out, counter = [], [0]
    state = {"return_received": False}

    def mk(typ, status, **kw):
        counter[0] += 1
        e = {"evidence_id": "EV-%s-%02d" % (case_no, counter[0]), "case_id": dispute_id, "type": typ,
             "status": status, "source": "SELLER_CENTER", "file_uri": None, "sha256": None,
             "captured_at": None, "captured_at_source": None, "extracted": {}, "match_basis": None,
             "seller_attestations": [], "recoverable": False}
        e.update(kw)
        return e

    files, foreign = _find_files(root, order_id)
    meta = _load_meta(root, order_id)
    declared = {x.upper() for x in meta.get("declared_unavailable", [])}

    for typ in TYPE_ORDER:
        if typ not in need:
            continue
        if typ in API_PART:
            part = API_PART[typ]
            code, data = session.get_json("/api/disputes/%s/evidence/%s" % (dispute_id, part))
            if code != 200 or not data or part not in data:
                out.append(mk(typ, "FAILED", extracted={"error": "HTTP %s" % code}, recoverable=True))
                continue
            p = data[part]
            if typ == "WAYBILL":
                ex = dict(p)
                ex["event_timestamps"] = []
                out.append(mk(typ, "COLLECTED", extracted=ex))
            elif typ == "TRACKING":
                by = {}
                for ev in p:
                    by.setdefault(ev["status"], ev["ts"])
                picked, delivered = by.get("PICKED_UP"), by.get("DELIVERED")
                from datetime import datetime
                consistent = None
                if delivered:
                    consistent = picked is None or datetime.fromisoformat(delivered) >= datetime.fromisoformat(picked)
                state["return_received"] = "RETURN_RECEIVED" in by
                out.append(mk(typ, "COLLECTED", extracted={
                    "has_delivered": bool(delivered), "delivered_at": delivered, "picked_up_at": picked,
                    "has_return_events": any(k.startswith("RETURN_") for k in by),
                    "timestamps_consistent": consistent,
                    "events": [{"ts": e["ts"], "status": e["status"]} for e in p],
                    "event_timestamps": [e["ts"] for e in p]}))
            elif typ == "CHAT_LOG":
                if not p:
                    out.append(mk(typ, "MISSING", recoverable=False, extracted={"message_count": 0}))
                else:
                    out.append(mk(typ, "COLLECTED", captured_at=p[-1]["ts"], extracted={
                        "message_count": len(p), "event_timestamps": [m["ts"] for m in p]}))
            else:  # ORDER_RECORD
                out.append(mk(typ, "COLLECTED", captured_at=p["captured_at"], extracted={
                    "variant_ordered": p["variant_ordered"], "price_idr": p["price_idr"],
                    "product": p["title"], "event_timestamps": [p["captured_at"]]}))
            continue

        cands = [f for f in files if role_of(f.name) == typ]
        chosen = (meta.get("chosen") or {}).get(typ)
        confirmed = False
        if len(cands) > 1 and chosen in [f.name for f in cands]:
            cands, confirmed = [f for f in cands if f.name == chosen], True
        if not cands:
            recov = typ not in declared and not (typ == "RETURN_RECEIPT_VIDEO" and state["return_received"])
            out.append(mk(typ, "MISSING", source="LOCAL_FOLDER", recoverable=recov, match_basis="no_candidate"))
        elif len(cands) == 1:
            f = cands[0]
            b = f.read_bytes()
            m = meta["files"].get(f.name, {})
            cap = m.get("captured_at")
            out.append(mk(typ, "COLLECTED", source="LOCAL_FOLDER", file_uri=f.relative_to(root).as_posix(),
                          sha256=hashlib.sha256(b).hexdigest(), captured_at=cap,
                          seller_attestations=list(m.get("attestations", [])),
                          captured_at_source=(m.get("captured_at_source", "FILE_META") if cap else None),
                          match_basis=("seller_confirmed" if confirmed else
                                       "folder_name_equals_order_id" if f.parent.name.lower() == order_id.lower()
                                       else "filename_contains_order_id"),
                          extracted={"event_timestamps": [cap] if cap else []}))
        else:
            out.append(mk(typ, "NEEDS_CONFIRMATION", source="LOCAL_FOLDER", recoverable=True,
                          match_basis="multiple_candidates",
                          extracted={"candidates": [f.name for f in cands]}))
    if foreign:
        tgt = next((e for e in out if e["type"] in MEDIA_TYPES), None)
        if tgt is not None:
            tgt["extracted"]["foreign_order_ids"] = foreign
    return out


def agent_view(e):
    """Bentuk bukti untuk input agent (tanpa field internal)."""
    ex = {k: v for k, v in (e.get("extracted") or {}).items() if k not in ("event_timestamps", "events")}
    return {"evidence_id": e["evidence_id"], "type": e["type"], "status": e["status"],
            "captured_at": e["captured_at"], "captured_at_source": e.get("captured_at_source"), "extracted": ex,
            "seller_attestations": e.get("seller_attestations", [])}


def read_file(media_root, e):
    return (Path(media_root) / e["file_uri"]).read_bytes()


def build_hash_index(media_root):
    """{sha256: [order_id, ...]} untuk semua berkas media di folder kerja (folder per pesanan + berkas di root yang memuat no. pesanan).
    Berkas di subfolder (mis. _tidak_dipakai) tidak dihitung."""
    root, idx = Path(media_root), {}
    if not root.exists():
        return idx

    def add(f, oid):
        idx.setdefault(hashlib.sha256(f.read_bytes()).hexdigest(), set()).add(oid.upper())
    for p in root.iterdir():
        if p.is_dir() and ORDER_RE.fullmatch(p.name):
            for f in p.iterdir():
                if f.is_file() and f.suffix.lower() in MEDIA_EXT:
                    add(f, p.name)
        elif p.is_file() and p.suffix.lower() in MEDIA_EXT:
            m = ORDER_RE.search(p.name)
            if m:
                add(p, m.group(0))
    return {k: sorted(v) for k, v in idx.items()}
