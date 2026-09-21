"""
Tests for park_layers.py — Parks vertical generic connector.

Strategy
--------
Every public function is covered in isolation. All DataFrames are built
in-memory; no CSV reads and no network calls occur.

Key traps each covered by a dedicated test:

  1. bbox_centroid polygon     — Polygon ring coordinates → correct bbox center.
  2. bbox_centroid multipolygon— MultiPolygon is handled the same way.
  3. bbox_centroid point       — Point geometry → pass-through (no bbox needed).
  4. bbox_centroid null        — None geometry → (None, None), not an exception.
  5. fetch geometry preserved  — _geometry column carries the raw GeoJSON dict.
  6. fetch properties key      — feature['properties'] path works.
  7. fetch attributes key      — feature['attributes'] fallback path works.
  8. normalize lat/lon from bbox— polygon feature → lat/lon derived from centroid.
  9. normalize area flow-through— area_field present → size_value/metric/unit set.
 10. normalize null area        — area_field absent in cfg → size fields all None.
 11. normalize state_field      — state_field set → _state column populated.
 12. normalize single-state cfg — state_field null, one state → _state hardcoded.
 13. to_canonical all columns   — output has every CANONICAL_COLUMNS column.
 14. to_canonical id_field key  — id_field used as natural_key when configured.
 15. to_canonical objectid fallback — natural_key falls back to OBJECTID string.
 16. to_canonical vertical parks— vertical column is always 'parks'.
 17. assert_source_shape passes — well-formed df passes without error.
 18. assert_source_shape min rows— fewer than 10 rows raises ValueError.
 19. assert_source_shape col missing — missing name_field raises ValueError.
 20. iter_features kwargs        — fetch() passes correct where/out_fields to iter_features.
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from lib.schema import CANONICAL_COLUMNS

import park_layers as mod
from lib.enums import SEGMENT_MUNICIPAL, SEGMENT_STATE
from parks.config_loader import LayerConfig


# ===========================================================================
# Helpers
# ===========================================================================

def _make_cfg(**overrides) -> LayerConfig:
    """Return a minimal LayerConfig with sensible defaults."""
    defaults = dict(
        source_id="test_parks",
        url="https://example.com/arcgis/rest/services/Parks/FeatureServer/0",
        where="1=1",
        states=["FL"],
        name_field="PARK_NAME",
        account_type="municipal_park",
        state_field=None,
        id_field="PARK_ID",
        owner_field=None,
        manager_field=None,
        area_field="ACRES",
        area_unit="acres",
    )
    defaults.update(overrides)
    return LayerConfig(**defaults)


def _polygon_geometry(
    lon_min: float = -82.0,
    lon_max: float = -81.9,
    lat_min: float = 28.0,
    lat_max: float = 28.1,
) -> dict:
    """GeoJSON Polygon geometry with a simple rectangular ring."""
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon_min, lat_min],
            [lon_max, lat_min],
            [lon_max, lat_max],
            [lon_min, lat_max],
            [lon_min, lat_min],
        ]],
    }


def _multipolygon_geometry() -> dict:
    """GeoJSON MultiPolygon with two simple rectangles."""
    ring_a = [[-82.0, 28.0], [-81.9, 28.0], [-81.9, 28.1], [-82.0, 28.1], [-82.0, 28.0]]
    ring_b = [[-83.0, 29.0], [-82.9, 29.0], [-82.9, 29.1], [-83.0, 29.1], [-83.0, 29.0]]
    return {"type": "MultiPolygon", "coordinates": [[ring_a], [ring_b]]}


def _point_geometry(lon: float = -81.5, lat: float = 28.5) -> dict:
    return {"type": "Point", "coordinates": [lon, lat]}


def _props_feature(
    park_id: int = 1,
    name: str = "Riverside Park",
    acres: float = 45.0,
    geometry: dict | None = None,
    **extra_props,
) -> dict:
    """Build a GeoJSON feature with a 'properties' dict."""
    if geometry is None:
        geometry = _polygon_geometry()
    props = {
        "PARK_ID": park_id,
        "PARK_NAME": name,
        "ACRES": acres,
        "OBJECTID": park_id,
    }
    props.update(extra_props)
    return {"type": "Feature", "properties": props, "geometry": geometry}


def _attributes_feature(
    park_id: int = 2,
    name: str = "Lakefront Park",
    acres: float = 22.5,
    geometry: dict | None = None,
) -> dict:
    """Build an ESRI JSON feature with an 'attributes' dict (fallback path)."""
    if geometry is None:
        geometry = _polygon_geometry(-83.0, -82.9, 29.0, 29.1)
    attrs = {
        "PARK_ID": park_id,
        "PARK_NAME": name,
        "ACRES": acres,
        "OBJECTID": park_id,
    }
    return {"type": "Feature", "attributes": attrs, "geometry": geometry}


def _make_raw(cfg: LayerConfig | None = None, n: int = 1, **overrides) -> pd.DataFrame:
    """
    Return an n-row raw DataFrame as fetch() would produce, without a network call.
    """
    if cfg is None:
        cfg = _make_cfg()
    rows = []
    for i in range(n):
        row = {
            "PARK_ID": i + 1,
            "PARK_NAME": f"Park {i + 1}",
            "ACRES": 10.0 + i,
            "OBJECTID": i + 1,
            "_geometry": _polygon_geometry(),
            "source_file": cfg.url,
        }
        row.update(overrides)
        rows.append(row)
    return pd.DataFrame(rows)


def _make_large_raw(cfg: LayerConfig | None = None, n: int = 15) -> pd.DataFrame:
    """Return an n-row DataFrame that satisfies assert_source_shape()."""
    return _make_raw(cfg=cfg, n=n)


# ===========================================================================
# Tests: bbox_centroid()
# ===========================================================================

class TestBboxCentroid:

    def test_polygon_centroid_is_bbox_center(self):
        """Centroid of a rectangle must be the midpoint of its bounding box."""
        geom = _polygon_geometry(lon_min=-82.0, lon_max=-81.9, lat_min=28.0, lat_max=28.1)
        lon, lat = mod.bbox_centroid(geom)
        assert lon == pytest.approx(-81.95)
        assert lat == pytest.approx(28.05)

    def test_multipolygon_uses_overall_bbox(self):
        """MultiPolygon centroid spans the bounding box of all sub-polygons."""
        geom = _multipolygon_geometry()
        lon, lat = mod.bbox_centroid(geom)
        # lon: min=-83.0, max=-81.9 → center=-82.45
        # lat: min=28.0,  max=29.1  → center=28.55
        assert lon == pytest.approx(-82.45)
        assert lat == pytest.approx(28.55)

    def test_point_geometry_returns_coordinates_directly(self):
        """Point geometry: no bbox needed — return the coordinate pair as-is."""
        geom = _point_geometry(-81.5, 28.5)
        lon, lat = mod.bbox_centroid(geom)
        assert lon == pytest.approx(-81.5)
        assert lat == pytest.approx(28.5)

    def test_none_geometry_returns_none_pair(self):
        """None geometry must return (None, None), not raise."""
        assert mod.bbox_centroid(None) == (None, None)

    def test_empty_dict_returns_none_pair(self):
        assert mod.bbox_centroid({}) == (None, None)

    def test_unknown_geometry_type_returns_none(self):
        assert mod.bbox_centroid({"type": "LineString", "coordinates": [[0, 0], [1, 1]]}) == (None, None)

    def test_polygon_with_empty_coordinates_returns_none(self):
        assert mod.bbox_centroid({"type": "Polygon", "coordinates": []}) == (None, None)


# ===========================================================================
# Tests: fetch()
# ===========================================================================

class TestFetch:
    """patch lib.arcgis.iter_features — park_layers imports via `from lib import arcgis`."""

    def test_happy_path_returns_dataframe(self):
        cfg = _make_cfg()
        feature = _props_feature()
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch(cfg)
        assert isinstance(df, pd.DataFrame)

    def test_one_row_per_feature(self):
        cfg = _make_cfg()
        features = [_props_feature(park_id=i, name=f"Park {i}") for i in range(1, 6)]
        with patch("lib.arcgis.iter_features", return_value=iter(features)):
            df = mod.fetch(cfg)
        assert len(df) == 5

    def test_geometry_column_preserved(self):
        """_geometry must carry the raw GeoJSON dict from each feature."""
        cfg = _make_cfg()
        geom = _polygon_geometry()
        feature = _props_feature(geometry=geom)
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch(cfg)
        assert "_geometry" in df.columns
        assert df["_geometry"].iloc[0] == geom

    def test_properties_key_works(self):
        """Features with 'properties' key are the primary path."""
        cfg = _make_cfg()
        feature = _props_feature(name="Green Valley Park")
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch(cfg)
        assert df["PARK_NAME"].iloc[0] == "Green Valley Park"

    def test_attributes_key_fallback(self):
        """Features with 'attributes' key (ESRI JSON) must also be read correctly."""
        cfg = _make_cfg()
        feature = _attributes_feature(name="Lakefront Park")
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch(cfg)
        assert df["PARK_NAME"].iloc[0] == "Lakefront Park"

    def test_source_file_column_added(self):
        cfg = _make_cfg()
        feature = _props_feature()
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch(cfg)
        assert "source_file" in df.columns
        assert df["source_file"].iloc[0] == cfg.url

    def test_iter_features_called_with_return_geometry_true(self):
        """Geometry must be requested — polygons need it for bbox_centroid()."""
        cfg = _make_cfg()
        with patch("lib.arcgis.iter_features", return_value=iter([])) as mock_iter:
            mod.fetch(cfg)
        call_kwargs = mock_iter.call_args.kwargs
        assert call_kwargs.get("return_geometry") is True

    def test_iter_features_called_with_correct_where(self):
        cfg = _make_cfg(where="Mang_Type = 'LOC'")
        with patch("lib.arcgis.iter_features", return_value=iter([])) as mock_iter:
            mod.fetch(cfg)
        call_kwargs = mock_iter.call_args.kwargs
        assert call_kwargs.get("where") == "Mang_Type = 'LOC'"

    def test_iter_features_out_fields_includes_name_field(self):
        """out_fields must include the configured name_field."""
        cfg = _make_cfg(name_field="UNIT_NM")
        with patch("lib.arcgis.iter_features", return_value=iter([])) as mock_iter:
            mod.fetch(cfg)
        call_kwargs = mock_iter.call_args.kwargs
        out_fields = call_kwargs.get("out_fields", "")
        assert "UNIT_NM" in out_fields

    def test_empty_feature_stream_returns_empty_dataframe(self):
        cfg = _make_cfg()
        with patch("lib.arcgis.iter_features", return_value=iter([])):
            df = mod.fetch(cfg)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0

    def test_objectid_coerced_to_numeric(self):
        cfg = _make_cfg()
        feature = _props_feature(park_id=7)
        feature["properties"]["OBJECTID"] = "7"  # arrives as string
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch(cfg)
        assert pd.api.types.is_numeric_dtype(df["OBJECTID"])


# ===========================================================================
# Tests: assert_source_shape()
# ===========================================================================

class TestAssertSourceShape:

    def test_passes_on_well_formed_dataframe(self):
        cfg = _make_cfg()
        df = _make_large_raw(cfg)
        mod.assert_source_shape(df, cfg)

    def test_raises_when_name_field_column_missing(self):
        cfg = _make_cfg(name_field="UNIT_NM")
        df = _make_large_raw(cfg)
        df = df.drop(columns=["PARK_NAME"], errors="ignore")
        # UNIT_NM was never in _make_raw, so the column is absent
        with pytest.raises(ValueError, match="UNIT_NM"):
            mod.assert_source_shape(df, cfg)

    def test_raises_when_row_count_below_minimum(self):
        cfg = _make_cfg()
        df = _make_raw(cfg, n=5)
        with pytest.raises(ValueError):
            mod.assert_source_shape(df, cfg)

    def test_passes_at_exactly_minimum_rows(self):
        cfg = _make_cfg()
        df = _make_raw(cfg, n=mod._MIN_EXPECTED_ROWS)
        mod.assert_source_shape(df, cfg)

    def test_raises_at_one_below_minimum(self):
        cfg = _make_cfg()
        df = _make_raw(cfg, n=mod._MIN_EXPECTED_ROWS - 1)
        with pytest.raises(ValueError):
            mod.assert_source_shape(df, cfg)


# ===========================================================================
# Tests: normalize()
# ===========================================================================

class TestNormalize:

    def test_name_normalized_column_added(self):
        cfg = _make_cfg()
        df = _make_raw(cfg)
        out = mod.normalize(df, cfg)
        assert "name_normalized" in out.columns

    def test_name_normalized_is_string(self):
        cfg = _make_cfg()
        df = _make_raw(cfg, PARK_NAME="Riverside Park")
        out = mod.normalize(df, cfg)
        assert isinstance(out["name_normalized"].iloc[0], str)

    def test_latitude_derived_from_polygon_bbox(self):
        """Polygon centroid: lat = (min_lat + max_lat) / 2."""
        cfg = _make_cfg()
        geom = _polygon_geometry(lon_min=-82.0, lon_max=-81.9, lat_min=28.0, lat_max=28.1)
        df = _make_raw(cfg, _geometry=geom)
        out = mod.normalize(df, cfg)
        assert out["latitude"].iloc[0] == pytest.approx(28.05)

    def test_longitude_derived_from_polygon_bbox(self):
        cfg = _make_cfg()
        geom = _polygon_geometry(lon_min=-82.0, lon_max=-81.9, lat_min=28.0, lat_max=28.1)
        df = _make_raw(cfg, _geometry=geom)
        out = mod.normalize(df, cfg)
        assert out["longitude"].iloc[0] == pytest.approx(-81.95)

    def test_null_geometry_gives_null_latlon(self):
        """Features without geometry must produce None lat/lon, not crash."""
        cfg = _make_cfg()
        df = _make_raw(cfg, _geometry=None)
        out = mod.normalize(df, cfg)
        assert pd.isna(out["latitude"].iloc[0])
        assert pd.isna(out["longitude"].iloc[0])

    def test_size_value_populated_when_area_field_present(self):
        cfg = _make_cfg(area_field="ACRES", area_unit="acres")
        df = _make_raw(cfg, ACRES=95.0)
        out = mod.normalize(df, cfg)
        assert out["size_value"].iloc[0] == pytest.approx(95.0)

    def test_size_metric_is_area_unit_when_area_present(self):
        cfg = _make_cfg(area_field="ACRES", area_unit="acres")
        df = _make_raw(cfg, ACRES=95.0)
        out = mod.normalize(df, cfg)
        assert out["size_metric"].iloc[0] == "acres"

    def test_size_unit_is_area_unit_when_area_present(self):
        cfg = _make_cfg(area_field="ACRES", area_unit="acres")
        df = _make_raw(cfg, ACRES=95.0)
        out = mod.normalize(df, cfg)
        assert out["size_unit"].iloc[0] == "acres"

    def test_size_fields_none_when_area_field_null_in_cfg(self):
        """When area_field is not configured, all size fields must be None."""
        cfg = _make_cfg(area_field=None, area_unit=None)
        df = _make_raw(cfg, ACRES=95.0)
        out = mod.normalize(df, cfg)
        assert out["size_value"].iloc[0] is None
        assert out["size_metric"].iloc[0] is None
        assert out["size_unit"].iloc[0] is None

    def test_size_value_nan_when_area_value_null(self):
        cfg = _make_cfg(area_field="ACRES", area_unit="acres")
        df = _make_raw(cfg, ACRES=None)
        out = mod.normalize(df, cfg)
        assert pd.isna(out["size_value"].iloc[0])

    def test_state_field_populates_state_column(self):
        """When state_field is set, _state comes from that column."""
        cfg = _make_cfg(state_field="STATE_ABBR", states=["FL", "TX"])
        df = _make_raw(cfg, STATE_ABBR="TX")
        out = mod.normalize(df, cfg)
        assert out["_state"].iloc[0] == "TX"

    def test_single_state_hardcoded_when_no_state_field(self):
        """state_field=None + one state in cfg → _state is that state."""
        cfg = _make_cfg(state_field=None, states=["FL"])
        df = _make_raw(cfg)
        out = mod.normalize(df, cfg)
        assert out["_state"].iloc[0] == "FL"

    def test_zip5_is_always_none(self):
        """Parks have no ZIP source — zip5 must always be None."""
        cfg = _make_cfg()
        df = _make_raw(cfg)
        out = mod.normalize(df, cfg)
        assert out["zip5"].iloc[0] is None

    def test_normalize_handles_multiple_rows(self):
        cfg = _make_cfg()
        df = _make_raw(cfg, n=5)
        out = mod.normalize(df, cfg)
        assert len(out) == 5


# ===========================================================================
# Tests: to_canonical()
# ===========================================================================

class TestToCanonical:

    def _norm(self, cfg: LayerConfig | None = None, **overrides) -> pd.DataFrame:
        if cfg is None:
            cfg = _make_cfg()
        return mod.normalize(_make_large_raw(cfg, **overrides), cfg)

    def test_all_canonical_columns_present(self):
        cfg = _make_cfg()
        out = mod.to_canonical(self._norm(cfg), cfg)
        missing = set(CANONICAL_COLUMNS) - set(out.columns)
        assert not missing, f"Missing columns: {missing}"

    def test_source_id_is_constant_from_cfg(self):
        cfg = _make_cfg(source_id="fdep_state_parks")
        out = mod.to_canonical(self._norm(cfg), cfg)
        assert (out["source_id"] == "fdep_state_parks").all()

    def test_vertical_is_parks(self):
        cfg = _make_cfg()
        out = mod.to_canonical(self._norm(cfg), cfg)
        assert (out["vertical"] == "parks").all()

    def test_account_type_flows_from_cfg(self):
        cfg = _make_cfg(account_type="state_park")
        out = mod.to_canonical(self._norm(cfg), cfg)
        assert (out["account_type"] == "state_park").all()

    def test_natural_key_uses_id_field(self):
        """When id_field is set, natural_key comes from that column."""
        cfg = _make_cfg(id_field="PARK_ID")
        df = _make_large_raw(cfg)
        # Set a known PARK_ID on the first row
        df.at[0, "PARK_ID"] = 42
        norm = mod.normalize(df, cfg)
        out = mod.to_canonical(norm, cfg)
        assert out["natural_key"].iloc[0] == "42"

    def test_natural_key_falls_back_to_objectid(self):
        """When id_field is None, natural_key comes from OBJECTID."""
        cfg = _make_cfg(id_field=None)
        df = _make_large_raw(cfg)
        df.at[0, "OBJECTID"] = 99
        norm = mod.normalize(df, cfg)
        out = mod.to_canonical(norm, cfg)
        assert out["natural_key"].iloc[0] == "99"

    def test_state_from_state_column(self):
        cfg = _make_cfg(state_field="STATE_ABBR", states=["FL", "NC"])
        df = _make_large_raw(cfg)
        df["STATE_ABBR"] = "NC"
        norm = mod.normalize(df, cfg)
        out = mod.to_canonical(norm, cfg)
        assert (out["state"] == "NC").all()

    def test_size_value_acres_flows_through(self):
        cfg = _make_cfg(area_field="ACRES", area_unit="acres")
        df = _make_large_raw(cfg)
        df["ACRES"] = 123.4
        norm = mod.normalize(df, cfg)
        out = mod.to_canonical(norm, cfg)
        assert out["size_value"].iloc[0] == pytest.approx(123.4)

    def test_phone_raw_is_none(self):
        cfg = _make_cfg()
        out = mod.to_canonical(self._norm(cfg), cfg)
        assert out["phone_raw"].iloc[0] is None

    def test_ein_is_none(self):
        cfg = _make_cfg()
        out = mod.to_canonical(self._norm(cfg), cfg)
        assert out["ein"].iloc[0] is None

    def test_county_fips_is_none(self):
        cfg = _make_cfg()
        out = mod.to_canonical(self._norm(cfg), cfg)
        assert out["county_fips"].iloc[0] is None

    def test_segment_municipal_for_municipal_park(self):
        """A municipal_park source resolves segment='municipal'."""
        cfg = _make_cfg(account_type="municipal_park")
        out = mod.to_canonical(self._norm(cfg), cfg)
        assert out["segment"].iloc[0] == SEGMENT_MUNICIPAL

    def test_segment_state_for_state_park(self):
        """A state_park source resolves segment='state'.

        Distinguishing the two is what activates lib.enums.SEGMENT_STATE, which was
        previously dead code because to_canonical always passed segment=None.  The
        two segments are different sales motions: one state agency with many sites
        versus one city with a few.
        """
        cfg = _make_cfg(account_type="state_park")
        out = mod.to_canonical(self._norm(cfg), cfg)
        assert out["segment"].iloc[0] == SEGMENT_STATE

    def test_row_count_preserved(self):
        cfg = _make_cfg()
        df = _make_large_raw(cfg, n=20)
        norm = mod.normalize(df, cfg)
        out = mod.to_canonical(norm, cfg)
        assert len(out) == 20

    def test_latitude_flows_through_from_normalize(self):
        cfg = _make_cfg()
        geom = _polygon_geometry(lon_min=-82.0, lon_max=-81.9, lat_min=28.0, lat_max=28.1)
        df = _make_large_raw(cfg)
        df["_geometry"] = [geom] * len(df)
        norm = mod.normalize(df, cfg)
        out = mod.to_canonical(norm, cfg)
        assert out["latitude"].iloc[0] == pytest.approx(28.05)

    def test_source_file_preserved(self):
        cfg = _make_cfg()
        out = mod.to_canonical(self._norm(cfg), cfg)
        assert "example.com" in out["source_file"].iloc[0]


# ===========================================================================
# Tests: report_quality()
# ===========================================================================

class TestReportQuality:

    def test_does_not_raise_on_well_formed_input(self):
        cfg = _make_cfg()
        df = mod.normalize(_make_raw(cfg), cfg)
        mod.report_quality(df, cfg)

    def test_does_not_raise_with_null_geometry(self):
        cfg = _make_cfg()
        df = mod.normalize(_make_raw(cfg, _geometry=None), cfg)
        mod.report_quality(df, cfg)

    def test_produces_stderr_output(self, capsys):
        cfg = _make_cfg()
        df = mod.normalize(_make_large_raw(cfg, n=15), cfg)
        mod.report_quality(df, cfg)
        captured = capsys.readouterr()
        assert "15" in captured.err

    def test_manager_breakdown_written_when_configured(self, capsys):
        cfg = _make_cfg(manager_field="MGT_AGENCY")
        df = _make_large_raw(cfg)
        df["MGT_AGENCY"] = "SCPRT"
        norm = mod.normalize(df, cfg)
        mod.report_quality(norm, cfg)
        captured = capsys.readouterr()
        assert "MGT_AGENCY" in captured.err


# ===========================================================================
# Tests: config_loader integration
# ===========================================================================

class TestConfigLoader:
    """Minimal integration tests for load_layer_config() + assert_config_shape()."""

    def test_loads_default_yaml_without_error(self):
        """The default park_layers.yaml must parse and validate cleanly."""
        from parks.config_loader import load_layer_config
        registry = load_layer_config()
        assert len(registry) > 0

    def test_all_default_entries_have_required_fields(self):
        from parks.config_loader import load_layer_config
        registry = load_layer_config()
        for source_id, cfg in registry.items():
            assert cfg.url.startswith("http"), f"{source_id}: url must be http(s)"
            assert cfg.name_field, f"{source_id}: name_field must be set"
            assert cfg.account_type, f"{source_id}: account_type must be set"
            assert cfg.states, f"{source_id}: states must not be empty"

    def test_padus_entry_present(self):
        from parks.config_loader import load_layer_config
        registry = load_layer_config()
        assert "padus_parks" in registry

    def test_all_five_state_specific_sources_present(self):
        from parks.config_loader import load_layer_config
        registry = load_layer_config()
        for expected in ("fdep_state_parks", "nc_state_parks", "sc_state_parks",
                         "tpwd_state_parks", "pasda_dcnr_parks"):
            assert expected in registry, f"Missing registry entry: {expected}"

    def test_invalid_config_raises_on_missing_url(self, tmp_path):
        from parks.config_loader import load_layer_config
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("broken:\n  where: '1=1'\n  states: [FL]\n  name_field: X\n  account_type: Y\n")
        with pytest.raises(ValueError, match="url"):
            load_layer_config(bad_yaml)


# ---------------------------------------------------------------- esri geometry

def _cw_ring(x=0.0, y=0.0, size=10.0) -> list:
    """A clockwise ring — ESRI's outer-ring convention."""
    return [[x, y], [x, y + size], [x + size, y + size], [x + size, y], [x, y]]


