# PEMBELA SELLER — prototipe inti (Indosat Automation Hackathon)

Bantu penjual marketplace menyiapkan banding sengketa dengan **bukti nyata milik penjual**. Tidak ada banding terkirim tanpa persetujuan manusia.
Seluruh demo memakai **mock "MarketMock" + data dummy**. Nama merek/produk fiktif.

## Isi
| Folder | Fungsi |
|---|---|
| `marketmock/` | Seller Center tiruan (Flask) + mode chaos (layout, testid, captcha, timeout) + `DATASET=` untuk memuat 30 kasus |
| `pembela/` | Inti deterministik: `rubric.py` (skor), `validator.py` (skema, sitasi, kata terlarang, hash draf), `evidence.py` (pengumpul bukti), `robot_http.py` (pengajuan + gerbang), `approval.py`, `pipeline.py` (alur per kasus), `backends.py` |
| `agents/` | `schemas.json` (kontrak I/O 3 agent) + `agent1/2/3_system.txt` (prompt untuk Agent Builder) |
| `datagen/` | `generate_dummy.py`: 30 kasus (24 inti + 6 khusus), ground truth, media placeholder |
| `eval/` | `run_eval.py` (pipeline vs ground truth), `export_agent_inputs.py` (evaluation set untuk Agent Builder), `score_agent_outputs.py` (menilai agent sungguhan) |
| `tests/test_core.py` | 32 tes keselamatan/konsistensi |
| `console/` | **Konsol web** (dashboard, detail kasus, tombol setujui/tolak/catatan) + mock dalam satu server |
| `demo.py` | Demo konsol-terminal satu kasus dengan persetujuan y/N |

## Menjalankan konsol web (cara termudah)
```
python console/server.py
```
Buka **http://127.0.0.1:5000/console**. Klik "Analisis semua kasus", buka kasus berstatus "Perlu keputusanmu" (mis. DSP-001),
baca draf, centang persetujuan, lalu "Setujui & ajukan". Seller Center tiruan ada di http://127.0.0.1:5000/orders (seller_demo / demo123).
"Reset demo" mengembalikan semua kasus dan berkas bukti. Tes konsol: `python tests/test_console.py`.

Di halaman kasus penjual bisa: **menyatakan isi video** (centang sendiri; sistem tidak menonton video), **menambah bukti** (unggah + waktu rekam),
dan **memilih berkas** bila ada dua kandidat (yang tidak dipilih dipindah ke `_tidak_dipakai`, tidak dihapus). Setiap perubahan memicu analisis ulang
dan draf lama tidak berlaku. Unggahan masuk ke salinan kerja `.demo_media/` (data asli di `data/media/` tidak berubah).
Coba: DSP-005 (unggah video packing waktu rekam 2026-10-21T10:00, centang kedua pernyataan -> layak banding);
isi waktu rekam setelah 2026-10-21T15:00 untuk melihat flag "video direkam setelah barang diserahkan ke kurir".

## Menjalankan lewat terminal
```
pip install -r requirements.txt
python3 datagen/generate_dummy.py          # (sudah ada di data/)
python3 tests/test_core.py                 # 32 tes
python3 eval/run_eval.py                   # laporan di eval/report.md
python3 demo.py DSP-001                    # demo interaktif; DSP-005 collect-more; DSP-006 tidak banding
# server nyata (untuk robot UiPath):
DATASET=data/cases.json RESOLVE_AFTER_S=30 python3 marketmock/app.py   # http://127.0.0.1:5000  (seller_demo / demo123)
```

## Jaminan keselamatan yang DIUJI (tests/test_core.py)
- Tanpa approval / approval REJECT / hash draf berubah / berkas bukti berubah sejak intake / sisa waktu < buffer / status bukan OPEN / pengajuan ganda → **diblokir** (`GateError`).
- Edit manusia yang memuat klaim tanpa bukti ditolak validator; edit sah wajib di-approve ulang (hash baru).
- Timeout 504 → coba lagi dengan cek status dulu → tepat satu pengajuan.
- Tenggat tidak ada → eskalasi (tidak ada tenggat default). Sisa waktu 10 mnt → eskalasi.
- Teks klaim berisi perintah tersembunyi tidak terbawa ke ringkasan maupun mengubah hasil.
- Berkas asli tidak pernah diubah; berkas bernomor pesanan lain dikecualikan dan di-flag; video direkam setelah barang diserahkan ke kurir di-flag HIGH.

## Hasil eval baseline — BACA CATATANNYA
Status akhir sesuai 21/30; semua 9 kasus tersisa adalah klaim **parafrase tanpa kata kunci** (baseline kata kunci memang gagal: 0/9).
Itu **bukan** akurasi agent LLM. Baseline hanya penguji pipeline. Nilai LLM yang sebenarnya diukur dengan:
1. `python3 eval/export_agent_inputs.py` → `eval/agent_inputs/*.jsonl` (input + jawaban acuan) untuk Evaluation Set di Agent Builder.
2. Simpan keluaran agent per baris JSON → `python3 eval/score_agent_outputs.py --agent1 ... --agent2 ... --agent3 ...`.
`eval/agent_inputs/contoh_keluaran_baseline/` hanya contoh format (agent 1 di sana memakai jenis kasus dari ground truth, jadi 30/30 tidak bermakna).

## Keterbatasan jujur
- **Circularity**: ground truth dihitung dari rubrik yang sama dengan pipeline. Tes ini membuktikan implementasi konsisten, BUKAN rubrik benar di dunia nyata. Bobot/ambang rubrik = ASUMSI, kalibrasi dengan wawancara penjual.
- Isi video **tidak dibaca**; klaim isi video hanya berasal dari pernyataan penjual (`seller_attestations` di `meta.json`).
- Media di `data/media` adalah placeholder, bukan video asli. Hasil sengketa mock (menang/kalah) = **ILUSTRASI**: sejak v6 skenario mock mengikuti skor bukti (skor ≥75 → menang, selain itu kalah) agar demo terbaca masuk akal; ini BUKAN penilaian atau prediksi hasil marketplace sungguhan.
- **Bukti sama di pesanan berbeda** (v6): berkas dengan isi identik di folder pesanan lain diberi flag `DUPLICATE_ACROSS_ORDERS` (HIGH, −15 poin). Deteksi hanya mencocokkan hash berkas persis; video yang direkam ulang/diedit tidak terdeteksi.
- Halaman hasil mock menampilkan "Uraian banding" (teks yang benar-benar dikirim) agar bisa dicek penjual.
- Tidak ada angka dampak/waktu baseline: semua harus dari wawancara penjual nyata atau sumber terkutip.
- Robot di sini (HTTP) adalah stand-in. Versi UiPath harus meniru pemeriksaan gerbang di `robot_http.file_appeal` sebelum membuka browser.
- Input eval memuat `deadline_at` relatif waktu ekspor; ekspor ulang bila sudah lama.

## Pemetaan ke UiPath (yang dikerjakan di tenant-mu)
| Di sini | Di UiPath |
|---|---|
| `agents/*_system.txt` + `schemas.json` | Agent Builder (3 agent), skema ke Data Manager |
| `pipeline.run_case` | Maestro (BPMN) |
| `evidence.collect` | Robot `PS_Evidence_Collect` (API + folder lokal) |
| `ScriptedApprover` | Task Action Center (tampilkan draf, skor, flag; tombol Setujui/Tolak/Edit) |
| `robot_http.file_appeal` | Robot `PS_Appeal_File` (UI automation ke MarketMock; ulangi cek gerbang) |
| `rubric.score`, `validator` | Kode deterministik (Coded workflow / Python) di luar agent |
