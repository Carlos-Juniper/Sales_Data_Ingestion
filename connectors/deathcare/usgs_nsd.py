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

import sys

import pandas as pd
import requests

from lib import arcgis
from lib.normalize import normalize_name, normalize_zip
from lib.schema import build_canonical
from lib.validate import assert_columns_present, assert_fill_rate, assert_min_rows

# ---------------------------------------------------------------- constants

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

        row = {col: props.get(col) for col in _OUT_FIELDS}
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
    return build_canonical(
        df.index,
        source_id="nsd:" + df["PERMANENT_IDENTIFIER"].fillna(""),
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
