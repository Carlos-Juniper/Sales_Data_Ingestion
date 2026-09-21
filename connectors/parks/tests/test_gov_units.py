"""
Tests for parks.gov_units — the TIGERweb government-unit connector.

No network: every HTTP call is intercepted at requests.Session.get, matching the
pattern used by the healthcare connector tests.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from parks import gov_units as mod  # noqa: E402
from parks.config_loader import GovLayerConfig  # noqa: E402


# ---------------------------------------------------------------- factories

def _make_cfg(**overrides) -> GovLayerConfig:
    defaults = dict(
        source_id="tiger_places",
        url="https://tigerweb.geo.census.gov/x/MapServer/4",
        where="STATE IN ('12') AND FUNCSTAT='A'",
        states=["FL", "NC", "TX", "PA", "SC"],
        account_type="municipality",
        county_field=None,
        page_size=500,
    )
    defaults.update(overrides)
    return GovLayerConfig(**defaults)


def _polygon(lon: float = -81.5, lat: float = 28.5) -> dict:
    d = 0.01
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon, lat], [lon, lat + d], [lon + d, lat + d], [lon + d, lat], [lon, lat],
        ]],
    }


def _feature(geoid="1200375", name="Alachua city", basename="Alachua",
             state="12", county="001", arealand=16111680,
             lat="+29.7", lon="-082.4", geometry=True) -> dict:
    return {
        "type": "Feature",
        "properties": {
            "GEOID": geoid, "NAME": name, "BASENAME": basename,
            "STATE": state, "COUNTY": county, "AREALAND": arealand,
            "INTPTLAT": lat, "INTPTLON": lon,
        },
        "geometry": _polygon() if geometry else None,
    }


def _make_raw(cfg=None, n=1, **overrides) -> pd.DataFrame:
    cfg = cfg or _make_cfg()
    rows = []
    for i in range(n):
        props = dict(_feature(geoid=f"120{i:04d}")["properties"])
        props.update(overrides)
        props["_geometry"] = _polygon()
        props["source_file"] = cfg.url
        rows.append(props)
    return pd.DataFrame(rows)


def _response(features: list[dict], exceeded=None) -> MagicMock:
    resp = MagicMock()
    payload = {"type": "FeatureCollection", "features": features}
    if exceeded is not None:
        payload["exceededTransferLimit"] = exceeded
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------- out fields

class TestBuildOutFields:
    def test_includes_all_mapped_fields(self):
        fields = mod._build_out_fields(_make_cfg()).split(",")
        for expected in ("GEOID", "NAME", "BASENAME", "STATE", "INTPTLAT",
                         "INTPTLON", "AREALAND"):
            assert expected in fields

    def test_omits_county_when_not_configured(self):
        """Places have no single COUNTY value."""
        assert "COUNTY" not in mod._build_out_fields(_make_cfg(county_field=None))

    def test_includes_county_when_configured(self):
        assert "COUNTY" in mod._build_out_fields(_make_cfg(county_field="COUNTY"))

    def test_is_deduplicated_and_sorted(self):
        fields = mod._build_out_fields(_make_cfg(order_by_field="GEOID")).split(",")
        assert fields == sorted(set(fields))

    def test_omits_area_when_disabled(self):
        assert "AREALAND" not in mod._build_out_fields(_make_cfg(area_field=None))


class TestGeometryParams:
    def test_both_reduction_params_emitted(self):
        params = mod._geometry_params(_make_cfg())
        assert params == {"geometryPrecision": 6, "maxAllowableOffset": 3e-5}

    def test_omitted_when_none(self):
        cfg = _make_cfg(geometry_precision=None, max_allowable_offset=None)
        assert mod._geometry_params(cfg) == {}

    def test_partial_configuration(self):
        cfg = _make_cfg(max_allowable_offset=None)
        assert mod._geometry_params(cfg) == {"geometryPrecision": 6}


# ---------------------------------------------------------------- fetch

class TestFetch:
    def test_requests_geometry_and_reduction_params(self):
        with patch("requests.Session.get", return_value=_response([_feature()])) as get:
            mod.fetch(_make_cfg(), session=requests.Session())
        params = get.call_args.kwargs["params"]
        assert params["returnGeometry"] == "true"
        assert params["geometryPrecision"] == 6
        assert params["maxAllowableOffset"] == 3e-5

    def test_page_size_skips_the_metadata_probe(self):
        """Passing max_record_count avoids a second HTTP round trip per source."""
        with patch("requests.Session.get", return_value=_response([_feature()])) as get:
            mod.fetch(_make_cfg(page_size=500), session=requests.Session())
        assert get.call_count == 1
        assert get.call_args.kwargs["params"]["resultRecordCount"] == 500

    def test_always_orders_results(self):
        """Unordered offset paging silently duplicates and drops rows."""
        with patch("requests.Session.get", return_value=_response([_feature()])) as get:
            mod.fetch(_make_cfg(), session=requests.Session())
        assert get.call_args.kwargs["params"]["orderByFields"] == "GEOID"

    def test_geometry_and_source_file_attached(self):
        with patch("requests.Session.get", return_value=_response([_feature()])):
            df = mod.fetch(_make_cfg(), session=requests.Session())
        assert df.loc[0, "_geometry"]["type"] == "Polygon"
        assert df.loc[0, "source_file"].startswith("https://tigerweb")

    def test_empty_response(self):
        with patch("requests.Session.get", return_value=_response([])):
            assert mod.fetch(_make_cfg(), session=requests.Session()).empty


# ---------------------------------------------------------------- checks

class TestAssertSourceShape:
    def test_passes_on_healthy_frame(self):
        cfg = _make_cfg()
        assert mod.assert_source_shape(_make_raw(cfg, n=3500), cfg) is None

    def test_missing_required_column(self):
        cfg = _make_cfg()
        df = _make_raw(cfg, n=3500).drop(columns=["GEOID"])
        with pytest.raises(ValueError):
            mod.assert_source_shape(df, cfg)

    def test_row_count_floor(self):
        """A truncated spine is worse than none: missing municipalities become
        parks misattributed to their county."""
        cfg = _make_cfg()
        with pytest.raises(ValueError):
            mod.assert_source_shape(_make_raw(cfg, n=10), cfg)

    def test_geometry_required(self):
        """A geometry-less spine cannot anchor the spatial rollup."""
        cfg = _make_cfg()
        df = _make_raw(cfg, n=3500)
        df["_geometry"] = None
        with pytest.raises(ValueError, match="carry geometry"):
            mod.assert_source_shape(df, cfg)

    def test_unknown_source_uses_default_floor(self):
        cfg = _make_cfg(source_id="tiger_unknown")
        assert mod.assert_source_shape(_make_raw(cfg, n=150), cfg) is None


# ---------------------------------------------------------------- normalize

class TestNormalize:
    def test_state_fips_converted_to_abbreviation(self):
        cfg = _make_cfg()
        out = mod.normalize(_make_raw(cfg, STATE="12"), cfg)
        assert out.loc[0, "state_abbr"] == "FL"

    def test_zero_padded_fips_handled(self):
        """FIPS codes are zero-padded strings and the leading zero matters."""
        cfg = _make_cfg()
        out = mod.normalize(_make_raw(cfg, STATE="1"), cfg)
        assert out.loc[0, "state_abbr"] == "AL"

    def test_name_normalized_comes_from_basename(self):
        """NAME carries the LSAD suffix ('Cary town'); matching a manager string
        against that would leave a stray civic token on one side."""
        cfg = _make_cfg()
        out = mod.normalize(_make_raw(cfg, NAME="Cary town", BASENAME="Cary"), cfg)
        assert out.loc[0, "name_normalized"] == "CARY"
        assert out.loc[0, "name_display"] == "Cary town"

    def test_signed_padded_coordinates_parsed(self):
        """TIGER emits '+34.1785440' / '-082.3776868'."""
        cfg = _make_cfg()
        out = mod.normalize(_make_raw(cfg, INTPTLAT="+34.1785440",
                                      INTPTLON="-082.3776868"), cfg)
        assert out.loc[0, "latitude"] == pytest.approx(34.178544)
        assert out.loc[0, "longitude"] == pytest.approx(-82.3776868)

    def test_county_fips_null_for_places(self):
        cfg = _make_cfg(county_field=None)
        assert mod.normalize(_make_raw(cfg), cfg).loc[0, "county_fips"] is None

    def test_county_fips_is_five_digits(self):
        cfg = _make_cfg(county_field="COUNTY")
        out = mod.normalize(_make_raw(cfg, STATE="12", COUNTY="1"), cfg)
        assert out.loc[0, "county_fips"] == "12001"

    def test_arealand_converted_to_acres(self):
        cfg = _make_cfg()
        out = mod.normalize(_make_raw(cfg, AREALAND=4046.8564224), cfg)
        assert out.loc[0, "size_value"] == pytest.approx(1.0)
        assert out.loc[0, "size_unit"] == "acres"

    def test_size_metric_labels_land_area(self):
        """This is the unit's total land area, never a maintained-turf figure."""
        cfg = _make_cfg()
        out = mod.normalize(_make_raw(cfg), cfg)
        assert out.loc[0, "size_metric"] == "land_area_acres"

    def test_area_disabled(self):
        cfg = _make_cfg(area_field=None)
        out = mod.normalize(_make_raw(cfg), cfg)
        assert out.loc[0, "size_value"] is None
        assert out.loc[0, "size_unit"] is None


