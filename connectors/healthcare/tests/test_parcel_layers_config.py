"""
Structural and coverage tests for connectors/healthcare/config/parcel_layers.yaml.

No network calls are made — these tests load the YAML from disk and exercise
the shape-validation helpers and layer-map builder defined in parcel_acreage_enrich.
They are intended to catch registry regressions (missing entries, bad field names,
wrong area_unit) before they surface as silent enrichment failures in production.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest
from healthcare.parcel_acreage_enrich import (
    LayerConfig,
    _build_layer_maps,
    assert_config_shape,
    load_layer_config,
)

# ---------------------------------------------------------------------------
# Shared fixture — load the real YAML once per test session.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def raw_config() -> dict:
    """Return the parsed parcel_layers.yaml dict, validated by load_layer_config."""
    return load_layer_config()


# ---------------------------------------------------------------------------
# 1. YAML loads without error
# ---------------------------------------------------------------------------


def test_yaml_loads_without_error() -> None:
    """load_layer_config() must not raise on the bundled YAML."""
    load_layer_config()


# ---------------------------------------------------------------------------
# 2. assert_config_shape passes on the real YAML
# ---------------------------------------------------------------------------


def test_assert_config_shape_passes_on_real_yaml(raw_config: dict) -> None:
    """assert_config_shape() must not raise on the current registry."""
    assert_config_shape(raw_config)


# ---------------------------------------------------------------------------
# 3 & 4. State-level county count targets
# ---------------------------------------------------------------------------


# The registry used to assert that 15 TX and 10 PA counties were *present*.
# Those assertions passed while every one of the 25 URLs was dead, so they
# measured intent rather than coverage. Every endpoint was probed on 2026-09-21;
# the results below are what actually resolves. Re-verify before editing.
#
# A county earns an entry only when its area field holds real LOT area:
#   Travis  SDE_TCAD_PARCELS_AREA — 100% NULL in sampled rows
#   Dallas  DEEDACRES — 0 in sampled rows; SQFT is building area, not lot area
KNOWN_COVERAGE_GAPS = {
    "TX": ["48201", "48113", "48453", "48029", "48085", "48439", "48121",
           "48339", "48157", "48491", "48167", "48039", "48215", "48141", "48355"],
    "PA": ["42101", "42003", "42017", "42045", "42029", "42071", "42133", "42043", "42079"],
}


@pytest.mark.parametrize("state", sorted(KNOWN_COVERAGE_GAPS))
def test_known_coverage_gaps_are_still_absent(raw_config: dict, state: str) -> None:
    """
    Pin the counties we know we do NOT cover.

    This fails when someone adds one of them, which is the point: the new entry
    must be accompanied by removing the FIPS from KNOWN_COVERAGE_GAPS, so the
    documented gap list can never silently drift from the registry.
    """
    configured = set(raw_config["county"].get(state) or {})
    resolved = sorted(configured & set(KNOWN_COVERAGE_GAPS[state]))
    assert not resolved, (
        f"{state} counties {resolved} are now configured but still listed as known "
        "gaps — verify the endpoint returns real lot area, then drop them from "
        "KNOWN_COVERAGE_GAPS."
    )


def test_montgomery_pa_is_the_verified_county_entry(raw_config: dict) -> None:
    """Montgomery PA (42091) is the one county endpoint verified to return lot acreage."""
    entry = (raw_config["county"].get("PA") or {}).get("42091")
    assert entry is not None, "42091 (Montgomery PA) is the only verified county source"
    assert entry["area_field"] == "LAND_ACRES"
    assert entry["area_unit"] == "acres"


def test_no_entry_uses_shape_area(raw_config: dict) -> None:
    """
    Shape__Area / SHAPE_Area is in the layer's own projection.

    On a Web Mercator layer it is not convertible to acres by any fixed factor,
    so treating it as sqft silently produces wrong acreage rather than an error.
    Only a recorded-area attribute (LAND_ACRES, LND_SQFOOT, CALC_ACRES) is valid.
    """
    offenders = [
        f"{ctx}:{entry['area_field']}"
        for ctx, entry in _all_entries(raw_config)
        if entry.get("area_field", "").replace("_", "").lower() == "shapearea"
    ]
    assert not offenders, f"area_field must not be a projected shape area: {offenders}"


# ---------------------------------------------------------------------------
# 7. All entries have required fields
# ---------------------------------------------------------------------------


def _all_entries(raw: dict) -> list[tuple[str, dict]]:
    """Yield (context_label, entry_dict) for every statewide and county entry."""
    entries: list[tuple[str, dict]] = []
    for state, cfg in (raw.get("statewide") or {}).items():
        entries.append((f"statewide.{state}", cfg))
    for state, counties in (raw.get("county") or {}).items():
        for fips, cfg in (counties or {}).items():
            entries.append((f"county.{state}.{fips}", cfg))
    return entries


_REQUIRED_FIELDS = {"url", "area_field", "area_unit", "parcel_id_field"}


def test_all_entries_have_required_fields(raw_config: dict) -> None:
    """Every registry entry must contain all four required fields."""
    for context, cfg in _all_entries(raw_config):
        missing = _REQUIRED_FIELDS - set(cfg.keys())
        assert not missing, (
            f"Entry {context!r} is missing required field(s): {missing}"
        )


# ---------------------------------------------------------------------------
# 8. All area_unit values are valid
# ---------------------------------------------------------------------------

_VALID_AREA_UNITS = {"sqft", "acres"}


def test_all_area_units_valid(raw_config: dict) -> None:
    """Every entry's area_unit must be 'sqft' or 'acres'."""
    for context, cfg in _all_entries(raw_config):
        unit = cfg.get("area_unit")
        assert unit in _VALID_AREA_UNITS, (
            f"Entry {context!r} has invalid area_unit={unit!r}; "
            f"must be one of {_VALID_AREA_UNITS}"
        )


