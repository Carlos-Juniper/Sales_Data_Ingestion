"""
Unit tests for lib/arcgis.py.

All HTTP is mocked via unittest.mock.patch on requests.Session.get so that
no real network calls are made.  Each test controls the mock return values
precisely so we are testing only the client logic, not the server.

Patch target is 'requests.Session.get' because arcgis.py calls s.get() on
a Session instance — patching at the class level intercepts every call
regardless of how/when the Session is instantiated inside the module.
"""

from unittest.mock import MagicMock, call, patch

import pytest
import requests

from lib.arcgis import get_layer_info, iter_features, spatial_point_lookup

BASE_URL = "https://fake.arcgis.com/FeatureServer/0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(json_data: dict, status_code: int = 200) -> MagicMock:
    """Build a mock requests.Response with a .json() method and raise_for_status."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(
            response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# get_layer_info
# ---------------------------------------------------------------------------


class TestGetLayerInfo:
    def test_happy_path_returns_dict(self):
        expected = {
            "maxRecordCount": 1000,
            "advancedQueryCapabilities": {"supportsPagination": True},
            "fields": [{"name": "OBJECTID"}],
        }

        with patch("requests.Session.get", return_value=_make_response(expected)) as mock_get:
            result = get_layer_info(BASE_URL)

        assert result == expected
        mock_get.assert_called_once()
        # Verify the call used the correct base URL (no trailing slash, with f=json).
        call_kwargs = mock_get.call_args
        assert "f" in call_kwargs.kwargs.get("params", {})
        assert call_kwargs.kwargs["params"]["f"] == "json"

    def test_http_error_raises(self):
        with patch("requests.Session.get", return_value=_make_response({}, status_code=500)):
            with pytest.raises(requests.HTTPError):
                get_layer_info(BASE_URL)

    def test_trailing_slash_stripped_from_url(self):
        expected = {"maxRecordCount": 500}
        with patch("requests.Session.get", return_value=_make_response(expected)) as mock_get:
            get_layer_info(BASE_URL + "/")

        called_url = mock_get.call_args.args[0]
        assert not called_url.endswith("/")

    def test_token_passed_in_params_when_provided(self):
        expected = {"maxRecordCount": 500}
        with patch("requests.Session.get", return_value=_make_response(expected)) as mock_get:
            get_layer_info(BASE_URL, token="mytoken")

        params = mock_get.call_args.kwargs["params"]
        assert params.get("token") == "mytoken"


# ---------------------------------------------------------------------------
# spatial_point_lookup
# ---------------------------------------------------------------------------


class TestSpatialPointLookup:
    def test_happy_path_returns_features_from_point_query(self):
        feature = {"type": "Feature", "properties": {"PARCELNO": "001"}}
        response_data = {"features": [feature]}

        with patch("requests.Session.get", return_value=_make_response(response_data)):
            result = spatial_point_lookup(BASE_URL, lon=-81.38, lat=28.54)

        assert result == [feature]

    def test_envelope_fallback_used_when_point_returns_empty(self):
        feature = {"type": "Feature", "properties": {"PARCELNO": "002"}}
        empty_response = _make_response({"features": []})
        envelope_response = _make_response({"features": [feature]})

        with patch("requests.Session.get", side_effect=[empty_response, envelope_response]) as mock_get:
            result = spatial_point_lookup(BASE_URL, lon=-81.38, lat=28.54)

        assert result == [feature]
        assert mock_get.call_count == 2

        # First call uses esriGeometryPoint, second uses esriGeometryEnvelope.
        first_params = mock_get.call_args_list[0].kwargs["params"]
        second_params = mock_get.call_args_list[1].kwargs["params"]
        assert first_params["geometryType"] == "esriGeometryPoint"
        assert second_params["geometryType"] == "esriGeometryEnvelope"

    def test_both_queries_empty_returns_empty_list(self):
        empty_response = _make_response({"features": []})

        with patch("requests.Session.get", side_effect=[empty_response, empty_response]):
            result = spatial_point_lookup(BASE_URL, lon=-81.38, lat=28.54)

        assert result == []

    def test_envelope_geometry_contains_all_four_coords(self):
        """Envelope bbox string must have 4 comma-separated values."""
        empty_response = _make_response({"features": []})
        feature_response = _make_response({"features": [{"type": "Feature"}]})

        with patch("requests.Session.get", side_effect=[empty_response, feature_response]) as mock_get:
            spatial_point_lookup(BASE_URL, lon=-81.38, lat=28.54, envelope_fallback_m=50.0)

        envelope_params = mock_get.call_args_list[1].kwargs["params"]
        coords = envelope_params["geometry"].split(",")
        assert len(coords) == 4
        # All four parts should be parseable as floats.
        floats = [float(c) for c in coords]
        assert floats[0] < -81.38  # min lon < input lon
        assert floats[2] > -81.38  # max lon > input lon

    def test_point_query_url_has_query_suffix(self):
        feature_response = _make_response({"features": [{"type": "Feature"}]})

        with patch("requests.Session.get", return_value=feature_response) as mock_get:
            spatial_point_lookup(BASE_URL, lon=-81.38, lat=28.54)

        called_url = mock_get.call_args.args[0]
        assert called_url.endswith("/query")


# ---------------------------------------------------------------------------
# iter_features — supportsPagination=True (offset mode)
# ---------------------------------------------------------------------------


class TestIterFeaturesOffsetPagination:
    def _layer_info(self, supports_pagination: bool = True) -> dict:
        return {
            "maxRecordCount": 2,
            "advancedQueryCapabilities": {"supportsPagination": supports_pagination},
        }

    def test_single_page_no_exceeded_limit_yields_all_features_and_stops(self):
        features = [{"id": 1}, {"id": 2}]
        layer_info_resp = _make_response(self._layer_info())
        page_resp = _make_response({"features": features, "exceededTransferLimit": False})

        with patch("requests.Session.get", side_effect=[layer_info_resp, page_resp]):
            result = list(iter_features(BASE_URL))

        assert result == features

    def test_two_pages_yields_features_from_both_pages(self):
        page1_features = [{"id": 1}, {"id": 2}]
        page2_features = [{"id": 3}]
        layer_info_resp = _make_response(self._layer_info())
        page1_resp = _make_response({"features": page1_features, "exceededTransferLimit": True})
        page2_resp = _make_response({"features": page2_features, "exceededTransferLimit": False})

        with patch("requests.Session.get", side_effect=[layer_info_resp, page1_resp, page2_resp]):
            result = list(iter_features(BASE_URL))

        assert result == page1_features + page2_features

    def test_two_pages_second_call_uses_correct_offset(self):
        page1_features = [{"id": 1}, {"id": 2}]
        page2_features = [{"id": 3}]
        layer_info_resp = _make_response(self._layer_info())
        page1_resp = _make_response({"features": page1_features, "exceededTransferLimit": True})
        page2_resp = _make_response({"features": page2_features, "exceededTransferLimit": False})

        with patch("requests.Session.get", side_effect=[layer_info_resp, page1_resp, page2_resp]) as mock_get:
            list(iter_features(BASE_URL))

        # Call index 0 = layer info, 1 = page 1 (offset=0), 2 = page 2 (offset=2).
        page2_params = mock_get.call_args_list[2].kwargs["params"]
        assert page2_params["resultOffset"] == 2

    def test_empty_features_with_exceeded_limit_stops_to_prevent_infinite_loop(self):
        """Guard: exceededTransferLimit=True but 0 features returned must stop."""
        layer_info_resp = _make_response(self._layer_info())
        bad_resp = _make_response({"features": [], "exceededTransferLimit": True})

        with patch("requests.Session.get", side_effect=[layer_info_resp, bad_resp]):
            result = list(iter_features(BASE_URL))

        assert result == []

    def test_max_record_count_supplied_skips_layer_info_call(self):
        """When max_record_count is provided, no GET to the layer info endpoint."""
        features = [{"id": 1}]
        page_resp = _make_response({"features": features, "exceededTransferLimit": False})

        with patch("requests.Session.get", return_value=page_resp) as mock_get:
            result = list(iter_features(BASE_URL, max_record_count=100))

        assert result == features
        # Only one HTTP call: the page query (no layer info fetch).
        assert mock_get.call_count == 1


# ---------------------------------------------------------------------------
# iter_features — supportsPagination=False (ID-chunk mode)
# ---------------------------------------------------------------------------


class TestIterFeaturesIdChunkMode:
    def _layer_info_no_pagination(self) -> dict:
        return {
            "maxRecordCount": 2,
            "advancedQueryCapabilities": {"supportsPagination": False},
        }

    def test_fetches_ids_first_then_queries_by_id_chunk(self):
        layer_info_resp = _make_response(self._layer_info_no_pagination())
        ids_resp = _make_response({"objectIds": [10, 20, 30]})
        chunk_resp = _make_response({"features": [{"id": 10}, {"id": 20}]})
        chunk_resp2 = _make_response({"features": [{"id": 30}]})

        with patch(
            "requests.Session.get",
            side_effect=[layer_info_resp, ids_resp, chunk_resp, chunk_resp2],
        ) as mock_get:
            result = list(iter_features(BASE_URL))

        assert len(result) == 3

        # Second call (index 1) must use returnIdsOnly=true.
        ids_call_params = mock_get.call_args_list[1].kwargs["params"]
        assert ids_call_params.get("returnIdsOnly") == "true"

    def test_id_chunks_contain_correct_objectids(self):
        layer_info_resp = _make_response(self._layer_info_no_pagination())
        ids_resp = _make_response({"objectIds": [10, 20, 30]})
        chunk1_resp = _make_response({"features": [{"id": 10}, {"id": 20}]})
        chunk2_resp = _make_response({"features": [{"id": 30}]})

        with patch(
            "requests.Session.get",
            side_effect=[layer_info_resp, ids_resp, chunk1_resp, chunk2_resp],
        ) as mock_get:
            list(iter_features(BASE_URL))

        # Chunk 1 objectIds: "10,20"; chunk 2: "30".
        chunk1_params = mock_get.call_args_list[2].kwargs["params"]
        chunk2_params = mock_get.call_args_list[3].kwargs["params"]
        assert chunk1_params["objectIds"] == "10,20"
        assert chunk2_params["objectIds"] == "30"

    def test_empty_id_list_returns_no_features(self):
        layer_info_resp = _make_response(self._layer_info_no_pagination())
        ids_resp = _make_response({"objectIds": []})

        with patch("requests.Session.get", side_effect=[layer_info_resp, ids_resp]):
            result = list(iter_features(BASE_URL))

        assert result == []

    def test_null_object_ids_field_returns_no_features(self):
        """objectIds key absent or null in response — must not crash."""
        layer_info_resp = _make_response(self._layer_info_no_pagination())
        ids_resp = _make_response({"objectIds": None})

        with patch("requests.Session.get", side_effect=[layer_info_resp, ids_resp]):
            result = list(iter_features(BASE_URL))

        assert result == []
