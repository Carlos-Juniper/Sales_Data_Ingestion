"""
Unit tests for four shared lib modules:

    lib/validate   – assert_columns_present, assert_fill_rate, assert_min_rows
    lib/match      – name_similarity, edit_distance
    lib/schema     – CANONICAL_COLUMNS, validate_canonical, build_canonical
    lib/enrich_runner – run_enrichment (serial and parallel paths)

No external I/O is exercised.  All tests are pure in-memory.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import pytest

from lib.enrich_runner import run_enrichment
from lib.match import edit_distance, name_similarity
from lib.schema import CANONICAL_COLUMNS, build_canonical, validate_canonical
from lib.validate import assert_columns_present, assert_fill_rate, assert_min_rows


# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------


def _make_df(**kwargs) -> pd.DataFrame:
    """Thin factory: keyword args become column-name → list-of-values."""
    return pd.DataFrame(kwargs)


# ---------------------------------------------------------------------------
# TestValidate
# ---------------------------------------------------------------------------


class TestValidate:
    # --- assert_columns_present ---

    def test_assert_columns_present_passes_when_all_required_present(self):
        df = _make_df(a=[1, 2], b=[3, 4], c=[5, 6])

        # No exception expected — all three columns are in the DataFrame.
        assert_columns_present(df, ["a", "b", "c"])

    def test_assert_columns_present_passes_when_required_is_subset(self):
        df = _make_df(a=[1], b=[2], extra=[3])

        assert_columns_present(df, ["a", "b"])

    def test_assert_columns_present_raises_when_one_column_missing(self):
        df = _make_df(a=[1, 2])

        with pytest.raises(ValueError, match="missing required columns"):
            assert_columns_present(df, ["a", "z"])

    def test_assert_columns_present_error_lists_missing_column_name(self):
        df = _make_df(a=[1])

        with pytest.raises(ValueError, match="z"):
            assert_columns_present(df, ["a", "z"])

    def test_assert_columns_present_includes_label_in_message_when_given(self):
        df = _make_df(a=[1])

        with pytest.raises(ValueError, match="my_source"):
            assert_columns_present(df, ["a", "missing_col"], label="my_source")

    def test_assert_columns_present_passes_with_empty_required_list(self):
        df = _make_df(a=[1])

        # No required columns → always passes.
        assert_columns_present(df, [])

    # --- assert_fill_rate ---

    def test_assert_fill_rate_passes_when_fill_equals_threshold(self):
        # 3 non-null out of 3 → 100 % fill; threshold 1.0 → exactly at boundary.
        # Boundary condition: fill < threshold raises; fill == threshold passes.
        df = _make_df(col=["a", "b", "c"])

        assert_fill_rate(df, "col", threshold=1.0)

    def test_assert_fill_rate_passes_when_fill_above_threshold(self):
        # 3 non-null out of 4 = 0.75; threshold 0.70 → above.
        df = _make_df(col=["a", "b", "c", None])

        assert_fill_rate(df, "col", threshold=0.70)

    def test_assert_fill_rate_raises_when_fill_below_threshold(self):
        # 1 non-null out of 4 = 0.25; threshold 0.50 → below.
        df = _make_df(col=["a", None, None, None])

        with pytest.raises(ValueError, match="populated"):
            assert_fill_rate(df, "col", threshold=0.50)

    def test_assert_fill_rate_raises_just_below_threshold(self):
        # Pinning the exact boundary: fill < threshold (strict less-than).
        # 2 non-null out of 4 = 0.50; threshold 0.51 → just below.
        df = _make_df(col=["a", "b", None, None])

        with pytest.raises(ValueError):
            assert_fill_rate(df, "col", threshold=0.51)

    def test_assert_fill_rate_passes_at_exactly_half(self):
        # 2/4 = 0.50 == threshold 0.50 → should NOT raise (condition is strict <).
        df = _make_df(col=["a", "b", None, None])

        assert_fill_rate(df, "col", threshold=0.50)

    def test_assert_fill_rate_includes_label_in_message(self):
        df = _make_df(col=[None, None])

        with pytest.raises(ValueError, match="my_stage"):
            assert_fill_rate(df, "col", threshold=0.50, label="my_stage")

    # --- assert_min_rows ---

    def test_assert_min_rows_passes_when_row_count_equals_minimum(self):
        # Boundary: exactly at minimum → must pass (condition is strict <).
        df = _make_df(a=[1, 2, 3])

        assert_min_rows(df, minimum=3)

    def test_assert_min_rows_passes_when_row_count_above_minimum(self):
        df = _make_df(a=[1, 2, 3, 4])

        assert_min_rows(df, minimum=3)

    def test_assert_min_rows_raises_when_fewer_rows_than_minimum(self):
        df = _make_df(a=[1, 2])

        with pytest.raises(ValueError, match="rows returned"):
            assert_min_rows(df, minimum=3)

    def test_assert_min_rows_raises_on_empty_dataframe(self):
        df = pd.DataFrame({"a": []})

        with pytest.raises(ValueError):
            assert_min_rows(df, minimum=1)

    def test_assert_min_rows_includes_label_in_message(self):
        df = _make_df(a=[1])

        with pytest.raises(ValueError, match="parcel_stage"):
            assert_min_rows(df, minimum=5, label="parcel_stage")


# ---------------------------------------------------------------------------
# TestMatch
# ---------------------------------------------------------------------------


class TestMatch:
    # --- name_similarity ---

    def test_name_similarity_both_empty_strings_returns_zero(self):
        # Two unnamed sites have no name evidence to confirm identity.
        assert name_similarity("", "") == 0.0

    def test_name_similarity_one_empty_string_returns_zero(self):
        assert name_similarity("Oak Hill", "") == 0.0

    def test_name_similarity_none_inputs_return_zero(self):
        assert name_similarity(None, None) == 0.0

    def test_name_similarity_identical_strings_returns_one(self):
        assert name_similarity("foo", "foo") == pytest.approx(1.0)

    def test_name_similarity_long_identical_strings_returns_one(self):
        assert name_similarity(
            "Sunset Memorial Gardens Cemetery",
            "Sunset Memorial Gardens Cemetery",
        ) == pytest.approx(1.0)

    def test_name_similarity_partial_overlap_returns_value_between_zero_and_one(self):
        result = name_similarity("Oak Hill Cemetery", "Oak Hill Memorial")
        assert 0.0 < result < 1.0

    def test_name_similarity_completely_different_strings_is_below_half(self):
        result = name_similarity("aaa", "zzz")
        assert result < 0.5

    # --- Threshold boundary tests (the 0.80 "high" cutoff used by deathcare_merge) ---
    # name_similarity itself returns a float; the 'high' label is applied by the
    # caller (deathcare_merge._NAME_SIMILARITY_THRESHOLD = 0.80, condition: >= 0.80).
    # These tests verify the float output of name_similarity sits on the expected
    # side of that boundary so the caller will classify correctly.

    def test_name_similarity_above_0_80_threshold(self):
        # "Green Acres Cemetery" vs "Green Acres Cemetary" — one-character typo.
        # edit_distance = 1, max_len = 20 → similarity = 1 - 1/20 = 0.95
        a = "Green Acres Cemetery"
        b = "Green Acres Cemetary"
        result = name_similarity(a, b)
        assert result >= 0.80, f"Expected >= 0.80 but got {result:.4f}"

    def test_name_similarity_below_0_80_threshold(self):
        # "Oak" vs "Elm" — edit_distance = 3, max_len = 3 → similarity = 0.0
        result = name_similarity("Oak", "Elm")
        assert result < 0.80, f"Expected < 0.80 but got {result:.4f}"

    @pytest.mark.parametrize(
        "a,b,expected_ge_threshold",
        [
            # high-confidence pair: one-letter difference in a long name
            ("Pleasant View Cemetery", "Pleasant View Cemetary", True),
            # low-confidence pair: completely different names
            ("Rose Hill", "Valley View", False),
        ],
    )
    def test_name_similarity_threshold_parametrize(self, a, b, expected_ge_threshold):
        result = name_similarity(a, b)
        if expected_ge_threshold:
            assert result >= 0.80
        else:
            assert result < 0.80

    # --- edit_distance ---

    def test_edit_distance_same_string_returns_zero(self):
        assert edit_distance("cemetery", "cemetery") == 0

    def test_edit_distance_empty_strings_returns_zero(self):
        assert edit_distance("", "") == 0

    def test_edit_distance_single_substitution_returns_one(self):
        # "cat" → "bat": one substitution.
        assert edit_distance("cat", "bat") == 1

    def test_edit_distance_single_insertion_returns_one(self):
        # "cat" → "cats": one insertion.
        assert edit_distance("cat", "cats") == 1

    def test_edit_distance_single_deletion_returns_one(self):
        # "cats" → "cat": one deletion.
        assert edit_distance("cats", "cat") == 1

    def test_edit_distance_empty_to_nonempty_equals_length(self):
        assert edit_distance("", "hello") == 5

    def test_edit_distance_nonempty_to_empty_equals_length(self):
        assert edit_distance("hello", "") == 5

    def test_edit_distance_is_symmetric(self):
        assert edit_distance("kitten", "sitting") == edit_distance("sitting", "kitten")

    def test_edit_distance_known_value_kitten_sitting(self):
        # Classic example: edit_distance("kitten", "sitting") == 3.
        assert edit_distance("kitten", "sitting") == 3


# ---------------------------------------------------------------------------
# TestSchema
# ---------------------------------------------------------------------------


class TestSchema:
    # --- CANONICAL_COLUMNS membership and length ---

    def test_canonical_columns_has_exactly_21_entries(self):
        assert len(CANONICAL_COLUMNS) == 21

    @pytest.mark.parametrize(
        "col",
        [
            "source_id",
            "natural_key",
            "name_raw",
            "name_normalized",
            "latitude",
            "longitude",
            "state",
            "county_fips",
            "source_file",
        ],
    )
    def test_canonical_columns_contains_key_column(self, col):
        assert col in CANONICAL_COLUMNS, f"Expected '{col}' in CANONICAL_COLUMNS"

    def test_canonical_columns_contains_all_expected_entries(self):
        expected = {
            "source_id", "natural_key", "vertical", "account_type",
            "name_raw", "name_normalized", "address_line_1", "city",
            "state", "zip5", "phone_raw", "phone_normalized",
            "latitude", "longitude", "segment", "ein", "county_fips",
            "size_metric", "size_value", "size_unit", "source_file",
        }
        assert set(CANONICAL_COLUMNS) == expected

    # --- validate_canonical ---

    def test_validate_canonical_passes_on_correctly_shaped_dataframe(self):
        # Arrange: build a DataFrame with every canonical column present.
        df = pd.DataFrame({col: [] for col in CANONICAL_COLUMNS})

        # Act / Assert: no exception raised.
        validate_canonical(df)

    def test_validate_canonical_raises_on_missing_column(self):
        cols_minus_one = [c for c in CANONICAL_COLUMNS if c != "source_id"]
        df = pd.DataFrame({col: [] for col in cols_minus_one})

        with pytest.raises(ValueError, match="source_id"):
            validate_canonical(df)

    def test_validate_canonical_error_message_names_all_missing_columns(self):
        # Drop two columns; both should appear in the error message.
        cols_minus_two = [c for c in CANONICAL_COLUMNS if c not in ("source_id", "ein")]
        df = pd.DataFrame({col: [] for col in cols_minus_two})

        with pytest.raises(ValueError) as exc_info:
            validate_canonical(df)

        msg = str(exc_info.value)
        assert "source_id" in msg
        assert "ein" in msg

    def test_validate_canonical_raises_on_extra_columns_only(self):
        # A DataFrame with extra columns but missing canonical ones must still raise.
        df = pd.DataFrame({"extra_col": [1, 2, 3]})

        with pytest.raises(ValueError):
            validate_canonical(df)

    # --- build_canonical ---

    def test_build_canonical_returns_exactly_canonical_columns(self):
        index = pd.RangeIndex(3)

        result = build_canonical(index)

        assert list(result.columns) == CANONICAL_COLUMNS

    def test_build_canonical_row_count_matches_index(self):
        index = pd.RangeIndex(5)

        result = build_canonical(index)

        assert len(result) == 5

    def test_build_canonical_missing_kwargs_default_to_none(self):
        index = pd.RangeIndex(2)

        result = build_canonical(index)

        # Every cell should be NaN/None since no kwargs were supplied.
        assert result.isna().all().all()

    def test_build_canonical_scalar_value_broadcasts_across_all_rows(self):
        index = pd.RangeIndex(4)

        result = build_canonical(index, vertical="deathcare")

        assert (result["vertical"] == "deathcare").all()

    def test_build_canonical_list_value_populates_per_row(self):
        index = pd.RangeIndex(3)

        result = build_canonical(index, source_id=["a", "b", "c"])

        assert list(result["source_id"]) == ["a", "b", "c"]

    def test_build_canonical_extra_kwargs_not_in_canonical_are_dropped(self):
        # build_canonical returns df[CANONICAL_COLUMNS], so extra kwargs are
        # silently dropped — the extra column is never present in output.
        index = pd.RangeIndex(2)

        result = build_canonical(index, not_a_canonical_col="should_vanish")

        assert "not_a_canonical_col" not in result.columns
        assert list(result.columns) == CANONICAL_COLUMNS

    def test_build_canonical_accepts_custom_pandas_index(self):
        index = pd.Index(["site_001", "site_002"])

        result = build_canonical(index)

        assert list(result.index) == ["site_001", "site_002"]


# ---------------------------------------------------------------------------
# TestEnrichRunner
# ---------------------------------------------------------------------------


class TestEnrichRunner:
    """Tests for run_enrichment — both serial (workers=1) and parallel (workers=2)."""

    @staticmethod
    def _make_rows(n: int) -> list[tuple[int, int]]:
        """Return n (idx, idx) tuples so fn can embed the original idx in the result."""
        return [(i, i) for i in range(n)]

    @staticmethod
    def _identity_fn(row: int) -> dict:
        """Mock enrichment function — echoes the row value back as the result payload."""
        return {"status": "ok", "idx": row}

    # --- Serial path (workers=1) ---

    def test_serial_processes_all_rows(self):
        rows = self._make_rows(5)

        results = run_enrichment(rows, self._identity_fn, workers=1)

        assert len(results) == 5

    def test_serial_returns_results_in_original_order(self):
        rows = self._make_rows(5)

        results = run_enrichment(rows, self._identity_fn, workers=1)

        for expected_idx, (actual_idx, result) in enumerate(results):
            assert actual_idx == expected_idx
            assert result["idx"] == expected_idx

    def test_serial_result_contains_correct_idx_keys(self):
        rows = self._make_rows(3)

        results = run_enrichment(rows, self._identity_fn, workers=1)

        returned_idxs = [idx for idx, _ in results]
        assert returned_idxs == [0, 1, 2]

    def test_serial_returns_empty_list_for_empty_input(self):
        results = run_enrichment([], self._identity_fn, workers=1)

        assert results == []

    def test_serial_single_row(self):
        rows = [(42, 42)]

        results = run_enrichment(rows, self._identity_fn, workers=1)

        assert len(results) == 1
        idx, result = results[0]
        assert idx == 42
        assert result == {"status": "ok", "idx": 42}

    # --- Parallel path (workers=2) ---

    def test_parallel_processes_all_rows(self):
        rows = self._make_rows(10)

        results = run_enrichment(rows, self._identity_fn, workers=2)

        assert len(results) == 10

    def test_parallel_returns_results_in_original_order(self):
        rows = self._make_rows(10)

        results = run_enrichment(rows, self._identity_fn, workers=2)

        for expected_idx, (actual_idx, result) in enumerate(results):
            assert actual_idx == expected_idx
            assert result["idx"] == expected_idx

    def test_parallel_result_contains_correct_idx_keys(self):
        rows = self._make_rows(6)

        results = run_enrichment(rows, self._identity_fn, workers=2)

        returned_idxs = [idx for idx, _ in results]
        assert returned_idxs == list(range(6))

    def test_parallel_returns_empty_list_for_empty_input(self):
        results = run_enrichment([], self._identity_fn, workers=2)

        assert results == []

    def test_parallel_single_row(self):
        rows = [(99, 99)]

        results = run_enrichment(rows, self._identity_fn, workers=2)

        assert len(results) == 1
        idx, result = results[0]
        assert idx == 99
        assert result == {"status": "ok", "idx": 99}

    def test_parallel_result_status_ok_for_all_rows(self):
        rows = self._make_rows(8)

        results = run_enrichment(rows, self._identity_fn, workers=2)

        assert all(result["status"] == "ok" for _, result in results)

    # --- workers=1 vs workers=2 produce identical output ---

    def test_serial_and_parallel_produce_identical_output(self):
        rows = self._make_rows(7)

        serial_results = run_enrichment(rows, self._identity_fn, workers=1)
        parallel_results = run_enrichment(rows, self._identity_fn, workers=2)

        assert serial_results == parallel_results

    # --- label and progress_interval params do not affect results ---

    def test_label_and_progress_interval_do_not_affect_output(self, capsys):
        rows = self._make_rows(3)

        results = run_enrichment(
            rows,
            self._identity_fn,
            workers=1,
            label="test_stage",
            progress_interval=1,
        )

        assert len(results) == 3
        # Progress lines are written to stderr; confirm no stdout pollution.
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_label_appears_in_stderr_progress_output(self, capsys):
        rows = self._make_rows(2)

        run_enrichment(
            rows,
            self._identity_fn,
            workers=1,
            label="my_enricher",
            progress_interval=1,
        )

        captured = capsys.readouterr()
        assert "my_enricher" in captured.err
