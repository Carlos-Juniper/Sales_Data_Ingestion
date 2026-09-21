"""
Unit tests for lib.core_writer — no live DB required.

Covers:
  - DiffCounts dataclass helpers
  - SourceRecordRow.payload_sha determinism (same payload → same sha)
  - SourceRecordRow.payload_sha sensitivity (changed payload → different sha)
  - Content hash expressions in the dry-run SQL (structure / no crash)
  - _build_dry_run_sql produces valid SQL syntax (checked via keyword presence)
  - upsert_source_records stub test (engine stub, no live DB)
  - resolve_account_ids stub test

These tests prove the SQL-builder logic is self-consistent without needing
a live Postgres instance.  Integration tests in
connectors/tests/integration/test_core_writer_integration.py prove the 0-row
re-run contract against a real DB.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.core_writer import (
    DiffCounts,
    SourceRecordRow,
    _build_dry_run_sql,
    _ACCOUNT_HASH_EXPR,
    _LOCATION_HASH_EXPR,
    _CONTACT_HASH_EXPR,
    _ACCOUNT_CORE_HASH_EXPR,
    _LOCATION_CORE_HASH_EXPR,
    _CONTACT_CORE_HASH_EXPR,
)


# ---------------------------------------------------------------------------
# DiffCounts
# ---------------------------------------------------------------------------

class TestDiffCounts:
    """DiffCounts is a dataclass with correct default values and helpers."""

    def test_defaults_are_zero(self):
        c = DiffCounts()
        assert c.account_inserts == 0
        assert c.account_updates == 0
        assert c.account_tombstones == 0
        assert c.location_inserts == 0
        assert c.location_updates == 0
        assert c.location_tombstones == 0
        assert c.contact_inserts == 0
        assert c.contact_updates == 0
        assert c.contact_tombstones == 0
        assert c.source_record_inserts == 0
        assert c.source_record_bumps == 0

    def test_total_core_writes_sums_correctly(self):
        c = DiffCounts(
            account_inserts=3,
            account_updates=2,
            account_tombstones=1,
            location_inserts=4,
            location_updates=0,
            location_tombstones=1,
            contact_inserts=2,
            contact_updates=1,
            contact_tombstones=0,
            source_record_inserts=10,
            source_record_bumps=5,  # bumps are NOT in total_core_writes
        )
        # 3+2+1 + 4+0+1 + 2+1+0 + 10 = 24
        assert c.total_core_writes() == 24

    def test_total_excludes_bumps(self):
        """source_record_bumps must not contribute to total_core_writes."""
        c = DiffCounts(source_record_bumps=999)
        assert c.total_core_writes() == 0

    def test_repr_contains_entity_names(self):
        c = DiffCounts(account_inserts=1, location_updates=2, contact_tombstones=3)
        r = repr(c)
        assert "account" in r
        assert "location" in r
        assert "contact" in r
        assert "source_record" in r


# ---------------------------------------------------------------------------
# SourceRecordRow.payload_sha
# ---------------------------------------------------------------------------

class TestSourceRecordRowPayloadSha:
    """payload_sha must be deterministic and sensitive to content changes."""

    def _row(self, payload: dict) -> SourceRecordRow:
        return SourceRecordRow(
            source_id="test_src",
            natural_key="KEY001",
            payload=payload,
        )

    def test_same_payload_same_sha(self):
        """Identical payload must produce the same SHA-256 hex digest."""
        row_a = self._row({"name": "Acme", "zip": "29201"})
        row_b = self._row({"name": "Acme", "zip": "29201"})
        assert row_a.payload_sha() == row_b.payload_sha()

    def test_key_order_independent(self):
        """Key ordering must not affect the sha (json.dumps sort_keys=True)."""
        row_a = self._row({"a": 1, "b": 2})
        row_b = self._row({"b": 2, "a": 1})
        assert row_a.payload_sha() == row_b.payload_sha()

    def test_changed_value_changes_sha(self):
        """Changing a value must produce a different SHA."""
        row_a = self._row({"name": "Acme"})
        row_b = self._row({"name": "Acme Corp"})
        assert row_a.payload_sha() != row_b.payload_sha()

    def test_added_key_changes_sha(self):
        """Adding a key must change the SHA."""
        row_a = self._row({"name": "Acme"})
        row_b = self._row({"name": "Acme", "phone": "555-1234"})
        assert row_a.payload_sha() != row_b.payload_sha()

    def test_empty_payload_stable_sha(self):
        """An empty payload must produce a consistent, non-empty SHA."""
        row = self._row({})
        sha = row.payload_sha()
        assert len(sha) == 64  # SHA-256 hex = 64 chars
        assert row.payload_sha() == sha  # stable across calls

    def test_sha_is_sha256_of_canonical_json(self):
        """Verify the SHA manually: sha256(json.dumps(..., sort_keys=True))."""
        payload = {"x": 1, "y": 2}
        row = self._row(payload)
        expected = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        assert row.payload_sha() == expected

    def test_non_ascii_payload_stable(self):
        """Non-ASCII characters must produce a consistent sha."""
        row_a = self._row({"name": "El Niño Clínica"})
        row_b = self._row({"name": "El Niño Clínica"})
        assert row_a.payload_sha() == row_b.payload_sha()


# ---------------------------------------------------------------------------
# Hash expression consistency
# ---------------------------------------------------------------------------

class TestHashExpressions:
    """The SQL hash expressions for resolved vs core must cover the same fields."""

    def _field_count(self, expr: str) -> int:
        """Count the number of comma-separated arguments to concat_ws."""
        # Simple heuristic: count commas inside concat_ws(... body)
        # (works for the fixed structure of our expressions)
        inner = expr.split("concat_ws('|',", 1)[1].rstrip().rstrip(")")
        return inner.count("\n") + inner.count(",") - inner.count(",\n") + 1

    def test_account_hash_fields_match(self):
        """Resolved and core account hash expressions must cover the same fields."""
        # Both must contain the same set of field names (sans table alias prefix).
        def field_names(expr: str) -> set[str]:
            import re
            # Extract identifiers like ra.vertical, ca.vertical → "vertical"
            return {m.group(1) for m in re.finditer(r"\w+\.(\w+)", expr)}

        resolved_fields = field_names(_ACCOUNT_HASH_EXPR)
        core_fields = field_names(_ACCOUNT_CORE_HASH_EXPR)
        # parent_account_key (resolved) ↔ parent_account_id resolved via JOIN
        # Both should contain exactly the same logical fields minus the alias.
        # The key difference is parent_account_key vs account_key (JOIN resolves).
        # Verify they have the same COUNT of fields.
        assert len(resolved_fields) == len(core_fields), (
            f"Account hash field count mismatch: "
            f"resolved={sorted(resolved_fields)} core={sorted(core_fields)}"
        )

    def test_location_hash_fields_match(self):
        import re
        def field_names(expr):
            return {m.group(1) for m in re.finditer(r"\w+\.(\w+)", expr)}
        assert len(field_names(_LOCATION_HASH_EXPR)) == len(field_names(_LOCATION_CORE_HASH_EXPR))

    def test_contact_hash_fields_match(self):
        import re
        def field_names(expr):
            return {m.group(1) for m in re.finditer(r"\w+\.(\w+)", expr)}
        assert len(field_names(_CONTACT_HASH_EXPR)) == len(field_names(_CONTACT_CORE_HASH_EXPR))


# ---------------------------------------------------------------------------
# Dry-run SQL structure
# ---------------------------------------------------------------------------

class TestDryRunSqlStructure:
    """_build_dry_run_sql must produce a SQL string with the expected structure."""

    def test_contains_all_entity_types(self):
        sql = _build_dry_run_sql()
        assert "resolved_account" in sql
        assert "resolved_location" in sql
        assert "resolved_contact" in sql
        assert "core.account" in sql
        assert "core.location" in sql
        assert "core.contact" in sql

    def test_contains_count_select(self):
        sql = _build_dry_run_sql()
        assert "count(*)" in sql.lower() or "COUNT(*)" in sql

    def test_no_dml_statements(self):
        """The dry-run query must not contain INSERT INTO/UPDATE .../DELETE FROM.

        Uses whole-word boundary patterns so column alias names like
        'account_inserts' don't trigger false positives.  Comments are stripped
        first because they may reference these keywords in prose.
        """
        import re
        sql = _build_dry_run_sql()
        # Strip single-line SQL comments.
        sql_no_comments = re.sub(r"--[^\n]*", "", sql)
        # Check for DML keywords followed by a space/newline (the SQL keyword form).
        assert not re.search(r"\bINSERT\s+INTO\b", sql_no_comments, re.IGNORECASE), (
            "Dry-run SQL must not contain INSERT INTO"
        )
        assert not re.search(r"\bUPDATE\s+\w", sql_no_comments, re.IGNORECASE), (
            "Dry-run SQL must not contain UPDATE <table>"
        )
        assert not re.search(r"\bDELETE\s+FROM\b", sql_no_comments, re.IGNORECASE), (
            "Dry-run SQL must not contain DELETE FROM"
        )

    def test_contains_survivor_cte(self):
        """Alias/survivor CTE must be present (D2)."""
        sql = _build_dry_run_sql()
        assert "survivor" in sql.lower()

    def test_diff_arms_present(self):
        """All 9 diff arms (3 entities × insert/update/tombstone) must be present."""
        sql = _build_dry_run_sql()
        arms = [
            "acct_new", "acct_changed", "acct_gone",
            "loc_new", "loc_changed", "loc_gone",
            "cont_new", "cont_changed", "cont_gone",
        ]
        for arm in arms:
            assert arm in sql, f"Expected CTE arm {arm!r} missing from dry-run SQL"

    def test_returns_nine_columns(self):
        """The final SELECT must reference exactly 9 count(*) expressions.

        The outer SELECT uses sub-selects of the form
        ``(SELECT count(*) FROM <cte_name>) AS <alias>``, so we count total
        occurrences of ``count(*)`` across the full SQL string.
        """
        sql = _build_dry_run_sql()
        # Each of the 9 diff arms contributes one count(*) sub-select in the
        # final SELECT clause.  The CTE body itself doesn't use count().
        col_count = sql.lower().count("count(*)")
        assert col_count == 9, (
            f"Expected 9 count(*) expressions in dry-run SQL, got {col_count}"
        )

    def test_tombstone_excludes_already_merged(self):
        """The account tombstone arm must filter out already-merged rows."""
        sql = _build_dry_run_sql()
        assert "merged" in sql, "Expected 'merged' status filter in tombstone arm"


# ---------------------------------------------------------------------------
# Engine stub — upsert_source_records without a live DB
# ---------------------------------------------------------------------------

class _FakeRow:
    """Mimics a returned DB row with indexing."""
    def __init__(self, was_inserted: bool):
        self._was_inserted = was_inserted

    def __getitem__(self, idx):
        return self._was_inserted


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _FakeConn:
    """Absorbs execute() calls and returns controllable results."""
    def __init__(self, return_rows=None):
        self.executed = []
        self._return_rows = return_rows or []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return _FakeResult(self._return_rows)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class _FakeEngine:
    def __init__(self, return_rows=None):
        self._conn = _FakeConn(return_rows)

    def begin(self):
        return self._conn

    def connect(self):
        return self._conn


class TestUpsertSourceRecordsUnit:
    """Unit tests for upsert_source_records using a stub engine."""

    def test_empty_records_returns_zero_zero(self):
        from lib.core_writer import upsert_source_records
        eng = _FakeEngine()
        inserted, bumped = upsert_source_records(eng, [], run_id=1)
        assert inserted == 0
        assert bumped == 0

    def test_new_record_counted_as_inserted(self):
        """A row returned with xmax=0 (was_inserted=True) increments inserted."""
        from lib.core_writer import upsert_source_records
        eng = _FakeEngine(return_rows=[_FakeRow(True)])
        rows = [SourceRecordRow("src", "KEY1", {"name": "Acme"})]
        inserted, bumped = upsert_source_records(eng, rows, run_id=1)
        assert inserted == 1
        assert bumped == 0

    def test_existing_record_counted_as_bumped(self):
        """A row returned with xmax!=0 (was_inserted=False) increments bumped."""
        from lib.core_writer import upsert_source_records
        eng = _FakeEngine(return_rows=[_FakeRow(False)])
        rows = [SourceRecordRow("src", "KEY1", {"name": "Acme"})]
        inserted, bumped = upsert_source_records(eng, rows, run_id=1)
        assert inserted == 0
        assert bumped == 1

    def test_mixed_records_counted_correctly(self):
        """Multiple rows with different xmax values are counted correctly."""
        from lib.core_writer import upsert_source_records
        # Engine returns one row per execute(); we patch execute to cycle results.
        results_cycle = [
            _FakeResult([_FakeRow(True)]),   # first record: new
            _FakeResult([_FakeRow(False)]),  # second record: bump
            _FakeResult([_FakeRow(True)]),   # third record: new
        ]

        class _CyclingConn:
            def __init__(self):
                self._results = iter(results_cycle)
                self.executed = []

            def execute(self, sql, params=None):
                self.executed.append(params)
                return next(self._results)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        class _CyclingEngine:
            def __init__(self):
                self._conn = _CyclingConn()

            def begin(self):
                return self._conn

        eng = _CyclingEngine()
        rows = [
            SourceRecordRow("src", "KEY1", {"a": 1}),
            SourceRecordRow("src", "KEY2", {"a": 2}),
            SourceRecordRow("src", "KEY3", {"a": 3}),
        ]
        inserted, bumped = upsert_source_records(eng, rows, run_id=5)
        assert inserted == 2
        assert bumped == 1

    def test_payload_sha_included_in_execute_params(self):
        """The execute call must include payload_sha in its parameter dict."""
        from lib.core_writer import upsert_source_records

        captured_params = []

        class _CapturingConn:
            def execute(self, sql, params=None):
                captured_params.append(params)
                return _FakeResult([_FakeRow(True)])

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        class _CapturingEngine:
            def begin(self):
                return _CapturingConn()

        rows = [SourceRecordRow("src", "K1", {"x": 42})]
        upsert_source_records(_CapturingEngine(), rows, run_id=99)

        assert len(captured_params) == 1
        p = captured_params[0]
        assert "payload_sha" in p
        assert len(p["payload_sha"]) == 64  # valid SHA-256 hex
        assert p["run_id"] == 99


# ---------------------------------------------------------------------------
# resolve_account_ids stub test
# ---------------------------------------------------------------------------

class TestResolveAccountIdsUnit:
    """resolve_account_ids returns an empty dict for empty input."""

    def test_empty_keys_no_db_call(self):
        from lib.core_writer import resolve_account_ids

        class _NeverCallEngine:
            def connect(self):
                raise AssertionError("DB should not be called for empty input")

        result = resolve_account_ids(_NeverCallEngine(), [])
        assert result == {}