class TestFilterToTargetStates:
    def test_keeps_in_scope_rows(self):
        cfg = _make_cfg()
        df = mod.normalize(_make_raw(cfg, STATE="12"), cfg)
        assert len(mod.filter_to_target_states(df, cfg)) == 1

    def test_drops_out_of_scope_rows(self):
        """D10: the isin filter must run client-side before any DB write, because
        a hand-edited WHERE clause is exactly what silently widens scope."""
        cfg = _make_cfg()
        df = mod.normalize(_make_raw(cfg, STATE="06"), cfg)
        assert mod.filter_to_target_states(df, cfg).empty

    def test_reports_dropped_count(self, capsys):
        cfg = _make_cfg()
        df = mod.normalize(_make_raw(cfg, STATE="06"), cfg)
        mod.filter_to_target_states(df, cfg)
        assert "filtered 1" in capsys.readouterr().err


# ---------------------------------------------------------------- canonical

class TestToCanonical:
    def _out(self, cfg=None, **overrides):
        cfg = cfg or _make_cfg()
        return mod.to_canonical(mod.normalize(_make_raw(cfg, **overrides), cfg), cfg)

    def test_canonical_column_set(self):
        from lib.schema import CANONICAL_COLUMNS
        assert list(self._out().columns) == CANONICAL_COLUMNS

    def test_natural_key_is_geoid(self):
        """GEOID is the parks Tier-1 match key (plan §5.1)."""
        assert self._out(GEOID="3710740").loc[0, "natural_key"] == "3710740"

    def test_vertical_and_account_type(self):
        out = self._out()
        assert out.loc[0, "vertical"] == "parks"
        assert out.loc[0, "account_type"] == "municipality"

    def test_county_account_type(self):
        cfg = _make_cfg(source_id="tiger_counties", account_type="county",
                        county_field="COUNTY")
        assert self._out(cfg).loc[0, "account_type"] == "county"

    def test_no_contact_fields_populated(self):
        """TIGER carries no contact data of any kind."""
        out = self._out()
        for col in ("phone_raw", "phone_normalized", "ein"):
            assert out.loc[0, col] is None