def _ccw_ring(x=4.0, y=4.0, size=2.0) -> list:
    """A counter-clockwise ring — ESRI's hole convention."""
    return [[x, y], [x + size, y], [x + size, y + size], [x, y + size], [x, y]]


class TestEsriRingsToGeojson:
    """The f=json sources (fdep, nc) return ESRI rings, not GeoJSON.

    ESRI packs every ring of a multi-part polygon into one flat list and marks
    holes only by winding direction, whereas GeoJSON nests [outer, hole, ...].
    Treating the flat list as GeoJSON coordinates would turn every hole into solid
    ground and inflate acreage — the one number this vertical must get right.
    """

    def test_single_outer_ring_is_a_polygon(self):
        g = mod.esri_rings_to_geojson({"rings": [_cw_ring()]})
        assert g["type"] == "Polygon"
        assert len(g["coordinates"]) == 1

    def test_hole_is_nested_not_promoted(self):
        g = mod.esri_rings_to_geojson({"rings": [_cw_ring(), _ccw_ring()]})
        assert g["type"] == "Polygon"
        assert len(g["coordinates"]) == 2

    def test_two_outer_rings_become_multipolygon(self):
        g = mod.esri_rings_to_geojson({
            "rings": [_cw_ring(0, 0), _cw_ring(50, 50)]
        })
        assert g["type"] == "MultiPolygon"
        assert len(g["coordinates"]) == 2

    def test_hole_assigned_to_containing_outer(self):
        g = mod.esri_rings_to_geojson({
            "rings": [_cw_ring(0, 0), _cw_ring(50, 50), _ccw_ring(4, 4)],
        })
        assert g["type"] == "MultiPolygon"
        sizes = sorted(len(p) for p in g["coordinates"])
        assert sizes == [1, 2]

    def test_output_follows_geojson_winding(self):
        """Outer CCW, holes CW per RFC 7946.  PostGIS keys off ring order rather
        than winding, so this is belt-and-braces for other consumers."""
        g = mod.esri_rings_to_geojson({"rings": [_cw_ring(), _ccw_ring()]})
        assert mod._ring_signed_area(g["coordinates"][0]) > 0
        assert mod._ring_signed_area(g["coordinates"][1]) < 0

    def test_all_ccw_input_promotes_largest_rather_than_dropping(self):
        """Some servers emit non-ESRI winding; classifying every ring as a hole
        would silently discard the feature."""
        g = mod.esri_rings_to_geojson({"rings": [_ccw_ring(0, 0, 10)]})
        assert g is not None
        assert g["type"] == "Polygon"

    def test_unattributable_hole_is_dropped(self):
        """Over-stating acreage is the worse error for a bid."""
        g = mod.esri_rings_to_geojson({
            "rings": [_cw_ring(0, 0), _ccw_ring(500, 500)],
        })
        assert g["type"] == "Polygon"
        assert len(g["coordinates"]) == 1

    def test_degenerate_rings_ignored(self):
        assert mod.esri_rings_to_geojson({"rings": [[[0, 0], [1, 1]]]}) is None

    @pytest.mark.parametrize("payload", [None, {}, {"rings": []}])
    def test_empty_inputs(self, payload):
        assert mod.esri_rings_to_geojson(payload) is None


