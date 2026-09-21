"""
VA Health Facilities — VA Facilities API (Lighthouse).

Source  : https://api.va.gov/services/va_facilities/v1/facilities
Auth    : Free API key via apikey header — register at https://developer.va.gov
Filter  : type=health only (excludes national_cemetery, benefits, vet_center
          which are ingested separately by connectors/deathcare/va_cemeteries.py)
License : Public domain — U.S. Department of Veterans Affairs open data.

Output columns
--------------
  natural_key    — facility id (e.g. vha_123)
  name_raw       — facility name as returned by API
  address_line_1 — physical street address
  city           — city name
  site_state     — 2-letter state abbreviation
  zip5           — 5-digit ZIP
  latitude       — WGS84 decimal degrees
  longitude      — WGS84 decimal degrees
  facility_type  — facilityType from API (e.g. va_health_facility)
  operating_status — operatingStatus.code (e.g. NORMAL, CLOSED)

Usage:
    VA_API_KEY=yourkey python va_facilities.py --out va_health.csv
    python va_facilities.py --out va_health.csv  # reads VA_API_KEY from .env

Sandbox vs. production
-----------------------
A sandbox API key (the free, instant-approval kind from developer.va.gov) only
works against the sandbox host and returns 401 Unauthorized against
production. Production access to real nationwide facility data requires a
separate approval step. Set VA_API_BASE_URL to override the host — e.g.:

    VA_API_BASE_URL=https://sandbox-api.va.gov/services/va_facilities/v1/facilities \\
        python va_facilities.py --out va_health_sandbox.csv
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from typing import Generator

import pandas as pd
import requests

from lib.db import finish_source_run, get_engine, upsert_staging, write_source_run
from lib.enums import HEALTHCARE_TARGET_STATES
from lib.gcs import raw_sha256, upload_raw
from lib.http import get_secret, make_session
from lib.normalize import normalize_name, normalize_phone, normalize_zip
from lib.schema import build_canonical

# ---------------------------------------------------------------- constants

SOURCE_ID = "va_facilities"
VERTICAL = "healthcare"

PRODUCTION_BASE_URL = "https://api.va.gov/services/va_facilities/v1/facilities"
SANDBOX_BASE_URL = "https://sandbox-api.va.gov/services/va_facilities/v1/facilities"

# Defaults to production — the real target for this connector. Override with
# VA_API_BASE_URL to point at the sandbox host when only a sandbox key is
# available (sandbox keys 401 against the production host).
SOURCE_URL = os.getenv("VA_API_BASE_URL", PRODUCTION_BASE_URL)

_MIN_EXPECTED_ROWS = 100

# Facility types that belong in this connector. Any new type from the API
# is surfaced as a ValueError in assert_source_shape so we can triage it.
_KNOWN_FACILITY_TYPES = {
    "va_health_facility",
}

# Alias the shared constant so report_quality() and any callers of _TARGET_STATES
# stay readable without duplicating the tuple.  This is the single source of truth.
_TARGET_STATES = HEALTHCARE_TARGET_STATES


# ---------------------------------------------------------------- extract

def fetch_all_facilities(
    session: requests.Session,
    api_key: str,
    per_page: int = 100,
) -> Generator[dict, None, None]:
    """
    Paginate through all VA health facilities and yield raw facility dicts.

    Passes type=health to exclude national_cemetery, benefits, and vet_center
    records — those are ingested by connectors/deathcare/va_cemeteries.py.

    The API uses 1-based page numbering. Pagination stops when currentPage
    reaches totalPages (not when the data list is empty), matching the
    Lighthouse pagination contract.
    """
    headers = {"apikey": api_key}
    page = 1

    while True:
        params = {
            "type": "health",
            "page": page,
            "per_page": per_page,
        }
        resp = session.get(SOURCE_URL, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        body = resp.json()

        facilities = body.get("data", [])
        for facility in facilities:
            yield facility

        pagination = body.get("meta", {}).get("pagination", {})
        current_page = pagination.get("currentPage", page)
        total_pages = pagination.get("totalPages", 1)

        if current_page >= total_pages:
            break

        page += 1


def load_raw(
    session: requests.Session,
    api_key: str,
    per_page: int = 100,
) -> tuple[pd.DataFrame, bytes]:
    """
    Fetch all VA health facilities and return a raw DataFrame plus raw bytes.

    Each row corresponds to one facility object as returned by the API.
    Nested attributes are flattened into individual columns here so that
    downstream stages work on a flat, predictable shape.

    The raw bytes are the canonical JSON serialisation of the facility list
    (sort_keys=True for determinism) — these are what get hashed and uploaded
    to GCS (D7 / B4 fix).

    Returns:
        (DataFrame of flattened facility rows, JSON bytes of the raw facility list)
    """
    raw_facilities = list(fetch_all_facilities(session, api_key, per_page=per_page))

    rows = []
    for facility in raw_facilities:
        attrs = facility.get("attributes", {})
        physical = attrs.get("address", {}).get("physical", {})
        operating_status = attrs.get("operatingStatus", {})
        # VA Lighthouse API exposes attributes.phone.main for the main phone number.
        # The phone object may be absent for some facilities — default to "".
        phone_obj = attrs.get("phone") or {}
        phone_main = str(phone_obj.get("main") or "").strip()
        rows.append({
            "id": facility.get("id"),
            "name": attrs.get("name"),
            "facilityType": attrs.get("facilityType"),
            "address1": physical.get("address1"),
            "city": physical.get("city"),
            "state": physical.get("state"),
            "zip": physical.get("zip"),
            "lat": attrs.get("lat"),
            "long": attrs.get("long"),
            "operating_status_code": operating_status.get("code"),
            "phone_main": phone_main,
        })

    print(f"  va_facilities: fetched {len(rows):,} raw records", file=sys.stderr)

    # Serialise the original API objects (not the flattened rows) so the hash
    # captures the full fidelity payload — sort_keys=True for determinism.
    raw_bytes = json.dumps(raw_facilities, sort_keys=True, default=str).encode("utf-8")
    return pd.DataFrame(rows), raw_bytes


# ---------------------------------------------------------------- checks

def assert_source_shape(df: pd.DataFrame) -> None:
    """
    Raise ValueError if the loaded data does not match the known API shape.

    Guards against: API contract changes, new undocumented facility types,
    truncated responses, and the row-count floor dropping below the
    historical minimum.
    """
    required_columns = {
        "id", "name", "facilityType", "address1",
        "city", "state", "zip", "lat", "long", "operating_status_code",
    }
    missing = required_columns - set(df.columns)
    if missing:
        raise ValueError(
            f"va_facilities: missing column(s) {missing} — API shape changed"
        )

    if len(df) < _MIN_EXPECTED_ROWS:
        raise ValueError(
            f"va_facilities: only {len(df):,} rows — expected at least "
            f"{_MIN_EXPECTED_ROWS:,}. Possible truncated response or API error."
        )

    unknown_types = set(df["facilityType"].dropna().unique()) - _KNOWN_FACILITY_TYPES
    if unknown_types:
        raise ValueError(
            f"va_facilities: unknown facilityType value(s) {unknown_types}. "
            "Update _KNOWN_FACILITY_TYPES or verify the type=health filter is still "
            "excluding non-health records."
        )


# ---------------------------------------------------------------- transform

def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """
    Parse and clean raw API fields into typed, well-shaped columns.

    Latitude and longitude come from the API as floats but may occasionally
    arrive as strings — pd.to_numeric handles both safely.
    ZIP codes from the API can be 9-digit ZIP+4; normalize_zip extracts the
    first 5 digits and preserves leading zeros as strings.
    """
    df = df.copy()

    df["latitude"] = pd.to_numeric(df["lat"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["long"], errors="coerce")

    # normalize_zip returns "" on null/unparseable input rather than raising.
    df["zip5"] = df["zip"].map(normalize_zip)

    df["address_line_1"] = df["address1"].str.strip().fillna("")
    df["city_clean"] = df["city"].str.strip().fillna("")
    df["state_abbr"] = df["state"].str.strip().str.upper().fillna("")

    # Normalize phone: phone_main is extracted by load_raw() from the API;
    # it may be absent in unit-test DataFrames that don't go through load_raw().
    # Use .get() with a fallback so existing tests that pass raw-shaped DataFrames
    # directly to normalize() don't fail on the missing column.
    phone_main_series = df["phone_main"] if "phone_main" in df.columns else pd.Series("", index=df.index)
    df["phone_raw"] = phone_main_series.fillna("").astype(str).str.strip()
    df["phone_normalized"] = df["phone_raw"].map(normalize_phone)

    return df


# ---------------------------------------------------------------- state filter

def filter_to_target_states(df: pd.DataFrame) -> pd.DataFrame:
    """
    Drop rows whose state_abbr is not in HEALTHCARE_TARGET_STATES (FL/NC/TX/PA/SC).

    Must be called AFTER normalize() — which produces state_abbr — and BEFORE
    to_canonical() and upsert_staging() so that out-of-state facilities never
    reach geocoding or the database (D10).

    Returns a copy; the input DataFrame is not mutated.
    """
    before = len(df)
    filtered = df[df["state_abbr"].isin(_TARGET_STATES)].copy()
    after = len(filtered)
    print(
        f"  va_facilities: state filter ({'/'.join(sorted(_TARGET_STATES))}): "
        f"{before:,} -> {after:,} rows",
        file=sys.stderr,
    )
    return filtered


# ---------------------------------------------------------------- quality

def report_quality(df: pd.DataFrame) -> None:
    """Log data quality metrics to stderr."""
    total = len(df)
    sys.stderr.write(f"  va_facilities: {total:,} total records\n")

    sys.stderr.write("  va_facilities: target-state breakdown\n")
    for abbr in _TARGET_STATES:
        count = (df["state_abbr"] == abbr).sum()
        sys.stderr.write(f"    {abbr}  {count:>5,}\n")

    sys.stderr.write("  va_facilities: operating_status breakdown\n")
    for val, count in df["operating_status_code"].value_counts(dropna=False).items():
        sys.stderr.write(f"    {val}  {count:>5,}\n")


# ---------------------------------------------------------------- canonical output

def to_canonical(df: pd.DataFrame) -> pd.DataFrame:
    """
    Map normalized columns to the standard healthcare output shape.

    The output columns are the contract consumed by parcel_acreage_enrich.py:
    natural_key and site_state drive the parcel lookup; latitude and longitude
    drive the spatial point query.
    """
    return pd.DataFrame({
        "natural_key": df["id"],
        "name_raw": df["name"],
        "address_line_1": df["address_line_1"],
        "city": df["city_clean"],
        "site_state": df["state_abbr"],
        "zip5": df["zip5"],
        "latitude": df["latitude"],
        "longitude": df["longitude"],
        "facility_type": df["facilityType"],
        "operating_status": df["operating_status_code"],
        "phone_raw": df["phone_raw"],
        "phone_normalized": df["phone_normalized"],
    })


# ---------------------------------------------------------------- entrypoint

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--out", default="va_health.csv",
                    help="Output CSV path (default: va_health.csv)")
    ap.add_argument("--per-page", type=int, default=100,
                    help="Records per API page (default: 100, max: 100)")
    ap.add_argument(
        "--write-db",
        action="store_true",
        help="Also write results to Postgres staging (requires DATABASE_URL). "
             "Off by default — the CSV is always written regardless.",
    )
    args = ap.parse_args()

    api_key = get_secret("VA_API_KEY", required=True)
    session = make_session()

    raw, raw_bytes = load_raw(session, api_key, per_page=args.per_page)
    assert_source_shape(raw)

    # B4 fix: hash the actual fetched JSON bytes (sort_keys=True serialisation
    # of the raw API response objects), not a pandas re-serialisation.
    sha256_hex = raw_sha256(raw_bytes)
    byte_count = len(raw_bytes)
    print(
        f"  va_facilities: sha256={sha256_hex[:16]}…  bytes={byte_count:,}",
        file=sys.stderr,
    )

    df = normalize(raw)
    # D10: filter to the 5 target states before building canonical output or
    # writing to the database — out-of-state rows must not reach upsert_staging().
    df = filter_to_target_states(df)
    report_quality(df)

    canonical = to_canonical(df)
    canonical.to_csv(args.out, index=False)

    print(f"\n  wrote {len(canonical):,} records -> {args.out}", file=sys.stderr)

    # Write to Postgres only when explicitly requested via --write-db.
    if args.write_db:
        if not get_secret("DATABASE_URL"):
            sys.exit(
                "ERROR: --write-db was given but DATABASE_URL is not set. "
                "Copy .env.example -> .env and fill it in."
            )

        # D7: upload raw payload to GCS before writing source_run.
        # Returns None when GCS is disabled/unavailable — never raises.
        run_date = datetime.date.today().isoformat()
        raw_uri = upload_raw(SOURCE_ID, run_date, raw_bytes)

        engine = get_engine()
        source_run_id: int | None = None
        try:
            source_run_id = write_source_run(
                engine,
                source_id=SOURCE_ID,
                byte_count=byte_count,
                sha256=sha256_hex,
                connector_version="1.0",
                license_string="VA Facilities — public domain, U.S. Department of Veterans Affairs",
                raw_uri=raw_uri,
            )

            full_canonical = build_canonical(
                canonical.index,
                source_id=SOURCE_ID,
                natural_key=canonical["natural_key"],
                vertical=VERTICAL,
                account_type=canonical["facility_type"],
                name_raw=canonical["name_raw"],
                name_normalized=canonical["name_raw"].map(normalize_name),
                address_line_1=canonical["address_line_1"],
                city=canonical["city"],
                state=canonical["site_state"],
                zip5=canonical["zip5"],
                latitude=canonical["latitude"],
                longitude=canonical["longitude"],
                phone_raw=canonical["phone_raw"],
                phone_normalized=canonical["phone_normalized"],
                source_file=SOURCE_URL,
            )

            upsert_staging(engine, SOURCE_ID, full_canonical)

            finish_source_run(
                engine,
                source_run_id,
                status="succeeded",
                row_count=len(full_canonical),
            )
            print(
                f"  va_facilities: wrote {len(full_canonical):,} rows "
                f"to staging.{SOURCE_ID} (source_run_id={source_run_id})",
                file=sys.stderr,
            )
        except Exception as exc:
            if source_run_id is not None:
                finish_source_run(engine, source_run_id, status="failed")
            print(f"  va_facilities: DB write failed — {exc}", file=sys.stderr)
            raise


if __name__ == "__main__":
    main()