# ---------------------------------------------------------------- boundaries

class TestBoundaryRows:
    def test_one_row_per_feature(self):
        cfg = _make_cfg()
        norm = mod.normalize(_make_raw(cfg, n=3), cfg)
        assert len(mod._boundary_rows(norm, cfg)) == 3

    def test_geojson_serialized(self):
        cfg = _make_cfg()
        rows = mod._boundary_rows(mod.normalize(_make_raw(cfg), cfg), cfg)
        assert json.loads(rows[0]["geojson"])["type"] == "Polygon"

    def test_area_comes_from_arealand_not_geometry(self):
        """AREALAND is the Census's authoritative full-resolution land area, and is
        unaffected by the server-side generalization applied to the boundary."""
        cfg = _make_cfg()
        norm = mod.normalize(_make_raw(cfg, AREALAND=4046.8564224), cfg)
        assert mod._boundary_rows(norm, cfg)[0]["area_acres"] == pytest.approx(1.0)

    def test_missing_geometry_yields_null(self):
        cfg = _make_cfg()
        norm = mod.normalize(_make_raw(cfg), cfg)
        norm["_geometry"] = None
        assert mod._boundary_rows(norm, cfg)[0]["geojson"] is None

    def test_keys_match_canonical_natural_key(self):
        """park_rollup and the spine join on natural_key, so the two must agree."""
        cfg = _make_cfg()
        norm = mod.normalize(_make_raw(cfg, n=3), cfg)
        canonical = mod.to_canonical(norm, cfg)
        rows = mod._boundary_rows(norm, cfg)
        assert [r["natural_key"] for r in rows] == canonical["natural_key"].tolist()


# ---------------------------------------------------------------- provenance

class TestBuildRawBytes:
    def test_deterministic(self):
        raw = _make_raw(n=3)
        assert mod.build_raw_bytes(raw) == mod.build_raw_bytes(raw)

    def test_geometry_replaced_by_hash_not_dropped(self):
        """Carrying real coordinates would be ~90 MB for tiger_places; dropping
        geometry entirely would make a boundary revision produce an identical run
        hash and read as 'no change'."""
        payload = json.loads(mod.build_raw_bytes(_make_raw(n=1)).decode())
        assert "_geom_sha256" in payload[0]
        assert len(payload[0]["_geom_sha256"]) == 64
        assert "_geometry" not in payload[0]

    def test_geometry_change_changes_the_hash(self):
        a = _make_raw(n=1)
        b = a.copy()
        b.at[0, "_geometry"] = _polygon(lon=-80.0, lat=27.0)
        assert mod.build_raw_bytes(a) != mod.build_raw_bytes(b)

    def test_null_geometry_yields_null_hash(self):
        raw = _make_raw(n=1)
        raw["_geometry"] = None
        payload = json.loads(mod.build_raw_bytes(raw).decode())
        assert payload[0]["_geom_sha256"] is None


class TestModuleContract:
    def test_vertical_constant(self):
        assert mod.VERTICAL == "parks"

    def test_row_floors_defined_for_every_registry_entry(self):
        from parks.config_loader import load_gov_config
        for source_id in load_gov_config():
            assert source_id in mod._MIN_EXPECTED_ROWS
