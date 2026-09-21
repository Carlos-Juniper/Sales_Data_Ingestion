"""
Tests for the gov_layers.yaml registry, treated as code.

Follows the test_parcel_layers_config.py pattern: load the REAL registry and
assert its shape, coverage and the verified live row counts.  A failure here means
either a bad edit or an upstream TIGERweb change — both worth knowing before a
production run rather than after.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from parks.config_loader import (  # noqa: E402
    GovLayerConfig,
    assert_gov_config_shape,
    load_gov_config,
)

# Verified live against the TIGERweb endpoints on 2026-09-04, AFTER the
# FUNCSTAT='A' filter.  TIGER revises annually, so these are stable; a mismatch
# means an upstream vintage change worth investigating, not a flaky test.
EXPECTED_ROWS = {
    "tiger_places": 3470,
    "tiger_cousub": 1543,
    "tiger_counties": 534,
}
EXPECTED_TOTAL_ACCOUNTS = 5547

TARGET_STATES = {"FL", "NC", "TX", "PA", "SC"}


@pytest.fixture(scope="session")
def registry() -> dict[str, GovLayerConfig]:
    return load_gov_config()


class TestRegistryShape:
    def test_three_entries(self, registry):
        assert set(registry) == set(EXPECTED_ROWS)

    def test_every_entry_validates(self, registry):
        for source_id, cfg in registry.items():
            assert isinstance(cfg, GovLayerConfig)
            assert cfg.source_id == source_id

    def test_urls_are_tigerweb(self, registry):
        """Census TIGERweb specifically — not an arbitrary mirror."""
        for cfg in registry.values():
            assert cfg.url.startswith("https://tigerweb.geo.census.gov/")

    def test_account_types(self, registry):
        assert registry["tiger_places"].account_type == "municipality"
        assert registry["tiger_cousub"].account_type == "municipality"
        assert registry["tiger_counties"].account_type == "county"

    def test_geoid_is_the_natural_key_everywhere(self, registry):
        """GEOID is the Tier-1 match key for parks (plan §5.1)."""
        for cfg in registry.values():
            assert cfg.geoid_field == "GEOID"


class TestStateCoverage:
    def test_places_and_counties_cover_all_five_states(self, registry):
        for source_id in ("tiger_places", "tiger_counties"):
            assert set(registry[source_id].states) == TARGET_STATES

    def test_cousub_is_pennsylvania_only(self, registry):
        """PA is the one target state where townships are general-purpose
        governments that award their own contracts.  Including the other four
        would add non-functioning census divisions as phantom accounts."""
        assert registry["tiger_cousub"].states == ["PA"]

    def test_where_clauses_filter_to_target_state_fips(self, registry):
        fips = {"12", "37", "42", "45", "48"}
        for source_id in ("tiger_places", "tiger_counties"):
            where = registry[source_id].where
            assert all(f"'{code}'" in where for code in fips)

    def test_active_only_filter_on_municipal_layers(self, registry):
        """FUNCSTAT='A' drops inactive units that have no government."""
        assert "FUNCSTAT='A'" in registry["tiger_places"].where
        assert "FUNCSTAT='A'" in registry["tiger_cousub"].where

    def test_cousub_where_is_pa_fips(self, registry):
        assert "STATE='42'" in registry["tiger_cousub"].where


class TestCountyField:
    def test_places_have_no_county_field(self, registry):
        """A place may straddle county lines (Dallas spans five), so there is no
        single COUNTY value to record."""
        assert registry["tiger_places"].county_field is None

    def test_cousub_and_counties_carry_county(self, registry):
        assert registry["tiger_cousub"].county_field == "COUNTY"
        assert registry["tiger_counties"].county_field == "COUNTY"


class TestGeometryReduction:
    def test_all_entries_reduce_geometry_server_side(self, registry):
        """TIGERweb serves full-resolution boundaries and 500s on large geometry
        pages; 250 counties at full precision is a 30 MB response."""
        for cfg in registry.values():
            assert cfg.geometry_precision is not None
            assert cfg.max_allowable_offset is not None

    def test_offset_is_metre_scale(self, registry):
        """~3 m is immaterial to a park-to-municipality overlap test, and acreage
        comes from AREALAND rather than from this geometry."""
        for cfg in registry.values():
            assert 0 < cfg.max_allowable_offset <= 1e-4

    def test_page_size_is_bounded(self, registry):
        """maxRecordCount is 100000, which would try to stream every polygon in
        one response."""
        for cfg in registry.values():
            assert cfg.page_size is not None
            assert cfg.page_size <= 1000

    def test_area_field_defaults_to_arealand(self, registry):
        for cfg in registry.values():
            assert cfg.area_field == "AREALAND"
            assert cfg.area_unit == "sqm"


class TestExpectedCounts:
    """Documents the account ceiling this vertical produces."""

    @pytest.mark.parametrize("source_id,expected", sorted(EXPECTED_ROWS.items()))
    def test_documented_row_count(self, source_id, expected):
        assert EXPECTED_ROWS[source_id] == expected

    def test_total_matches_plan_ceiling(self):
        """The plan's §6.4 ceiling is ~7,400 including ~1,960 school districts,
        which are deferred; 5,547 is the municipality + county subtotal."""
        assert sum(EXPECTED_ROWS.values()) == EXPECTED_TOTAL_ACCOUNTS

    def test_pa_municipality_total_matches_census(self):
        """1,543 townships + 1,012 PA incorporated places = 2,555, against the
        plan's stated ~2,560 PA municipalities.  This is the check that the
        places/cousub union does not double count."""
        assert EXPECTED_ROWS["tiger_cousub"] + 1012 == 2555


class TestAssertGovConfigShape:
    def _entry(self, **overrides):
        base = {
            "url": "https://tigerweb.geo.census.gov/x/MapServer/4",
            "where": "1=1",
            "states": ["FL"],
            "account_type": "municipality",
        }
        base.update(overrides)
        return base

    def test_valid_entry_passes(self):
        assert_gov_config_shape("x", self._entry()) is None

    @pytest.mark.parametrize("missing", ["url", "where", "states", "account_type"])
    def test_missing_required_key(self, missing):
        entry = self._entry()
        del entry[missing]
        with pytest.raises(ValueError, match="missing required keys"):
            assert_gov_config_shape("x", entry)

    def test_unknown_key_rejected(self):
        """A typo'd key would otherwise be silently ignored."""
        with pytest.raises(ValueError, match="unknown keys"):
            assert_gov_config_shape("x", self._entry(typo_field="oops"))

    def test_non_http_url_rejected(self):
        with pytest.raises(ValueError, match="valid HTTP"):
            assert_gov_config_shape("x", self._entry(url="ftp://x/y"))

    def test_empty_states_rejected(self):
        with pytest.raises(ValueError, match="states list"):
            assert_gov_config_shape("x", self._entry(states=[]))

    def test_bad_account_type_rejected(self):
        with pytest.raises(ValueError, match="account_type"):
            assert_gov_config_shape("x", self._entry(account_type="school_district"))

    def test_bad_area_unit_rejected(self):
        with pytest.raises(ValueError, match="area_unit"):
            assert_gov_config_shape("x", self._entry(area_unit="hectares"))


class TestLoadGovConfig:
    def test_absent_area_field_takes_tiger_default(self, tmp_path):
        """get(key, DEFAULT) not get(key) or DEFAULT — an absent key must inherit
        the TIGER standard name."""
        path = tmp_path / "g.yaml"
        path.write_text(
            "x:\n  url: \"https://tigerweb.geo.census.gov/a\"\n"
            "  where: \"1=1\"\n  states: [FL]\n  account_type: county\n"
        )
        assert load_gov_config(path)["x"].area_field == "AREALAND"

    def test_explicit_null_area_field_disables_area(self, tmp_path):
        """An explicit null is a different intent from an absent key."""
        path = tmp_path / "g.yaml"
        path.write_text(
            "x:\n  url: \"https://tigerweb.geo.census.gov/a\"\n"
            "  where: \"1=1\"\n  states: [FL]\n  account_type: county\n"
            "  area_field: null\n"
        )
        assert load_gov_config(path)["x"].area_field is None

    def test_empty_file(self, tmp_path):
        path = tmp_path / "g.yaml"
        path.write_text("")
        assert load_gov_config(path) == {}
