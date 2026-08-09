"""
telegram_gateway/main.py — Deploy folder ini (utuh) ke Railway.

Fungsi: kotak surat SEMENTARA yang publik. Penghuni submit KTP lewat Mini
App di sini (OCR juga jalan di sini, supaya penghuni langsung lihat
hasilnya). Server LOKAL (di belakang WireGuard) yang aktif menjemput data
ini secara berkala lewat GET /api/gateway/pending — server lokal TIDAK
PERNAH menerima koneksi masuk untuk fitur ini.

Data di sini TIDAK PERMANEN: begitu server lokal berhasil menjemput &
mengonfirmasi (POST /api/gateway/ack), baris terkait dihapus. Kalau
container Railway restart sebelum sempat dijemput, submission yang lagi
"mengambang" di jendela pendek itu bisa hilang — penghuni tinggal submit
ulang. Untuk keandalan lebih, pasang Railway Volume yang di-mount ke
DATA_DIR (lihat README.md).

Environment variables yang WAJIB diisi di Railway (tab Variables):
    TELEGRAM_BOT_TOKEN   — sama persis dengan punya bot di server lokal
    GATEWAY_SECRET       — string acak buatan sendiri (mis. openssl rand -hex 32),
                            HARUS sama persis dengan telegram_gateway_secret
                            di config.json server lokal
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from ktp_core import run_ocr_pipeline, verify_telegram_init_data, decode_nik

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("GATEWAY_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "gateway.db"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GATEWAY_SECRET = os.environ.get("GATEWAY_SECRET", "")

app = FastAPI(title="Griya Beruang — Telegram Mini App Gateway")


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_submissions (
                id TEXT PRIMARY KEY,
                telegram_user_id TEXT NOT NULL,
                telegram_username TEXT,
                telegram_first_name TEXT,
                phone TEXT NOT NULL,
                ktp_nik TEXT, ktp_nama TEXT, ktp_tempat_lahir TEXT, ktp_tgl_lahir TEXT,
                ktp_jenis_kelamin TEXT, ktp_alamat TEXT, ktp_kecamatan TEXT,
                ktp_kabupaten_kota TEXT, ktp_provinsi TEXT,
                photo_base64 TEXT, catatan_nik TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)


init_db()


def _check_secret(token: str):
    if not GATEWAY_SECRET or not hmac.compare_digest(token or "", GATEWAY_SECRET):
        raise HTTPException(401, "Token gateway salah atau belum diset.")


# ---------- Halaman Mini App ----------

@app.get("/telegram-checkin", response_class=HTMLResponse, include_in_schema=False)
def telegram_checkin_page():
    path = BASE_DIR / "telegram_checkin.html"
    # WebView Telegram (terutama di iOS) cenderung cache halaman ini dengan
    # agresif -- tanpa header ini, update HTML tidak akan kelihatan sampai
    # entah kapan meski server sudah punya versi baru.
    return HTMLResponse(
        path.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/", include_in_schema=False)
def root():
    return {"service": "griya-beruang-telegram-gateway", "status": "ok"}


# ---------- Dipanggil oleh Mini App (penghuni, publik) ----------

@app.post("/api/telegram-app/ocr")
async def telegram_app_ocr(
    init_data: str = Form(...),
    file: UploadFile = File(...),
    nik_strip: Optional[UploadFile] = File(None),
):
    user = verify_telegram_init_data(init_data, BOT_TOKEN)
    if not user:
        raise HTTPException(401, "Verifikasi Telegram gagal — buka ulang lewat tombol menu bot.")

    content = await file.read()
    strip_content = await nik_strip.read() if nik_strip is not None else None
    try:
        return run_ocr_pipeline(content, strip_content)
    except Exception as e:
        raise HTTPException(500, f"OCR gagal: {e}")


class NikDecodeBody(BaseModel):
    init_data: str
    nik: str


@app.post("/api/telegram-app/nik-decode")
def telegram_app_nik_decode(body: NikDecodeBody):
    """Dipanggil begitu penyewa selesai mengetik/mengoreksi NIK secara
    manual di Step 3 -- mengisi otomatis tanggal lahir, jenis kelamin,
    kecamatan, kabupaten/kota, provinsi. Alamat TIDAK diisi dari sini --
    NIK tidak menyimpan alamat jalan sama sekali, cuma kode wilayah
    sampai level kecamatan."""
    user = verify_telegram_init_data(body.init_data, BOT_TOKEN)
    if not user:
        raise HTTPException(401, "Verifikasi Telegram gagal — buka ulang lewat tombol menu bot.")
    return decode_nik(body.nik)


class SubmitBody(BaseModel):
    init_data: str
    phone: str
    ktp_nik: Optional[str] = None
    ktp_nama: Optional[str] = None
    ktp_tempat_lahir: Optional[str] = None
    ktp_tgl_lahir: Optional[str] = None
    ktp_jenis_kelamin: Optional[str] = None
    ktp_alamat: Optional[str] = None
    ktp_kecamatan: Optional[str] = None
    ktp_kabupaten_kota: Optional[str] = None
    ktp_provinsi: Optional[str] = None
    ktp_photo_base64: Optional[str] = None
    catatan_nik: Optional[str] = None


@app.post("/api/telegram-app/submit")
def telegram_app_submit(body: SubmitBody):
    user = verify_telegram_init_data(body.init_data, BOT_TOKEN)
    if not user:
        raise HTTPException(401, "Verifikasi Telegram gagal — buka ulang lewat tombol menu bot.")
    if not body.phone or not body.phone.strip():
        raise HTTPException(400, "Nomor HP wajib diisi.")

    sub_id = uuid.uuid4().hex
    with db() as conn:
        conn.execute(
            """
            INSERT INTO pending_submissions (
                id, telegram_user_id, telegram_username, telegram_first_name, phone,
                ktp_nik, ktp_nama, ktp_tempat_lahir, ktp_tgl_lahir, ktp_jenis_kelamin,
                ktp_alamat, ktp_kecamatan, ktp_kabupaten_kota, ktp_provinsi,
                photo_base64, catatan_nik
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                sub_id, str(user["id"]), user.get("username"), user.get("first_name"), body.phone.strip(),
                body.ktp_nik, body.ktp_nama, body.ktp_tempat_lahir, body.ktp_tgl_lahir, body.ktp_jenis_kelamin,
                body.ktp_alamat, body.ktp_kecamatan, body.ktp_kabupaten_kota, body.ktp_provinsi,
                body.ktp_photo_base64, body.catatan_nik,
            ),
        )
    return {"ok": True}


