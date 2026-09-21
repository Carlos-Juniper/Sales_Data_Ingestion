"""
TxDOT Texas Cemeteries — ArcGIS FeatureServer connector.

Source  : https://services.arcgis.com/KTcxiTD9dsQw4r7Z/arcgis/rest/services/Texas_Cemeteries/FeatureServer/0
License : Texas Department of Transportation open data, public use.

Verified field names and record counts from live API on 2026-08-18:
  GID          — Geometry ID (double), upstream source key used as natural key
  CEMETERY_NM  — cemetery name (some null)
  CITY_NM      — city name (frequently null)
  CNTY_NBR     — county integer code 1–254 (NOT a FIPS code — do not conflate)
  DIST_NM      — TxDOT district name (e.g., "Abilene")
  DIST_NBR     — TxDOT district number integer
  OBJECTID     — ArcGIS OID, used for pagination ordering

Total records: 7,991 (all Texas; no state filter needed)

No address, phone, ZIP, or EIN data exists in this layer. Geometry is the
only location signal. CITY_NM is frequently null — do not rely on it.
CNTY_NBR is an integer county code (1–254), not a FIPS code; pass through
as-is and never attempt to decode it into county_fips.
"""

from __future__ import annotations

import argparse
import datetime
import sys

import pandas as pd
import requests

from lib import arcgis
from lib.normalize import int_key, normalize_name
from lib.schema import build_canonical
from lib.validate import assert_columns_present, assert_fill_rate, assert_min_rows

# ---------------------------------------------------------------- constants

# D3: source_id is a constant per source; the per-row id lives in natural_key.
SOURCE_ID = "txdot_cemeteries"

SOURCE_URL = (
    "https://services.arcgis.com/KTcxiTD9dsQw4r7Z/arcgis/rest/services"
    "/Texas_Cemeteries/FeatureServer/0"
)

_OUT_FIELDS = "CEMETERY_NM,CITY_NM,CNTY_NBR,DIST_NM,GID,OBJECTID"

# Row count guard — 7,991 records confirmed on 2026-08-18.
# 6,000 allows for moderate attrition without masking a broken fetch.
_MIN_EXPECTED_ROWS = 6_000


# ---------------------------------------------------------------- extract

def fetch(session: requests.Session | None = None) -> pd.DataFrame:
    """
    Download all Texas cemetery point features.

    Geometry is requested in WGS84 (outSR=4326) — the layer stores features
    in Web Mercator (EPSG:3857) by default. Coordinates come from the GeoJSON
    geometry object and are parsed here before returning.

    Returns a raw DataFrame with one row per TxDOT cemetery feature.
    """
    rows = []
    for feature in arcgis.iter_features(
        base_url=SOURCE_URL,
        where="1=1",
        out_fields=_OUT_FIELDS,
        order_by="OBJECTID",
        return_geometry=True,
        session=session,
    ):
        props = arcgis.feature_props(feature)
        lon, lat = arcgis.feature_lonlat(feature)

        row = {
            "CEMETERY_NM": props.get("CEMETERY_NM"),
            "CITY_NM": props.get("CITY_NM"),
            "CNTY_NBR": props.get("CNTY_NBR"),
            "DIST_NM": props.get("DIST_NM"),
            "GID": props.get("GID"),
            "OBJECTID": props.get("OBJECTID"),
            "_lon": lon,
            "_lat": lat,
        }
        rows.append(row)

    df = pd.DataFrame(rows)
    df["OBJECTID"] = pd.to_numeric(df.get("OBJECTID"), errors="coerce")
    df["source_file"] = SOURCE_URL
    return df


# ---------------------------------------------------------------- checks

def assert_source_shape(df: pd.DataFrame) -> None:
    """
    Raise ValueError if the fetched data does not match the known source shape.

    Guards against: API field renames, truncated fetches, and accidental
    filtering that would cause silent data loss downstream.
    """
    assert_columns_present(df, ["GID", "CEMETERY_NM", "OBJECTID"], label="TxDOT cemeteries fetch")
    # GID is a double field; some nulls are acceptable but a fill rate this
    # low would mean the natural key is unusable for deduplication.
    assert_fill_rate(
        df, "GID", 0.95,
        label="GID is the natural key — a low fill rate means the layer changed",
    )
    assert_min_rows(df, _MIN_EXPECTED_ROWS, label="TxDOT cemeteries returned")


