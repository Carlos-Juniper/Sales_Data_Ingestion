"""
Composable DataFrame validation helpers.

Callers keep their own source-specific assert_source_shape() logic.
These helpers eliminate the repeated if/raise pattern for common checks.
"""

from __future__ import annotations

import pandas as pd


def assert_columns_present(
    df: pd.DataFrame,
    required: list[str],
    label: str = "",
) -> None:
    """Raise ValueError listing missing columns."""
    missing = [c for c in required if c not in df.columns]
    if missing:
        prefix = f"{label}: " if label else ""
        raise ValueError(f"{prefix}missing required columns: {missing}")


def assert_fill_rate(
    df: pd.DataFrame,
    col: str,
    threshold: float,
    label: str = "",
) -> None:
    """Raise ValueError if column fill rate (non-null fraction) < threshold."""
    fill = df[col].notna().mean()
    if fill < threshold:
        prefix = f"{label}: " if label else ""
        raise ValueError(
            f"{prefix}{col} only {fill:.1%} populated (expected >={threshold:.0%})"
        )


def assert_min_rows(
    df: pd.DataFrame,
    minimum: int,
    label: str = "",
) -> None:
    """Raise ValueError if row count < minimum."""
    if len(df) < minimum:
        prefix = f"{label}: " if label else ""
        raise ValueError(
            f"{prefix}only {len(df):,} rows returned (expected >={minimum:,})"
        )
