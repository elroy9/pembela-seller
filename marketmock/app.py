"""MarketMock - Seller Center TIRUAN (fiktif) untuk demo PEMBELA SELLER.

Semua data adalah dummy. Ini BUKAN tiruan platform marketplace manapun.
Jalankan:  python app.py   ->  http://127.0.0.1:5000
"""
import hashlib
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import (Flask, abort, g, jsonify, redirect, render_template,
                   request, session, url_for)
from markupsafe import Markup
from werkzeug.utils import secure_filename

WIB = timezone(timedelta(hours=7))
BASE = Path(__file__).resolve().parent
UPLOADS = BASE / "uploads"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-not-secret")
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024

# Kredensial DUMMY (untuk demo lokal saja). Di UiPath simpan di Orchestrator Assets.
USER = os.environ.get("MOCK_USER", "seller_demo")
PASSWORD = os.environ.get("MOCK_PASS", "demo123")
API_KEY = os.environ.get("MOCK_API_KEY", "demo-api-key")
TWOFA_CODE = "123456"  # 2FA PALSU

RESOLVE_AFTER_S = int(os.environ.get("RESOLVE_AFTER_S", "30"))  # simulasi keputusan
TIMEOUT_FAILS = int(os.environ.get("TIMEOUT_FAILS", "1"))  # berapa kali submit gagal saat chaos timeout
ALLOWED_EXT = {".mp4", ".jpg", ".jpeg", ".png", ".pdf", ".txt"}
CHAOS_MODES = {"layout", "testid", "captcha", "timeout"}

STATE = {
    "chaos": {m for m in os.environ.get("CHAOS", "").split(",") if m in CHAOS_MODES},
    "twofa": os.environ.get("TWOFA", "0") == "1",
    "timeouts": {},
}
DISPUTES = {}


# ----------------------------------------------------------------- data dummy
def _seed_from_dataset(path, now):
    """Muat kasus dari berkas JSON hasil datagen (tanpa ground truth)."""
    import json
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    for r in rows:
        off = r.get("deadline_offset_min")
        DISPUTES[r["dispute_id"]] = {
            "dispute_id": r["dispute_id"], "order_id": r["order_id"], "product": r["product"],
            "order_value_idr": r["order_value_idr"],
            "buyer_ref": "buyer_%s" % hashlib.sha256(r["dispute_id"].encode()).hexdigest()[:6],
            "buyer_claim_text": r["buyer_claim_text"],
            "deadline_at": None if off is None else (now + timedelta(minutes=off)).isoformat(timespec="seconds"),
            "courier": r.get("courier", "Kurir-A"), "status": "OPEN",
            "waybill": r["waybill"], "tracking": r["tracking"], "chat": r["chat"], "listing": r["listing"],
            "appeal": None, "_demo_outcome": r.get("demo_outcome", "RESOLVED_LOST"),
            "_truth": None, "_faults": r.get("faults", []),
        }


