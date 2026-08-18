"""
Haversine distance utilities — scalar and vectorized — plus spatial clustering
and geographic reference data.

Used by:
  - deathcare_merge.py — spatial deduplication within 150 m radius
  - va_cemeteries.py   — state full-name → abbreviation lookup
"""

from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

_EARTH_RADIUS_KM: float = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Compute the great-circle distance in kilometres between two WGS84 points.

    Uses the Haversine formula implemented in pure Python/NumPy — no
    geopandas or shapely dependency.  Handles scalar float inputs only;
    for vectorised use, call haversine_km_vec().

    Args:
        lat1: Latitude of point 1 in decimal degrees.
        lon1: Longitude of point 1 in decimal degrees.
        lat2: Latitude of point 2 in decimal degrees.
        lon2: Longitude of point 2 in decimal degrees.

    Returns:
        Distance in kilometres (non-negative float).
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    return 2.0 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def haversine_km_vec(
    lat1: np.ndarray,
    lon1: np.ndarray,
    lat2: np.ndarray,
    lon2: np.ndarray,
) -> np.ndarray:
    """Vectorised Haversine over NumPy arrays.  Returns an array of distances in km."""
    phi1 = np.radians(lat1)
    phi2 = np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlambda = np.radians(lon2 - lon1)

    a = (
        np.sin(dphi / 2.0) ** 2
        + np.cos(phi1) * np.cos(phi2) * np.sin(dlambda / 2.0) ** 2
    )
    return 2.0 * _EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))


# ---------------------------------------------------------------- reference data

# Full state name → 2-letter abbreviation.
# Covers all 50 states, DC, and the five inhabited U.S. territories that appear
# in federal datasets (AS, GU, MP, PR, VI).  "Philippines" is included because
# it appears in VA National Cemetery exports for historically administered sites.
STATE_NAME_TO_ABBR: dict[str, str] = {
    "Alabama": "AL",
    "Alaska": "AK",
    "Arizona": "AZ",
    "Arkansas": "AR",
    "California": "CA",
    "Colorado": "CO",
    "Connecticut": "CT",
    "Delaware": "DE",
    "Florida": "FL",
    "Georgia": "GA",
    "Hawaii": "HI",
    "Idaho": "ID",
    "Illinois": "IL",
    "Indiana": "IN",
    "Iowa": "IA",
    "Kansas": "KS",
    "Kentucky": "KY",
    "Louisiana": "LA",
    "Maine": "ME",
    "Maryland": "MD",
    "Massachusetts": "MA",
    "Michigan": "MI",
    "Minnesota": "MN",
    "Mississippi": "MS",
    "Missouri": "MO",
    "Montana": "MT",
    "Nebraska": "NE",
    "Nevada": "NV",
    "New Hampshire": "NH",
    "New Jersey": "NJ",
    "New Mexico": "NM",
    "New York": "NY",
    "North Carolina": "NC",
    "North Dakota": "ND",
    "Ohio": "OH",
    "Oklahoma": "OK",
    "Oregon": "OR",
    "Pennsylvania": "PA",
    "Rhode Island": "RI",
    "South Carolina": "SC",
    "South Dakota": "SD",
    "Tennessee": "TN",
    "Texas": "TX",
    "Utah": "UT",
    "Vermont": "VT",
    "Virginia": "VA",
    "Washington": "WA",
    "West Virginia": "WV",
    "Wisconsin": "WI",
    "Wyoming": "WY",
    "District of Columbia": "DC",
    "American Samoa": "AS",
    "Guam": "GU",
    "Northern Mariana Islands": "MP",
    "Puerto Rico": "PR",
    "U.S. Virgin Islands": "VI",
    "Virgin Islands": "VI",
    "Philippines": "PH",
}


# ---------------------------------------------------------------- clustering

def cluster_within_radius(
    lats: list[float],
    lons: list[float],
    radius_km: float,
    cell_deg: float = 0.1,
) -> list[list[int]]:
    """
    Return index groups where each group is a set of point indices connected
    within *radius_km*.  Uses degree-grid blocking to avoid O(n²) comparisons.

    Points are assigned to a grid cell of size *cell_deg* degrees.  For each
    point the 3×3 neighbourhood of adjacent cells is checked, so no pair within
    *radius_km* can be missed as long as ``cell_deg`` is much larger than the
    angular equivalent of *radius_km* (~0.0014° per km at mid-latitudes).  The
    default 0.1° cell is ~73× the 150 m deathcare merge radius.

    Connectivity is computed with path-compressed Union-Find, so the cost is
    effectively linear in the number of within-radius pairs found.

    Args:
        lats: Parallel list of WGS84 latitudes (decimal degrees).  Caller must
              ensure all values are valid floats — no None filtering is done here.
        lons: Parallel list of WGS84 longitudes (decimal degrees), same length
              as *lats*.
        radius_km: Distance threshold in kilometres.  Point pairs at or below
                   this distance are placed in the same cluster.
        cell_deg: Degree-grid cell size used for blocking.  Must be >> the
                  angular equivalent of *radius_km*.  Default 0.1°.

    Returns:
        List of clusters.  Each cluster is a list of integer indices into
        *lats*/*lons*.  Singletons are included (every input point appears in
        exactly one cluster).  Order within each cluster and order of clusters
        are both stable (derived from ascending index order).
    """
    n = len(lats)

    # Union-Find: each point starts as its own root.
    parent = list(range(n))

    def _find(x: int) -> int:
        """Path-compressed find — uses path halving for cache efficiency."""
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # path halving
            x = parent[x]
        return x

    def _union(x: int, y: int) -> None:
        """Merge the components containing x and y."""
        rx, ry = _find(x), _find(y)
        if rx != ry:
            parent[ry] = rx

    # Assign every point to a grid cell and build a cell → [indices] index.
    grid_rows = [math.floor(lat / cell_deg) for lat in lats]
    grid_cols = [math.floor(lon / cell_deg) for lon in lons]

    cell_index: dict[tuple[int, int], list[int]] = {}
    for idx in range(n):
        cell = (grid_rows[idx], grid_cols[idx])
        cell_index.setdefault(cell, []).append(idx)

    # For each point, compare against the 3×3 neighbourhood.  The checked_pairs
    # set ensures each ordered pair (i, j) with i < j is evaluated exactly once.
    checked_pairs: set[tuple[int, int]] = set()

    for idx in range(n):
        r, c = grid_rows[idx], grid_cols[idx]
        candidates: list[int] = []
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                candidates.extend(cell_index.get((r + dr, c + dc), []))

        for jdx in candidates:
            if jdx <= idx:
                # Skip self-comparisons and already-ordered pairs.
                continue
            pair = (idx, jdx)
            if pair in checked_pairs:
                continue
            checked_pairs.add(pair)

            if haversine_km(lats[idx], lons[idx], lats[jdx], lons[jdx]) <= radius_km:
                _union(idx, jdx)

    # Collect all indices into their component buckets keyed by root.
    components: dict[int, list[int]] = defaultdict(list)
    for idx in range(n):
        components[_find(idx)].append(idx)

    return list(components.values())
