# MarketMock: Seller Center TIRUAN untuk PEMBELA SELLER

Situs fiktif untuk demo. Semua data dummy. Bukan tiruan platform marketplace manapun.

## Menjalankan

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py                                         # http://127.0.0.1:5000
python test_smoke.py                                  # 39 uji otomatis (opsional)
```

Akun dummy: `seller_demo` / `demo123` (bisa diganti lewat env `MOCK_USER`, `MOCK_PASS`).
Kunci API/admin dummy: `demo-api-key` (env `MOCK_API_KEY`). Di UiPath simpan semua ini di Orchestrator Assets.

## Halaman

| URL | Fungsi |
|---|---|
| `/login`, `/login/2fa` | Login. 2FA palsu (kode `123456`) aktif bila `twofa=1` |
| `/orders` | Daftar sengketa/komplain |
| `/disputes/<id>` | Detail: klaim, hitung mundur tenggat, resi, tracking, listing, chat (sudah dimasking) |
| `/disputes/<id>/appeal` | Form banding + unggah bukti |
| `/disputes/<id>/result` | Status/hasil + nomor pengajuan + waktu |
| `/captcha` | CAPTCHA palsu (hanya saat chaos `captcha`) |

Data seed: D-1001 (rusak), D-1002 (varian), D-1003 (retur kosong), D-1004 (tidak diterima, tenggat 25 menit dari seed). Tenggat dihitung relatif dari saat server start atau `/admin/reset`.
Hasil banding disimulasikan dari nilai tetap di seed setelah `RESOLVE_AFTER_S` detik (default 30): ini **ILUSTRASI**, bukan statistik.

## API baca-saja (header `X-Mock-Key: demo-api-key`)

`GET /api/disputes` · `GET /api/disputes/<id>` · `GET /api/disputes/<id>/evidence` (resi, tracking, chat, listing)

Saran: pakai API untuk **mengumpulkan** bukti (cepat, stabil), dan UI automation hanya untuk **login** dan **mengajukan banding** (bagian yang diuji chaos).

## Chaos mode

```
GET /admin/chaos?set=layout,testid,captcha,timeout&key=demo-api-key     # aktifkan
GET /admin/chaos?set=off&key=demo-api-key                                # matikan
GET /admin/chaos?twofa=1&key=demo-api-key                                # 2FA palsu
GET /admin/reset?key=demo-api-key                                        # seed ulang + tenggat baru
GET /admin/state?key=demo-api-key
```

| Mode | Efek | Perilaku robot yang benar |
|---|---|---|
| `layout` | `id`/`class` acak tiap muat, urutan field form dibalik | Selector pakai `data-testid` atau label, bukan `id` |
| `testid` | Atribut `data-testid` diganti `data-qa-*` bersufiks acak | Turun ke selector cadangan (label/teks), catat peringatan di log |
| `captcha` | `/login` dan form banding minta CAPTCHA | **Berhenti dan eskalasi ke manusia.** Jangan mencoba melewatinya |
| `timeout` | Kiriman banding pertama per kasus dibalas 504 (env `TIMEOUT_FAILS`) | Retry terbatas (maks. 2), lalu eskalasi. Pastikan tidak ada pengajuan ganda |

## Daftar `data-testid`

- Login: `login-username`, `login-password`, `login-submit`, `login-error`; 2FA: `twofa-code`, `twofa-submit`, `twofa-error`
- Captcha: `captcha-box`, `captcha-input`, `captcha-submit`
- Orders: `orders-table`, `order-row-<id>`, `order-status-<id>`, `open-dispute-<id>`
- Detail: `dispute-id`, `order-id`, `order-value`, `buyer-claim`, `deadline-at`, `countdown`, `dispute-status`, `waybill-no`, `ship-weight`, `return-weight`, `tracking-table`, `listing-variant`, `listing-price`, `chat-log`, `btn-start-appeal`, `btn-view-result`
- Banding: `appeal-reason`, `appeal-text`, `appeal-files`, `appeal-submit`, `appeal-error`; timeout: `error-timeout`, `retry-appeal`
- Hasil: `result-status`, `result-text`, `filing-ref`, `filed-at`, `back-to-orders`

## Catatan penting

- Robot UiPath **cloud (serverless)** tidak bisa mengakses `127.0.0.1` di laptop Anda. Untuk robot di komputer sendiri `localhost` aman. Untuk robot cloud, mock harus di-host di alamat publik (khusus data dummy). Lihat Bagian 5.
- Ground truth (`_truth`) dan hasil simulasi (`_demo_outcome`) tidak pernah keluar lewat UI atau API.
- Server ini untuk demo lokal. Jangan dipakai sebagai layanan publik tanpa pengamanan tambahan.