# ---------------------------------------------------------------- transform

def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add derived columns used by to_canonical() and the deathcare merge module.

    Does NOT drop rows with null CEMETERY_NM — unnamed cemeteries are valid
    records and the filter decision belongs to the merge layer.

    GID is a double (e.g., 2.0); natural_key_str casts it to an integer string
    (e.g., '2') to avoid floating-point suffixes in the key.
    """
    df = df.copy()

    df["name_normalized"] = df["CEMETERY_NM"].map(normalize_name)

    # GID arrives as a float (e.g., 2.0) — drop the decimal for a clean key.
    df["natural_key_str"] = df["GID"].map(int_key)

    df["latitude"] = pd.to_numeric(df["_lat"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["_lon"], errors="coerce")
    df["city"] = df["CITY_NM"]
    df["state"] = "TX"

    # No county FIPS, EIN, phone, ZIP, or segment data in this source.
    df["county_fips"] = None
    df["ein"] = None
    df["zip5"] = None
    df["segment"] = None

    return df


# ---------------------------------------------------------------- quality

def report_quality(df: pd.DataFrame) -> None:
    """Log data quality metrics to stderr."""
    total = len(df)
    sys.stderr.write(f"  txdot_cemeteries: {total:,} total records\n")

    null_name = df["CEMETERY_NM"].isna().mean()
    null_city = df["CITY_NM"].isna().mean()
    sys.stderr.write(f"  txdot_cemeteries: null CEMETERY_NM  {null_name:.1%}\n")
    sys.stderr.write(f"  txdot_cemeteries: null CITY_NM       {null_city:.1%}\n")

    sys.stderr.write("  txdot_cemeteries: top 5 districts by record count\n")
    dist_counts = df["DIST_NM"].value_counts().head(5)
    for dist, count in dist_counts.items():
        sys.stderr.write(f"    {dist}  {count:>6,}\n")


# ---------------------------------------------------------------- canonical output

def to_canonical(df: pd.DataFrame) -> pd.DataFrame:
    """Map normalized TxDOT cemetery columns to the standard deathcare output shape."""
    # D3: source_id is the constant SOURCE_ID; natural_key carries the per-row id.
    return build_canonical(
        df.index,
        source_id=SOURCE_ID,
        natural_key=df["natural_key_str"],
        vertical="deathcare",
        account_type="cemetery",
        name_raw=df["CEMETERY_NM"],
        name_normalized=df["name_normalized"],
        city=df["city"],
        state=df["state"],
        latitude=df["latitude"],
        longitude=df["longitude"],
        source_file=df["source_file"],
    )


# ---------------------------------------------------------------- entrypoint


def main() -> None:
    """CLI entrypoint — fetch TxDOT cemetery data and optionally write to DB."""
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--out",
        default="txdot_cemeteries.csv",
        help="Output CSV path (default: txdot_cemeteries.csv)",
    )
    ap.add_argument(
        "--write-db",
        action="store_true",
        help="Also write results to Postgres staging (requires DATABASE_URL). "
             "Off by default — the CSV is always written regardless.",
    )
    ap.add_argument(
        "--max-records",
        type=int,
        default=None,
        help="Truncate fetch to at most N records (useful for testing).",
    )
    args = ap.parse_args()

    sys.stderr.write(f"  txdot_cemeteries: fetching {SOURCE_URL}\n")

    session = requests.Session()
    raw = fetch(session=session)

    if args.max_records is not None:
        raw = raw.head(args.max_records)

    assert_source_shape(raw)

    normalized = normalize(raw)
    report_quality(normalized)
    canonical = to_canonical(normalized)

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
            f"  txdot_cemeteries: sha256={sha256_hex[:16]}…  bytes={byte_count:,}\n"
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
                license_string="TxDOT open data — public use",
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
                f"  txdot_cemeteries: wrote {len(canonical):,} rows "
                f"to staging.{SOURCE_ID} (source_run_id={source_run_id})\n"
            )
        except Exception as exc:
            if source_run_id is not None:
                finish_source_run(engine, source_run_id, status="failed")
            sys.exit(f"ERROR: DB write failed — {exc}")


if __name__ == "__main__":
    main()
