"""
Writer for the staging.resolved_* handoff tables (D5, D15).

Every vertical's merge stage materializes its output as resolved_account /
resolved_location / resolved_contact rows, and lib/core_apply then does one 3-way
diff into core.*.  This module owns the write half of that contract.

Extracted because healthcare_pipeline.py and deathcare_merge.py each carry a
verbatim copy of these three upserts — twice over, in fact, since both files
define standalone upsert_resolved_* functions AND re-declare the same SQL inside
their own _replace_and_upsert.  Parks would have been the third copy.

Two properties matter and are easy to get wrong:

  Replace-then-upsert in ONE transaction (D15).  A vertical's rows must be deleted
  before the fresh set is written, or records that disappeared upstream would
  linger forever.  Doing the delete in its own transaction means a mid-run failure
  leaves the table empty and the subsequent core diff tombstones the entire
  vertical.  Sharing one engine.begin() makes the whole replace atomic.

  Every DELETE is scoped by `WHERE vertical = :v`.  These tables are shared across
  verticals, so an unscoped delete would wipe a sibling vertical's rows.
"""

from __future__ import annotations

import logging

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# Columns the caller supplies as private side-channel values rather than as real
# table columns.  They are consumed by SQL expressions (geom/boundary construction)
# and must be stripped from any straight column list.
_PRIVATE_LOCATION_COLS = ("_latitude", "_longitude", "_boundary_geojson", "_natural_key")
_PRIVATE_ACCOUNT_COLS = ("_source_id", "_natural_key", "_cluster_id")

_ACCOUNT_SQL = text("""
    INSERT INTO staging.resolved_account (
        account_key, vertical, account_type, legal_name, name_normalized,
        dba_name, parent_account_key, mailing_address, phone, email,
        website, status, external_keys, size_metric, size_metric_unit, confidence
    ) VALUES (
        :account_key, :vertical, :account_type, :legal_name, :name_normalized,
        :dba_name, :parent_account_key,
        CAST(:mailing_address AS jsonb), :phone, :email,
        :website, :status,
        CAST(:external_keys AS jsonb),
        CAST(:size_metric AS numeric), :size_metric_unit,
        CAST(:confidence AS numeric)
    )
    ON CONFLICT (account_key) DO UPDATE SET
        vertical           = EXCLUDED.vertical,
        account_type       = EXCLUDED.account_type,
        legal_name         = EXCLUDED.legal_name,
        name_normalized    = EXCLUDED.name_normalized,
        dba_name           = EXCLUDED.dba_name,
        parent_account_key = EXCLUDED.parent_account_key,
        mailing_address    = EXCLUDED.mailing_address,
        phone              = EXCLUDED.phone,
        email              = EXCLUDED.email,
        website            = EXCLUDED.website,
        status             = EXCLUDED.status,
        external_keys      = EXCLUDED.external_keys,
        size_metric        = EXCLUDED.size_metric,
        size_metric_unit   = EXCLUDED.size_metric_unit,
        confidence         = EXCLUDED.confidence
""")

# geom is built in SQL from :_latitude / :_longitude, and boundary from
# :_boundary_geojson.  Both named parameters are CAST identically at every use:
# Postgres deduces one type per named parameter across the whole statement, and
# reusing :_latitude as both numeric and float8 raises "inconsistent types deduced
# for parameter".  Pin to numeric, then step to double precision only inside
# ST_MakePoint.
_LOCATION_SQL = text("""
    INSERT INTO staging.resolved_location (
        location_key, account_key, vertical, location_name, site_address,
        geom, boundary, geocode_precision, geometry_source,
        maintained_acres, acres_confidence, site_type
    ) VALUES (
        :location_key, :account_key, :vertical, :location_name,
        CAST(:site_address AS jsonb),
        CASE
            WHEN :_latitude IS NOT NULL AND :_longitude IS NOT NULL
            THEN ST_SetSRID(
                ST_MakePoint(
                    CAST(:_longitude AS numeric)::double precision,
                    CAST(:_latitude  AS numeric)::double precision
                ), 4326)
            ELSE NULL
        END,
        CASE
            WHEN :_boundary_geojson IS NULL THEN NULL
            ELSE ST_Multi(ST_CollectionExtract(
                ST_MakeValid(ST_GeomFromGeoJSON(CAST(:_boundary_geojson AS text))), 3))
        END,
        :geocode_precision, :geometry_source,
        CAST(:maintained_acres AS numeric), :acres_confidence, :site_type
    )
    ON CONFLICT (location_key) DO UPDATE SET
        account_key       = EXCLUDED.account_key,
        vertical          = EXCLUDED.vertical,
        location_name     = EXCLUDED.location_name,
        site_address      = EXCLUDED.site_address,
        geom              = EXCLUDED.geom,
        boundary          = EXCLUDED.boundary,
        geocode_precision = EXCLUDED.geocode_precision,
        geometry_source   = EXCLUDED.geometry_source,
        maintained_acres  = EXCLUDED.maintained_acres,
        acres_confidence  = EXCLUDED.acres_confidence,
        site_type         = EXCLUDED.site_type
""")