def seed():
    now = datetime.now(WIB)
    ds_path = os.environ.get("DATASET")
    if ds_path and Path(ds_path).exists():
        DISPUTES.clear()
        STATE["timeouts"].clear()
        _seed_from_dataset(ds_path, now)
        return

    def iso(dt):
        return dt.isoformat(timespec="seconds")

    def ago(minutes):
        return iso(now - timedelta(minutes=minutes))

    DISPUTES.clear()
    STATE["timeouts"].clear()

    def chat(order_id, lines):
        return [{"ts": ago(m), "from": who, "text": text} for m, who, text in lines]

    rows = [
        dict(dispute_id="D-1001", order_id="MM-261020-1001", product="Botol Kaca 500ml",
             variant="Bening", price=185000, courier="Kurir-A", ship_w=0.8, ret_w=None,
             claim="Barang pecah saat diterima, kemasan penyok.",
             deadline=now + timedelta(hours=6), outcome="RESOLVED_WON", truth="DAMAGED",
             chat=[(3000, "BUYER", "Kak, pesanan saya sudah sampai?"),
                   (2900, "SELLER", "Sudah dikirim ya, resi sudah di halaman pesanan."),
                   (120, "BUYER", "Barangnya pecah, saya minta refund. HP [TELEPON]")]),
        dict(dispute_id="D-1002", order_id="MM-261020-1002", product="Kaos Polos Premium",
             variant="Biru", price=249000, courier="Kurir-B", ship_w=0.3, ret_w=None,
             claim="Warna yang datang tidak sesuai, saya pesan biru tapi dapat hitam.",
             deadline=now + timedelta(hours=3), outcome="RESOLVED_LOST", truth="NOT_AS_DESCRIBED",
             chat=[(2600, "BUYER", "Ini varian biru kan?"),
                   (2590, "SELLER", "Betul, varian Biru sesuai pesanan."),
                   (200, "BUYER", "Yang datang hitam, tolong diganti.")]),
        dict(dispute_id="D-1003", order_id="MM-261021-1003", product="Casing Charger",
             variant="Putih", price=99000, courier="Kurir-A", ship_w=0.9, ret_w=0.1,
             claim="Paket retur dari saya berisi barang, tapi penjual bilang kosong.",
             deadline=now + timedelta(hours=8), outcome="RESOLVED_WON", truth="RETURN_EMPTY_OR_INCOMPLETE",
             chat=[(1900, "BUYER", "Saya sudah kirim balik barangnya."),
                   (300, "SELLER", "Paket retur yang saya terima hanya kardus.")]),
        dict(dispute_id="D-1004", order_id="MM-261021-1004", product="Dompet Kulit",
             variant="Coklat", price=135000, courier="Kurir-C", ship_w=0.4, ret_w=None,
             claim="Barang tidak sampai padahal status sudah terkirim.",
             deadline=now + timedelta(minutes=25), outcome="RESOLVED_LOST", truth="NOT_RECEIVED",
             chat=[(1500, "BUYER", "Paket belum datang."),
                   (1490, "SELLER", "Di tracking sudah delivered ya kak.")]),
    ]
    for i, r in enumerate(rows):
        delivered_at = now - timedelta(hours=30)
        tracking = [
            {"ts": iso(now - timedelta(hours=52)), "status": "PICKED_UP", "note": "Diserahkan ke kurir"},
            {"ts": iso(now - timedelta(hours=40)), "status": "IN_TRANSIT", "note": "Di hub sortir"},
            {"ts": iso(delivered_at), "status": "DELIVERED", "note": "Diterima (tanpa foto serah-terima)"},
        ]
        if r["ret_w"] is not None:
            tracking += [
                {"ts": iso(now - timedelta(hours=10)), "status": "RETURN_PICKED_UP", "note": "Retur diambil kurir"},
                {"ts": iso(now - timedelta(hours=2)), "status": "RETURN_RECEIVED", "note": "Retur diterima penjual"},
            ]
        DISPUTES[r["dispute_id"]] = {
            "dispute_id": r["dispute_id"],
            "order_id": r["order_id"],
            "product": r["product"],
            "order_value_idr": r["price"],
            "buyer_ref": "buyer_%s" % hashlib.sha256(r["dispute_id"].encode()).hexdigest()[:6],
            "buyer_claim_text": r["claim"],
            "deadline_at": iso(r["deadline"]),
            "courier": r["courier"],
            "status": "OPEN",
            "waybill": {"tracking_no": "TRK-%06d" % (100 + i),
                        "shipped_weight_kg": r["ship_w"],
                        "return_weight_kg": r["ret_w"]},
            "tracking": tracking,
            "chat": chat(r["order_id"], r["chat"]),
            "listing": {"title": r["product"], "variant_ordered": r["variant"],
                        "price_idr": r["price"], "captured_at": ago(2900)},
            "appeal": None,
            "_demo_outcome": r["outcome"],   # simulasi (ILUSTRASI), tidak diekspos
            "_truth": r["truth"],            # ground truth untuk pengujian, tidak diekspos
        }


seed()


# --------------------------------------------------------------------- helper
def effective_status(d):
    if d["status"] == "APPEAL_FILED":
        filed = datetime.fromisoformat(d["appeal"]["filed_at"])
        if (datetime.now(WIB) - filed).total_seconds() >= RESOLVE_AFTER_S:
            d["status"] = d["_demo_outcome"]
    elif d["status"] == "OPEN" and d["deadline_at"]:
        if datetime.now(WIB) > datetime.fromisoformat(d["deadline_at"]):
            d["status"] = "EXPIRED"
    return d["status"]


