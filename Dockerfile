# Railway: pilih "Dockerfile" sebagai build method (bukan Nixpacks otomatis).
FROM python:3.12-slim

# libgomp1 WAJIB -- PaddlePaddle butuh runtime OpenMP ini, python:3.12-slim
# tidak menyertakannya secara default (beda dari base image yang lebih besar).
# Tanpa ini, "import paddle" gagal diam-diam saat runtime, bukan saat build.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
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
