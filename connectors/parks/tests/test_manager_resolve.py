"""
Tests for parks.manager_resolve — the §5.4 manager-string -> GEOID join.

The plan calls this join the most likely way the parks vertical slips, so the
string logic is tested exhaustively here against the real traps found in live
PAD-US data rather than only on happy-path inputs.
"""

from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from parks.manager_resolve import (  # noqa: E402
    AUTO_THRESHOLD,
    METHOD_COUNTY,
    METHOD_DECLARED,
    METHOD_NAME,
    METHOD_SPATIAL,
    METHOD_SPATIAL_NAME,
    QUEUE_THRESHOLD,
    build_gov_index,
    candidates_for,
    combine_assignments,
    declared_agency_assignments,
    enqueue_review_pairs,
    parse_manager_string,
    resolve_manager_names,
    review_candidates,
    role_word_trim_sequence,
    score_manager_against_gov,
    strip_trailing_role_words,
)
from parks.config_loader import LayerConfig  # noqa: E402


# ---------------------------------------------------------------- factories

def _gov(source_id: str, natural_key: str, state: str, name: str) -> dict:
    return {
        "gov_source_id": source_id,
        "gov_natural_key": natural_key,
        "state": state,
        "name_normalized": name,
    }


def _gov_df(*rows: dict) -> pd.DataFrame:
    return pd.DataFrame(list(rows))


# A small stand-in for the real spine, carrying the ambiguities that matter.
_SPINE = _gov_df(
    _gov("tiger_places", "3710740", "NC", "CARY"),
    _gov("tiger_places", "3770540", "NC", "WAKE FOREST"),
    _gov("tiger_counties", "37183", "NC", "WAKE"),
    _gov("tiger_places", "4504690", "SC", "BEAUFORT"),
    _gov("tiger_counties", "45013", "SC", "BEAUFORT"),
    _gov("tiger_places", "4805984", "TX", "BAY CITY"),
    _gov("tiger_places", "4806128", "TX", "BAYTOWN"),
    _gov("tiger_counties", "48201", "TX", "HARRIS"),
    _gov("tiger_counties", "48203", "TX", "HARRISON"),
    _gov("tiger_places", "4205888", "PA", "BERWICK"),
    _gov("tiger_cousub", "4200105880", "PA", "BERWICK"),
    _gov("tiger_places", "1278300", "FL", "WINTER PARK"),
    _gov("tiger_places", "1256975", "FL", "PINELLAS PARK"),
)


def _resolve_one(state: str, manager: str, spine: pd.DataFrame = _SPINE) -> pd.Series:
    df = pd.DataFrame([{"state": state, "manager_normalized": manager}])
    return resolve_manager_names(df, spine).iloc[0]


# ---------------------------------------------------------------- parsing

class TestStripTrailingRoleWords:
    def test_strips_departmental_tail(self):
        assert strip_trailing_role_words(
            ["CARY", "PARKS", "RECREATION", "CULTURAL", "RESOURCES"]
        ) == ["CARY"]

    def test_strips_bare_numerals(self):
        """Precinct/district indices are numerals and never end a place name."""
        assert strip_trailing_role_words(
            ["HARRIS", "COUNTY", "PRECINCT", "4", "PARKS"]
        ) == ["HARRIS", "COUNTY"]

    def test_never_strips_from_the_middle(self):
        """Only the tail is eligible — a role word mid-string is untouched."""
        assert strip_trailing_role_words(["CARY", "PARKS", "DEPARTMENT"]) == ["CARY"]
        assert strip_trailing_role_words(["OVERLAND", "PARK", "TRAIL"]) == [
            "OVERLAND", "PARK", "TRAIL",
        ]

    def test_maximal_trim_can_overshoot(self):
        """Documents WHY role_word_trim_sequence exists.

        This function trims greedily, so a place whose own name ends in role
        vocabulary is cut too far.  The intermediate depths are recovered by
        role_word_trim_sequence, not here.
        """
        assert strip_trailing_role_words(
            ["WINTER", "PARK", "PARKS", "AND", "RECREATION"]
        ) == ["WINTER"]


