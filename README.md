# SiteCloner

Aplikasi lokal untuk **menyalin seluruh website** dari satu alamat domain dan menjalankannya kembali di komputer Anda, tanpa internet.

> **Status: MVP (v0.1.0)** — Tahap 1 selesai: crawl, mirror, dan preview lokal.
> Analisis AI, SQLite, API otomatis, dan terjemahan ada di [Roadmap](#roadmap).

## Fitur

- **UI web sederhana**: ketik domain, klik *Mulai clone*, pantau progres, buka hasilnya.
- **Crawler async** (httpx): cepat, paralel, dengan jeda dan batas halaman yang bisa diatur.
- **Mirror yang bisa dibuka offline**: HTML, CSS (`url()` dan `@import`), `srcset`, gambar, font, dan skrip ditulis ulang menjadi link relatif.
- **Aset dari domain lain** (CDN, font, gambar) ikut diunduh ke folder `_ext/`.
- **Mode render JavaScript** (opsional, Playwright) untuk situs React/Vue/SPA.
- **Stop dan lanjutkan**: antrean disimpan di SQLite, jadi proses bisa dihentikan lalu dilanjutkan atau diperbarui.
- **Filter URL** dengan regex (hanya/lewati), batas kedalaman, dan batas jumlah halaman.
- **Patuh robots.txt** (bawaan aktif).
- **Preview lokal** di `http://127.0.0.1:8000/preview/<nama>/` dan **unduh ZIP** hasil mirror.
- Data tiap domain terpisah dalam satu folder, mudah dipindah atau dihapus.

## Persyaratan

- Python **3.10+** (diuji di 3.12)
- Windows, macOS, atau Linux
- Opsional untuk mode render JavaScript: Playwright + Chromium (~150 MB)

## Instalasi

```bash
# 1. Ekstrak ZIP, masuk ke folder
cd sitecloner

# 2. Buat virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Pasang dependensi
pip install -r requirements.txt
```

**Opsional, untuk situs yang dibuat dengan JavaScript (SPA):**

```bash
pip install -r requirements-js.txt
playwright install chromium
```

## Penggunaan

```bash
python run.py
```

Browser terbuka otomatis di `http://127.0.0.1:8000` (ganti port dengan `PORT=9000 python run.py`).

1. Masukkan domain utama, misalnya `https://contoh.com`.
2. (Opsional) buka **Pengaturan lanjutan**.
3. Klik **Mulai clone** dan tunggu status menjadi *Selesai*.
4. Klik **Buka situs lokal** untuk melihat hasilnya, atau **Unduh ZIP**.

Hasil mirror berisi file statis biasa. Untuk menjalankannya di luar aplikasi:

```bash
cd data/projects/contoh.com/mirror
python -m http.server 8080
```

### Pengaturan

| Opsi | Bawaan | Keterangan |
|---|---|---|
| Maks. halaman | 200 | Batas jumlah halaman HTML |
| Kedalaman link | 3 | Jumlah klik dari halaman awal |
| Koneksi paralel | 6 | Naikkan hati-hati agar tidak membebani server target |
| Jeda per permintaan | 0.2 dtk | Per koneksi |
| Patuhi robots.txt | aktif | Nonaktifkan hanya untuk situs milik sendiri |
| Aset domain lain | aktif | Unduh CDN, font, dan gambar eksternal |
| Render JavaScript | nonaktif | Perlu Playwright |
| Hapus JavaScript | nonaktif | Disarankan bersama render JS |
| Mulai dari nol | nonaktif | Hapus salinan lama untuk domain itu |
| Include / Exclude | kosong | Regex, satu per baris |

### Struktur data

```
data/projects/<domain>/
├─ config.json    # pengaturan yang dipakai
├─ crawl.db       # antrean dan status URL (SQLite)
├─ raw/           # HTML/CSS asli sebelum link ditulis ulang
└─ mirror/        # hasil akhir, siap dibuka offline
```

## Cara kerja

```
input domain → crawl (httpx / Playwright) → simpan raw + aset
            → finalize: tulis ulang link relatif → mirror/ → preview
```

1. URL dinormalisasi, didedup, dan disimpan di `crawl.db` dengan status `pending/running/done/error`.
2. HTML dan CSS dipindai untuk menemukan halaman dan aset baru.
3. Setelah antrean habis, semua link ditulis ulang berdasarkan daftar URL yang berhasil diunduh. Link ke halaman yang tidak tersalin diarahkan kembali ke URL aslinya.

## API

Dokumentasi interaktif tersedia di `http://127.0.0.1:8000/docs`.

| Method | Endpoint | Fungsi |
|---|---|---|
| POST | `/api/jobs` | Mulai clone (atau lanjutkan jika proyek sudah ada) |
| GET | `/api/projects` | Daftar proyek dan progres |
| POST | `/api/projects/{nama}/stop` | Hentikan |
| POST | `/api/projects/{nama}/resume` | Lanjutkan atau perbarui |
| DELETE | `/api/projects/{nama}` | Hapus proyek |
| GET | `/api/projects/{nama}/errors` | URL yang gagal |
| GET | `/api/projects/{nama}/download` | ZIP hasil mirror |
| GET | `/preview/{nama}/…` | Situs hasil clone |

## Pengembangan

```
sitecloner/
├─ app/
│  ├─ crawler.py   # frontier, fetcher, ekstraksi, penulis ulang link
│  └─ main.py      # FastAPI: API, preview, ZIP
├─ static/index.html   # UI (HTML + JS murni, tanpa build)
├─ run.py
└─ data/projects/
```

- Jalankan dengan auto-reload: `uvicorn app.main:app --reload`
- Lokasi data dapat diubah lewat `SITECLONER_DATA=/path/data`.
- Di Windows, Playwright butuh event loop Proactor. Gunakan `python run.py` (tanpa `--reload`).
- Titik perluasan: fungsi `Crawler.fetch()` (sumber konten) dan `Crawler.finalize()` (pasca-proses). Tahap 2 dan seterusnya akan membaca `crawl.db` dan `mirror/`.

## Roadmap

- [x] **Tahap 1 (MVP)**: crawl, mirror, preview lokal, UI
- [ ] **Tahap 2**: analisis AI. Kelompokkan halaman per template, usulkan skema, ekstraksi dengan selector, bangun SQLite
- [ ] **Tahap 3**: generator API (CRUD, filter, pencarian, Swagger) dan ekspor proyek
- [ ] **Tahap 4**: terjemahan ke bahasa Indonesia (batch, cache, glosarium, UI review, estimasi biaya)
- [ ] **Berikutnya**: re-crawl inkremental (ETag) dan diff, screenshot per template, pencarian full-text, scheduler, mode AI lokal (Ollama), ekspor CSV/JSON/WXR/CI4
- [ ] **Ditunda**: login/cookie crawling dan autentikasi aplikasi

## Batasan yang diketahui

- Fitur yang butuh server (form, pencarian, login, API dinamis) tidak berfungsi di salinan statis.
- Situs dengan proteksi anti-bot (misalnya Cloudflare) dapat menolak crawler.
- Pemuatan konten lewat JavaScript hanya tertangkap jika mode render JavaScript aktif.
- Video streaming (HLS/DASH) dan konten di balik login belum didukung.
- Pada mode render, salinan terbaik dihasilkan bersama opsi *Hapus JavaScript*.

## Etika dan hukum

Gunakan hanya pada situs milik Anda atau dengan izin pemiliknya. Hormati hak cipta, ketentuan layanan, dan `robots.txt`. Pertahankan jeda dan koneksi paralel yang wajar agar tidak membebani server orang lain. Anda bertanggung jawab atas penggunaan alat ini.

## Pemecahan masalah

| Gejala | Solusi |
|---|---|
| "Halaman awal gagal diunduh" | Periksa alamat. Alamat tanpa `http://` dianggap `https://` |
| Hasil kosong pada situs modern | Aktifkan *Render JavaScript* dan *Hapus JavaScript* |
| Banyak status gagal | Klik angka gagal untuk melihat URL dan alasannya, lalu turunkan koneksi paralel |
| Halaman tertentu tidak tersalin | Naikkan batas halaman atau kedalaman, lalu klik *Perbarui* |
| `playwright` belum terpasang | Jalankan langkah instalasi opsional di atas |

## Lisensi

MIT. Lihat berkas `LICENSE`.
