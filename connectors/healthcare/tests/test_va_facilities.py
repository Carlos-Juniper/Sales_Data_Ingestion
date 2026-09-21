"""
Unit tests for connectors/healthcare/va_facilities.py.

All HTTP is mocked via unittest.mock.patch on requests.Session.get so that
no real network calls are made. Patch target is 'requests.Session.get'
because va_facilities.py calls session.get() on a Session instance —
patching at the class level intercepts every call regardless of when/how
the Session is instantiated.

conftest.py in this directory inserts the parent package onto sys.path,
so imports from 'healthcare.*' and 'lib.*' resolve correctly.
"""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import requests

from healthcare.va_facilities import (
    _TARGET_STATES,
    assert_source_shape,
    fetch_all_facilities,
    filter_to_target_states,
    load_raw,
    normalize,
    to_canonical,
)
from lib.enums import HEALTHCARE_TARGET_STATES

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TAMPA_FACILITY = {
    "id": "vha_402",
    "attributes": {
        "name": "Tampa VA Medical Center",
        "facilityType": "va_health_facility",
        "address": {
            "physical": {
                "address1": "13000 Bruce B Downs Blvd",
                "city": "Tampa",
                "state": "FL",
                "zip": "33612",
            }
        },
        "lat": 28.0636,
        "long": -82.4274,
        "operatingStatus": {"code": "NORMAL"},
    },
}

_HOUSTON_FACILITY = {
    "id": "vha_580",
    "attributes": {
        "name": "Michael E. DeBakey VA Medical Center",
        "facilityType": "va_health_facility",
        "address": {
            "physical": {
                "address1": "2002 Holcombe Blvd",
                "city": "Houston",
                "state": "TX",
                "zip": "77030",
            }
        },
        "lat": 29.7089,
        "long": -95.4003,
        "operatingStatus": {"code": "NORMAL"},
    },
}


