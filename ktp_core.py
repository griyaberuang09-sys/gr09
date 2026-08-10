"""
ktp_core.py — Logika bersama untuk decode NIK, OCR KTP, dan verifikasi
Telegram WebApp initData. Dipakai oleh DUA proyek terpisah:

  1. app.py (server lokal, di belakang WireGuard) — untuk OCR di menu
     Check-in Penyewa (webcam/unggah admin) dan validasi NIK manual.
     Bisa pakai engine="paddleocr" (opsional, lihat config.json).
  2. telegram_gateway/main.py (di Railway, publik) — untuk OCR & validasi
     saat penghuni submit KTP lewat Mini App Telegram. SELALU pakai
     engine="tesseract" (default) — paddleocr sengaja tidak dipasang di
     sini karena risiko OOM di container beresource terbatas.

PENTING: file ini harus identik di kedua tempat. Kalau salah satu diubah,
salin ulang ke folder satunya — supaya hasil decode NIK & parsing OCR
tidak pernah berbeda antara server lokal dan gateway publik. File ini
AMAN disalin apa adanya ke Railway meski berisi kode PaddleOCR, karena
importnya lazy (di dalam fungsi) — tidak akan pernah dieksekusi selama
Railway tetap memanggil run_ocr_pipeline(..., engine="tesseract").
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

# Tabel desa/kelurahan (83.466 baris), dikelompokkan per kode kecamatan (6
# digit, sama dengan _WILAYAH_KEC di atas). Dipakai untuk MENCOCOKKAN --
# bukan mengganti buta -- hasil OCR kolom Kel/Desa terhadap nama desa
# ASLI yang benar-benar ada di kecamatan itu (yang kecamatannya sendiri
# sudah pasti benar dari NIK). Kandidatnya sengaja dibatasi cuma desa
# dalam SATU kecamatan yang sama (biasanya 5-20 desa) supaya pencocokan
# akurat -- bukan dicocokkan ke 83 ribu desa se-Indonesia yang rawan
# salah pilih desa dengan nama mirip di kecamatan lain.
# Sumber sama seperti wilayah_nik.csv (Kepmendagri, bundel cahyadsn/wilayah).
_WILAYAH_DESA_PER_KEC: dict[str, list] = {}


def _load_wilayah_desa():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wilayah_desa.csv")
    if not os.path.exists(path):
        return
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            _WILAYAH_DESA_PER_KEC.setdefault(row["kode_kec"], []).append(row["nama_desa"])


_load_wilayah_desa()


def _fuzzy_match(ocr_text: Optional[str], candidates: list, min_ratio: float = 0.55) -> dict:
    """Cocokkan teks OCR terhadap daftar kandidat, kembalikan hasil terbaik
    + skor. TIDAK PERNAH mengembalikan 'matched' kalau skornya di bawah
    ambang -- supaya pemanggil tidak menimpa data asli dengan tebakan
    yang tidak yakin."""
    out = {"matched": None, "score": 0.0}
    if not ocr_text or not candidates:
        return out
    clean = re.sub(r"[^A-Z]", "", ocr_text.upper())
    if not clean:
        return out
    import difflib
    best_name, best_score = None, 0.0
    for name in candidates:
        name_clean = re.sub(r"[^A-Z]", "", name.upper())
        score = difflib.SequenceMatcher(None, clean, name_clean).ratio()
        if score > best_score:
            best_name, best_score = name, score
    out["score"] = round(best_score, 3)
    if best_score >= min_ratio:
        out["matched"] = best_name
    return out


# 6 agama resmi diakui negara -- daftar tertutup, tidak berubah, aman
# dipakai koreksi otomatis dengan yakin.
_AGAMA_RESMI = ["ISLAM", "KRISTEN", "KATOLIK", "HINDU", "BUDDHA", "KONGHUCU"]

# 4 kategori status perkawinan standar KTP -- daftar tertutup, aman.
_STATUS_PERKAWINAN_RESMI = ["BELUM KAWIN", "KAWIN", "CERAI HIDUP", "CERAI MATI"]

# CATATAN JUJUR: ini BUKAN daftar 99/106 kategori resmi Permendagri --
# saya tidak menemukan daftar lengkap terverifikasi dari sumber yang bisa
# diakses (situs pemerintah/berita yang punya daftar itu memblokir akses
# otomatis). Ini cuma nilai yang PALING SERING muncul di KTP dalam
# praktiknya, jadi ambang kecocokannya dibuat lebih tinggi (0.62, bukan
# 0.55) supaya lebih hati-hati -- kalau pekerjaan penyewa tidak ada di
# sini, teks OCR asli tetap dipertahankan apa adanya, tidak dipaksa cocok
# ke salah satu daftar ini.
_PEKERJAAN_UMUM = [
    "BELUM/TIDAK BEKERJA", "MENGURUS RUMAH TANGGA", "PELAJAR/MAHASISWA",
    "PEGAWAI NEGERI SIPIL", "TENTARA NASIONAL INDONESIA", "KEPOLISIAN RI",
    "PERDAGANGAN", "PETANI/PEKEBUN", "PETERNAK", "NELAYAN/PERIKANAN",
    "INDUSTRI", "KONSTRUKSI", "TRANSPORTASI", "KARYAWAN SWASTA",
    "KARYAWAN BUMN", "KARYAWAN BUMD", "KARYAWAN HONORER", "BURUH HARIAN LEPAS",
    "BURUH TANI/PERKEBUNAN", "PEMBANTU RUMAH TANGGA", "TUKANG CUKUR",
    "TUKANG LISTRIK", "TUKANG BATU", "TUKANG KAYU", "TUKANG SOL SEPATU",
    "TUKANG LAS/PANDAI BESI", "TUKANG JAHIT", "TUKANG GIGI", "PENATA RIAS",
    "PENATA BUSANA", "PENATA RAMBUT", "MEKANIK", "SENIMAN", "TABIB",
    "PARANORMAL", "PERANCANG BUSANA", "PENTERJEMAH", "IMAM MASJID",
    "PENDETA", "PASTOR", "WARTAWAN", "USTADZ/MUBALIGH", "JURU MASAK",
    "PROMOTOR ACARA", "ANGGOTA DPR-RI", "ANGGOTA DPD", "ANGGOTA BPK",
    "PRESIDEN", "WAKIL PRESIDEN", "ANGGOTA MAHKAMAH KONSTITUSI",
    "ANGGOTA KABINET/KEMENTERIAN", "DUTA BESAR", "GUBERNUR", "WAKIL GUBERNUR",
    "BUPATI", "WAKIL BUPATI", "WALIKOTA", "WAKIL WALIKOTA", "DOSEN", "GURU",
    "PILOT", "PENGACARA", "NOTARIS", "ARSITEK", "AKUNTAN", "KONSULTAN",
    "DOKTER", "BIDAN", "PERAWAT", "APOTEKER", "PSIKIATER/PSIKOLOG",
    "PENYIAR TELEVISI", "PENYIAR RADIO", "PELAUT", "PENELITI", "SOPIR",
    "PIALANG", "PARANJI", "PERANCANG BUSANA", "PENATA RIAS", "PEDAGANG",
    "PERANGKAT DESA", "KEPALA DESA", "BIARAWATI", "WIRASWASTA",
]


def _match_kel_desa(ocr_text: Optional[str], kode_kec: Optional[str], min_ratio: float = 0.55) -> dict:
    """Cocokkan teks OCR kolom Kel/Desa terhadap daftar desa ASLI di
    kecamatan yang sudah pasti benar (dari NIK) -- kandidatnya sengaja
    dibatasi ke SATU kecamatan, jadi lebih akurat dari _fuzzy_match biasa."""
    candidates = _WILAYAH_DESA_PER_KEC.get(kode_kec, []) if kode_kec else []
    result = _fuzzy_match(ocr_text, candidates, min_ratio=min_ratio)
    result["candidates"] = candidates
    return result


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


# ---------- OCR KTP ----------

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


# ---------- OCR: PaddleOCR ----------
# Tesseract DIHAPUS dari file ini (Agustus 2026) -- terbukti gagal total
# (0 karakter terbaca) pada beberapa foto KTP nyata, termasuk yang sudah
# di-crop & diluruskan sempurna, karena pola pengaman hologram di kartu
# KTP terlalu padat untuk pipeline threshold klasik Tesseract. PaddleOCR
# (deep learning) terbukti bisa membaca kartu yang sama dengan akurat.
#
# CATATAN PENTING: file ini SEKARANG BERBEDA dari
# telegram_gateway/ktp_core.py di Railway -- itu TETAP pakai Tesseract
# (lihat catatan risiko OOM PaddleOCR di CPU beresource terbatas).
# JANGAN salin file ini ke folder telegram_gateway/.

_paddle_engine = None  # singleton, di-load sekali saat request OCR pertama


def _get_paddle_engine():
    global _paddle_engine
    if _paddle_engine is None:
        from paddleocr import PaddleOCR  # import lazy, lihat catatan di atas
        _paddle_engine = PaddleOCR(
            lang="id",
            use_doc_orientation_classify=True,  # foto KTP dari HP sering rotasi 90/180/270 -- ini otomatis meluruskan (download 1 model kecil tambahan saat pertama jalan)
            use_doc_unwarping=False,
            use_textline_orientation=True,
            # oneDNN/MKL-DNN aktif secara default di versi ini (DEFAULT_ENABLE_MKLDNN=True
            # di paddleocr/_constants.py, sudah dicek langsung ke source code), TAPI
            # konverter atribut PIR-nya belum mendukung sebagian tipe atribut operator
            # yang dipakai model PP-OCRv5 ini -- selalu gagal dengan error
            # "ConvertPirAttribute2RuntimeAttribute not support [...]" di setiap
            # panggilan OCR. Matikan supaya jatuh ke jalur eksekusi standar (sedikit
            # lebih lambat, tapi jauh lebih kompatibel & TERBUKTI tidak crash).
            enable_mkldnn=False,
        )
    return _paddle_engine


def _prepare_for_paddle(content: bytes, min_width: int = 1600) -> "Image.Image":
    """Beda dari _preprocess_for_ocr: TIDAK di-threshold hitam-putih.
    Model deep learning PaddleOCR dilatih di gambar natural (RGB/grayscale
    halus) — thresholding keras justru menghilangkan informasi yang
    modelnya butuhkan, beda dengan Tesseract yang klasik/berbasis piksel.

    min_width dinaikkan dari 1000 ke 1600 (Agustus 2026) -- PaddleOCR
    tidak punya parameter resmi utk "paksa deteksi spasi" (sudah dicek
    source code-nya, return_word_box TIDAK membantu kasus ini karena cuma
    mengelompokkan teks yang SUDAH dipisah spasi oleh model, bukan
    mendeteksi celah piksel independen). Resolusi lebih tinggi = celah
    antar kata jadi lebih banyak piksel = model lebih mungkin berhasil
    memprediksi token spasi -- ini pengaruh terbesar yang bisa dikontrol
    dari luar model tanpa retraining."""
    img = Image.open(io.BytesIO(content))
    img = ImageOps.exif_transpose(img).convert("RGB")
    w, h = img.size
    if w < min_width:
        scale = min_width / w
        img = img.resize((int(w * scale), int(h * scale)))
    return img


def _ocr_paddle_extract_text(img: "Image.Image") -> str:
    import numpy as np
    engine = _get_paddle_engine()
    arr = np.array(img)
    results = engine.predict(arr)
    lines: list[str] = []
    for res in results:
        # OCRResult (paddlex) mewarisi dict langsung -- res["rec_texts"] resmi,
        # sudah dicek ke source code paddlex, bukan tebakan dari dokumentasi.
        lines.extend(res.get("rec_texts", []))
    return "\n".join(lines)


def _ocr_nik_strip_paddle(content: bytes) -> Optional[str]:
    """Versi PaddleOCR dari _ocr_nik_strip. Tidak ada whitelist digit
    resmi di API PaddleOCR yang simpel, jadi validasi sepenuhnya
    mengandalkan decode_nik() -- sama seperti fallback di jalur Tesseract."""
    img = _prepare_for_paddle(content, min_width=700)
    text = _ocr_paddle_extract_text(img)
    for candidate in re.findall(r"\d[\d\s]{14,20}\d", text):
        digits = re.sub(r"\D", "", candidate)
        if len(digits) == 16 and decode_nik(digits)["valid"]:
            return digits
    return None


def _parse_ktp_lines_paddle(lines: list) -> dict:
    """Parser KHUSUS format keluaran PaddleOCR: daftar potongan teks
    BERURUTAN top-to-bottom (label di satu elemen, isinya di elemen
    BERIKUTNYA) -- BEDA TOTAL dari format Tesseract ('Label: Isi' dalam
    satu baris) yang dibaca parse_ktp_text(). Kalau dipakaikan
    parse_ktp_text() ke sini, NIK/Nama TIDAK AKAN kebaca sama sekali
    walau OCR mentahnya sudah bagus, karena pola yang dicari beda.

    Toleran terhadap label yang sedikit salah baca (mis. 'Kecamaran' utk
    'Kecamatan', 'TgiLai' utk 'Tgl Lahir') lewat pencocokan prefix
    pendek, bukan exact match -- diuji langsung terhadap sampel nyata."""
    data = {
        "nik": None, "nama": None, "tempat_lahir": None, "tgl_lahir": None, "alamat": None,
        "gol_darah": None, "rt": None, "rw": None, "kel_desa": None,
        "agama": None, "status_perkawinan": None, "pekerjaan": None,
    }
    n = len(lines)

    def next_nonempty(i):
        j = i + 1
        while j < n and not lines[j].strip():
            j += 1
        return lines[j].strip() if j < n else None

    for i, raw in enumerate(lines):
        line = (raw or "").strip()
        if not line:
            continue
        up_nospace = re.sub(r"[^A-Z]", "", line.upper())

        if not data["nik"]:
            digits_here = re.sub(r"\D", "", line)
            if len(digits_here) == 16:
                data["nik"] = digits_here
            elif up_nospace == "NIK":
                nxt = next_nonempty(i)
                if nxt:
                    digits = re.sub(r"\D", "", nxt)
                    if len(digits) >= 15:
                        data["nik"] = digits[:16]

        if not data["nama"] and up_nospace.startswith("NAMA"):
            nxt = next_nonempty(i)
            if nxt and len(nxt) > 1:
                data["nama"] = nxt

        if not data["tempat_lahir"] and up_nospace.startswith("TEMPAT"):
            nxt = next_nonempty(i)
            if nxt:
                parts = re.split(r",", nxt, maxsplit=1)
                data["tempat_lahir"] = parts[0].strip().rstrip(",")
                if len(parts) > 1:
                    iso = _to_iso_date(parts[1])
                    if iso:
                        data["tgl_lahir"] = iso
                if not data["tgl_lahir"]:
                    iso = _to_iso_date(nxt)
                    if iso:
                        data["tgl_lahir"] = iso

        if not data["alamat"] and up_nospace.startswith("ALAMAT"):
            nxt = next_nonempty(i)
            if nxt:
                data["alamat"] = nxt

        # Beberapa field muncul BERGABUNG dengan label sebelumnya di baris yang
        # sama tanpa spasi (mis. "Gol DarahA" dari sampel nyata) -- cek dua
        # kemungkinan: label persis (nilai di baris berikutnya) ATAU label+nilai
        # menyatu di baris yang sama (ambil sisa karakter setelah label).
        if not data["gol_darah"] and up_nospace.startswith("GOLDARAH"):
            sisa = re.sub(r"(?i)^.*GOL\.?\s*DARAH", "", line).strip()
            m = re.search(r"\b(AB|A|B|O)\b", sisa.upper()) or re.match(r"^(AB|A|B|O)$", sisa.upper())
            if m:
                data["gol_darah"] = m.group(1)
            else:
                nxt = next_nonempty(i)
                if nxt:
                    m2 = re.match(r"^(AB|A|B|O)$", nxt.strip().upper())
                    if m2:
                        data["gol_darah"] = m2.group(1)

        if not data["rt"] and up_nospace.startswith("RTRW"):
            nxt = next_nonempty(i)
            if nxt:
                m = re.match(r"\s*(\d{1,3})\s*/\s*(\d{1,3})", nxt)
                if m:
                    data["rt"], data["rw"] = m.group(1).zfill(3), m.group(2).zfill(3)

        if not data["kel_desa"] and (up_nospace.startswith("KELDESA") or up_nospace.startswith("KELURAHAN") or up_nospace.startswith("DESA")):
            nxt = next_nonempty(i)
            if nxt:
                data["kel_desa"] = nxt

        if not data["agama"] and up_nospace.startswith("AGAMA"):
            nxt = next_nonempty(i)
            if nxt:
                data["agama"] = nxt

        # Toleran typo umum PaddleOCR: "Status Parkawioan" (harusnya "Perkawinan")
        # -- cocokkan prefix pendek "STATUSP" yang bertahan di kedua kasus.
        if not data["status_perkawinan"] and up_nospace.startswith("STATUSP"):
            nxt = next_nonempty(i)
            if nxt:
                data["status_perkawinan"] = nxt

        if not data["pekerjaan"] and up_nospace.startswith("PEKERJAAN"):
            nxt = next_nonempty(i)
            if nxt:
                data["pekerjaan"] = nxt

    return data


def run_ocr_pipeline(card_bytes: bytes, nik_strip_bytes: Optional[bytes] = None) -> dict:
    """Satu pintu untuk seluruh pipeline: OCR paragraf penuh + (opsional)
    OCR strip NIK terpisah + validasi silang lewat decode_nik().
    Selalu pakai PaddleOCR di file ini (Tesseract dihapus, lihat catatan
    di atas _get_paddle_engine)."""
    img = _prepare_for_paddle(card_bytes)
    raw_text = _ocr_paddle_extract_text(img)
    parsed = _parse_ktp_lines_paddle(raw_text.split("\n"))
    parsed["raw_text"] = raw_text.strip()

    if nik_strip_bytes:
        strip_nik = _ocr_nik_strip_paddle(nik_strip_bytes)
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

    # Cocokkan Kel/Desa hasil OCR terhadap daftar desa ASLI di kecamatan
    # yang sudah pasti benar dari NIK -- kandidatnya dibatasi cuma desa
    # dalam kecamatan itu (bukan 83 ribu desa se-Indonesia), jadi cukup
    # andal dipakai koreksi otomatis. Skor rendah -> TIDAK ditimpa, teks
    # OCR asli tetap dipertahankan supaya tidak salah cocok tanpa disadari.
    parsed["kel_desa_ocr"] = parsed.get("kel_desa")
    parsed["kel_desa_match_score"] = None
    if nik_info.get("kode_kecamatan"):
        match = _match_kel_desa(parsed.get("kel_desa"), nik_info["kode_kecamatan"])
        parsed["kel_desa_match_score"] = match["score"]
        if match["matched"]:
            parsed["kel_desa"] = match["matched"]

    # Agama & Status Perkawinan: daftar tertutup resmi (6 agama, 4 status)
    # -- aman dikoreksi dengan yakin, ambang standar (0.55).
    parsed["agama_ocr"] = parsed.get("agama")
    m_agama = _fuzzy_match(parsed.get("agama"), _AGAMA_RESMI)
    if m_agama["matched"]:
        parsed["agama"] = m_agama["matched"]

    parsed["status_perkawinan_ocr"] = parsed.get("status_perkawinan")
    m_status = _fuzzy_match(parsed.get("status_perkawinan"), _STATUS_PERKAWINAN_RESMI)
    if m_status["matched"]:
        parsed["status_perkawinan"] = m_status["matched"]

    # Pekerjaan: BUKAN daftar resmi lengkap (lihat catatan di _PEKERJAAN_UMUM),
    # jadi ambang dinaikkan (0.62) supaya lebih hati-hati -- kalau pekerjaan
    # penyewa tidak ada di daftar umum ini, teks OCR asli dipertahankan.
    parsed["pekerjaan_ocr"] = parsed.get("pekerjaan")
    m_kerja = _fuzzy_match(parsed.get("pekerjaan"), _PEKERJAAN_UMUM, min_ratio=0.62)
    if m_kerja["matched"]:
        parsed["pekerjaan"] = m_kerja["matched"]

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
