"""
ktp_core.py — Logika bersama untuk decode NIK, OCR KTP, dan verifikasi
Telegram WebApp initData. Dipakai oleh DUA proyek terpisah:

  1. app.py (server lokal, di belakang WireGuard) — untuk OCR di menu
     Check-in Penyewa (webcam/unggah admin) dan validasi NIK manual.
  2. telegram_gateway/main.py (di Railway, publik) — untuk OCR & validasi
     saat penghuni submit KTP lewat Mini App Telegram.

PENTING: file ini harus identik di kedua tempat. Kalau salah satu diubah,
salin ulang ke folder satunya — supaya hasil decode NIK & parsing OCR
tidak pernah berbeda antara server lokal dan gateway publik.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import re
import urllib.parse
from datetime import date, datetime
from typing import Optional

import pytesseract
from PIL import Image, ImageOps

# ---------- Decode NIK ----------
# NIK 16 digit: PP KK CC DDMMYY NNNN (Kepmendagri 300.2.2-2430/2025).
# Ini sumber kebenaran yang deterministik — dipakai untuk memvalidasi/
# mengoreksi hasil OCR, bukan sekadar tampilan tambahan.

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
# sama persis dengan 6 digit pertama NIK). Dipakai untuk mengisi nama
# kecamatan & kabupaten/kota otomatis dari NIK, tanpa OCR sama sekali.
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
        "jenis_kelamin": None, "tanggal_lahir": None, "usia": None, "catatan": [],
    }
    digits = re.sub(r"\D", "", nik or "")
    if len(digits) != 16:
        if digits:
            out["catatan"].append(f"NIK {len(digits)} digit, seharusnya 16.")
        return out

    hard_fail = False
    prov = digits[0:2]
    kode_kec = digits[0:6]
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


# ---------- OCR KTP ----------

_ALAMAT_STOP_LABELS = [
    "RT/RW", "RT / RW", "KEL/DESA", "KEL / DESA", "KELURAHAN", "DESA",
    "KECAMATAN", "AGAMA", "STATUS PERKAWINAN", "STATUS PERKAWIN",
    "PEKERJAAN", "KEWARGANEGARAAN", "BERLAKU HINGGA", "JENIS KELAMIN",
    "GOL. DARAH", "GOL DARAH",
]


def _label_value(line: str, labels: list) -> Optional[str]:
    up = line.upper()
    for lab in labels:
        idx = up.find(lab)
        if idx != -1:
            rest = line[idx + len(lab):]
            rest = rest.lstrip(": ").strip(" :.-")
            return rest.strip()
    return None


def _to_iso_date(raw: str) -> Optional[str]:
    m = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", raw)
    if not m:
        return None
    d, mo, y = m.groups()
    if len(y) == 2:
        y = ("19" + y) if int(y) > 30 else ("20" + y)
    try:
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    except ValueError:
        return None


def parse_ktp_text(text: str) -> dict:
    """Heuristik ekstraksi field dari teks hasil OCR KTP Indonesia.
    Tidak sempurna (kualitas tergantung foto) — hasil tetap harus dicek/diedit user."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    data = {"nik": None, "nama": None, "tempat_lahir": None, "tgl_lahir": None, "alamat": None}
    alamat_parts = []
    capturing_alamat = False

    for line in lines:
        up = line.upper()

        if not data["nik"] and "NIK" in up:
            digits = re.sub(r"\D", "", line)
            if len(digits) >= 15:
                data["nik"] = digits[:16]

        if not data["nama"] and "NAMA" in up and "NIK" not in up:
            v = _label_value(line, ["NAMA"])
            if v and len(v) > 1:
                data["nama"] = v

        if not data["tempat_lahir"] and ("TEMPAT" in up and "LAHIR" in up):
            v = _label_value(line, [
                "TEMPAT/TGL LAHIR", "TEMPAT/TANGGAL LAHIR", "TEMPAT, TGL LAHIR",
                "TEMPAT TGL LAHIR", "TEMPAT LAHIR",
            ])
            if v:
                parts = re.split(r",", v, maxsplit=1)
                data["tempat_lahir"] = parts[0].strip().rstrip(",")
                if len(parts) > 1:
                    iso = _to_iso_date(parts[1])
                    if iso:
                        data["tgl_lahir"] = iso
                if not data["tgl_lahir"]:
                    iso = _to_iso_date(v)
                    if iso:
                        data["tgl_lahir"] = iso

        if "ALAMAT" in up and not capturing_alamat and not data["alamat"]:
            v = _label_value(line, ["ALAMAT"])
            if v:
                alamat_parts.append(v)
            capturing_alamat = True
            continue

        if capturing_alamat:
            if any(stop in up for stop in _ALAMAT_STOP_LABELS):
                capturing_alamat = False
            else:
                alamat_parts.append(line)

    if alamat_parts and not data["alamat"]:
        data["alamat"] = " ".join(p.strip(" :") for p in alamat_parts if p.strip(" :"))

    return data


