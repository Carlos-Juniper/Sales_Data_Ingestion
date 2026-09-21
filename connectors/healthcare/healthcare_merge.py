"""
Healthcare deduplication pipeline.

Merges canonical records from CMS, NPPES, state supplements, and VA into a
single spine via three tiers:
  Tier 1 — exact key joins (CCN, then NPI)
  Tier 2 — exact composite join on (name_normalized, zip5, site_state),
            excluding known chain brands
  Tier 3 — fuzzy scoring with blocking-key candidate reduction

Entry point: merge_all(df) → (canonical_df, review_queue_df)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Optional

import pandas as pd
from sqlalchemy.engine import Engine

from lib.match import blocking_keys, score_pair
from lib.match_queue import STRATEGY_TIER3_FUZZY, enqueue_tier3_matches
from lib.normalize import normalize_name, normalize_phone, normalize_zip

logger = logging.getLogger(__name__)


def _is_present(val: Any) -> bool:
    """True only when val carries real data — not null, NaN, blank, or the literal string 'nan'."""
    if val is None:
        return False
    if isinstance(val, float) and (val != val):  # NaN float
        return False
    s = str(val).strip()
    return s != "" and s.lower() != "nan"


def _unmerged_mask(df: pd.DataFrame) -> pd.Series:
    """Vectorized test: True for rows whose cluster_id still equals their singleton src: key."""
    return df["cluster_id"] == ("src:" + df["source_id"].astype(str) + ":" + df["natural_key"].astype(str))

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Chain brands excluded from Tier 2 exact-name matching because their
# name_normalized is non-unique by design — many distinct campuses share it.
KNOWN_CHAINS: frozenset[str] = frozenset(
    {
        "HCA HEALTHCARE",
        "ASCENSION HEALTH",
        "COMMONSPIRIT HEALTH",
        "ADVENT HEALTH",
        "TENET HEALTHCARE",
        "COMMUNITY HEALTH SYSTEMS",
    }
)

# Source priority (index 0 = highest) used by survivorship to pick field values.
# Federal/state regulators carry the most authoritative licensing data.
_SOURCE_PRIORITY: list[str] = [
    "nc_dhsr",          # phantom — not yet implemented (§10 open item)
    "pa_doh",           # phantom — not yet implemented (§10 open item)
    "fl_ahca",          # phantom — not yet implemented (§10 open item)
    "sc_dph",           # phantom — not yet implemented (§10 open item)
    "va_facilities",
    "cms_general",
    "cms_nursing_home",
    "nppes_practice_locations",
]


def _source_rank(source_id: str) -> int:
    """Lower integer = higher priority. Unknown sources sort last."""
    try:
        return _SOURCE_PRIORITY.index(source_id)
    except ValueError:
        return len(_SOURCE_PRIORITY)


# ---------------------------------------------------------------------------
# Stage: prepare
# ---------------------------------------------------------------------------


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise fields and initialise cluster_id to each row's index string."""
    df = df.copy().reset_index(drop=True)

    df["name_normalized"] = df["name_raw"].map(normalize_name)
    df["phone"] = df["phone"].map(lambda v: normalize_phone(v) if pd.notna(v) else "")
    df["zip5"] = df["zip5"].map(lambda v: normalize_zip(str(v)) if pd.notna(v) else "")
    df["site_state"] = df["site_state"].fillna("").str.strip().str.upper()

    # Each record starts as its own cluster; Tier 1/2/3 will merge these.
    # Singleton ids are derived from content, not row position, so they are
    # stable across any shuffle of the input DataFrame.
    df["cluster_id"] = "src:" + df["source_id"].astype(str) + ":" + df["natural_key"].astype(str)

    return df


# ---------------------------------------------------------------------------
# Stage: Tier 1 — exact key merges
# ---------------------------------------------------------------------------


def tier1_merge_ccn(df: pd.DataFrame) -> pd.DataFrame:
    """Assign shared cluster_id to all rows that share the same non-empty CCN."""
    df = df.copy()
    has_ccn = df["ccn"].fillna("").str.strip() != ""
    ccn_rows = df[has_ccn]

    for ccn, group in ccn_rows.groupby("ccn"):
        cluster = f"ccn:{ccn}"
        df.loc[group.index, "cluster_id"] = cluster

    return df


