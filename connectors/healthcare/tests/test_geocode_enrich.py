"""
Unit tests for connectors/healthcare/geocode_enrich.py.

All tests operate on in-memory data.  No real network calls are made.
HTTP calls are patched at the function level via unittest.mock.patch so
test isolation is strict and no Session state bleeds between cases.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pandas as pd
import pytest

from healthcare.geocode_enrich import (
    CENSUS_BATCH_SIZE,
    assert_input_shape,
    enrich,
    geocode_single_nominatim,
    parse_census_response,
    prepare_census_batch,
    print_summary,
    submit_census_batch,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(**overrides) -> dict:
    """Return a minimal record dict; override any field as needed."""
    base = {
        "id": "1111111111|1",
        "address_line_1": "100 Main St",
        "city": "Miami",
        "site_state": "FL",
        "zip5": "33101",
    }
    base.update(overrides)
    return base


def _make_df(rows: list[dict] | None = None) -> pd.DataFrame:
    """
    Build a minimal DataFrame suitable for enrich().

    Each dict in rows may override any of the default field values.
    """
    if rows is None:
        rows = [{}]

    records = []
    for i, overrides in enumerate(rows, start=1):
        base = {
            "natural_key": f"111111111{i}|1",
            "address_line_1": "100 Main St",
            "city": "Miami",
            "site_state": "FL",
            "zip5": "33101",
        }
        base.update(overrides)
        records.append(base)

    return pd.DataFrame(records)


def _census_match_row(
    input_id: str = "1111111111|1",
    match: str = "Match",
    match_type: str = "Exact",
    lat: float = 25.7617,
    lon: float = -80.1918,
    matched_address: str = "100 MAIN ST, MIAMI, FL, 33101",
) -> str:
    """
    Build a single Census response CSV row string.

    The Census API quotes the coordinates field ("lon,lat") because it
    contains a comma.  Without quoting, csv.reader would split it into two
    columns and shift every subsequent column index, breaking parse_census_response.
    """
    coords = f"{lon},{lat}"
    return (
        f"{input_id},"
        f'"100 Main St, Miami, FL, 33101",'
        f"{match},{match_type},"
        f'"{matched_address}",'
        f'"{coords}",'
        f"12345678,R"
    )


# ---------------------------------------------------------------------------
# prepare_census_batch
# ---------------------------------------------------------------------------


class TestPrepareCensusBatch:
    def test_single_record_produces_correct_columns_and_no_header(self):
        """CSV output has id, address, city, state, zip in correct order with no header."""
        records = [_make_record()]
        csv_text = prepare_census_batch(records)
        lines = [ln for ln in csv_text.strip().splitlines() if ln]
        assert len(lines) == 1
        # Parse back with csv to handle any quoting.
        import csv as csv_mod
        row = next(csv_mod.reader([lines[0]]))
        assert row[0] == "1111111111|1"
        assert row[1] == "100 Main St"
        assert row[2] == "Miami"
        assert row[3] == "FL"
        assert row[4] == "33101"

    def test_address_with_commas_does_not_break_csv_parsing(self):
        """Addresses containing commas must be quoted so the column count stays fixed."""
        import csv as csv_mod
        records = [_make_record(address_line_1="Suite 1, 100 Main St")]
        csv_text = prepare_census_batch(records)
        row = next(csv_mod.reader(csv_text.splitlines()))
        # After CSV round-trip, the address field must be intact with its comma.
        assert row[1] == "Suite 1, 100 Main St"
        assert len(row) == 5

    def test_empty_list_returns_empty_string(self):
        assert prepare_census_batch([]) == ""


# ---------------------------------------------------------------------------
# parse_census_response
# ---------------------------------------------------------------------------


class TestParseCensusResponse:
    @pytest.mark.parametrize(
        "match_type, expected_precision",
        [
            ("Exact", "rooftop"),
            ("Non_Exact", "street"),
        ],
    )
    def test_match_type_maps_to_correct_precision(self, match_type, expected_precision):
        """Exact → rooftop, Non_Exact → street."""
        csv_text = _census_match_row(match_type=match_type)
        result = parse_census_response(csv_text)
        assert result["1111111111|1"]["geocode_precision"] == expected_precision

    def test_coordinates_are_parsed_as_lon_lat_order(self):
        """Census returns lon,lat — verify lat and lon are assigned to the right fields."""
        lat, lon = 25.7617, -80.1918
        csv_text = _census_match_row(lat=lat, lon=lon)
        result = parse_census_response(csv_text)
        geo = result["1111111111|1"]
        assert geo["latitude"] == pytest.approx(lat)
        assert geo["longitude"] == pytest.approx(lon)

    def test_no_match_row_excluded(self):
        csv_text = _census_match_row(match="No_Match")
        result = parse_census_response(csv_text)
        assert result == {}

    def test_tie_row_excluded(self):
        csv_text = _census_match_row(match="Tie")
        result = parse_census_response(csv_text)
        assert result == {}

    def test_empty_string_returns_empty_dict(self):
        assert parse_census_response("") == {}

    def test_malformed_csv_returns_empty_dict_without_raising(self):
        assert parse_census_response("not,a,valid,census,response\n") == {}

    def test_match_row_populates_geocode_address_returned(self):
        csv_text = _census_match_row(matched_address="100 MAIN ST, MIAMI, FL, 33101")
        result = parse_census_response(csv_text)
        assert result["1111111111|1"]["geocode_address_returned"] == "100 MAIN ST, MIAMI, FL, 33101"


# ---------------------------------------------------------------------------
# submit_census_batch
# ---------------------------------------------------------------------------


class TestSubmitCensusBatch:
    def test_successful_post_returns_parsed_results(self):
        """A 200 response body is passed through parse_census_response."""
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.text = _census_match_row(input_id="1111111111|1")
        mock_session.post.return_value = mock_response

        records = [_make_record(id="1111111111|1")]
        result = submit_census_batch(records, mock_session)

        assert "1111111111|1" in result
        assert result["1111111111|1"]["geocode_precision"] == "rooftop"

    def test_http_500_returns_empty_dict_without_raising(self, capsys):
        """A server error must not propagate; returns empty dict and logs to stderr."""
        import requests as req

        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = req.exceptions.HTTPError(
            response=MagicMock(status_code=500)
        )
        mock_session.post.return_value = mock_response

        result = submit_census_batch([_make_record()], mock_session)

        assert result == {}
        assert "census geocoder" in capsys.readouterr().err

    def test_timeout_returns_empty_dict_without_raising(self, capsys):
        """Timeout must not propagate; returns empty dict and logs to stderr."""
        import requests as req

        mock_session = MagicMock()
        mock_session.post.side_effect = req.exceptions.Timeout()

        result = submit_census_batch([_make_record()], mock_session)

        assert result == {}
        assert "timeout" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# geocode_single_nominatim
# ---------------------------------------------------------------------------


class TestGeocodeSingleNominatim:
    def test_successful_response_returns_lat_lon_and_source(self):
        """A non-empty Nominatim response extracts lat/lon and sets source='nominatim'."""
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = [{"lat": "25.7617", "lon": "-80.1918"}]
        mock_session.get.return_value = mock_response

        with patch("healthcare.geocode_enrich.time.sleep"):
            result = geocode_single_nominatim(_make_record(), mock_session)

        assert result is not None
        assert result["latitude"] == pytest.approx(25.7617)
        assert result["longitude"] == pytest.approx(-80.1918)
        assert result["geocode_source"] == "nominatim"

    def test_empty_results_list_returns_none(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = []
        mock_session.get.return_value = mock_response

        with patch("healthcare.geocode_enrich.time.sleep"):
            result = geocode_single_nominatim(_make_record(), mock_session)

        assert result is None

    def test_http_error_returns_none(self):
        """Any requests exception must be swallowed; returns None."""
        import requests as req

        mock_session = MagicMock()
        mock_session.get.side_effect = req.exceptions.HTTPError()

        with patch("healthcare.geocode_enrich.time.sleep"):
            result = geocode_single_nominatim(_make_record(), mock_session)

        assert result is None

    def test_rate_limit_sleep_is_called(self):
        """time.sleep must be called after every Nominatim request."""
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = []
        mock_session.get.return_value = mock_response

        with patch("healthcare.geocode_enrich.time.sleep") as mock_sleep:
            geocode_single_nominatim(_make_record(), mock_session)

        mock_sleep.assert_called_once()


# ---------------------------------------------------------------------------
# enrich (integration-level, all HTTP mocked)
# ---------------------------------------------------------------------------


class TestEnrich:
    def test_rows_with_existing_latitude_are_skipped(self):
        """submit_census_batch must not receive rows that already have latitude."""
        df = _make_df([
            {"natural_key": "A|1", "latitude": 25.7617, "longitude": -80.1918},
        ])

        with patch("healthcare.geocode_enrich.submit_census_batch") as mock_census:
            mock_census.return_value = {}
            result = enrich(df)

        mock_census.assert_not_called()
        # Pre-existing lat/lon must be preserved.
        assert result.loc[result["natural_key"] == "A|1", "latitude"].iloc[0] == pytest.approx(25.7617)

    def test_census_matches_some_nominatim_catches_remainder(self):
        """Two rows: Census hits one, Nominatim fills the other; both appear in output."""
        df = _make_df([
            {"natural_key": "A|1"},
            {"natural_key": "B|1"},
        ])

        census_result = {
            "A|1": {
                "latitude": 25.7617,
                "longitude": -80.1918,
                "geocode_precision": "rooftop",
                "geocode_match_type": "Exact",
                "geocode_address_returned": "100 MAIN ST, MIAMI, FL, 33101",
            }
        }
        nominatim_result = {
            "latitude": 30.2672,
            "longitude": -97.7431,
            "geocode_precision": "street",
            "geocode_source": "nominatim",
        }

        with (
            patch("healthcare.geocode_enrich.submit_census_batch", return_value=census_result),
            patch("healthcare.geocode_enrich.geocode_single_nominatim", return_value=nominatim_result),
            patch("healthcare.geocode_enrich.time.sleep"),
        ):
            result = enrich(df)

        a = result.loc[result["natural_key"] == "A|1"].iloc[0]
        assert a["geocode_source"] == "census"
        assert a["latitude"] == pytest.approx(25.7617)

        b = result.loc[result["natural_key"] == "B|1"].iloc[0]
        assert b["geocode_source"] == "nominatim"
        assert b["latitude"] == pytest.approx(30.2672)

    def test_fallback_nominatim_false_never_calls_nominatim(self):
        """When fallback_nominatim=False, Nominatim is never invoked for Census misses."""
        df = _make_df([{"natural_key": "A|1"}])

        with (
            patch("healthcare.geocode_enrich.submit_census_batch", return_value={}),
            patch("healthcare.geocode_enrich.geocode_single_nominatim") as mock_nom,
        ):
            result = enrich(df, fallback_nominatim=False)

        mock_nom.assert_not_called()
        assert result.loc[result["natural_key"] == "A|1", "geocode_source"].iloc[0] == "none"

    def test_all_output_columns_present_for_matched_and_unmatched_rows(self):
        """Both matched and unmatched rows must have all six enrichment columns."""
        df = _make_df([
            {"natural_key": "A|1"},
            {"natural_key": "B|1"},
        ])

        census_result = {
            "A|1": {
                "latitude": 25.7617,
                "longitude": -80.1918,
                "geocode_precision": "rooftop",
                "geocode_match_type": "Exact",
                "geocode_address_returned": "100 MAIN ST",
            }
        }

        with (
            patch("healthcare.geocode_enrich.submit_census_batch", return_value=census_result),
            patch("healthcare.geocode_enrich.geocode_single_nominatim", return_value=None),
            patch("healthcare.geocode_enrich.time.sleep"),
        ):
            result = enrich(df)

        expected_cols = {
            "latitude", "longitude", "geocode_precision",
            "geocode_source", "geocode_match_type", "geocode_address_returned",
        }
        assert expected_cols.issubset(set(result.columns))

        unmatched = result.loc[result["natural_key"] == "B|1"].iloc[0]
        assert unmatched["geocode_precision"] == "no_match"
        assert unmatched["geocode_source"] == "none"
        assert pd.isna(unmatched["latitude"])

    def test_all_rows_missing_geocode_triggers_census_then_nominatim_for_each(self):
        """With two unmatched rows and Census returning empty, Nominatim is called twice."""
        df = _make_df([{"natural_key": "A|1"}, {"natural_key": "B|1"}])

        with (
            patch("healthcare.geocode_enrich.submit_census_batch", return_value={}),
            patch("healthcare.geocode_enrich.geocode_single_nominatim", return_value=None) as mock_nom,
            patch("healthcare.geocode_enrich.time.sleep"),
        ):
            enrich(df, fallback_nominatim=True)

        assert mock_nom.call_count == 2


# ---------------------------------------------------------------------------
# assert_input_shape
# ---------------------------------------------------------------------------


class TestAssertInputShape:
    def test_raises_on_missing_required_column(self):
        df = pd.DataFrame({"natural_key": ["A|1"], "address_line_1": ["1 St"]})
        with pytest.raises(ValueError, match="missing required column"):
            assert_input_shape(df)

    def test_passes_with_all_required_columns_present(self):
        df = _make_df()
        assert_input_shape(df)  # must not raise


# ---------------------------------------------------------------------------
# print_summary
# ---------------------------------------------------------------------------


class TestPrintSummary:
    def test_does_not_raise_and_writes_to_stderr(self, capsys):
        df = _make_df([
            {"natural_key": "A|1"},
            {"natural_key": "B|1"},
        ])
        df["geocode_source"] = ["census", "none"]
        df["latitude"] = [25.7617, None]
        df["longitude"] = [-80.1918, None]

        print_summary(df)

        captured = capsys.readouterr()
        assert "geocode summary" in captured.err
        assert "match rate" in captured.err
