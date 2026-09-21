"""
VA National Cemetery Sites — Socrata CSV export.

Source  : https://datahub.va.gov/api/views/fcxt-zc8r/rows.csv?accessType=DOWNLOAD
Format  : CSV, ~170 records, refreshed periodically by the VA.
License : Public domain — U.S. Department of Veterans Affairs open data.

Verified field names and record counts from live API on 2026-08-18:
  cemetery_name — cemetery name (always populated)
  state         — FULL state name ("Alabama"), NOT 2-letter abbreviation
  address       — PACKED: "Street, City, ST ZIPCODE" in one string
  latitude      — decimal degrees WGS84 (float as string)
  longitude     — decimal degrees WGS84 (float as string)
  contact       — PACKED: "Phone: NNN-NNN-NNNN, FAX: ..." in one string
  burial_space  — "Open", "Closed", or "Cremation Only"

No dedicated ID field exists in the CSV export. Natural key:
  cemetery_name + '|' + state (state as full name — stable).

All 170 records are federal sites managed by the National Cemetery
Administration. None qualify as commercial leads; segment='federal' is
set here to exclude them at the merge layer without a separate filter pass.
"""

from __future__ import annotations

import argparse
import datetime
import io
import re
import sys

import pandas as pd
import requests

from lib.geo import STATE_NAME_TO_ABBR
from lib.normalize import normalize_name, normalize_phone, normalize_zip
from lib.schema import build_canonical
from lib.validate import assert_columns_present, assert_min_rows

# ---------------------------------------------------------------- constants

# D3: source_id is a constant per source; the per-row id lives in natural_key.
SOURCE_ID = "va_cemeteries"

SOURCE_URL = "https://datahub.va.gov/api/views/fcxt-zc8r/rows.csv?accessType=DOWNLOAD"

_MIN_EXPECTED_ROWS = 150

_REQUIRED_COLUMNS = [
    "cemetery_name",
    "state",
    "address",
    "latitude",
    "longitude",
    "contact",
]

_TARGET_STATES = ("FL", "TX", "NC", "SC", "PA")

# Packed-field regexes — compiled once at module load.
_RE_ZIP = re.compile(r"(\d{5})$")
_RE_STATE_ABBR = re.compile(r"\b([A-Z]{2})\s+\d{5}$")
# First phone number before a comma, "Or" alternate, or end of string.
_RE_PHONE = re.compile(r"Phone:\s*([\d\-\(\) ]+?)(?:,|\s+Or\s+|$)", re.IGNORECASE)


# ---------------------------------------------------------------- helpers

def _extract_zip(address: str | None) -> str:
    """Return 5-digit ZIP from a packed address string, or ''."""
    if not address:
        return ""
    m = _RE_ZIP.search(address.strip())
    return m.group(1) if m else ""


def _extract_state_abbr_from_address(address: str | None) -> str:
    """Return 2-letter state abbreviation from a packed address string, or ''."""
    if not address:
        return ""
    m = _RE_STATE_ABBR.search(address.strip())
    return m.group(1) if m else ""


def _extract_city(address: str | None) -> str:
    """
    Return city from a packed "Street, City, ST ZIPCODE" string.

    City is the segment immediately before the "ST ZIPCODE" token.
    Splitting on comma and taking the second-to-last part is robust to
    streets that themselves contain commas.
    """
    if not address:
        return ""
    # Drop the trailing "ST ZIPCODE" suffix before splitting.
    # Everything up to and including the 2-letter state + ZIP is removed.
    stripped = _RE_STATE_ABBR.sub("", address.strip()).rstrip(" ,")
    parts = [p.strip() for p in stripped.split(",")]
    return parts[-1] if parts else ""


