"""
Tests for parks.parks_merge — polygon dedup, acreage reconciliation, and the
government-as-account rollup.
"""

from __future__ import annotations

import json
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from parks import parks_merge as mod  # noqa: E402
from parks.config_loader import LayerConfig  # noqa: E402

_GEOJSON = json.dumps({
    "type": "Polygon",
    "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]],
})


def _park(source_id="padus_parks", natural_key="P1", name="Bond Park",
          published=310.0, computed=312.0, gov_key="3710740",
          gov_source="tiger_places", boundary=_GEOJSON, **overrides) -> dict:
    row = {
        "source_id": source_id,
        "natural_key": natural_key,
        "account_type": "municipal_park",
        "name_raw": name,
        "name_normalized": name.upper(),
        "address_line_1": None,
        "city": None,
        "state": "NC",
        "zip5": None,
        "latitude": 35.0,
        "longitude": -78.0,
        "segment": "municipal",
        "size_value": published,
        "acres_published": published,
        "acres_computed": computed,
        "owner_raw": None,
        "manager_raw": None,
        "boundary_geojson": boundary,
        "gov_source_id": gov_source,
        "gov_natural_key": gov_key,
        "rollup_method": "spatial+name",
        "rollup_score": 1.0,
    }
    row.update(overrides)
    return row


def _parks(*rows: dict) -> pd.DataFrame:
    return pd.DataFrame(list(rows))


def _pair(a_key="P1", b_key="S1", a_src="padus_parks",
          b_src="pasda_dcnr_parks", iou=0.93) -> dict:
    return {
        "a_source_id": a_src, "a_natural_key": a_key,
        "b_source_id": b_src, "b_natural_key": b_key, "iou": iou,
    }


def _gov(source_id="tiger_places", natural_key="3710740", name="Cary town",
         account_type="municipality", state="NC") -> dict:
    return {
        "source_id": source_id,
        "natural_key": natural_key,
        "account_type": account_type,
        "name_raw": name,
        "name_normalized": name.split()[0].upper(),
        "state": state,
        "county_fips": None,
        "latitude": 35.7,
        "longitude": -78.8,
    }


# ---------------------------------------------------------------- precedence

class TestSourcePrecedence:
    def test_state_layers_outrank_padus(self):
        """Plan §5.3: state layer beats the national aggregate for geometry."""
        assert mod._precedence("fdep_state_parks") < mod._precedence("padus_parks")
        assert mod._precedence("nc_state_parks") < mod._precedence("padus_parks")

    def test_state_inventory_outranks_padus(self):
        assert mod._precedence("pasda_dcnr_parks") < mod._precedence("padus_parks")

    def test_unknown_source_sorts_last(self):
        assert mod._precedence("something_new") > mod._precedence("padus_parks")


# ---------------------------------------------------------------- dedup

