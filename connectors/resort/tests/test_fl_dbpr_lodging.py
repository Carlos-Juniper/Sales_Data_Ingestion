"""
Tests for fl_dbpr_lodging.py — Florida DBPR public lodging connector.

Strategy
--------
Every public function is covered in isolation.  The two traps called out in the
module header each have a dedicated test:

  1. Row-per-unit trap  — collapse_to_property must use max(), never sum().
  2. DWEL/BNB discard  — filter_qualified must remove those rank codes.

All DataFrames are built in-memory; no CSV files are read from disk.
The connector's stderr logging is suppressed via capsys (no mocking needed).
"""

from __future__ import annotations

import sys
import os

import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Make the connectors package importable when pytest is run from the repo root
# or from inside connectors/.
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fl_dbpr_lodging as mod


# ===========================================================================
# Helpers
# ===========================================================================

def _required_cols() -> list[str]:
    """Minimum column set that every raw-like DataFrame needs."""
    return [
        "License Number",
        "Rank Code",
        "Primary Status Code",
        mod.UNIT_COL,
        "Licensee Name",
        "Business Name",
        "Location Street Address",
        "Location City",
        "Location State Code",
        "Location Zip Code",
        "Location County",
        "Primary Phone Number",
        "Secondary Phone Number",
        "Mailing Street Address",
        "Mailing City",
        "Mailing State Code",
        "Mailing Zip Code",
        "License Expiry Date",
        "Last Inspection Date",
    ]


def _make_raw(**overrides) -> pd.DataFrame:
    """
    Return a single-row DataFrame that satisfies every field the module touches.
    Override individual column values with keyword arguments.
    """
    defaults = {
        "License Number": "CND2300001",
        "Rank Code": "HOTL",
        "Primary Status Code": "20",
        mod.UNIT_COL: "50",
        "Licensee Name": "SUNSET RESORT LLC",
        "Business Name": "Sunset Resort",
        "Location Street Address": "100 Ocean Dr",
        "Location City": "Miami Beach",
        "Location State Code": "FL",
        "Location Zip Code": "33139",
        "Location County": "Miami-Dade",
        "Primary Phone Number": "3055550100",
        "Secondary Phone Number": "",
        "Mailing Street Address": "100 Ocean Dr",
        "Mailing City": "Miami Beach",
        "Mailing State Code": "FL",
        "Mailing Zip Code": "33139",
        "License Expiry Date": "2027-06-30",
        "Last Inspection Date": "2026-01-15",
        "_source_file": "hrlodge1.csv",
    }
    defaults.update(overrides)
    return pd.DataFrame([defaults])


def _make_multi_row(license_no: str, units_str: str, n_rows: int,
                    rank: str = "HOTL") -> pd.DataFrame:
    """
    Build a DataFrame with ``n_rows`` rows for the same license, all
    reporting the same ``units_str``.  Address Line 2 varies (simulating
    the source extract), every other field is constant.
    """
    rows = []
    for i in range(n_rows):
        row = _make_raw(
            **{
                "License Number": license_no,
                "Rank Code": rank,
                "Primary Status Code": "20",
                mod.UNIT_COL: units_str,
                "Location Street Address": f"100 Ocean Dr Unit {i + 1}",
            }
        ).iloc[0].to_dict()
        rows.append(row)
    return pd.DataFrame(rows)


# ===========================================================================
# Tests: RANK_MAP structural integrity
# ===========================================================================

class TestRankMap:
    def test_every_value_is_a_two_tuple_of_str_and_bool(self):
        for code, value in mod.RANK_MAP.items():
            assert isinstance(value, tuple), f"{code}: expected tuple, got {type(value)}"
            assert len(value) == 2, f"{code}: tuple length should be 2, got {len(value)}"
            assert isinstance(value[0], str), (
                f"{code}: first element should be str, got {type(value[0])}"
            )
            assert isinstance(value[1], bool), (
                f"{code}: second element should be bool, got {type(value[1])}"
            )

    def test_known_keep_codes_are_marked_true(self):
        keep_codes = {"HOTL", "MOTL", "CNDO", "TAPT", "NAPT"}
        for code in keep_codes:
            assert mod.RANK_MAP[code][1] is True, f"{code} should be keep=True"

    def test_bnb_and_dwel_are_marked_false(self):
        assert mod.RANK_MAP["BNB"][1] is False
        assert mod.RANK_MAP["DWEL"][1] is False


# ===========================================================================
# Tests: normalize()
# ===========================================================================