def _extract_street(address: str | None) -> str:
    """
    Return street portion from a packed "Street, City, ST ZIPCODE" string.

    Street is everything before the city segment — i.e., all comma-delimited
    parts except the last one (city) after the state+ZIP suffix is stripped.
    """
    if not address:
        return ""
    stripped = _RE_STATE_ABBR.sub("", address.strip()).rstrip(" ,")
    parts = [p.strip() for p in stripped.split(",")]
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return ", ".join(parts[:-1])


def _extract_phone(contact: str | None) -> str:
    """Return first phone number string from a packed contact field, or ''."""
    if not contact:
        return ""
    m = _RE_PHONE.search(contact)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------- extract

def load_raw(path_or_url: str | None = None) -> pd.DataFrame:
    """
    Download the VA National Cemetery CSV and return a raw DataFrame.

    ``path_or_url`` defaults to the Socrata download URL. Pass a local file
    path to load a cached copy without a network call (used in tests).

    All columns are read as strings to prevent pandas from coercing ZIP codes
    or phone fragments into floats.
    """
    source = path_or_url or SOURCE_URL

    if source.startswith("http"):
        response = requests.get(source, timeout=60)
        response.raise_for_status()
        text = response.text
        df = pd.read_csv(io.StringIO(text), dtype=str)
    else:
        df = pd.read_csv(source, dtype=str)

    # Socrata has since re-exported this dataset with Title Case headers
    # ("Cemetery Name", "Burial Space") instead of the lowercase snake_case
    # this connector was written against ("cemetery_name", "burial_space").
    # Normalize so downstream column lookups keep working either way.
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    df["source_file"] = SOURCE_URL
    return df


# ---------------------------------------------------------------- checks

def assert_source_shape(df: pd.DataFrame) -> None:
    """
    Raise ValueError if the loaded data does not match the known source shape.

    Guards against: Socrata layout changes, truncated downloads, and encoding
    issues that would corrupt full-name state values.
    """
    assert_columns_present(df, _REQUIRED_COLUMNS, label="va_cemeteries")
    assert_min_rows(df, _MIN_EXPECTED_ROWS, label="va_cemeteries")

    known_names = set(STATE_NAME_TO_ABBR.keys())
    actual_names = set(df["state"].dropna().unique())
    unknown = actual_names - known_names
    if unknown:
        raise ValueError(
            f"va_cemeteries: unrecognized state full-names: {sorted(unknown)}. "
            "Update STATE_NAME_TO_ABBR in lib/geo.py or check for encoding issues in the source."
        )


# ---------------------------------------------------------------- transform