class TestPolygonDedup:
    def test_high_iou_and_similar_name_merges(self):
        parks = _parks(_park(natural_key="P1"),
                       _park(source_id="pasda_dcnr_parks", natural_key="S1"))
        out = mod.polygon_dedup(parks, pd.DataFrame([_pair()]))
        assert len(out) == 1

    def test_survivor_is_highest_precedence_source(self):
        parks = _parks(_park(natural_key="P1"),
                       _park(source_id="pasda_dcnr_parks", natural_key="S1"))
        out = mod.polygon_dedup(parks, pd.DataFrame([_pair()]))
        assert out.iloc[0]["source_id"] == "pasda_dcnr_parks"

    def test_merged_sources_records_all_identities(self):
        """Provenance is preserved, not discarded (§5.2)."""
        parks = _parks(_park(natural_key="P1"),
                       _park(source_id="pasda_dcnr_parks", natural_key="S1"))
        out = mod.polygon_dedup(parks, pd.DataFrame([_pair()]))
        merged = out.iloc[0]["merged_sources"]
        assert "padus_parks:P1" in merged
        assert "pasda_dcnr_parks:S1" in merged

    def test_dissimilar_names_block_the_merge(self):
        """Geometry alone would merge a park with the preserve containing it."""
        parks = _parks(
            _park(natural_key="P1", name="Bond Park"),
            _park(source_id="pasda_dcnr_parks", natural_key="S1",
                  name="Completely Different Preserve"),
        )
        out = mod.polygon_dedup(parks, pd.DataFrame([_pair()]))
        assert len(out) == 2

    def test_same_source_pairs_are_not_offered(self):
        """load_iou_pairs excludes them; two overlapping polygons from one source
        are usually a genuine sub-unit relationship."""
        parks = _parks(_park(natural_key="P1"), _park(natural_key="P2"))
        out = mod.polygon_dedup(parks, pd.DataFrame())
        assert len(out) == 2

    def test_three_sources_collapse_to_one(self):
        """Connected components, so a park described by three sources yields one
        record rather than two."""
        parks = _parks(
            _park(source_id="padus_parks", natural_key="P1"),
            _park(source_id="pasda_dcnr_parks", natural_key="S1"),
            _park(source_id="fdep_state_parks", natural_key="F1"),
        )
        pairs = pd.DataFrame([
            _pair("P1", "S1", "padus_parks", "pasda_dcnr_parks"),
            _pair("S1", "F1", "pasda_dcnr_parks", "fdep_state_parks"),
        ])
        out = mod.polygon_dedup(parks, pairs)
        assert len(out) == 1
        assert out.iloc[0]["source_id"] == "fdep_state_parks"

    def test_merge_iou_recorded(self):
        parks = _parks(_park(natural_key="P1"),
                       _park(source_id="pasda_dcnr_parks", natural_key="S1"))
        out = mod.polygon_dedup(parks, pd.DataFrame([_pair(iou=0.88)]))
        assert out.iloc[0]["merge_iou"] == pytest.approx(0.88)

    def test_singleton_has_null_iou_and_own_provenance(self):
        out = mod.polygon_dedup(_parks(_park(natural_key="P1")), pd.DataFrame())
        assert out.iloc[0]["merge_iou"] is None
        assert out.iloc[0]["merged_sources"] == "padus_parks:P1"

    def test_coalesces_missing_fields_from_folded_rows(self):
        parks = _parks(
            _park(source_id="pasda_dcnr_parks", natural_key="S1", computed=None),
            _park(source_id="padus_parks", natural_key="P1", computed=999.0),
        )
        out = mod.polygon_dedup(parks, pd.DataFrame([_pair()]))
        assert out.iloc[0]["acres_computed"] == 999.0

    def test_empty_input(self):
        out = mod.polygon_dedup(pd.DataFrame(columns=["source_id", "natural_key"]),
                                pd.DataFrame())
        assert out.empty

    def test_pair_referencing_unknown_park_is_ignored(self):
        parks = _parks(_park(natural_key="P1"))
        out = mod.polygon_dedup(parks, pd.DataFrame([_pair("P1", "GHOST")]))
        assert len(out) == 1


class TestThresholds:
    def test_match_plan_section_5_2(self):
        """'intersection-over-union > 0.60 of boundary geometry plus name
        similarity > 0.5'"""
        assert mod.IOU_THRESHOLD == 0.60
        assert mod.NAME_THRESHOLD == 0.50


# ---------------------------------------------------------------- acreage

