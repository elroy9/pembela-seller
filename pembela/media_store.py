"""Pengelola berkas bukti milik penjual (folder media + meta.json).

Aturan: berkas bukti asli TIDAK pernah ditimpa atau diubah. Unggahan baru selalu menjadi berkas baru;
pernyataan penjual dan pilihan kandidat hanya ditulis ke meta.json (sidecar), bukan ke berkas bukti.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .evidence import MEDIA_EXT, MEDIA_TYPES, role_of

BASENAME = {"PACKING_VIDEO": "packing_video", "HANDOVER_PHOTO": "handover_photo",
            "LISTING_SNAPSHOT": "listing_snapshot", "RETURN_RECEIPT_VIDEO": "return_receipt_video"}
MAX_BYTES = 50 * 1024 * 1024
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$")


class MediaError(ValueError):
    pass


def meta_path(root, order_id):
    root = Path(root)
    for p in (root / order_id / "meta.json", root / (order_id + ".meta.json")):
        if p.exists():
            return p
    return root / order_id / "meta.json"


def load_meta(root, order_id):
    p = meta_path(root, order_id)
    if p.exists():
        m = json.loads(p.read_text("utf-8"))
    else:
        m = {}
    m.setdefault("files", {})
    m.setdefault("declared_unavailable", [])
    return p, m


def _save(p, m):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(m, indent=1, ensure_ascii=False), "utf-8")


def set_attestations(root, order_id, filename, items):
    """Simpan pernyataan penjual (daftar item_id) untuk satu berkas. Hanya menulis meta.json."""
    root = Path(root)
    if not any(f.name == filename for f in list((root / order_id).glob("*")) + list(root.glob("*%s*" % order_id))):
        raise MediaError("berkas tidak ditemukan: %s" % filename)
    p, m = load_meta(root, order_id)
    m["files"].setdefault(filename, {})["attestations"] = sorted(set(items))
    _save(p, m)


def choose_candidate(root, order_id, role, filename):
    root = Path(root)
    if role not in MEDIA_TYPES:
        raise MediaError("jenis bukti tidak dikenal")
    if role_of(filename) != role or not (root / order_id / filename).exists():
        raise MediaError("berkas bukan kandidat untuk jenis ini")
    p, m = load_meta(root, order_id)
    m.setdefault("chosen", {})[role] = filename
    _save(p, m)


def save_upload(root, order_id, role, original_name, data, captured_at=None):
    """Simpan berkas baru. Mengembalikan nama berkas. Tidak pernah menimpa berkas yang ada."""
    root = Path(root)
    if role not in BASENAME:
        raise MediaError("jenis bukti tidak valid")
    ext = Path(original_name or "").suffix.lower()
    if ext not in MEDIA_EXT:
        raise MediaError("tipe berkas tidak diizinkan (boleh: %s)" % ", ".join(sorted(MEDIA_EXT)))
    if not data:
        raise MediaError("berkas kosong")
    if len(data) > MAX_BYTES:
        raise MediaError("berkas lebih dari 50 MB")
    if captured_at and not TS_RE.match(captured_at):
        raise MediaError("format waktu rekam tidak valid")
    folder = root / order_id
    folder.mkdir(parents=True, exist_ok=True)
    name, n = BASENAME[role] + ext, 1
    while (folder / name).exists():
        n += 1
        name = "%s_%d%s" % (BASENAME[role], n, ext)
    (folder / name).write_bytes(data)
    p, m = load_meta(root, order_id)
    entry = {"attestations": []}
    if captured_at:
        entry["captured_at"] = captured_at + ("+07:00" if len(captured_at) > 0 else "")
        entry["captured_at_source"] = "SELLER_DECLARED"
    m["files"][name] = entry
    _save(p, m)
    return name