class TestNormalize:
    def test_units_parsed_to_numeric(self):
        df = _make_raw(**{mod.UNIT_COL: "42"})
        out = mod.normalize(df)
        assert out["units"].iloc[0] == 42

    def test_units_coerced_to_nan_for_non_numeric(self):
        df = _make_raw(**{mod.UNIT_COL: "N/A"})
        out = mod.normalize(df)
        assert pd.isna(out["units"].iloc[0])

    # -- ZIP normalisation ---------------------------------------------------

    def test_zip_hyphenated_9_digit_stripped_to_5(self):
        df = _make_raw(**{"Location Zip Code": "33139-5808"})
        out = mod.normalize(df)
        assert out["zip5"].iloc[0] == "33139"

    def test_zip_concatenated_9_digit_stripped_to_5(self):
        df = _make_raw(**{"Location Zip Code": "331394209"})
        out = mod.normalize(df)
        assert out["zip5"].iloc[0] == "33139"

    def test_zip_plain_5_digit_unchanged(self):
        df = _make_raw(**{"Location Zip Code": "33139"})
        out = mod.normalize(df)
        assert out["zip5"].iloc[0] == "33139"

    def test_zip_non_digit_value_keeps_leading_chars(self):
        # "ABC" → strip non-digits → "" → str[:5] → "" (empty, not erroring)
        df = _make_raw(**{"Location Zip Code": "ABC"})
        out = mod.normalize(df)
        # Should not raise; result is an empty string (no digits to take)
        assert out["zip5"].iloc[0] == ""

    # -- is_association flag -------------------------------------------------

    def test_is_association_true_when_name_contains_association(self):
        df = _make_raw(**{"Licensee Name": "SUNSET BEACH ASSOCIATION INC"})
        out = mod.normalize(df)
        assert bool(out["is_association"].iloc[0]) is True

    def test_is_association_true_when_name_contains_assn(self):
        df = _make_raw(**{"Licensee Name": "OCEAN VIEW ASSN"})
        out = mod.normalize(df)
        assert bool(out["is_association"].iloc[0]) is True

    def test_is_association_true_when_name_contains_hoa(self):
        df = _make_raw(**{"Licensee Name": "PALM GROVE HOA"})
        out = mod.normalize(df)
        assert bool(out["is_association"].iloc[0]) is True

    def test_is_association_true_when_name_contains_owners(self):
        df = _make_raw(**{"Licensee Name": "BRICKELL OWNERS CORP"})
        out = mod.normalize(df)
        assert bool(out["is_association"].iloc[0]) is True

    def test_is_association_false_for_hotel_company(self):
        df = _make_raw(**{"Licensee Name": "RITZ CARLTON HOTEL CO"})
        out = mod.normalize(df)
        assert bool(out["is_association"].iloc[0]) is False

    def test_is_association_false_for_plain_resort_name(self):
        df = _make_raw(**{"Licensee Name": "OCEAN BREEZE RESORT LLC"})
        out = mod.normalize(df)
        assert bool(out["is_association"].iloc[0]) is False

    # -- vertical mapping ----------------------------------------------------

    def test_vertical_mapped_from_rank_code(self):
        df = _make_raw(**{"Rank Code": "NAPT"})
        out = mod.normalize(df)
        assert out["vertical"].iloc[0] == "multifamily"

    def test_vertical_defaults_to_resort_for_unknown_rank(self):
        df = _make_raw(**{"Rank Code": "XXXX"})
        out = mod.normalize(df)
        assert out["vertical"].iloc[0] == "resort"


# ===========================================================================
# Tests: collapse_to_property() — the row-per-unit trap
# ===========================================================================

class TestCollapseToProperty:
    def test_row_per_unit_trap_produces_single_row_not_sum(self):
        """
        CRITICAL: One license with 3 rows all reporting units=15.
        After collapse, exactly 1 row must remain and units must be 15, not 45.
        SUM() would inflate to 45 — verify max() is used.
        """
        df = _make_multi_row("CND2300049", units_str="15", n_rows=3)
        df = mod.normalize(df)

        out = mod.collapse_to_property(df)

        assert len(out) == 1, (
            f"Expected 1 row after collapse, got {len(out)}. "
            "Dedup on License Number failed."
        )
        assert out["units"].iloc[0] == 15, (
            f"units == {out['units'].iloc[0]}. SUM() was used instead of max(). "
            "This inflates unit counts by the number of address rows."
        )

    def test_collapse_records_unit_row_count(self):
        df = _make_multi_row("CND2300049", units_str="15", n_rows=3)
        df = mod.normalize(df)
        out = mod.collapse_to_property(df)
        assert out["unit_rows_in_source"].iloc[0] == 3

    def test_multiple_licenses_each_collapse_independently(self):
        df_a = _make_multi_row("LIC0001", units_str="30", n_rows=2)
        df_b = _make_multi_row("LIC0002", units_str="50", n_rows=4)
        combined = pd.concat([df_a, df_b], ignore_index=True)
        combined = mod.normalize(combined)

        out = mod.collapse_to_property(combined)

        assert len(out) == 2
        units_by_lic = dict(zip(out["License Number"], out["units"]))
        assert units_by_lic["LIC0001"] == 30
        assert units_by_lic["LIC0002"] == 50

    def test_empty_dataframe_returns_empty(self):
        df = _make_raw()
        df = mod.normalize(df)
        empty = df.iloc[0:0]
        out = mod.collapse_to_property(empty)
        assert out.empty