class TestRoleWordTrimSequence:
    def test_ladder_includes_every_depth(self):
        """37 real places end in a role word, so the correct name can sit at an
        intermediate trim depth."""
        ladder = role_word_trim_sequence(["WINTER", "PARK", "PARKS", "RECREATION"])
        assert ["WINTER"] in ladder
        assert ["WINTER", "PARK"] in ladder
        assert ["WINTER", "PARK", "PARKS", "RECREATION"] in ladder

    def test_most_trimmed_first_untouched_last(self):
        ladder = role_word_trim_sequence(["CARY", "PARKS", "DEPARTMENT"])
        assert ladder[0] == ["CARY"]
        assert ladder[-1] == ["CARY", "PARKS", "DEPARTMENT"]

    def test_no_role_words_yields_single_entry(self):
        assert role_word_trim_sequence(["HILTON", "HEAD", "ISLAND"]) == [
            ["HILTON", "HEAD", "ISLAND"]
        ]

    def test_empty_input(self):
        assert role_word_trim_sequence([]) == []

    def test_always_keeps_at_least_one_token(self):
        """An all-role-word string is not erased to nothing."""
        assert strip_trailing_role_words(["PARKS", "RECREATION"]) == ["PARKS"]

    def test_empty_input(self):
        assert strip_trailing_role_words([]) == []


class TestParseManagerString:
    @pytest.mark.parametrize(
        "raw,expected_class",
        [
            ("CITY OF CHARLESTON", "place"),
            ("TOWN OF CARY", "place"),
            ("VILLAGE OF X", "place"),
            ("BOROUGH OF BERWICK", "place"),
            ("TOWNSHIP OF UPPER DARBY", "cousub"),
            ("COUNTY OF WAKE", "county"),
            ("BEAUFORT COUNTY", "county"),
            ("BERWICK TOWNSHIP", "cousub"),
            ("HILTON HEAD ISLAND", None),
        ],
    )
    def test_entity_class_detected(self, raw, expected_class):
        """The entity hint is load-bearing: Beaufort County and City of Beaufort
        are different governments with the same base name."""
        _, entity_class = parse_manager_string(raw)
        assert entity_class == expected_class

    def test_longest_prefix_wins(self):
        """'COUNTY OF' must not be shadowed by a shorter prefix match."""
        variants, entity_class = parse_manager_string("COUNTY OF WAKE")
        assert entity_class == "county"
        assert "WAKE" in variants

    def test_entity_suffix_offers_both_forms(self):
        """161 real places have a BASENAME ending in an entity word, so the
        de-suffixed form cannot be the only candidate."""
        variants, _ = parse_manager_string("CITY OF BAY CITY")
        assert variants == ["BAY", "BAY CITY"]

    def test_entity_word_found_after_role_stripping(self):
        """'Wake County Parks, Recreation and Open Space' puts COUNTY mid-string;
        role words must be removed before the entity word is looked for."""
        variants, entity_class = parse_manager_string(
            "WAKE COUNTY PARKS RECREATION AND OPEN SPACE"
        )
        assert entity_class == "county"
        assert variants[0] == "WAKE"

    def test_full_form_always_retained(self):
        variants, _ = parse_manager_string("CITY OF CARY PARKS RECREATION")
        assert "CARY PARKS RECREATION" in variants

    def test_no_entity_suffix_when_it_is_the_only_token(self):
        """A manager literally named 'COUNTY' is not erased."""
        variants, _ = parse_manager_string("COUNTY")
        assert variants == ["COUNTY"]

    @pytest.mark.parametrize("empty", [None, "", "   ", float("nan"), pd.NA])
    def test_empty_inputs_yield_no_variants(self, empty):
        variants, entity_class = parse_manager_string(empty)
        assert variants == []
        assert entity_class is None

    def test_variants_are_deduplicated_in_order(self):
        variants, _ = parse_manager_string("CARY")
        assert variants == ["CARY"]


# ---------------------------------------------------------------- scoring