# ---------------------------------------------------------------------------
# 9 & 10. Statewide layer presence
# ---------------------------------------------------------------------------


def test_fl_statewide_is_configured(raw_config: dict) -> None:
    """FL must appear in the statewide section (Florida Statewide Cadastral)."""
    assert "FL" in raw_config["statewide"], (
        "'FL' is missing from statewide in parcel_layers.yaml"
    )


def test_nc_statewide_is_configured(raw_config: dict) -> None:
    """NC must appear in the statewide section (NC OneMap Statewide Parcels)."""
    assert "NC" in raw_config["statewide"], (
        "'NC' is missing from statewide in parcel_layers.yaml"
    )


# ---------------------------------------------------------------------------
# 11. assert_config_shape raises on missing top-level key
# ---------------------------------------------------------------------------


def test_assert_config_shape_raises_on_missing_top_level_key() -> None:
    """assert_config_shape() must raise AssertionError when 'county' is absent."""
    with pytest.raises(AssertionError, match="missing top-level section"):
        assert_config_shape({"statewide": {}})


# ---------------------------------------------------------------------------
# 12. assert_config_shape raises on invalid area_unit
# ---------------------------------------------------------------------------


def test_assert_config_shape_raises_on_invalid_area_unit() -> None:
    """assert_config_shape() must raise ValueError when area_unit is not sqft/acres."""
    bad_raw = {
        "statewide": {
            "XX": {
                "url": "https://example.com/arcgis/rest/services/Parcels/FeatureServer/0",
                "area_field": "AREA",
                "area_unit": "meters",
                "parcel_id_field": "PID",
            }
        },
        "county": {},
    }
    with pytest.raises(ValueError, match="invalid area_unit"):
        assert_config_shape(bad_raw)


# ---------------------------------------------------------------------------
# 13. assert_config_shape raises on missing required field
# ---------------------------------------------------------------------------


def test_assert_config_shape_raises_on_missing_required_field() -> None:
    """assert_config_shape() must raise ValueError when parcel_id_field is absent."""
    bad_raw = {
        "statewide": {
            "XX": {
                "url": "https://example.com/arcgis/rest/services/Parcels/FeatureServer/0",
                "area_field": "AREA",
                "area_unit": "sqft",
                # parcel_id_field intentionally omitted
            }
        },
        "county": {},
    }
    with pytest.raises(ValueError, match="missing required field"):
        assert_config_shape(bad_raw)


# ---------------------------------------------------------------------------
# 14. _build_layer_maps returns correct types
# ---------------------------------------------------------------------------


def test_build_layer_maps_returns_correct_types(raw_config: dict) -> None:
    """_build_layer_maps() must return (dict[str, LayerConfig], dict[str, dict[str, LayerConfig]])."""
    statewide, county = _build_layer_maps(raw_config)

    assert isinstance(statewide, dict), "first return value must be a dict"
    for state, cfg in statewide.items():
        assert isinstance(state, str), f"statewide key {state!r} must be str"
        assert isinstance(cfg, LayerConfig), (
            f"statewide[{state!r}] must be a LayerConfig, got {type(cfg)}"
        )

    assert isinstance(county, dict), "second return value must be a dict"
    for state, fips_map in county.items():
        assert isinstance(state, str), f"county key {state!r} must be str"
        assert isinstance(fips_map, dict), (
            f"county[{state!r}] must be a dict, got {type(fips_map)}"
        )
        for fips, cfg in fips_map.items():
            assert isinstance(fips, str), (
                f"county[{state!r}] key {fips!r} must be str"
            )
            assert isinstance(cfg, LayerConfig), (
                f"county[{state!r}][{fips!r}] must be a LayerConfig, got {type(cfg)}"
            )


# ---------------------------------------------------------------------------
# 15. LayerConfig.to_acres() conversion
# ---------------------------------------------------------------------------


def test_fl_layer_config_area_conversion(raw_config: dict) -> None:
    """
    FL uses sqft: to_acres(43560.0) must return approx 1.0.
    NC uses acres: to_acres(2.5) must return approx 2.5 (pass-through).
    """
    statewide, _ = _build_layer_maps(raw_config)

    fl_config = statewide["FL"]
    assert fl_config.to_acres(43560.0) == pytest.approx(1.0), (
        "FL uses sqft — 43,560 sqft should convert to exactly 1.0 acre"
    )

    nc_config = statewide["NC"]
    assert nc_config.to_acres(2.5) == pytest.approx(2.5), (
        "NC uses acres — to_acres() should return the raw value unchanged"
    )
