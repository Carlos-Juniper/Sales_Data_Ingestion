"""
Unit tests for parcel_acreage_enrich.py public functions.

spatial_point_lookup (imported inside parcel_acreage_enrich) is patched at
its USE-SITE — 'parcel_acreage_enrich.spatial_point_lookup' — so no real
HTTP calls are made.

Layer registries are no longer populated at module level (import-time file I/O
was removed).  Tests that exercise state/county routing use either the
real_layer_maps fixture (built once from the on-disk YAML) or pass maps derived
from the minimal_yaml fixture directly to lookup_parcel.
"""


import os
import sys
from unittest.mock import patch

import pytest
import requests

# Make the connector module importable when pytest is run from the repo root
# or from inside connectors/.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import parcel_acreage_enrich as pe
from parcel_acreage_enrich import (
    LayerConfig,
    _build_layer_maps,
    _extract_property,
    load_layer_config,
    lookup_parcel,
)

# Patch target: spatial_point_lookup as imported into parcel_acreage_enrich.
_PATCH_TARGET = "parcel_acreage_enrich.spatial_point_lookup"

# A real FL lat/lon and session stub used across multiple tests.
_FL_LAT = 28.538
_FL_LON = -81.379
_FAKE_SESSION = object()


# ---------------------------------------------------------------------------
# Session-scoped fixture: real layer maps built once from the on-disk YAML.
# Used by tests that exercise state/county routing (SC, TX, PA, unknown state)
# because those tests rely on which states are registered in the real config.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def real_layer_maps():
    """Build (state_layers, county_layers) from the default parcel_layers.yaml."""
    raw = load_layer_config()
    return _build_layer_maps(raw)


# ---------------------------------------------------------------------------
# load_layer_config
# ---------------------------------------------------------------------------


class TestLoadLayerConfig:
    def test_loads_real_yaml_returns_dict_with_statewide_and_county_keys(self):
        # Arrange: uses the real config path (no argument → default).
        # Act
        raw = load_layer_config()

        # Assert
        assert isinstance(raw, dict)
        assert "statewide" in raw
        assert "county" in raw

    def test_statewide_section_contains_fl(self):
        raw = load_layer_config()
        assert "FL" in raw["statewide"]

    def test_raises_file_not_found_on_bad_path(self):
        with pytest.raises(FileNotFoundError):
            load_layer_config("/tmp/does_not_exist_xyz_parcel.yaml")


# ---------------------------------------------------------------------------
# _build_layer_maps
# ---------------------------------------------------------------------------


class TestBuildLayerMaps:
    def test_builds_statewide_layer_config_for_fl(self, minimal_yaml):
        # Arrange / Act
        statewide, county = _build_layer_maps(minimal_yaml)

        # Assert
        assert "FL" in statewide
        fl = statewide["FL"]
        assert isinstance(fl, LayerConfig)
        assert fl.url == "https://fake.arcgis.com/FL/FeatureServer/0"
        assert fl.area_field == "LND_SQFOOT"
        assert fl.area_unit == "sqft"
        assert fl.parcel_id_field == "PARCELNO"
        assert fl.owner_field == "OWN_NAME"

    def test_builds_county_layer_config_for_tx_48201(self, minimal_yaml):
        statewide, county = _build_layer_maps(minimal_yaml)

        assert "TX" in county
        assert "48201" in county["TX"]
        tx = county["TX"]["48201"]
        assert isinstance(tx, LayerConfig)
        assert tx.url == "https://fake.arcgis.com/TX/48201/FeatureServer/0"

    def test_statewide_out_fields_includes_parcel_id_area_and_owner(self, minimal_yaml):
        statewide, _ = _build_layer_maps(minimal_yaml)
        fl = statewide["FL"]
        # out_fields must contain all three field names.
        parts = fl.out_fields.split(",")
        assert fl.parcel_id_field in parts
        assert fl.area_field in parts
        assert fl.owner_field in parts

    def test_out_fields_excludes_owner_when_not_configured(self, minimal_yaml):
        # Remove owner_field from FL entry.
        minimal_yaml["statewide"]["FL"].pop("owner_field")
        statewide, _ = _build_layer_maps(minimal_yaml)
        fl = statewide["FL"]
        assert fl.owner_field is None
        # out_fields should only have parcel_id_field and area_field.
        parts = fl.out_fields.split(",")
        assert len(parts) == 2

    def test_to_acres_sqft_converts_correctly(self, minimal_yaml):
        statewide, _ = _build_layer_maps(minimal_yaml)
        fl = statewide["FL"]
        # 87120 sq ft == exactly 2.0 acres.
        assert fl.to_acres(87120) == pytest.approx(2.0, rel=1e-6)

    def test_to_acres_acres_unit_returns_value_unchanged(self, minimal_yaml):
        # Build an entry with area_unit = "acres".
        minimal_yaml["statewide"]["NC"] = {
            "url": "https://fake.arcgis.com/NC/FeatureServer/0",
            "area_field": "CALC_ACRES",
            "area_unit": "acres",
            "parcel_id_field": "PARNO",
            "owner_field": "OWNER",
        }
        statewide, _ = _build_layer_maps(minimal_yaml)
        nc = statewide["NC"]
        assert nc.to_acres(5.5) == pytest.approx(5.5)

    def test_empty_yaml_returns_empty_dicts(self):
        statewide, county = _build_layer_maps({})
        assert statewide == {}
        assert county == {}

    def test_none_sections_return_empty_dicts(self):
        statewide, county = _build_layer_maps({"statewide": None, "county": None})
        assert statewide == {}
        assert county == {}