class TestScoreManagerAgainstGov:
    def test_exact_match_scores_one(self):
        cands = [("tiger_places", "3710740", "CARY", 0)]
        best, score = score_manager_against_gov(["CARY"], cands)
        assert best == ("tiger_places", "3710740")
        assert score == 1.0

    def test_no_candidates(self):
        assert score_manager_against_gov(["CARY"], []) == (None, 0.0)

    def test_longer_variant_wins_on_tie(self):
        """Both BAY and BAY CITY match exactly; the specific name is right."""
        cands = [
            ("tiger_places", "111", "BAY", 0),
            ("tiger_places", "222", "BAY CITY", 0),
        ]
        best, score = score_manager_against_gov(["BAY", "BAY CITY"], cands)
        assert best == ("tiger_places", "222")
        assert score == 1.0

    def test_lower_source_rank_wins_on_tie(self):
        """Berwick borough vs Berwick township — only the rank separates them."""
        cands = [
            ("tiger_places", "4205888", "BERWICK", 1),
            ("tiger_cousub", "4200105880", "BERWICK", 0),
        ]
        best, _ = score_manager_against_gov(["BERWICK"], cands)
        assert best == ("tiger_cousub", "4200105880")

    def test_tie_break_is_order_independent(self):
        """Reversing candidate order must not change the winner."""
        cands = [
            ("tiger_cousub", "4200105880", "BERWICK", 0),
            ("tiger_places", "4205888", "BERWICK", 1),
        ]
        assert score_manager_against_gov(["BERWICK"], cands)[0] == (
            "tiger_cousub", "4200105880",
        )
        assert score_manager_against_gov(["BERWICK"], list(reversed(cands)))[0] == (
            "tiger_cousub", "4200105880",
        )


class TestCandidatesFor:
    def test_entity_class_excludes_other_layers(self):
        index = build_gov_index(_SPINE)
        cands = candidates_for(index, "SC", ["BEAUFORT"], "county")
        assert {c[0] for c in cands} == {"tiger_counties"}

    def test_no_hint_permits_all_layers_by_default_priority(self):
        index = build_gov_index(_SPINE)
        cands = candidates_for(index, "SC", ["BEAUFORT"], None)
        assert {c[0] for c in cands} == {"tiger_places", "tiger_counties"}

    def test_blocking_is_state_scoped(self):
        index = build_gov_index(_SPINE)
        assert candidates_for(index, "TX", ["CARY"], None) == []

    def test_cousub_hint_ranks_cousub_first(self):
        index = build_gov_index(_SPINE)
        cands = candidates_for(index, "PA", ["BERWICK"], "cousub")
        ranks = {c[0]: c[3] for c in cands}
        assert ranks["tiger_cousub"] < ranks["tiger_places"]


# ---------------------------------------------------------------- end to end

