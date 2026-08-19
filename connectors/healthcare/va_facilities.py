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
import os
import sys
from typing import Generator

import pandas as pd
import requests

from lib.http import get_secret, make_session
from lib.normalize import normalize_zip

# ---------------------------------------------------------------- constants

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

_TARGET_STATES = ("FL", "TX", "NC", "SC", "PA")


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
) -> pd.DataFrame:
    """
    Fetch all VA health facilities and return a raw DataFrame.

    Each row corresponds to one facility object as returned by the API.
    Nested attributes are flattened into individual columns here so that
    downstream stages work on a flat, predictable shape.
    """
    rows = []
    for facility in fetch_all_facilities(session, api_key, per_page=per_page):
        attrs = facility.get("attributes", {})
        physical = attrs.get("address", {}).get("physical", {})
        operating_status = attrs.get("operatingStatus", {})
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
        })

    print(f"  va_facilities: fetched {len(rows):,} raw records", file=sys.stderr)
    return pd.DataFrame(rows)


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

    return df


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
    args = ap.parse_args()

    api_key = get_secret("VA_API_KEY", required=True)
    session = make_session()

    raw = load_raw(session, api_key, per_page=args.per_page)
    assert_source_shape(raw)

    df = normalize(raw)
    report_quality(df)

    canonical = to_canonical(df)
    canonical.to_csv(args.out, index=False)

    print(f"\n  wrote {len(canonical):,} records -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