# ===========================================================================
# Tests: filter_qualified() — funnel ordering and DWEL/BNB discard
# ===========================================================================

class TestFilterQualified:
    def _make_normalized_row(self, **overrides) -> dict:
        base = _make_raw(**overrides).iloc[0].to_dict()
        return base

    def _frame_from_rows(self, rows: list[dict]) -> pd.DataFrame:
        return mod.normalize(pd.DataFrame(rows))

    def test_dwel_bnb_discard_hotl_survives(self):
        """
        CRITICAL: DWEL and BNB rows must be discarded. HOTL must survive.
        """
        rows = [
            _make_raw(**{"Rank Code": "HOTL", mod.UNIT_COL: "50",
                         "Primary Status Code": "20", "License Number": "LIC_HOTL"}).iloc[0].to_dict(),
            _make_raw(**{"Rank Code": "DWEL", mod.UNIT_COL: "50",
                         "Primary Status Code": "20", "License Number": "LIC_DWEL"}).iloc[0].to_dict(),
        ]
        df = mod.normalize(pd.DataFrame(rows))
        out = mod.filter_qualified(df)

        assert len(out) == 1, (
            f"Expected 1 row (HOTL only), got {len(out)}. "
            "DWEL should have been filtered by _keep_rank."
        )
        assert out["Rank Code"].iloc[0] == "HOTL"

    def test_bnb_row_is_discarded(self):
        rows = [
            _make_raw(**{"Rank Code": "HOTL", mod.UNIT_COL: "50",
                         "Primary Status Code": "20", "License Number": "LIC_HOTL"}).iloc[0].to_dict(),
            _make_raw(**{"Rank Code": "BNB", mod.UNIT_COL: "50",
                         "Primary Status Code": "20", "License Number": "LIC_BNB"}).iloc[0].to_dict(),
        ]
        df = mod.normalize(pd.DataFrame(rows))
        out = mod.filter_qualified(df)

        assert all(out["Rank Code"] != "BNB"), "BNB rows must be discarded by filter_qualified"

    def test_expired_status_filtered_before_rank_filter(self):
        """Status code != '20' must be dropped first in the funnel."""
        rows = [
            _make_raw(**{"Primary Status Code": "45", "Rank Code": "HOTL",
                         mod.UNIT_COL: "100", "License Number": "LIC_EXPIRED"}).iloc[0].to_dict(),
            _make_raw(**{"Primary Status Code": "20", "Rank Code": "HOTL",
                         mod.UNIT_COL: "100", "License Number": "LIC_ACTIVE"}).iloc[0].to_dict(),
        ]
        df = mod.normalize(pd.DataFrame(rows))
        out = mod.filter_qualified(df)

        assert len(out) == 1
        assert out["License Number"].iloc[0] == "LIC_ACTIVE"

    def test_units_below_min_units_filtered(self):
        rows = [
            _make_raw(**{"Rank Code": "HOTL", mod.UNIT_COL: "5",
                         "Primary Status Code": "20", "License Number": "LIC_SMALL"}).iloc[0].to_dict(),
            _make_raw(**{"Rank Code": "HOTL", mod.UNIT_COL: "50",
                         "Primary Status Code": "20", "License Number": "LIC_BIG"}).iloc[0].to_dict(),
        ]
        df = mod.normalize(pd.DataFrame(rows))
        out = mod.filter_qualified(df, min_units=20)

        assert all(out["units"] >= 20), "Rows below min_units must be dropped"
        assert len(out) == 1
        assert out["License Number"].iloc[0] == "LIC_BIG"

    def test_collapse_runs_last_deduplicating_unit_rows(self):
        """
        Three rows for the same license — all pass the earlier filters —
        collapse_to_property runs at the end and reduces them to one row.
        """
        df = _make_multi_row("LIC_MULTI", units_str="30", n_rows=3)
        df = mod.normalize(df)
        out = mod.filter_qualified(df, min_units=20)

        assert len(out) == 1


