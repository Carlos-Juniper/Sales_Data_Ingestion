"""
Unit tests for sc_parcel_ingest.py.

All tests use in-memory DataFrames or tmp_path CSVs. No network calls.
"""

from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sc_parcel_ingest as spi
from sc_parcel_ingest import (
    SC_COUNTY_CONFIGS,
    _to_acres,
    assert_county_csv_shape,
    enrich,
    enrich_location,
    load_county_csv,
    match_location_to_parcel,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_charleston_csv(tmp_path, rows: list[dict] | None = None) -> str:
    """Write a minimal Charleston-format CSV to a temp file and return the path."""
    cfg = SC_COUNTY_CONFIGS["charleston"]
    default_rows = [
        {
            cfg["parcel_id_col"]: "CHS-001",
            cfg["acreage_col"]: "2.5",
            cfg["owner_col"]: "ROPER HOSPITAL",
            cfg["address_col"]: "316 CALHOUN STREET",
        }
    ]
    data = rows if rows is not None else default_rows
    path = tmp_path / "charleston.csv"
    pd.DataFrame(data).to_csv(path, index=False)
    return str(path)


def _make_greenville_csv(tmp_path, rows: list[dict] | None = None) -> str:
    """Write a minimal Greenville-format CSV to a temp file and return the path."""
    cfg = SC_COUNTY_CONFIGS["greenville"]
    default_rows = [
        {
            cfg["parcel_id_col"]: "GVL-999",
            cfg["acreage_col"]: "4.1",
            cfg["owner_col"]: "GREENVILLE HEALTH",
            cfg["address_col"]: "701 GROVE ROAD",
        }
    ]
    data = rows if rows is not None else default_rows
    path = tmp_path / "greenville.csv"
    pd.DataFrame(data).to_csv(path, index=False)
    return str(path)


def _parcel_df_from_rows(rows: list[dict]) -> pd.DataFrame:
    """Build a normalized parcel DataFrame without touching the filesystem."""
    from lib.normalize import normalize_name

    return pd.DataFrame(
        [
            {
                "parcel_id": r["parcel_id"],
                "acreage_raw": r["acreage_raw"],
                "owner_name": r["owner_name"],
                "address_normalized": normalize_name(r["address"]),
            }
            for r in rows
        ]
    )


def _location(
    address: str = "316 CALHOUN STREET",
    county: str = "charleston",
    natural_key: str = "CHS-CCN-001",
) -> dict:
    return {
        "natural_key": natural_key,
        "site_state": "SC",
        "address_line_1": address,
        "city": "Charleston",
        "zip5": "29401",
        "county_name": county,
    }


# ---------------------------------------------------------------------------
# load_county_csv
# ---------------------------------------------------------------------------


class TestLoadCountyCsv:
    def test_charleston_config_columns_present_loads_and_normalizes(self, tmp_path):
        path = _make_charleston_csv(tmp_path)
        df = load_county_csv(path, "charleston")

        assert list(df.columns) == [
            "parcel_id",
            "acreage_raw",
            "owner_name",
            "address_normalized",
        ]
        assert len(df) == 1
        assert df.iloc[0]["parcel_id"] == "CHS-001"
        assert df.iloc[0]["acreage_raw"] == "2.5"
        # normalize_name uppercases and strips punctuation
        assert df.iloc[0]["address_normalized"] == "316 CALHOUN STREET"

    def test_missing_required_column_raises_value_error_with_county_name(self, tmp_path):
        path = tmp_path / "bad_charleston.csv"
        # Write CSV without the ACREAGE column
        pd.DataFrame(
            [{"PARCEL_ID": "X", "OWNER_NAME": "Y", "SITUS_ADDRESS": "Z"}]
        ).to_csv(path, index=False)

        with pytest.raises(ValueError, match="charleston"):
            load_county_csv(str(path), "charleston")

    def test_greenville_county_uses_calc_acreage_and_account_no(self, tmp_path):
        path = _make_greenville_csv(tmp_path)
        df = load_county_csv(path, "greenville")

        assert df.iloc[0]["parcel_id"] == "GVL-999"
        assert df.iloc[0]["acreage_raw"] == "4.1"

    def test_unknown_county_raises_value_error(self, tmp_path):
        path = tmp_path / "fake.csv"
        pd.DataFrame([{"A": "1"}]).to_csv(path, index=False)

        with pytest.raises(ValueError, match="not in SC_COUNTY_CONFIGS"):
            load_county_csv(str(path), "unknown_county")

    def test_address_col_is_normalized_via_normalize_name(self, tmp_path):
        cfg = SC_COUNTY_CONFIGS["richland"]
        path = tmp_path / "richland.csv"
        pd.DataFrame(
            [
                {
                    cfg["parcel_id_col"]: "PIN-1",
                    cfg["acreage_col"]: "1.0",
                    cfg["owner_col"]: "PALMETTO HEALTH",
                    cfg["address_col"]: "5 RICHLAND MEDICAL PARK DR",
                }
            ]
        ).to_csv(path, index=False)

        df = load_county_csv(str(path), "richland")
        # normalize_name strips punctuation and collapses whitespace
        assert "RICHLAND" in df.iloc[0]["address_normalized"]


# ---------------------------------------------------------------------------
# assert_county_csv_shape
# ---------------------------------------------------------------------------


class TestAssertCountyCsvShape:
    def test_empty_dataframe_raises_value_error(self):
        df = pd.DataFrame(
            columns=["parcel_id", "acreage_raw", "owner_name", "address_normalized"]
        )
        with pytest.raises(ValueError, match="empty"):
            assert_county_csv_shape(df, "charleston")

    def test_non_empty_dataframe_passes(self):
        df = pd.DataFrame(
            [
                {
                    "parcel_id": "X",
                    "acreage_raw": "1.0",
                    "owner_name": "Y",
                    "address_normalized": "123 MAIN ST",
                }
            ]
        )
        # Should not raise
        assert_county_csv_shape(df, "charleston")

    def test_missing_required_column_raises_value_error(self):
        df = pd.DataFrame(
            [{"parcel_id": "X", "acreage_raw": "1.0", "owner_name": "Y"}]
        )
        with pytest.raises(ValueError, match="address_normalized"):
            assert_county_csv_shape(df, "charleston")


# ---------------------------------------------------------------------------
# _to_acres
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("12.34", 12.34),
        ("1,234.56", 1234.56),
        ("0", None),
        ("-5.0", None),
        ("abc", None),
        ("", None),
        ("  8.00  ", 8.0),
    ],
)
def test_to_acres_parametrized(raw: str, expected: float | None):
    result = _to_acres(raw)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# match_location_to_parcel