# ---------- Dipanggil oleh server lokal (outbound, terjadwal) ----------

@app.get("/api/gateway/pending")
def gateway_pending(token: str = ""):
    """Server lokal memanggil ini tiap ~60 detik. Dilindungi GATEWAY_SECRET,
    BUKAN oleh Telegram initData (server lokal bukan pengguna Telegram)."""
    _check_secret(token)
    with db() as conn:
        rows = conn.execute("SELECT * FROM pending_submissions ORDER BY created_at ASC").fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["gateway_id"] = d.pop("id")
        items.append(d)
    return {"items": items}


class AckBody(BaseModel):
    ids: list[str]


@app.post("/api/gateway/ack")
def gateway_ack(body: AckBody, token: str = ""):
    """Server lokal memanggil ini setelah berhasil menyimpan draft ke DB-nya
    sendiri — baris terkait dihapus di sini supaya tidak diambil dobel."""
    _check_secret(token)
    if not body.ids:
        return {"ok": True, "deleted": 0}
    with db() as conn:
        placeholders = ",".join("?" * len(body.ids))
        conn.execute(f"DELETE FROM pending_submissions WHERE id IN ({placeholders})", body.ids)
    return {"ok": True, "deleted": len(body.ids)}


@app.get("/api/gateway/health")
def health():
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM pending_submissions").fetchone()["c"]
    return {"ok": True, "bot_token_set": bool(BOT_TOKEN), "secret_set": bool(GATEWAY_SECRET), "pending_count": n}
