"""
Unit tests for connectors/healthcare/nppes_practice_locations.py.

All tests operate on small in-memory DataFrames — no file I/O occurs.
Taxonomy filtering logic is tested directly via filter_to_facility_orgs()
and its helpers, which are pure DataFrame transformations.
"""

from __future__ import annotations

import io
import os
import sys

# Ensure the connectors root is on the import path so `lib.*` and
# `healthcare.*` both resolve when running pytest from any directory.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pandas as pd
import pytest

from healthcare.nppes_practice_locations import (
    _PL_COL_ADDR1,
    _PL_COL_CITY,
    _PL_COL_STATE,
    _PL_COL_ZIP,
    _TAXONOMY_COLS,
    assert_source_shape,
    filter_to_facility_orgs,
    normalize_and_join,
    report_quality,
)


# ---------------------------------------------------------------------------
# Fixtures — shared small DataFrames
# ---------------------------------------------------------------------------


def _make_main_df(rows: list[dict]) -> pd.DataFrame:
    """
    Build a main_df-shaped DataFrame from a list of dicts.

    Each dict may supply any subset of the real main columns; missing
    columns are filled with empty strings so filter_to_facility_orgs()
    can iterate over the taxonomy columns without KeyError.
    """
    base: dict[str, list] = {
        "NPI": [],
        "Entity Type Code": [],
        "Provider Organization Name (Legal Business Name)": [],
    }
    for col in _TAXONOMY_COLS:
        base[col] = []

    for row in rows:
        for key in base:
            base[key].append(row.get(key, ""))

    return pd.DataFrame(base)


def _make_pl_df(rows: list[dict]) -> pd.DataFrame:
    """
    Build a pl_df-shaped DataFrame with all required columns present.

    Missing fields default to empty string.
    """
    required_cols = [
        "NPI",
        _PL_COL_ADDR1,
        _PL_COL_CITY,
        _PL_COL_STATE,
        _PL_COL_ZIP,
    ]
    data: dict[str, list] = {col: [] for col in required_cols}
    for row in rows:
        for col in required_cols:
            data[col].append(row.get(col, ""))
    return pd.DataFrame(data)


# ---------------------------------------------------------------------------
# normalize_and_join
# ---------------------------------------------------------------------------


