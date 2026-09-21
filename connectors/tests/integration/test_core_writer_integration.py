"""
Integration tests for lib.core_writer — require a live Postgres instance.

Run with:
    docker compose up -d postgres
    python db/run_migrations.py
    ALLOW_DESTRUCTIVE_DB_TESTS=1 pytest -m integration connectors/tests/integration/

These tests write and then clean up from core.account, core.location,
core.contact, and core.source_record.  They must NEVER point at Cloud SQL
(see §8 of HANDOFF-end-to-end-pipeline.md).  The ALLOW_DESTRUCTIVE_DB_TESTS
opt-in is the real gate — the localhost check is defence in depth only.

Key acceptance criteria tested here:
  §3 idempotency contract — after two consecutive applies on unchanged
      staging.resolved_*, core.account/location/contact and core.source_record
      must gain 0 rows.
  D5 dry-run proof — dry_run_diff counts must match apply_core_diff counts on
      first apply, and report 0 on second apply.
  D8 source_record content-addressing — unchanged payload only bumps
      last_seen_run_id; changed payload inserts a new row.
  D2 tombstoning — absent keys get status='merged'; parent_account_id set
      to survivor where resolvable.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Generator

import pytest

pytestmark = pytest.mark.integration

# The conftest.py in this directory registers the 'integration' marker and
# provides the 'engine' fixture (with ALLOW_DB_INTEGRATION_TESTS opt-in).
# We add ALLOW_DESTRUCTIVE_DB_TESTS on top because these tests write to
# core.*, not just staging.test_*.


def _allow_destructive() -> bool:
    return os.environ.get("ALLOW_DESTRUCTIVE_DB_TESTS", "").lower() in ("1", "true", "yes")


@pytest.fixture(autouse=True)
def require_destructive_opt_in():
    """Skip every test in this file unless ALLOW_DESTRUCTIVE_DB_TESTS=1."""
    if not _allow_destructive():
        pytest.skip(
            "Core-writer integration tests modify core.* tables. "
            "Set ALLOW_DESTRUCTIVE_DB_TESTS=1 and point DATABASE_URL at your "
            "local docker Postgres — NOT the Cloud SQL proxy."
        )


# ---------------------------------------------------------------------------
# Helpers — insert and clean up resolved_* work-table rows
# ---------------------------------------------------------------------------

_FAKE_ACCOUNT_KEY = "test_account_key_integration_001"
_FAKE_ACCOUNT_KEY_2 = "test_account_key_integration_002"
_FAKE_LOCATION_KEY = "test_location_key_integration_001"
_FAKE_CONTACT_KEY = "test_contact_key_integration_001"
_FAKE_SOURCE_ID = "test_core_writer_src"


def _insert_resolved_account(conn, key: str, name: str = "Test Corp", status: str = "active",
                              parent_key: str | None = None) -> None:
    from sqlalchemy import text
    conn.execute(text("""
        INSERT INTO staging.resolved_account (
            account_key, vertical, account_type,
            legal_name, name_normalized, dba_name,
            parent_account_key,
            mailing_address, phone, email, website,
            status, external_keys,
            size_metric, size_metric_unit, confidence
        ) VALUES (
            :key, 'healthcare', 'single_site',
            :name, :name_lower, NULL,
            :parent_key,
            NULL, NULL, NULL, NULL,
            :status, NULL,
            NULL, NULL, NULL
        )
        ON CONFLICT (account_key) DO UPDATE
            SET legal_name = EXCLUDED.legal_name,
                name_normalized = EXCLUDED.name_normalized,
                status = EXCLUDED.status,
                parent_account_key = EXCLUDED.parent_account_key
    """), {
        "key": key,
        "name": name,
        "name_lower": name.lower(),
        "status": status,
        "parent_key": parent_key,
    })


def _insert_resolved_location(conn, key: str, account_key: str) -> None:
    from sqlalchemy import text
    conn.execute(text("""
        INSERT INTO staging.resolved_location (
            location_key, account_key, location_name,
            site_address, geocode_precision, site_type
        ) VALUES (
            :key, :account_key, 'Main Clinic',
            NULL, 'zip', 'campus'
        )
        ON CONFLICT (location_key) DO UPDATE
            SET account_key = EXCLUDED.account_key
    """), {"key": key, "account_key": account_key})


def _insert_resolved_contact(conn, key: str, account_key: str, name: str = "Jane Doe") -> None:
    from sqlalchemy import text
    conn.execute(text("""
        INSERT INTO staging.resolved_contact (
            contact_key, account_key, full_name,
            role, role_rank, source_id, is_current
        ) VALUES (
            :key, :account_key, :name,
            'facilities_dir', 1, 'test_src', true
        )
        ON CONFLICT (contact_key) DO UPDATE
            SET full_name = EXCLUDED.full_name
    """), {"key": key, "account_key": account_key, "name": name})


def _delete_resolved_rows(conn) -> None:
    """Remove test rows from staging.resolved_* tables."""
    from sqlalchemy import text
    for key in (_FAKE_ACCOUNT_KEY, _FAKE_ACCOUNT_KEY_2):
        conn.execute(text(
            "DELETE FROM staging.resolved_account WHERE account_key = :k"
        ), {"k": key})
    conn.execute(text(
        "DELETE FROM staging.resolved_location WHERE location_key = :k"
    ), {"k": _FAKE_LOCATION_KEY})
    conn.execute(text(
        "DELETE FROM staging.resolved_contact WHERE contact_key = :k"
    ), {"k": _FAKE_CONTACT_KEY})


def _delete_core_rows(conn) -> None:
    """Remove test rows from core.* tables (cleanup after tests)."""
    from sqlalchemy import text
    # Remove source_records first (FKs to account/location)
    conn.execute(text(
        "DELETE FROM core.source_record WHERE source_id = :sid"
    ), {"sid": _FAKE_SOURCE_ID})
    # Contacts FK to account
    conn.execute(text(
        "DELETE FROM core.contact WHERE contact_key = :k"
    ), {"k": _FAKE_CONTACT_KEY})
    # Locations FK to account
    conn.execute(text(
        "DELETE FROM core.location WHERE location_key = :k"
    ), {"k": _FAKE_LOCATION_KEY})
    # Accounts (clear parent_account_id self-FK first)
    for key in (_FAKE_ACCOUNT_KEY, _FAKE_ACCOUNT_KEY_2):
        conn.execute(text("""
            UPDATE core.account
            SET parent_account_id = NULL
            WHERE account_key = :k
        """), {"k": key})
    for key in (_FAKE_ACCOUNT_KEY, _FAKE_ACCOUNT_KEY_2):
        conn.execute(text(
            "DELETE FROM core.account WHERE account_key = :k"
        ), {"k": key})


def _row_count(conn, table: str, where_col: str, where_val: str) -> int:
    from sqlalchemy import text
    return conn.execute(text(
        f"SELECT count(*) FROM {table} WHERE {where_col} = :v"
    ), {"v": where_val}).scalar()


# ---------------------------------------------------------------------------
# Fixture: set up and tear down resolved_* + core.* rows
# ---------------------------------------------------------------------------

@pytest.fixture()
def clean_state(engine):
    """Ensure test rows are absent before and after each test."""
    with engine.begin() as conn:
        _delete_core_rows(conn)
        _delete_resolved_rows(conn)
    yield engine
    with engine.begin() as conn:
        _delete_core_rows(conn)
        _delete_resolved_rows(conn)


# ---------------------------------------------------------------------------
# Basic insert path
# ---------------------------------------------------------------------------

class TestCoreAccountInsert:
    """Account rows not in core are inserted on apply."""

    def test_new_account_inserted(self, clean_state):
        from lib.core_writer import apply_core_diff
        engine = clean_state

        with engine.begin() as conn:
            _insert_resolved_account(conn, _FAKE_ACCOUNT_KEY)

        counts = apply_core_diff(engine, run_id=1)
        assert counts.account_inserts >= 1

        from sqlalchemy import text
        with engine.connect() as conn:
            n = _row_count(conn, "core.account", "account_key", _FAKE_ACCOUNT_KEY)
        assert n == 1

    def test_new_location_inserted(self, clean_state):
        from lib.core_writer import apply_core_diff
        engine = clean_state

        with engine.begin() as conn:
            _insert_resolved_account(conn, _FAKE_ACCOUNT_KEY)
            _insert_resolved_location(conn, _FAKE_LOCATION_KEY, _FAKE_ACCOUNT_KEY)

        counts = apply_core_diff(engine, run_id=1)
        assert counts.location_inserts >= 1

        from sqlalchemy import text
        with engine.connect() as conn:
            n = _row_count(conn, "core.location", "location_key", _FAKE_LOCATION_KEY)
        assert n == 1

    def test_new_contact_inserted(self, clean_state):
        from lib.core_writer import apply_core_diff
        engine = clean_state

        with engine.begin() as conn:
            _insert_resolved_account(conn, _FAKE_ACCOUNT_KEY)
            _insert_resolved_contact(conn, _FAKE_CONTACT_KEY, _FAKE_ACCOUNT_KEY)

        counts = apply_core_diff(engine, run_id=1)
        assert counts.contact_inserts >= 1

        from sqlalchemy import text
        with engine.connect() as conn:
            n = _row_count(conn, "core.contact", "contact_key", _FAKE_CONTACT_KEY)
        assert n == 1


# ---------------------------------------------------------------------------
# §3 Idempotency contract — the core acceptance criterion
# ---------------------------------------------------------------------------

class TestIdempotencyContract:
    """After two consecutive applies on unchanged resolved_*, 0 rows gained."""

    def test_zero_rows_on_second_apply_account(self, clean_state):
        """core.account must gain 0 rows on second apply (§3)."""
        from lib.core_writer import apply_core_diff
        from sqlalchemy import text
        engine = clean_state

        with engine.begin() as conn:
            _insert_resolved_account(conn, _FAKE_ACCOUNT_KEY)

        # First apply — should insert
        c1 = apply_core_diff(engine, run_id=1)
        assert c1.account_inserts >= 1

        with engine.connect() as conn:
            count_after_first = _row_count(conn, "core.account", "account_key", _FAKE_ACCOUNT_KEY)

        # Second apply on identical resolved_* — must be 0 new rows
        c2 = apply_core_diff(engine, run_id=2)
        assert c2.account_inserts == 0, (
            f"Second apply inserted {c2.account_inserts} account rows — idempotency violated"
        )
        assert c2.account_updates == 0, (
            f"Second apply updated {c2.account_updates} account rows — content unchanged"
        )

        with engine.connect() as conn:
            count_after_second = _row_count(conn, "core.account", "account_key", _FAKE_ACCOUNT_KEY)

        assert count_after_first == count_after_second, (
            f"Row count changed on second apply: {count_after_first} → {count_after_second}"
        )

    def test_zero_rows_on_second_apply_location(self, clean_state):
        """core.location must gain 0 rows on second apply (§3)."""
        from lib.core_writer import apply_core_diff
        engine = clean_state

        with engine.begin() as conn:
            _insert_resolved_account(conn, _FAKE_ACCOUNT_KEY)
            _insert_resolved_location(conn, _FAKE_LOCATION_KEY, _FAKE_ACCOUNT_KEY)

        apply_core_diff(engine, run_id=1)

        c2 = apply_core_diff(engine, run_id=2)
        assert c2.location_inserts == 0
        assert c2.location_updates == 0

    def test_zero_rows_on_second_apply_contact(self, clean_state):
        """core.contact must gain 0 rows on second apply (§3)."""
        from lib.core_writer import apply_core_diff
        engine = clean_state

        with engine.begin() as conn:
            _insert_resolved_account(conn, _FAKE_ACCOUNT_KEY)
            _insert_resolved_contact(conn, _FAKE_CONTACT_KEY, _FAKE_ACCOUNT_KEY)

        apply_core_diff(engine, run_id=1)

        c2 = apply_core_diff(engine, run_id=2)
        assert c2.contact_inserts == 0
        assert c2.contact_updates == 0

    def test_zero_rows_all_entities_second_apply(self, clean_state):
        """Full idempotency: all entities show 0 inserts/updates on second apply."""
        from lib.core_writer import apply_core_diff
        engine = clean_state

        with engine.begin() as conn:
            _insert_resolved_account(conn, _FAKE_ACCOUNT_KEY)
            _insert_resolved_location(conn, _FAKE_LOCATION_KEY, _FAKE_ACCOUNT_KEY)
            _insert_resolved_contact(conn, _FAKE_CONTACT_KEY, _FAKE_ACCOUNT_KEY)

        c1 = apply_core_diff(engine, run_id=1)
        assert c1.account_inserts == 1
        assert c1.location_inserts == 1
        assert c1.contact_inserts == 1

        c2 = apply_core_diff(engine, run_id=2)
        assert c2.account_inserts == 0, "Second apply must insert 0 accounts"
        assert c2.account_updates == 0, "Second apply must update 0 accounts"
        assert c2.location_inserts == 0, "Second apply must insert 0 locations"
        assert c2.location_updates == 0, "Second apply must update 0 locations"
        assert c2.contact_inserts == 0, "Second apply must insert 0 contacts"
        assert c2.contact_updates == 0, "Second apply must update 0 contacts"


# ---------------------------------------------------------------------------
# D5: dry-run query parity
# ---------------------------------------------------------------------------

class TestDryRunParity:
    """dry_run_diff counts must match apply counts on first apply, then all-zero."""

    def test_dry_run_matches_apply_on_first_run(self, clean_state):
        """dry_run_diff reports the same inserts as apply on a fresh state."""
        from lib.core_writer import dry_run_diff, apply_core_diff
        engine = clean_state

        with engine.begin() as conn:
            _insert_resolved_account(conn, _FAKE_ACCOUNT_KEY)

        dry = dry_run_diff(engine)
        applied = apply_core_diff(engine, run_id=1)

        assert dry.account_inserts == applied.account_inserts, (
            f"Dry-run reported {dry.account_inserts} inserts but apply did "
            f"{applied.account_inserts}"
        )

    def test_dry_run_reports_zero_after_apply(self, clean_state):
        """After apply, dry_run_diff must report 0 for unchanged resolved_*."""
        from lib.core_writer import dry_run_diff, apply_core_diff
        engine = clean_state

        with engine.begin() as conn:
            _insert_resolved_account(conn, _FAKE_ACCOUNT_KEY)

        apply_core_diff(engine, run_id=1)

        dry2 = dry_run_diff(engine)
        assert dry2.account_inserts == 0
        assert dry2.account_updates == 0

    def test_dry_run_does_not_write(self, clean_state):
        """dry_run_diff must not insert any rows into core.account."""
        from lib.core_writer import dry_run_diff
        from sqlalchemy import text
        engine = clean_state

        with engine.begin() as conn:
            _insert_resolved_account(conn, _FAKE_ACCOUNT_KEY)

        dry_run_diff(engine)

        with engine.connect() as conn:
            n = _row_count(conn, "core.account", "account_key", _FAKE_ACCOUNT_KEY)
        assert n == 0, "dry_run_diff wrote rows — it must be read-only"


# ---------------------------------------------------------------------------
# D8: source_record content-addressed upsert
# ---------------------------------------------------------------------------

class TestSourceRecordUpsert:
    """core.source_record: unchanged payload = 0 new rows (D8 acceptance)."""

    def _make_run_id(self, engine) -> int:
        """Create a real ingest.source_run row and return its id."""
        from lib.db import write_source_run
        return write_source_run(
            engine,
            source_id=_FAKE_SOURCE_ID,
            byte_count=0,
            sha256="a" * 64,
        )

    def test_new_record_inserted(self, clean_state):
        from lib.core_writer import upsert_source_records, SourceRecordRow
        from sqlalchemy import text
        engine = clean_state
        run_id = self._make_run_id(engine)

        rows = [SourceRecordRow(_FAKE_SOURCE_ID, "KEY001", {"name": "Acme"})]
        inserted, bumped = upsert_source_records(engine, rows, run_id)
        assert inserted == 1
        assert bumped == 0

    def test_unchanged_payload_bumps_only(self, clean_state):
        """Second call with identical payload inserts 0 rows — only bumps last_seen_run_id."""
        from lib.core_writer import upsert_source_records, SourceRecordRow
        from sqlalchemy import text
        engine = clean_state
        run_id_1 = self._make_run_id(engine)
        run_id_2 = self._make_run_id(engine)

        payload = {"name": "Acme", "zip": "29201"}
        rows = [SourceRecordRow(_FAKE_SOURCE_ID, "KEY001", payload)]

        inserted1, bumped1 = upsert_source_records(engine, rows, run_id_1)
        assert inserted1 == 1
        assert bumped1 == 0

        inserted2, bumped2 = upsert_source_records(engine, rows, run_id_2)
        assert inserted2 == 0, (
            "Identical payload on second call must insert 0 rows (D8)"
        )
        assert bumped2 == 1, "last_seen_run_id bump must be counted"

        # Confirm only 1 row in DB, not 2
        with engine.connect() as conn:
            n = conn.execute(text(
                "SELECT count(*) FROM core.source_record "
                "WHERE source_id = :sid AND natural_key = 'KEY001'"
            ), {"sid": _FAKE_SOURCE_ID}).scalar()
        assert n == 1

    def test_changed_payload_inserts_new_row(self, clean_state):
        """Changed payload (new sha) inserts a new row alongside the old one."""
        from lib.core_writer import upsert_source_records, SourceRecordRow
        from sqlalchemy import text
        engine = clean_state
        run_id_1 = self._make_run_id(engine)
        run_id_2 = self._make_run_id(engine)

        rows_v1 = [SourceRecordRow(_FAKE_SOURCE_ID, "KEY001", {"name": "Acme v1"})]
        rows_v2 = [SourceRecordRow(_FAKE_SOURCE_ID, "KEY001", {"name": "Acme v2"})]

        upsert_source_records(engine, rows_v1, run_id_1)
        inserted2, _ = upsert_source_records(engine, rows_v2, run_id_2)
        assert inserted2 == 1, "Changed payload must insert a new content-addressed row"

        with engine.connect() as conn:
            n = conn.execute(text(
                "SELECT count(*) FROM core.source_record "
                "WHERE source_id = :sid AND natural_key = 'KEY001'"
            ), {"sid": _FAKE_SOURCE_ID}).scalar()
        assert n == 2, "Both payload versions must coexist as separate rows"

    def test_zero_row_gain_on_identical_rerun(self, clean_state):
        """§3 acceptance for source_record: 0 new rows on identical re-run."""
        from lib.core_writer import upsert_source_records, SourceRecordRow
        from sqlalchemy import text
        engine = clean_state
        run_id_1 = self._make_run_id(engine)
        run_id_2 = self._make_run_id(engine)

        payload_set = [
            SourceRecordRow(_FAKE_SOURCE_ID, f"KEY{i:03d}", {"value": i})
            for i in range(5)
        ]

        upsert_source_records(engine, payload_set, run_id_1)

        with engine.connect() as conn:
            count_before = conn.execute(text(
                "SELECT count(*) FROM core.source_record WHERE source_id = :sid"
            ), {"sid": _FAKE_SOURCE_ID}).scalar()

        upsert_source_records(engine, payload_set, run_id_2)

        with engine.connect() as conn:
            count_after = conn.execute(text(
                "SELECT count(*) FROM core.source_record WHERE source_id = :sid"
            ), {"sid": _FAKE_SOURCE_ID}).scalar()

        assert count_after == count_before, (
            f"source_record gained rows on identical re-run: "
            f"{count_before} → {count_after} (§3 violated)"
        )

    def test_last_seen_run_id_bumped(self, clean_state):
        """last_seen_run_id must reflect the most recent run_id on each bump."""
        from lib.core_writer import upsert_source_records, SourceRecordRow
        from sqlalchemy import text
        engine = clean_state
        run_id_1 = self._make_run_id(engine)
        run_id_2 = self._make_run_id(engine)

        rows = [SourceRecordRow(_FAKE_SOURCE_ID, "KEY001", {"name": "Stable"})]
        upsert_source_records(engine, rows, run_id_1)
        upsert_source_records(engine, rows, run_id_2)

        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT first_seen_run_id, last_seen_run_id
                FROM core.source_record
                WHERE source_id = :sid AND natural_key = 'KEY001'
            """), {"sid": _FAKE_SOURCE_ID}).fetchone()

        assert row[0] == run_id_1, "first_seen_run_id must not change"
        assert row[1] == run_id_2, "last_seen_run_id must reflect most recent run"