def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add derived columns used by to_canonical() and the deathcare merge module.

    Packed fields (address, contact) are parsed here so that to_canonical()
    can map individual columns without any parsing logic.
    """
    df = df.copy()

    df["name_normalized"] = df["cemetery_name"].map(normalize_name)

    df["address_line_1"] = df["address"].map(_extract_street)
    df["city"] = df["address"].map(_extract_city)
    df["state_abbr"] = df["address"].map(_extract_state_abbr_from_address)
    df["zip5"] = df["address"].map(_extract_zip).map(normalize_zip)

    df["phone_raw"] = df["contact"].map(_extract_phone)
    df["phone_normalized"] = df["phone_raw"].map(normalize_phone)

    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")

    df["segment"] = "federal"
    df["county_fips"] = None
    df["ein"] = None

    return df


# ---------------------------------------------------------------- quality

def report_quality(df: pd.DataFrame) -> None:
    """Log data quality metrics to stderr."""
    total = len(df)
    sys.stderr.write(f"  va_cemeteries: {total:,} total records\n")

    sys.stderr.write("  va_cemeteries: target-state rows\n")
    for abbr in _TARGET_STATES:
        count = (df["state_abbr"] == abbr).sum()
        sys.stderr.write(f"    {abbr}  {count:>5,}\n")

    sys.stderr.write("  va_cemeteries: burial_space breakdown\n")
    if "burial_space" in df.columns:
        for val, count in df["burial_space"].value_counts(dropna=False).items():
            sys.stderr.write(f"    {val}  {count:>5,}\n")

    parseable_phone = df["phone_normalized"].str.len().gt(0).mean()
    sys.stderr.write(f"  va_cemeteries: parseable phone  {parseable_phone:.1%}\n")


# ---------------------------------------------------------------- canonical output

def to_canonical(df: pd.DataFrame) -> pd.DataFrame:
    """Map normalized VA cemetery columns to the standard deathcare output shape."""
    # D3: source_id is the constant SOURCE_ID; natural_key carries the per-row id.
    # The per-row natural key uses the full state name (stable across renames) to
    # match the original design; the name+state combo is the only stable identifier.
    natural_key = df["cemetery_name"] + "|" + df["state"]

    return build_canonical(
        df.index,
        source_id=SOURCE_ID,
        natural_key=natural_key,
        vertical="deathcare",
        account_type="federal",
        name_raw=df["cemetery_name"],
        name_normalized=df["name_normalized"],
        address_line_1=df["address_line_1"],
        city=df["city"],
        state=df["state_abbr"],
        zip5=df["zip5"],
        phone_raw=df["phone_raw"],
        phone_normalized=df["phone_normalized"],
        latitude=df["latitude"],
        longitude=df["longitude"],
        segment="federal",
        source_file=df["source_file"],
    )


# ---------------------------------------------------------------- entrypoint


def main() -> None:
    """CLI entrypoint — fetch VA cemetery data and optionally write to DB."""
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--out",
        default="va_cemeteries.csv",
        help="Output CSV path (default: va_cemeteries.csv)",
    )
    ap.add_argument(
        "--write-db",
        action="store_true",
        help="Also write results to Postgres staging (requires DATABASE_URL). "
             "Off by default — the CSV is always written regardless.",
    )
    ap.add_argument(
        "--input",
        default=None,
        metavar="PATH_OR_URL",
        help="Local CSV path or URL to load instead of the live VA source. "
             "Useful for testing or offline replay.",
    )
    args = ap.parse_args()

    source = args.input or SOURCE_URL
    sys.stderr.write(f"  va_cemeteries: loading {source}\n")

    raw = load_raw(source)
    assert_source_shape(raw)

    normalized = normalize(raw)
    report_quality(normalized)
    canonical = to_canonical(normalized)

    # Use the raw CSV bytes for the sha256 (or serialize if from URL).
    import json as _json
    raw_bytes = _json.dumps(
        raw.to_dict(orient="records"), sort_keys=True
    ).encode("utf-8")

    canonical.to_csv(args.out, index=False)
    sys.stderr.write(f"\n  wrote {len(canonical):,} records -> {args.out}\n")

    if args.write_db:
        from lib.db import get_engine, write_source_run, upsert_staging, finish_source_run
        from lib.gcs import raw_sha256, upload_raw
        from lib.http import get_secret

        if not get_secret("DATABASE_URL"):
            sys.exit(
                "ERROR: --write-db was given but DATABASE_URL is not set. "
                "Copy .env.example -> .env and fill it in."
            )

        sha256_hex = raw_sha256(raw_bytes)
        byte_count = len(raw_bytes)
        sys.stderr.write(
            f"  va_cemeteries: sha256={sha256_hex[:16]}…  bytes={byte_count:,}\n"
        )

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
                license_string="VA open data — public domain",
                raw_uri=raw_uri,
            )

            upsert_staging(engine, SOURCE_ID, canonical)

            finish_source_run(
                engine,
                source_run_id,
                status="succeeded",
                row_count=len(canonical),
            )
            sys.stderr.write(
                f"  va_cemeteries: wrote {len(canonical):,} rows "
                f"to staging.{SOURCE_ID} (source_run_id={source_run_id})\n"
            )
        except Exception as exc:
            if source_run_id is not None:
                finish_source_run(engine, source_run_id, status="failed")
            sys.exit(f"ERROR: DB write failed — {exc}")


if __name__ == "__main__":
    main()
