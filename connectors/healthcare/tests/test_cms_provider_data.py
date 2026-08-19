"""
Unit tests for connectors/healthcare/cms_provider_data.py.

All HTTP is mocked via unittest.mock.patch on requests.Session.get so that
no real network calls are made. Each test controls the mock return values
precisely so we are testing only the client logic, not the server.

Patch target is 'requests.Session.get' because cms_provider_data.py calls
s.get() on a Session instance — patching at the class level intercepts every
call regardless of how/when the Session is instantiated inside the module.
"""

import os
import sys

# Ensure the connectors root is on the import path so `lib.*` resolves.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from unittest.mock import MagicMock, call, patch

import pandas as pd
import pytest
import requests

from healthcare.cms_provider_data import (
    assert_source_shape,
    iter_datastore_rows,
    load_raw,
    to_canonical,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(json_data: dict, status_code: int = 200) -> MagicMock:
    """Build a mock requests.Response with a .json() method and raise_for_status."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Sample payloads
# ---------------------------------------------------------------------------

_DATASTORE_TWO_ROWS = {
    "count": 2,
    "results": [
        {
            "CMS Certification Number (CCN)": "12345",
            "Provider Name": "Test Facility",
            "Address": "100 Main St",
            "City/Town": "Miami",
            "State": "FL",
            "ZIP Code": "33101",
        },
        {
            "CMS Certification Number (CCN)": "67890",
            "Provider Name": "Other Facility",
            "Address": "200 Oak Ave",
            "City/Town": "Austin",
            "State": "TX",
            "ZIP Code": "73301",
        },
    ],
}

_DATASTORE_EMPTY = {
    "count": 0,
    "results": [],
}


# ---------------------------------------------------------------------------
# iter_datastore_rows
# ---------------------------------------------------------------------------


class TestIterDatastoreRows:
    def test_iter_datastore_rows_single_page(self):
        """Single page with count == len(results): yields all rows and stops."""
        session = requests.Session()
        with patch(
            "requests.Session.get",
            return_value=_make_response(_DATASTORE_TWO_ROWS),
        ):
            rows = list(iter_datastore_rows("abc123-uuid", session, page_size=1000))

        assert len(rows) == 2
        assert rows[0]["CMS Certification Number (CCN)"] == "12345"
        assert rows[1]["CMS Certification Number (CCN)"] == "67890"

    def test_iter_datastore_rows_paginates_correctly(self):
        """
        When page 1 returns fewer rows than total, a second request is issued
        with the correct offset.
        """
        page1 = {
            "count": 3,
            "results": [
                {"CMS Certification Number (CCN)": "11111", "Provider Name": "A"},
                {"CMS Certification Number (CCN)": "22222", "Provider Name": "B"},
            ],
        }
        page2 = {
            "count": 3,
            "results": [
                {"CMS Certification Number (CCN)": "33333", "Provider Name": "C"},
            ],
        }
        # Page 3 would be empty — stops the loop.
        page3 = {"count": 3, "results": []}

        session = requests.Session()
        with patch(
            "requests.Session.get",
            side_effect=[
                _make_response(page1),
                _make_response(page2),
                _make_response(page3),
            ],
        ) as mock_get:
            rows = list(iter_datastore_rows("abc123-uuid", session, page_size=2))

        # All three rows across two pages.
        assert len(rows) == 3
        assert rows[2]["CMS Certification Number (CCN)"] == "33333"

        # Second call must use offset=2 (len of page 1 results).
        second_call_params = mock_get.call_args_list[1].kwargs["params"]
        assert second_call_params["offset"] == 2

    def test_iter_datastore_rows_stops_when_offset_reaches_total(self):
        """Pagination stops without an extra call once offset >= total."""
        page1 = {
            "count": 2,
            "results": [
                {"id": "1"},
                {"id": "2"},
            ],
        }
        session = requests.Session()
        with patch(
            "requests.Session.get",
            return_value=_make_response(page1),
        ) as mock_get:
            rows = list(iter_datastore_rows("abc123-uuid", session, page_size=1000))

        assert len(rows) == 2
        # Only one HTTP call because offset (2) == total (2) after the first page.
        assert mock_get.call_count == 1

    def test_iter_datastore_rows_request_includes_count_param(self):
        """Each request must include count="true" so the total is returned.

        Must be the literal string "true", not 1 — the live datastore
        validates this as a JSON boolean and 400s on count=1.
        """
        session = requests.Session()
        with patch(
            "requests.Session.get",
            return_value=_make_response(_DATASTORE_EMPTY),
        ) as mock_get:
            list(iter_datastore_rows("abc123-uuid", session))

        params = mock_get.call_args.kwargs["params"]
        assert params.get("count") == "true"

    def test_iter_datastore_rows_empty_response_yields_nothing(self):
        """An immediately empty results list terminates the generator."""
        session = requests.Session()
        with patch(
            "requests.Session.get",
            return_value=_make_response(_DATASTORE_EMPTY),
        ):
            rows = list(iter_datastore_rows("abc123-uuid", session))

        assert rows == []


# ---------------------------------------------------------------------------
# load_raw
# ---------------------------------------------------------------------------


class TestLoadRaw:
    def test_load_raw_returns_dataframe(self):
        """load_raw wraps iter_datastore_rows and returns a DataFrame."""
        session = requests.Session()
        with patch(
            "requests.Session.get",
            return_value=_make_response(_DATASTORE_TWO_ROWS),
        ):
            df = load_raw("abc123-uuid", session, page_size=1000)

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 2
        assert "CMS Certification Number (CCN)" in df.columns
        assert "Provider Name" in df.columns

    def test_load_raw_empty_response_returns_empty_dataframe(self):
        """An empty API response yields an empty DataFrame (not an error)."""
        session = requests.Session()
        with patch(
            "requests.Session.get",
            return_value=_make_response(_DATASTORE_EMPTY),
        ):
            df = load_raw("abc123-uuid", session)

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0


# ---------------------------------------------------------------------------
# assert_source_shape
# ---------------------------------------------------------------------------


class TestAssertSourceShape:
    def test_assert_source_shape_raises_on_empty_df(self):
        """An empty DataFrame must raise ValueError."""
        with pytest.raises(ValueError, match="empty"):
            assert_source_shape(pd.DataFrame())

    def test_assert_source_shape_raises_on_too_few_columns(self):
        """A DataFrame with fewer than 5 columns must raise ValueError."""
        df = pd.DataFrame([{"a": 1, "b": 2, "c": 3}])
        with pytest.raises(ValueError, match="column"):
            assert_source_shape(df)

    def test_assert_source_shape_passes_with_sufficient_data(self):
        """A DataFrame with >= 5 columns and at least one row passes silently."""
        df = pd.DataFrame([{f"col_{i}": i for i in range(6)}])
        assert_source_shape(df)  # must not raise

    def test_assert_source_shape_passes_on_realistic_cms_frame(self):
        """A realistic CMS-shaped DataFrame passes the check."""
        session = requests.Session()
        with patch(
            "requests.Session.get",
            return_value=_make_response(_DATASTORE_TWO_ROWS),
        ):
            df = load_raw("abc123-uuid", session)

        assert_source_shape(df)  # must not raise


# ---------------------------------------------------------------------------
# to_canonical
# ---------------------------------------------------------------------------


class TestToCanonical:
    def _make_df(self) -> pd.DataFrame:
        """Build a minimal DataFrame that mirrors a live CMS API response."""
        return pd.DataFrame(
            [
                {
                    "CMS Certification Number (CCN)": "12345",
                    "Provider Name": "Test Nursing Home",
                    "Address": "100 Main St",
                    "City/Town": "Miami",
                    "State": "FL",
                    "ZIP Code": "33101",
                },
                {
                    "CMS Certification Number (CCN)": "67890",
                    "Provider Name": "  Other Home  ",
                    "Address": "200 Oak Ave",
                    "City/Town": "Austin",
                    "State": "TX",
                    "ZIP Code": "73301-1234",
                },
            ]
        )

    def test_to_canonical_maps_columns(self):
        """Output DataFrame must contain all required canonical columns."""
        df = self._make_df()
        result = to_canonical(df, dataset_key="nursing_home")

        assert set(_CANONICAL_COLS_EXPECTED).issubset(set(result.columns))

    def test_to_canonical_natural_key_is_ccn(self):
        """natural_key must be derived from the CCN column."""
        df = self._make_df()
        result = to_canonical(df, dataset_key="nursing_home")

        assert list(result["natural_key"]) == ["12345", "67890"]

    def test_to_canonical_name_raw_is_stripped(self):
        """name_raw values must have leading/trailing whitespace stripped."""
        df = self._make_df()
        result = to_canonical(df, dataset_key="nursing_home")

        assert result["name_raw"].iloc[1] == "Other Home"

    def test_to_canonical_zip5_normalizes_zip_plus_4(self):
        """ZIP+4 values must be truncated to 5 digits."""
        df = self._make_df()
        result = to_canonical(df, dataset_key="nursing_home")

        assert result["zip5"].iloc[1] == "73301"

    def test_to_canonical_site_state_is_uppercased(self):
        """site_state must be uppercase regardless of source casing."""
        df = self._make_df()
        df["State"] = df["State"].str.lower()
        result = to_canonical(df, dataset_key="nursing_home")

        assert all(s == s.upper() for s in result["site_state"] if s)

    def test_to_canonical_dataset_key_column_populated(self):
        """dataset_key column must carry the key passed to the function."""
        df = self._make_df()
        result = to_canonical(df, dataset_key="nursing_home")

        assert (result["dataset_key"] == "nursing_home").all()

    def test_to_canonical_general_dataset(self):
        """General dataset key flows through to dataset_key column."""
        df = self._make_df()
        result = to_canonical(df, dataset_key="general")

        assert (result["dataset_key"] == "general").all()

    def test_to_canonical_resolves_natural_key_for_hospital_facility_id_column(self):
        """
        Regression test: the live "general" (Hospital General Information)
        dataset has no CCN-named column at all — its identifier is
        "facility_id". Without "facility_id" in _CCN_HINTS, natural_key
        silently fills with "" for every row (confirmed live against
        xubh-q36u: 5,419/5,419 rows missing natural_key).
        """
        df = pd.DataFrame(
            [
                {
                    "facility_id": "010001",
                    "facility_name": "SOUTHEAST HEALTH MEDICAL CENTER",
                    "address": "1108 ROSS CLARK CIRCLE",
                    "citytown": "DOTHAN",
                    "state": "AL",
                    "zip_code": "36301",
                }
            ]
        )
        result = to_canonical(df, dataset_key="general")

        assert result["natural_key"].iloc[0] == "010001"

    def test_to_canonical_missing_column_fills_with_empty_string(self):
        """When a column cannot be resolved, output is filled with '' (no crash)."""
        # Deliberately omit the address column.
        df = pd.DataFrame(
            [
                {
                    "CMS Certification Number (CCN)": "12345",
                    "Provider Name": "Test",
                    "City/Town": "Miami",
                    "State": "FL",
                    "ZIP Code": "33101",
                }
            ]
        )
        result = to_canonical(df, dataset_key="nursing_home")
        # Should not raise; address_line_1 should be empty string.
        assert "address_line_1" in result.columns
        assert result["address_line_1"].iloc[0] == ""

    def test_to_canonical_output_row_count_matches_input(self):
        """Output DataFrame must have the same number of rows as the input."""
        df = self._make_df()
        result = to_canonical(df, dataset_key="nursing_home")

        assert len(result) == len(df)


# ---------------------------------------------------------------------------
# Module-level constant used by tests
# ---------------------------------------------------------------------------

_CANONICAL_COLS_EXPECTED = [
    "natural_key",
    "name_raw",
    "address_line_1",
    "city",
    "site_state",
    "zip5",
    "facility_type",
    "dataset_key",
]