class TestNormalizeAndJoin:
    """Tests for the join and normalization logic."""

    def test_produces_one_row_per_secondary_location(self):
        """Two NPIs with 2+1 pl_ rows produce 3 output rows."""
        main_df = _make_main_df([
            {"NPI": "1111111111", "Entity Type Code": "2",
             "Healthcare Provider Taxonomy Code_1": "261QM0801X"},
            {"NPI": "2222222222", "Entity Type Code": "2",
             "Healthcare Provider Taxonomy Code_1": "282N00000X"},
        ])
        # filter_to_facility_orgs is applied before normalize_and_join in
        # the real pipeline; here we simulate having already filtered.
        main_slim = main_df[["NPI", "Provider Organization Name (Legal Business Name)"]].copy()
        main_slim["taxonomy_primary"] = "261QM0801X"

        pl_df = _make_pl_df([
            {"NPI": "1111111111", _PL_COL_ADDR1: "100 Main St", _PL_COL_STATE: "FL", _PL_COL_ZIP: "33001"},
            {"NPI": "1111111111", _PL_COL_ADDR1: "200 Oak Ave", _PL_COL_STATE: "FL", _PL_COL_ZIP: "33002"},
            {"NPI": "2222222222", _PL_COL_ADDR1: "300 Pine Rd", _PL_COL_STATE: "TX", _PL_COL_ZIP: "75001"},
        ])

        result = normalize_and_join(main_slim, pl_df)
        assert len(result) == 3

    def test_natural_key_format_is_npi_pipe_seq(self):
        """First row for NPI 1234567890 should produce natural_key '1234567890|1'."""
        main_slim = pd.DataFrame({
            "NPI": ["1234567890"],
            "Provider Organization Name (Legal Business Name)": ["Test Org"],
            "taxonomy_primary": ["261QM0801X"],
        })
        pl_df = _make_pl_df([
            {"NPI": "1234567890", _PL_COL_ADDR1: "1 Test Blvd",
             _PL_COL_STATE: "CA", _PL_COL_ZIP: "90001"},
        ])

        result = normalize_and_join(main_slim, pl_df)
        assert result.iloc[0]["natural_key"] == "1234567890|1"

    def test_location_seq_restarts_per_npi(self):
        """NPI-A gets seq 1 and 2; NPI-B gets seq 1."""
        main_slim = pd.DataFrame({
            "NPI": ["AAAAAAAAAA", "BBBBBBBBBB"],
            "Provider Organization Name (Legal Business Name)": ["Org A", "Org B"],
            "taxonomy_primary": ["261QM0801X", "282N00000X"],
        })
        pl_df = _make_pl_df([
            {"NPI": "AAAAAAAAAA", _PL_COL_ADDR1: "1 A St", _PL_COL_STATE: "FL", _PL_COL_ZIP: "33001"},
            {"NPI": "AAAAAAAAAA", _PL_COL_ADDR1: "2 A St", _PL_COL_STATE: "FL", _PL_COL_ZIP: "33002"},
            {"NPI": "BBBBBBBBBB", _PL_COL_ADDR1: "1 B St", _PL_COL_STATE: "TX", _PL_COL_ZIP: "75001"},
        ])

        result = normalize_and_join(main_slim, pl_df)
        a_rows = result[result["npi"] == "AAAAAAAAAA"].sort_values("location_seq")
        b_rows = result[result["npi"] == "BBBBBBBBBB"]

        assert list(a_rows["location_seq"]) == [1, 2]
        assert list(b_rows["location_seq"]) == [1]

    def test_inner_join_excludes_pl_rows_with_unknown_npi(self):
        """A pl_ row whose NPI is not in main_df is excluded from output."""
        main_slim = pd.DataFrame({
            "NPI": ["1111111111"],
            "Provider Organization Name (Legal Business Name)": ["Known Org"],
            "taxonomy_primary": ["261QM0801X"],
        })
        pl_df = _make_pl_df([
            {"NPI": "1111111111", _PL_COL_ADDR1: "1 Known St",
             _PL_COL_STATE: "FL", _PL_COL_ZIP: "33001"},
            # NPI not in main_df — must be dropped by the inner join.
            {"NPI": "9999999999", _PL_COL_ADDR1: "1 Unknown St",
             _PL_COL_STATE: "CA", _PL_COL_ZIP: "90001"},
        ])

        result = normalize_and_join(main_slim, pl_df)
        assert len(result) == 1
        assert result.iloc[0]["npi"] == "1111111111"

    def test_zip5_stripped_from_9digit_postal_code(self):
        """Postal code '330265213' becomes zip5 '33026'."""
        main_slim = pd.DataFrame({
            "NPI": ["1234567890"],
            "Provider Organization Name (Legal Business Name)": ["Zip Test Org"],
            "taxonomy_primary": ["261QM0801X"],
        })
        pl_df = _make_pl_df([
            {"NPI": "1234567890", _PL_COL_ADDR1: "1 Zip Blvd",
             _PL_COL_STATE: "FL", _PL_COL_ZIP: "330265213"},
        ])

        result = normalize_and_join(main_slim, pl_df)
        assert result.iloc[0]["zip5"] == "33026"

    def test_output_columns_match_expected_schema(self):
        """Output DataFrame has exactly the required columns in order."""
        expected_cols = [
            "natural_key", "npi", "location_seq", "name_raw",
            "address_line_1", "city", "site_state", "zip5", "taxonomy_primary",
        ]
        main_slim = pd.DataFrame({
            "NPI": ["1234567890"],
            "Provider Organization Name (Legal Business Name)": ["Schema Org"],
            "taxonomy_primary": ["261QM0801X"],
        })
        pl_df = _make_pl_df([
            {"NPI": "1234567890", _PL_COL_ADDR1: "1 Schema St",
             _PL_COL_STATE: "FL", _PL_COL_ZIP: "33001"},
        ])

        result = normalize_and_join(main_slim, pl_df)
        assert list(result.columns) == expected_cols


# ---------------------------------------------------------------------------
# assert_source_shape
# ---------------------------------------------------------------------------


class TestAssertSourceShape:
    def test_raises_value_error_when_pl_df_missing_address_column(self):
        """Drop the address column — should raise ValueError with a clear message."""
        main_df = pd.DataFrame({
            "NPI": ["1111111111"],
            "Provider Organization Name (Legal Business Name)": ["Org A"],
            "taxonomy_primary": ["261QM0801X"],
        })
        # Build pl_df without _PL_COL_ADDR1.
        pl_df = pd.DataFrame({
            "NPI": ["1111111111"],
            _PL_COL_STATE: ["FL"],
            _PL_COL_ZIP: ["33001"],
            # _PL_COL_ADDR1 intentionally omitted
        })

        with pytest.raises(ValueError, match="missing required column"):
            assert_source_shape(main_df, pl_df)

    def test_raises_value_error_when_main_df_missing_npi(self):
        main_df = pd.DataFrame({
            "Provider Organization Name (Legal Business Name)": ["Org A"],
            "taxonomy_primary": ["261QM0801X"],
            # NPI intentionally omitted
        })
        pl_df = _make_pl_df([
            {"NPI": "1111111111", _PL_COL_ADDR1: "1 St",
             _PL_COL_STATE: "FL", _PL_COL_ZIP: "33001"},
        ])

        with pytest.raises(ValueError, match="missing required column"):
            assert_source_shape(main_df, pl_df)

    def test_passes_with_valid_dataframes(self):
        """Well-formed DataFrames must not raise."""
        main_df = pd.DataFrame({
            "NPI": ["1111111111"],
            "Provider Organization Name (Legal Business Name)": ["Org A"],
            "taxonomy_primary": ["261QM0801X"],
        })
        pl_df = _make_pl_df([
            {"NPI": "1111111111", _PL_COL_ADDR1: "1 Main St",
             _PL_COL_STATE: "FL", _PL_COL_ZIP: "33001"},
        ])
        # Should not raise.
        assert_source_shape(main_df, pl_df)


