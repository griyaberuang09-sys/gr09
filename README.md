# Mini App Telegram — Penghuni Submit KTP Sendiri (via Railway)

## Arsitektur

```
Penghuni (Telegram)
      │
      ▼
Railway (telegram_gateway/) ── publik, HTTPS otomatis
  - serve halaman /telegram-checkin
  - OCR (tesseract) jalan di sini
  - simpan submission SEMENTARA (buffer)
      │
      │  ◄── server lokal MENJEMPUT tiap 60 detik (outbound, keluar dari WireGuard)
      │      TIDAK PERNAH ada koneksi masuk ke server lokal untuk fitur ini
      ▼
Server lokal (app.py, di belakang WireGuard)
  - tabel telegram_checkin_drafts = SATU-SATUNYA sumber kebenaran
  - admin review & approve di menu Check-in Penyewa seperti biasa
```

Kenapa begini: server lokal cuma bisa diakses lewat WireGuard, dan penghuni
tidak punya akses itu. Solusinya BUKAN membuka jaringan rumah ke internet
(itu memperbesar area serang), tapi server lokal yang aktif mengambil data
dari Railway secara berkala — pola yang sama persis dengan notifikasi
jatuh tempo yang sudah jalan sekarang (selalu outbound, tidak pernah inbound).

## Berkas yang berubah/baru

- `app.py` — endpoint `/telegram-checkin` & `/api/telegram-app/*` **dipindah**
  ke Railway (dihapus dari sini). Diganti job polling `pull_telegram_gateway_drafts()`
  yang jalan tiap 60 detik, plus endpoint admin `/api/telegram/gateway-pull-now`
  untuk tes manual.
- `ktp_core.py` — **baru**, modul bersama (decode NIK, OCR, verifikasi
  Telegram initData) dipakai oleh `app.py` DAN `telegram_gateway/`. Kalau
  diubah, salin ulang ke `telegram_gateway/ktp_core.py`.
- `telegram_gateway/` — **baru**, proyek terpisah untuk di-deploy ke Railway.
  Lihat `telegram_gateway/README.md` untuk langkah deploy lengkap.
- `index.html` — tidak berubah dari sebelumnya (tombol "📲 Draft Telegram",
  modal review, approve/reject tetap bekerja sama seperti dulu — cuma
  sumber datanya sekarang lewat penjemputan, bukan submission langsung).

## Setup singkat

1. Deploy `telegram_gateway/` ke Railway (detail: `telegram_gateway/README.md`).
   Catat URL publiknya, mis. `https://xxx.up.railway.app`.
2. Set env var di Railway: `TELEGRAM_BOT_TOKEN`, `GATEWAY_SECRET` (buat sendiri, acak).
3. Isi di `config.json` server lokal:
   ```json
   "telegram_gateway_url": "https://xxx.up.railway.app",
   "telegram_gateway_secret": "<SAMA PERSIS dengan GATEWAY_SECRET di Railway>",
   "telegram_miniapp_url": "https://xxx.up.railway.app/telegram-checkin"
   ```
4. Restart `app.py`, login sebagai admin, panggil sekali:
   ```
   POST /api/telegram/set-menu-button
   ```

## Alur penghuni & admin

Tidak berubah dari sebelumnya — penghuni buka bot → tombol menu → isi HP +
foto KTP → OCR otomatis → kirim. Admin dapat notifikasi Telegram + badge
**"📲 Draft Telegram"** di menu Penyewa, klik **Setujui & Lanjut Check-in**
untuk membuka form Check-in yang sudah ter-prefill, pilih Kamar, Simpan.

## Keamanan

- `telegram_gateway/api/telegram-app/*` (Railway, publik) dilindungi tanda
  tangan HMAC Telegram (`initData`) — sama seperti desain sebelumnya.
- `telegram_gateway/api/gateway/*` (dipanggil server lokal) dilindungi
  `GATEWAY_SECRET` terpisah — BUKAN token bot Telegram — supaya kompromi
  salah satu secret tidak otomatis membuka yang lain.
- Server lokal **tidak pernah** menerima koneksi masuk untuk fitur ini —
  jaringan rumah tetap tertutup dari internet publik seperti sebelumnya.
- Data di Railway bersifat sementara (dihapus begitu berhasil dijemput) —
  database penuh (tenants, pembayaran, kunci pintu) tetap 100% di server lokal.