def public(d):
    out = {k: v for k, v in d.items() if not k.startswith("_")}
    out["status"] = effective_status(d)
    return out


def get_dispute(did):
    d = DISPUTES.get(did)
    if not d:
        abort(404)
    return d


def chaos(mode):
    return mode in STATE["chaos"]


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("auth"):
            return redirect(url_for("login"))
        return fn(*a, **kw)
    return wrapper


def api_key_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if request.headers.get("X-Mock-Key") != API_KEY and request.args.get("key") != API_KEY:
            return jsonify(error="unauthorized"), 401
        return fn(*a, **kw)
    return wrapper


def captcha_needed():
    return chaos("captcha") and time.time() > session.get("captcha_ok_until", 0)


@app.context_processor
def template_helpers():
    """tid/eid/rid/cls: titik tunggal tempat chaos mengubah selector."""
    if not hasattr(g, "ids"):
        g.ids = {}

    def rid(name):
        if not chaos("layout"):
            return name
        return g.ids.setdefault(name, "x" + secrets.token_hex(4))

    def tid(name):
        if chaos("testid"):  # data-testid hilang -> robot harus pakai selector cadangan
            return Markup('data-qa="%s-%s"' % (name, secrets.token_hex(2)))
        return Markup('data-testid="%s"' % name)

    def eid(name):
        return Markup('id="%s"' % rid(name))

    def cls(name):
        return Markup('class="%s"' % (("c" + secrets.token_hex(3)) if chaos("layout") else name))

    return dict(tid=tid, eid=eid, rid=rid, cls=cls, rev=chaos("layout"))


