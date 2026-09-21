#!/usr/bin/env bash
# Deathcare vertical pipeline — D9 sequential execution.
#
# Stage order (each must succeed before the next starts):
#   1. FGDL Cemeteries      (ESRI/ArcGIS REST, no key)
#   2. TxDOT Cemeteries     (Texas open data, no key)
#   3. USGS NSD             (USGS GeoNames, no key)
#   4. VA Cemeteries        (VA open CSV, no key)
#   5. IRS BMF Deathcare    (IRS bulk CSV, no key)
#   6. IRS 990 Enrich       (ProPublica 990 API, no key — rate-limited)
#   7. Deathcare merge driver -> staging.resolved_*
#   8. core_apply           -> core.*
#
# Required env vars:
#   DATABASE_URL    — libpq connection string; composed below from DB_PASSWORD_SECRET
#   GCS_RAW_BUCKET  — GCS bucket for raw landing (default: juniper-ingest-raw)
#   DISABLE_GCS     — set to "1" to skip GCS upload (local/CI use)

set -euo pipefail

# ---------------------------------------------------------------------------
# 0. Resolve DATABASE_URL from DB_PASSWORD_SECRET when not already injected.
# ---------------------------------------------------------------------------
if [[ -z "${DATABASE_URL:-}" ]]; then
    echo "[deathcare] DATABASE_URL not set — composing from DB_PASSWORD_SECRET" >&2
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

# ---------------------------------------------------------------------------
# Pipeline-level run id (replaces the --run-id 0 sentinel). One
# ingest.source_run row covers this whole script invocation; core_apply
# stamps it onto core.source_record instead of 0. See lib/pipeline_run.py.
# ---------------------------------------------------------------------------
RUN_ID=$(python -m lib.pipeline_run start --source-id deathcare_pipeline)
echo "[deathcare] source_run_id=${RUN_ID}" >&2

cleanup() {
    local exit_code=$?
    local final_status="failed"
    [[ "${exit_code}" -eq 0 ]] && final_status="succeeded"
    python -m lib.pipeline_run finish --run-id "${RUN_ID}" --status "${final_status}" || true
    rm -rf "${WORK_DIR}"
}
trap cleanup EXIT

echo "[deathcare] working directory: ${WORK_DIR}" >&2

# ---------------------------------------------------------------------------
# 1. FGDL Cemeteries
# ---------------------------------------------------------------------------
echo "[deathcare] stage 1/8 — FGDL Cemeteries" >&2
python -m deathcare.fgdl_cemeteries \
    --out "${WORK_DIR}/fgdl_cemeteries.csv" \
    --write-db

# ---------------------------------------------------------------------------
# 2. TxDOT Cemeteries
# ---------------------------------------------------------------------------
echo "[deathcare] stage 2/8 — TxDOT Cemeteries" >&2
python -m deathcare.txdot_cemeteries \
    --out "${WORK_DIR}/txdot_cemeteries.csv" \
    --write-db

# ---------------------------------------------------------------------------
# 3. USGS NSD
#    Default states: FL TX NC SC PA (matches _DEFAULT_STATES in the connector).
#    Override with USGS_STATES env var if needed: "FL TX NC SC PA VA".
# ---------------------------------------------------------------------------
echo "[deathcare] stage 3/8 — USGS NSD" >&2
USGS_STATES_ARGS=""
if [[ -n "${USGS_STATES:-}" ]]; then
    # shellcheck disable=SC2086
    USGS_STATES_ARGS="--state ${USGS_STATES}"
fi
# shellcheck disable=SC2086
python -m deathcare.usgs_nsd \
    --out "${WORK_DIR}/usgs_nsd.csv" \
    --write-db \
    ${USGS_STATES_ARGS}

# ---------------------------------------------------------------------------
# 4. VA Cemeteries
# ---------------------------------------------------------------------------
echo "[deathcare] stage 4/8 — VA Cemeteries" >&2
python -m deathcare.va_cemeteries \
    --out "${WORK_DIR}/va_cemeteries.csv" \
    --write-db

# ---------------------------------------------------------------------------
# 5. IRS BMF Deathcare
#    Default sources: IRS live eo2.csv and eo3.csv URLs.
#    Override with IRS_BMF_INPUTS env var for local/offline use.
# ---------------------------------------------------------------------------
echo "[deathcare] stage 5/8 — IRS BMF Deathcare" >&2
IRS_BMF_ARGS=""
if [[ -n "${IRS_BMF_INPUTS:-}" ]]; then
    # shellcheck disable=SC2086
    IRS_BMF_ARGS="--input ${IRS_BMF_INPUTS}"
fi
# shellcheck disable=SC2086
python -m deathcare.irs_bmf_deathcare \
    --out "${WORK_DIR}/irs_bmf_deathcare.csv" \
    --write-db \
    ${IRS_BMF_ARGS}

# ---------------------------------------------------------------------------
# 6. IRS 990 Enrich
#    Reads canonical BMF CSV produced in stage 5.
#    --workers and --sleep can be tuned via env vars to respect rate limits.
# ---------------------------------------------------------------------------
echo "[deathcare] stage 6/8 — IRS 990 Enrich" >&2
python -m deathcare.irs_990_enrich \
    --input   "${WORK_DIR}/irs_bmf_deathcare.csv" \
    --out     "${WORK_DIR}/irs_990_enriched.csv" \
    --workers "${IRS_990_WORKERS:-4}" \
    --sleep   "${IRS_990_SLEEP:-0.5}" \
    --write-db

# ---------------------------------------------------------------------------
# 7. Deathcare merge driver -> staging.resolved_*
# ---------------------------------------------------------------------------
echo "[deathcare] stage 7/8 — Deathcare merge" >&2
python -m deathcare.deathcare_merge

# ---------------------------------------------------------------------------
# 8. core_apply -> core.*
# ---------------------------------------------------------------------------
echo "[deathcare] stage 8/8 — core_apply" >&2
python -m lib.core_apply --run-id "${RUN_ID}"

echo "[deathcare] pipeline complete" >&2
