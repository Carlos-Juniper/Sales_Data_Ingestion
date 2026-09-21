# One image for all verticals. The entrypoint script is selected at
# container startup via the VERTICAL env var or by overriding CMD directly.
#
# Build:
#   docker build -t connector:local .
#
# Run (example):
#   docker run --env-file .env -e VERTICAL=healthcare connector:local

FROM python:3.13-slim

# Keeps Python output unbuffered so Cloud Run Logging captures it in real time.
ENV PYTHONUNBUFFERED=1

# All lib.* imports resolve without a PYTHONPATH flag at the connector level.
ENV PYTHONPATH=/app/connectors

WORKDIR /app

# Install system deps needed by psycopg and by the HOA certificate OCR pass.
# libpq5 is the Postgres client lib pulled by psycopg[binary].
# tesseract-ocr + poppler-utils: tx_trec_pdf_enrich renders scanned
# management-certificate PDFs (pdf2image/pdftoppm) and OCRs them. The
# dominant TREC filing is a CCITT image with no text layer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        tesseract-ocr \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (layer cached unless requirements.txt changes).
COPY connectors/requirements.txt /app/connectors/requirements.txt
RUN pip install --no-cache-dir -r /app/connectors/requirements.txt

# Copy connector source.
COPY connectors/ /app/connectors/

# Copy per-vertical entrypoint scripts.
COPY scripts/ /app/scripts/
RUN chmod +x /app/scripts/*.sh

# Default entrypoint delegates to the per-vertical script selected by VERTICAL.
# Cloud Run overrides CMD per-job; this default is a safe fallback.
CMD ["/bin/bash", "-c", "/app/scripts/run_${VERTICAL:?VERTICAL env var is required}.sh"]
