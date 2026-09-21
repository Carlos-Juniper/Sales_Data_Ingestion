"""
Unit tests for lib.db helper functions — no live DB required.

Covers:
  C1  — intra-batch dedup in _prepare_rows (last occurrence wins)
  C2  — NaN→None cleaning across ALL columns, not just numeric ones
  C3  — _STAGING_COLS is the same object as CANONICAL_COLUMNS from lib.schema
  C4  — _assert_safe_identifier raises on unsafe SQL identifiers
  B5  — upsert_staging aborts when empty natural_key fraction exceeds threshold
"""

from __future__ import annotations

import math
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.db import _assert_safe_identifier, _prepare_rows, _STAGING_COLS
from lib.schema import CANONICAL_COLUMNS


# ---------------------------------------------------------------------------
# C3: CANONICAL_COLUMNS lockstep
# ---------------------------------------------------------------------------

class TestStagingColsLockstep:
    """_STAGING_COLS must be the canonical list, not an independent copy."""

    def test_staging_cols_is_canonical_columns(self):
        # After the C3 fix, _STAGING_COLS is just an alias for CANONICAL_COLUMNS.
        assert _STAGING_COLS is CANONICAL_COLUMNS

    def test_all_canonical_columns_present(self):
        # Belt-and-suspenders: the set contents must match exactly.
        assert set(_STAGING_COLS) == set(CANONICAL_COLUMNS)


# ---------------------------------------------------------------------------
# C1: Intra-batch deduplication
# ---------------------------------------------------------------------------

def _minimal_df(**overrides) -> pd.DataFrame:
    """Build the smallest valid canonical DataFrame for testing _prepare_rows."""
    data = {col: [None] for col in CANONICAL_COLUMNS}
    data["natural_key"] = ["key-A"]
    data["source_id"] = ["test_source"]
    data.update({k: [v] for k, v in overrides.items()})
    return pd.DataFrame(data)


class TestIntraBatchDedup:
    """C1 — batch with duplicate (source_id, natural_key) must produce one row."""

    def test_duplicate_key_keeps_last_occurrence(self):
        """Two rows with the same natural_key → only the last survives."""
        df = pd.DataFrame({
            col: [None, None] for col in CANONICAL_COLUMNS
        })
        df["natural_key"] = ["key-A", "key-A"]
        df["source_id"] = ["src", "src"]
        df["name_raw"] = ["first", "last"]

        rows = _prepare_rows(df, "src")

        assert len(rows) == 1, "Duplicate key must collapse to one row"
        assert rows[0]["name_raw"] == "last", "Last occurrence must win"

    def test_distinct_keys_all_preserved(self):
        """Three rows with distinct keys must all survive dedup."""
        df = pd.DataFrame({
            col: [None, None, None] for col in CANONICAL_COLUMNS
        })
        df["natural_key"] = ["key-A", "key-B", "key-C"]
        df["source_id"] = ["src", "src", "src"]

        rows = _prepare_rows(df, "src")

        assert len(rows) == 3

    def test_mixed_duplicates_and_unique(self):
        """One duplicate pair + one unique → two rows, last of pair wins."""
        df = pd.DataFrame({
            col: [None, None, None] for col in CANONICAL_COLUMNS
        })
        df["natural_key"] = ["key-A", "key-A", "key-B"]
        df["source_id"] = ["src", "src", "src"]
        df["name_raw"] = ["first-A", "last-A", "only-B"]

        rows = _prepare_rows(df, "src")

        assert len(rows) == 2
        by_key = {r["natural_key"]: r for r in rows}
        assert by_key["key-A"]["name_raw"] == "last-A"
        assert by_key["key-B"]["name_raw"] == "only-B"


# ---------------------------------------------------------------------------
# C2: NaN cleaning across all columns
# ---------------------------------------------------------------------------

class TestNanCleaning:
    """C2 — float NaN in any column must become None in the output dict."""

    def test_nan_in_text_column_becomes_none(self):
        """A NaN that arrived in a text column (e.g. from a join) must be None."""
        df = _minimal_df()
        # Simulate a float NaN landing in a text column (common after pandas joins).
        df["name_raw"] = float("nan")

        rows = _prepare_rows(df, "test_source")

        assert rows[0]["name_raw"] is None, (
            "float NaN in a text column must be converted to None"
        )

    def test_nan_in_numeric_column_becomes_none(self):
        """NaN in a numeric column (the original behaviour) still works."""
        df = _minimal_df()
        df["latitude"] = float("nan")

        rows = _prepare_rows(df, "test_source")

        assert rows[0]["latitude"] is None

    def test_valid_string_is_preserved(self):
        """A normal string must survive NaN cleaning unchanged."""
        df = _minimal_df()
        df["city"] = "Springfield"

        rows = _prepare_rows(df, "test_source")

        assert rows[0]["city"] == "Springfield"

    def test_none_in_text_column_stays_none(self):
        """An explicit Python None must not be disturbed."""
        df = _minimal_df()
        df["city"] = None

        rows = _prepare_rows(df, "test_source")

        assert rows[0]["city"] is None

    def test_nan_in_multiple_text_columns_all_cleaned(self):
        """NaN in several text columns must all become None."""
        df = _minimal_df()
        for col in ("city", "state", "ein", "segment"):
            df[col] = float("nan")

        rows = _prepare_rows(df, "test_source")

        for col in ("city", "state", "ein", "segment"):
            assert rows[0][col] is None, f"Expected None for column {col!r}"