def tier1_merge_npi(df: pd.DataFrame) -> pd.DataFrame:
    """
    Assign shared cluster_id to rows that share the same non-empty NPI.

    CCN clusters take precedence — a record already assigned a 'ccn:' cluster
    keeps that assignment even if it also carries an NPI.
    """
    df = df.copy()
    has_npi = df["npi"].fillna("").str.strip() != ""
    # Exclude rows already claimed by a CCN cluster.
    already_ccn = df["cluster_id"].str.startswith("ccn:")
    npi_rows = df[has_npi & ~already_ccn]

    for npi, group in npi_rows.groupby("npi"):
        cluster = f"npi:{npi}"
        df.loc[group.index, "cluster_id"] = cluster

    return df


# ---------------------------------------------------------------------------
# Stage: Tier 2 — exact composite match
# ---------------------------------------------------------------------------


def tier2_merge(df: pd.DataFrame) -> pd.DataFrame:
    """
    Exact (name_normalized, zip5, site_state) join for records still un-merged.

    Chain brands are excluded because their normalised name is shared across
    many legally distinct campuses that must not be merged.
    """
    df = df.copy()
    not_chain = ~df["name_normalized"].isin(KNOWN_CHAINS)
    candidates = df[_unmerged_mask(df) & not_chain]

    key_cols = ["name_normalized", "zip5", "site_state"]
    for (name_norm, zip5, state), group in candidates.groupby(key_cols):
        if len(group) < 2:
            continue
        # Derive cluster_id from the join key itself, not from any member's
        # existing id, so the result is identical regardless of input row order.
        anchor_cluster = f"t2:{name_norm}|{zip5}|{state}"
        df.loc[group.index, "cluster_id"] = anchor_cluster

    return df


# ---------------------------------------------------------------------------
# Stage: Tier 3 — fuzzy merge with blocking
# ---------------------------------------------------------------------------


