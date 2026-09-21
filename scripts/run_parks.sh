#!/usr/bin/env bash
# Parks / Municipal vertical pipeline — D9 sequential execution.
#
# Stage order (each must succeed before the next starts):
#   1. Government spine  (TIGERweb: places, PA townships, counties) -> staging + boundaries
#   2. Park layers       (6 ArcGIS sources)                         -> staging + park_attrs
#   3. Manager resolve   (park -> government, §5.4)                 -> staging.park_rollup
#   4. Parks merge       (dedup, acreage, rollup)                   -> staging.resolved_*
#   5. core_apply                                                   -> core.*
#
# Stage 1 must run before stage 3: the spatial rollup joins against
# staging.gov_unit_boundary, and an empty spine would send every park to the
# county fallback (or to nothing at all).
#
# Required env vars:
#   DATABASE_URL          — libpq connection string; composed below from DB_PASSWORD_SECRET
#   GCS_RAW_BUCKET        — GCS bucket for raw landing (default: juniper-ingest-raw)
#   DISABLE_GCS           — set to "1" to skip GCS upload (local/CI use)
#
# No API keys are needed anywhere in this vertical: every source is a public
# ArcGIS endpoint (Census TIGERweb, USGS PAD-US, PASDA, TPWD, FDEP, NCDPR, SCPRT).
#
# Optional:
#   PARKS_OUT_DIR         — keep the intermediate CSVs (default: a temp dir, deleted)
#
# If DATABASE_URL is not already set, this script composes it from
# DB_PASSWORD_SECRET (resolved into a plain env var by Cloud Run's native Secret
# Manager integration — see main.tf's secret_key_ref) plus DB_HOST, DB_PORT,
# DB_USER, DB_NAME.

set -euo pipefail

# ---------------------------------------------------------------------------
# 0. Resolve DATABASE_URL from DB_PASSWORD_SECRET when not already injected.
# ---------------------------------------------------------------------------
if [[ -z "${DATABASE_URL:-}" ]]; then
    echo "[parks] DATABASE_URL not set — composing from DB_PASSWORD_SECRET" >&2
    : "${DB_PASSWORD_SECRET:?DB_PASSWORD_SECRET must be set (Cloud Run secret_key_ref) or DATABASE_URL provided directly}"
    DB_PASS="${DB_PASSWORD_SECRET}"
    DB_HOST="${DB_HOST:-localhost}"
    DB_PORT="${DB_PORT:-5432}"
    DB_USER="${DB_USER:-ingest_app}"
    DB_NAME="${DB_NAME:-ingestion}"
    export DATABASE_URL="postgresql://${DB_USER}:${DB_PASS}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
fi

export PYTHONPATH="${PYTHONPATH:-/app/connectors}"

if [[ -n "${PARKS_OUT_DIR:-}" ]]; then
    WORK_DIR="${PARKS_OUT_DIR}"
    mkdir -p "${WORK_DIR}"
    KEEP_WORK_DIR=1
else
    WORK_DIR=$(mktemp -d)
    KEEP_WORK_DIR=0
fi

# ---------------------------------------------------------------------------
# Pipeline-level run id. One ingest.source_run row covers this whole script
# invocation and core_apply stamps it onto core.source_record — NOT the
# --run-id 0 sentinel that run_hoa.sh / run_resort.sh still pass.
# See lib/pipeline_run.py.
# ---------------------------------------------------------------------------
RUN_ID=$(python -m lib.pipeline_run start --source-id parks_pipeline)
echo "[parks] source_run_id=${RUN_ID}" >&2

cleanup() {
    local exit_code=$?
    local final_status="failed"
    [[ "${exit_code}" -eq 0 ]] && final_status="succeeded"
    python -m lib.pipeline_run finish --run-id "${RUN_ID}" --status "${final_status}" || true
    if [[ "${KEEP_WORK_DIR}" -eq 0 ]]; then
        rm -rf "${WORK_DIR}"
    fi
}
trap cleanup EXIT

echo "[parks] working directory: ${WORK_DIR}" >&2

# ---------------------------------------------------------------------------
# 1. Government spine — must precede the rollup in stage 3.
# ---------------------------------------------------------------------------
echo "[parks] stage 1/5 — government spine (TIGERweb)" >&2
python -m parks.gov_units \
    --out-dir "${WORK_DIR}" \
    --write-db

# ---------------------------------------------------------------------------
# 2. Park layers — all six sources from park_layers.yaml.
# ---------------------------------------------------------------------------
echo "[parks] stage 2/5 — park layers" >&2
python -m parks.park_layers \
    --out-dir "${WORK_DIR}" \
    --write-db

# ---------------------------------------------------------------------------
# 3. Park -> government resolution (§5.4).
# ---------------------------------------------------------------------------
echo "[parks] stage 3/5 — manager resolve" >&2
python -m parks.manager_resolve --write-db

# ---------------------------------------------------------------------------
# 4. Merge, acreage reconciliation, rollup -> staging.resolved_*
# ---------------------------------------------------------------------------
echo "[parks] stage 4/5 — parks merge" >&2
python -m parks.parks_merge

# ---------------------------------------------------------------------------
# 5. core_apply -> core.*
# ---------------------------------------------------------------------------
echo "[parks] stage 5/5 — core_apply" >&2
python -m lib.core_apply --run-id "${RUN_ID}"

echo "[parks] pipeline complete" >&2