_CONTACT_SQL = text("""
    INSERT INTO staging.resolved_contact (
        contact_key, account_key, vertical, full_name, role, role_rank,
        phone, email, address, source_id, is_current
    ) VALUES (
        :contact_key, :account_key, :vertical, :full_name, :role, :role_rank,
        :phone, :email, CAST(:address AS jsonb), :source_id, :is_current
    )
    ON CONFLICT (contact_key) DO UPDATE SET
        account_key = EXCLUDED.account_key,
        vertical    = EXCLUDED.vertical,
        full_name   = EXCLUDED.full_name,
        role        = EXCLUDED.role,
        role_rank   = EXCLUDED.role_rank,
        phone       = EXCLUDED.phone,
        email       = EXCLUDED.email,
        address     = EXCLUDED.address,
        source_id   = EXCLUDED.source_id,
        is_current  = EXCLUDED.is_current
""")

# Numeric columns that must be scrubbed of NaN before they reach Postgres.
_NUMERIC_SCRUB = {
    "resolved_account": ("size_metric", "confidence"),
    "resolved_location": ("maintained_acres",),
    "resolved_contact": ("role_rank",),
}


def _to_rows(df: pd.DataFrame, table: str, drop: tuple[str, ...]) -> list[dict]:
    """
    Convert a DataFrame to the executemany payload for *table*.

    Two conversions here are load-bearing:

    NaN -> None on numeric columns.  Postgres `numeric` natively accepts NaN, so a
    pandas float column that upcast a Python None to NaN lands a literal NaN in
    core.* instead of SQL NULL — no error, just a poisoned value that breaks every
    later comparison.  Scrubbing has to happen after to_dict() has moved values out
    of the numpy dtype.

    pd.NA -> None everywhere.  psycopg cannot adapt pandas' NA sentinel and raises
    at execute time.
    """
    if df.empty:
        return []
    rows = df.drop(columns=list(drop), errors="ignore").to_dict(orient="records")
    numeric_cols = _NUMERIC_SCRUB.get(table, ())
    for row in rows:
        for col in numeric_cols:
            if col in row and pd.isna(row[col]):
                row[col] = None
        for key, val in row.items():
            if val is pd.NA:
                row[key] = None
    return rows


def replace_and_upsert(
    engine: Engine,
    account_df: pd.DataFrame,
    location_df: pd.DataFrame,
    contact_df: pd.DataFrame,
    vertical: str,
) -> tuple[int, int, int]:
    """
    Replace one vertical's slice of staging.resolved_* with the supplied rows.

    Deletes the vertical's existing contact, location and account rows and inserts
    the fresh set, all inside a single transaction (D15).  Other verticals' rows
    are never touched.

    location_df may carry the private columns `_latitude`, `_longitude` and
    `_boundary_geojson`; they feed the geom and boundary SQL expressions and are
    not written as columns.  Supply `_boundary_geojson` as a GeoJSON string to
    populate resolved_location.boundary, or omit/None it for a point-only location.

    Returns (n_account, n_location, n_contact) written.
    """
    acct_rows = _to_rows(account_df, "resolved_account", _PRIVATE_ACCOUNT_COLS)
    loc_rows = _to_rows(location_df, "resolved_location", _PRIVATE_LOCATION_COLS)
    con_rows = _to_rows(contact_df, "resolved_contact", ())

    # location_df need not carry boundary at all; default the parameter so the SQL
    # binds cleanly for callers that only have points.
    for row in loc_rows:
        row.setdefault("_boundary_geojson", None)
    if not location_df.empty:
        for row, (_, src) in zip(loc_rows, location_df.iterrows()):
            row["_latitude"] = src.get("_latitude")
            row["_longitude"] = src.get("_longitude")
            geojson = src.get("_boundary_geojson")
            row["_boundary_geojson"] = None if pd.isna(geojson) else geojson

    with engine.begin() as conn:
        # Delete children before parents, and scope every delete to this vertical.
        conn.execute(
            text("DELETE FROM staging.resolved_contact WHERE vertical = :v"), {"v": vertical}
        )
        conn.execute(
            text("DELETE FROM staging.resolved_location WHERE vertical = :v"), {"v": vertical}
        )
        conn.execute(
            text("DELETE FROM staging.resolved_account WHERE vertical = :v"), {"v": vertical}
        )
        logger.info("deleted existing staging.resolved_* rows for vertical=%s", vertical)

        if acct_rows:
            conn.execute(_ACCOUNT_SQL, acct_rows)
        if loc_rows:
            conn.execute(_LOCATION_SQL, loc_rows)
        if con_rows:
            conn.execute(_CONTACT_SQL, con_rows)

    logger.info(
        "upserted vertical=%s: account=%d, location=%d, contact=%d",
        vertical, len(acct_rows), len(loc_rows), len(con_rows),
    )
    return len(acct_rows), len(loc_rows), len(con_rows)
