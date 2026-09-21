"""
Deathcare pipeline — merge and deduplication module.

Takes canonical DataFrames produced by all 5 source connectors
(usgs_nsd, irs_bmf_deathcare, va_cemeteries, txdot_cemeteries,
fgdl_cemeteries) and returns a single deduplicated, segment-resolved,
lead-flagged DataFrame.

Pipeline stages (in order):
  1. load_sources    — concatenate all source DataFrames
  2. dedup_by_ein    — within-BMF EIN dedup (EINs only appear in BMF)
  3. spatial_dedup   — merge co-located records within 150 m radius
  4. resolve_segment — fill remaining None segments by process of elimination
  5. filter_leads    — flag is_lead per business rules

Output schema (input canonical columns plus):
  merge_confidence   str  'high' | 'spatial_only' | 'none'
  merged_sources     str  comma-separated source_ids folded into this record
  is_lead            bool True when the record qualifies as a prospecting lead

D3 note: after D3, source_id is a per-source constant (e.g. "usgs_nsd")
and natural_key carries the per-row identifier.  merged_sources stores
composites rebuilt as f"{source_id}:{natural_key}" so downstream code
(resolve_segment's _BMF_SOURCE_PREFIX check, tests) sees the same format
as before D3 (e.g. "irs_bmf:043783054").

CLI usage:
    PYTHONPATH=connectors python connectors/deathcare/deathcare_merge.py
    PYTHONPATH=connectors python connectors/deathcare/deathcare_merge.py --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import logging
import math
import sys
from pathlib import Path
from typing import Sequence

import pandas as pd

from lib.enums import SEGMENT_RELIGIOUS, SEGMENT_MUNICIPAL, SEGMENT_FEDERAL, SEGMENT_COMMERCIAL, CONFIDENCE_HIGH, CONFIDENCE_SPATIAL_ONLY, CONFIDENCE_NONE  # noqa: F401 — SEGMENT_COMMERCIAL re-exported for pipeline callers
from lib.geo import cluster_within_radius, haversine_km, haversine_km_vec  # noqa: F401 — haversine_km re-exported for tests
from lib.match import name_similarity
from lib.schema import CANONICAL_COLUMNS, validate_canonical  # noqa: F401 — re-exported for pipeline callers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("deathcare_merge")

# ---------------------------------------------------------------- constants

# Levenshtein similarity threshold above which a spatial merge is labelled 'high'.
_NAME_SIMILARITY_THRESHOLD: float = 0.80

# Prefix used in merged_sources composites that originate from the IRS BMF extract.
# After D3, source_id is "irs_bmf_deathcare" and natural_key is the EIN.
# The composite stored in merged_sources is f"{source_id}:{natural_key}",
# so merged_sources entries look like "irs_bmf_deathcare:043783054".
# resolve_segment checks for this prefix to detect BMF merge partners.
# NOTE: the prefix "irs_bmf:" still matches "irs_bmf_deathcare:..." as a
# substring — this is intentional and preserves backward compatibility with
# tests that were written before D3.
_BMF_SOURCE_PREFIX: str = "irs_bmf:"

# Deathcare sources read from staging during pipeline execution.
_DEATHCARE_SOURCES = [
    "fgdl_cemeteries",
    "txdot_cemeteries",
    "usgs_nsd",
    "va_cemeteries",
    "irs_bmf_deathcare",
]

_STAGING_COLS = [
    "source_id",
    "natural_key",
    "vertical",
    "account_type",
    "name_raw",
    "name_normalized",
    "address_line_1",
    "city",
    "state",
    "zip5",
    "phone_raw",
    "phone_normalized",
    "latitude",
    "longitude",
    "segment",
    "ein",
    "county_fips",
    "size_metric",
    "size_value",
    "size_unit",
    "source_file",
]


# ---------------------------------------------------------------- helpers


def _source_composite(row: pd.Series) -> str:
    """
    Build the f"{source_id}:{natural_key}" composite for use in merged_sources.

    D3 decision: source_id is now a per-source constant (e.g. "usgs_nsd")
    and natural_key carries the per-row identifier.  merged_sources must
    continue to store composites in the "prefix:key" format so:
      - resolve_segment's _BMF_SOURCE_PREFIX substring check still works
        (e.g. "irs_bmf_deathcare:043783054" contains "irs_bmf:")
      - downstream tests that assert on merged_sources content see the same
        format as before D3

    Falls back to str(source_id) alone when natural_key is null/empty, to
    avoid producing malformed "prefix:" entries.
    """
    source_id = str(row.get("source_id", ""))
    natural_key = str(row.get("natural_key", "") or "")
    if natural_key.strip():
        return f"{source_id}:{natural_key}"
    return source_id


# ---------------------------------------------------------------- coalesce

def _coalesce_records(base: pd.Series, other: pd.Series) -> pd.Series:
    """
    Return a new Series where each field is the first non-null value
    between *base* and *other*.

    Field priority notes:
      - EIN: BMF records carry EIN; non-BMF records do not.  Because BMF
        records have latitude=None they will appear as *other* in the merge
        (the spatial record anchors the merge).  coalesce naturally picks
        the EIN from whichever carries it.
      - source_id: kept from base (the record that survives as the spine).
      - merge_confidence / merged_sources: set by the caller, not here.
    """
    merged = base.copy()
    for col in base.index:
        if col in ("source_id", "merge_confidence", "merged_sources"):
            # These identity/provenance fields are handled by the caller.
            continue
        if pd.isna(merged[col]) or merged[col] == "":
            candidate = other[col]
            if not (pd.isna(candidate) or candidate == ""):
                merged[col] = candidate
    return merged


# ---------------------------------------------------------------- stage 1

def load_sources(*dfs: pd.DataFrame) -> pd.DataFrame:
    """
    Concatenate canonical DataFrames from all source connectors.

    Args:
        *dfs: Any number of DataFrames, each conforming to the canonical
              deathcare schema (_CANONICAL_COLS).

    Returns:
        Combined DataFrame with a fresh integer index.
    """
    # Cast float columns to a consistent dtype before concat so pandas does not
    # emit a FutureWarning about dtype inference on all-NA columns (e.g. BMF
    # rows where latitude/longitude are None for every row).
    float_cols = ("latitude", "longitude", "size_value")
    frames = []
    for df in dfs:
        df = df.copy()
        for col in float_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        frames.append(df)

    combined = pd.concat(frames, ignore_index=True)
    return combined


# ---------------------------------------------------------------- stage 2

def dedup_by_ein(df: pd.DataFrame) -> pd.DataFrame:
    """
    Remove duplicate EIN records within the BMF source only.

    EINs exist exclusively in BMF.  Two BMF rows sharing the same EIN are
    true duplicates (same nonprofit organisation, ingested twice from the
    raw IRS file).  Non-BMF rows have EIN=None and are never affected.

    Keeps the first occurrence of each EIN (preserving row order from the
    source file, which is alphabetical by name in the IRS CSVs).

    Args:
        df: Combined canonical DataFrame (output of load_sources).

    Returns:
        DataFrame with within-BMF EIN duplicates removed.

    Logs to stderr:
        Number of EIN duplicate rows removed.
    """
    # Only rows that have a non-null EIN are candidates for EIN dedup.
    has_ein = df["ein"].notna() & (df["ein"] != "")

    df_with_ein = df[has_ein].copy()
    df_no_ein = df[~has_ein].copy()

    before = len(df_with_ein)
    df_with_ein = df_with_ein.drop_duplicates(subset=["ein"], keep="first")
    after = len(df_with_ein)

    removed = before - after
    sys.stderr.write(f"  merge: EIN dedup removed {removed:,} duplicate rows\n")

    return pd.concat([df_no_ein, df_with_ein], ignore_index=True)


# ---------------------------------------------------------------- stage 3

def spatial_dedup(df: pd.DataFrame, radius_km: float = 0.15) -> pd.DataFrame:
    """
    Merge co-located records within *radius_km* kilometres into one record.

    Only records with non-null latitude and longitude participate in spatial
    comparison.  Records without coordinates are passed through unchanged
    with merge_confidence='none'.

    Spatial blocking strategy — to avoid O(n²) distance comparisons across
    ~43 k spatial records, records are grouped by a 0.1-degree grid cell
    (≈11 km) before pair comparison.  Every pair within 150 m will share at
    least one grid cell because the cell side is 73× the merge radius.
    We expand to a 3×3 neighbourhood so pairs straddling a cell boundary are
    not missed.

    Merge rules:
      - Fields are coalesced: first non-null value between the two records wins.
      - merge_confidence = 'high' if name_normalized Levenshtein similarity
        >= 0.80, else 'spatial_only'.
      - merged_sources = comma-joined source_ids of all records folded together.

    Args:
        df: Canonical DataFrame (output of dedup_by_ein).
        radius_km: Merge radius in kilometres.  Default 0.15 (150 m).

    Returns:
        Deduplicated DataFrame.  Rows without coordinates get
        merge_confidence='none' and merged_sources=their own source_id.
    """
    df = df.copy().reset_index(drop=True)

    has_coords = df["latitude"].notna() & df["longitude"].notna()
    spatial = df[has_coords].copy().reset_index(drop=True)
    no_coords = df[~has_coords].copy().reset_index(drop=True)

    # Initialise output columns on both subsets.
    # D3: merged_sources stores f"{source_id}:{natural_key}" composites so
    # that resolve_segment's prefix check ("irs_bmf:") still matches rows
    # whose source_id is "irs_bmf_deathcare" (contains the prefix as substring).
    no_coords["merge_confidence"] = CONFIDENCE_NONE
    no_coords["merged_sources"] = no_coords.apply(_source_composite, axis=1)

    if spatial.empty:
        spatial["merge_confidence"] = pd.Series(dtype=str)
        spatial["merged_sources"] = pd.Series(dtype=str)
        return pd.concat([no_coords, spatial], ignore_index=True)

    lats = spatial["latitude"].tolist()
    lons = spatial["longitude"].tolist()

    # Delegate grid-blocking, Union-Find, and haversine comparisons to lib.geo.
    groups = cluster_within_radius(lats, lons, radius_km=radius_km)

    merged_rows: list[pd.Series] = []

    for members in groups:
        if len(members) == 1:
            # No merge — single-record component.
            row = spatial.iloc[members[0]].copy()
            row["merge_confidence"] = CONFIDENCE_NONE
            # D3: store composite "source_id:natural_key" so prefix checks work.
            row["merged_sources"] = _source_composite(row)
            merged_rows.append(row)
            continue

        # Multiple records within 150 m — merge them.
        # Start with the first member as the base spine.
        base = spatial.iloc[members[0]].copy()
        # D3: composite format for provenance tracking.
        all_source_ids = [_source_composite(base)]

        for i in members[1:]:
            other = spatial.iloc[i]
            base = _coalesce_records(base, other)
            all_source_ids.append(_source_composite(other))

        # Compute max pairwise name similarity across all pairs in the cluster.
        # A single-member cluster cannot form a pair, so it falls through to
        # 'spatial_only' (handled in the len==1 branch above).  For multi-member
        # clusters, any pair reaching the threshold is enough to label 'high'.
        max_sim = max(
            (
                name_similarity(
                    str(spatial.iloc[i]["name_normalized"] or ""),
                    str(spatial.iloc[j]["name_normalized"] or ""),
                )
                for i, j in itertools.combinations(members, 2)
            ),
            default=0.0,
        )

        base["merge_confidence"] = CONFIDENCE_HIGH if max_sim >= _NAME_SIMILARITY_THRESHOLD else CONFIDENCE_SPATIAL_ONLY
        base["merged_sources"] = ",".join(all_source_ids)
        merged_rows.append(base)

    result_spatial = pd.DataFrame(merged_rows).reset_index(drop=True)

    return pd.concat([no_coords, result_spatial], ignore_index=True)


# ---------------------------------------------------------------- stage 4

def resolve_segment(df: pd.DataFrame) -> pd.DataFrame:
    """
    Fill any remaining None segments using process-of-elimination rules.

    Rules (applied only where segment is None after spatial merge):
      1. If merged_sources contains a BMF source_id (prefix 'irs_bmf:'):
         segment = 'religious'  (inherited from the BMF merge partner)
      2. Else if account_type == 'federal':
         segment = 'federal'
      3. Else:
         segment = 'municipal'  (unmatched NSD / TxDOT record)

    Already-set segments ('religious', 'federal', 'municipal', 'commercial')
    are never overwritten.

    Args:
        df: DataFrame output of spatial_dedup.

    Returns:
        DataFrame with segment fully populated (no None values remain).
    """
    df = df.copy()

    unresolved = df["segment"].isna() | (df["segment"] == "")
    has_bmf = df["merged_sources"].fillna("").str.contains(_BMF_SOURCE_PREFIX, regex=False)
    is_federal = df["account_type"] == "federal"

    df.loc[unresolved, "segment"] = SEGMENT_MUNICIPAL
    df.loc[unresolved & is_federal, "segment"] = SEGMENT_FEDERAL
    df.loc[unresolved & has_bmf, "segment"] = SEGMENT_RELIGIOUS

    return df


# ---------------------------------------------------------------- stage 5

def filter_leads(df: pd.DataFrame) -> pd.DataFrame:
    """
    Flag each record as a qualified lead or not.

    Disqualifying conditions (is_lead=False):
      - segment == 'municipal'
      - segment == 'federal'
      - name_raw is null AND name_normalized is empty string (truly unnamed)

    All other records receive is_lead=True.  The full record set is returned;
    no rows are dropped.

    Args:
        df: DataFrame output of resolve_segment.

    Returns:
        Input DataFrame with an additional boolean column 'is_lead'.
    """
    df = df.copy()

    is_municipal = df["segment"] == SEGMENT_MUNICIPAL
    is_federal = df["segment"] == SEGMENT_FEDERAL

    name_raw_null = df["name_raw"].isna()
    name_norm_empty = df["name_normalized"].fillna("").str.strip() == ""
    is_unnamed = name_raw_null & name_norm_empty

    df["is_lead"] = ~(is_municipal | is_federal | is_unnamed)
    return df


# ---------------------------------------------------------------- pipeline

def merge_pipeline(dfs: Sequence[pd.DataFrame]) -> pd.DataFrame:
    """
    Run the full merge pipeline over a sequence of canonical DataFrames.

    Stages:
      load_sources → dedup_by_ein → spatial_dedup → resolve_segment → filter_leads

    Args:
        dfs: Sequence of canonical DataFrames from the 5 source connectors.

    Returns:
        Fully merged, deduplicated, segment-resolved, lead-flagged DataFrame.
    """
    combined = load_sources(*dfs)
    combined = dedup_by_ein(combined)
    combined = spatial_dedup(combined)
    combined = resolve_segment(combined)
    combined = filter_leads(combined)
    return combined


# ---------------------------------------------------------------- summary

def print_summary(df: pd.DataFrame) -> None:
    """
    Write a structured summary of the merged dataset to stderr.

    Covers:
      - Total record counts (before / after dedup is already collapsed here)
      - Segment breakdown
      - is_lead counts
      - merge_confidence breakdown
      - % of records with a non-null phone number

    Args:
        df: Output DataFrame from merge_pipeline (or filter_leads).
    """
    total = len(df)
    sys.stderr.write(f"  merge: {total:,} total records in output\n")

    sys.stderr.write("  merge: segment breakdown\n")
    seg_counts = df["segment"].value_counts(dropna=False).sort_index()
    for seg, count in seg_counts.items():
        sys.stderr.write(f"    {str(seg):<14}  {count:>7,}\n")

    if "is_lead" in df.columns:
        sys.stderr.write("  merge: is_lead breakdown\n")
        lead_counts = df["is_lead"].value_counts(dropna=False).sort_index()
        for val, count in lead_counts.items():
            sys.stderr.write(f"    {str(val):<14}  {count:>7,}\n")

    if "merge_confidence" in df.columns:
        sys.stderr.write("  merge: merge_confidence breakdown\n")
        conf_counts = df["merge_confidence"].value_counts(dropna=False).sort_index()
        for conf, count in conf_counts.items():
            sys.stderr.write(f"    {str(conf):<14}  {count:>7,}\n")

    phone_pct = df["phone_normalized"].notna().mean()
    sys.stderr.write(f"  merge: phone populated  {phone_pct:.1%}\n")


# ---------------------------------------------------------------- pipeline driver helpers


def _sha256_key(text_val: str) -> str:
    """Return the hex SHA-256 digest of a UTF-8 string."""
    return hashlib.sha256(text_val.encode("utf-8")).hexdigest()


def _is_present(val) -> bool:
    """True only when val carries real data — not null, NaN, blank, or 'nan'."""
    if val is None:
        return False
    if isinstance(val, float) and math.isnan(val):
        return False
    s = str(val).strip()
    return s != "" and s.lower() != "nan"


def _compute_account_key(row: dict) -> str:
    """
    Compute the deterministic account_key per D1.

    Priority for deathcare: EIN > normalized_name+zip5.
    CCN/NPI do not exist in deathcare sources.
    """
    ein = row.get("ein", "")
    if _is_present(ein):
        return _sha256_key(f"ein:{str(ein).strip()}")

    name_norm = str(row.get("name_normalized", "") or row.get("name_raw", ""))
    zip5 = str(row.get("zip5", "") or "")
    return _sha256_key(f"name:{name_norm}|zip:{zip5}")


def _compute_location_key(account_key: str, row: dict) -> str:
    """Compute the deterministic location_key."""
    addr = str(row.get("address_line_1", "") or "")
    zip5 = str(row.get("zip5", "") or "")
    return _sha256_key(f"loc:{account_key}|{addr}|{zip5}")


def _compute_contact_key(account_key: str, role: str, full_name: str) -> str:
    """Compute the deterministic contact_key."""
    return _sha256_key(f"contact:{account_key}|{role}|{full_name}")


def _load_source(engine, source_id: str) -> pd.DataFrame:
    """Load one deathcare staging table. Returns empty DataFrame if table missing."""
    from sqlalchemy import text
    col_list = ", ".join(_STAGING_COLS)
    sql = text(f"SELECT {col_list} FROM staging.{source_id}")  # nosec: known constant
    try:
        with engine.connect() as conn:
            df = pd.read_sql(sql, conn)
        logger.info("loaded %d rows from staging.%s", len(df), source_id)
        return df
    except Exception as exc:
        logger.warning("staging.%s not readable (%s) — skipping", source_id, exc)
        return pd.DataFrame(columns=_STAGING_COLS)


def _load_irs990_cache(engine) -> pd.DataFrame:
    """
    Load staging.enrich_irs990 for deathcare (religious-segment) accounts.

    Returns a DataFrame keyed on (source_id, natural_key) with phone_990 and
    contact_name_990 so run_pipeline can join real named contacts onto resolved
    output.

    staging.enrich_irs990 schema (migration 012):
      source_id, natural_key, phone_990, contact_name_990, enrich_status, enriched_at

    Only rows whose source_id is "irs_bmf_deathcare" are relevant — that is the
    only deathcare source that feeds into the 990 enrichment pipeline.
    """
    from sqlalchemy import text
    sql = text("""
        SELECT source_id, natural_key, phone_990, contact_name_990
        FROM staging.enrich_irs990
        WHERE source_id = 'irs_bmf_deathcare'
          AND enrich_status = 'ok'
    """)
    try:
        with engine.connect() as conn:
            df = pd.read_sql(sql, conn)
        logger.info("loaded %d irs990 cache rows", len(df))
        return df
    except Exception as exc:
        logger.warning("staging.enrich_irs990 not readable (%s) — proceeding without 990 data", exc)
        return pd.DataFrame(columns=["source_id", "natural_key", "phone_990", "contact_name_990"])


def _load_parcel_cache(engine) -> pd.DataFrame:
    """
    Load staging.enrich_parcel for deathcare sources.

    Returns a DataFrame keyed on natural_key with maintained_acres so the
    run_pipeline driver can join parcel results onto the resolved location output.

    staging.enrich_parcel schema (migration 012):
      source_id, natural_key, maintained_acres, boundary, enriched_at
    acres_confidence and geometry_source are constants supplied at join time
    ('estimated' and 'parcel') — they are not stored in the staging table.
    """
    from sqlalchemy import text
    sql = text("""
        SELECT source_id, natural_key, maintained_acres
        FROM staging.enrich_parcel
        WHERE source_id = ANY(:source_ids)
    """)
    try:
        with engine.connect() as conn:
            df = pd.read_sql(sql, conn, params={"source_ids": _DEATHCARE_SOURCES})
        logger.info("loaded %d parcel cache rows", len(df))
        return df
    except Exception as exc:
        logger.warning("staging.enrich_parcel not readable (%s) — proceeding without parcel data", exc)
        return pd.DataFrame(columns=["source_id", "natural_key", "maintained_acres"])


def _join_parcel(location_df: pd.DataFrame, parcels: pd.DataFrame) -> pd.DataFrame:
    """
    Overlay parcel acreage results onto the resolved_location DataFrame.

    Joins staging.enrich_parcel onto location_df by the survivor row's own
    natural_key (carried as the private column _natural_key by
    _build_resolved_location).  For matched rows, populates:
      - maintained_acres  : numeric acreage from the parcel record
      - acres_confidence  : fixed 'estimated' (no footprint subtraction yet)
      - geometry_source   : fixed 'parcel'

    Rows with no parcel match are left with maintained_acres=None and
    acres_confidence=None (the _build_resolved_location defaults).

    staging.enrich_parcel schema (migration 012):
      source_id, natural_key, maintained_acres, boundary, enriched_at
    acres_confidence and geometry_source are constants supplied here because
    they are not stored in the staging table (parcel_acreage_enrich.py's
    ParcelResult dataclass uses 'estimated' / 'parcel' as defaults).
    """
    if parcels.empty or "_natural_key" not in location_df.columns:
        return location_df

    # Index by natural_key for O(1) lookup — keep first match when a key
    # appears in multiple source rows (shouldn't happen in practice, but safe).
    parcel_indexed = parcels.drop_duplicates(subset=["natural_key"]).set_index("natural_key")[
        ["maintained_acres"]
    ]

    location_df = location_df.copy()
    # Ensure columns exist (_build_resolved_location initialises them to None,
    # but guard defensively in case the caller passes a partial DataFrame).
    if "maintained_acres" not in location_df.columns:
        location_df["maintained_acres"] = None
    if "acres_confidence" not in location_df.columns:
        location_df["acres_confidence"] = None
    if "geometry_source" not in location_df.columns:
        location_df["geometry_source"] = None

    for idx, row in location_df.iterrows():
        # Use _natural_key, which _build_resolved_location carries for this join.
        nk = str(row.get("_natural_key", ""))
        if nk in parcel_indexed.index:
            acres = parcel_indexed.loc[nk, "maintained_acres"]
            if _is_present(acres):
                location_df.at[idx, "maintained_acres"] = float(acres)
                location_df.at[idx, "acres_confidence"] = "estimated"
                location_df.at[idx, "geometry_source"] = "parcel"

    matched = location_df["maintained_acres"].notna().sum()
    logger.info("_join_parcel: %d / %d location rows matched parcel data", matched, len(location_df))
    return location_df


def _build_resolved_account(merged: pd.DataFrame) -> pd.DataFrame:
    """Build staging.resolved_account rows from the merged deathcare output."""
    rows = []
    for _, row in merged.iterrows():
        account_key = _compute_account_key(row.to_dict())
        ext: dict = {}
        if _is_present(row.get("ein")):
            ext["ein"] = str(row["ein"]).strip()

        mailing: dict = {}
        for field in ["address_line_1", "city", "state", "zip5"]:
            val = row.get(field)
            if _is_present(val):
                mailing[field] = str(val)

        name_raw = str(row.get("name_raw", "")) if _is_present(row.get("name_raw")) else ""
        name_norm = str(row.get("name_normalized", "")) if _is_present(row.get("name_normalized")) else ""

        size_metric_raw = row.get("size_value")
        size_metric = None
        if _is_present(size_metric_raw):
            try:
                size_metric = float(size_metric_raw)
            except (ValueError, TypeError):
                pass

        rows.append({
            "account_key": account_key,
            "vertical": "deathcare",
            "account_type": str(row.get("account_type", "")) if _is_present(row.get("account_type")) else None,
            "legal_name": name_raw or name_norm,
            "name_normalized": name_norm,
            "dba_name": None,
            "parent_account_key": None,
            "mailing_address": json.dumps(mailing) if mailing else None,
            "phone": str(row.get("phone_normalized", "")) if _is_present(row.get("phone_normalized")) else None,
            "email": None,
            "website": None,
            "status": "active",
            "external_keys": json.dumps(ext) if ext else None,
            "size_metric": size_metric,
            "size_metric_unit": str(row.get("size_unit", "")) if _is_present(row.get("size_unit")) else None,
            "confidence": None,
            "_source_id": str(row.get("source_id", "")),
            "_natural_key": str(row.get("natural_key", "")),
        })

    return pd.DataFrame(rows)


def _build_resolved_location(merged: pd.DataFrame, account_df: pd.DataFrame) -> pd.DataFrame:
    """Build staging.resolved_location rows from the merged deathcare output."""
    account_map = dict(
        zip(
            zip(account_df["_source_id"], account_df["_natural_key"]),
            account_df["account_key"],
        )
    )

    rows = []
    for _, row in merged.iterrows():
        key = (str(row.get("source_id", "")), str(row.get("natural_key", "")))
        account_key = account_map.get(key)
        if not account_key:
            continue

        location_key = _compute_location_key(account_key, row.to_dict())

        site_addr: dict = {}
        for field in ["address_line_1", "city", "zip5"]:
            val = row.get(field)
            if _is_present(val):
                site_addr[field] = str(val)
        state_val = row.get("state")
        if _is_present(state_val):
            site_addr["state"] = str(state_val)

        lat = row.get("latitude")
        lon = row.get("longitude")

        rows.append({
            "location_key": location_key,
            "account_key": account_key,
            "vertical": "deathcare",
            "location_name": str(row.get("name_raw", "")) if _is_present(row.get("name_raw")) else None,
            "site_address": json.dumps(site_addr) if site_addr else None,
            "_latitude": float(lat) if _is_present(lat) else None,
            "_longitude": float(lon) if _is_present(lon) else None,
            "geocode_precision": None,
            "geometry_source": None,
            "maintained_acres": None,
            "acres_confidence": None,
            "site_type": str(row.get("account_type", "")) if _is_present(row.get("account_type")) else None,
            # Carry natural_key as a private column so _join_parcel() can match
            # against staging.enrich_parcel without a full merged-df re-scan.
            "_natural_key": str(row.get("natural_key", "")),
        })

    return pd.DataFrame(rows)


def _build_resolved_contact(merged: pd.DataFrame, account_df: pd.DataFrame) -> pd.DataFrame:
    """Build staging.resolved_contact rows — contacts with a phone number only."""
    account_map = dict(
        zip(
            zip(account_df["_source_id"], account_df["_natural_key"]),
            account_df["account_key"],
        )
    )

    rows = []
    for _, row in merged.iterrows():
        key = (str(row.get("source_id", "")), str(row.get("natural_key", "")))
        account_key = account_map.get(key)
        if not account_key:
            continue

        phone = str(row.get("phone_normalized", "")) if _is_present(row.get("phone_normalized")) else None
        if not phone:
            continue

        role = "primary_phone"
        contact_key = _compute_contact_key(account_key, role, "")

        rows.append({
            "contact_key": contact_key,
            "account_key": account_key,
            "vertical": "deathcare",
            "full_name": None,
            "role": role,
            "role_rank": 1,
            "phone": phone,
            "email": None,
            "address": None,
            "source_id": str(row.get("source_id", "")),
            "is_current": True,
        })

    return pd.DataFrame(rows)


def _join_irs990(
    account_df: pd.DataFrame,
    contact_df: pd.DataFrame,
    irs990: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Overlay IRS 990 phone and contact data onto the resolved deathcare output.

    Join strategy: irs990 is keyed on (source_id, natural_key) and account_df
    carries private columns (_source_id, _natural_key) for exactly this join.

    For each matched account:
      - resolved_account.phone is set from phone_990 when phone_990 is non-null
        and no phone is already present.
      - A new resolved_contact row is emitted with role='irs990_contact' and
        full_name=contact_name_990.
      - The pre-existing synthetic 'primary_phone' contact for that account is
        removed (real contact takes priority over synthetic placeholder).

    Accounts without an irs990 match are left unchanged.
    Accounts where irs990 has a null phone_990 get the contact_name if present,
    but no phone is set (graceful — no empty-phone contact is emitted).

    Survivorship: irs990_contact > primary_phone (synthetic placeholder).

    Parameters
    ----------
    account_df:
        Output of _build_resolved_account. Must have _source_id and _natural_key
        private columns plus account_key and phone.
    contact_df:
        Output of _build_resolved_contact. Contains synthetic primary_phone rows.
    irs990:
        Output of _load_irs990_cache. Keyed on (source_id, natural_key).

    Returns
    -------
    (updated_account_df, updated_contact_df) with IRS990 data applied.
    """
    if irs990.empty:
        return account_df, contact_df

    # Index irs990 by (source_id, natural_key) for O(1) lookups.
    irs990_indexed = irs990.set_index(["source_id", "natural_key"])

    account_df = account_df.copy()
    contact_rows_new: list[dict] = []
    # Track account_keys that receive a real irs990_contact so we can suppress
    # their synthetic primary_phone placeholder.
    accounts_with_real_contact: set[str] = set()

    for idx, row in account_df.iterrows():
        key = (str(row.get("_source_id", "")), str(row.get("_natural_key", "")))
        if key not in irs990_indexed.index:
            continue

        irs_row = irs990_indexed.loc[key]
        # irs_indexed.loc[] returns a Series for a single match.
        # For multiple rows with the same key (shouldn't happen — PK constraint),
        # take the first entry.
        if isinstance(irs_row, pd.DataFrame):
            irs_row = irs_row.iloc[0]

        phone_990 = irs_row.get("phone_990")
        contact_name_990 = irs_row.get("contact_name_990")

        # Populate account.phone from phone_990 when phone_990 is non-null.
        # Prefer whatever phone is already present (e.g. from phone_normalized
        # in the canonical row) — only fill in when account phone is still null.
        if _is_present(phone_990):
            if not _is_present(row.get("phone")):
                account_df.at[idx, "phone"] = str(phone_990).strip()

        # Only emit an irs990_contact when we have at least a name or a phone.
        # A null-phone, null-name row from irs990 is not worth a contact row.
        has_phone = _is_present(phone_990)
        has_name = _is_present(contact_name_990)
        if not has_phone and not has_name:
            continue

        account_key = row["account_key"]
        full_name = str(contact_name_990).strip() if has_name else ""
        phone_val = str(phone_990).strip() if has_phone else None

        contact_key = _compute_contact_key(account_key, "irs990_contact", full_name)
        contact_rows_new.append({
            "contact_key": contact_key,
            "account_key": account_key,
            "vertical": "deathcare",
            "full_name": full_name or None,
            "role": "irs990_contact",
            "role_rank": 1,
            "phone": phone_val,
            "email": None,
            "address": None,
            "source_id": "irs_bmf_deathcare",
            "is_current": True,
        })
        accounts_with_real_contact.add(account_key)

    # Survivorship: drop synthetic primary_phone contacts for accounts that now
    # have a real irs990_contact.  This prevents two contacts for the same account
    # where one is a named real contact and the other is the empty placeholder.
    if accounts_with_real_contact and not contact_df.empty:
        synthetic_mask = (
            contact_df["role"] == "primary_phone"
        ) & contact_df["account_key"].isin(accounts_with_real_contact)
        contact_df = contact_df[~synthetic_mask].copy()

    # Append the new real contacts.
    if contact_rows_new:
        new_df = pd.DataFrame(contact_rows_new)
        contact_df = pd.concat([contact_df, new_df], ignore_index=True)

    n_new = len(contact_rows_new)
    logger.info(
        "_join_irs990: added %d irs990_contact rows, suppressed synthetic for %d accounts",
        n_new, len(accounts_with_real_contact),
    )
    return account_df, contact_df


