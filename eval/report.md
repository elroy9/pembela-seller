# Laporan evaluasi (30 kasus dummy)

Backend: **BaselineBackend**. Ini baseline berbasis aturan, BUKAN LLM: angka di bawah menguji pipeline, bukan akurasi agent.
Ground truth dihitung dari rubrik yang sama (ASUMSI) -> uji konsistensi, bukan validasi dunia nyata.

| Metrik | Hasil |
|---|---|
| Jenis kasus benar (triase) | 21/30 |
| ...pada klaim 'hard' (parafrase tanpa kata kunci) | 0/9 |
| Kekuatan bukti sesuai (kasus yang dieskalasi sebelum dinilai dihitung salah) | 18/29 |
| Rekomendasi sesuai | 18/28 |
| Status akhir sesuai | 21/30 |
| Injeksi prompt tidak terbawa | 30/30 |

## Ketidaksesuaian

| Kasus | Tier | Harapan | Hasil | Alasan |
|---|---|---|---|---|
| DSP-002 | STRONG_B | OUTCOME_RECORDED | ESCALATED_TO_HUMAN | triase tidak yakin (jenis kasus tidak jelas) |
| DSP-004 | MEDIUM_B | OUTCOME_RECORDED | ESCALATED_TO_HUMAN | triase tidak yakin (jenis kasus tidak jelas) |
| DSP-008 | STRONG_B | OUTCOME_RECORDED | ESCALATED_TO_HUMAN | triase tidak yakin (jenis kasus tidak jelas) |
| DSP-010 | MEDIUM_B | OUTCOME_RECORDED | ESCALATED_TO_HUMAN | triase tidak yakin (jenis kasus tidak jelas) |
| DSP-014 | STRONG_B | OUTCOME_RECORDED | ESCALATED_TO_HUMAN | triase tidak yakin (jenis kasus tidak jelas) |
| DSP-016 | MEDIUM_B | OUTCOME_RECORDED | ESCALATED_TO_HUMAN | triase tidak yakin (jenis kasus tidak jelas) |
| DSP-020 | STRONG_B | OUTCOME_RECORDED | ESCALATED_TO_HUMAN | triase tidak yakin (jenis kasus tidak jelas) |
| DSP-022 | MEDIUM_B | OUTCOME_RECORDED | ESCALATED_TO_HUMAN | triase tidak yakin (jenis kasus tidak jelas) |
| DSP-028 | SPECIAL_MESSY_NAMING | OUTCOME_RECORDED | ESCALATED_TO_HUMAN | triase tidak yakin (jenis kasus tidak jelas) |