class TestResolveAcreage:
    def test_measured_preferred_over_published(self):
        """acres_computed is measured on the ellipsoid from the real boundary;
        published figures can be wrong (TPWD's Web Mercator Shape__Area)."""
        out = mod.resolve_acreage(_parks(_park(published=310.0, computed=312.0)))
        assert out.iloc[0]["maintained_acres"] == 312.0
        assert out.iloc[0]["acres_confidence"] == "measured"

    def test_published_only_is_estimated(self):
        out = mod.resolve_acreage(_parks(_park(published=310.0, computed=None)))
        assert out.iloc[0]["maintained_acres"] == 310.0
        assert out.iloc[0]["acres_confidence"] == "estimated"

    def test_neither_is_banded(self):
        out = mod.resolve_acreage(_parks(_park(published=None, computed=None)))
        assert out.iloc[0]["acres_confidence"] == "banded"
        assert pd.isna(out.iloc[0]["maintained_acres"])

    def test_computed_only_is_measured(self):
        """TPWD's case after area_field was disabled."""
        out = mod.resolve_acreage(_parks(_park(published=None, computed=500.0)))
        assert out.iloc[0]["maintained_acres"] == 500.0
        assert out.iloc[0]["acres_confidence"] == "measured"

    def test_falls_back_to_size_value(self):
        out = mod.resolve_acreage(
            _parks(_park(published=None, computed=None, size_value=42.0))
        )
        assert out.iloc[0]["maintained_acres"] == 42.0
        assert out.iloc[0]["acres_confidence"] == "estimated"

    def test_agreement_within_tolerance_not_flagged(self):
        out = mod.resolve_acreage(_parks(_park(published=310.0, computed=312.0)))
        assert not out.iloc[0]["acres_disagrees"]

    def test_disagreement_flagged(self):
        """TPWD's Web Mercator inflation was ~35%."""
        out = mod.resolve_acreage(_parks(_park(published=310.0, computed=420.0)))
        assert out.iloc[0]["acres_disagrees"]
        assert out.iloc[0]["acres_variance"] == pytest.approx(110 / 310)

    def test_zero_published_not_divided_by(self):
        out = mod.resolve_acreage(_parks(_park(published=0.0, computed=5.0)))
        assert not out.iloc[0]["acres_disagrees"]

    def test_confidence_values_match_core_schema(self):
        """core.location.acres_confidence documents measured|estimated|banded."""
        out = mod.resolve_acreage(_parks(
            _park(natural_key="a", published=1.0, computed=1.0),
            _park(natural_key="b", published=1.0, computed=None),
            _park(natural_key="c", published=None, computed=None, size_value=None),
        ))
        assert set(out["acres_confidence"]) <= {"measured", "estimated", "banded"}


class TestReportAcreage:
    def test_reports_confidence_breakdown(self, capsys):
        out = mod.resolve_acreage(_parks(_park()))
        mod.report_acreage(out)
        assert "measured" in capsys.readouterr().err

    def test_reports_disagreement_by_source(self, capsys):
        out = mod.resolve_acreage(_parks(_park(published=310.0, computed=420.0)))
        mod.report_acreage(out)
        err = capsys.readouterr().err
        assert "disagree" in err
        assert "padus_parks" in err


# ---------------------------------------------------------------- accounts