# ---------------------------------------------------------------------- login
@app.get("/")
def index():
    return redirect(url_for("orders") if session.get("auth") else url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if captcha_needed():
        return redirect(url_for("captcha", next=url_for("login")))
    error = None
    if request.method == "POST":
        if request.form.get("username") == USER and request.form.get("password") == PASSWORD:
            if STATE["twofa"]:
                session["pre_auth"] = True
                session["twofa_tries"] = 0
                return redirect(url_for("twofa"))
            session["auth"] = True
            return redirect(url_for("orders"))
        error = "Username atau password salah."
    return render_template("login.html", error=error)


@app.route("/login/2fa", methods=["GET", "POST"])
def twofa():
    if not session.get("pre_auth"):
        return redirect(url_for("login"))
    error = None
    if request.method == "POST":
        session["twofa_tries"] = session.get("twofa_tries", 0) + 1
        if session["twofa_tries"] > 3:
            error = "Terlalu banyak percobaan. Coba lagi nanti."
        elif request.form.get("code") == TWOFA_CODE:
            session.pop("pre_auth", None)
            session["auth"] = True
            return redirect(url_for("orders"))
        else:
            error = "Kode verifikasi salah."
    return render_template("twofa.html", error=error)


@app.route("/captcha", methods=["GET", "POST"])
def captcha():
    nxt = request.args.get("next", "/")
    if request.method == "POST":
        if request.form.get("answer", "").strip().upper() == session.get("captcha_word"):
            session["captcha_ok_until"] = time.time() + 120
            return redirect(nxt)
    session["captcha_word"] = secrets.choice(["KUCING", "RUMAH", "BUKU", "PINTU"])
    return render_template("captcha.html", word=session["captcha_word"], nxt=nxt)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------- halaman
@app.get("/orders")
@login_required
def orders():
    rows = [public(d) for d in DISPUTES.values()]
    return render_template("orders.html", rows=rows)


@app.get("/disputes/<did>")
@login_required
def dispute(did):
    return render_template("dispute.html", d=public(get_dispute(did)))


@app.get("/disputes/<did>/appeal")
@login_required
def appeal_form(did):
    if captcha_needed():
        return redirect(url_for("captcha", next=request.path))
    return render_template("appeal.html", d=public(get_dispute(did)), error=None)


@app.post("/disputes/<did>/appeal")
@login_required
def appeal_submit(did):
    d = get_dispute(did)
    status = effective_status(d)
    if status != "OPEN":
        return render_template("appeal.html", d=public(d),
                               error="Banding tidak dapat diajukan (status: %s)." % status), 409
    if chaos("timeout"):
        n = STATE["timeouts"].get(did, 0)
        if n < TIMEOUT_FAILS:
            STATE["timeouts"][did] = n + 1
            time.sleep(2)
            return render_template("timeout.html", did=did), 504
    text = (request.form.get("appeal_text") or "").strip()
    reason = request.form.get("reason", "")
    files = [f for f in request.files.getlist("evidence") if f and f.filename]
    if len(text) < 20 or not files:
        return render_template("appeal.html", d=public(d),
                               error="Isi alasan (min. 20 karakter) dan unggah minimal 1 bukti."), 400
    folder = UPLOADS / did
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        name = secure_filename(f.filename)
        if Path(name).suffix.lower() not in ALLOWED_EXT:
            return render_template("appeal.html", d=public(d),
                                   error="Tipe berkas tidak diizinkan: %s" % name), 400
        data = f.read()
        (folder / name).write_bytes(data)
        saved.append({"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    d["appeal"] = {
        "submission_ref": "SUB-%s-%s" % (did, secrets.token_hex(3).upper()),
        "filed_at": datetime.now(WIB).isoformat(timespec="seconds"),
        "reason": reason, "text": text, "files": saved,
    }
    d["status"] = "APPEAL_FILED"
    return redirect(url_for("result", did=did))


@app.get("/disputes/<did>/result")
@login_required
def result(did):
    return render_template("result.html", d=public(get_dispute(did)))


# ------------------------------------------------------------------------ API
@app.get("/api/disputes")
@api_key_required
def api_list():
    return jsonify([{k: public(d)[k] for k in
                     ("dispute_id", "order_id", "order_value_idr", "buyer_claim_text",
                      "deadline_at", "status", "courier")} for d in DISPUTES.values()])


@app.get("/api/disputes/<did>")
@api_key_required
def api_detail(did):
    return jsonify(public(get_dispute(did)))


@app.get("/api/disputes/<did>/evidence")
@api_key_required
def api_evidence(did):
    d = get_dispute(did)
    return jsonify({"dispute_id": did, "waybill": d["waybill"], "tracking": d["tracking"],
                    "chat": d["chat"], "listing": d["listing"]})


@app.get("/api/disputes/<did>/evidence/<part>")
@api_key_required
def api_evidence_part(did, part):
    """Satu bagian bukti per panggilan. Bagian yang ada di `_faults` dibalas 503 (uji kegagalan)."""
    d = get_dispute(did)
    if part not in ("waybill", "tracking", "chat", "listing"):
        abort(404)
    if part in d.get("_faults", []):
        return jsonify(error="upstream unavailable", part=part), 503
    return jsonify({"dispute_id": did, part: d[part]})


# ---------------------------------------------------------------------- admin
@app.get("/admin/state")
@api_key_required
def admin_state():
    return jsonify(chaos=sorted(STATE["chaos"]), twofa=STATE["twofa"],
                   resolve_after_s=RESOLVE_AFTER_S, timeout_fails=TIMEOUT_FAILS)


@app.get("/admin/chaos")
@api_key_required
def admin_chaos():
    """/admin/chaos?set=layout,testid,captcha,timeout | off  [&twofa=1|0]"""
    val = request.args.get("set")
    if val is not None:
        STATE["chaos"] = {m for m in val.split(",") if m in CHAOS_MODES}
    if "twofa" in request.args:
        STATE["twofa"] = request.args["twofa"] == "1"
    STATE["timeouts"].clear()
    return admin_state()


@app.get("/admin/demo_outcome")
@api_key_required
def admin_demo_outcome():
    """Pengendali SKENARIO DEMO (bukan penilaian bukti): /admin/demo_outcome?dispute=DSP-001&outcome=RESOLVED_WON"""
    d = get_dispute(request.args.get("dispute", ""))
    o = request.args.get("outcome")
    if o not in ("RESOLVED_WON", "RESOLVED_LOST") or d["status"] != "APPEAL_FILED":
        return jsonify(ok=False), 400
    d["_demo_outcome"] = o
    return jsonify(ok=True)


@app.get("/admin/reset")
@api_key_required
def admin_reset():
    seed()
    return jsonify(ok=True)


if __name__ == "__main__":
    UPLOADS.mkdir(exist_ok=True)
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