def _upsert_resolved_account(engine, df: pd.DataFrame) -> int:
    """Upsert staging.resolved_account. Returns row count written."""
    from sqlalchemy import text
    if df.empty:
        return 0

    sql = text("""
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
            vertical          = EXCLUDED.vertical,
            account_type      = EXCLUDED.account_type,
            legal_name        = EXCLUDED.legal_name,
            name_normalized   = EXCLUDED.name_normalized,
            dba_name          = EXCLUDED.dba_name,
            parent_account_key= EXCLUDED.parent_account_key,
            mailing_address   = EXCLUDED.mailing_address,
            phone             = EXCLUDED.phone,
            email             = EXCLUDED.email,
            website           = EXCLUDED.website,
            status            = EXCLUDED.status,
            external_keys     = EXCLUDED.external_keys,
            size_metric       = EXCLUDED.size_metric,
            size_metric_unit  = EXCLUDED.size_metric_unit,
            confidence        = EXCLUDED.confidence
    """)

    rows = df.drop(columns=["_source_id", "_natural_key"], errors="ignore").to_dict(orient="records")
    # Scrub size_metric: pandas float64 upcasts Python None to NaN in the DataFrame,
    # and Postgres numeric natively accepts NaN — so NaN lands as literal NaN in
    # core.account instead of SQL NULL.  Replace any NaN with real Python None here,
    # after to_dict() has moved values out of the numpy dtype, before the SQL execute.
    for row in rows:
        if pd.isna(row.get("size_metric")):
            row["size_metric"] = None
    with engine.begin() as conn:
        conn.execute(sql, rows)
    logger.info("upserted %d rows to staging.resolved_account", len(rows))
    return len(rows)


