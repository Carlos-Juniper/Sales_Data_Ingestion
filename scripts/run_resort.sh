#!/usr/bin/env bash
# Resort vertical pipeline — D9 sequential execution.
#
# Stage order:
#   1. FL DBPR Lodging  (Florida open data CSV, no key)
#   2. core_apply       -> core.*
#
# Required env vars:
#   DATABASE_URL          — libpq connection string; composed below from DB_PASSWORD_SECRET
#   RESORT_DATA_GLOB      — glob matching hrlodge*.csv district files
#                           (required; files must be pre-staged in the container or volume)
#   GCS_RAW_BUCKET        — GCS bucket for raw landing (default: juniper-ingest-raw)
#   DISABLE_GCS           — set to "1" to skip GCS upload (local/CI use)
#
# Optional env vars:
#   RESORT_MIN_UNITS          — minimum unit threshold (default: connector default)
#   RESORT_INCLUDE_MULTIFAMILY — set to "1" to retain NAPT rows (default: excluded)

set -euo pipefail

# ---------------------------------------------------------------------------
# 0. Resolve DATABASE_URL from DB_PASSWORD_SECRET when not already injected.
# ---------------------------------------------------------------------------
if [[ -z "${DATABASE_URL:-}" ]]; then
    echo "[resort] DATABASE_URL not set — composing from DB_PASSWORD_SECRET" >&2
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

echo "[resort] working directory: ${WORK_DIR}" >&2

# ---------------------------------------------------------------------------
# 1. FL DBPR Lodging
#    fl_dbpr_lodging takes positional file paths (glob-expanded by the
#    connector via glob.glob() on each pattern).
# ---------------------------------------------------------------------------
echo "[resort] stage 1/2 — FL DBPR Lodging" >&2
: "${RESORT_DATA_GLOB:?RESORT_DATA_GLOB must be set to glob matching hrlodge*.csv}"

MIN_UNITS_ARGS=""
if [[ -n "${RESORT_MIN_UNITS:-}" ]]; then
    MIN_UNITS_ARGS="--min-units ${RESORT_MIN_UNITS}"
fi

MULTIFAMILY_ARGS=""
if [[ "${RESORT_INCLUDE_MULTIFAMILY:-0}" == "1" ]]; then
    MULTIFAMILY_ARGS="--include-multifamily"
fi

# shellcheck disable=SC2086
python -m resort.fl_dbpr_lodging \
    "${RESORT_DATA_GLOB}" \
    --out "${WORK_DIR}/fl_dbpr_qualified.csv" \
    --write-db \
    ${MIN_UNITS_ARGS} \
    ${MULTIFAMILY_ARGS}

# ---------------------------------------------------------------------------
# 2. core_apply -> core.*
# ---------------------------------------------------------------------------
echo "[resort] stage 2/2 — core_apply" >&2
python -m lib.core_apply --run-id 0

echo "[resort] pipeline complete" >&2
