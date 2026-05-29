# ── Koyeb-optimized Dockerfile ─────────────────────────────────────────────
# Python 3.11 slim = small image, fast build, ~200MB base
FROM python:3.11-slim

# Prevent Python from writing .pyc files and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Upload folder in /tmp (ephemeral OK — files sent to Telegram immediately)
ENV UPLOAD_FOLDER=/tmp/uploads

# ── System dependencies ──────────────────────────────────────────────────────
# libzbar0      → pyzbar barcode/QR decoding
# tesseract-ocr → OCR fallback
# tesseract-ocr-amh → Amharic language pack for Tesseract
# libgl1 libglib2.0-0 → OpenCV headless requirements
RUN apt-get update && apt-get install -y --no-install-recommends \
        libzbar0 \
        tesseract-ocr \
        tesseract-ocr-amh \
        libgl1 \
        libglib2.0-0 \
        && apt-get clean \
        && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Install Python packages ──────────────────────────────────────────────────
# Copy requirements first so Docker caches this layer (faster rebuilds)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ── Pre-download rembg u2netp model during build ─────────────────────────────
# This bakes the 4.7MB model into the image so it never downloads at runtime.
# Prevents timeout on first user request.
RUN python3 -c "from rembg import new_session; new_session('u2netp'); print('✅ u2netp model cached')"

# ── Copy application files ───────────────────────────────────────────────────
COPY gech_koyeb.py .
COPY id_template.png .
COPY NotoSansEthiopic-Regular.ttf .

# ── Start bot ────────────────────────────────────────────────────────────────
CMD ["python3", "-u", "gech_koyeb.py"]
