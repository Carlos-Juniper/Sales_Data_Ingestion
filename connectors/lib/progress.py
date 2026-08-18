"""
Progress reporting utilities for long-running enrichment loops.

Used by:
  - parcel_acreage_enrich.py — spatial parcel lookup batch runner
  - irs_990_enrich.py        — ProPublica 990 batch enrichment
"""

from __future__ import annotations

import sys

from collections import Counter


def print_progress(
    done: int,
    total: int,
    results: list[dict],
    label: str = "",
    status_key: str = "status",
    ok_values: list[str] | None = None,
) -> None:
    """Log progress to stderr every N rows."""
    if ok_values is None:
        ok_values = ["ok"]
    counts = Counter(r[status_key] for r in results)
    ok = sum(counts[v] for v in ok_values)
    pct = 100 * done / total
    prefix = f"  {label}: " if label else "  "
    sys.stderr.write(
        f"{prefix}{done:>{len(str(total))}}/{total} ({pct:.0f}%)  ok={ok}\n"
    )