# ---------------------------------------------------------------------------
# report_quality
# ---------------------------------------------------------------------------


class TestReportQuality:
    def test_does_not_crash(self, capsys):
        """report_quality must complete without raising on a valid DataFrame."""
        main_slim = pd.DataFrame({
            "NPI": ["1111111111", "2222222222"],
            "Provider Organization Name (Legal Business Name)": ["Org A", "Org B"],
            "taxonomy_primary": ["261QM0801X", "282N00000X"],
        })
        pl_df = _make_pl_df([
            {"NPI": "1111111111", _PL_COL_ADDR1: "1 A St", _PL_COL_STATE: "FL", _PL_COL_ZIP: "33001"},
            {"NPI": "2222222222", _PL_COL_ADDR1: "1 B St", _PL_COL_STATE: "TX", _PL_COL_ZIP: "750015678"},
        ])
        result = normalize_and_join(main_slim, pl_df)
        report_quality(result)  # Must not raise.
        captured = capsys.readouterr()
        assert "quality report" in captured.err


# ---------------------------------------------------------------------------
# filter_to_facility_orgs (taxonomy filter)
# ---------------------------------------------------------------------------


class TestTaxonomyFilter:
    """
    Tests for filter_to_facility_orgs(), which applies both the entity-type
    and taxonomy prefix filters in a single pass.
    """

    def test_keeps_org_with_261q_taxonomy(self):
        """Entity Type 2 + 261QM0801X (Ambulatory Health Care) is kept."""
        df = _make_main_df([
            {"NPI": "1111111111", "Entity Type Code": "2",
             "Healthcare Provider Taxonomy Code_1": "261QM0801X"},
        ])
        result = filter_to_facility_orgs(df)
        assert len(result) == 1
        assert result.iloc[0]["NPI"] == "1111111111"

    def test_excludes_individual_provider_even_with_facility_taxonomy(self):
        """Entity Type 1 is always excluded, even if a facility taxonomy is present."""
        df = _make_main_df([
            {"NPI": "2222222222", "Entity Type Code": "1",
             "Healthcare Provider Taxonomy Code_1": "261QM0801X"},
        ])
        result = filter_to_facility_orgs(df)
        assert len(result) == 0

    def test_excludes_org_with_only_individual_practitioner_taxonomy(self):
        """Entity Type 2 but only a non-facility taxonomy (207R = Internal Medicine) is dropped."""
        df = _make_main_df([
            {"NPI": "3333333333", "Entity Type Code": "2",
             "Healthcare Provider Taxonomy Code_1": "207R00000X"},
        ])
        result = filter_to_facility_orgs(df)
        assert len(result) == 0

    def test_keeps_org_with_282_hospital_taxonomy(self):
        """282N00000X (General Acute Care Hospital) is a facility taxonomy."""
        df = _make_main_df([
            {"NPI": "4444444444", "Entity Type Code": "2",
             "Healthcare Provider Taxonomy Code_1": "282N00000X"},
        ])
        result = filter_to_facility_orgs(df)
        assert len(result) == 1

    def test_keeps_org_with_facility_taxonomy_in_later_position(self):
        """Facility prefix in col _3 (not _1) should still pass the filter."""
        row = {"NPI": "5555555555", "Entity Type Code": "2"}
        # _1 and _2 are non-facility, _3 is an Ambulatory Health Care facility.
        row["Healthcare Provider Taxonomy Code_1"] = "207R00000X"
        row["Healthcare Provider Taxonomy Code_2"] = "207Q00000X"
        row["Healthcare Provider Taxonomy Code_3"] = "261QM0801X"
        df = _make_main_df([row])
        result = filter_to_facility_orgs(df)
        assert len(result) == 1

    def test_excludes_org_with_no_taxonomies(self):
        """An organization row with all empty taxonomy columns is dropped."""
        df = _make_main_df([
            {"NPI": "6666666666", "Entity Type Code": "2"},
        ])
        result = filter_to_facility_orgs(df)
        assert len(result) == 0

    def test_mixed_rows_returns_only_facility_orgs(self):
        """Three rows: one org+facility, one individual+facility, one org+non-facility."""
        df = _make_main_df([
            {"NPI": "1000000001", "Entity Type Code": "2",
             "Healthcare Provider Taxonomy Code_1": "261QM0801X"},  # kept
            {"NPI": "1000000002", "Entity Type Code": "1",
             "Healthcare Provider Taxonomy Code_1": "261QM0801X"},  # dropped — individual
            {"NPI": "1000000003", "Entity Type Code": "2",
             "Healthcare Provider Taxonomy Code_1": "207R00000X"},  # dropped — non-facility
        ])
        result = filter_to_facility_orgs(df)
        assert len(result) == 1
        assert result.iloc[0]["NPI"] == "1000000001"