class TestBuildResolvedAccount:
    def test_account_per_government_not_per_park(self):
        """The whole point of §6.4: three parks in one city is ONE account."""
        parks = mod.resolve_acreage(_parks(
            _park(natural_key="P1"), _park(natural_key="P2"), _park(natural_key="P3"),
        ))
        out = mod.build_resolved_account(pd.DataFrame([_gov()]), parks, {})
        assert len(out) == 1

    def test_account_key_derives_from_geoid(self):
        from lib.keys import compute_account_key
        parks = mod.resolve_acreage(_parks(_park()))
        out = mod.build_resolved_account(pd.DataFrame([_gov()]), parks, {})
        expected = compute_account_key({"geoid": "3710740"}, priority=("geoid",))
        assert out.iloc[0]["account_key"] == expected

    def test_geoid_recorded_in_external_keys(self):
        parks = mod.resolve_acreage(_parks(_park()))
        out = mod.build_resolved_account(pd.DataFrame([_gov()]), parks, {})
        assert json.loads(out.iloc[0]["external_keys"])["geoid"] == "3710740"

    def test_size_metric_is_summed_park_acreage(self):
        """The rolled-up total is the number a rep quotes."""
        parks = mod.resolve_acreage(_parks(
            _park(natural_key="P1", computed=100.0),
            _park(natural_key="P2", computed=250.0),
        ))
        out = mod.build_resolved_account(pd.DataFrame([_gov()]), parks, {})
        assert out.iloc[0]["size_metric"] == pytest.approx(350.0)
        assert out.iloc[0]["size_metric_unit"] == "acres"

    def test_government_with_no_parks_excluded(self):
        """An account with nothing to maintain is not a lead."""
        parks = mod.resolve_acreage(_parks(_park(gov_key="3710740")))
        gov = pd.DataFrame([_gov(), _gov(natural_key="3799999", name="Parkless town")])
        out = mod.build_resolved_account(gov, parks, {})
        assert out["_gov_natural_key"].tolist() == ["3710740"]

    def test_state_agency_account_created(self):
        registry = {
            "tpwd_state_parks": LayerConfig(
                source_id="tpwd_state_parks",
                url="https://x/FeatureServer/0", where="1=1", states=["TX"],
                name_field="ParkName", account_type="state_park",
                managing_agency="Texas Parks and Wildlife Department",
                managing_agency_slug="tpwd",
            )
        }
        parks = mod.resolve_acreage(_parks(
            _park(source_id="tpwd_state_parks", natural_key="T1",
                  gov_source="state_agency", gov_key="tpwd", computed=1000.0)
        ))
        out = mod.build_resolved_account(pd.DataFrame(), parks, registry)
        assert len(out) == 1
        assert out.iloc[0]["account_type"] == "state_agency"
        assert out.iloc[0]["legal_name"] == "Texas Parks and Wildlife Department"
        assert out.iloc[0]["size_metric"] == pytest.approx(1000.0)

    def test_agency_key_is_stable_and_distinct(self):
        from lib.keys import compute_account_key
        a = compute_account_key({"agency": "tpwd"}, priority=("agency",))
        b = compute_account_key({"agency": "fdep"}, priority=("agency",))
        assert a != b
        assert a == compute_account_key({"agency": "tpwd"}, priority=("agency",))

    def test_all_rows_carry_parks_vertical(self):
        parks = mod.resolve_acreage(_parks(_park()))
        out = mod.build_resolved_account(pd.DataFrame([_gov()]), parks, {})
        assert set(out["vertical"]) == {"parks"}


class TestBuildResolvedLocation:
    def _built(self, parks):
        resolved = mod.resolve_acreage(parks)
        gov = pd.DataFrame([_gov()])
        account = mod.build_resolved_account(gov, resolved, {})
        return mod.build_resolved_location(resolved, account), account

    def test_one_location_per_park(self):
        loc, _ = self._built(_parks(_park(natural_key="P1"), _park(natural_key="P2")))
        assert len(loc) == 2

    def test_location_keys_distinct_without_addresses(self):
        """Every park in a city shares one account and has no street address, so
        without a discriminator they would all collapse to one location_key."""
        loc, _ = self._built(_parks(
            _park(natural_key="P1"), _park(natural_key="P2"), _park(natural_key="P3"),
        ))
        assert loc["location_key"].nunique() == 3

    def test_location_key_is_deterministic(self):
        a, _ = self._built(_parks(_park(natural_key="P1")))
        b, _ = self._built(_parks(_park(natural_key="P1")))
        assert a.iloc[0]["location_key"] == b.iloc[0]["location_key"]

    def test_points_and_boundaries_both_populated(self):
        loc, _ = self._built(_parks(_park()))
        row = loc.iloc[0]
        assert row["_latitude"] == 35.0
        assert row["_longitude"] == -78.0
        assert json.loads(row["_boundary_geojson"])["type"] == "Polygon"

    def test_site_type_is_park(self):
        loc, _ = self._built(_parks(_park()))
        assert loc.iloc[0]["site_type"] == "park"

    def test_geometry_source_distinguishes_padus(self):
        loc, _ = self._built(_parks(_park(source_id="padus_parks")))
        assert loc.iloc[0]["geometry_source"] == "padus"
        loc2, _ = self._built(_parks(_park(source_id="fdep_state_parks")))
        assert loc2.iloc[0]["geometry_source"] == "state_layer"

    def test_park_without_account_is_skipped(self):
        loc, _ = self._built(_parks(_park(gov_key="9999999")))
        assert loc.empty

    def test_acreage_carried_through(self):
        loc, _ = self._built(_parks(_park(computed=312.0)))
        assert loc.iloc[0]["maintained_acres"] == 312.0
        assert loc.iloc[0]["acres_confidence"] == "measured"

    def test_empty_account_frame(self):
        assert mod.build_resolved_location(_parks(_park()), pd.DataFrame()).empty


