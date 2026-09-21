"""
core_apply — CLI driver for the core write path.

Usage
-----
  # Show what would change — no writes
  python -m lib.core_apply --dry-run --run-id 42

  # Apply the diff transactionally
  python -m lib.core_apply --run-id 42

  # Apply + report
  python -m lib.core_apply --run-id 42 --verbose

The ``--run-id`` is the ingest.source_run.source_run_id stamped into
core.source_record's first_seen_run_id / last_seen_run_id columns.
If omitted, a dummy run-id of 0 is used (suitable for manual testing
against staging data; do not use 0 in production pipelines).

Exit codes
----------
  0 — success (including zero-change re-runs)
  1 — unexpected error (logged to stderr)
"""

from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger(__name__)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )


def _print_counts(counts, *, mode: str) -> None:
    """Print a human-readable summary of DiffCounts."""
    print(f"\n=== core diff ({mode}) ===")
    print(f"  account:  +{counts.account_inserts} insert, "
          f"~{counts.account_updates} update, "
          f"{counts.account_tombstones} tombstone")
    print(f"  location: +{counts.location_inserts} insert, "
          f"~{counts.location_updates} update, "
          f"{counts.location_tombstones} tombstone")
    print(f"  contact:  +{counts.contact_inserts} insert, "
          f"~{counts.contact_updates} update, "
          f"{counts.contact_tombstones} tombstone")
    if hasattr(counts, "source_record_inserts"):
        print(f"  source_record: +{counts.source_record_inserts} insert, "
              f"~{counts.source_record_bumps} bump")
    print()


def main(argv: list[str] | None = None) -> int:
    """Entry point — returns exit code."""
    parser = argparse.ArgumentParser(
        prog="core_apply",
        description="3-way diff from staging.resolved_* into core.* (D5).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report insert/update/tombstone counts without writing.",
    )
    parser.add_argument(
        "--run-id",
        type=int,
        default=0,
        metavar="N",
        help=(
            "ingest.source_run.source_run_id to stamp on source_record rows. "
            "Use 0 only for manual testing; production must supply a real id."
        ),
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    try:
        from lib.db import get_engine
        from lib.core_writer import dry_run_diff, apply_core_diff
    except ImportError as exc:
        logger.error("Import failed — run with PYTHONPATH=connectors: %s", exc)
        return 1

    try:
        engine = get_engine()

        if args.dry_run:
            logger.info("dry-run mode — no writes will occur")
            counts = dry_run_diff(engine)
            _print_counts(counts, mode="dry-run")
            return 0

        # Apply mode
        if args.run_id == 0:
            logger.warning(
                "--run-id not supplied (or 0); source_record rows will have "
                "first_seen_run_id=0.  Provide a real source_run_id in production."
            )

        counts = apply_core_diff(engine, args.run_id)
        _print_counts(counts, mode="applied")
        return 0

    except Exception:
        logger.exception("core_apply failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