def _make_response(json_data: dict, status_code: int = 200) -> MagicMock:
    """Build a mock requests.Response with .json() and raise_for_status."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


def _single_page_response(facilities: list, total_pages: int = 1, current_page: int = 1) -> dict:
    """Build a well-formed Lighthouse API envelope for the given facilities."""
    return {
        "data": facilities,
        "meta": {
            "pagination": {
                "currentPage": current_page,
                "perPage": len(facilities),
                "totalEntries": len(facilities),
                "totalPages": total_pages,
            }
        },
    }


# ---------------------------------------------------------------------------
# fetch_all_facilities
# ---------------------------------------------------------------------------


class TestFetchAllFacilities:
    def test_fetch_single_page_returns_data(self):
        """A one-page response yields exactly the two facilities in data[]."""
        payload = _single_page_response([_TAMPA_FACILITY, _HOUSTON_FACILITY])

        with patch("requests.Session.get", return_value=_make_response(payload)):
            session = requests.Session()
            result = list(fetch_all_facilities(session, api_key="test-key"))

        assert len(result) == 2
        assert result[0]["id"] == "vha_402"
        assert result[1]["id"] == "vha_580"

    def test_fetch_multiple_pages_paginates(self):
        """
        When totalPages=2, the generator must request page 2 and return all
        facilities from both pages.
        """
        page1 = _single_page_response([_TAMPA_FACILITY], total_pages=2, current_page=1)
        page2 = _single_page_response([_HOUSTON_FACILITY], total_pages=2, current_page=2)

        with patch(
            "requests.Session.get",
            side_effect=[_make_response(page1), _make_response(page2)],
        ) as mock_get:
            session = requests.Session()
            result = list(fetch_all_facilities(session, api_key="test-key"))

        assert len(result) == 2
        assert mock_get.call_count == 2

        # Verify page=2 was sent on the second call.
        second_call_params = mock_get.call_args_list[1].kwargs["params"]
        assert second_call_params["page"] == 2

    def test_type_health_filter_is_always_sent(self):
        """The type=health query param must be present on every request."""
        payload = _single_page_response([_TAMPA_FACILITY])

        with patch("requests.Session.get", return_value=_make_response(payload)) as mock_get:
            session = requests.Session()
            list(fetch_all_facilities(session, api_key="test-key"))

        params = mock_get.call_args.kwargs["params"]
        assert params["type"] == "health"

    def test_api_key_sent_as_header_not_query_param(self):
        """The Lighthouse API requires the key in the apikey header, not as a param."""
        payload = _single_page_response([_TAMPA_FACILITY])

        with patch("requests.Session.get", return_value=_make_response(payload)) as mock_get:
            session = requests.Session()
            list(fetch_all_facilities(session, api_key="my-secret-key"))

        call_headers = mock_get.call_args.kwargs["headers"]
        assert call_headers["apikey"] == "my-secret-key"

        call_params = mock_get.call_args.kwargs["params"]
        assert "apikey" not in call_params


# ---------------------------------------------------------------------------
# load_raw
# ---------------------------------------------------------------------------


class TestLoadRaw:
    def test_load_raw_returns_dataframe_with_expected_columns(self):
        """load_raw should flatten nested attributes into a flat DataFrame plus bytes."""
        payload = _single_page_response([_TAMPA_FACILITY, _HOUSTON_FACILITY])

        with patch("requests.Session.get", return_value=_make_response(payload)):
            session = requests.Session()
            df, raw_bytes = load_raw(session, api_key="test-key")

        expected_columns = {
            "id", "name", "facilityType", "address1",
            "city", "state", "zip", "lat", "long", "operating_status_code",
        }
        assert expected_columns.issubset(set(df.columns))
        assert len(df) == 2

    def test_load_raw_extracts_nested_address_fields(self):
        """address1, city, state, zip should come from attributes.address.physical.*"""
        payload = _single_page_response([_TAMPA_FACILITY])

        with patch("requests.Session.get", return_value=_make_response(payload)):
            session = requests.Session()
            df, _raw_bytes = load_raw(session, api_key="test-key")

        row = df.iloc[0]
        assert row["address1"] == "13000 Bruce B Downs Blvd"
        assert row["city"] == "Tampa"
        assert row["state"] == "FL"
        assert row["zip"] == "33612"

    def test_load_raw_extracts_operating_status(self):
        """operating_status_code should come from attributes.operatingStatus.code"""
        payload = _single_page_response([_TAMPA_FACILITY])

        with patch("requests.Session.get", return_value=_make_response(payload)):
            session = requests.Session()
            df, _raw_bytes = load_raw(session, api_key="test-key")

        assert df.iloc[0]["operating_status_code"] == "NORMAL"

    def test_load_raw_returns_bytes_of_original_api_objects(self):
        """load_raw raw_bytes should be a deterministic JSON serialisation."""
        import json

        payload = _single_page_response([_TAMPA_FACILITY])

        with patch("requests.Session.get", return_value=_make_response(payload)):
            session = requests.Session()
            _df, raw_bytes = load_raw(session, api_key="test-key")

        assert isinstance(raw_bytes, bytes)
        # Must be valid JSON and contain the facility id.
        parsed = json.loads(raw_bytes)
        assert isinstance(parsed, list)
        assert any(f.get("id") == "vha_402" for f in parsed)


# ---------------------------------------------------------------------------
# assert_source_shape
# ---------------------------------------------------------------------------


class TestAssertSourceShape:
    def _minimal_valid_df(self, n: int = 101) -> pd.DataFrame:
        """Return a DataFrame that just passes all shape checks."""
        return pd.DataFrame({
            "id": [f"vha_{i}" for i in range(n)],
            "name": ["Test Facility"] * n,
            "facilityType": ["va_health_facility"] * n,
            "address1": ["123 Main St"] * n,
            "city": ["Tampa"] * n,
            "state": ["FL"] * n,
            "zip": ["33612"] * n,
            "lat": [28.0] * n,
            "long": [-82.0] * n,
            "operating_status_code": ["NORMAL"] * n,
        })

    def test_assert_source_shape_raises_on_missing_column(self):
        """A DataFrame missing natural_key equivalent ('id') must raise ValueError."""
        df = self._minimal_valid_df().drop(columns=["id"])
        with pytest.raises(ValueError, match="missing column"):
            assert_source_shape(df)

    def test_assert_source_shape_raises_on_too_few_rows(self):
        """Fewer than _MIN_EXPECTED_ROWS rows must raise ValueError."""
        df = self._minimal_valid_df(n=50)
        with pytest.raises(ValueError, match="only 50"):
            assert_source_shape(df)

    def test_assert_source_shape_raises_on_unknown_facility_type(self):
        """An unknown facilityType (e.g. from a future API expansion) must raise."""
        df = self._minimal_valid_df()
        df.loc[0, "facilityType"] = "va_benefits_facility"
        with pytest.raises(ValueError, match="unknown facilityType"):
            assert_source_shape(df)

    def test_assert_source_shape_passes_on_valid_data(self):
        """Valid data must not raise."""
        df = self._minimal_valid_df()
        assert_source_shape(df)  # should not raise


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_normalize_parses_zip_to_5_digits(self):
        """A 9-digit ZIP+4 should be truncated to 5 digits."""
        df = pd.DataFrame({
            "id": ["vha_1"],
            "name": ["Test"],
            "facilityType": ["va_health_facility"],
            "address1": ["123 Main St"],
            "city": ["Tampa"],
            "state": ["FL"],
            "zip": ["33612-1234"],  # ZIP+4 format
            "lat": [28.0],
            "long": [-82.0],
            "operating_status_code": ["NORMAL"],
        })
        result = normalize(df)
        assert result.iloc[0]["zip5"] == "33612"

    def test_normalize_converts_lat_lon_to_float(self):
        """lat/long columns should be coerced to float in latitude/longitude."""
        df = pd.DataFrame({
            "id": ["vha_1"],
            "name": ["Test"],
            "facilityType": ["va_health_facility"],
            "address1": ["123 Main St"],
            "city": ["Tampa"],
            "state": ["FL"],
            "zip": ["33612"],
            "lat": ["28.0636"],   # string form as might come from edge-case API responses
            "long": ["-82.4274"],
            "operating_status_code": ["NORMAL"],
        })
        result = normalize(df)
        assert result.iloc[0]["latitude"] == pytest.approx(28.0636)
        assert result.iloc[0]["longitude"] == pytest.approx(-82.4274)

    def test_normalize_uppercases_state_abbreviation(self):
        """State abbreviations must be normalized to uppercase."""
        df = pd.DataFrame({
            "id": ["vha_1"],
            "name": ["Test"],
            "facilityType": ["va_health_facility"],
            "address1": ["123 Main St"],
            "city": ["Tampa"],
            "state": ["fl"],  # lowercase as a defensive test
            "zip": ["33612"],
            "lat": [28.0],
            "long": [-82.0],
            "operating_status_code": ["NORMAL"],
        })
        result = normalize(df)
        assert result.iloc[0]["state_abbr"] == "FL"

    def test_normalize_handles_null_zip_gracefully(self):
        """A null ZIP should produce an empty string, not raise."""
        df = pd.DataFrame({
            "id": ["vha_1"],
            "name": ["Test"],
            "facilityType": ["va_health_facility"],
            "address1": ["123 Main St"],
            "city": ["Tampa"],
            "state": ["FL"],
            "zip": [None],
            "lat": [28.0],
            "long": [-82.0],
            "operating_status_code": ["NORMAL"],
        })
        result = normalize(df)
        assert result.iloc[0]["zip5"] == ""


# ---------------------------------------------------------------------------
# to_canonical
# ---------------------------------------------------------------------------


class TestToCanonical:
    def _normalized_df(self) -> pd.DataFrame:
        """Return a pre-normalized DataFrame ready for to_canonical()."""
        raw = pd.DataFrame({
            "id": ["vha_402", "vha_580"],
            "name": ["Tampa VA Medical Center", "Michael E. DeBakey VA Medical Center"],
            "facilityType": ["va_health_facility", "va_health_facility"],
            "address1": ["13000 Bruce B Downs Blvd", "2002 Holcombe Blvd"],
            "city": ["Tampa", "Houston"],
            "state": ["FL", "TX"],
            "zip": ["33612", "77030"],
            "lat": [28.0636, 29.7089],
            "long": [-82.4274, -95.4003],
            "operating_status_code": ["NORMAL", "NORMAL"],
        })
        return normalize(raw)

    def test_to_canonical_maps_columns_correctly(self):
        """Output DataFrame must contain exactly the expected canonical columns.

        D17a: phone_raw and phone_normalized are now included so the pipeline
        can carry VA facility phone numbers into resolved_account.phone and
        resolved_contact.phone.
        """
        expected_columns = {
            "natural_key", "name_raw", "address_line_1", "city",
            "site_state", "zip5", "latitude", "longitude",
            "facility_type", "operating_status",
            "phone_raw", "phone_normalized",
        }
        df = self._normalized_df()
        result = to_canonical(df)
        assert set(result.columns) == expected_columns

    def test_to_canonical_natural_key_is_facility_id(self):
        """natural_key must be the facility id string (e.g. 'vha_402')."""
        df = self._normalized_df()
        result = to_canonical(df)
        assert list(result["natural_key"]) == ["vha_402", "vha_580"]

    def test_to_canonical_site_state_is_2_letter_abbr(self):
        """site_state must carry the 2-letter state abbreviation."""
        df = self._normalized_df()
        result = to_canonical(df)
        assert list(result["site_state"]) == ["FL", "TX"]

    def test_to_canonical_latitude_longitude_are_numeric(self):
        """latitude and longitude columns must hold float values."""
        df = self._normalized_df()
        result = to_canonical(df)
        assert result["latitude"].dtype in (float, "float64")
        assert result["longitude"].dtype in (float, "float64")


# ---------------------------------------------------------------------------
# main — API key guard
# ---------------------------------------------------------------------------


class TestMainApiKeyGuard:
    def test_main_raises_on_missing_api_key(self):
        """
        When VA_API_KEY is absent, get_secret(required=True) must raise RuntimeError
        before any HTTP call is attempted.
        """
        with patch("healthcare.va_facilities.get_secret", return_value=None) as mock_secret:
            # get_secret with required=True raises RuntimeError when the value is None.
            # Simulate this — the connector delegates to get_secret, which is the
            # component under test here.
            mock_secret.side_effect = RuntimeError("Required secret 'VA_API_KEY' is not set.")

            with pytest.raises(RuntimeError, match="VA_API_KEY"):
                from healthcare.va_facilities import main
                import sys
                sys.argv = ["va_facilities.py", "--out", "/dev/null"]
                main()


# ---------------------------------------------------------------------------
# D10: state filter + shared constant tests
# ---------------------------------------------------------------------------


def _make_normalized_df(states: list[str]) -> pd.DataFrame:
    """
    Build a post-normalize() shaped DataFrame with one row per state.

    All required columns are present so filter_to_target_states() and
    to_canonical() can operate on the result without KeyError.
    """
    n = len(states)
    raw = pd.DataFrame({
        "id": [f"vha_{i}" for i in range(n)],
        "name": ["Test Facility"] * n,
        "facilityType": ["va_health_facility"] * n,
        "address1": ["123 Main St"] * n,
        "city": ["Test City"] * n,
        "state": states,
        "zip": ["33612"] * n,
        "lat": [28.0] * n,
        "long": [-82.0] * n,
        "operating_status_code": ["NORMAL"] * n,
    })
    return normalize(raw)


class TestFilterToTargetStates:
    def test_out_of_state_rows_are_dropped(self):
        """Rows whose state_abbr is not in the 5 target states must be removed."""
        df = _make_normalized_df(["FL", "CA", "TX", "NY", "PA"])
        result = filter_to_target_states(df)
        # CA and NY are out-of-scope; FL, TX, PA are kept.
        assert set(result["state_abbr"]) == {"FL", "TX", "PA"}
        assert len(result) == 3

    def test_all_target_state_rows_are_kept(self):
        """One row per target state — all five must survive the filter."""
        target_list = sorted(HEALTHCARE_TARGET_STATES)
        df = _make_normalized_df(target_list)
        result = filter_to_target_states(df)
        assert len(result) == 5
        assert set(result["state_abbr"]) == HEALTHCARE_TARGET_STATES

    def test_empty_dataframe_returns_empty(self):
        """An empty input (all rows filtered upstream) produces empty output without raising."""
        # Build a normalized-shaped DataFrame with a state_abbr string column but
        # zero rows — mirrors the real case where every row fails assert_source_shape
        # or all API rows are out-of-scope.
        df = _make_normalized_df(["CA"])           # 1 row so normalize() works
        df = df.iloc[0:0].copy()                   # slice to 0 rows, keep dtypes
        result = filter_to_target_states(df)
        assert len(result) == 0

    def test_all_rows_out_of_state_returns_empty(self):
        """When every row is out-of-state, the result is an empty DataFrame."""
        df = _make_normalized_df(["CA", "NY", "OH", "AZ"])
        result = filter_to_target_states(df)
        assert len(result) == 0

    def test_filter_does_not_mutate_input(self):
        """filter_to_target_states must return a copy, not modify the input."""
        df = _make_normalized_df(["FL", "CA"])
        original_len = len(df)
        filter_to_target_states(df)
        assert len(df) == original_len  # original unchanged


class TestSharedConstant:
    def test_target_states_alias_resolves_to_shared_constant(self):
        """
        va_facilities._TARGET_STATES must be the same object as
        HEALTHCARE_TARGET_STATES — not a separate tuple copy (D10).
        """
        assert _TARGET_STATES is HEALTHCARE_TARGET_STATES

    def test_target_states_contains_exactly_5_states(self):
        """The healthcare scope is exactly FL, NC, TX, PA, SC — no more, no less."""
        assert HEALTHCARE_TARGET_STATES == frozenset({"FL", "NC", "TX", "PA", "SC"})

    def test_filter_uses_shared_constant_not_independent_copy(self):
        """
        Out-of-state rows (CA) are dropped and in-state rows (NC) are kept,
        confirming the filter operates on HEALTHCARE_TARGET_STATES values.
        """
        df = _make_normalized_df(["NC", "CA"])
        result = filter_to_target_states(df)
        assert list(result["state_abbr"]) == ["NC"]
