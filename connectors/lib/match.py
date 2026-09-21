"""
String similarity utilities for record matching.

Used by:
  - deathcare_merge.py — name similarity check during spatial deduplication
  - healthcare_merge.py — Tier 3 fuzzy deduplication across healthcare sources
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Callable

from rapidfuzz.distance import JaroWinkler as _JW

from lib.geo import haversine_km
from lib.normalize import normalize_name, normalize_phone, normalize_zip


def edit_distance(a: str, b: str) -> int:
    """
    Standard dynamic-programming Levenshtein edit distance.

    Complexity: O(len(a) * len(b)).  Strings are assumed short (names),
    so this is acceptable without further optimisation.
    """
    m, n = len(a), len(b)
    # dp[j] holds the edit distance between a[:i] and b[:j].
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev = dp[0]
        dp[0] = i
        for j in range(1, n + 1):
            temp = dp[j]
            if a[i - 1] == b[j - 1]:
                dp[j] = prev
            else:
                dp[j] = 1 + min(prev, dp[j], dp[j - 1])
            prev = temp
    return dp[n]


def name_similarity(a: str | None, b: str | None) -> float:
    """
    Levenshtein-based similarity ratio in [0, 1].

    Returns 0.0 when either name is empty — two unnamed sites have
    no name evidence to confirm they are the same place, so we do not
    award them a 'high' confidence merge.
    """
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return 0.0
    max_len = max(len(a), len(b))
    return 1.0 - edit_distance(a, b) / max_len


# ---------------------------------------------------------------------------
# Healthcare Tier 2/3 similarity functions
# ---------------------------------------------------------------------------


def jaro_winkler_similarity(a: str, b: str) -> float:
    """Jaro-Winkler similarity in [0, 1]."""
    if not a or not b:
        return 0.0
    return _JW.normalized_similarity(a, b)


def _trigram_set(s: str) -> Counter:
    """Return a Counter of all character trigrams in *s*."""
    return Counter(s[i : i + 3] for i in range(len(s) - 2))


def trigram_similarity(a: str, b: str) -> float:
    """Trigram overlap ratio: |A ∩ B| / max(|A|, |B|)."""
    if len(a) < 3 or len(b) < 3:
        return 0.0
    ta, tb = _trigram_set(a), _trigram_set(b)
    intersection = sum((ta & tb).values())
    # Denominator is max of the two multiset sizes, not the union, so longer
    # strings aren't systematically penalised vs. shorter ones.
    denominator = max(sum(ta.values()), sum(tb.values()))
    return intersection / denominator if denominator else 0.0


def compound_name_similarity(a: str, b: str) -> float:
    """Weighted blend of Jaro-Winkler (60 %) and trigram overlap (40 %)."""
    return 0.6 * jaro_winkler_similarity(a, b) + 0.4 * trigram_similarity(a, b)


def _feat_name(a: dict, b: dict) -> float:
    name_a = normalize_name(a.get("name_normalized", "") or a.get("name_raw", "") or "")
    name_b = normalize_name(b.get("name_normalized", "") or b.get("name_raw", "") or "")
    return compound_name_similarity(name_a, name_b)


def _feat_address(a: dict, b: dict) -> float:
    addr_a = normalize_name(a.get("address_line_1", "") or "")
    addr_b = normalize_name(b.get("address_line_1", "") or "")
    return compound_name_similarity(addr_a, addr_b) if addr_a and addr_b else 0.0


def _feat_spatial(a: dict, b: dict) -> float:
    lat_a, lon_a = a.get("latitude"), a.get("longitude")
    lat_b, lon_b = b.get("latitude"), b.get("longitude")
    if lat_a is None or lon_a is None or lat_b is None or lon_b is None:
        return 0.0
    dist_km = haversine_km(float(lat_a), float(lon_a), float(lat_b), float(lon_b))
    # Decays linearly from 1.0 at 0 m to 0.0 at 500 m (0.5 km).
    return max(0.0, 1.0 - dist_km / 0.5)


def _feat_phone(a: dict, b: dict) -> float:
    ph_a = normalize_phone(a.get("phone", "") or "")
    ph_b = normalize_phone(b.get("phone", "") or "")
    return 1.0 if ph_a and ph_b and ph_a == ph_b else 0.0


def _feat_size(a: dict, b: dict) -> float:
    sz_a, sz_b = a.get("size_metric"), b.get("size_metric")
    if sz_a is None or sz_b is None:
        return 0.0
    max_sz = max(float(sz_a), float(sz_b))
    if max_sz == 0:
        return 0.0
    ratio = abs(float(sz_a) - float(sz_b)) / max_sz
    return 1.0 if ratio < 0.20 else 0.0


_SCORE_FEATURES: list[tuple[float, Callable[[dict, dict], float]]] = [
    (0.35, _feat_name),
    (0.30, _feat_address),
    (0.20, _feat_spatial),
    (0.10, _feat_phone),
    (0.05, _feat_size),
]


def score_pair(a: dict, b: dict) -> float:
    """
    Weighted match score for a candidate record pair (Tier 3).

    Weights: name 35 %, address 30 %, spatial 20 %, phone 10 %, size 5 %.
    Returns a float in [0, 1].
    """
    return sum(w * fn(a, b) for w, fn in _SCORE_FEATURES)


def blocking_keys(rec: dict) -> list[str]:
    """
    Return opaque blocking key strings for a record.

    Two records are candidate pairs only if they share at least one key.
    This prunes the O(n²) comparison space before scoring.
    """
    state = rec.get("site_state", "") or ""
    zip5 = normalize_zip(rec.get("zip5", "") or "")
    name_prefix = (rec.get("name_normalized", "") or "")[:4]
    keys: list[str] = [f"szn:{state}:{zip5}:{name_prefix}"]

    phone = normalize_phone(rec.get("phone", "") or "")
    if phone:
        keys.append(f"sph:{state}:{phone}")

    lat, lon = rec.get("latitude"), rec.get("longitude")
    if lat is not None and lon is not None:
        # Cheap integer geohash — floor to 1-degree cells, no external library.
        # Two facilities within ~111 km of each other share the same cell, which
        # is intentionally coarse: the haversine in score_pair applies the real
        # 500 m threshold.
        geohash4 = int(math.floor(float(lat))) * 1000 + int(math.floor(float(lon)))
        keys.append(f"geo:{geohash4}")

    return keys
