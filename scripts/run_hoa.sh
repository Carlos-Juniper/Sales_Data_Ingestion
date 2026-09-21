#!/usr/bin/env bash
# HOA vertical pipeline — D9 sequential execution.
#
# Stage order:
#   1. TX TREC HOA  (Texas open data CSV, no key)
#   2. core_apply   -> core.*
#
# Required env vars:
#   DATABASE_URL      — libpq connection string; composed below from DB_PASSWORD_SECRET
#   HOA_DATA_GLOB     — glob matching TREC_HOA_Management_Certificates_*.csv files
#                       (required; files must be pre-staged in the container or volume)
#   GCS_RAW_BUCKET    — GCS bucket for raw landing (default: juniper-ingest-raw)
#   DISABLE_GCS       — set to "1" to skip GCS upload (local/CI use)
#
# Optional env vars:
#   HOA_ZCTA_CROSSWALK — path to the Census ZCTA-to-county relationship file;
#                        used to backfill county from ZIP when available

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
echo "[hoa] stage 1/2 — TX TREC HOA" >&2
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
# 2. core_apply -> core.*
# ---------------------------------------------------------------------------
echo "[hoa] stage 2/2 — core_apply" >&2
python -m lib.core_apply --run-id 0

echo "[hoa] pipeline complete" >&2