class TestBuildResolvedContact:
    def test_empty_by_design(self):
        """Plan §6.4: park-level data carries no contact information at all."""
        assert mod.build_resolved_contact().empty

    def test_has_the_resolved_contact_columns(self):
        """Empty but correctly shaped, so the writer binds cleanly."""
        cols = mod.build_resolved_contact().columns
        for expected in ("contact_key", "account_key", "vertical", "role"):
            assert expected in cols


class TestPrintSummary:
    def test_reports_account_and_location_counts(self, capsys):
        parks = mod.resolve_acreage(_parks(_park()))
        gov = pd.DataFrame([_gov()])
        account = mod.build_resolved_account(gov, parks, {})
        loc = mod.build_resolved_location(parks, account)
        mod.print_summary(parks, account, loc)
        err = capsys.readouterr().err
        assert "resolved_account" in err
        assert "total rolled-up acreage" in err

    def test_warns_on_orphaned_parks(self, capsys):
        parks = mod.resolve_acreage(_parks(_park(gov_key="9999999")))
        gov = pd.DataFrame([_gov()])
        account = mod.build_resolved_account(gov, parks, {})
        loc = mod.build_resolved_location(parks, account)
        mod.print_summary(parks, account, loc)
        assert "WARNING" in capsys.readouterr().err


class TestLoadIouPairs:
    """An empty result set is a normal state, not an error."""

    def _engine(self):
        from unittest.mock import MagicMock
        engine, conn = MagicMock(), MagicMock()
        engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        return engine

    def test_empty_result_returns_shaped_frame(self):
        """A first run against an empty park_attrs returns a frame with NO columns,
        so filtering on 'iou' would raise KeyError."""
        from unittest.mock import patch
        with patch("pandas.read_sql", return_value=pd.DataFrame()):
            out = mod.load_iou_pairs(self._engine(), ["padus_parks"])
        assert out.empty
        assert "iou" in out.columns

    def test_empty_result_feeds_polygon_dedup_cleanly(self):
        from unittest.mock import patch
        with patch("pandas.read_sql", return_value=pd.DataFrame()):
            pairs = mod.load_iou_pairs(self._engine(), ["padus_parks"])
        out = mod.polygon_dedup(_parks(_park()), pairs)
        assert len(out) == 1

    def test_below_threshold_pairs_filtered_out(self):
        from unittest.mock import patch
        rows = pd.DataFrame([
            {**_pair(iou=0.95)}, {**_pair(a_key="P9", iou=0.10)},
        ])
        with patch("pandas.read_sql", return_value=rows):
            out = mod.load_iou_pairs(self._engine(), ["padus_parks"])
        assert len(out) == 1
        assert out.iloc[0]["iou"] == 0.95

    def test_null_iou_filtered_out(self):
        from unittest.mock import patch
        rows = pd.DataFrame([{**_pair(iou=None)}])
        with patch("pandas.read_sql", return_value=rows):
            out = mod.load_iou_pairs(self._engine(), ["padus_parks"])
        assert out.empty
