"""
Shared database helpers — engine creation, ingest provenance, staging upserts.

Reads DATABASE_URL from env (or .env file via python-dotenv).
In local dev, point at the docker-compose Postgres via the Auth Proxy.
In Cloud Run, inject DATABASE_URL from Secret Manager as an env var.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Optional

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from lib.http import get_secret
from lib.schema import CANONICAL_COLUMNS

logger = logging.getLogger(__name__)

# C3: Use the canonical list from lib.schema instead of a hand-copied duplicate.
# _STAGING_COLS is kept as an alias so any internal reference is obvious.
_STAGING_COLS = CANONICAL_COLUMNS

_NUMERIC_COLS = {"latitude", "longitude", "size_value"}

# C4: Safe SQL identifier pattern — lowercase letters/digits/underscore only,
# must start with a letter or underscore. Prevents SQL injection via source_id.
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def _assert_safe_identifier(name: str) -> None:
    """Raise ValueError if *name* is not a safe SQL identifier.

    Checked before any f-string interpolation into SQL.  Rejects colons,
    hyphens (after the dash→underscore replacement), spaces, and anything
    else that could escape the identifier context.
    """
    if not _SAFE_IDENTIFIER_RE.match(name):
        raise ValueError(
            f"Unsafe SQL identifier {name!r}: must match ^[a-z_][a-z0-9_]*$"
        )


def get_engine() -> Engine:
    """Return a SQLAlchemy engine built from DATABASE_URL.

    DATABASE_URL is stored in the bare ``postgresql://`` form on purpose:
    db/run_migrations.py hands it straight to libpq (``psycopg.connect``),
    which rejects a ``+psycopg`` dialect suffix. SQLAlchemy, on the other
    hand, resolves a bare ``postgresql://`` URL to the psycopg2 dialect —
    and we ship psycopg 3, not psycopg2, so that import fails. Normalise the
    scheme here so the single env var serves both callers.
    """
    url = _normalize_sqlalchemy_url(get_secret("DATABASE_URL", required=True))
    return create_engine(url, pool_pre_ping=True)


def _normalize_sqlalchemy_url(url: str) -> str:
    """Force the psycopg (v3) driver for a bare libpq-style Postgres URL.

    Leaves the migration runner's bare DATABASE_URL untouched — it only ever
    sees this transformed value inside SQLAlchemy. If the caller already
    picked a driver (``+psycopg`` / ``+psycopg2``), respect their choice.
    """
    if url.startswith(("postgresql+", "postgres+")):
        return url  # explicit driver already selected
    for scheme in ("postgresql://", "postgres://"):
        if url.startswith(scheme):
            return "postgresql+psycopg://" + url[len(scheme):]
    return url


def write_source_run(
    engine: Engine,
    *,
    source_id: str,
    byte_count: int,
    sha256: str,
    connector_version: str = "",
    license_string: str = "",
    raw_uri: Optional[str] = None,
) -> int:
    """
    Insert a new ingest.source_run row with status='running'.

    Args:
        engine: SQLAlchemy engine.
        source_id: Connector identifier (e.g. ``"cms_general"``).
        byte_count: Size of the raw payload in bytes.
        sha256: Hex SHA-256 digest of the raw payload.
        connector_version: Optional semver string (default ``""``).
        license_string: Optional data-license description (default ``""``).
        raw_uri: Optional ``gs://`` URI where the raw payload was landed
                 (D7 raw landing).  ``None`` when GCS is unavailable/disabled.

    Returns:
        The generated source_run_id (bigserial PK).
    """
    sql = text("""
        INSERT INTO ingest.source_run
            (source_id, byte_count, sha256, connector_version, license_string,
             raw_uri, status)
        VALUES
            (:source_id, :byte_count, :sha256, :connector_version, :license_string,
             :raw_uri, 'running')
        RETURNING source_run_id
    """)
    with engine.begin() as conn:
        row = conn.execute(sql, {
            "source_id": source_id,
            "byte_count": byte_count,
            "sha256": sha256,
            "connector_version": connector_version,
            "license_string": license_string,
            "raw_uri": raw_uri,
        }).fetchone()
    source_run_id = row[0]
    logger.info(
        "write_source_run: source_id=%r → source_run_id=%d raw_uri=%r",
        source_id, source_run_id, raw_uri,
    )
    return source_run_id


def finish_source_run(
    engine: Engine,
    source_run_id: int,
    *,
    status: str,
    row_count: Optional[int] = None,
) -> None:
    """
    Update status and row_count on a source_run row.

    status should be 'succeeded' or 'failed'.
    """
    sql = text("""
        UPDATE ingest.source_run
        SET status = :status, row_count = :row_count
        WHERE source_run_id = :id
    """)
    with engine.begin() as conn:
        conn.execute(sql, {"status": status, "row_count": row_count, "id": source_run_id})
    logger.info("finish_source_run: source_run_id=%d status=%r", source_run_id, status)


def upsert_staging(
    engine: Engine,
    source_id: str,
    df: pd.DataFrame,
    *,
    max_empty_natural_key_fraction: float = 0.01,
) -> None:
    """
    Write a canonical DataFrame into staging.<source_id>.

    - Creates the table if it doesn't exist (same schema as migration 006).
    - Upserts on (source_id, natural_key) — updates all other columns on conflict.
    - Computes geom from latitude/longitude where both are non-null.
    - Expects df to contain the columns listed in CANONICAL_COLUMNS (lib/schema.py).
      Missing columns are filled with None; extra columns are ignored.
    - Aborts (raises ValueError) when the fraction of empty/blank natural_keys
      exceeds *max_empty_natural_key_fraction* (default 1 %).  An all-empty
      regression would collapse the entire dataset to one row silently (B5).
    """
    # C4: Normalise hyphens then assert the result is a safe SQL identifier
    # before f-string interpolation. Colons and other hazardous characters
    # are caught here even after D3 removes colon-sourced source_ids.
    table = source_id.replace("-", "_")
    _assert_safe_identifier(table)
    _ensure_staging_table(engine, table)

    if df.empty:
        logger.warning("upsert_staging: empty DataFrame for source_id=%r — nothing written", source_id)
        return

    # B5: Guard against a regression where natural_key is accidentally empty
    # for the whole (or nearly the whole) dataset.  Because the PK is
    # (source_id, natural_key), all-empty keys collapse to a single row
    # without raising any DB error — the bug is silent and catastrophic.
    if "natural_key" in df.columns:
        empty_mask = df["natural_key"].isna() | (df["natural_key"].astype(str).str.strip() == "")
        empty_fraction = empty_mask.sum() / max(len(df), 1)
        if empty_fraction > max_empty_natural_key_fraction:
            raise ValueError(
                f"upsert_staging: {empty_fraction:.1%} of natural_keys are empty "
                f"for source_id={source_id!r} — exceeds threshold "
                f"{max_empty_natural_key_fraction:.1%}. "
                f"Check the connector's key extraction logic before writing to DB."
            )

    rows = _prepare_rows(df, source_id)
    if not rows:
        return

    upsert_sql = text(f"""
        INSERT INTO staging.{table} (
            source_id, natural_key, vertical, account_type,
            name_raw, name_normalized, address_line_1, city, state, zip5,
            phone_raw, phone_normalized, latitude, longitude, geom,
            segment, ein, county_fips, size_metric, size_value, size_unit, source_file
        ) VALUES (
            :source_id, :natural_key, :vertical, :account_type,
            :name_raw, :name_normalized, :address_line_1, :city, :state, :zip5,
            :phone_raw, :phone_normalized,
            -- Every typed use of :latitude/:longitude must deduce the SAME
            -- type, or a NULL (untyped) value trips "inconsistent types
            -- deduced for parameter": the lat/long columns want numeric while
            -- ST_MakePoint wants double precision. Pin both params to numeric
            -- everywhere, then step numeric -> float8 only for the geom.
            CAST(:latitude AS numeric), CAST(:longitude AS numeric),
            CASE WHEN :latitude IS NOT NULL AND :longitude IS NOT NULL
                 THEN ST_SetSRID(
                     ST_MakePoint(
                         CAST(:longitude AS numeric)::double precision,
                         CAST(:latitude AS numeric)::double precision
                     ), 4326)
                 ELSE NULL END,
            :segment, :ein, :county_fips, :size_metric, :size_value, :size_unit, :source_file
        )
        ON CONFLICT (source_id, natural_key) DO UPDATE SET
            vertical         = EXCLUDED.vertical,
            account_type     = EXCLUDED.account_type,
            name_raw         = EXCLUDED.name_raw,
            name_normalized  = EXCLUDED.name_normalized,
            address_line_1   = EXCLUDED.address_line_1,
            city             = EXCLUDED.city,
            state            = EXCLUDED.state,
            zip5             = EXCLUDED.zip5,
            phone_raw        = EXCLUDED.phone_raw,
            phone_normalized = EXCLUDED.phone_normalized,
            latitude         = EXCLUDED.latitude,
            longitude        = EXCLUDED.longitude,
            geom             = EXCLUDED.geom,
            segment          = EXCLUDED.segment,
            ein              = EXCLUDED.ein,
            county_fips      = EXCLUDED.county_fips,
            size_metric      = EXCLUDED.size_metric,
            size_value       = EXCLUDED.size_value,
            size_unit        = EXCLUDED.size_unit,
            source_file      = EXCLUDED.source_file,
            loaded_at        = now()
    """)

    with engine.begin() as conn:
        conn.execute(upsert_sql, rows)

    logger.info("upsert_staging: wrote %d rows to staging.%s", len(rows), table)


# ---------------------------------------------------------------- internals

def _ensure_staging_table(engine: Engine, table: str) -> None:
    """CREATE TABLE IF NOT EXISTS staging.<table> with the canonical schema."""
    # C4: guard here too — _ensure_staging_table is also called directly in tests.
    _assert_safe_identifier(table)
    sql = text(f"""
        CREATE TABLE IF NOT EXISTS staging.{table} (
            source_id        text NOT NULL,
            natural_key      text NOT NULL,
            vertical         text,
            account_type     text,
            name_raw         text,
            name_normalized  text,
            address_line_1   text,
            city             text,
            state            text,
            zip5             text,
            phone_raw        text,
            phone_normalized text,
            latitude         numeric,
            longitude        numeric,
            geom             geometry(Point, 4326),
            segment          text,
            ein              text,
            county_fips      text,
            size_metric      text,
            size_value       numeric,
            size_unit        text,
            source_file      text,
            loaded_at        timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (source_id, natural_key)
        )
    """)
    with engine.begin() as conn:
        conn.execute(sql)


def _prepare_rows(df: pd.DataFrame, source_id: str) -> list[dict]:
    """
    Normalise a canonical DataFrame into a list of dicts ready for executemany.

    - Fills missing CANONICAL_COLUMNS with None.
    - Overrides source_id with the caller-supplied value.
    - Coerces NaN/None across ALL columns (not just numeric) so psycopg never
      receives a Python float NaN in a text column (C2).
    - Deduplicates on (source_id, natural_key) keeping the LAST occurrence so
      a batch with repeated keys doesn't trigger "cannot affect row a second
      time" from Postgres's ON CONFLICT DO UPDATE (C1).
    """
    out = pd.DataFrame(index=df.index)
    for col in _STAGING_COLS:
        out[col] = df[col] if col in df.columns else None

    # Caller-supplied source_id is authoritative.
    out["source_id"] = source_id

    # Coerce numeric columns: strings → float (NaN where invalid).
    for col in _NUMERIC_COLS:
        out[col] = pd.to_numeric(out[col], errors="coerce")

    # C1: Deduplicate on the upsert conflict key (source_id, natural_key),
    # keeping the last occurrence.  ON CONFLICT DO UPDATE in executemany raises
    # "cannot affect row a second time" when both sides of the conflict live in
    # the same batch — deduping here prevents that entirely.
    out = out.drop_duplicates(subset=["source_id", "natural_key"], keep="last")

    rows = out.to_dict(orient="records")

    # C2: Replace any float NaN with None across every column.  The original
    # code only cleaned _NUMERIC_COLS; a NaN in a text column (e.g. a pandas
    # float NaN from a missing join) reaches psycopg as-is and can cause
    # unexpected DB behaviour or silent string "nan" values.
    for row in rows:
        for key, val in row.items():
            try:
                if val is not None and isinstance(val, float) and math.isnan(val):
                    row[key] = None
            except (TypeError, ValueError):
                pass  # non-scalar types (lists, dicts) are not NaN

    return rows


# ---------------------------------------------------------------- boundaries

# Exact square metres per acre (international acre).  Defined here so every
# caller converts identically instead of each inlining its own rounded literal.
SQM_PER_ACRE: float = 4046.8564224

# The robust GeoJSON -> MultiPolygon chain, used for both the stored geometry and
# the area measurement so the two can never describe different shapes.
#
# Why each step is here:
#   ST_GeomFromGeoJSON   — parse.  Errors on malformed input rather than silently
#                          producing an empty geometry.
#   ST_MakeValid         — real-world government and park polygons routinely have
#                          self-intersections and duplicate vertices.  Without this
#                          ST_Intersects/ST_Intersection raise GEOS TopologyException
#                          mid-run, which would abort the whole rollup.
#   ST_CollectionExtract(...,3) — ST_MakeValid may return a GeometryCollection when
#                          it has to split a bowtie; type 3 pulls out just the
#                          polygonal parts and discards degenerate slivers/lines.
#   ST_Multi             — the target column is geometry(MultiPolygon,4326) and most
#                          sources hand back a plain Polygon, which would be
#                          rejected by the type constraint.
_GEOJSON_TO_MULTIPOLYGON = (
    "ST_CollectionExtract("
    "ST_MakeValid(ST_GeomFromGeoJSON(CAST(:geojson AS text)))"
    ", 3)"
)


def upsert_boundaries(
    engine: Engine,
    table: str,
    rows: list[dict],
    *,
    boundary_col: str = "boundary",
    area_col: Optional[str] = None,
    extra_cols: tuple[str, ...] = (),
    simplify_tolerance: Optional[float] = 1e-5,
) -> int:
    """
    Upsert polygon boundaries into a staging side table, keyed (source_id, natural_key).

    Each row dict must carry ``source_id``, ``natural_key``, ``geojson`` (a GeoJSON
    geometry string, or None for a row with no geometry) plus one entry per name in
    *extra_cols*.  A None ``geojson`` writes NULL geometry and NULL area rather
    than failing — a source with partial geometry coverage is normal.

    *area_col*, when given, is populated with acreage measured from the
    **unsimplified** geometry.  This ordering is the whole point: simplification is
    applied only to what gets stored, so the acreage figure is never degraded by it
    and remains an honest independent check against a source's own published acres.

    *simplify_tolerance* is in degrees; the 1e-5 default is ~1.1 m at the latitudes
    of the five target states.  Pass None to store geometry unsimplified.

    Returns the number of rows written.
    """
    _assert_safe_identifier(table)
    for col in (boundary_col, *( (area_col,) if area_col else () ), *extra_cols):
        _assert_safe_identifier(col)

    if not rows:
        logger.warning("upsert_boundaries: no rows for %s — nothing written", table)
        return 0

    if simplify_tolerance is None:
        boundary_expr = f"ST_Multi({_GEOJSON_TO_MULTIPOLYGON})"
    else:
        boundary_expr = (
            f"ST_Multi(ST_SimplifyPreserveTopology("
            f"{_GEOJSON_TO_MULTIPOLYGON}, {float(simplify_tolerance)!r}))"
        )

    insert_cols = ["source_id", "natural_key", *extra_cols, boundary_col]
    value_exprs = [
        ":source_id",
        ":natural_key",
        *[f":{c}" for c in extra_cols],
        f"CASE WHEN :geojson IS NULL THEN NULL ELSE {boundary_expr} END",
    ]
    if area_col:
        insert_cols.append(area_col)
        value_exprs.append(
            f"CASE WHEN :geojson IS NULL THEN NULL ELSE "
            f"ST_Area({_GEOJSON_TO_MULTIPOLYGON}::geography) / {SQM_PER_ACRE!r} END"
        )

    # Every non-key column is refreshed on conflict so a re-run is idempotent.
    update_cols = [c for c in insert_cols if c not in ("source_id", "natural_key")]
    update_clause = ",\n            ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    sql = text(f"""
        INSERT INTO staging.{table} ({", ".join(insert_cols)})
        VALUES ({", ".join(value_exprs)})
        ON CONFLICT (source_id, natural_key) DO UPDATE SET
            {update_clause},
            loaded_at = now()
    """)  # nosec: identifiers validated above

    # ON CONFLICT DO UPDATE raises "cannot affect row a second time" if one batch
    # carries two rows with the same conflict key, so collapse duplicates first.
    deduped: dict[tuple, dict] = {}
    for row in rows:
        deduped[(row.get("source_id"), row.get("natural_key"))] = row
    payload = list(deduped.values())
    dropped = len(rows) - len(payload)
    if dropped:
        logger.warning(
            "upsert_boundaries: dropped %d duplicate (source_id, natural_key) row(s) for %s",
            dropped, table,
        )

    # NaN never reaches Postgres: a numeric column accepts literal NaN, so a
    # pandas-derived None that got upcast to NaN would land as NaN, not NULL.
    scrubbed = []
    for row in payload:
        clean = {}
        for key, val in row.items():
            if isinstance(val, float) and math.isnan(val):
                clean[key] = None
            elif val is pd.NA:
                clean[key] = None
            else:
                clean[key] = val
        scrubbed.append(clean)

    with engine.begin() as conn:
        conn.execute(sql, scrubbed)

    logger.info("upserted %d boundary rows to staging.%s", len(scrubbed), table)
    return len(scrubbed)
