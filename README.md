# Deploy ke Railway

Folder ini (`telegram_gateway/`) adalah proyek TERPISAH dari `app.py` utama —
deploy folder ini saja ke Railway, bukan seluruh Griya Beruang.

## Arsitektur (Agustus 2026): upload foto saja, TIDAK ADA OCR di Railway

Setelah beberapa putaran percobaan (Tesseract gagal baca KTP ber-hologram,
lalu PaddleOCR kena macam-macam masalah teknis di Railway — libGL hilang,
error internal PIR/oneDNN, risiko kebocoran memori kronis), diputuskan:
**Railway cuma menerima upload foto KTP mentah dari penghuni lewat Mini
App, TIDAK memproses OCR sama sekali.** OCR (PaddleOCR) sepenuhnya jalan
di **server lokal**, otomatis begitu admin membuka draft dari menu
"Draft Telegram" — admin tetap dapat pengalaman auto-fill yang sama,
cuma titik prosesnya pindah ke server yang resource-nya jauh lebih leluasa
dan sudah terbukti stabil.

Konsekuensinya, folder ini sekarang JAUH lebih ringan dari versi
sebelumnya:
- Tidak ada `paddlepaddle`/`paddleocr`/`numpy`/`Pillow` di `requirements.txt`.
- Tidak ada `libgomp1`/`libgl1`/`libglib2.0-0` di `Dockerfile`.
- Tidak ada endpoint `/api/telegram-app/ocr` sama sekali di `main.py`.
- Tidak ada katup pengaman restart otomatis (tidak perlu lagi, tidak ada
  proses berat yang bisa bocor memori di sini).
- Mini App (`telegram_checkin.html`) cuma 2 langkah: Nomor HP (share
  kontak Telegram) → Unggah Foto KTP → Kirim. Tidak ada form NIK/data
  KTP di Mini App sama sekali.

## Langkah

1. **Push folder ini ke repo Git sendiri** (atau subfolder di repo yang sama,
   asal saat setup Railway kamu arahkan "Root Directory" ke `telegram_gateway/`).

2. Di Railway: **New Project → Deploy from GitHub repo** → pilih repo ini.
   - Kalau `telegram_gateway/` bukan root repo, isi **Root Directory** =
     `telegram_gateway` di Settings.
   - Build method **Dockerfile** atau Nixpacks otomatis — dua-duanya aman
     sekarang, karena tidak ada lagi dependency sistem khusus yang
     dibutuhkan (beda dari waktu masih pakai Tesseract/PaddleOCR).

3. Di tab **Variables**, isi:
   ```
   TELEGRAM_BOT_TOKEN = <sama persis dengan token bot di config.json server lokal>
   GATEWAY_SECRET      = <string acak buatan sendiri>
   ```
   Buat `GATEWAY_SECRET` dengan:
   ```bash
   openssl rand -hex 32
   ```

4. Di tab **Settings → Networking**, klik **Generate Domain** — Railway
   kasih URL publik otomatis, mis. `https://griya-beruang-gateway-production.up.railway.app`.

5. (Opsional tapi disarankan) Pasang **Volume** di Settings → mount ke
   `/app/data` — supaya draft yang lagi "mengambang" tidak hilang kalau
   container restart persis di jendela sebelum sempat dijemput server lokal.
   Tanpa Volume pun tetap jalan; risikonya kecil (cuma submission yang
   sangat kebetulan waktunya, dan penghuni tinggal submit ulang). Restart
   Policy juga tidak lagi krusial seperti versi PaddleOCR sebelumnya —
   boleh dibiarkan default.

## Setelah Railway jalan, kembali ke server lokal

Isi di `config.json` server lokal (BUKAN di Railway):

```json
"telegram_gateway_url": "https://griya-beruang-gateway-production.up.railway.app",
"telegram_gateway_secret": "<harus SAMA PERSIS dengan GATEWAY_SECRET di Railway>",
"telegram_miniapp_url": "https://griya-beruang-gateway-production.up.railway.app/telegram-checkin"
```

Restart `app.py`, lalu panggil sekali (login dulu sebagai admin):

```bash
curl -X POST https://<server-lokal-atau-wireguard>/api/telegram/set-menu-button \
  -H "Cookie: session_token=<token>"
```

Server lokal otomatis menjemput draft baru tiap 60 detik (job `telegram_gateway_pull`
di scheduler). Untuk tes cepat tanpa menunggu jadwal:

```bash
curl -X POST https://<server-lokal>/api/telegram/gateway-pull-now \
  -H "Cookie: session_token=<token>"
```

**Setelah draft dijemput:** buka menu "Draft Telegram" di panel admin,
klik "Setujui & Lanjut Check-in" pada satu draft — kalau drafnya cuma
berisi foto (tanpa NIK), sistem OTOMATIS menjalankan OCR PaddleOCR lokal
terhadap foto itu dan mengisi form. Cek hasilnya sebelum Simpan, seperti
biasa.

## Cek kesehatan gateway

```
GET https://<railway-url>/api/gateway/health
```
Menampilkan `TELEGRAM_BOT_TOKEN`/`GATEWAY_SECRET` sudah terset dan berapa
submission menunggu dijemput.

## Kalau ktp_core.py di server lokal berubah

Folder ini punya `ktp_core.py` versi RINGKAS (cuma `decode_nik` +
`verify_telegram_init_data`) — SENGAJA BEDA dari `ktp_core.py` di server
lokal (yang lengkap dengan PaddleOCR). **JANGAN salin seluruh file dari
server lokal ke sini** — kalau `_WILAYAH_KEC`/`decode_nik`/`PROVINSI_KTP`
di server lokal berubah, salin bagian itu saja secara manual.
