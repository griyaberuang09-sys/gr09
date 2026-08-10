# Railway: pilih "Dockerfile" sebagai build method (bukan Nixpacks otomatis).
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway menyuntikkan $PORT saat runtime — jangan hardcode 8000.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