# ---------------------------------------------------------------------------
# C4: SQL identifier whitelist
# ---------------------------------------------------------------------------

class TestAssertSafeIdentifier:
    """C4 — unsafe table names must raise ValueError before SQL interpolation."""

    @pytest.mark.parametrize("safe_name", [
        "cms_general",
        "nppes_practice_locations",
        "staging",
        "_private",
        "a",
        "table123",
    ])
    def test_valid_identifiers_pass(self, safe_name: str):
        # Should not raise.
        _assert_safe_identifier(safe_name)

    @pytest.mark.parametrize("bad_name", [
        "txdot:cemeteries",      # colon — the pre-D3 format
        "source-with-hyphens",   # hyphen not replaced before validation
        "1starts_with_digit",    # leading digit
        "has space",             # space
        "",                      # empty
        "CamelCase",             # uppercase
        "a;DROP TABLE staging;", # SQL injection attempt
        "staging.table",         # dot — schema separator
    ])
    def test_unsafe_identifiers_raise(self, bad_name: str):
        with pytest.raises(ValueError, match="Unsafe SQL identifier"):
            _assert_safe_identifier(bad_name)


# ---------------------------------------------------------------------------
# B5: Empty natural_key threshold guard
# ---------------------------------------------------------------------------

class TestEmptyNaturalKeyGuard:
    """B5 — upsert_staging must abort when too many natural_keys are empty."""

    def _make_engine_stub(self):
        """Return a lightweight stub Engine that absorbs DDL/DML calls."""

        class _FakeConn:
            def execute(self, *a, **kw):
                return self

            def fetchone(self):
                return (1,)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        class _FakeEngine:
            def begin(self):
                return _FakeConn()

            def connect(self):
                return _FakeConn()

        return _FakeEngine()

    def test_all_empty_natural_keys_raises(self):
        """All-empty natural_key must raise ValueError — not silently write one row."""
        from lib.db import upsert_staging

        df = pd.DataFrame({col: ["x"] for col in CANONICAL_COLUMNS})
        df["natural_key"] = ""   # 100 % empty → collapses to one row in DB

        with pytest.raises(ValueError, match="natural_key"):
            upsert_staging(self._make_engine_stub(), "test_source", df)

    def test_mostly_empty_natural_keys_raises(self):
        """50 % empty natural_keys (well above the 1 % default) must raise."""
        from lib.db import upsert_staging

        df = pd.DataFrame({col: ["x", "x"] for col in CANONICAL_COLUMNS})
        df["natural_key"] = ["", "real-key"]

        with pytest.raises(ValueError, match="natural_key"):
            upsert_staging(self._make_engine_stub(), "test_source", df)

    def test_all_populated_natural_keys_passes_guard(self):
        """All non-empty keys must not raise (DB call may still fail in stub, that's ok)."""
        from lib.db import upsert_staging

        df = pd.DataFrame({col: ["x", "y"] for col in CANONICAL_COLUMNS})
        df["natural_key"] = ["key-1", "key-2"]
        df["source_id"] = ["test_source", "test_source"]

        # The stub engine swallows the SQL — we only care that ValueError is not raised.
        try:
            upsert_staging(self._make_engine_stub(), "test_source", df)
        except ValueError as exc:
            if "natural_key" in str(exc):
                raise  # re-raise: the guard fired incorrectly
            # Other ValueError (e.g. unexpected stub behaviour) is fine to ignore.

    def test_custom_threshold_respected(self):
        """A looser custom threshold (10 %) must accept 5 % empty keys."""
        from lib.db import upsert_staging

        # 1 empty out of 20 = 5 %, which is above the default 1 % but below 10 %.
        rows = 20
        df = pd.DataFrame({col: ["v"] * rows for col in CANONICAL_COLUMNS})
        df["natural_key"] = [""] + [f"key-{i}" for i in range(rows - 1)]
        df["source_id"] = ["test_source"] * rows

        try:
            upsert_staging(
                self._make_engine_stub(),
                "test_source",
                df,
                max_empty_natural_key_fraction=0.10,
            )
        except ValueError as exc:
            if "natural_key" in str(exc):
                raise