# ---------------------------------------------------------------------------
# _extract_property
# ---------------------------------------------------------------------------


class TestExtractProperty:
    def test_exact_case_match_returns_value(self):
        feature = {"properties": {"LND_SQFOOT": 87120}}
        assert _extract_property(feature, "LND_SQFOOT") == 87120

    def test_case_insensitive_match_when_field_name_differs_in_case(self):
        feature = {"properties": {"lnd_sqfoot": 87120}}
        assert _extract_property(feature, "LND_SQFOOT") == 87120

    def test_missing_field_returns_none(self):
        feature = {"properties": {"OTHER_FIELD": "x"}}
        assert _extract_property(feature, "LND_SQFOOT") is None

    def test_empty_properties_returns_none(self):
        feature = {"properties": {}}
        assert _extract_property(feature, "LND_SQFOOT") is None

    def test_no_properties_key_returns_none(self):
        feature = {}
        assert _extract_property(feature, "LND_SQFOOT") is None

    def test_none_properties_value_returns_none(self):
        feature = {"properties": None}
        assert _extract_property(feature, "LND_SQFOOT") is None

    def test_returns_zero_correctly(self):
        """Zero is a valid field value; must not be confused with missing."""
        feature = {"properties": {"AREA": 0}}
        assert _extract_property(feature, "AREA") == 0

    def test_exact_match_takes_priority_over_case_insensitive(self):
        """If both 'AREA' and 'area' exist, the exact match is returned."""
        feature = {"properties": {"AREA": 100, "area": 999}}
        assert _extract_property(feature, "AREA") == 100


# ---------------------------------------------------------------------------
# lookup_parcel
# ---------------------------------------------------------------------------


class TestLookupParcelNoGeometry:
    # These tests exit before reaching get_layer_config, so empty registries suffice.
    _SL: dict = {}
    _CL: dict = {}

    def test_lat_none_returns_no_geometry_status(self):
        result = lookup_parcel("KEY1", "FL", lat=None, lon=_FL_LON, county_fips=None,
                               session=_FAKE_SESSION, state_layers=self._SL, county_layers=self._CL)
        assert result.lookup_status == "no_geometry"

    def test_lon_none_returns_no_geometry_status(self):
        result = lookup_parcel("KEY1", "FL", lat=_FL_LAT, lon=None, county_fips=None,
                               session=_FAKE_SESSION, state_layers=self._SL, county_layers=self._CL)
        assert result.lookup_status == "no_geometry"

    def test_both_none_returns_no_geometry_status(self):
        result = lookup_parcel("KEY1", "FL", lat=None, lon=None, county_fips=None,
                               session=_FAKE_SESSION, state_layers=self._SL, county_layers=self._CL)
        assert result.lookup_status == "no_geometry"

    def test_no_geometry_note_mentions_geocode(self):
        result = lookup_parcel("KEY1", "FL", lat=None, lon=None, county_fips=None,
                               session=_FAKE_SESSION, state_layers=self._SL, county_layers=self._CL)
        assert "geocode" in result.lookup_note.lower()