def _preprocess_for_ocr(content: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(content))
    img = ImageOps.exif_transpose(img).convert("L")
    w, h = img.size
    if w < 1200:
        scale = 1200 / w
        img = img.resize((int(w * scale), int(h * scale)))
    img = img.point(lambda p: 255 if p > 145 else 0)
    return img


def _preprocess_digit_strip(content: bytes) -> Image.Image:
    """Sama seperti _preprocess_for_ocr tapi untuk crop sempit baris NIK
    saja — upscale lebih agresif karena tingginya cuma satu baris teks."""
    img = Image.open(io.BytesIO(content))
    img = ImageOps.exif_transpose(img).convert("L")
    w, h = img.size
    if w < 900:
        scale = 900 / w
        img = img.resize((int(w * scale), int(h * scale)))
    return img.point(lambda p: 255 if p > 140 else 0)


def _ocr_nik_strip(content: bytes) -> Optional[str]:
    """OCR baris NIK dengan whitelist angka saja. Whitelist menghapus
    kesalahan klasik O/0, I/1, S/5 karena huruf sama sekali tidak
    dikenali — tapi tetap bisa salah baca ANTAR angka (mis. 25 terbaca
    51). Karena itu hasilnya HANYA dipakai kalau lolos decode_nik()
    penuh (checksum tanggal/bulan/provinsi), bukan sekadar 16 digit
    dengan prefix provinsi yang kebetulan cocok."""
    img = _preprocess_digit_strip(content)
    txt = pytesseract.image_to_string(
        img, lang="ind",
        config="--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789",
    )
    digits = re.sub(r"\D", "", txt)
    if len(digits) == 16 and decode_nik(digits)["valid"]:
        return digits
    return None


def run_ocr_pipeline(card_bytes: bytes, nik_strip_bytes: Optional[bytes] = None) -> dict:
    """Satu pintu untuk seluruh pipeline: OCR paragraf penuh + (opsional)
    OCR strip NIK terpisah + validasi silang lewat decode_nik(). Dipakai
    identik oleh app.py (endpoint admin) dan telegram_gateway (Mini App)."""
    img = _preprocess_for_ocr(card_bytes)
    raw_text = pytesseract.image_to_string(img, lang="ind+eng")
    parsed = parse_ktp_text(raw_text)
    parsed["raw_text"] = raw_text.strip()

    if nik_strip_bytes:
        strip_nik = _ocr_nik_strip(nik_strip_bytes)
        if strip_nik:
            parsed["nik"] = strip_nik

    nik_info = decode_nik(parsed.get("nik"))
    if nik_info["valid"]:
        parsed["tgl_lahir"] = nik_info["tanggal_lahir"]
    parsed["nik_valid"] = nik_info["valid"]
    parsed["jenis_kelamin"] = nik_info["jenis_kelamin"]
    parsed["usia"] = nik_info["usia"]
    parsed["provinsi_nik"] = nik_info["provinsi"]
    parsed["kabupaten_kota_nik"] = nik_info["kabupaten_kota"]
    parsed["kecamatan_nik"] = nik_info["kecamatan"]
    parsed["catatan_nik"] = nik_info["catatan"]
    return parsed


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