# ===========================================================================
# Tests: to_canonical() — output shape
# ===========================================================================

class TestToCanonical:
    EXPECTED_COLUMNS = {
        "source_id", "natural_key", "vertical", "account_type",
        "legal_name", "name_normalized", "dba_name", "location_name",
        "site_street", "site_city", "site_state", "site_zip", "site_county",
        "phone", "mailing_street", "mailing_city", "mailing_state", "mailing_zip",
        "size_metric", "size_metric_unit", "license_no", "license_class",
        "license_expiry", "last_inspection", "is_association",
        "unit_rows_in_source", "geocode_status",
    }

    def _qualified_df(self) -> pd.DataFrame:
        df = _make_multi_row("LIC_CANONIC", units_str="50", n_rows=2)
        df = mod.normalize(df)
        return mod.filter_qualified(df, min_units=20)

    def test_all_expected_columns_present(self):
        out = mod.to_canonical(self._qualified_df())
        missing = self.EXPECTED_COLUMNS - set(out.columns)
        assert not missing, f"Missing columns: {missing}"

    def test_geocode_status_is_pending_on_every_row(self):
        out = mod.to_canonical(self._qualified_df())
        assert (out["geocode_status"] == "pending").all()

    def test_size_metric_maps_to_units_column(self):
        df = self._qualified_df()
        out = mod.to_canonical(df)
        assert (out["size_metric"] == df["units"].values).all()

    def test_size_metric_unit_is_rental_units(self):
        out = mod.to_canonical(self._qualified_df())
        assert (out["size_metric_unit"] == "rental_units").all()

    def test_natural_key_matches_license_number(self):
        df = self._qualified_df()
        out = mod.to_canonical(df)
        assert (out["natural_key"] == df["License Number"].values).all()

    def test_source_id_constant(self):
        out = mod.to_canonical(self._qualified_df())
        assert (out["source_id"] == mod.SOURCE_ID).all()

    def test_phone_fallback_to_secondary_when_primary_empty(self):
        df = _make_raw(**{
            "Primary Phone Number": "   ",
            "Secondary Phone Number": "3055550199",
            mod.UNIT_COL: "50",
        })
        df = mod.normalize(df)
        df["unit_rows_in_source"] = 1
        out = mod.to_canonical(df)
        assert out["phone"].iloc[0] == "3055550199"


# ===========================================================================
# Tests: assert_source_shape()
# ===========================================================================

class TestAssertSourceShape:
    def _well_formed_df(self, n: int = 100) -> pd.DataFrame:
        """Return a DataFrame that passes all shape assertions."""
        rows = []
        for i in range(n):
            rows.append(
                _make_raw(**{
                    "Rank Code": "HOTL",
                    mod.UNIT_COL: "50",
                    "Licensee Name": f"RESORT {i} LLC",
                    "Location Street Address": f"{i} Ocean Dr",
                    "License Number": f"LIC{i:05d}",
                }).iloc[0].to_dict()
            )
        return pd.DataFrame(rows)

    def test_passes_on_well_formed_dataframe(self):
        df = self._well_formed_df()
        # Should not raise
        mod.assert_source_shape(df)

    def test_raises_when_unit_col_missing(self):
        df = self._well_formed_df()
        df = df.drop(columns=[mod.UNIT_COL])
        with pytest.raises(ValueError, match="missing size field"):
            mod.assert_source_shape(df)

    def test_raises_when_unit_col_fill_rate_below_99_percent(self):
        df = self._well_formed_df(n=200)
        # Set 10 rows (5%) to empty string — crosses the 1% threshold.
        df.loc[:9, mod.UNIT_COL] = ""
        with pytest.raises(ValueError, match=mod.UNIT_COL):
            mod.assert_source_shape(df)

    def test_raises_on_unmapped_rank_code(self):
        df = self._well_formed_df()
        df.loc[0, "Rank Code"] = "UNKN"
        with pytest.raises(ValueError, match="unmapped Rank Code"):
            mod.assert_source_shape(df)

    def test_raises_when_licensee_name_fill_rate_below_99_percent(self):
        df = self._well_formed_df(n=200)
        df.loc[:9, "Licensee Name"] = ""
        with pytest.raises(ValueError, match="Licensee Name"):
            mod.assert_source_shape(df)

    def test_raises_when_location_street_address_fill_rate_below_99_percent(self):
        df = self._well_formed_df(n=200)
        df.loc[:9, "Location Street Address"] = ""
        with pytest.raises(ValueError, match="Location Street Address"):
            mod.assert_source_shape(df)
