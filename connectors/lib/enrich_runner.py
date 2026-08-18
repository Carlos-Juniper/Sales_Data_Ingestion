"""
Generic serial/parallel enrichment runner.

Abstracts the branching loop shared by irs_990_enrich.py and
parcel_acreage_enrich.py: build a shared session outside, pass (idx, row)
tuples in, get results back in original submission order.
"""

from __future__ import annotations

import concurrent.futures
import sys
from typing import Any, Callable


def run_enrichment(
    rows: list[tuple[Any, Any]],
    fn: Callable[[Any], dict[str, Any]],
    *,
    workers: int = 1,
    label: str = "",
    progress_interval: int = 50,
) -> list[tuple[Any, dict[str, Any]]]:
    """Run fn over rows serially or via ThreadPoolExecutor. Returns results in original order.

    Parameters
    ----------
    rows:
        List of ``(idx, row)`` tuples. ``idx`` is an arbitrary hashable value
        (typically a DataFrame index label) used to correlate results back to
        the caller's data structure. ``row`` is passed directly to ``fn``.
    fn:
        Callable that accepts a single ``row`` value and returns a result dict.
        Must be thread-safe when ``workers > 1``.
    workers:
        Thread pool size. Values <= 1 run serially (useful for debugging or
        when the underlying API enforces strict sequential access).
    label:
        Optional prefix for progress lines written to stderr.
    progress_interval:
        Emit a progress line every this many completed items (and always on
        the last item).

    Returns
    -------
    List of ``(idx, result_dict)`` tuples in the same order as the input
    ``rows`` list, regardless of completion order in the parallel path.
    """
    total = len(rows)
    if total == 0:
        return []

    prefix = f"  {label}: " if label else "  "
    width = len(str(total))

    # Pre-allocate output slots so we can write by position and return in
    # original order without a sort step — important for the parallel path
    # where futures complete in arbitrary order.
    ordered: list[tuple[Any, dict[str, Any]] | None] = [None] * total
    # Map from original position to idx so we can populate ordered[pos].
    pos_to_idx: dict[int, Any] = {pos: idx for pos, (idx, _) in enumerate(rows)}

    def _emit_progress(done: int) -> None:
        pct = 100 * done / total
        sys.stderr.write(f"{prefix}{done:>{width}}/{total} ({pct:.0f}%)\n")

    if workers <= 1:
        for pos, (idx, row) in enumerate(rows):
            result = fn(row)
            ordered[pos] = (idx, result)
            done = pos + 1
            if done % progress_interval == 0 or done == total:
                _emit_progress(done)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            # Submit all futures and record their original position so we can
            # write results into the correct slot of `ordered`.
            future_to_pos: dict[concurrent.futures.Future, int] = {
                pool.submit(fn, row): pos for pos, (idx, row) in enumerate(rows)
            }
            done = 0
            for fut in concurrent.futures.as_completed(future_to_pos):
                pos = future_to_pos[fut]
                idx = pos_to_idx[pos]
                ordered[pos] = (idx, fut.result())
                done += 1
                if done % progress_interval == 0 or done == total:
                    _emit_progress(done)

    return ordered  # type: ignore[return-value]  # all slots filled by construction