class TestRingSignedArea:
    def test_sign_encodes_winding(self):
        assert mod._ring_signed_area(_ccw_ring(0, 0, 10)) > 0
        assert mod._ring_signed_area(_cw_ring(0, 0, 10)) < 0

    def test_magnitude_is_the_area(self):
        assert abs(mod._ring_signed_area(_cw_ring(0, 0, 10))) == pytest.approx(100.0)


class TestPointInRing:
    def test_inside(self):
        assert mod._point_in_ring([5, 5], _cw_ring(0, 0, 10))

    def test_outside(self):
        assert not mod._point_in_ring([50, 50], _cw_ring(0, 0, 10))


class TestToGeojsonGeometry:
    def test_geojson_passes_through(self):
        geom = {"type": "Polygon", "coordinates": [_cw_ring()]}
        assert mod.to_geojson_geometry(geom) is geom

    def test_esri_rings_converted(self):
        assert mod.to_geojson_geometry({"rings": [_cw_ring()]})["type"] == "Polygon"

    def test_esri_point_has_no_boundary(self):
        assert mod.to_geojson_geometry({"x": 1.0, "y": 2.0}) is None

    @pytest.mark.parametrize("payload", [None, {}, {"unexpected": 1}])
    def test_unusable_inputs(self, payload):
        assert mod.to_geojson_geometry(payload) is None


