"""
Geocoding Enrichment — append lat/lon to any healthcare source's records.

Source  : US Census Bureau Geocoder batch endpoint (public domain, no key)
Fallback: Nominatim public API (rate-limited per OSM usage policy)

The Census batch endpoint is the primary path because it handles 2 000 rows
per call and imposes no storage restrictions.  Nominatim is a one-at-a-time
fallback only for Census misses; it is rate-limited to ≤ 1 req/sec.

Results are written to staging.enrich_geocode (PK: source_id, natural_key),
which doubles as a cache per D6 — re-runs skip rows that already have a result.

Usage:
    python geocode_enrich.py \\
        --source-id  nppes_practice_locations \\
        --input      nppes_practice_locations.csv \\
        --out        geocode_enriched.csv

    python geocode_enrich.py \\
        --source-id  cms_general \\
        --input      cms_general.csv \\
        --write-db
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import math
import sys
import time
from pathlib import Path

import pandas as pd
import requests
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import get_engine
from lib.http import get_secret, make_session

logger = logging.getLogger(__name__)

CENSUS_BATCH_SIZE = 2000

_CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_NOMINATIM_USER_AGENT = (
    "juniper-healthcare-pipeline/1.0 carlos.hernandez@juniperlandscaping.com"
)
_NOMINATIM_RATE_LIMIT_SEC = 1.1

_REQUIRED_COLUMNS = {"natural_key", "address_line_1", "city", "site_state", "zip5"}

_ENRICH_COLUMNS = [
    "latitude",
    "longitude",
    "geocode_precision",
    "geocode_source",
    "geocode_match_type",
    "geocode_address_returned",
]


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def assert_input_shape(df: pd.DataFrame) -> None:
    """Raise ValueError if any required column is absent."""
    missing = _REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(
            f"Input DataFrame is missing required column(s): {missing}. "
            f"Columns present: {list(df.columns)}"
        )


# ---------------------------------------------------------------------------
# Census batch helpers
# ---------------------------------------------------------------------------


def prepare_census_batch(records: list[dict]) -> str:
    """Convert records to a Census-format CSV string (no header row)."""
    if not records:
        return ""

    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    for rec in records:
        writer.writerow([
            rec["id"],
            rec["address_line_1"],
            rec["city"],
            rec["site_state"],
            rec["zip5"],
        ])
    return buf.getvalue()


_CENSUS_COL_ID     = 0
_CENSUS_COL_MATCH  = 2
_CENSUS_COL_MTYPE  = 3
_CENSUS_COL_ADDR   = 4
_CENSUS_COL_COORDS = 5


def parse_census_response(csv_text: str) -> dict[str, dict]:
    """
    Parse Census geocoder response CSV.

    Returns a dict mapping natural_key to geocode fields.  Only rows where
    Match == 'Match' are included; 'No_Match' and 'Tie' rows are silently
    dropped.  Returns empty dict on empty or malformed input rather than
    raising — callers handle misses by falling back to Nominatim.
    """
    if not csv_text or not csv_text.strip():
        return {}

    results: dict[str, dict] = {}
    try:
        reader = csv.reader(io.StringIO(csv_text))
        for row in reader:
            if len(row) < 6:
                continue

            input_id = row[_CENSUS_COL_ID].strip()
            match_status = row[_CENSUS_COL_MATCH].strip()
            match_type = row[_CENSUS_COL_MTYPE].strip()
            matched_address = row[_CENSUS_COL_ADDR].strip()
            coordinates = row[_CENSUS_COL_COORDS].strip()

            if match_status != "Match":
                continue

            # Coordinates are "lon,lat" — note reversed order relative to convention.
            try:
                lon_str, lat_str = coordinates.split(",", 1)
                latitude = float(lat_str.strip())
                longitude = float(lon_str.strip())
            except (ValueError, AttributeError):
                # Malformed coordinate field for an otherwise-matched row —
                # treat as a miss so Nominatim gets a chance at it.
                continue

            precision = "rooftop" if match_type == "Exact" else "street"

            results[input_id] = {
                "latitude": latitude,
                "longitude": longitude,
                "geocode_precision": precision,
                "geocode_match_type": match_type,
                "geocode_address_returned": matched_address,
            }
    except (csv.Error, ValueError, IndexError):
        return {}

    return results


def submit_census_batch(
    records: list[dict],
    session: requests.Session,
) -> dict[str, dict]:
    """
    POST a batch of records to the Census geocoder.

    Returns parsed results dict.  Logs to stderr and returns empty dict on
    any HTTP error or timeout so callers can fall back to Nominatim.
    """
    csv_text = prepare_census_batch(records)
    if not csv_text:
        return {}

    try:
        response = session.post(
            _CENSUS_URL,
            data={"benchmark": "Public_AR_Current"},
            files={"addressFile": ("batch.csv", csv_text.encode("utf-8"), "text/plain")},
            timeout=120,
        )
        response.raise_for_status()
    except requests.exceptions.Timeout:
        print(
            f"census geocoder timeout for batch of {len(records)} records",
            file=sys.stderr,
        )
        return {}
    except requests.exceptions.RequestException as exc:
        print(f"census geocoder HTTP error: {exc}", file=sys.stderr)
        return {}

    return parse_census_response(response.text)


# ---------------------------------------------------------------------------
# Nominatim fallback
# ---------------------------------------------------------------------------


def geocode_single_nominatim(
    record: dict,
    session: requests.Session,
) -> dict | None:
    """
    Geocode a single record via Nominatim.

    Rate-limited to ≤ 1 req/sec as required by OSM usage policy.  Returns
    None on empty results or any HTTP/parse error.
    """
    query = (
        f"{record['address_line_1']}, {record['city']}, "
        f"{record['site_state']} {record['zip5']}"
    )
    try:
        response = session.get(
            _NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "us"},
            headers={"User-Agent": _NOMINATIM_USER_AGENT},
            timeout=30,
        )
        response.raise_for_status()
        results = response.json()
    except Exception as exc:
        print(f"nominatim error for {record.get('id', '?')}: {exc}", file=sys.stderr)
        return None
    finally:
        # Sleep after every call — including failed ones — so a retry burst
        # does not violate the 1 req/sec OSM rate limit.
        time.sleep(_NOMINATIM_RATE_LIMIT_SEC)

    if not results:
        return None

    first = results[0]
    try:
        return {
            "latitude": float(first["lat"]),
            "longitude": float(first["lon"]),
            "geocode_precision": "street",
            "geocode_source": "nominatim",
        }
    except (KeyError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Enrichment helpers
# ---------------------------------------------------------------------------


def _to_geocode_record(row: pd.Series) -> dict:
    """Extract the fields needed for a geocode request from a DataFrame row."""
    return {
        "id": row["natural_key"],
        "address_line_1": row.get("address_line_1", ""),
        "city": row.get("city", ""),
        "site_state": row.get("site_state", ""),
        "zip5": row.get("zip5", ""),
    }


def _write_geo(result: pd.DataFrame, i: int, geo: dict) -> None:
    """Write geocode result fields onto a result DataFrame row in-place."""
    result.at[i, "latitude"]                 = geo.get("latitude")
    result.at[i, "longitude"]                = geo.get("longitude")
    result.at[i, "geocode_precision"]        = geo.get("geocode_precision", "no_match")
    result.at[i, "geocode_source"]           = geo.get("geocode_source", "none")
    result.at[i, "geocode_match_type"]       = geo.get("geocode_match_type", "")
    result.at[i, "geocode_address_returned"] = geo.get("geocode_address_returned", "")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def print_summary(df: pd.DataFrame) -> None:
    """Print geocode coverage statistics to stderr."""
    total = len(df)
    census_count = (df.get("geocode_source") == "census").sum()
    nominatim_count = (df.get("geocode_source") == "nominatim").sum()
    unmatched_count = (df.get("geocode_source") == "none").sum()
    match_rate = (census_count + nominatim_count) / total * 100 if total > 0 else 0.0

    print(f"\ngeocode summary:", file=sys.stderr)
    print(f"  total rows      : {total:,}", file=sys.stderr)
    print(f"  census matches  : {census_count:,}", file=sys.stderr)
    print(f"  nominatim hits  : {nominatim_count:,}", file=sys.stderr)
    print(f"  unmatched       : {unmatched_count:,}", file=sys.stderr)
    print(f"  match rate      : {match_rate:.1f}%", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main enrichment function
# ---------------------------------------------------------------------------


def enrich(
    df: pd.DataFrame,
    session: requests.Session | None = None,
    fallback_nominatim: bool = True,
) -> pd.DataFrame:
    """
    Geocode a DataFrame of practice locations and return it with lat/lon columns.

    Rows that already have a non-null latitude are skipped (idempotent).
    Census is tried first in batches; Nominatim fills remaining misses when
    fallback_nominatim=True.

    Parameters
    ----------
    df:
        DataFrame with columns: natural_key, address_line_1, city, site_state, zip5.
    session:
        Shared requests.Session.  Created internally if not provided.
    fallback_nominatim:
        When False, Census misses are left as no_match without calling Nominatim.

    Returns
    -------
    Input DataFrame with columns appended: latitude, longitude, geocode_precision,
    geocode_source, geocode_match_type, geocode_address_returned.
    """
    assert_input_shape(df)

    if session is None:
        session = make_session()

    result = df.copy()

    # Initialise enrichment columns so rows that are skipped or unmatched have
    # consistent nulls/defaults rather than NaN mismatches later in the pipeline.
    if "latitude" not in result.columns:
        result["latitude"] = None
    if "longitude" not in result.columns:
        result["longitude"] = None
    for col in ["geocode_precision", "geocode_source", "geocode_match_type", "geocode_address_returned"]:
        if col not in result.columns:
            result[col] = None

    # Only attempt geocoding on rows that have not already been resolved.
    needs_geocode_mask = result["latitude"].isna()
    pending = result[needs_geocode_mask].copy()

    if pending.empty:
        return result

    # --- Census batch pass ---
    census_hits: dict[str, dict] = {}
    records_for_census = [_to_geocode_record(row) for _, row in pending.iterrows()]

    # Submit in chunks — census has practical timeout limits even though the
    # API accepts up to 10k; 2k keeps individual calls well under 2 minutes.
    for start in range(0, len(records_for_census), CENSUS_BATCH_SIZE):
        chunk = records_for_census[start : start + CENSUS_BATCH_SIZE]
        batch_hits = submit_census_batch(chunk, session)
        census_hits.update(batch_hits)

    # Apply Census results to result df.
    for natural_key, geo in census_hits.items():
        idx = result.index[result["natural_key"] == natural_key]
        if idx.empty:
            continue
        i = idx[0]
        _write_geo(result, i, {**geo, "geocode_source": "census"})

    # --- Nominatim fallback for Census misses ---
    if fallback_nominatim:
        still_missing_mask = result["latitude"].isna() & needs_geocode_mask
        for _, row in result[still_missing_mask].iterrows():
            geo = geocode_single_nominatim(_to_geocode_record(row), session)
            if geo:
                _write_geo(result, row.name, geo)

    # Fill remaining unmatched rows with explicit sentinel values.
    unmatched_mask = result["latitude"].isna()
    result.loc[unmatched_mask, "geocode_precision"] = "no_match"
    result.loc[unmatched_mask, "geocode_source"] = "none"
    result.loc[unmatched_mask, "geocode_match_type"] = result.loc[unmatched_mask, "geocode_match_type"].fillna("")
    result.loc[unmatched_mask, "geocode_address_returned"] = result.loc[unmatched_mask, "geocode_address_returned"].fillna("")

    return result


# ---------------------------------------------------------------------------
# DB write helper
# ---------------------------------------------------------------------------


def upsert_enrich_geocode(engine, source_id: str, enriched: pd.DataFrame) -> int:
    """
    Upsert geocode results into staging.enrich_geocode.

    PK is (source_id, natural_key) — on conflict the geocode fields are
    updated and enriched_at is bumped, which makes re-runs idempotent per D6.

    Only rows that have a latitude (i.e. geocode succeeded) contribute a geom;
    unmatched rows land with NULL lat/lon/geom.

    Returns the number of rows written.
    """
    if enriched.empty:
        logger.warning("upsert_enrich_geocode: empty DataFrame for source_id=%r — nothing written", source_id)
        return 0

    upsert_sql = text("""
        INSERT INTO staging.enrich_geocode
            (source_id, natural_key, latitude, longitude, geom, precision, source, match_type)
        VALUES (
            :source_id, :natural_key,
            CAST(:latitude AS numeric),
            CAST(:longitude AS numeric),
            CASE
                WHEN :latitude IS NOT NULL AND :longitude IS NOT NULL
                THEN ST_SetSRID(
                    ST_MakePoint(
                        CAST(:longitude AS numeric)::double precision,
                        CAST(:latitude AS numeric)::double precision
                    ), 4326)
                ELSE NULL
            END,
            :precision, :source, :match_type
        )
        ON CONFLICT (source_id, natural_key) DO UPDATE SET
            latitude     = EXCLUDED.latitude,
            longitude    = EXCLUDED.longitude,
            geom         = EXCLUDED.geom,
            precision    = EXCLUDED.precision,
            source       = EXCLUDED.source,
            match_type   = EXCLUDED.match_type,
            enriched_at  = now()
    """)

    rows = []
    for _, row in enriched.iterrows():
        lat = row.get("latitude")
        lon = row.get("longitude")

        # Coerce NaN → None so the CAST(:latitude AS numeric) doesn't error.
        def _clean(v):
            try:
                if v is None:
                    return None
                if isinstance(v, float) and math.isnan(v):
                    return None
                return v
            except (TypeError, ValueError):
                return None

        rows.append({
            "source_id": source_id,
            "natural_key": str(row["natural_key"]),
            "latitude": _clean(lat),
            "longitude": _clean(lon),
            "precision": row.get("geocode_precision") or None,
            "source": row.get("geocode_source") or None,
            "match_type": row.get("geocode_match_type") or None,
        })

    with engine.begin() as conn:
        conn.execute(upsert_sql, rows)

    logger.info(
        "upsert_enrich_geocode: wrote %d rows to staging.enrich_geocode (source_id=%r)",
        len(rows), source_id,
    )
    return len(rows)


def load_cached_geocodes(engine, source_id: str) -> dict[str, dict]:
    """
    Fetch already-geocoded rows from staging.enrich_geocode for this source.

    Returns a dict mapping natural_key → {latitude, longitude, geocode_precision,
    geocode_source, geocode_match_type}.  Used to pre-populate the input DataFrame
    so the enrich() function skips cached rows (D6 cache-hit path).
    """
    sql = text("""
        SELECT natural_key, latitude, longitude, precision, source, match_type
        FROM staging.enrich_geocode
        WHERE source_id = :source_id
          AND latitude IS NOT NULL
    """)
    with engine.connect() as conn:
        result = conn.execute(sql, {"source_id": source_id})
        rows = result.fetchall()

    cache: dict[str, dict] = {}
    for row in rows:
        cache[row[0]] = {
            "latitude": float(row[1]) if row[1] is not None else None,
            "longitude": float(row[2]) if row[2] is not None else None,
            "geocode_precision": row[3],
            "geocode_source": row[4],
            "geocode_match_type": row[5],
        }
    return cache


def apply_cache(df: pd.DataFrame, cache: dict[str, dict]) -> pd.DataFrame:
    """
    Pre-populate lat/lon columns from the enrich_geocode cache so enrich()
    skips those rows.  Rows not in cache are left with NaN latitude (will geocode).
    """
    df = df.copy()
    if "latitude" not in df.columns:
        df["latitude"] = None
    if "longitude" not in df.columns:
        df["longitude"] = None

    for col in ["geocode_precision", "geocode_source", "geocode_match_type", "geocode_address_returned"]:
        if col not in df.columns:
            df[col] = None

    for idx, row in df.iterrows():
        nk = str(row["natural_key"])
        if nk in cache:
            cached = cache[nk]
            df.at[idx, "latitude"] = cached["latitude"]
            df.at[idx, "longitude"] = cached["longitude"]
            df.at[idx, "geocode_precision"] = cached.get("geocode_precision")
            df.at[idx, "geocode_source"] = cached.get("geocode_source")
            df.at[idx, "geocode_match_type"] = cached.get("geocode_match_type")

    return df


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entrypoint — load input CSV, geocode any source, write enriched output."""
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--source-id",
        required=True,
        metavar="SOURCE_ID",
        help="source_id matching the staging table (e.g. nppes_practice_locations, cms_general)",
    )
    ap.add_argument("--input", required=True, metavar="CSV", help="input CSV path")
    ap.add_argument(
        "--out",
        default="geocode_enriched.csv",
        metavar="CSV",
        help="output CSV path (default: geocode_enriched.csv)",
    )
    ap.add_argument(
        "--no-nominatim",
        action="store_true",
        help="skip Nominatim fallback for Census misses",
    )
    ap.add_argument(
        "--write-db",
        action="store_true",
        help="Write geocode results to staging.enrich_geocode (requires DATABASE_URL). "
             "Uses the cache table as a skip-list so already-geocoded rows are not re-fetched.",
    )
    args = ap.parse_args()

    df = pd.read_csv(args.input, dtype=str, keep_default_na=False)
    print(f"loaded {len(df):,} rows from {args.input}", file=sys.stderr)

    session = make_session()

    # If writing to DB, pre-populate from the geocode cache so the enrich()
    # function skips rows we already have results for (D6 idempotency).
    if args.write_db:
        if not get_secret("DATABASE_URL"):
            sys.exit(
                "ERROR: --write-db was given but DATABASE_URL is not set. "
                "Copy .env.example -> .env and fill it in."
            )
        engine = get_engine()
        cache = load_cached_geocodes(engine, args.source_id)
        print(
            f"  geocode_enrich: {len(cache):,} rows already cached for source_id={args.source_id!r}",
            file=sys.stderr,
        )
        df = apply_cache(df, cache)
    else:
        engine = None

    enriched = enrich(df, session=session, fallback_nominatim=not args.no_nominatim)

    enriched.to_csv(args.out, index=False)
    print(f"wrote {len(enriched):,} rows -> {args.out}", file=sys.stderr)

    print_summary(enriched)

    if args.write_db and engine is not None:
        written = upsert_enrich_geocode(engine, args.source_id, enriched)
        print(
            f"  geocode_enrich: upserted {written:,} rows to staging.enrich_geocode",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
