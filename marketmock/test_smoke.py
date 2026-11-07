"""Uji asap untuk MarketMock. Jalankan: python test_smoke.py"""
import io
import re
import shutil

import app as m

KEY = {"X-Mock-Key": m.API_KEY}
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


def fresh():
    m.STATE["chaos"] = set()
    m.STATE["twofa"] = False
    m.seed()
    shutil.rmtree(m.UPLOADS, ignore_errors=True)
    return m.app.test_client()


def login(c, code=None):
    r = c.post("/login", data={"username": m.USER, "password": m.PASSWORD})
    if code:
        r = c.post("/login/2fa", data={"code": code})
    return r


# 1. alur normal
c = fresh()
check("orders butuh login", c.get("/orders").status_code == 302)
r = login(c)
check("login ok -> /orders", r.status_code == 302 and r.headers["Location"].endswith("/orders"))
html = c.get("/orders").get_data(as_text=True)
check("orders: 4 baris + data-testid", html.count('data-testid="order-row-') == 4)
html = c.get("/disputes/D-1001").get_data(as_text=True)
for t in ["dispute-id", "order-id", "order-value", "buyer-claim", "deadline-at", "countdown",
          "waybill-no", "tracking-table", "chat-log", "listing-variant", "btn-start-appeal"]:
    check("dispute testid " + t, 'data-testid="%s"' % t in html)
check("chat dimasking", "[TELEPON]" in html)
r = c.post("/disputes/D-1001/appeal", data={"reason": "x", "appeal_text": "pendek"})
check("banding tanpa bukti ditolak (400)", r.status_code == 400)
r = c.post("/disputes/D-1001/appeal", data={
    "reason": "goods_ok_before_shipping",
    "appeal_text": "Barang dikirim utuh sesuai bukti EV-0001 dan EV-0002.",
    "evidence": (io.BytesIO(b"dummy video"), "packing.mp4")},
    content_type="multipart/form-data")
check("banding terkirim -> redirect result", r.status_code == 302)
html = c.get("/disputes/D-1001/result").get_data(as_text=True)
check("result: APPEAL_FILED + filing-ref", "APPEAL_FILED" in html and 'data-testid="filing-ref"' in html)
r = c.post("/disputes/D-1001/appeal", data={"appeal_text": "x" * 30}, content_type="multipart/form-data")
check("banding ganda ditolak (409)", r.status_code == 409)
m.RESOLVE_AFTER_S = 0
check("hasil tersimulasi WON", "RESOLVED_WON" in c.get("/disputes/D-1001/result").get_data(as_text=True))
m.RESOLVE_AFTER_S = 30
r = c.get("/api/disputes")
check("API tanpa key = 401", r.status_code == 401)
r = c.get("/api/disputes", headers=KEY)
check("API list 4 item", r.status_code == 200 and len(r.get_json()) == 4)
check("API tidak bocorkan ground truth", "_truth" not in c.get("/api/disputes/D-1002", headers=KEY).get_data(as_text=True))
check("API evidence", set(c.get("/api/disputes/D-1003/evidence", headers=KEY).get_json()) ==
      {"dispute_id", "waybill", "tracking", "chat", "listing"})

# 2. tenggat lewat
c = fresh()
login(c)
m.DISPUTES["D-1004"]["deadline_at"] = "2020-01-01T00:00:00+07:00"
check("tenggat lewat -> EXPIRED", c.get("/api/disputes/D-1004", headers=KEY).get_json()["status"] == "EXPIRED")
r = c.post("/disputes/D-1004/appeal", data={"appeal_text": "x" * 30}, content_type="multipart/form-data")
check("banding setelah tenggat ditolak", r.status_code == 409)

# 3. 2FA palsu
c = fresh()
m.STATE["twofa"] = True
r = login(c)
check("2FA: diarahkan ke /login/2fa", r.headers["Location"].endswith("/login/2fa"))
check("2FA salah ditolak", "salah" in c.post("/login/2fa", data={"code": "000000"}).get_data(as_text=True))
r = c.post("/login/2fa", data={"code": m.TWOFA_CODE})
check("2FA benar -> /orders", r.status_code == 302 and r.headers["Location"].endswith("/orders"))

# 4. chaos: testid
c = fresh()
login(c)
m.STATE["chaos"] = {"testid"}
html = c.get("/orders").get_data(as_text=True)
check("chaos testid: data-testid hilang, data-qa ada",
      "data-testid" not in html and "data-qa=" in html and "Buka" in html)

# 5. chaos: layout
m.STATE["chaos"] = {"layout"}
ids = [re.findall(r'id="(x[0-9a-f]{8})"', c.get("/disputes/D-1001/appeal").get_data(as_text=True)) for _ in range(2)]
check("chaos layout: id acak berubah tiap muat", ids[0] and ids[0] != ids[1])
html = c.get("/disputes/D-1001/appeal").get_data(as_text=True)
check("chaos layout: label for = id input",
      all(('for="%s"' % i) in html for i in re.findall(r'<input[^>]*id="(x[0-9a-f]{8})"', html)))

# 6. chaos: captcha
c = fresh()
m.STATE["chaos"] = {"captcha"}
r = c.get("/login")
check("chaos captcha: /login -> /captcha", r.status_code == 302 and "/captcha" in r.headers["Location"])
html = c.get(r.headers["Location"]).get_data(as_text=True)
check("captcha-box ada", 'data-testid="captcha-box"' in html)

# 7. chaos: timeout
c = fresh()
login(c)
m.STATE["chaos"] = {"timeout"}
m.STATE["timeouts"].clear()
payload = lambda: dict(data={"reason": "x", "appeal_text": "Uraian banding yang cukup panjang ya.",
                             "evidence": (io.BytesIO(b"x"), "a.txt")}, content_type="multipart/form-data")
r1 = c.post("/disputes/D-1002/appeal", **payload())
check("chaos timeout: kiriman pertama 504", r1.status_code == 504 and "error-timeout" in r1.get_data(as_text=True))
r2 = c.post("/disputes/D-1002/appeal", **payload())
check("chaos timeout: retry berhasil", r2.status_code == 302)

# 8. admin
c = fresh()
r = c.get("/admin/chaos?set=layout,captcha&key=" + m.API_KEY)
check("admin chaos set", set(r.get_json()["chaos"]) == {"layout", "captcha"})
r = c.get("/admin/chaos?set=off&key=" + m.API_KEY)
check("admin chaos off", r.get_json()["chaos"] == [])
check("admin tanpa key = 401", c.get("/admin/chaos?set=layout").status_code == 401)

shutil.rmtree(m.UPLOADS, ignore_errors=True)
bad = [n for n, ok in results if not ok]
print("\n%d/%d lulus" % (len(results) - len(bad), len(results)))
raise SystemExit(1 if bad else 0)