class TestResolveManagerNames:
    @pytest.mark.parametrize(
        "state,manager,expected_key",
        [
            # The plan's own worked example (§5.4).
            ("NC", "CITY OF CARY PARKS RECREATION CULTURAL RESOURCES", "3710740"),
            # County vs place with the same base name.
            ("SC", "BEAUFORT COUNTY", "45013"),
            ("SC", "CITY OF BEAUFORT", "4504690"),
            # A basename that ends in an entity word.
            ("TX", "CITY OF BAY CITY", "4805984"),
            ("TX", "CITY OF BAYTOWN", "4806128"),
            # Entity word behind the role words.
            ("NC", "WAKE COUNTY PARKS RECREATION AND OPEN SPACE", "37183"),
            # Texas commissioner precincts.
            ("TX", "HARRIS COUNTY PRECINCT 4 PARKS", "48201"),
            # Borough vs township.
            ("PA", "BERWICK TOWNSHIP", "4200105880"),
            ("PA", "BOROUGH OF BERWICK", "4205888"),
            # Place names that END in role vocabulary — the correct name sits at
            # an intermediate trim depth, not at the maximal trim.
            ("FL", "CITY OF WINTER PARK PARKS AND RECREATION DEPARTMENT", "1278300"),
            ("FL", "CITY OF PINELLAS PARK PUBLIC WORKS", "1256975"),
        ],
    )
    def test_real_world_manager_strings(self, state, manager, expected_key):
        assert _resolve_one(state, manager).gov_natural_key == expected_key

    def test_harris_not_confused_with_harrison(self):
        """A near neighbour must not win on fuzzy score."""
        assert _resolve_one("TX", "HARRIS COUNTY PARKS").gov_natural_key == "48201"

    def test_unmatchable_string_scores_below_queue_floor(self):
        """Garbage must not produce a confident assignment."""
        row = _resolve_one("NC", "ZZZZ QQQQ NONSENSE")
        assert row.score < QUEUE_THRESHOLD

    def test_null_manager_yields_no_match(self):
        row = _resolve_one("NC", None)
        assert row.gov_source_id is None
        assert row.score == 0.0

    def test_resolution_is_per_distinct_string(self):
        """Duplicate manager strings collapse to one resolution."""
        df = pd.DataFrame([
            {"state": "NC", "manager_normalized": "CITY OF CARY"},
            {"state": "NC", "manager_normalized": "CITY OF CARY"},
            {"state": "NC", "manager_normalized": "COUNTY OF WAKE"},
        ])
        assert len(resolve_manager_names(df, _SPINE)) == 2

    def test_empty_input(self):
        out = resolve_manager_names(
            pd.DataFrame(columns=["state", "manager_normalized"]), _SPINE
        )
        assert out.empty


# ---------------------------------------------------------------- combine

def _parks_df(*rows: tuple[str, str, str, str | None]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "park_source_id": src,
            "park_natural_key": key,
            "state": state,
            "manager_normalized": mgr,
        }
        for src, key, state, mgr in rows
    ])