class TestLookupParcelStateNotSupported:
    def test_sc_returns_state_not_supported(self, real_layer_maps):
        sl, cl = real_layer_maps
        result = lookup_parcel("KEY2", "SC", lat=33.0, lon=-80.0, county_fips=None,
                               session=_FAKE_SESSION, state_layers=sl, county_layers=cl)
        assert result.lookup_status == "state_not_supported"

    def test_sc_note_mentions_county_assessor(self, real_layer_maps):
        sl, cl = real_layer_maps
        result = lookup_parcel("KEY2", "SC", lat=33.0, lon=-80.0, county_fips=None,
                               session=_FAKE_SESSION, state_layers=sl, county_layers=cl)
        assert "county assessor" in result.lookup_note.lower()

    def test_unknown_state_returns_state_not_supported(self, real_layer_maps):
        # "ZZ" is not in any registry.
        sl, cl = real_layer_maps
        result = lookup_parcel("KEY3", "ZZ", lat=33.0, lon=-80.0, county_fips=None,
                               session=_FAKE_SESSION, state_layers=sl, county_layers=cl)
        assert result.lookup_status == "state_not_supported"


class TestLookupParcelCountyNotConfigured:
    def test_tx_with_unknown_fips_returns_county_not_configured(self, real_layer_maps):
        # TX is in county_layers but FIPS "99999" has no entry.
        sl, cl = real_layer_maps
        result = lookup_parcel(
            "KEY4", "TX", lat=29.76, lon=-95.36,
            county_fips="99999", session=_FAKE_SESSION,
            state_layers=sl, county_layers=cl,
        )
        assert result.lookup_status == "county_not_configured"

    def test_county_not_configured_note_mentions_fips(self, real_layer_maps):
        sl, cl = real_layer_maps
        result = lookup_parcel(
            "KEY4", "TX", lat=29.76, lon=-95.36,
            county_fips="99999", session=_FAKE_SESSION,
            state_layers=sl, county_layers=cl,
        )
        assert "99999" in result.lookup_note

    def test_pa_with_unknown_fips_returns_county_not_configured(self, real_layer_maps):
        sl, cl = real_layer_maps
        result = lookup_parcel(
            "KEY5", "PA", lat=40.0, lon=-75.0,
            county_fips="99999", session=_FAKE_SESSION,
            state_layers=sl, county_layers=cl,
        )
        assert result.lookup_status == "county_not_configured"


class TestLookupParcelNotFound:
    def test_empty_feature_list_returns_not_found(self, real_layer_maps):
        sl, cl = real_layer_maps
        with patch(_PATCH_TARGET, return_value=[]):
            result = lookup_parcel(
                "KEY6", "FL", lat=_FL_LAT, lon=_FL_LON,
                county_fips=None, session=_FAKE_SESSION,
                state_layers=sl, county_layers=cl,
            )
        assert result.lookup_status == "not_found"

    def test_not_found_note_mentions_envelope_fallback(self, real_layer_maps):
        sl, cl = real_layer_maps
        with patch(_PATCH_TARGET, return_value=[]):
            result = lookup_parcel(
                "KEY6", "FL", lat=_FL_LAT, lon=_FL_LON,
                county_fips=None, session=_FAKE_SESSION,
                state_layers=sl, county_layers=cl,
            )
        assert "envelope" in result.lookup_note.lower()


class TestLookupParcelNoAreaField:
    def test_feature_with_null_area_field_returns_no_area_field(self, fl_feature, real_layer_maps):
        # Set area to None so float() coercion gives None → area_found stays False.
        fl_feature["properties"]["LND_SQFOOT"] = None
        sl, cl = real_layer_maps

        with patch(_PATCH_TARGET, return_value=[fl_feature]):
            result = lookup_parcel(
                "KEY7", "FL", lat=_FL_LAT, lon=_FL_LON,
                county_fips=None, session=_FAKE_SESSION,
                state_layers=sl, county_layers=cl,
            )

        assert result.lookup_status == "no_area_field"

    def test_feature_with_zero_area_returns_no_area_field(self, fl_feature, real_layer_maps):
        fl_feature["properties"]["LND_SQFOOT"] = 0
        sl, cl = real_layer_maps

        with patch(_PATCH_TARGET, return_value=[fl_feature]):
            result = lookup_parcel(
                "KEY7", "FL", lat=_FL_LAT, lon=_FL_LON,
                county_fips=None, session=_FAKE_SESSION,
                state_layers=sl, county_layers=cl,
            )

        assert result.lookup_status == "no_area_field"

    def test_no_area_field_still_populates_parcel_count(self, fl_feature, real_layer_maps):
        fl_feature["properties"]["LND_SQFOOT"] = None
        sl, cl = real_layer_maps

        with patch(_PATCH_TARGET, return_value=[fl_feature]):
            result = lookup_parcel(
                "KEY7", "FL", lat=_FL_LAT, lon=_FL_LON,
                county_fips=None, session=_FAKE_SESSION,
                state_layers=sl, county_layers=cl,
            )

        assert result.parcel_count == 1

    def test_no_area_field_note_mentions_field_name(self, fl_feature, real_layer_maps):
        fl_feature["properties"]["LND_SQFOOT"] = None
        sl, cl = real_layer_maps

        with patch(_PATCH_TARGET, return_value=[fl_feature]):
            result = lookup_parcel(
                "KEY7", "FL", lat=_FL_LAT, lon=_FL_LON,
                county_fips=None, session=_FAKE_SESSION,
                state_layers=sl, county_layers=cl,
            )

        assert "LND_SQFOOT" in result.lookup_note