# ---------------------------------------------------------------------------
# D2: Tombstoning
# ---------------------------------------------------------------------------

class TestTombstoning:
    """Absent account keys → status='merged'; contact is_current → false."""

    def test_account_tombstoned_when_absent(self, clean_state):
        """A core.account row absent from resolved_account gets status='merged'."""
        from lib.core_writer import apply_core_diff
        from sqlalchemy import text
        engine = clean_state

        # Insert and apply so the account exists in core
        with engine.begin() as conn:
            _insert_resolved_account(conn, _FAKE_ACCOUNT_KEY)
        apply_core_diff(engine, run_id=1)

        # Remove from resolved — simulates cluster merge eliminating this key
        with engine.begin() as conn:
            conn.execute(text(
                "DELETE FROM staging.resolved_account WHERE account_key = :k"
            ), {"k": _FAKE_ACCOUNT_KEY})

        c2 = apply_core_diff(engine, run_id=2)
        assert c2.account_tombstones >= 1

        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT status FROM core.account WHERE account_key = :k"
            ), {"k": _FAKE_ACCOUNT_KEY}).fetchone()

        assert row is not None, "Tombstoned account must still exist in core"
        assert row[0] == "merged", (
            f"Expected status='merged', got {row[0]!r}"
        )

    def test_already_tombstoned_not_double_counted(self, clean_state):
        """A row already status='merged' must not appear in tombstone count again."""
        from lib.core_writer import apply_core_diff
        from sqlalchemy import text
        engine = clean_state

        with engine.begin() as conn:
            _insert_resolved_account(conn, _FAKE_ACCOUNT_KEY)
        apply_core_diff(engine, run_id=1)

        # Remove from resolved to trigger tombstone on second apply
        with engine.begin() as conn:
            conn.execute(text(
                "DELETE FROM staging.resolved_account WHERE account_key = :k"
            ), {"k": _FAKE_ACCOUNT_KEY})

        apply_core_diff(engine, run_id=2)  # tombstones the row

        # Third apply — resolved still empty for this key
        c3 = apply_core_diff(engine, run_id=3)
        assert c3.account_tombstones == 0, (
            "Already-merged row must not be tombstoned again"
        )

    def test_contact_set_inactive_when_absent(self, clean_state):
        """core.contact rows absent from resolved get is_current=false."""
        from lib.core_writer import apply_core_diff
        from sqlalchemy import text
        engine = clean_state

        with engine.begin() as conn:
            _insert_resolved_account(conn, _FAKE_ACCOUNT_KEY)
            _insert_resolved_contact(conn, _FAKE_CONTACT_KEY, _FAKE_ACCOUNT_KEY)
        apply_core_diff(engine, run_id=1)

        # Remove contact from resolved
        with engine.begin() as conn:
            conn.execute(text(
                "DELETE FROM staging.resolved_contact WHERE contact_key = :k"
            ), {"k": _FAKE_CONTACT_KEY})

        c2 = apply_core_diff(engine, run_id=2)
        assert c2.contact_tombstones >= 1

        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT is_current FROM core.contact WHERE contact_key = :k"
            ), {"k": _FAKE_CONTACT_KEY}).fetchone()

        assert row is not None, "Contact must still exist in core"
        assert row[0] is False, "Absent contact must have is_current=false"

    def test_tombstone_with_survivor_sets_parent_id(self, clean_state):
        """Tombstoned account gets parent_account_id pointing to survivor (D2)."""
        from lib.core_writer import apply_core_diff
        from sqlalchemy import text
        engine = clean_state

        # Insert two accounts
        with engine.begin() as conn:
            _insert_resolved_account(conn, _FAKE_ACCOUNT_KEY)
            _insert_resolved_account(conn, _FAKE_ACCOUNT_KEY_2)
        apply_core_diff(engine, run_id=1)

        # Simulate merge: remove key_1, key_2 becomes survivor and points at key_1
        with engine.begin() as conn:
            conn.execute(text(
                "DELETE FROM staging.resolved_account WHERE account_key = :k"
            ), {"k": _FAKE_ACCOUNT_KEY})
            # key_2 now claims to be the survivor that absorbed key_1
            conn.execute(text("""
                UPDATE staging.resolved_account
                SET parent_account_key = :dead_key
                WHERE account_key = :survivor_key
            """), {"dead_key": _FAKE_ACCOUNT_KEY, "survivor_key": _FAKE_ACCOUNT_KEY_2})

        apply_core_diff(engine, run_id=2)

        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT ca.status, pa.account_key AS parent_key
                FROM core.account ca
                LEFT JOIN core.account pa ON pa.account_id = ca.parent_account_id
                WHERE ca.account_key = :k
            """), {"k": _FAKE_ACCOUNT_KEY}).fetchone()

        assert row[0] == "merged", f"Expected status='merged', got {row[0]!r}"
        assert row[1] == _FAKE_ACCOUNT_KEY_2, (
            f"Expected parent_account_key={_FAKE_ACCOUNT_KEY_2!r}, got {row[1]!r}"
        )


# ---------------------------------------------------------------------------
# Update path
# ---------------------------------------------------------------------------

class TestAccountUpdate:
    """Changed content triggers an UPDATE, not a second INSERT."""

    def test_changed_name_triggers_update(self, clean_state):
        """Changing legal_name in resolved_account must UPDATE, not INSERT."""
        from lib.core_writer import apply_core_diff
        from sqlalchemy import text
        engine = clean_state

        with engine.begin() as conn:
            _insert_resolved_account(conn, _FAKE_ACCOUNT_KEY, name="Acme v1")
        apply_core_diff(engine, run_id=1)

        # Change the name in resolved
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE staging.resolved_account
                SET legal_name = 'Acme v2', name_normalized = 'acme v2'
                WHERE account_key = :k
            """), {"k": _FAKE_ACCOUNT_KEY})

        c2 = apply_core_diff(engine, run_id=2)
        assert c2.account_inserts == 0, "Should UPDATE, not INSERT"
        assert c2.account_updates >= 1

        with engine.connect() as conn:
            n = _row_count(conn, "core.account", "account_key", _FAKE_ACCOUNT_KEY)
            row = conn.execute(text(
                "SELECT legal_name FROM core.account WHERE account_key = :k"
            ), {"k": _FAKE_ACCOUNT_KEY}).fetchone()

        assert n == 1, "Still exactly one row after update"
        assert row[0] == "Acme v2", f"Expected 'Acme v2', got {row[0]!r}"

    def test_update_preserves_first_seen(self, clean_state):
        """first_seen must not change after an UPDATE (mutable fields only)."""
        from lib.core_writer import apply_core_diff
        from sqlalchemy import text
        engine = clean_state

        with engine.begin() as conn:
            _insert_resolved_account(conn, _FAKE_ACCOUNT_KEY, name="v1")
        apply_core_diff(engine, run_id=1)

        with engine.connect() as conn:
            first_seen_before = conn.execute(text(
                "SELECT first_seen FROM core.account WHERE account_key = :k"
            ), {"k": _FAKE_ACCOUNT_KEY}).scalar()

        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE staging.resolved_account
                SET legal_name = 'v2', name_normalized = 'v2'
                WHERE account_key = :k
            """), {"k": _FAKE_ACCOUNT_KEY})

        apply_core_diff(engine, run_id=2)

        with engine.connect() as conn:
            first_seen_after = conn.execute(text(
                "SELECT first_seen FROM core.account WHERE account_key = :k"
            ), {"k": _FAKE_ACCOUNT_KEY}).scalar()

        assert first_seen_before == first_seen_after, (
            "first_seen must not change on update"
        )