class TestCombineAssignments:
    def _run(self, parks, spatial=None, names=None, counties=None, declared=None):
        empty = pd.DataFrame()
        return combine_assignments(
            parks,
            spatial if spatial is not None else empty,
            names if names is not None else empty,
            counties if counties is not None else empty,
            declared if declared is not None else empty,
        )

    def test_declared_agency_wins_outright(self):
        parks = _parks_df(("tpwd_state_parks", "F", "TX", None))
        declared = pd.DataFrame([{
            "park_source_id": "tpwd_state_parks", "park_natural_key": "F",
            "gov_source_id": "state_agency", "gov_natural_key": "tpwd",
        }])
        spatial = pd.DataFrame([{
            "park_source_id": "tpwd_state_parks", "park_natural_key": "F",
            "gov_source_id": "tiger_places", "gov_natural_key": "4805000",
        }])
        out = self._run(parks, spatial=spatial, declared=declared)
        assert out.iloc[0]["method"] == METHOD_DECLARED
        assert out.iloc[0]["gov_natural_key"] == "tpwd"

    def test_agreement_is_labelled_spatial_plus_name(self):
        parks = _parks_df(("padus_parks", "A", "NC", "CITY OF CARY"))
        gov = {"gov_source_id": "tiger_places", "gov_natural_key": "3710740"}
        spatial = pd.DataFrame([{**gov, "park_source_id": "padus_parks", "park_natural_key": "A"}])
        names = pd.DataFrame([{
            **gov, "state": "NC", "manager_normalized": "CITY OF CARY",
            "score": 1.0, "entity_class": "place",
        }])
        out = self._run(parks, spatial=spatial, names=names)
        assert out.iloc[0]["method"] == METHOD_SPATIAL_NAME

    def test_spatial_wins_when_name_disagrees(self):
        parks = _parks_df(("padus_parks", "B", "NC", "CITY OF CARY"))
        spatial = pd.DataFrame([{
            "park_source_id": "padus_parks", "park_natural_key": "B",
            "gov_source_id": "tiger_places", "gov_natural_key": "9999999",
        }])
        names = pd.DataFrame([{
            "state": "NC", "manager_normalized": "CITY OF CARY",
            "gov_source_id": "tiger_places", "gov_natural_key": "3710740",
            "score": 1.0, "entity_class": "place",
        }])
        out = self._run(parks, spatial=spatial, names=names)
        assert out.iloc[0]["method"] == METHOD_SPATIAL
        assert out.iloc[0]["gov_natural_key"] == "9999999"

    def test_name_only_accepted_above_queue_floor(self):
        parks = _parks_df(("padus_parks", "C", "NC", "SOMETHING"))
        names = pd.DataFrame([{
            "state": "NC", "manager_normalized": "SOMETHING",
            "gov_source_id": "tiger_places", "gov_natural_key": "3710740",
            "score": 0.80, "entity_class": None,
        }])
        out = self._run(parks, names=names)
        assert out.iloc[0]["method"] == METHOD_NAME

    def test_weak_name_match_is_discarded(self):
        """Below the review floor a name result is not evidence of anything."""
        parks = _parks_df(("padus_parks", "C", "NC", "SOMETHING"))
        names = pd.DataFrame([{
            "state": "NC", "manager_normalized": "SOMETHING",
            "gov_source_id": "tiger_places", "gov_natural_key": "3710740",
            "score": 0.40, "entity_class": None,
        }])
        counties = pd.DataFrame([{
            "park_source_id": "padus_parks", "park_natural_key": "C",
            "gov_source_id": "tiger_counties", "gov_natural_key": "37183",
        }])
        out = self._run(parks, names=names, counties=counties)
        assert out.iloc[0]["method"] == METHOD_COUNTY

    def test_county_fallback_is_last_resort(self):
        parks = _parks_df(("padus_parks", "D", "NC", None))
        counties = pd.DataFrame([{
            "park_source_id": "padus_parks", "park_natural_key": "D",
            "gov_source_id": "tiger_counties", "gov_natural_key": "37183",
        }])
        out = self._run(parks, counties=counties)
        assert out.iloc[0]["method"] == METHOD_COUNTY

    def test_unresolved_park_is_kept_with_null_method(self):
        """Nothing is silently dropped — an unassignable park is still reported."""
        parks = _parks_df(("padus_parks", "E", "NC", None))
        out = self._run(parks)
        assert len(out) == 1
        assert pd.isna(out.iloc[0]["method"])
        assert out.iloc[0]["gov_source_id"] is None

    def test_every_park_appears_exactly_once(self):
        parks = _parks_df(
            ("padus_parks", "A", "NC", "CITY OF CARY"),
            ("padus_parks", "B", "NC", "CITY OF CARY"),
            ("padus_parks", "C", "NC", None),
        )
        out = self._run(parks)
        assert len(out) == 3
        assert not out[["park_source_id", "park_natural_key"]].duplicated().any()

    def test_nan_from_left_join_is_not_treated_as_present(self):
        """bool(float('nan')) is True — presence must be checked properly or every
        park looks like it has a declared agency."""
        parks = _parks_df(("padus_parks", "A", "NC", None))
        declared = pd.DataFrame([{
            "park_source_id": "padus_parks", "park_natural_key": "OTHER",
            "gov_source_id": "state_agency", "gov_natural_key": "tpwd",
        }])
        out = self._run(parks, declared=declared)
        assert pd.isna(out.iloc[0]["method"])


class TestReviewCandidates:
    def _combined(self, method, score):
        return pd.DataFrame([{
            "park_source_id": "padus_parks", "park_natural_key": "A",
            "gov_source_id": "tiger_places", "gov_natural_key": "3710740",
            "method": method, "score": score,
        }])

    def test_name_only_in_band_is_queued(self):
        out = review_candidates(self._combined(METHOD_NAME, 0.80), pd.DataFrame())
        assert len(out) == 1

    @pytest.mark.parametrize("score", [0.70, 0.95, 1.0])
    def test_outside_band_not_queued(self, score):
        out = review_candidates(self._combined(METHOD_NAME, score), pd.DataFrame())
        assert out.empty

    def test_corroborated_match_not_queued(self):
        """spatial+name agreement needs no human adjudication."""
        out = review_candidates(self._combined(METHOD_SPATIAL_NAME, 0.80), pd.DataFrame())
        assert out.empty

    def test_empty_input(self):
        assert review_candidates(pd.DataFrame(), pd.DataFrame()).empty


