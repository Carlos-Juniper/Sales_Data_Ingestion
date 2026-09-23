#!/usr/bin/env bash
# HOA vertical pipeline — D9 sequential execution.
#
# Stage order:
#   1. TX TREC HOA          (Texas open data CSV, no key)
#   2. TX TREC PDF OCR      (certificate queue from stage 1 → enrich_hoa_pdf_contact)
#   3. hoa_gmaps_enrich     (Google Places supplemental phone/website/email lookup —
#                            skips associations stage 2 already found a contact for)
#   4. core_apply           -> core.*
#
# Required env vars:
#   DATABASE_URL      — libpq connection string; composed below from DB_PASSWORD_SECRET
#   HOA_DATA_GLOB     — glob matching TREC_HOA_Management_Certificates_*.csv files
#                       (required; files must be pre-staged in the container or volume)
#   GOOGLE_MAPS_API_KEY — Places API key for stage 3 (see .env.example)
#   GCS_RAW_BUCKET    — GCS bucket for raw landing (default: juniper-ingest-raw)
#   DISABLE_GCS       — set to "1" to skip GCS upload (local/CI use)
#
# Optional env vars:
#   HOA_ZCTA_CROSSWALK — path to the Census ZCTA-to-county relationship file;
#                        used to backfill county from ZIP when available
#   HOA_PDF_WORKERS    — OCR thread count (default 1). Downloads stay at
#                        HOA_PDF_SLEEP seconds apart regardless of this value.
#   HOA_PDF_SLEEP      — seconds between certificate downloads (default 1.0)
#   HOA_PDF_LIMIT      — process only the first N queue rows (metro slice / smoke)
#   HOA_PDF_COUNTY     — process only this county_primary (e.g. HARRIS)
#   HOA_GMAPS_WORKERS   — thread pool size for stage 3 (default: 4)
#   HOA_GMAPS_RATE_PAUSE — min seconds between Places API request starts (default: 0.2)

set -euo pipefail

# ---------------------------------------------------------------------------
# 0. Resolve DATABASE_URL from DB_PASSWORD_SECRET when not already injected.
# ---------------------------------------------------------------------------
if [[ -z "${DATABASE_URL:-}" ]]; then
    echo "[hoa] DATABASE_URL not set — composing from DB_PASSWORD_SECRET" >&2
    : "${DB_PASSWORD_SECRET:?DB_PASSWORD_SECRET must be set (Cloud Run secret_key_ref) or DATABASE_URL provided directly}"
    DB_PASS="${DB_PASSWORD_SECRET}"
    DB_HOST="${DB_HOST:-localhost}"
    DB_PORT="${DB_PORT:-5432}"
    DB_USER="${DB_USER:-ingest_app}"
    DB_NAME="${DB_NAME:-ingestion}"
    export DATABASE_URL="postgresql://${DB_USER}:${DB_PASS}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
fi

export PYTHONPATH="${PYTHONPATH:-/app/connectors}"
WORK_DIR=$(mktemp -d)
trap 'rm -rf "${WORK_DIR}"' EXIT

echo "[hoa] working directory: ${WORK_DIR}" >&2

# ---------------------------------------------------------------------------
# 1. TX TREC HOA
#    tx_trec_hoa takes positional file paths (glob-expanded by the script, not
#    the shell, because the connector calls glob.glob() on each pattern).
# ---------------------------------------------------------------------------
echo "[hoa] stage 1/4 — TX TREC HOA" >&2
: "${HOA_DATA_GLOB:?HOA_DATA_GLOB must be set to glob matching TREC_HOA_Management_Certificates_*.csv}"

ZCTA_ARGS=""
if [[ -n "${HOA_ZCTA_CROSSWALK:-}" ]]; then
    ZCTA_ARGS="--zcta-crosswalk ${HOA_ZCTA_CROSSWALK}"
fi

# shellcheck disable=SC2086
python -m hoa.tx_trec_hoa \
    "${HOA_DATA_GLOB}" \
    --out "${WORK_DIR}/tx_trec_hoa.csv" \
    --queue "${WORK_DIR}/tx_trec_pdf_queue.csv" \
    --write-db \
    ${ZCTA_ARGS}

# ---------------------------------------------------------------------------
# 2. TX TREC certificate PDF OCR
#    Queue CSV from stage 1. Scanned certificates — OCR is the primary path.
#    Cached ok rows (same certificate_id) are skipped inside the connector.
# ---------------------------------------------------------------------------
echo "[hoa] stage 2/4 — TX TREC PDF OCR enrich" >&2

PDF_ARGS=(
    --queue "${WORK_DIR}/tx_trec_pdf_queue.csv"
    --out "${WORK_DIR}/tx_trec_pdf_enriched.csv"
    --workers "${HOA_PDF_WORKERS:-1}"
    --sleep "${HOA_PDF_SLEEP:-1.0}"
    --write-db
)
if [[ -n "${HOA_PDF_LIMIT:-}" ]]; then
    PDF_ARGS+=(--limit "${HOA_PDF_LIMIT}")
fi
if [[ -n "${HOA_PDF_COUNTY:-}" ]]; then
    PDF_ARGS+=(--county "${HOA_PDF_COUNTY}")
fi

python -m hoa.tx_trec_pdf_enrich "${PDF_ARGS[@]}"

# ---------------------------------------------------------------------------
# 3. hoa_gmaps_enrich — Google Places supplemental phone/website/email lookup
#    Fed stage 2's output so it can skip associations the certificate OCR
#    already produced a usable mgmt_phone/mgmt_email for (per
#    Ingestion-Plan-of-Action.md §5.3: a regulator filing outranks a Places
#    guess). Places is a fallback for associations OCR came back empty on,
#    plus a source of `website`, which the certificate often lacks.
# ---------------------------------------------------------------------------
echo "[hoa] stage 3/4 — Google Places enrichment" >&2
: "${GOOGLE_MAPS_API_KEY:?GOOGLE_MAPS_API_KEY must be set (see .env.example)}"

python -m hoa.hoa_gmaps_enrich \
    --input "${WORK_DIR}/tx_trec_hoa.csv" \
    --pdf-contact-csv "${WORK_DIR}/tx_trec_pdf_enriched.csv" \
    --out "${WORK_DIR}/hoa_gmaps_enriched.csv" \
    --write-db \
    --workers "${HOA_GMAPS_WORKERS:-4}" \
    --rate-pause "${HOA_GMAPS_RATE_PAUSE:-0.2}"

# ---------------------------------------------------------------------------
# 4. core_apply -> core.*
# ---------------------------------------------------------------------------
echo "[hoa] stage 4/4 — core_apply" >&2
python -m lib.core_apply --run-id 0

echo "[hoa] pipeline complete" >&2
