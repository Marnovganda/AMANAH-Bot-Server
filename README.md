---
title: IoT Task Reminder System
emoji: 📅
colorFrom: blue
colorTo: purple
sdk: docker
pinned: false
---

# IoT Task Reminder System (KTI Project)

Sistem Pengingat Tugas berbasis IoT menggunakan **ESP32**, **Mic INMP441**, dan **LCD ILI9341 Capacitive Touch**.

## Fitur Utama
- **Voice Registration**: Daftar tugas hanya lewat suara.
- **Auto Deadline Detection**: Menggunakan NLP (dateparser) untuk mendeteksi tanggal.
- **Capacitive Touch Interface**: Konfirmasi simpan/hapus langsung di layar LCD.
- **WhatsApp Notification**: Notifikasi instan dan pengingat harian jam 18:00 WIB via Fonnte.
- **Cloud-Ready**: Dideploy menggunakan Docker di Hugging Face Spaces.

## Cara Deploy ke Hugging Face
1. Buat **New Space** di Hugging Face.
2. Pilih SDK: **Docker**.
3. Pilih Template: **Blank**.
4. Upload semua file dari folder `Otomatisasi` (`app.py`, `requirements.txt`, `Dockerfile`).
5. Tunggu status hingga **Running**.
6. Gunakan URL Space tersebut di kode ESP32.

## Lisensi
Proyek ini dilisensikan di bawah [MIT License](LICENSE).
