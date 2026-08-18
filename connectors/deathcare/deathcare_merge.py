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

No CLI entry point — import and call merge_pipeline() or individual stages.
"""

from __future__ import annotations

import itertools
import sys
from typing import Sequence

import pandas as pd

from lib.enums import SEGMENT_RELIGIOUS, SEGMENT_MUNICIPAL, SEGMENT_FEDERAL, SEGMENT_COMMERCIAL, CONFIDENCE_HIGH, CONFIDENCE_SPATIAL_ONLY, CONFIDENCE_NONE  # noqa: F401 — SEGMENT_COMMERCIAL re-exported for pipeline callers
from lib.geo import cluster_within_radius, haversine_km, haversine_km_vec  # noqa: F401 — haversine_km re-exported for tests
from lib.match import name_similarity
from lib.schema import CANONICAL_COLUMNS, validate_canonical  # noqa: F401 — re-exported for pipeline callers

# ---------------------------------------------------------------- constants

# Levenshtein similarity threshold above which a spatial merge is labelled 'high'.
_NAME_SIMILARITY_THRESHOLD: float = 0.80

# Prefix used in source_id values that originate from the IRS BMF extract.
_BMF_SOURCE_PREFIX: str = "irs_bmf:"


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
    no_coords["merge_confidence"] = CONFIDENCE_NONE
    no_coords["merged_sources"] = no_coords["source_id"].astype(str)

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
            row["merged_sources"] = str(row["source_id"])
            merged_rows.append(row)
            continue

        # Multiple records within 150 m — merge them.
        # Start with the first member as the base spine.
        base = spatial.iloc[members[0]].copy()
        all_source_ids = [str(base["source_id"])]

        for i in members[1:]:
            other = spatial.iloc[i]
            base = _coalesce_records(base, other)
            all_source_ids.append(str(other["source_id"]))

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
