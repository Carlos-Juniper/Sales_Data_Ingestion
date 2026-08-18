"""
FGDL / GeoPlan Florida Cemetery Facilities — ArcGIS FeatureServer connector.

Source  : https://services.arcgis.com/LBbVDC0hKPAnLRpO/arcgis/rest/services/gc_cemetery_dec24/FeatureServer/0
Provider: University of Florida GeoPlan Center (December 2024 update)
License : Florida Geographic Data Library — public-domain state GIS data.

Verified field names and record counts from live API on 2026-08-18:
  GCID      — integer, stable unique identifier (natural key, >99% populated)
  NAME      — cemetery name
  TYPE      — cemetery classification (22 distinct values)
  LAT_DD    — WGS84 latitude (pre-computed, reliable — preferred over geometry)
  LONG_DD   — WGS84 longitude (pre-computed, reliable — preferred over geometry)
  ZIPCODE   — Integer field; must be converted to zero-padded 5-digit string.
  ACRES     — parcel acreage (double, may be null)

Total records: 3,880 (all Florida).
Geometry is in WKID 3087 (Florida GDL Albers) — NOT used. LAT_DD/LONG_DD are
WGS84 and are the authoritative coordinate fields for this layer.
"""

from __future__ import annotations

import sys

import pandas as pd
import requests

from lib import arcgis
from lib.normalize import int_key, normalize_name
from lib.schema import build_canonical
from lib.validate import assert_columns_present, assert_fill_rate, assert_min_rows

# ---------------------------------------------------------------- constants

SOURCE_URL = (
    "https://services.arcgis.com/LBbVDC0hKPAnLRpO/arcgis/rest/services"
    "/gc_cemetery_dec24/FeatureServer/0"
)

_OUT_FIELDS = (
    "GCID,NAME,ADDRESS,CITY,ZIPCODE,COUNTY,TYPE,OWNER,"
    "OPERATING,LAT_DD,LONG_DD,ACRES,FLAG,OBJECTID"
)

_MIN_EXPECTED_ROWS = 3_000


# ---------------------------------------------------------------- extract

def fetch(session: requests.Session | None = None) -> pd.DataFrame:
    """
    Download all cemetery features from the FGDL GeoPlan FeatureServer layer.

    Geometry is not requested — LAT_DD and LONG_DD fields are pre-computed
    WGS84 coordinates that are more reliable than the projected geometry
    (WKID 3087, Florida GDL Albers) stored on each feature.

    Returns a raw DataFrame with one row per cemetery feature.
    """
    rows = []
    for feature in arcgis.iter_features(
        base_url=SOURCE_URL,
        where="1=1",
        out_fields=_OUT_FIELDS,
        order_by="OBJECTID",
        return_geometry=False,
        session=session,
    ):
        props = arcgis.feature_props(feature)
        row = {col: props.get(col) for col in _OUT_FIELDS.split(",")}
        row["source_file"] = SOURCE_URL
        rows.append(row)

    df = pd.DataFrame(rows)
    df["OBJECTID"] = pd.to_numeric(df.get("OBJECTID"), errors="coerce")
    return df


# ---------------------------------------------------------------- checks

def assert_source_shape(df: pd.DataFrame) -> None:
    """
    Raise ValueError if the fetched DataFrame does not match the known source shape.

    Guards against: API layout changes, truncated fetches, and accidental
    filtering that would cause silent data loss downstream.
    """
    assert_columns_present(df, ["GCID", "NAME", "LAT_DD", "LONG_DD", "TYPE"], label="FGDL cemeteries fetch")
    assert_fill_rate(
        df, "GCID", 0.99,
        label="GCID is the natural key — a low fill rate means the layer changed",
    )
    assert_min_rows(df, _MIN_EXPECTED_ROWS, label="FGDL cemeteries returned")


# ---------------------------------------------------------------- transform