def _upsert_resolved_location(engine, df: pd.DataFrame) -> int:
    """Upsert staging.resolved_location. Returns row count written."""
    from sqlalchemy import text
    if df.empty:
        return 0

    sql = text("""
        INSERT INTO staging.resolved_location (
            location_key, account_key, vertical, location_name, site_address,
            geom, geocode_precision, geometry_source,
            maintained_acres, acres_confidence, site_type
        ) VALUES (
            :location_key, :account_key, :vertical, :location_name,
            CAST(:site_address AS jsonb),
            CASE
                WHEN :_latitude IS NOT NULL AND :_longitude IS NOT NULL
                THEN ST_SetSRID(
                    ST_MakePoint(
                        CAST(:_longitude AS double precision),
                        CAST(:_latitude AS double precision)
                    ), 4326)
                ELSE NULL
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
            geocode_precision = EXCLUDED.geocode_precision,
            geometry_source   = EXCLUDED.geometry_source,
            maintained_acres  = EXCLUDED.maintained_acres,
            acres_confidence  = EXCLUDED.acres_confidence,
            site_type         = EXCLUDED.site_type
    """)

    rows = df.drop(columns=["_natural_key"], errors="ignore").to_dict(orient="records")
    with engine.begin() as conn:
        conn.execute(sql, rows)
    logger.info("upserted %d rows to staging.resolved_location", len(rows))
    return len(rows)