# ---------------------------------------------------------------------------


class TestMatchLocationToParcel:
    def test_exact_address_match_returns_parcel_row(self):
        parcel_df = _parcel_df_from_rows(
            [
                {
                    "parcel_id": "CHS-001",
                    "acreage_raw": "2.5",
                    "owner_name": "ROPER HOSPITAL",
                    "address": "316 CALHOUN STREET",
                }
            ]
        )
        loc = _location(address="316 CALHOUN STREET")
        result = match_location_to_parcel(loc, parcel_df)

        assert result is not None
        assert result["parcel_id"] == "CHS-001"

    def test_close_address_above_threshold_returns_match(self):
        # "316 CALHOUN ST" vs "316 CALHOUN STREET" — high similarity
        parcel_df = _parcel_df_from_rows(
            [
                {
                    "parcel_id": "CHS-002",
                    "acreage_raw": "3.0",
                    "owner_name": "ROPER",
                    "address": "316 CALHOUN STREET",
                }
            ]
        )
        loc = _location(address="316 CALHOUN ST")
        result = match_location_to_parcel(loc, parcel_df, threshold=0.70)

        assert result is not None
        assert result["parcel_id"] == "CHS-002"

    def test_below_threshold_address_returns_none(self):
        parcel_df = _parcel_df_from_rows(
            [
                {
                    "parcel_id": "CHS-003",
                    "acreage_raw": "1.0",
                    "owner_name": "X",
                    "address": "9999 COMPLETELY DIFFERENT BOULEVARD SUITE 400",
                }
            ]
        )
        loc = _location(address="1 MAIN ST")
        result = match_location_to_parcel(loc, parcel_df, threshold=0.70)

        assert result is None

    def test_empty_parcel_df_returns_none(self):
        empty_df = pd.DataFrame(
            columns=["parcel_id", "acreage_raw", "owner_name", "address_normalized"]
        )
        loc = _location(address="316 CALHOUN STREET")
        result = match_location_to_parcel(loc, empty_df)

        assert result is None

    def test_empty_location_address_returns_none(self):
        parcel_df = _parcel_df_from_rows(
            [
                {
                    "parcel_id": "CHS-004",
                    "acreage_raw": "2.0",
                    "owner_name": "Y",
                    "address": "316 CALHOUN STREET",
                }
            ]
        )
        loc = _location(address="")
        result = match_location_to_parcel(loc, parcel_df)

        assert result is None


