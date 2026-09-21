"""
USGS National Structures Dataset (NSD) — cemetery layer.

Source  : https://carto.nationalmap.gov/arcgis/rest/services/structures/MapServer/37
Layer 37: Cemeteries (point features, WGS84 geometry)
License : Public domain — USGS National Map, no use restrictions.

Verified field names and record counts from live API on 2026-08-18:
  PERMANENT_IDENTIFIER — stable UUID string (natural key, >99% populated)
  NAME                 — cemetery name (frequently null for unnamed sites)
  STATE, CITY, ADDRESS, ZIPCODE, OBJECTID

Record counts by state: FL=3,951  TX=11,004  NC=4,928  SC=3,335  PA=9,530
Total across 5 states: 32,748

NAME IS FREQUENTLY NULL — unnamed burial sites are real records. Do not drop
them here. Downstream merge logic decides what to do with nameless sites.

No county FIPS field exists in this layer. No phone data. No EIN data.
"""

from __future__ import annotations

import argparse
import datetime
import sys

import pandas as pd
import requests

from lib import arcgis
from lib.normalize import normalize_name, normalize_zip
from lib.schema import build_canonical
from lib.validate import assert_columns_present, assert_fill_rate, assert_min_rows

# ---------------------------------------------------------------- constants

# D3: source_id is a constant per source; the per-row id lives in natural_key.
SOURCE_ID = "usgs_nsd"

SOURCE_URL = "https://carto.nationalmap.gov/arcgis/rest/services/structures/MapServer/37"

_DEFAULT_STATES = ("FL", "TX", "NC", "SC", "PA")

_OUT_FIELDS = [
    "PERMANENT_IDENTIFIER",
    "NAME",
    "STATE",
    "CITY",
    "ADDRESS",
    "ZIPCODE",
    "OBJECTID",
]

# Row count guard — we know 32,748 exist across the 5 target states.
# A fetch returning fewer than this suggests a truncated or filtered response.
_MIN_EXPECTED_ROWS = 25_000


# ---------------------------------------------------------------- extract

def fetch(
    session: requests.Session | None = None,
    states: tuple[str, ...] | list[str] | None = None,
) -> pd.DataFrame:
    """
    Download all cemetery point features for the target states.

    Geometry is requested in WGS84 (outSR=4326). The FeatureServer layer
    stores no dedicated lat/lon fields — coordinates come from the geometry
    object only and are parsed here before returning.

    Returns a raw DataFrame with one row per NSD feature. All attribute
    columns are kept as strings; latitude and longitude are floats.
    """
    target_states = list(states or _DEFAULT_STATES)
    state_list = ", ".join(f"'{s}'" for s in target_states)
    where = f"STATE IN ({state_list})"
    out_fields = ",".join(_OUT_FIELDS)

    rows = []
    for feature in arcgis.iter_features(
        base_url=SOURCE_URL,
        where=where,
        out_fields=out_fields,
        order_by="OBJECTID",
        return_geometry=True,
        session=session,
    ):
        props = arcgis.feature_props(feature)
        # iter_features requests f=geojson so coordinates come as [lon, lat].
        lon, lat = arcgis.feature_lonlat(feature)

        # The live layer returns attribute keys in lowercase (e.g.
        # 'permanent_identifier') even though _OUT_FIELDS and the WHERE clause
        # use uppercase — ArcGIS's SQL WHERE evaluation is case-insensitive on
        # column names, but the JSON/GeoJSON attribute payload preserves the
        # server's actual storage casing. Match case-insensitively here so a
        # casing mismatch doesn't silently null out every field.
        props_upper = {k.upper(): v for k, v in props.items()}
        row = {col: props_upper.get(col) for col in _OUT_FIELDS}
        row["_lon"] = lon
        row["_lat"] = lat
        rows.append(row)

    df = pd.DataFrame(rows)

    if df.empty:
        return df

    # Coerce attribute columns to string; preserve None as pd.NA.
    str_cols = [c for c in _OUT_FIELDS if c != "OBJECTID"]
    for col in str_cols:
        df[col] = df[col].astype("string")

    df["OBJECTID"] = pd.to_numeric(df.get("OBJECTID"), errors="coerce")
    df["source_file"] = SOURCE_URL
    return df


# ---------------------------------------------------------------- checks