def _infer_segment(type_val: str | None, operating_val: str | None) -> str | None:
    """
    Infer the business segment from TYPE and OPERATING fields.

    Returns 'religious', 'municipal', or None. None means the merge module
    will resolve the segment from other signals (EIN lookups, etc.).
    """
    type_upper = (type_val or "").upper()
    operating_upper = (operating_val or "").upper()

    if "RELIGIOUS" in type_upper:
        return "religious"
    if "MUNICIPAL" in type_upper or operating_upper == "PUBLIC":
        return "municipal"
    return None


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add derived columns used by to_canonical() and the deathcare merge module.

    ZIPCODE arrives as an integer from the ArcGIS API — it must be converted
    to a string and zero-padded to 5 digits before use as zip5.
    """
    df = df.copy()

    df["name_normalized"] = df["NAME"].map(normalize_name)

    # ZIPCODE is an integer field; convert and zero-pad before slicing.
    def _zip_from_int(val) -> str:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return ""
        return str(int(val)).zfill(5)[:5]

    df["zip5"] = df["ZIPCODE"].map(_zip_from_int)

    df["latitude"] = pd.to_numeric(df["LAT_DD"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["LONG_DD"], errors="coerce")

    df["segment"] = df.apply(
        lambda row: _infer_segment(row.get("TYPE"), row.get("OPERATING")),
        axis=1,
    )

    df["county_fips"] = None
    df["ein"] = None

    # ACRES flows through as size_value; treat null acres as no size data.
    acres = pd.to_numeric(df["ACRES"], errors="coerce")
    df["size_value"] = acres
    df["size_metric"] = acres.where(acres.notna()).map(lambda v: "acres" if pd.notna(v) else None)
    df["size_unit"] = df["size_metric"]

    return df


# ---------------------------------------------------------------- quality

def report_quality(df: pd.DataFrame) -> None:
    """Log data quality metrics to stderr."""
    total = len(df)
    sys.stderr.write(f"  fgdl_cemeteries: {total:,} total records\n")

    sys.stderr.write("  fgdl_cemeteries: TYPE breakdown (top 10)\n")
    type_counts = df["TYPE"].value_counts().head(10)
    for type_val, count in type_counts.items():
        sys.stderr.write(f"    {type_val:<40}  {count:>5,}\n")

    sys.stderr.write("  fgdl_cemeteries: OPERATING breakdown\n")
    op_counts = df["OPERATING"].value_counts()
    for op_val, count in op_counts.items():
        sys.stderr.write(f"    {op_val:<20}  {count:>5,}\n")

    verified_pct = (df["FLAG"] == "V").mean()
    acres_pct = df["ACRES"].notna().mean()
    addr_pct = df["ADDRESS"].notna().mean()

    sys.stderr.write(f"  fgdl_cemeteries: verified (FLAG=='V')   {verified_pct:.1%}\n")
    sys.stderr.write(f"  fgdl_cemeteries: non-null ACRES          {acres_pct:.1%}\n")
    sys.stderr.write(f"  fgdl_cemeteries: non-null ADDRESS        {addr_pct:.1%}\n")


# ---------------------------------------------------------------- canonical output

def to_canonical(df: pd.DataFrame) -> pd.DataFrame:
    """Map normalized FGDL columns to the standard deathcare output shape."""
    gcid_str = df["GCID"].map(int_key)
    return build_canonical(
        df.index,
        source_id="fgdl:" + gcid_str,
        natural_key=gcid_str,
        vertical="deathcare",
        account_type="cemetery",
        name_raw=df["NAME"],
        name_normalized=df["name_normalized"],
        address_line_1=df.get("ADDRESS"),
        city=df.get("CITY"),
        state="FL",
        zip5=df["zip5"],
        latitude=df["latitude"],
        longitude=df["longitude"],
        segment=df["segment"],
        size_metric=df["size_metric"],
        size_value=df["size_value"],
        size_unit=df["size_unit"],
        source_file=df["source_file"],
    )
