# Railway: pilih "Dockerfile" sebagai build method (bukan Nixpacks otomatis).
FROM python:3.12-slim

# Library sistem yang WAJIB tapi TIDAK disertakan python:3.12-slim:
#   - libgomp1        : runtime OpenMP, dibutuhkan PaddlePaddle
#   - libgl1          : libGL.so.1, dibutuhkan OpenCV (dipakai internal oleh
#                        PaddleOCR/PaddleX) meski jalan headless tanpa GUI
#   - libglib2.0-0     : libgthread-2.0.so.0, biasanya jadi error BERIKUTNYA
#                        setelah libGL diperbaiki -- ditambah sekalian di sini
#                        supaya tidak muncul lagi masalah serupa gelombang kedua
# Tanpa paket-paket ini, "import paddle"/"import cv2" gagal diam-diam saat
# runtime (request pertama kena error), bukan saat build/deploy.
#
# CATATAN kalau baris di bawah GAGAL BUILD tepat di libglib2.0-0: sebagian
# rilis Debian/Ubuntu terbaru mengganti namanya jadi "libglib2.0-0t64"
# (transisi 64-bit time_t). Kalau itu terjadi, ganti baris libglib2.0-0
# di bawah jadi libglib2.0-0t64, lalu redeploy.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Model PaddleOCR (~100MB+) di-download otomatis ke sini saat request
# pertama. TANPA Volume Railway yang di-mount ke path ini, model akan
# terunduh ulang setiap kali container restart (termasuk restart otomatis
# dari katup pengaman kebocoran memori di main.py) -- pasang Volume ke
# /root/.paddlex supaya tidak berulang kali unduh dari server Baidu/HF.
ENV HOME=/root

# Railway menyuntikkan $PORT saat runtime — jangan hardcode 8000.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