# ---------------------------------------------------------------------------
# enrich_location
# ---------------------------------------------------------------------------


class TestEnrichLocation:
    def _parcel_df(self, acreage: str = "5.25", address: str = "316 CALHOUN STREET"):
        return _parcel_df_from_rows(
            [
                {
                    "parcel_id": "CHS-001",
                    "acreage_raw": acreage,
                    "owner_name": "ROPER HOSPITAL",
                    "address": address,
                }
            ]
        )

    def test_matched_parcel_with_acreage_returns_ok_status(self):
        result = enrich_location(
            _location(address="316 CALHOUN STREET"),
            self._parcel_df(acreage="5.25"),
            "charleston",
        )
        assert result["lookup_status"] == "ok"

    def test_matched_parcel_with_acreage_returns_positive_maintained_acres(self):
        result = enrich_location(
            _location(address="316 CALHOUN STREET"),
            self._parcel_df(acreage="5.25"),
            "charleston",
        )
        assert result["maintained_acres"] == pytest.approx(5.25)

    def test_matched_parcel_with_null_acreage_returns_no_acreage_status(self):
        result = enrich_location(
            _location(address="316 CALHOUN STREET"),
            self._parcel_df(acreage="0"),
            "charleston",
        )
        assert result["lookup_status"] == "no_acreage"

    def test_matched_parcel_with_null_acreage_has_none_maintained_acres(self):
        result = enrich_location(
            _location(address="316 CALHOUN STREET"),
            self._parcel_df(acreage=""),
            "charleston",
        )
        assert result["maintained_acres"] is None

    def test_no_match_returns_no_match_status(self):
        result = enrich_location(
            _location(address="1 TOTALLY DIFFERENT AVENUE SUITE 9999"),
            self._parcel_df(),
            "charleston",
        )
        assert result["lookup_status"] == "no_match"

    def test_ok_result_has_geometry_source_sc_assessor_csv(self):
        result = enrich_location(
            _location(address="316 CALHOUN STREET"),
            self._parcel_df(),
            "charleston",
        )
        assert result["geometry_source"] == "sc_assessor_csv"

    def test_ok_result_has_parcel_count_one(self):
        result = enrich_location(
            _location(address="316 CALHOUN STREET"),
            self._parcel_df(),
            "charleston",
        )
        assert result["parcel_count"] == 1

    def test_ok_result_has_none_boundary_geojson(self):
        result = enrich_location(
            _location(address="316 CALHOUN STREET"),
            self._parcel_df(),
            "charleston",
        )
        assert result["boundary_geojson"] is None

    def test_natural_key_preserved_in_result(self):
        result = enrich_location(
            _location(address="316 CALHOUN STREET", natural_key="MY-KEY-42"),
            self._parcel_df(),
            "charleston",
        )
        assert result["natural_key"] == "MY-KEY-42"


# ---------------------------------------------------------------------------
# enrich (multi-county batch)
# ---------------------------------------------------------------------------


