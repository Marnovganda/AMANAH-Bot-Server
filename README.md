📅 AMANAH Backend & WhatsApp Bot Server

Repositori ini berisi berkas *backend server* untuk sistem **AMANAH (Asisten Manajemen Notifikasi Tugas Harian)**. Layanan berbasis **Flask (Python)** ini bertindak sebagai pusat pemrosesan data tugas, integrasi AI Gemini, basis data Supabase, serta pengelola otomatisasi bot WhatsApp via Fonnte API[cite: 8, 10].
Server ini dirancang untuk dapat di-deploy secara mudah di **Hugging Face Spaces** menggunakan Docker.


## ⚙️ Fungsi Utama Server (`app.py`)

* **Endpoint Penerima Audio ESP32 (`/upload`)**: Menerima aliran data (*stream*) rekaman audio `.wav` dari perangkat fisik ESP32-S3 di kelas.
* **Pengolahan AI Gemini**: Memproses audio maupun teks pesan untuk mengekstraksi mata pelajaran, deskripsi tugas, dan tanggal *deadline* secara otomatis.
* **Manajemen Tugas Pending (`/confirm`)**: Menangani konfirmasi tugas dari perangkat fisik dan menanyakan informasi yang belum lengkap ke grup WhatsApp.
* **Otomatisasi Bot WhatsApp (`/webhook`)**: Menerima pesan dari grup WhatsApp untuk mengelola tugas (perintah `/tugas`, `/add`, `/edit`, `/hapus`, dll.).
* **Penjadwal Pengingat Harian (APScheduler)**: Mengirimkan pengingat tugas aktif setiap pukul **18:00 WIB** (waktu dapat diubah) secara otomatis[cite: 10].

---

## 📂 Struktur Repositori

```
├── app.py              # Backend utama (Flask API, Gemini AI, & Bot Webhook)
├── Dockerfile          # Konfigurasi container untuk deployment di Hugging Face
├── requirements.txt    # Daftar dependensi modul Python
├── .gitattributes      # Konfigurasi pelacakan Git LFS
└── README.md           # Dokumentasi repositori backend

```

## 🚀 Panduan Deployment ke Hugging Face Spaces
### 1. Buat Space Baru

1. Buka [Hugging Face Spaces](https://huggingface.co/spaces).
2. Klik **Create new Space**.
3. Masukkan nama Space.
4. Pilih **SDK: Static** dan pilih template **Blank**.



### 2. Atur Secrets / Environment Variables
Masuk ke menu **Settings -> Variables and secrets** pada Space kamu, lalu tambahkan *secrets* berikut:
* `SUPABASE_URL`: URL proyek Supabase kamu.
* `SUPABASE_KEY`: API Key / Anon Key Supabase.
* `GEMINI_API_KEY`: API Key utama Google Gemini.
* `GEMINI_API_KEY_BACKUP`: API Key cadangan Gemini (opsional, untuk *fallback*).
* `FONNTE_TOKEN`: Token API dari Fonnte.
* `FONNTE_GROUP_ID`: ID grup WhatsApp tujuan (misal: `120363407069913309@g.us`).

### 3. Upload File
Unggah seluruh berkas di repositori ini (`app.py`, `Dockerfile`, `requirements.txt`, `.gitattributes`, `README.md`) ke Space kamu. Server akan berjalan secara otomatis pada port `7860`.

---

## 🔗 Hubungkan Webhook Fonnte
1. Buka dashboard [Fonnte](https://fonnte.com).
2. Masuk ke menu **Webhook**.
3. Isikan URL webhook server kamu:
```text
https://[nama-user]-[nama-space].hf.space/webhook
```
4. Simpan perubahan.
---


## 🤖 Perintah Bot WhatsApp

Seluruh anggota grup dapat menggunakan perintah berikut di WhatsApp:
* `/tugas` — Menampilkan seluruh daftar tugas aktif.
* `/add [deskripsi]` — Menambah tugas baru via teks.
* `/edit [nomor] [perubahan]` — Mengubah data tugas.
* `/hapus [nomor/semua]` — Menghapus tugas.
* `/waktu [jam:menit]` — Cek atau ubah jam pengingat harian.
* `/deadline [YYYY-MM-DD]` — Melengkapi tanggal tugas pending.
* `/mapel [nama_mapel]` — Melengkapi mata pelajaran tugas pending.
---

## 📝 Lisensi

Proyek ini dibuat untuk kebutuhan Karya Tulis Ilmiah (KTI) dan dilisensikan di bawah [MIT License](https://www.google.com/search?q=LICENSE).

```
