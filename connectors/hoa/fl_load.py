"""
Shared --write-db path for the Florida HOA connectors (migrations 022-025).

Every FL connector writes the same way as connectors/resort/fl_dbpr_lodging.py:
land the raw bytes, open an ingest.source_run, upsert the canonical columns,
then fill the attrs jsonb the canonical shape has no column for.  This module
holds that sequence once so the four connectors differ only in parsing.
"""

from __future__ import annotations

import datetime
import sys

import pandas as pd

from lib.db import (
    finish_source_run,
    get_engine,
    update_staging_attrs,
    upsert_staging,
    write_source_run,
)
from lib.gcs import raw_sha256, upload_raw
from lib.http import get_secret

FL_PUBLIC_RECORDS = "Florida public records, Ch. 119 F.S. — free to store and use"


def write_staging(
    source_id: str,
    canonical: pd.DataFrame,
    attrs: dict[str, dict],
    raw_bytes: bytes,
    *,
    connector_version: str = "1.0",
    license_string: str = FL_PUBLIC_RECORDS,
) -> int:
    """Write one run to staging.<source_id>; returns the source_run_id."""
    if not get_secret("DATABASE_URL"):
        sys.exit(
            "ERROR: --write-db was given but DATABASE_URL is not set. "
            "Copy .env.example -> .env and fill it in."
        )
    run_date = datetime.date.today().isoformat()
    raw_uri = upload_raw(source_id, run_date, raw_bytes)

    engine = get_engine()
    source_run_id: int | None = None
    try:
        source_run_id = write_source_run(
            engine,
            source_id=source_id,
            byte_count=len(raw_bytes),
            sha256=raw_sha256(raw_bytes),
            connector_version=connector_version,
            license_string=license_string,
            raw_uri=raw_uri,
        )
        upsert_staging(engine, source_id, canonical)
        n_attrs = update_staging_attrs(engine, source_id, attrs)
        finish_source_run(engine, source_run_id, status="succeeded", row_count=len(canonical))
        print(
            f"  {source_id}: wrote {len(canonical):,} rows ({n_attrs:,} attrs) "
            f"to staging.{source_id} (source_run_id={source_run_id})",
            file=sys.stderr,
        )
        return source_run_id
    except Exception as exc:
        if source_run_id is not None:
            finish_source_run(engine, source_run_id, status="failed")
        print(f"  {source_id}: DB write failed — {exc}", file=sys.stderr)
        raise


def clean(value) -> str | None:
    """Strip a source string; blank becomes None so jsonb stays compact."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def drop_empty(d: dict) -> dict:
    """Remove None / empty values from an attrs dict (one level)."""
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}