class TestEnrich:
    def test_multi_county_charleston_and_greenville_both_enriched(self, tmp_path):
        chs_path = _make_charleston_csv(tmp_path)
        gvl_path = _make_greenville_csv(tmp_path)

        locations_df = pd.DataFrame(
            [
                {
                    "natural_key": "CHS-001",
                    "site_state": "SC",
                    "address_line_1": "316 CALHOUN STREET",
                    "city": "Charleston",
                    "zip5": "29401",
                    "county_name": "charleston",
                },
                {
                    "natural_key": "GVL-001",
                    "site_state": "SC",
                    "address_line_1": "701 GROVE ROAD",
                    "city": "Greenville",
                    "zip5": "29605",
                    "county_name": "greenville",
                },
            ]
        )

        result = enrich(
            locations_df,
            {"charleston": chs_path, "greenville": gvl_path},
        )

        assert len(result) == 2
        chs_row = result[result["natural_key"] == "CHS-001"].iloc[0]
        gvl_row = result[result["natural_key"] == "GVL-001"].iloc[0]
        assert chs_row["lookup_status"] == "ok"
        assert gvl_row["lookup_status"] == "ok"

    def test_unknown_county_with_no_csv_path_returns_bad_county(self, tmp_path):
        locations_df = pd.DataFrame(
            [
                {
                    "natural_key": "ZZZ-001",
                    "site_state": "SC",
                    "address_line_1": "1 FAKE STREET",
                    "city": "Nowhere",
                    "zip5": "00000",
                    "county_name": "not_a_county",
                }
            ]
        )

        result = enrich(locations_df, {})

        assert result.iloc[0]["lookup_status"] == "bad_county"

    def test_known_county_with_no_csv_path_provided_returns_bad_county(self, tmp_path):
        # Charleston is a known county but no CSV path is passed in
        locations_df = pd.DataFrame(
            [
                {
                    "natural_key": "CHS-002",
                    "site_state": "SC",
                    "address_line_1": "316 CALHOUN STREET",
                    "city": "Charleston",
                    "zip5": "29401",
                    "county_name": "charleston",
                }
            ]
        )

        result = enrich(locations_df, {})

        assert result.iloc[0]["lookup_status"] == "bad_county"

    def test_address_not_found_in_county_csv_returns_no_match(self, tmp_path):
        chs_path = _make_charleston_csv(tmp_path)

        locations_df = pd.DataFrame(
            [
                {
                    "natural_key": "CHS-MISS",
                    "site_state": "SC",
                    "address_line_1": "9999 COMPLETELY UNRELATED BOULEVARD SUITE 400",
                    "city": "Charleston",
                    "zip5": "29401",
                    "county_name": "charleston",
                }
            ]
        )

        result = enrich(locations_df, {"charleston": chs_path})

        assert result.iloc[0]["lookup_status"] == "no_match"

    def test_output_has_all_expected_columns(self, tmp_path):
        chs_path = _make_charleston_csv(tmp_path)
        locations_df = pd.DataFrame(
            [
                {
                    "natural_key": "CHS-003",
                    "site_state": "SC",
                    "address_line_1": "316 CALHOUN STREET",
                    "city": "Charleston",
                    "zip5": "29401",
                    "county_name": "charleston",
                }
            ]
        )

        result = enrich(locations_df, {"charleston": chs_path})

        expected_cols = {
            "natural_key",
            "state",
            "parcel_id",
            "maintained_acres",
            "acres_confidence",
            "geometry_source",
            "owner_name",
            "parcel_count",
            "boundary_geojson",
            "lookup_status",
            "lookup_note",
        }
        assert expected_cols.issubset(set(result.columns))

    def test_one_output_row_per_input_location(self, tmp_path):
        chs_path = _make_charleston_csv(tmp_path)
        locations_df = pd.DataFrame(
            [
                {
                    "natural_key": f"CHS-{i}",
                    "site_state": "SC",
                    "address_line_1": "316 CALHOUN STREET",
                    "city": "Charleston",
                    "zip5": "29401",
                    "county_name": "charleston",
                }
                for i in range(3)
            ]
        )

        result = enrich(locations_df, {"charleston": chs_path})

        assert len(result) == 3