# ---------------------------------------------------------------- park attrs

class TestParkAttrRows:
    def _rows(self, cfg=None, n=1, **overrides):
        cfg = cfg or _make_cfg()
        raw = _make_raw(cfg, n=n, **overrides)
        return mod._park_attr_rows(mod.normalize(raw, cfg), cfg), cfg

    def test_one_row_per_park(self):
        rows, _ = self._rows(n=3)
        assert len(rows) == 3

    def test_natural_keys_match_to_canonical(self):
        """park_attrs joins staging.<source> on (source_id, natural_key), so two
        independent key derivations drifting apart would orphan every attribute
        row — boundaries and acreage would land but join to nothing."""
        cfg = _make_cfg()
        norm = mod.normalize(_make_raw(cfg, n=3), cfg)
        rows = mod._park_attr_rows(norm, cfg)
        canonical = mod.to_canonical(norm, cfg)
        assert [r["natural_key"] for r in rows] == canonical["natural_key"].tolist()

    def test_published_acreage_carried(self):
        rows, _ = self._rows()
        assert rows[0]["acres_published"] is not None

    def test_manager_normalized_only_when_field_is_a_name(self):
        """TPWD PropType and NC PK_TYPE are classification codes."""
        cfg = _make_cfg(manager_field="MGR", manager_field_role="classification")
        rows, _ = self._rows(cfg=cfg, MGR="SP")
        assert rows[0]["manager_raw"] == "SP"
        assert rows[0]["manager_normalized"] is None

    def test_manager_normalized_populated_for_real_names(self):
        cfg = _make_cfg(manager_field="MGR", manager_field_role="name")
        rows, _ = self._rows(cfg=cfg, MGR="City of Cary")
        assert rows[0]["manager_normalized"] == "CITY OF CARY"

    def test_nan_collapsed_to_none(self):
        """Postgres must get NULL, not the string 'nan'."""
        cfg = _make_cfg(owner_field="OWN")
        rows, _ = self._rows(cfg=cfg, OWN=float("nan"))
        assert rows[0]["owner_raw"] is None
