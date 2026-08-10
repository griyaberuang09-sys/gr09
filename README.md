# Deploy ke Railway

Folder ini (`telegram_gateway/`) adalah proyek TERPISAH dari `app.py` utama —
deploy folder ini saja ke Railway, bukan seluruh Griya Beruang.

## ⚠️ Sejak Agustus 2026: pakai PaddleOCR, bukan Tesseract lagi

Tesseract terbukti gagal total membaca KTP ber-hologram (0 karakter
terbaca di beberapa foto nyata). PaddleOCR jauh lebih akurat, TAPI punya
laporan kebocoran memori kronis di CPU (banyak issue GitHub 2021-2026,
RAM naik terus tiap request & tidak pernah turun). Ini risiko yang
**sengaja diterima** — mitigasinya proses restart sendiri secara berkala
(lihat `_MAX_OCR_REQUESTS_BEFORE_RESTART` di `main.py`). Konsekuensinya:

- **Butuh RAM lebih besar** dari sebelumnya — minimal 2GB, idealnya 4GB.
  Paket gratis/hobby Railway kemungkinan besar TIDAK CUKUP — cek dan
  upgrade plan kalau perlu di Settings → Usage.
- **Restart Policy WAJIB "On Failure"** (Settings → Deploy → Restart
  Policy) dengan max retries tinggi (mis. 10). Kalau ini disetel "Never",
  katup pengaman memori tidak akan pernah menghidupkan ulang prosesnya
  sendiri, dan container akan OOM-crash tanpa auto-recovery.
- Kalau Railway sering terlihat "restart sendiri" di Deploy Logs, itu
  **memang disengaja** — cek `ocr_requests_since_start` di
  `/api/gateway/health`, itu penghitung sebelum restart otomatis terpicu.

## Langkah

1. **Push folder ini ke repo Git sendiri** (atau subfolder di repo yang sama,
   asal saat setup Railway kamu arahkan "Root Directory" ke `telegram_gateway/`).

2. Di Railway: **New Project → Deploy from GitHub repo** → pilih repo ini.
   - Kalau `telegram_gateway/` bukan root repo, isi **Root Directory** =
     `telegram_gateway` di Settings.
   - Pastikan build method di Settings adalah **Dockerfile**, bukan Nixpacks
     (Nixpacks tidak akan memasang `libgomp1` yang dibutuhkan PaddlePaddle).

3. Di tab **Variables**, isi:
   ```
   TELEGRAM_BOT_TOKEN = <sama persis dengan token bot di config.json server lokal>
   GATEWAY_SECRET      = <string acak buatan sendiri>
   MAX_OCR_BEFORE_RESTART = 15   # opsional, default 15 kalau tidak diisi
   ```
   Buat `GATEWAY_SECRET` dengan:
   ```bash
   openssl rand -hex 32
   ```

4. Di tab **Settings → Networking**, klik **Generate Domain** — Railway
   kasih URL publik otomatis, mis. `https://griya-beruang-gateway-production.up.railway.app`.

5. **Settings → Deploy → Restart Policy = "On Failure"**, max retries
   setinggi mungkin. Ini WAJIB sekarang (lihat penjelasan di atas), bukan
   opsional lagi seperti versi Tesseract sebelumnya.

6. **Pasang Volume** (Settings → Volumes) mount ke `/root/.paddlex` —
   ini SEKARANG LEBIH PENTING dari sebelumnya: tanpa Volume, model
   PaddleOCR (~100MB+) akan ter-download ulang dari server Baidu/HuggingFace
   SETIAP KALI proses restart (termasuk restart otomatis dari katup
   pengaman memori) — bisa bikin gateway lambat/gagal terus-menerus kalau
   sumber modelnya lambat/di-rate-limit. Pasang juga Volume kedua ke
   `/app/data` (draft yang mengambang) seperti sebelumnya, atau gabung
   satu Volume yang mencakup keduanya kalau Railway plan-mu membatasi
   jumlah Volume per service.

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

## Cek kesehatan gateway

```
GET https://<railway-url>/api/gateway/health
```
Menampilkan `TELEGRAM_BOT_TOKEN`/`GATEWAY_SECRET` sudah terset, berapa
submission menunggu dijemput, DAN sekarang juga `ocr_requests_since_start`
+ `ocr_restart_threshold` — pantau ini kalau curiga ada masalah restart.

## Kalau ktp_core.py / wilayah_nik.csv / wilayah_desa.csv di server lokal berubah

Salin ulang ketiganya ke folder ini lalu redeploy — dua tempat ini sengaja
independen (Railway tidak bisa mengakses file server lokal), jadi tidak
otomatis tersinkron. **Sejak Agustus 2026, kedua `ktp_core.py` (lokal &
Railway) sama-sama PaddleOCR** — jadi sekarang aman disalin apa adanya
tanpa perlu edit manual seperti sebelumnya.

