"""Konsol web PEMBELA SELLER (prototipe). Jalankan: python console/server.py  ->  http://127.0.0.1:5000/console

Satu proses melayani: (1) mock Seller Center di /orders, /disputes/... dan (2) konsol penjual di /console.
Mesin penilai default = BaselineBackend (aturan, BUKAN LLM). Persetujuan manusia tetap melewati gerbang `file_appeal`.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import threading
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "marketmock"))
warnings.filterwarnings("ignore")
os.environ.setdefault("DATASET", str(ROOT / "data" / "cases.json"))
os.environ.setdefault("RESOLVE_AFTER_S", "20")

from flask import Blueprint, flash, redirect, render_template, request, url_for  # noqa: E402

import app as mm  # noqa: E402  (mock Seller Center)
from pembela import rubric  # noqa: E402
from pembela.approval import GateError
from pembela.evidence import MEDIA_TYPES, build_hash_index, role_of  # noqa: E402
from pembela.backends import BaselineBackend  # noqa: E402
from pembela.pipeline import AuditLog, _minutes_left, run_case  # noqa: E402
from pembela.robot_http import RobotError, file_appeal  # noqa: E402
from pembela.sessions import FlaskSession  # noqa: E402
from pembela.validator import draft_hash, validate_draft  # noqa: E402

WIB = timezone(timedelta(hours=7))
PRISTINE = ROOT / "data" / "media"
MEDIA = ROOT / ".demo_media"   # salinan kerja; unggahan masuk ke sini. "Reset demo" memulihkannya.


def restore_media():
    shutil.rmtree(MEDIA, ignore_errors=True)
    shutil.copytree(PRISTINE, MEDIA)


restore_media()
BACKEND = BaselineBackend()
LOCK = threading.Lock()
STORE = {}   # case_id -> {"state","res","req","audit","filing","outcome"}
S = None  # dibuat setelah blueprint terdaftar (Flask melarang registrasi sesudah request pertama)

bp = Blueprint("console", __name__, url_prefix="/console", template_folder="templates")

STATES = {  # state: (label, tone, penjelasan singkat)
    "NOT_PROCESSED": ("Belum diproses", "neutral", "Belum dianalisis"),
    "PENDING_APPROVAL": ("Perlu keputusanmu", "action", "Draf siap, menunggu persetujuanmu"),
    "AWAITING_MORE_EVIDENCE": ("Menunggu bukti", "warn", "Ada bukti yang masih bisa dilengkapi"),
    "ESCALATED_TO_HUMAN": ("Dieskalasi", "danger", "Perlu ditangani manual"),
    "CLOSED_NO_APPEAL": ("Tidak dibanding", "neutral", "Banding tidak disarankan atau ditolak"),
    "FILED": ("Sudah diajukan", "ok", "Banding terkirim, menunggu hasil"),
    "EXPIRED": ("Tenggat lewat", "danger", "Tenggat sudah terlewat"),
    "ALREADY_FILED": ("Diajukan di luar sistem", "neutral", "Sudah diajukan langsung di Seller Center"),
}
CASE_TYPE = {"DAMAGED": "Barang rusak", "NOT_AS_DESCRIBED": "Tidak sesuai deskripsi",
             "RETURN_EMPTY_OR_INCOMPLETE": "Retur kosong/tidak lengkap", "NOT_RECEIVED": "Paket tidak diterima",
             "UNKNOWN": "Tidak jelas"}
EV_TYPE = {"WAYBILL": "Data resi", "TRACKING": "Riwayat pelacakan", "CHAT_LOG": "Riwayat chat",
           "ORDER_RECORD": "Catatan pesanan", "PACKING_VIDEO": "Video packing", "HANDOVER_PHOTO": "Foto serah-terima",
           "LISTING_SNAPSHOT": "Tangkapan layar listing", "RETURN_RECEIPT_VIDEO": "Video penerimaan retur"}
EV_STATUS = {"COLLECTED": ("Terkumpul", "ok"), "MISSING": ("Tidak ditemukan", "danger"),
             "FAILED": ("Gagal diambil", "warn"), "NEEDS_CONFIRMATION": ("Perlu konfirmasi", "warn")}
ITEM_STATUS = {"MET": ("Terpenuhi", "ok"), "PARTIAL": ("Sebagian", "warn"),
               "NOT_MET": ("Tidak terpenuhi", "danger"), "UNKNOWN": ("Belum diketahui", "neutral")}
STRENGTH = {"STRONG": ("Kuat", "ok"), "MEDIUM": ("Sedang", "warn"), "WEAK": ("Lemah", "danger")}
RECO = {"APPEAL": "Ajukan banding", "COLLECT_MORE": "Lengkapi bukti dulu", "DO_NOT_APPEAL": "Tidak disarankan banding"}
NEXT_STEP = {
    "AWAITING_MORE_EVIDENCE": "Cari bukti yang kurang (yang memang sudah kamu miliki), lalu unggah lewat 'Tambah bukti' di bawah. Setelah itu nyatakan isinya jika berupa video.",
    "ESCALATED_TO_HUMAN": "Tinjau kasus ini secara manual. Sistem sengaja tidak menebak.",
    "CLOSED_NO_APPEAL": "Tidak ada tindakan. Bukti tidak cukup, atau kamu menolak draf.",
}


@bp.app_template_filter("fmt_ts")
def fmt_ts(v):
    if not v:
        return "-"
    d = datetime.fromisoformat(v)
    bln = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"][d.month - 1]
    return "%d %s %02d:%02d" % (d.day, bln, d.hour, d.minute)


@bp.app_template_filter("idr")
def idr(v):
    return "Rp " + "{:,}".format(v or 0).replace(",", ".")


def left_text(m):
    if m is None:
        return "Tenggat tidak diketahui"
    if m <= 0:
        return "Lewat"
    return "%d j %02d m" % (m // 60, m % 60) if m >= 60 else "%d menit" % m



ROLE_FILE = {"PACKING_VIDEO": "packing_video", "HANDOVER_PHOTO": "handover_photo",
             "LISTING_SNAPSHOT": "listing_snapshot", "RETURN_RECEIPT_VIDEO": "return_receipt_video"}
ROLE_LABEL = {"PACKING_VIDEO": "Video packing", "HANDOVER_PHOTO": "Foto serah-terima ke kurir",
              "LISTING_SNAPSHOT": "Tangkapan layar listing", "RETURN_RECEIPT_VIDEO": "Video penerimaan retur"}
UPLOAD_EXT = {".mp4", ".jpg", ".jpeg", ".png", ".pdf"}
MAX_UPLOAD = 50 * 1024 * 1024


def _meta_path(oid):
    for p in (MEDIA / oid / "meta.json", MEDIA / (oid + ".meta.json")):
        if p.exists():
            return p
    return MEDIA / oid / "meta.json"


def _load_meta(oid):
    p = _meta_path(oid)
    return json.loads(p.read_text("utf-8")) if p.exists() else {"files": {}, "declared_unavailable": []}


def _save_meta(oid, meta):
    p = _meta_path(oid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, indent=1, ensure_ascii=False), "utf-8")


def _audit(cid, event, **kw):
    st = STORE.get(cid)
    if st is not None:
        st["audit"].append({"ts": datetime.now(WIB).isoformat(timespec="seconds"), "case_id": cid, "event": event, **kw})


def _detail(cid):
    code, d = S.get_json("/api/disputes/%s" % cid)
    return d if code == 200 else None


def _row(cid):
    d = _detail(cid)
    st = STORE.get(cid)
    state = st["state"] if st else "NOT_PROCESSED"
    if st and state == "FILED":
        s = d["status"]
        if s in ("RESOLVED_WON", "RESOLVED_LOST"):
            st["outcome"] = s
    if d["status"] == "EXPIRED" and state not in ("FILED",):
        state = "EXPIRED"
    elif d["status"] in ("APPEAL_FILED", "RESOLVED_WON", "RESOLVED_LOST") and state == "NOT_PROCESSED":
        state = "ALREADY_FILED"
    m = _minutes_left(d["deadline_at"])
    return {"id": cid, "d": d, "state": state, "label": STATES[state][0], "tone": STATES[state][1],
            "minutes": m, "left": left_text(m), "urgent": m is not None and m < 60,
            "res": st["res"] if st else None}


def process(cid):
    cap = {}

    class Capture:
        def decide(self, req):
            cap["req"] = req
            return None
    audit = AuditLog()
    res = run_case(S, BACKEND, Capture(), cid, MEDIA, int(cid.split("-")[1]), audit, hash_index=build_hash_index(MEDIA))
    state = res["final_state"]
    if cap.get("req") and "approval belum" in res["reason"]:
        state = "PENDING_APPROVAL"
    STORE[cid] = {"state": state, "res": res, "req": cap.get("req"), "audit": audit.rows, "filing": None, "outcome": None}
    return state


def _ids():
    code, rows = S.get_json("/api/disputes")
    return [r["dispute_id"] for r in rows]


# ------------------------------------------------------------------ halaman
@bp.get("/")
def dashboard():
    rows = [_row(c) for c in _ids()]
    order = {"PENDING_APPROVAL": 0, "ESCALATED_TO_HUMAN": 1, "AWAITING_MORE_EVIDENCE": 2, "NOT_PROCESSED": 3,
             "FILED": 4, "ALREADY_FILED": 4, "CLOSED_NO_APPEAL": 5, "EXPIRED": 6}
    rows.sort(key=lambda r: (order[r["state"]], r["minutes"] if r["minutes"] is not None else 10 ** 9))
    grp = lambda st: "FILED" if st == "ALREADY_FILED" else st      # kartu "Sudah diajukan" mencakup yang diajukan di luar sistem
    counts = {k: sum(1 for r in rows if grp(r["state"]) == k) for k in STATES}
    flt = request.args.get("s")
    shown = [r for r in rows if not flt or grp(r["state"]) == flt]
    value = sum(r["d"]["order_value_idr"] or 0 for r in rows if r["state"] in ("PENDING_APPROVAL", "AWAITING_MORE_EVIDENCE"))
    urgent = [r for r in rows if r["urgent"] and r["state"] in ("PENDING_APPROVAL", "AWAITING_MORE_EVIDENCE", "NOT_PROCESSED")]
    return render_template("console_dashboard.html", rows=shown, counts=counts, total=len(rows), flt=flt, STATES=STATES,
                           CASE_TYPE=CASE_TYPE, value=value, urgent=urgent)


@bp.get("/case/<cid>")
def case(cid):
    r = _row(cid)
    st = STORE.get(cid)
    items = []
    if st and st["res"].get("items"):
        desc = {c[0]: c[3] for c in rubric.CHECKLISTS[st["res"]["triage"]["case_type"]]}
        wt = {c[0]: (c[1], c[2]) for c in rubric.CHECKLISTS[st["res"]["triage"]["case_type"]]}
        items = [dict(i, desc=desc[i["item_id"]], weight=wt[i["item_id"]][0], critical=wt[i["item_id"]][1]) for i in st["res"]["items"]]
    item_desc = {i["item_id"]: i["desc"] for i in items}
    video_items, upload_roles = {}, []
    if st and st["res"].get("triage") and st["res"]["triage"]["case_type"] != "UNKNOWN":
        ct = st["res"]["triage"]["case_type"]
        cdesc = {c[0]: c[3] for c in rubric.CHECKLISTS[ct]}
        for e in st["res"].get("evidence", []):
            if e["type"] in ("PACKING_VIDEO", "RETURN_RECEIPT_VIDEO") and e["status"] == "COLLECTED":
                video_items[e["evidence_id"]] = [{"item_id": i, "desc": cdesc[i], "checked": i in e["seller_attestations"]}
                                                 for i in cdesc if i in rubric.VIDEO_CLAIM_ITEMS and rubric.ITEM_TYPE[i] == e["type"]]
        upload_roles = [(t, ROLE_LABEL[t]) for t in ROLE_FILE if t in rubric.needed_types(ct)]
    return render_template("console_case.html", r=r, st=st, items=items, item_desc=item_desc,
                           video_items=video_items, upload_roles=upload_roles, STATES=STATES, CASE_TYPE=CASE_TYPE, EV_TYPE=EV_TYPE,
                           EV_STATUS=EV_STATUS, ITEM_STATUS=ITEM_STATUS, STRENGTH=STRENGTH, RECO=RECO, NEXT_STEP=NEXT_STEP)


# ------------------------------------------------------------------ aksi
@bp.post("/process/<cid>")
def do_process(cid):
    with LOCK:
        process(cid)
    return redirect(url_for("console.case", cid=cid))


@bp.post("/process-all")
def do_process_all():
    with LOCK:
        n = 0
        for c in _ids():
            if c not in STORE:
                process(c)
                n += 1
    flash("%d kasus dianalisis." % n)
    return redirect(url_for("console.dashboard"))


@bp.post("/case/<cid>/approve")
def approve(cid):
    st = STORE.get(cid)
    if not st or st["state"] != "PENDING_APPROVAL" or not st.get("req"):
        flash("Tidak ada draf yang menunggu persetujuan.")
        return redirect(url_for("console.case", cid=cid))
    if request.form.get("hash") != st["req"]["draft_hash"]:
        flash("Draf berubah sejak halaman dibuka. Baca ulang lalu setujui lagi.")
        return redirect(url_for("console.case", cid=cid))
    req = st["req"]
    approval = {"approval_id": "APR-" + os.urandom(4).hex().upper(), "case_id": cid, "decision": "APPROVE",
                "approved_draft_hash": req["draft_hash"], "decided_by": "HUMAN:console",
                "decided_at": datetime.now(WIB).isoformat(timespec="seconds")}
    st["audit"].append({"ts": approval["decided_at"], "case_id": cid, "event": "APPROVAL_DECISION", "decision": "APPROVE"})
    d = _detail(cid)
    try:
        with LOCK:
            filed = file_appeal(S, cid, st["res"]["triage"]["case_type"], req["appeal"], approval, st["res"]["evidence"],
                                MEDIA, _minutes_left(d["deadline_at"]))
    except (GateError, RobotError) as e:
        st["audit"].append({"ts": datetime.now(WIB).isoformat(timespec="seconds"), "case_id": cid, "event": "GATE_BLOCKED", "reason": str(e)})
        flash("Pengajuan DIBLOKIR oleh gerbang keamanan: %s" % e)
        return redirect(url_for("console.case", cid=cid))
    # Skenario demo: hasil simulasi di mock mengikuti skor (>= 75 menang). Ini ILUSTRASI, bukan prediksi hasil nyata.
    S.get_json("/admin/demo_outcome?dispute=%s&outcome=%s" % (cid, "RESOLVED_WON" if st["res"]["score"]["score"] >= 75 else "RESOLVED_LOST"))
    st.update(state="FILED", filing=filed, req=None)
    st["audit"].append({"ts": filed["filed_at"], "case_id": cid, "event": "FILED", "ref": filed["submission_ref"]})
    flash("Banding diajukan: %s" % filed["submission_ref"])
    return redirect(url_for("console.case", cid=cid))


@bp.post("/case/<cid>/reject")
def reject(cid):
    st = STORE.get(cid)
    if st and st["state"] == "PENDING_APPROVAL":
        st.update(state="CLOSED_NO_APPEAL", req=None)
        st["res"]["reason"] = "kamu menolak draf"
        st["audit"].append({"ts": datetime.now(WIB).isoformat(timespec="seconds"), "case_id": cid, "event": "APPROVAL_DECISION", "decision": "REJECT"})
        flash("Draf ditolak. Tidak ada banding yang dikirim.")
    return redirect(url_for("console.case", cid=cid))


@bp.post("/case/<cid>/note")
def note(cid):
    st = STORE.get(cid)
    text = (request.form.get("note") or "").strip()
    if not st or st["state"] != "PENDING_APPROVAL" or not text:
        return redirect(url_for("console.case", cid=cid))
    ap = copy.deepcopy(st["req"]["appeal"])
    ap["requests"].append({"kind": "request", "text": text})
    errs = validate_draft({"case_id": cid, "status": "DRAFTED", "refusal_reason": None, "appeal": ap}, st["res"]["evidence"])
    if errs:
        flash("Catatan ditolak validator: %s" % errs[0])
    else:
        st["req"]["appeal"], st["req"]["draft_hash"] = ap, draft_hash(ap)
        st["audit"].append({"ts": datetime.now(WIB).isoformat(timespec="seconds"), "case_id": cid, "event": "DRAFT_EDITED"})
        flash("Catatan ditambahkan. Draf berubah, jadi persetujuan harus diberikan ulang.")
    return redirect(url_for("console.case", cid=cid))



@bp.post("/case/<cid>/upload")
def upload(cid):
    d = _detail(cid)
    st = STORE.get(cid)
    role, f = request.form.get("role"), request.files.get("file")
    if not d or not st or st["state"] == "FILED":
        flash("Bukti tidak bisa ditambahkan pada kasus ini.")
        return redirect(url_for("console.case", cid=cid))
    ext = Path(f.filename or "").suffix.lower() if f else ""
    data = f.read() if f and f.filename else b""
    if role not in ROLE_FILE or ext not in UPLOAD_EXT or not data or len(data) > MAX_UPLOAD:
        flash("Berkas ditolak: pilih jenis bukti, lalu berkas mp4/jpg/png/pdf (maks. 50 MB).")
        return redirect(url_for("console.case", cid=cid))
    ca = None
    if request.form.get("captured_at"):
        try:
            ca = datetime.fromisoformat(request.form["captured_at"]).replace(tzinfo=WIB).isoformat(timespec="seconds")
        except ValueError:
            flash("Format waktu rekam tidak valid.")
            return redirect(url_for("console.case", cid=cid))
    oid = d["order_id"]
    folder = MEDIA / oid
    folder.mkdir(parents=True, exist_ok=True)
    name, n = ROLE_FILE[role] + ext, 1
    while (folder / name).exists():
        n += 1
        name = "%s_%d%s" % (ROLE_FILE[role], n, ext)
    (folder / name).write_bytes(data)
    meta = _load_meta(oid)
    meta["files"][name] = {"captured_at": ca, "attestations": []}   # pernyataan isi video diberikan terpisah oleh penjual
    _save_meta(oid, meta)
    with LOCK:
        process(cid)
        _audit(cid, "EVIDENCE_UPLOADED", reason=ROLE_LABEL[role])
    flash("%s ditambahkan dan kasus dianalisis ulang. Nyatakan isi video di bagian 'Pernyataan penjual' bila perlu." % ROLE_LABEL[role])
    return redirect(url_for("console.case", cid=cid))


@bp.post("/case/<cid>/attest")
def attest(cid):
    d, st = _detail(cid), STORE.get(cid)
    if not d or not st or st["state"] == "FILED":
        return redirect(url_for("console.case", cid=cid))
    checked = set(request.form.getlist("att"))   # "EV-5-04:PACK_VIDEO_CONDITION"
    meta = _load_meta(d["order_id"])
    now = datetime.now(WIB).isoformat(timespec="seconds")
    for e in st["res"]["evidence"]:
        if e["type"] not in ("PACKING_VIDEO", "RETURN_RECEIPT_VIDEO") or e["status"] != "COLLECTED":
            continue
        name = Path(e["file_uri"]).name
        items = [i for i in rubric.VIDEO_CLAIM_ITEMS if rubric.ITEM_TYPE[i] == e["type"] and "%s:%s" % (e["evidence_id"], i) in checked]
        entry = meta["files"].setdefault(name, {"captured_at": e["captured_at"], "attestations": []})
        entry["attestations"], entry["attested_at"] = sorted(items), now
    _save_meta(d["order_id"], meta)
    with LOCK:
        process(cid)
        _audit(cid, "SELLER_ATTESTATION_UPDATED")
    flash("Pernyataan disimpan dan kasus dianalisis ulang. Draf lama tidak berlaku lagi.")
    return redirect(url_for("console.case", cid=cid))


@bp.post("/case/<cid>/choose")
def choose(cid):
    d, st = _detail(cid), STORE.get(cid)
    keep, eid = request.form.get("keep"), request.form.get("evidence_id")
    ev = next((e for e in (st["res"]["evidence"] if st else []) if e["evidence_id"] == eid and e["status"] == "NEEDS_CONFIRMATION"), None)
    if not d or not ev or keep not in ev["extracted"].get("candidates", []):
        flash("Pilihan tidak valid.")
        return redirect(url_for("console.case", cid=cid))
    oid = d["order_id"]
    for name in ev["extracted"]["candidates"]:
        if name == keep:
            continue
        src = MEDIA / oid / name if (MEDIA / oid / name).exists() else MEDIA / name
        dst = MEDIA / oid / "_tidak_dipakai"
        dst.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst / name))   # dipindah, bukan dihapus
    with LOCK:
        process(cid)
        _audit(cid, "EVIDENCE_CONFIRMED", reason=keep)
    flash("Berkas yang dipakai: %s. Kandidat lain dipindah ke _tidak_dipakai (tidak dihapus)." % keep)
    return redirect(url_for("console.case", cid=cid))

@bp.post("/reset")
def reset():
    with LOCK:
        mm.seed()
        STORE.clear()
        restore_media()
    flash("Demo di-reset: semua kasus kembali ke awal.")
    return redirect(url_for("console.dashboard"))


mm.app.register_blueprint(bp)
S = FlaskSession(mm.app, mm.USER, mm.PASSWORD, mm.API_KEY)


if __name__ == "__main__":
    print("Konsol: http://127.0.0.1:5000/console   |   Mock Seller Center: http://127.0.0.1:5000/orders  (seller_demo / demo123)")
    mm.app.run(host="127.0.0.1", port=5000, debug=False, threaded=False)