class TestDeclaredAgencyAssignments:
    def _cfg(self, source_id, slug):
        return LayerConfig(
            source_id=source_id,
            url="https://example.com/FeatureServer/0",
            where="1=1",
            states=["TX"],
            name_field="NAME",
            account_type="state_park" if slug else "municipal_park",
            managing_agency="An Agency" if slug else None,
            managing_agency_slug=slug,
        )

    def test_declared_sources_assigned_to_agency(self):
        parks = _parks_df(
            ("tpwd_state_parks", "1", "TX", None),
            ("padus_parks", "2", "TX", "CITY OF X"),
        )
        registry = {
            "tpwd_state_parks": self._cfg("tpwd_state_parks", "tpwd"),
            "padus_parks": self._cfg("padus_parks", None),
        }
        out = declared_agency_assignments(parks, registry)
        assert len(out) == 1
        assert out.iloc[0]["gov_source_id"] == "state_agency"
        assert out.iloc[0]["gov_natural_key"] == "tpwd"

    def test_no_declared_sources(self):
        parks = _parks_df(("padus_parks", "2", "TX", None))
        registry = {"padus_parks": self._cfg("padus_parks", None)}
        assert declared_agency_assignments(parks, registry).empty

    def test_declared_source_with_no_parks(self):
        parks = _parks_df(("padus_parks", "2", "TX", None))
        registry = {
            "tpwd_state_parks": self._cfg("tpwd_state_parks", "tpwd"),
            "padus_parks": self._cfg("padus_parks", None),
        }
        assert declared_agency_assignments(parks, registry).empty


class TestThresholds:
    def test_thresholds_match_plan_tier3(self):
        """Plan §5.1: >=0.92 auto-merge, 0.75-0.92 review."""
        assert AUTO_THRESHOLD == 0.92
        assert QUEUE_THRESHOLD == 0.75


class TestEnqueueReviewPairs:
    """Uncorroborated name matches in the 0.75-0.92 band go to human review."""

    def _engine(self):
        from unittest.mock import MagicMock
        engine, conn = MagicMock(), MagicMock()
        engine.begin.return_value.__enter__ = MagicMock(return_value=conn)
        engine.begin.return_value.__exit__ = MagicMock(return_value=False)
        return engine

    def _queued(self, score=0.80):
        return pd.DataFrame([{
            "park_source_id": "padus_parks", "park_natural_key": "P1",
            "gov_source_id": "tiger_places", "gov_natural_key": "3710740",
            "method": METHOD_NAME, "score": score,
        }])

    def test_pairs_use_the_parks_strategy(self):
        from unittest.mock import patch
        from lib.match_queue import STRATEGY_PARKS_MANAGER
        with patch("lib.match_queue.enqueue_tier3_matches", return_value=1) as enq:
            enqueue_review_pairs(self._engine(), self._queued())
        assert enq.call_args.kwargs["merge_strategy"] == STRATEGY_PARKS_MANAGER

    def test_pair_shape_matches_the_queue_contract(self):
        from unittest.mock import patch
        with patch("lib.match_queue.enqueue_tier3_matches", return_value=1) as enq:
            enqueue_review_pairs(self._engine(), self._queued())
        pair = enq.call_args.args[1][0]
        assert pair["source_a"] == "padus_parks"
        assert pair["key_a"] == "P1"
        assert pair["source_b"] == "tiger_places"
        assert pair["key_b"] == "3710740"
        assert pair["score"] == pytest.approx(0.80)

    def test_empty_input_short_circuits(self):
        engine = self._engine()
        assert enqueue_review_pairs(engine, pd.DataFrame()) == 0
        engine.begin.assert_not_called()

    def test_enqueue_failure_does_not_fail_the_run(self):
        """The rollup is already committed; losing a review hint must not abort."""
        from unittest.mock import patch
        with patch("lib.match_queue.enqueue_tier3_matches",
                   side_effect=RuntimeError("review schema missing")):
            assert enqueue_review_pairs(self._engine(), self._queued()) == 0