def assert_source_shape(df: pd.DataFrame) -> None:
    """
    Raise ValueError if the fetched data does not match known source shape.

    Guards against: layout changes in the NSD API, truncated fetches, and
    accidental filtering that would cause silent data loss downstream.
    """
    assert_columns_present(df, ["PERMANENT_IDENTIFIER", "NAME", "STATE", "OBJECTID"], label="NSD fetch")

    unknown_states = set(df["STATE"].dropna().unique()) - set(_DEFAULT_STATES)
    if unknown_states:
        raise ValueError(
            f"NSD response contains unexpected STATE values: {sorted(unknown_states)}. "
            "Update _DEFAULT_STATES or widen the where clause."
        )

    assert_fill_rate(
        df, "PERMANENT_IDENTIFIER", 0.99,
        label="PERMANENT_IDENTIFIER is the natural key — a low fill rate means the layer changed",
    )
    assert_min_rows(df, _MIN_EXPECTED_ROWS, label="NSD returned")


# ---------------------------------------------------------------- transform

def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add derived columns used by to_canonical() and the deathcare merge module.

    Does NOT drop rows with null NAME — unnamed burial sites are valid NSD
    records and the filter decision belongs to the merge layer.
    """
    df = df.copy()

    df["name_normalized"] = df["NAME"].map(normalize_name)
    df["zip5"] = df["ZIPCODE"].map(normalize_zip)
    df["latitude"] = df["_lat"].astype(float)
    df["longitude"] = df["_lon"].astype(float)

    # No county FIPS, EIN, phone, or segment data in this source.
    df["county_fips"] = None
    df["ein"] = None
    df["segment"] = None

    return df


# ---------------------------------------------------------------- quality

def report_quality(df: pd.DataFrame) -> None:
    """Log data quality metrics to stderr."""
    total = len(df)
    sys.stderr.write(f"  nsd: {total:,} total records\n")

    sys.stderr.write("  nsd: rows by state\n")
    state_counts = df["STATE"].value_counts().sort_index()
    for state, count in state_counts.items():
        sys.stderr.write(f"    {state}  {count:>7,}\n")

    null_name = df["NAME"].isna().mean()
    null_addr = df["ADDRESS"].isna().mean()
    null_zip = df["ZIPCODE"].isna().mean()
    sys.stderr.write(f"  nsd: null NAME    {null_name:.1%}\n")
    sys.stderr.write(f"  nsd: null ADDRESS {null_addr:.1%}\n")
    sys.stderr.write(f"  nsd: null ZIPCODE {null_zip:.1%}\n")


# ---------------------------------------------------------------- canonical output

def to_canonical(df: pd.DataFrame) -> pd.DataFrame:
    """Map normalized NSD columns to the standard deathcare output shape."""
    # D3: source_id is the constant SOURCE_ID; natural_key carries the per-row id.
    return build_canonical(
        df.index,
        source_id=SOURCE_ID,
        natural_key=df["PERMANENT_IDENTIFIER"],
        vertical="deathcare",
        account_type="cemetery",
        name_raw=df["NAME"],
        name_normalized=df["name_normalized"],
        address_line_1=df["ADDRESS"],
        city=df["CITY"],
        state=df["STATE"],
        zip5=df["zip5"],
        latitude=df["latitude"],
        longitude=df["longitude"],
        source_file=df["source_file"],
    )


# ---------------------------------------------------------------- entrypoint


def main() -> None:
    """CLI entrypoint — fetch USGS NSD cemetery data and optionally write to DB."""
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--out",
        default="usgs_nsd.csv",
        help="Output CSV path (default: usgs_nsd.csv)",
    )
    ap.add_argument(
        "--write-db",
        action="store_true",
        help="Also write results to Postgres staging (requires DATABASE_URL). "
             "Off by default — the CSV is always written regardless.",
    )
    ap.add_argument(
        "--state",
        nargs="+",
        default=None,
        metavar="STATE",
        help="Two-letter state codes to fetch (default: FL TX NC SC PA). "
             "Example: --state FL TX",
    )
    args = ap.parse_args()

    states = args.state or list(_DEFAULT_STATES)
    sys.stderr.write(
        f"  usgs_nsd: fetching {SOURCE_URL} for states={states}\n"
    )

    session = requests.Session()
    raw = fetch(session=session, states=states)
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
            f"  usgs_nsd: sha256={sha256_hex[:16]}…  bytes={byte_count:,}\n"
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
                license_string="USGS National Map — public domain",
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
                f"  usgs_nsd: wrote {len(canonical):,} rows "
                f"to staging.{SOURCE_ID} (source_run_id={source_run_id})\n"
            )
        except Exception as exc:
            if source_run_id is not None:
                finish_source_run(engine, source_run_id, status="failed")
            sys.exit(f"ERROR: DB write failed — {exc}")


if __name__ == "__main__":
    main()
