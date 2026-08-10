"""
ktp_core.py (versi Railway/telegram_gateway) — CUMA decode NIK &
verifikasi Telegram WebApp initData. TIDAK ADA OCR SAMA SEKALI di sini.

Sejak Agustus 2026, arsitektur berubah: Mini App di Railway cuma
menerima UPLOAD FOTO KTP (tidak ada OCR di sisi ini sama sekali) —
OCR (PaddleOCR) sepenuhnya jalan di server lokal saat admin membuka
draft dari menu "Draft Telegram". Ini keputusan sadar demi kesederhanaan
& menghindari risiko kebocoran memori PaddleOCR di container Railway
yang resource-nya terbatas (sudah dicoba & terbukti bermasalah).

CATATAN: file ini SEKARANG BERBEDA dari ktp_core.py di server lokal
(yang PaddleOCR-lengkap). Ini sengaja, bukan lupa disinkronkan. Kalau
_WILAYAH_KEC/decode_nik/PROVINSI_KTP di server lokal berubah, salin
bagian itu saja ke sini — JANGAN salin seluruh file server lokal ke
sini (isinya jauh lebih banyak & butuh dependency yang tidak dipasang
di requirements.txt Railway).
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import json
import os
import re
import urllib.parse
from datetime import date, datetime
from typing import Optional

# ---------- Decode NIK ----------
# NIK 16 digit: PP KK CC DDMMYY NNNN (Kepmendagri 300.2.2-2430/2025).
# Deterministik, tidak butuh OCR/foto sama sekali.

PROVINSI_KTP = {
    "11": "Aceh", "12": "Sumatera Utara", "13": "Sumatera Barat", "14": "Riau",
    "15": "Jambi", "16": "Sumatera Selatan", "17": "Bengkulu", "18": "Lampung",
    "19": "Kepulauan Bangka Belitung", "21": "Kepulauan Riau",
    "31": "DKI Jakarta", "32": "Jawa Barat", "33": "Jawa Tengah",
    "34": "DI Yogyakarta", "35": "Jawa Timur", "36": "Banten",
    "51": "Bali", "52": "Nusa Tenggara Barat", "53": "Nusa Tenggara Timur",
    "61": "Kalimantan Barat", "62": "Kalimantan Tengah",
    "63": "Kalimantan Selatan", "64": "Kalimantan Timur", "65": "Kalimantan Utara",
    "71": "Sulawesi Utara", "72": "Sulawesi Tengah", "73": "Sulawesi Selatan",
    "74": "Sulawesi Tenggara", "75": "Gorontalo", "76": "Sulawesi Barat",
    "81": "Maluku", "82": "Maluku Utara",
    "91": "Papua", "92": "Papua Barat", "93": "Papua Selatan",
    "94": "Papua Tengah", "95": "Papua Pegunungan", "96": "Papua Barat Daya",
}

# Tabel kecamatan (kode 6 digit: prov+kab/kota+kecamatan, format Kemendagri —
# sama persis dengan 6 digit pertama NIK).
# Sumber: Kepmendagri No 300.2.2-2430 Tahun 2025 (bundel dari cahyadsn/wilayah).
_WILAYAH_KEC: dict[str, dict] = {}


def _load_wilayah_kec():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wilayah_nik.csv")
    if not os.path.exists(path):
        return
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            _WILAYAH_KEC[row["kode_kec"]] = {
                "kecamatan": row["nama_kec"],
                "kabupaten_kota": row["nama_kab"],
                "provinsi": row["nama_prov"],
            }


_load_wilayah_kec()


def decode_nik(nik: Optional[str]) -> dict:
    """Bongkar NIK 16 digit. Tanggal lahir perempuan ditambah 40 pada
    digit tanggal. Tidak butuh OCR sama sekali — akurat selama NIK-nya
    16 digit yang benar. Nama kecamatan/kabupaten/provinsi diambil dari
    tabel wilayah bundel (kode 6 digit pertama NIK = kode kecamatan)."""
    out = {
        "valid": False, "provinsi": None, "kabupaten_kota": None, "kecamatan": None,
        "kode_kecamatan": None, "jenis_kelamin": None, "tanggal_lahir": None, "usia": None, "catatan": [],
    }
    digits = re.sub(r"\D", "", nik or "")
    if len(digits) != 16:
        if digits:
            out["catatan"].append(f"NIK {len(digits)} digit, seharusnya 16.")
        return out

    hard_fail = False
    prov = digits[0:2]
    kode_kec = digits[0:6]
    out["kode_kecamatan"] = kode_kec
    wil = _WILAYAH_KEC.get(kode_kec)
    if wil:
        out["provinsi"] = wil["provinsi"]
        out["kabupaten_kota"] = wil["kabupaten_kota"]
        out["kecamatan"] = wil["kecamatan"]
    else:
        out["provinsi"] = PROVINSI_KTP.get(prov)  # fallback kalau kode kec tidak ada di tabel
        if not out["provinsi"]:
            out["catatan"].append(f"Kode provinsi {prov} tidak dikenal.")
            hard_fail = True
        else:
            out["catatan"].append(f"Kode kecamatan {kode_kec} tidak ditemukan di tabel wilayah — kecamatan/kab-kota tidak terisi otomatis.")

    dd, mm, yy = int(digits[6:8]), int(digits[8:10]), int(digits[10:12])
    out["jenis_kelamin"] = "PEREMPUAN" if dd > 40 else "LAKI-LAKI"
    if dd > 40:
        dd -= 40

    if not (1 <= mm <= 12):
        out["catatan"].append(f"Bulan lahir {mm:02d} pada NIK tidak valid.")
        return out

    tahun = 2000 + yy
    if tahun > date.today().year:
        tahun -= 100
    try:
        lahir = date(tahun, mm, dd)
    except ValueError:
        out["catatan"].append(f"Tanggal lahir {dd:02d}-{mm:02d}-{tahun} pada NIK tidak valid.")
        return out

    out["tanggal_lahir"] = lahir.isoformat()
    today = date.today()
    out["usia"] = today.year - lahir.year - ((today.month, today.day) < (lahir.month, lahir.day))
    if digits[12:16] == "0000":
        out["catatan"].append("Nomor urut NIK 0000 — periksa kembali.")
        hard_fail = True
    out["valid"] = not hard_fail
    return out


# ---------- Verifikasi Telegram WebApp initData ----------

def verify_telegram_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> Optional[dict]:
    """Validasi initData dari Telegram WebApp sesuai algoritma resmi:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    Mengembalikan dict user (id, username, first_name) kalau valid & belum
    kedaluwarsa, None kalau tidak valid/dipalsukan."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
        data = dict(pairs)
        received_hash = data.pop("hash", None)
        if not received_hash:
            return None

        check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(computed_hash, received_hash):
            return None

        auth_date = int(data.get("auth_date", "0"))
        if auth_date and (datetime.now().timestamp() - auth_date) > max_age_seconds:
            return None  # initData kedaluwarsa — cegah replay dari data lama yang bocor

        user = json.loads(data.get("user", "{}"))
        if not user.get("id"):
            return None
        return user
    except Exception:
        return None
