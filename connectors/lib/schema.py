"""
Canonical output schema definition for the deathcare pipeline.

CANONICAL_COLUMNS lists every column that all source connectors must produce.
validate_canonical() is the fast gate check used at pipeline boundaries.
"""

from __future__ import annotations

import pandas as pd

CANONICAL_COLUMNS: list[str] = [
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


def validate_canonical(df: pd.DataFrame) -> None:
    """Raise ValueError if df is missing any CANONICAL_COLUMNS column."""
    missing = [c for c in CANONICAL_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"DataFrame missing canonical column(s): {missing}")


def build_canonical(index, **cols) -> pd.DataFrame:
    """Return a DataFrame with exactly CANONICAL_COLUMNS, defaulting missing columns to None.
    Pass a pandas Index or RangeIndex as `index`. Scalar values broadcast across all rows."""
    out = pd.DataFrame({c: None for c in CANONICAL_COLUMNS}, index=index)
    for name, value in cols.items():
        out[name] = value
    return out[CANONICAL_COLUMNS]
