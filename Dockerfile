# Railway: pilih "Dockerfile" sebagai build method (bukan Nixpacks otomatis),
# supaya tesseract-ocr (paket sistem, bukan paket Python) ikut terpasang.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-ind \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway menyuntikkan $PORT saat runtime — jangan hardcode 8000.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