class TestLookupParcelOk:
    def test_single_parcel_fl_sqft_converted_to_acres(self, fl_feature, real_layer_maps):
        # 87120 sq ft / 43560 == exactly 2.0 acres.
        sl, cl = real_layer_maps
        with patch(_PATCH_TARGET, return_value=[fl_feature]):
            result = lookup_parcel(
                "KEY8", "FL", lat=_FL_LAT, lon=_FL_LON,
                county_fips=None, session=_FAKE_SESSION,
                state_layers=sl, county_layers=cl,
            )

        assert result.lookup_status == "ok"
        assert result.maintained_acres == pytest.approx(2.0, rel=1e-4)

    def test_single_parcel_has_parcel_count_one(self, fl_feature, real_layer_maps):
        sl, cl = real_layer_maps
        with patch(_PATCH_TARGET, return_value=[fl_feature]):
            result = lookup_parcel(
                "KEY8", "FL", lat=_FL_LAT, lon=_FL_LON,
                county_fips=None, session=_FAKE_SESSION,
                state_layers=sl, county_layers=cl,
            )
        assert result.parcel_count == 1

    def test_single_parcel_parcel_id_populated(self, fl_feature, real_layer_maps):
        sl, cl = real_layer_maps
        with patch(_PATCH_TARGET, return_value=[fl_feature]):
            result = lookup_parcel(
                "KEY8", "FL", lat=_FL_LAT, lon=_FL_LON,
                county_fips=None, session=_FAKE_SESSION,
                state_layers=sl, county_layers=cl,
            )
        assert result.parcel_id == "08-1234-567-0001"

    def test_single_parcel_owner_name_populated(self, fl_feature, real_layer_maps):
        sl, cl = real_layer_maps
        with patch(_PATCH_TARGET, return_value=[fl_feature]):
            result = lookup_parcel(
                "KEY8", "FL", lat=_FL_LAT, lon=_FL_LON,
                county_fips=None, session=_FAKE_SESSION,
                state_layers=sl, county_layers=cl,
            )
        assert result.owner_name is not None
        assert "GENERAL HOSPITAL" in result.owner_name

    def test_single_parcel_boundary_geojson_set(self, fl_feature, real_layer_maps):
        sl, cl = real_layer_maps
        with patch(_PATCH_TARGET, return_value=[fl_feature]):
            result = lookup_parcel(
                "KEY8", "FL", lat=_FL_LAT, lon=_FL_LON,
                county_fips=None, session=_FAKE_SESSION,
                state_layers=sl, county_layers=cl,
            )
        assert result.boundary_geojson is not None

    def test_natural_key_preserved_in_result(self, fl_feature, real_layer_maps):
        sl, cl = real_layer_maps
        with patch(_PATCH_TARGET, return_value=[fl_feature]):
            result = lookup_parcel(
                "HOSPITAL_001", "FL", lat=_FL_LAT, lon=_FL_LON,
                county_fips=None, session=_FAKE_SESSION,
                state_layers=sl, county_layers=cl,
            )
        assert result.natural_key == "HOSPITAL_001"


