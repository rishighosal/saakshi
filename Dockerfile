# Saakshi Impact (the cloud app) for Render, Railway, Fly.io or Hugging Face Spaces.
# Field devices run on laptops/phones, not in this container.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SAAKSHI_MODEL_DIR=/app/models SAAKSHI_DATA_DIR=/app/data

RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY saakshi_core ./saakshi_core
COPY impact ./impact
COPY scripts ./scripts
# Bake the CLIP models into the image so cold starts are fast
RUN python scripts/download_models.py || echo "CLIP download skipped; the app falls back until it can fetch it"

EXPOSE 8000
CMD ["sh", "-c", "python -m impact --host 0.0.0.0 --port ${PORT:-8000}"]