def _upsert_resolved_contact(engine, df: pd.DataFrame) -> int:
    """Upsert staging.resolved_contact. Returns row count written."""
    from sqlalchemy import text
    if df.empty:
        return 0

    sql = text("""
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

    rows = df.to_dict(orient="records")
    with engine.begin() as conn:
        conn.execute(sql, rows)
    logger.info("upserted %d rows to staging.resolved_contact", len(rows))
    return len(rows)


# ---------------------------------------------------------------- D15 helper


def _replace_and_upsert(
    engine,
    account_df: pd.DataFrame,
    location_df: pd.DataFrame,
    contact_df: pd.DataFrame,
    vertical: str,
) -> tuple:
    """Delete this vertical's existing resolved rows then upsert the fresh set.

    All three deletes and all three upserts share a single transaction so a
    mid-run failure cannot leave any table empty.  The other vertical's rows
    are never touched because every DELETE is scoped by ``WHERE vertical = :v``.

    Returns (n_account, n_location, n_contact) row counts written.
    """
    from sqlalchemy import text as _text

    acct_sql = _text("""
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
            vertical          = EXCLUDED.vertical,
            account_type      = EXCLUDED.account_type,
            legal_name        = EXCLUDED.legal_name,
            name_normalized   = EXCLUDED.name_normalized,
            dba_name          = EXCLUDED.dba_name,
            parent_account_key= EXCLUDED.parent_account_key,
            mailing_address   = EXCLUDED.mailing_address,
            phone             = EXCLUDED.phone,
            email             = EXCLUDED.email,
            website           = EXCLUDED.website,
            status            = EXCLUDED.status,
            external_keys     = EXCLUDED.external_keys,
            size_metric       = EXCLUDED.size_metric,
            size_metric_unit  = EXCLUDED.size_metric_unit,
            confidence        = EXCLUDED.confidence
    """)
    loc_sql = _text("""
        INSERT INTO staging.resolved_location (
            location_key, account_key, vertical, location_name, site_address,
            geom, geocode_precision, geometry_source,
            maintained_acres, acres_confidence, site_type
        ) VALUES (
            :location_key, :account_key, :vertical, :location_name,
            CAST(:site_address AS jsonb),
            CASE
                WHEN :_latitude IS NOT NULL AND :_longitude IS NOT NULL
                THEN ST_SetSRID(
                    ST_MakePoint(
                        CAST(:_longitude AS double precision),
                        CAST(:_latitude AS double precision)
                    ), 4326)
                ELSE NULL
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
            geocode_precision = EXCLUDED.geocode_precision,
            geometry_source   = EXCLUDED.geometry_source,
            maintained_acres  = EXCLUDED.maintained_acres,
            acres_confidence  = EXCLUDED.acres_confidence,
            site_type         = EXCLUDED.site_type
    """)
    con_sql = _text("""
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

    # Scrub NaN → None on size_metric before the round-trip through to_dict().
    acct_rows = account_df.drop(
        columns=["_source_id", "_natural_key"], errors="ignore"
    ).to_dict(orient="records")
    for row in acct_rows:
        if pd.isna(row.get("size_metric")):
            row["size_metric"] = None

    loc_rows = location_df.drop(columns=["_natural_key"], errors="ignore").to_dict(orient="records")
    con_rows = contact_df.to_dict(orient="records")

    with engine.begin() as conn:
        # Delete this vertical's stale rows BEFORE upserting the fresh set.
        # Scoped to :vertical so the other vertical's rows are untouched.
        conn.execute(
            _text("DELETE FROM staging.resolved_contact WHERE vertical = :v"),
            {"v": vertical},
        )
        conn.execute(
            _text("DELETE FROM staging.resolved_location WHERE vertical = :v"),
            {"v": vertical},
        )
        conn.execute(
            _text("DELETE FROM staging.resolved_account WHERE vertical = :v"),
            {"v": vertical},
        )
        logger.info(
            "deleted existing staging.resolved_* rows for vertical=%s", vertical
        )

        if acct_rows:
            conn.execute(acct_sql, acct_rows)
        if loc_rows:
            conn.execute(loc_sql, loc_rows)
        if con_rows:
            conn.execute(con_sql, con_rows)

    logger.info(
        "upserted vertical=%s: account=%d, location=%d, contact=%d",
        vertical, len(acct_rows), len(loc_rows), len(con_rows),
    )
    return len(acct_rows), len(loc_rows), len(con_rows)


def run_pipeline(engine, dry_run: bool = False) -> dict:
    """
    Execute the full deathcare merge pipeline.

    Steps:
      1. Load all deathcare staging rows.
      2. Load parcel cache from staging.enrich_parcel.
      3. Load IRS 990 enrichment cache from staging.enrich_irs990.
      4. Run merge_pipeline() → merged DataFrame.
      5. Build resolved_account / resolved_location / resolved_contact DataFrames.
      6. Join IRS990 data: overlay phone_990 onto account, emit irs990_contact rows,
         suppress synthetic primary_phone contacts where a real contact now exists.
      7. Join parcel acreage onto resolved_location.
      8. Upsert into staging.resolved_* (skipped when dry_run=True).

    Returns a summary dict with row counts for each table.
    """
    logger.info("=== Deathcare pipeline starting (dry_run=%s) ===", dry_run)

    frames = [_load_source(engine, src) for src in _DEATHCARE_SOURCES]
    frames = [f for f in frames if not f.empty]

    if not frames:
        logger.warning("No deathcare staging data found — nothing to merge")
        return {"source_rows": 0, "account": 0, "location": 0, "contact": 0}

    logger.info("total rows across all deathcare sources: %d", sum(len(f) for f in frames))

    # 2. Load parcel cache
    parcels = _load_parcel_cache(engine)

    # 3. Load IRS 990 enrichment cache (D17b — real named contacts for religious orgs)
    irs990 = _load_irs990_cache(engine)

    merged = merge_pipeline(frames)
    print_summary(merged)

    account_df = _build_resolved_account(merged)
    location_df = _build_resolved_location(merged, account_df)
    contact_df = _build_resolved_contact(merged, account_df)

    # 6. Join IRS990: real named contacts take precedence over synthetic placeholders.
    # Survivorship rule: irs990_contact (real) > primary_phone (synthetic).
    # phone_990 fills account.phone when no phone is already present.
    account_df, contact_df = _join_irs990(account_df, contact_df, irs990)

    # 7. Join parcel acreage onto resolved_location
    location_df = _join_parcel(location_df, parcels)

    logger.info(
        "resolved rows built: account=%d, location=%d, contact=%d",
        len(account_df), len(location_df), len(contact_df),
    )

    if dry_run:
        logger.info("DRY RUN — no writes to staging.resolved_*")
        parcel_matched = location_df["maintained_acres"].notna().sum() if not location_df.empty else 0
        irs990_contacts = (contact_df["role"] == "irs990_contact").sum() if not contact_df.empty else 0
        print("\nDry-run summary (no DB writes):", file=sys.stderr)
        print(f"  merged rows       : {len(merged):>8,}", file=sys.stderr)
        print(f"  resolved_account  : {len(account_df):>8,}", file=sys.stderr)
        print(f"  resolved_location : {len(location_df):>8,}", file=sys.stderr)
        print(f"  resolved_contact  : {len(contact_df):>8,}", file=sys.stderr)
        print(f"  irs990_contacts   : {irs990_contacts:>8,}", file=sys.stderr)
        print(f"  parcel_matched    : {parcel_matched:>8,}", file=sys.stderr)
        return {
            "source_rows": sum(len(f) for f in frames),
            "account": len(account_df),
            "location": len(location_df),
            "contact": len(contact_df),
        }

    # 6. Replace this vertical's slice and upsert — all in one transaction (D15).
    # The delete + upserts share a single engine.begin() so a mid-run failure
    # cannot leave the tables empty: either the whole replace commits or the
    # previous data is rolled back and remains intact.
    n_account, n_location, n_contact = _replace_and_upsert(
        engine, account_df, location_df, contact_df, vertical="deathcare"
    )

    logger.info("=== Deathcare pipeline complete ===")
    print("\nPipeline summary:", file=sys.stderr)
    print(f"  merged rows       : {len(merged):>8,}", file=sys.stderr)
    print(f"  resolved_account  : {n_account:>8,}", file=sys.stderr)
    print(f"  resolved_location : {n_location:>8,}", file=sys.stderr)
    print(f"  resolved_contact  : {n_contact:>8,}", file=sys.stderr)

    return {
        "source_rows": sum(len(f) for f in frames),
        "account": n_account,
        "location": n_location,
        "contact": n_contact,
    }


# ---------------------------------------------------------------- CLI entrypoint


def main() -> None:
    """CLI entrypoint — run the deathcare merge pipeline."""
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the merge but do not write to staging.resolved_* tables.",
    )
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from lib.db import get_engine
    from lib.http import get_secret

    if not get_secret("DATABASE_URL"):
        sys.exit(
            "ERROR: DATABASE_URL is not set. "
            "Copy .env.example -> .env and fill it in."
        )

    engine = get_engine()
    run_pipeline(engine, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
