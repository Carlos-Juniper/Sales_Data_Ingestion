"""
pipeline_run — CLI helper to open/close a pipeline-level ingest.source_run row.

Each vertical's shell script (run_healthcare.sh, run_deathcare.sh, ...) calls
this before its first stage and after its last stage so `core_apply --run-id`
gets a real ingest.source_run_id instead of the 0 sentinel.

This is a pragmatic, PIPELINE-level id: one row covers the whole multi-source
script invocation, not per-source-record provenance. core.source_record's own
per-record write path (upsert_source_records in core_writer.py) is separate,
currently unused in production, and tracked as follow-up work — this helper
does not touch it.

Usage
-----
  # Open a run, print the new source_run_id to stdout (nothing else goes to
  # stdout, so `RUN_ID=$(...)` captures exactly the id).
  RUN_ID=$(python -m lib.pipeline_run start --source-id healthcare_pipeline)

  # Close it once the pipeline finishes (success or failure).
  python -m lib.pipeline_run finish --run-id "$RUN_ID" --status succeeded
"""

from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pipeline_run",
        description="Open/close a pipeline-level ingest.source_run row.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="Insert a new source_run row; prints its id to stdout.")
    start.add_argument("--source-id", required=True, help="e.g. healthcare_pipeline, deathcare_pipeline")

    finish = sub.add_parser("finish", help="Update status/row_count on an existing source_run row.")
    finish.add_argument("--run-id", type=int, required=True)
    finish.add_argument("--status", required=True, choices=["succeeded", "failed"])
    finish.add_argument("--row-count", type=int, default=None)

    args = parser.parse_args(argv)
    _setup_logging()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    try:
        from lib.db import get_engine, write_source_run, finish_source_run
    except ImportError as exc:
        logger.error("Import failed — run with PYTHONPATH=connectors: %s", exc)
        return 1

    try:
        engine = get_engine()
        if args.command == "start":
            run_id = write_source_run(
                engine,
                source_id=args.source_id,
                byte_count=0,
                sha256="",
                connector_version="",
                license_string="pipeline-level run marker — not a single-source raw payload",
            )
            print(run_id)
            return 0

        # finish
        finish_source_run(
            engine,
            args.run_id,
            status=args.status,
            row_count=args.row_count,
        )
        return 0

    except Exception:
        logger.exception("pipeline_run %s failed", args.command)
        return 1


if __name__ == "__main__":
    sys.exit(main())