class TestLookupParcelOkMultiParcel:
    def test_two_parcels_returns_ok_multi_parcel_status(self, fl_feature, real_layer_maps):
        import copy
        sl, cl = real_layer_maps
        feature2 = copy.deepcopy(fl_feature)
        feature2["properties"]["PARCELNO"] = "08-1234-567-0002"
        feature2["properties"]["LND_SQFOOT"] = 43560  # 1.0 acre

        with patch(_PATCH_TARGET, return_value=[fl_feature, feature2]):
            result = lookup_parcel(
                "KEY9", "FL", lat=_FL_LAT, lon=_FL_LON,
                county_fips=None, session=_FAKE_SESSION,
                state_layers=sl, county_layers=cl,
            )

        assert result.lookup_status == "ok_multi_parcel"

    def test_two_parcels_acres_are_summed(self, fl_feature, real_layer_maps):
        import copy
        sl, cl = real_layer_maps
        feature2 = copy.deepcopy(fl_feature)
        feature2["properties"]["LND_SQFOOT"] = 43560  # 1.0 acre

        with patch(_PATCH_TARGET, return_value=[fl_feature, feature2]):
            result = lookup_parcel(
                "KEY9", "FL", lat=_FL_LAT, lon=_FL_LON,
                county_fips=None, session=_FAKE_SESSION,
                state_layers=sl, county_layers=cl,
            )

        # 2.0 + 1.0 == 3.0 acres.
        assert result.maintained_acres == pytest.approx(3.0, rel=1e-4)

    def test_two_parcels_parcel_count_is_two(self, fl_feature, real_layer_maps):
        import copy
        sl, cl = real_layer_maps
        feature2 = copy.deepcopy(fl_feature)
        feature2["properties"]["LND_SQFOOT"] = 43560

        with patch(_PATCH_TARGET, return_value=[fl_feature, feature2]):
            result = lookup_parcel(
                "KEY9", "FL", lat=_FL_LAT, lon=_FL_LON,
                county_fips=None, session=_FAKE_SESSION,
                state_layers=sl, county_layers=cl,
            )

        assert result.parcel_count == 2

    def test_two_parcels_boundary_is_geometry_collection(self, fl_feature, real_layer_maps):
        import copy
        import json
        sl, cl = real_layer_maps
        feature2 = copy.deepcopy(fl_feature)
        feature2["properties"]["LND_SQFOOT"] = 43560

        with patch(_PATCH_TARGET, return_value=[fl_feature, feature2]):
            result = lookup_parcel(
                "KEY9", "FL", lat=_FL_LAT, lon=_FL_LON,
                county_fips=None, session=_FAKE_SESSION,
                state_layers=sl, county_layers=cl,
            )

        geom = json.loads(result.boundary_geojson)
        assert geom["type"] == "GeometryCollection"
        assert len(geom["geometries"]) == 2


class TestLookupParcelError:
    def test_network_error_in_spatial_lookup_returns_error_status(self, real_layer_maps):
        sl, cl = real_layer_maps
        with patch(_PATCH_TARGET, side_effect=requests.ConnectionError("Connection refused")):
            result = lookup_parcel(
                "KEY10", "FL", lat=_FL_LAT, lon=_FL_LON,
                county_fips=None, session=_FAKE_SESSION,
                state_layers=sl, county_layers=cl,
            )

        assert result.lookup_status == "error"

    def test_network_error_message_appears_in_note(self, real_layer_maps):
        sl, cl = real_layer_maps
        with patch(_PATCH_TARGET, side_effect=requests.ConnectionError("Connection refused")):
            result = lookup_parcel(
                "KEY10", "FL", lat=_FL_LAT, lon=_FL_LON,
                county_fips=None, session=_FAKE_SESSION,
                state_layers=sl, county_layers=cl,
            )

        # Note format is "network: <exc>" for RequestException subtypes.
        assert "Connection refused" in result.lookup_note
        assert result.lookup_note.startswith("network:")

    def test_http_error_caught_and_returns_error_status(self, real_layer_maps):
        sl, cl = real_layer_maps
        with patch(_PATCH_TARGET, side_effect=requests.HTTPError("503 Service Unavailable")):
            result = lookup_parcel(
                "KEY10", "FL", lat=_FL_LAT, lon=_FL_LON,
                county_fips=None, session=_FAKE_SESSION,
                state_layers=sl, county_layers=cl,
            )

        assert result.lookup_status == "error"
        assert "503" in result.lookup_note

    def test_parse_error_key_error_returns_error_status(self, real_layer_maps):
        # KeyError from malformed response data should be caught, not propagate.
        sl, cl = real_layer_maps
        with patch(_PATCH_TARGET, side_effect=KeyError("features")):
            result = lookup_parcel(
                "KEY10", "FL", lat=_FL_LAT, lon=_FL_LON,
                county_fips=None, session=_FAKE_SESSION,
                state_layers=sl, county_layers=cl,
            )

        assert result.lookup_status == "error"
        assert result.lookup_note.startswith("parse:")

    def test_programming_error_propagates(self, real_layer_maps):
        # Bare Exception (e.g. a TypeError from a config bug) must NOT be swallowed.
        sl, cl = real_layer_maps
        with pytest.raises(TypeError):
            with patch(_PATCH_TARGET, side_effect=TypeError("unexpected type")):
                lookup_parcel(
                    "KEY10", "FL", lat=_FL_LAT, lon=_FL_LON,
                    county_fips=None, session=_FAKE_SESSION,
                    state_layers=sl, county_layers=cl,
                )