def tier3_fuzzy_merge(
    df: pd.DataFrame,
    auto_threshold: float = 0.92,
    queue_threshold: float = 0.75,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Fuzzy-score candidate pairs among still-unmerged records.

    Returns (updated_df, review_queue_df).  review_queue_df has columns:
    key_a, key_b, score, source_a, source_b.
    """
    df = df.copy()
    candidates = df[_unmerged_mask(df)].copy()

    # Build inverted index: blocking_key → list of positional indices into *candidates*.
    block_index: dict[str, list[int]] = defaultdict(list)
    records = candidates.to_dict("records")
    for pos, rec in enumerate(records):
        for key in blocking_keys(rec):
            block_index[key].append(pos)

    # Collect unique pairs that share at least one blocking key.
    evaluated_pairs: set[tuple[int, int]] = set()
    for positions in block_index.values():
        for i in range(len(positions)):
            for j in range(i + 1, len(positions)):
                evaluated_pairs.add((positions[i], positions[j]))

    # First pass: score all pairs and split into auto-merge vs. review.
    # Collecting auto_pairs before assigning cluster_ids avoids non-transitive
    # pairwise assignment — two merges (A~B) and (B~C) must land A, B, C in the
    # same cluster even when processed in different iterations.
    auto_pairs: list[tuple[int, int]] = []
    review_rows: list[dict[str, Any]] = []

    for pos_i, pos_j in evaluated_pairs:
        score = score_pair(records[pos_i], records[pos_j])
        if score >= auto_threshold:
            auto_pairs.append((pos_i, pos_j))
        elif score >= queue_threshold:
            review_rows.append(
                {
                    "key_a": records[pos_i]["natural_key"],
                    "key_b": records[pos_j]["natural_key"],
                    "score": score,
                    "source_a": records[pos_i]["source_id"],
                    "source_b": records[pos_j]["source_id"],
                }
            )

    # Union-Find over candidate positional indices to resolve connected components.
    n = len(records)
    parent = list(range(n))

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(x: int, y: int) -> None:
        rx, ry = _find(x), _find(y)
        if rx != ry:
            parent[ry] = rx

    for pos_i, pos_j in auto_pairs:
        _union(pos_i, pos_j)

    # Collect all natural_keys per connected component so we can pick the
    # lexicographic minimum — a deterministic choice that is stable across any
    # input ordering (union-find root is order-dependent and varies with shuffle).
    component_keys: dict[int, list[str]] = defaultdict(list)
    for pos in range(n):
        component_keys[_find(pos)].append(records[pos]["natural_key"])

    for pos in range(n):
        root = _find(pos)
        cluster = f"t3:{min(component_keys[root])}"
        df.loc[candidates.index[pos], "cluster_id"] = cluster

    review_queue_df = pd.DataFrame(
        review_rows,
        columns=["key_a", "key_b", "score", "source_a", "source_b"],
    )
    return df, review_queue_df


# ---------------------------------------------------------------------------
# Stage: survivorship
# ---------------------------------------------------------------------------


def survivorship(df: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse each cluster_id group into one canonical survivor row.

    Field selection follows §5.3 precedence rules encoded in _SOURCE_PRIORITY.
    Returns one row per cluster with a 'merged_source_ids' column.
    """
    survivors: list[dict[str, Any]] = []

    for cluster_id, group in df.groupby("cluster_id"):
        # Sort so the highest-priority source is first in each field race.
        ranked = group.copy()
        ranked["_rank"] = ranked["source_id"].map(_source_rank)
        ranked = ranked.sort_values("_rank")

        survivor: dict[str, Any] = {}

        # name_normalized: federal file > state supplement > NPPES
        # _SOURCE_PRIORITY order already encodes this; take first non-empty.
        for _, row in ranked.iterrows():
            val = row.get("name_normalized", "")
            if _is_present(val):
                survivor["name_normalized"] = val
                break
        else:
            survivor["name_normalized"] = ""

        # phone: state regulator > CMS > NPPES (same priority order).
        for _, row in ranked.iterrows():
            val = row.get("phone", "")
            if _is_present(val):
                survivor["phone"] = val
                break
        else:
            survivor["phone"] = ""

        # latitude/longitude: keep first non-null; VA exact coords preferred
        # because _SOURCE_PRIORITY places "va" above CMS and NPPES.
        # Lat and lon are picked together from the same row so they stay paired.
        survivor["latitude"] = None
        survivor["longitude"] = None
        for _, row in ranked.iterrows():
            lat = row.get("latitude")
            if _is_present(lat):
                survivor["latitude"] = lat
                survivor["longitude"] = row.get("longitude")
                break

        # size_metric: state licensing > CMS POS > CMS Care Compare.
        survivor["size_metric"] = None
        for _, row in ranked.iterrows():
            val = row.get("size_metric")
            if _is_present(val):
                survivor["size_metric"] = val
                break

        # All remaining scalar columns: take highest-priority non-empty value.
        scalar_cols = [
            c
            for c in df.columns
            if c
            not in {
                "cluster_id",
                "name_normalized",
                "phone",
                "latitude",
                "longitude",
                "size_metric",
                "_rank",
                "merged_source_ids",
            }
        ]
        for col in scalar_cols:
            for _, row in ranked.iterrows():
                val = row.get(col)
                if _is_present(val):
                    survivor[col] = val
                    break
            else:
                survivor[col] = ""

        survivor["cluster_id"] = cluster_id
        survivor["merged_source_ids"] = ",".join(
            str(nk) for nk in group["natural_key"].tolist()
        )
        survivors.append(survivor)

    result = pd.DataFrame(survivors)
    # Drop internal sort column if it leaked through.
    result = result.drop(columns=["_rank"], errors="ignore")
    return result.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


def merge_all(
    df: pd.DataFrame,
    *,
    engine: Optional[Engine] = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Run the full Tier 1 → 2 → 3 pipeline, then survivorship.

    Args:
        df: Combined canonical DataFrame from all healthcare sources.
        engine: Optional SQLAlchemy engine.  When provided (i.e. in the
                ``--write-db`` path) uncertain Tier-3 pairs are enqueued into
                ``review.pending_pairs`` via :func:`enqueue_tier3_matches`.
                Pass ``None`` (default) for dry-run / test invocations that
                must not touch the database.

    Returns:
        (canonical_df, review_queue_df) — review_queue_df has columns
        key_a, key_b, score, source_a, source_b for every pair in the
        uncertain band [queue_threshold, auto_threshold).
    """
    df = prepare(df)
    df = tier1_merge_ccn(df)
    df = tier1_merge_npi(df)
    df = tier2_merge(df)
    df, review_queue = tier3_fuzzy_merge(df)
    canonical = survivorship(df)

    if engine is not None and not review_queue.empty:
        # Convert review_queue_df rows into the dict format expected by
        # enqueue_tier3_matches.  Column names from tier3_fuzzy_merge:
        #   key_a, key_b, score, source_a, source_b
        uncertain_pairs = review_queue.rename(
            columns={"source_a": "source_a", "source_b": "source_b"}
        ).to_dict(orient="records")
        n_enqueued = enqueue_tier3_matches(engine, uncertain_pairs, merge_strategy=STRATEGY_TIER3_FUZZY)
        logger.info(
            "merge_all: enqueued %d uncertain Tier-3 pairs into review.pending_pairs",
            n_enqueued,
        )

    return canonical, review_queue
