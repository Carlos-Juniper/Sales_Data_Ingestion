#!/usr/bin/env bash
# Healthcare vertical pipeline — D9 sequential execution.
#
# Stage order (each must succeed before the next starts):
#   1. CMS General (live API, no key)
#   2. CMS Nursing Home (live API, no key)
#   3. VA Facilities (live API, requires VA_API_KEY)
#   4. NPPES Practice Locations (bulk flat-file, paths supplied via env)
#   5. Geocode enrichment for each source that lacks lat/lon
#   6. Healthcare merge driver  -> staging.resolved_*
#   7. core_apply              -> core.*
#
# Required env vars:
#   DATABASE_URL          — libpq connection string; composed below from DB_PASSWORD_SECRET
#   VA_API_KEY            — VA Facilities API key
#   GCS_RAW_BUCKET        — GCS bucket for raw landing (default: juniper-ingest-raw)
#   DISABLE_GCS           — set to "1" to skip GCS upload (local/CI use)
#
# NPPES flat-file env vars (must be pre-staged on the container or mounted volume):
#   NPPES_MAIN_GLOB       — glob matching npidata_pfile_*.csv (required for NPPES stage)
#   NPPES_PL_GLOB         — glob matching pl_pfile_*.csv      (required for NPPES stage)
#
# If DATABASE_URL is not already set, this script composes it from
# DB_PASSWORD_SECRET (the DB password, already resolved into a plain env var
# by Cloud Run's native Secret Manager env-var integration — see main.tf's
# secret_key_ref — no gcloud CLI call needed inside the container) plus
# DB_HOST, DB_PORT, DB_USER, DB_NAME. Cloud Run's built-in Cloud SQL connector
# exposes the socket at /cloudsql/<INSTANCE>.

set -euo pipefail

# ---------------------------------------------------------------------------
# 0. Resolve DATABASE_URL from DB_PASSWORD_SECRET when not already injected.
# ---------------------------------------------------------------------------
if [[ -z "${DATABASE_URL:-}" ]]; then
    echo "[healthcare] DATABASE_URL not set — composing from DB_PASSWORD_SECRET" >&2
    : "${DB_PASSWORD_SECRET:?DB_PASSWORD_SECRET must be set (Cloud Run secret_key_ref) or DATABASE_URL provided directly}"
    DB_PASS="${DB_PASSWORD_SECRET}"
    DB_HOST="${DB_HOST:-localhost}"
    DB_PORT="${DB_PORT:-5432}"
    DB_USER="${DB_USER:-ingest_app}"
    DB_NAME="${DB_NAME:-ingestion}"
    # Use TCP when explicit DB_HOST is set; otherwise default to localhost (proxy or socket).
    export DATABASE_URL="postgresql://${DB_USER}:${DB_PASS}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
fi

export PYTHONPATH="${PYTHONPATH:-/app/connectors}"
WORK_DIR=$(mktemp -d)

# ---------------------------------------------------------------------------
# Pipeline-level run id (replaces the --run-id 0 sentinel). One
# ingest.source_run row covers this whole script invocation; core_apply
# stamps it onto core.source_record instead of 0. See lib/pipeline_run.py.
# ---------------------------------------------------------------------------
RUN_ID=$(python -m lib.pipeline_run start --source-id healthcare_pipeline)
echo "[healthcare] source_run_id=${RUN_ID}" >&2

cleanup() {
    local exit_code=$?
    local final_status="failed"
    [[ "${exit_code}" -eq 0 ]] && final_status="succeeded"
    python -m lib.pipeline_run finish --run-id "${RUN_ID}" --status "${final_status}" || true
    rm -rf "${WORK_DIR}"
}
trap cleanup EXIT

echo "[healthcare] working directory: ${WORK_DIR}" >&2

# ---------------------------------------------------------------------------
# 1. CMS General
# ---------------------------------------------------------------------------
echo "[healthcare] stage 1/7 — CMS General" >&2
python -m healthcare.cms_provider_data \
    --dataset general \
    --out "${WORK_DIR}/cms_general.csv" \
    --write-db

# ---------------------------------------------------------------------------
# 2. CMS Nursing Home
# ---------------------------------------------------------------------------
echo "[healthcare] stage 2/7 — CMS Nursing Home" >&2
python -m healthcare.cms_provider_data \
    --dataset nursing_home \
    --out "${WORK_DIR}/cms_nursing_home.csv" \
    --write-db

# ---------------------------------------------------------------------------
# 3. VA Facilities
#    Non-fatal: a bad/sandbox VA_API_KEY shouldn't block CMS/NPPES from
#    landing. VA_OK gates whether stage 5 geocodes it and whether it's
#    included at all this run — backfill once a working key is in place.
# ---------------------------------------------------------------------------
echo "[healthcare] stage 3/7 — VA Facilities" >&2
VA_OK=1
python -m healthcare.va_facilities \
    --out "${WORK_DIR}/va_facilities.csv" \
    --write-db || {
        echo "[healthcare] WARNING: VA Facilities stage failed (non-fatal) — skipping this run; backfill once VA_API_KEY is valid" >&2
        VA_OK=0
    }

# ---------------------------------------------------------------------------
# 4. NPPES Practice Locations
#    Requires bulk flat-file. NPPES_MAIN_GLOB and NPPES_PL_GLOB must be set
#    to point at the pre-staged npidata_pfile and pl_pfile CSVs.
# ---------------------------------------------------------------------------
echo "[healthcare] stage 4/7 — NPPES Practice Locations" >&2
: "${NPPES_MAIN_GLOB:?NPPES_MAIN_GLOB must be set to glob matching npidata_pfile_*.csv}"
: "${NPPES_PL_GLOB:?NPPES_PL_GLOB must be set to glob matching pl_pfile_*.csv}"

python -m healthcare.nppes_practice_locations \
    --main "${NPPES_MAIN_GLOB}" \
    --pl   "${NPPES_PL_GLOB}" \
    --out  "${WORK_DIR}/nppes_practice_locations.csv" \
    --write-db

# ---------------------------------------------------------------------------
# 5. Geocode enrichment for each source
#    CMS General has no lat/lon — geocoding is mandatory (§5.1).
#    VA Facilities and NPPES carry coordinates; geocode_enrich caches hits
#    so re-running is cheap.
# ---------------------------------------------------------------------------
echo "[healthcare] stage 5/7 — Geocode enrichment" >&2

GEOCODE_SOURCES=(cms_general cms_nursing_home nppes_practice_locations)
if [[ "${VA_OK}" -eq 1 ]]; then
    GEOCODE_SOURCES+=(va_facilities)
fi

for SRC_ID in "${GEOCODE_SOURCES[@]}"; do
    echo "[healthcare] geocoding source_id=${SRC_ID}" >&2
    python -m healthcare.geocode_enrich \
        --source-id "${SRC_ID}" \
        --input     "${WORK_DIR}/${SRC_ID}.csv" \
        --out       "${WORK_DIR}/${SRC_ID}_geocoded.csv" \
        --write-db
done

# ---------------------------------------------------------------------------
# 6. Healthcare merge driver -> staging.resolved_*
# ---------------------------------------------------------------------------
echo "[healthcare] stage 6/7 — Healthcare merge" >&2
python -m healthcare.healthcare_pipeline

# ---------------------------------------------------------------------------
# 7. core_apply -> core.*
# ---------------------------------------------------------------------------
echo "[healthcare] stage 7/7 — core_apply" >&2
python -m lib.core_apply --run-id "${RUN_ID}"

echo "[healthcare] pipeline complete" >&2
