"""
Integration tests for lib/db.py and the migration runner.

Requires: docker compose up -d postgres && python db/run_migrations.py
Run with: ALLOW_DB_INTEGRATION_TESTS=1 pytest -m integration connectors/tests/integration/
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------- migration runner

def test_migration_runner_applies_all_files(engine):
    """run_migrations.py should apply 001..006 and record them in schema_migrations."""
    # parents[3] = project root (connectors/tests/integration/ → connectors/tests/ → connectors/ → root)
    project_root = Path(__file__).parents[3]
    migrations_dir = project_root / "db" / "migrations"
    runner_path = project_root / "db" / "run_migrations.py"
    assert runner_path.exists(), "db/run_migrations.py not found"

    expected = sorted(p.name for p in migrations_dir.glob("*.sql"))
    assert expected, "No migration files found in db/migrations/"

    # Import and run the runner (it's idempotent — already-applied files are skipped).
    import importlib.util
    spec = importlib.util.spec_from_file_location("run_migrations", str(runner_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.run()  # should not raise


def test_schema_migrations_table_populated(engine):
    """schema_migrations should record all applied filenames after run_migrations.py."""
    from sqlalchemy import text
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT filename FROM public.schema_migrations ORDER BY filename")
        ).fetchall()
    filenames = [r[0] for r in rows]
    assert filenames, "schema_migrations is empty — run: python db/run_migrations.py"
    for i in range(1, 7):
        prefix = f"{i:03d}_"
        assert any(f.startswith(prefix) for f in filenames), (
            f"Migration {prefix}* not found in schema_migrations — re-run migrations"
        )


# ---------------------------------------------------------------- schemas exist

def test_schemas_created(engine):
    from sqlalchemy import text
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name IN ('staging','core','ingest','review')"
        )).fetchall()
    names = {r[0] for r in rows}
    assert names == {"staging", "core", "ingest", "review"}


# ---------------------------------------------------------------- source_run

def test_write_source_run_returns_id(engine):
    from lib.db import finish_source_run, write_source_run
    run_id = write_source_run(
        engine,
        source_id="test_source",
        byte_count=1024,
        sha256="abc123",
        connector_version="test",
    )
    assert isinstance(run_id, int)
    assert run_id > 0
    # Clean up.
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM ingest.source_run WHERE source_run_id = :id"),
            {"id": run_id},
        )


def test_finish_source_run_updates_status(engine):
    from lib.db import finish_source_run, write_source_run
    from sqlalchemy import text

    run_id = write_source_run(
        engine,
        source_id="test_finish",
        byte_count=512,
        sha256="deadbeef",
    )

    finish_source_run(engine, run_id, status="succeeded", row_count=42)

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, row_count FROM ingest.source_run WHERE source_run_id = :id"),
            {"id": run_id},
        ).fetchone()

    assert row[0] == "succeeded"
    assert row[1] == 42

    # Clean up.
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM ingest.source_run WHERE source_run_id = :id"),
            {"id": run_id},
        )


def test_finish_source_run_failed(engine):
    from lib.db import finish_source_run, write_source_run
    from sqlalchemy import text

    run_id = write_source_run(
        engine,
        source_id="test_fail",
        byte_count=0,
        sha256="0" * 64,
    )
    finish_source_run(engine, run_id, status="failed")

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status FROM ingest.source_run WHERE source_run_id = :id"),
            {"id": run_id},
        ).fetchone()
    assert row[0] == "failed"

    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM ingest.source_run WHERE source_run_id = :id"),
            {"id": run_id},
        )


def test_write_source_run_persists_raw_uri(engine):
    """D7: write_source_run(..., raw_uri=...) persists the URI to ingest.source_run.

    Uses a gs:// URI string — no real GCS call is made.
    Requires ALLOW_DB_INTEGRATION_TESTS=1 and a live local Postgres with
    migration 009 applied (which added the raw_uri column).
    """
    from lib.db import write_source_run
    from sqlalchemy import text

    test_uri = "gs://juniper-ingest-raw/test_source/2026-08-20/abc123.json.gz"

    run_id = write_source_run(
        engine,
        source_id="test_raw_uri",
        byte_count=512,
        sha256="a" * 64,
        raw_uri=test_uri,
    )
    assert isinstance(run_id, int)
    assert run_id > 0

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT raw_uri FROM ingest.source_run WHERE source_run_id = :id"),
            {"id": run_id},
        ).fetchone()

    assert row is not None, "source_run row not found after insert"
    assert row[0] == test_uri, (
        f"Expected raw_uri={test_uri!r}, got {row[0]!r}. "
        "Verify migration 009 (ADD COLUMN raw_uri text) has been applied."
    )

    # Clean up.
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM ingest.source_run WHERE source_run_id = :id"),
            {"id": run_id},
        )


def test_write_source_run_raw_uri_none_by_default(engine):
    """write_source_run without raw_uri persists NULL — backward-compatible."""
    from lib.db import write_source_run
    from sqlalchemy import text

    run_id = write_source_run(
        engine,
        source_id="test_raw_uri_null",
        byte_count=100,
        sha256="b" * 64,
        # raw_uri omitted — must default to None without error
    )

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT raw_uri FROM ingest.source_run WHERE source_run_id = :id"),
            {"id": run_id},
        ).fetchone()

    assert row[0] is None

    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM ingest.source_run WHERE source_run_id = :id"),
            {"id": run_id},
        )


# ---------------------------------------------------------------- upsert_staging

def _minimal_canonical(source_id: str, n: int = 3) -> pd.DataFrame:
    """Build a small CANONICAL_COLUMNS DataFrame for testing."""
    from lib.schema import build_canonical
    return build_canonical(
        pd.RangeIndex(n),
        source_id=source_id,
        natural_key=[f"KEY{i:04d}" for i in range(n)],
        vertical="healthcare",
        name_raw=[f"Clinic {i}" for i in range(n)],
        name_normalized=[f"clinic {i}" for i in range(n)],
        address_line_1=[f"{i} Main St" for i in range(n)],
        city="Testville",
        state="SC",
        zip5="29201",
        latitude=[34.0 + i * 0.01 for i in range(n)],
        longitude=[-81.0 - i * 0.01 for i in range(n)],
    )


def test_upsert_staging_inserts_rows(engine):
    from lib.db import upsert_staging
    from sqlalchemy import text

    source_id = "test_integration_source"
    df = _minimal_canonical(source_id)

    upsert_staging(engine, source_id, df)

    with engine.connect() as conn:
        count = conn.execute(
            text(f"SELECT count(*) FROM staging.{source_id} WHERE source_id = :sid"),
            {"sid": source_id},
        ).scalar()
    assert count == 3

    # Clean up.
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS staging.{source_id}"))


def test_upsert_staging_is_idempotent(engine):
    from lib.db import upsert_staging
    from sqlalchemy import text

    source_id = "test_idempotent_source"
    df = _minimal_canonical(source_id, n=2)

    upsert_staging(engine, source_id, df)
    upsert_staging(engine, source_id, df)  # second call should not error or double-insert

    with engine.connect() as conn:
        count = conn.execute(
            text(f"SELECT count(*) FROM staging.{source_id} WHERE source_id = :sid"),
            {"sid": source_id},
        ).scalar()
    assert count == 2  # still 2, not 4

    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS staging.{source_id}"))


def test_upsert_staging_geom_populated(engine):
    from lib.db import upsert_staging
    from sqlalchemy import text

    source_id = "test_geom_source"
    df = _minimal_canonical(source_id, n=1)

    upsert_staging(engine, source_id, df)

    with engine.connect() as conn:
        row = conn.execute(
            text(
                f"SELECT ST_AsText(geom) FROM staging.{source_id} "
                "WHERE source_id = :sid LIMIT 1"
            ),
            {"sid": source_id},
        ).fetchone()

    assert row is not None
    assert row[0] is not None
    assert row[0].startswith("POINT")

    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS staging.{source_id}"))


def test_upsert_staging_null_geom_when_no_coords(engine):
    from lib.db import upsert_staging
    from sqlalchemy import text

    source_id = "test_null_geom"
    from lib.schema import build_canonical
    df = build_canonical(
        pd.RangeIndex(1),
        source_id=source_id,
        natural_key=["NOCOORD"],
        vertical="healthcare",
        name_raw=["No Coord Clinic"],
    )

    upsert_staging(engine, source_id, df)

    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT geom FROM staging.{source_id} WHERE source_id = :sid LIMIT 1"),
            {"sid": source_id},
        ).fetchone()

    assert row[0] is None

    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS staging.{source_id}"))
