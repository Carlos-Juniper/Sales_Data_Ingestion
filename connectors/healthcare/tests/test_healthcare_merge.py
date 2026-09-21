"""
Unit tests for the healthcare deduplication pipeline.

Covers both the new lib/match.py additions (jaro_winkler_similarity,
trigram_similarity, score_pair, blocking_keys) and every stage of
healthcare_merge.py.

All test data is in-memory only — no file I/O and no network calls.
Each test function verifies exactly one behaviour.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest
import pandas as pd

from lib.match import (
    blocking_keys,
    compound_name_similarity,
    jaro_winkler_similarity,
    score_pair,
    trigram_similarity,
)
from healthcare.healthcare_merge import (
    KNOWN_CHAINS,
    merge_all,
    prepare,
    survivorship,
    tier1_merge_ccn,
    tier1_merge_npi,
    tier2_merge,
    tier3_fuzzy_merge,
)


# ---------------------------------------------------------------------------
# Fixtures — minimal record dicts and DataFrames
# ---------------------------------------------------------------------------


def _rec(**overrides) -> dict:
    """Return a minimal valid record dict with caller-supplied overrides."""
    base = {
        "natural_key": "TEST001",
        "source_id": "cms_general",
        "name_raw": "General Hospital",
        "name_normalized": "GENERAL HOSPITAL",
        "address_line_1": "100 MAIN ST",
        "city": "Raleigh",
        "site_state": "NC",
        "zip5": "27601",
        "phone": "9195550100",
        "ccn": "",
        "npi": "",
        "size_metric": None,
        "latitude": None,
        "longitude": None,
    }
    base.update(overrides)
    return base


def _df(*records) -> pd.DataFrame:
    """Build a DataFrame from a sequence of _rec() dicts."""
    return pd.DataFrame(list(records))


# ---------------------------------------------------------------------------
# jaro_winkler_similarity
# ---------------------------------------------------------------------------


class TestJaroWinklerSimilarity:
    def test_identical_strings_return_one(self):
        assert jaro_winkler_similarity("CHAPEL HILL HOSPITAL", "CHAPEL HILL HOSPITAL") == pytest.approx(1.0)

    def test_empty_first_arg_returns_zero(self):
        assert jaro_winkler_similarity("", "CHAPEL HILL") == 0.0

    def test_empty_second_arg_returns_zero(self):
        assert jaro_winkler_similarity("CHAPEL HILL", "") == 0.0

    def test_both_empty_returns_zero(self):
        assert jaro_winkler_similarity("", "") == 0.0

    def test_clearly_different_strings_below_half(self):
        assert jaro_winkler_similarity("APPLE", "XYZQWERTY") < 0.5

    def test_near_identical_strings_above_threshold(self):
        # One transposed character — should still score high.
        score = jaro_winkler_similarity("GENERAL HOSPITAL", "GENERAL HOSPTIAL")
        assert score > 0.90


# ---------------------------------------------------------------------------
# trigram_similarity
# ---------------------------------------------------------------------------


class TestTrigramSimilarity:
    def test_identical_strings_return_one(self):
        assert trigram_similarity("HOSPITAL", "HOSPITAL") == pytest.approx(1.0)

    def test_empty_string_returns_zero(self):
        assert trigram_similarity("", "HOSPITAL") == 0.0

    def test_short_string_under_3_chars_returns_zero(self):
        assert trigram_similarity("AB", "ABCDEF") == 0.0

    def test_both_short_returns_zero(self):
        assert trigram_similarity("AB", "XY") == 0.0

    def test_completely_different_long_strings_return_low_score(self):
        assert trigram_similarity("ABCDEFGHIJ", "XYZUVWRSTU") < 0.15

    def test_partial_overlap_between_zero_and_one(self):
        score = trigram_similarity("HOSPITAL", "HOSPITALITY")
        assert 0.0 < score < 1.0


# ---------------------------------------------------------------------------
# score_pair — boundary cases
# ---------------------------------------------------------------------------


class TestScorePair:
    def _exact_pair(self) -> tuple[dict, dict]:
        a = _rec(
            name_normalized="SUNRISE MEDICAL CENTER",
            address_line_1="200 OAK AVE",
            zip5="27601",
            site_state="NC",
            phone="9195550101",
            latitude=35.7796,
            longitude=-78.6382,
            size_metric=120.0,
        )
        b = _rec(
            name_normalized="SUNRISE MEDICAL CENTER",
            address_line_1="200 OAK AVE",
            zip5="27601",
            site_state="NC",
            phone="9195550101",
            latitude=35.7796,
            longitude=-78.6382,
            size_metric=120.0,
        )
        return a, b

    def test_all_exact_match_scores_above_90(self):
        a, b = self._exact_pair()
        assert score_pair(a, b) > 0.90

    def test_all_different_scores_below_40(self):
        # Names, addresses, phones, coords, and sizes are all completely different.
        # The two facilities are on opposite coasts so spatial_prox == 0.
        # Even with non-zero name similarity the weighted score stays well below 0.40.
        a = _rec(
            name_normalized="SUNRISE MEDICAL CENTER",
            address_line_1="200 OAK AVE",
            zip5="27601",
            site_state="NC",
            phone="9195550101",
            latitude=35.7796,
            longitude=-78.6382,
            size_metric=120.0,
        )
        b = _rec(
            name_normalized="PACIFIC SHORE DIALYSIS",
            address_line_1="999 WAIKIKI BLVD",
            zip5="90210",
            site_state="CA",
            phone="3105550202",
            latitude=34.0522,
            longitude=-118.2437,
            size_metric=8.0,
        )
        assert score_pair(a, b) < 0.40

    @pytest.mark.parametrize(
        "dist_km, expected_prox",
        [
            (0.0, 1.0),
            (0.25, 0.5),
            (0.5, 0.0),
            (1.0, 0.0),
        ],
    )
    def test_spatial_proximity_decays_correctly(self, dist_km, expected_prox):
        # Fix all other fields equal so only spatial component varies.
        # lat delta of 1 degree ≈ 111 km, so we use fractional degrees.
        # Both name_normalized and name_raw are blanked: _feat_name uses name_raw
        # as a fallback when name_normalized is empty, so both must be absent to
        # isolate the spatial contribution.
        lat_a = 35.0
        lat_b = lat_a + dist_km / 111.0
        a = _rec(
            name_raw="",
            name_normalized="",
            address_line_1="",
            phone="",
            size_metric=None,
            latitude=lat_a,
            longitude=-78.0,
        )
        b = _rec(
            name_raw="",
            name_normalized="",
            address_line_1="",
            phone="",
            size_metric=None,
            latitude=lat_b,
            longitude=-78.0,
        )
        raw_score = score_pair(a, b)
        # Spatial is 20 % weight; extract the approximate spatial component.
        spatial_contribution = raw_score / 0.20
        assert spatial_contribution == pytest.approx(expected_prox, abs=0.05)

    def test_missing_lat_lon_gives_zero_spatial_contribution(self):
        a = _rec(name_normalized="X", address_line_1="", phone="", latitude=None, longitude=None)
        b = _rec(name_normalized="X", address_line_1="", phone="", latitude=None, longitude=None)
        # Without spatial, max possible score is 0.35 + 0.0 + 0.0 + 0.0 + 0.0 = 0.35
        # (name sim ~1.0) — so result must be < 0.40
        assert score_pair(a, b) < 0.40

    def test_phone_mismatch_does_not_add_phone_weight(self):
        a = _rec(name_normalized="HOSPITAL A", phone="9195550001")
        b = _rec(name_normalized="HOSPITAL A", phone="9195550002")
        score_with_mismatch = score_pair(a, b)
        b_match = _rec(name_normalized="HOSPITAL A", phone="9195550001")
        score_with_match = score_pair(a, b_match)
        # Phone weight = 10 %; matching phone must score at least 0.09 higher.
        assert score_with_match - score_with_mismatch > 0.08

    def test_size_within_20pct_adds_size_weight(self):
        a = _rec(name_normalized="CLINIC", size_metric=100.0)
        b_agree = _rec(name_normalized="CLINIC", size_metric=115.0)   # 15 % diff → agree
        b_disagree = _rec(name_normalized="CLINIC", size_metric=200.0) # 100 % diff → disagree
        assert score_pair(a, b_agree) > score_pair(a, b_disagree)


# ---------------------------------------------------------------------------
# blocking_keys
# ---------------------------------------------------------------------------


class TestBlockingKeys:
    def test_returns_list(self):
        rec = _rec()
        assert isinstance(blocking_keys(rec), list)

    def test_szn_key_always_present(self):
        rec = _rec(site_state="NC", zip5="27601", name_normalized="GENERAL HOSPITAL")
        keys = blocking_keys(rec)
        szn_keys = [k for k in keys if k.startswith("szn:")]
        assert len(szn_keys) == 1

    def test_phone_key_only_when_phone_non_empty(self):
        rec_with_phone = _rec(phone="9195550100", site_state="NC")
        rec_no_phone = _rec(phone="", site_state="NC")
        assert any(k.startswith("sph:") for k in blocking_keys(rec_with_phone))
        assert not any(k.startswith("sph:") for k in blocking_keys(rec_no_phone))

    def test_geo_key_only_when_lat_lon_present(self):
        rec_with_coords = _rec(latitude=35.77, longitude=-78.63)
        rec_no_coords = _rec(latitude=None, longitude=None)
        assert any(k.startswith("geo:") for k in blocking_keys(rec_with_coords))
        assert not any(k.startswith("geo:") for k in blocking_keys(rec_no_coords))

    def test_szn_key_uses_name_4char_prefix(self):
        rec = _rec(site_state="NC", zip5="27601", name_normalized="SUNRISE MEDICAL")
        szn_key = next(k for k in blocking_keys(rec) if k.startswith("szn:"))
        assert szn_key == "szn:NC:27601:SUNR"


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


class TestPrepare:
    def test_adds_cluster_id_column(self):
        df = _df(_rec(), _rec(natural_key="TEST002"))
        result = prepare(df)
        assert "cluster_id" in result.columns

    def test_cluster_id_initially_equals_src_content_key(self):
        # Cluster ids are now derived from source_id + natural_key rather than
        # row position so they survive any shuffle of the input DataFrame.
        df = _df(_rec(), _rec(natural_key="TEST002"))
        result = prepare(df)
        assert result.loc[0, "cluster_id"] == "src:cms_general:TEST001"
        assert result.loc[1, "cluster_id"] == "src:cms_general:TEST002"

    def test_normalises_name(self):
        df = _df(_rec(name_raw="sunrise medical center, inc."))
        result = prepare(df)
        # normalize_name strips corp suffixes and uppercases.
        assert result.loc[0, "name_normalized"] == "SUNRISE MEDICAL CENTER"

    def test_normalises_phone(self):
        df = _df(_rec(phone="(919) 555-0100"))
        result = prepare(df)
        assert result.loc[0, "phone"] == "9195550100"

    def test_strips_and_uppercases_state(self):
        df = _df(_rec(site_state="  nc  "))
        result = prepare(df)
        assert result.loc[0, "site_state"] == "NC"

    def test_normalises_zip(self):
        df = _df(_rec(zip5="27601-4321"))
        result = prepare(df)
        assert result.loc[0, "zip5"] == "27601"


# ---------------------------------------------------------------------------
# tier1_merge_ccn
# ---------------------------------------------------------------------------


class TestTier1MergeCCN:
    def test_two_rows_same_ccn_get_same_cluster_id(self):
        df = prepare(_df(
            _rec(natural_key="A", ccn="123456"),
            _rec(natural_key="B", ccn="123456"),
        ))
        result = tier1_merge_ccn(df)
        assert result.loc[0, "cluster_id"] == result.loc[1, "cluster_id"]
        assert result.loc[0, "cluster_id"] == "ccn:123456"

    def test_empty_ccn_rows_are_untouched(self):
        df = prepare(_df(
            _rec(natural_key="A", ccn=""),
            _rec(natural_key="B", ccn=""),
        ))
        result = tier1_merge_ccn(df)
        # cluster_ids remain as singleton src: keys (no CCN to merge on).
        assert result.loc[0, "cluster_id"] == "src:cms_general:A"
        assert result.loc[1, "cluster_id"] == "src:cms_general:B"

    def test_different_ccns_get_different_cluster_ids(self):
        df = prepare(_df(
            _rec(natural_key="A", ccn="111111"),
            _rec(natural_key="B", ccn="222222"),
        ))
        result = tier1_merge_ccn(df)
        assert result.loc[0, "cluster_id"] != result.loc[1, "cluster_id"]


# ---------------------------------------------------------------------------
# tier1_merge_npi
# ---------------------------------------------------------------------------


class TestTier1MergeNPI:
    def test_two_rows_same_npi_get_same_cluster_id(self):
        df = prepare(_df(
            _rec(natural_key="A", npi="1234567890"),
            _rec(natural_key="B", npi="1234567890"),
        ))
        df = tier1_merge_ccn(df)
        result = tier1_merge_npi(df)
        assert result.loc[0, "cluster_id"] == result.loc[1, "cluster_id"]
        assert result.loc[0, "cluster_id"] == "npi:1234567890"

    def test_ccn_cluster_takes_priority_over_npi(self):
        # Row 0 has both CCN and NPI; CCN cluster must survive.
        df = prepare(_df(
            _rec(natural_key="A", ccn="999999", npi="1111111111"),
            _rec(natural_key="B", ccn="",       npi="1111111111"),
        ))
        df = tier1_merge_ccn(df)
        result = tier1_merge_npi(df)
        # Row 0 keeps its CCN cluster; row 1 gets the NPI cluster.
        assert result.loc[0, "cluster_id"] == "ccn:999999"
        assert result.loc[1, "cluster_id"] == "npi:1111111111"

    def test_empty_npi_rows_untouched(self):
        df = prepare(_df(
            _rec(natural_key="A", npi=""),
            _rec(natural_key="B", npi=""),
        ))
        df = tier1_merge_ccn(df)
        result = tier1_merge_npi(df)
        # cluster_ids remain as singleton src: keys (no NPI or CCN to merge on).
        assert result.loc[0, "cluster_id"] == "src:cms_general:A"
        assert result.loc[1, "cluster_id"] == "src:cms_general:B"


# ---------------------------------------------------------------------------
# tier2_merge
# ---------------------------------------------------------------------------


class TestTier2Merge:
    def test_same_name_zip_state_gets_same_cluster(self):
        df = prepare(_df(
            _rec(natural_key="A", name_raw="sunrise medical", zip5="27601", site_state="NC"),
            _rec(natural_key="B", name_raw="sunrise medical", zip5="27601", site_state="NC"),
        ))
        df = tier1_merge_ccn(df)
        df = tier1_merge_npi(df)
        result = tier2_merge(df)
        assert result.loc[0, "cluster_id"] == result.loc[1, "cluster_id"]

    def test_different_zip_stays_distinct(self):
        df = prepare(_df(
            _rec(natural_key="A", name_raw="sunrise medical", zip5="27601", site_state="NC"),
            _rec(natural_key="B", name_raw="sunrise medical", zip5="90210", site_state="CA"),
        ))
        df = tier1_merge_ccn(df)
        df = tier1_merge_npi(df)
        result = tier2_merge(df)
        assert result.loc[0, "cluster_id"] != result.loc[1, "cluster_id"]

    def test_known_chain_brand_excluded_from_tier2(self):
        chain_name = next(iter(KNOWN_CHAINS))
        df = prepare(_df(
            _rec(natural_key="A", name_raw=chain_name, zip5="27601", site_state="NC"),
            _rec(natural_key="B", name_raw=chain_name, zip5="27601", site_state="NC"),
        ))
        df = tier1_merge_ccn(df)
        df = tier1_merge_npi(df)
        result = tier2_merge(df)
        # Both records are chain brands — they must not be merged by Tier 2.
        assert result.loc[0, "cluster_id"] != result.loc[1, "cluster_id"]


# ---------------------------------------------------------------------------
# tier3_fuzzy_merge
# ---------------------------------------------------------------------------


class TestTier3FuzzyMerge:
    def _run_tier3(self, records, auto=0.92, queue=0.75):
        df = prepare(_df(*records))
        df = tier1_merge_ccn(df)
        df = tier1_merge_npi(df)
        df = tier2_merge(df)
        return tier3_fuzzy_merge(df, auto_threshold=auto, queue_threshold=queue)

    def test_high_similarity_pair_auto_merged(self):
        a = _rec(
            natural_key="A",
            name_raw="sunrise medical center",
            address_line_1="200 Oak Ave",
            zip5="27601",
            site_state="NC",
            phone="9195550101",
            latitude=35.7796,
            longitude=-78.6382,
        )
        # Near-identical clone — should cross 0.92 threshold.
        b = _rec(
            natural_key="B",
            name_raw="sunrise medical ctr",
            address_line_1="200 Oak Ave",
            zip5="27601",
            site_state="NC",
            phone="9195550101",
            latitude=35.7796,
            longitude=-78.6382,
        )
        result_df, review = self._run_tier3([a, b], auto=0.80, queue=0.60)
        cluster_a = result_df.loc[0, "cluster_id"]
        cluster_b = result_df.loc[1, "cluster_id"]
        assert cluster_a == cluster_b

    def test_mid_range_pair_goes_to_review_queue(self):
        a = _rec(
            natural_key="A",
            name_raw="sunrise medical center",
            zip5="27601",
            site_state="NC",
            phone="",
            latitude=35.7796,
            longitude=-78.6382,
        )
        # Similar name, same block keys, but different address/phone — borderline.
        b = _rec(
            natural_key="B",
            name_raw="sunrise health centre",
            zip5="27601",
            site_state="NC",
            phone="",
            latitude=35.7800,
            longitude=-78.6382,
        )
        # Set thresholds so this pair falls in the queue band [0.40, 0.99].
        result_df, review = self._run_tier3([a, b], auto=0.99, queue=0.40)
        assert len(review) >= 1

    def test_low_similarity_pair_left_distinct(self):
        a = _rec(natural_key="A", name_raw="sunrise medical", zip5="27601", site_state="NC")
        b = _rec(natural_key="B", name_raw="oceanview urgent care", zip5="27601", site_state="NC")
        result_df, review = self._run_tier3([a, b], auto=0.92, queue=0.75)
        # cluster_ids remain different (both still their own index strings).
        assert result_df.loc[0, "cluster_id"] != result_df.loc[1, "cluster_id"]

    def test_review_queue_has_expected_columns(self):
        a = _rec(natural_key="A", name_raw="sunrise medical", zip5="27601", site_state="NC",
                 latitude=35.77, longitude=-78.63)
        b = _rec(natural_key="B", name_raw="sunrise medcal",  zip5="27601", site_state="NC",
                 latitude=35.77, longitude=-78.63)
        _, review = self._run_tier3([a, b], auto=0.99, queue=0.01)
        for col in ["key_a", "key_b", "score", "source_a", "source_b"]:
            assert col in review.columns


# ---------------------------------------------------------------------------
# survivorship
# ---------------------------------------------------------------------------


class TestSurvivorship:
    def test_nc_dhsr_phone_wins_over_cms(self):
        df = prepare(_df(
            _rec(natural_key="CMS001", source_id="cms_general",  phone="9195550001", ccn="CCN1"),
            _rec(natural_key="NC001",  source_id="nc_dhsr",      phone="9195550099", ccn="CCN1"),
        ))
        df = tier1_merge_ccn(df)
        result = survivorship(df)
        assert len(result) == 1
        # nc_dhsr outranks cms_general in _SOURCE_PRIORITY.
        assert result.loc[0, "phone"] == "9195550099"

    def test_merged_source_ids_contains_both_keys(self):
        df = prepare(_df(
            _rec(natural_key="CMS001", source_id="cms_general", ccn="CCN1"),
            _rec(natural_key="NC001",  source_id="nc_dhsr",     ccn="CCN1"),
        ))
        df = tier1_merge_ccn(df)
        result = survivorship(df)
        merged_ids = result.loc[0, "merged_source_ids"]
        assert "CMS001" in merged_ids
        assert "NC001" in merged_ids

    def test_one_survivor_per_cluster(self):
        df = prepare(_df(
            _rec(natural_key="A", ccn="CCN1"),
            _rec(natural_key="B", ccn="CCN1"),
            _rec(natural_key="C", ccn="CCN1"),
        ))
        df = tier1_merge_ccn(df)
        result = survivorship(df)
        assert len(result) == 1

    def test_lat_lon_chosen_from_non_null_source(self):
        df = prepare(_df(
            _rec(natural_key="A", source_id="cms_general", ccn="CCN1", latitude=None, longitude=None),
            _rec(natural_key="B", source_id="va",          ccn="CCN1", latitude=35.77, longitude=-78.63),
        ))
        df = tier1_merge_ccn(df)
        result = survivorship(df)
        assert result.loc[0, "latitude"] == pytest.approx(35.77)


# ---------------------------------------------------------------------------
# merge_all — end-to-end
# ---------------------------------------------------------------------------


class TestMergeAll:
    def test_end_to_end_four_records_produce_three_clusters(self):
        """
        Input:
          Row 0 + 1: share CCN → 1 cluster
          Row 2 + 3: share NPI → 1 cluster
          Row 4:     unrelated standalone → 1 cluster
        Expected: 3 distinct clusters in canonical output.
        """
        records = [
            _rec(natural_key="CMS_A",  source_id="cms_general",     ccn="CCN111", npi=""),
            _rec(natural_key="NC_A",   source_id="nc_dhsr",          ccn="CCN111", npi=""),
            _rec(natural_key="NPI_A",  source_id="nppes_pl",         ccn="",       npi="NPI999"),
            _rec(natural_key="NPI_B",  source_id="cms_nursing_home", ccn="",       npi="NPI999"),
            _rec(natural_key="SOLO",   source_id="cms_general",      ccn="",       npi=""),
        ]
        df = _df(*records)
        canonical, review_queue = merge_all(df)

        assert len(canonical) == 3

    def test_ccn_cluster_present_in_output(self):
        records = [
            _rec(natural_key="CMS_A", source_id="cms_general",  ccn="CCN111"),
            _rec(natural_key="NC_A",  source_id="nc_dhsr",       ccn="CCN111"),
            _rec(natural_key="SOLO",  source_id="cms_general",   ccn=""),
        ]
        df = _df(*records)
        canonical, _ = merge_all(df)
        # The CCN cluster row must contain both natural_keys in merged_source_ids.
        ccn_row = canonical[canonical["merged_source_ids"].str.contains("CMS_A")]
        assert len(ccn_row) == 1
        assert "NC_A" in ccn_row.iloc[0]["merged_source_ids"]

    def test_review_queue_is_dataframe(self):
        df = _df(_rec(natural_key="A"), _rec(natural_key="B"))
        _, review = merge_all(df)
        assert isinstance(review, pd.DataFrame)

    def test_standalone_record_survives_unchanged(self):
        df = _df(_rec(natural_key="SOLO", source_id="cms_general", ccn="", npi=""))
        canonical, _ = merge_all(df)
        assert len(canonical) == 1
        assert "SOLO" in canonical.loc[0, "merged_source_ids"]


# ---------------------------------------------------------------------------
# Cluster-id determinism: shuffle invariance
# ---------------------------------------------------------------------------


class TestClusterIdDeterminism:
    """
    Gates the idempotency contract: cluster_id must be identical for a given
    natural_key regardless of the order rows arrive in the input DataFrame.

    Each test builds a reference DataFrame, runs the full pipeline, then
    re-runs with a fixed permutation of the same rows and asserts that the
    mapping natural_key → cluster_id is byte-identical across both orderings.

    Covers Tier 2 (exact composite), Tier 3 (fuzzy union-find), and singletons.
    """

    # Fixed permutation used in every shuffle subtest.  Using an explicit list
    # rather than random to make failures reproducible without a seed.
    _PERM_5 = [3, 1, 4, 0, 2]  # permutation for 5-row DataFrames
    _PERM_6 = [5, 2, 0, 4, 1, 3]  # permutation for 6-row DataFrames

    @staticmethod
    def _cluster_map(df: pd.DataFrame) -> dict[str, str]:
        """Return {natural_key: cluster_id} for every row (post-prepare)."""
        return dict(zip(df["natural_key"].tolist(), df["cluster_id"].tolist()))

    def test_singleton_cluster_ids_invariant_under_shuffle(self):
        """
        Records that match no peer must always receive cluster_id
        'src:{source_id}:{natural_key}', independent of their row index.
        """
        records = [
            _rec(natural_key="S1", source_id="cms_general",     ccn="", npi=""),
            _rec(natural_key="S2", source_id="cms_nursing_home", ccn="", npi=""),
            _rec(natural_key="S3", source_id="nppes_pl",         ccn="", npi=""),
            _rec(natural_key="S4", source_id="va",               ccn="", npi=""),
            _rec(natural_key="S5", source_id="cms_general",      ccn="", npi=""),
        ]
        df_orig = _df(*records)
        df_shuf = df_orig.iloc[list(TestClusterIdDeterminism._PERM_5)].reset_index(drop=True)

        # Run only through prepare() — Tier 1/2/3 won't change pure singletons.
        orig_map = TestClusterIdDeterminism._cluster_map(prepare(df_orig))
        shuf_map = TestClusterIdDeterminism._cluster_map(prepare(df_shuf))

        assert orig_map == shuf_map, (
            f"Singleton cluster_ids changed under shuffle.\n"
            f"Original: {orig_map}\nShuffled: {shuf_map}"
        )
        # Spot-check the expected format.
        assert orig_map["S1"] == "src:cms_general:S1"
        assert orig_map["S3"] == "src:nppes_pl:S3"

    def test_tier2_cluster_ids_invariant_under_shuffle(self):
        """
        Two records sharing (name_normalized, zip5, site_state) must land in the
        same Tier-2 cluster regardless of which row comes first in the DataFrame.

        The cluster_id is derived from the join key itself ('t2:NAME|ZIP|STATE'),
        not from any member's row position.
        """
        # Rows 0 and 1 share all three key columns → should cluster together.
        # Rows 2 and 3 are distinct singletons (different zip).
        # Row 4 is a named singleton.
        records = [
            _rec(natural_key="T2A", source_id="cms_general",
                 name_raw="sunrise medical center", zip5="27601", site_state="NC",
                 ccn="", npi=""),
            _rec(natural_key="T2B", source_id="nppes_pl",
                 name_raw="sunrise medical center", zip5="27601", site_state="NC",
                 ccn="", npi=""),
            _rec(natural_key="T2C", source_id="cms_general",
                 name_raw="sunrise medical center", zip5="90210", site_state="CA",
                 ccn="", npi=""),
            _rec(natural_key="T2D", source_id="cms_general",
                 name_raw="pacific dialysis", zip5="27601", site_state="NC",
                 ccn="", npi=""),
            _rec(natural_key="T2E", source_id="cms_general",
                 name_raw="standalone clinic", zip5="10001", site_state="NY",
                 ccn="", npi=""),
        ]
        df_orig = _df(*records)
        df_shuf = df_orig.iloc[list(TestClusterIdDeterminism._PERM_5)].reset_index(drop=True)

        def _run(df: pd.DataFrame) -> dict[str, str]:
            df = prepare(df)
            df = tier1_merge_ccn(df)
            df = tier1_merge_npi(df)
            df = tier2_merge(df)
            return TestClusterIdDeterminism._cluster_map(df)

        orig_map = _run(df_orig)
        shuf_map = _run(df_shuf)

        assert orig_map == shuf_map, (
            f"Tier-2 cluster_ids changed under shuffle.\n"
            f"Original: {orig_map}\nShuffled: {shuf_map}"
        )
        # T2A and T2B must share the same deterministic key.
        assert orig_map["T2A"] == orig_map["T2B"]
        # That key must be derived from the join-key columns, not a row index.
        assert orig_map["T2A"].startswith("t2:")
        # T2C has a different zip — must be in a distinct cluster.
        assert orig_map["T2C"] != orig_map["T2A"]

    def test_tier3_cluster_ids_invariant_under_shuffle(self):
        """
        Records that auto-merge via fuzzy scoring must land in the same cluster
        regardless of input row order, and that cluster_id must be
        't3:{min(natural_keys)}' — the lexicographic minimum over the cluster.

        Six records: one fuzzy-merge pair (A+B) and four singletons (C..F).
        Thresholds are forced low (0.01) so a deliberately similar pair crosses.
        """
        shared_kwargs = dict(
            zip5="27601",
            site_state="NC",
            phone="9195550101",
            latitude=35.7796,
            longitude=-78.6382,
            ccn="",
            npi="",
        )
        records = [
            # Natural key "NK_A" < "NK_B" → cluster should be "t3:NK_A".
            _rec(natural_key="NK_A", source_id="cms_general",
                 name_raw="sunrise medical center", **shared_kwargs),
            _rec(natural_key="NK_B", source_id="nppes_pl",
                 name_raw="sunrise medical ctr", **shared_kwargs),
            # Singletons — distinct names, zips, states, and coords so they
            # are separated by both blocking keys and fuzzy score; each
            # receives its own src: cluster in the output.
            _rec(natural_key="NK_C", source_id="cms_general",
                 name_raw="oceanview urgent care",
                 zip5="11111", site_state="FL", phone="", ccn="", npi="",
                 latitude=25.0, longitude=-80.0),
            _rec(natural_key="NK_D", source_id="cms_general",
                 name_raw="downtown dialysis clinic",
                 zip5="22222", site_state="TX", phone="", ccn="", npi="",
                 latitude=30.0, longitude=-97.0),
            _rec(natural_key="NK_E", source_id="cms_nursing_home",
                 name_raw="lakeview health plaza",
                 zip5="33333", site_state="GA", phone="", ccn="", npi="",
                 latitude=33.0, longitude=-84.0),
            _rec(natural_key="NK_F", source_id="cms_general",
                 name_raw="metro surgery center",
                 zip5="44444", site_state="OH", phone="", ccn="", npi="",
                 latitude=41.0, longitude=-83.0),
        ]
        df_orig = _df(*records)
        df_shuf = df_orig.iloc[list(TestClusterIdDeterminism._PERM_6)].reset_index(drop=True)

        def _run(df: pd.DataFrame) -> dict[str, str]:
            df = prepare(df)
            df = tier1_merge_ccn(df)
            df = tier1_merge_npi(df)
            df = tier2_merge(df)
            # Low thresholds so the similar pair auto-merges.
            df, _ = tier3_fuzzy_merge(df, auto_threshold=0.01, queue_threshold=0.0)
            return TestClusterIdDeterminism._cluster_map(df)

        orig_map = _run(df_orig)
        shuf_map = _run(df_shuf)

        assert orig_map == shuf_map, (
            f"Tier-3 cluster_ids changed under shuffle.\n"
            f"Original: {orig_map}\nShuffled: {shuf_map}"
        )
        # Both fuzzy-merged records must share the same t3: cluster.
        assert orig_map["NK_A"] == orig_map["NK_B"]
        # The cluster id must be the lex-min natural_key ("NK_A" < "NK_B").
        assert orig_map["NK_A"] == "t3:NK_A"
        # Singletons must not have been pulled into the t3 cluster.
        for nk in ("NK_C", "NK_D", "NK_E", "NK_F"):
            assert orig_map[nk] != orig_map["NK_A"]


# ---------------------------------------------------------------------------
# TestSizeMetricNaNScrub — healthcare vertical
# ---------------------------------------------------------------------------


class TestSizeMetricNaNScrub:
    """
    Verify that upsert_resolved_account (healthcare_pipeline) never sends
    float NaN as size_metric to Postgres — it must arrive as Python None.

    Same root cause as the deathcare bug: pd.DataFrame(rows) infers float64
    when the row list mixes None and real floats, silently upcasting None to
    NaN.  Postgres numeric accepts NaN, so without the scrub it lands as
    literal NaN in core.account instead of SQL NULL.

    Healthcare's size_metric is currently always None from every wired source
    (dormant, not live), but the fix is defensive — it closes the class of bug
    off before any source starts populating size_value.
    """

    def _make_merged_df(self) -> pd.DataFrame:
        """
        Build a merged/survivorship-output DataFrame with a mix of rows:
        some have a real size_metric, some have None.
        """
        records = [
            _rec(natural_key="HC001", size_metric=200.0, source_id="cms_general"),
            _rec(natural_key="HC002", size_metric=None,  source_id="cms_general"),
            _rec(natural_key="HC003", size_metric=None,  source_id="nppes_practice_locations"),
        ]
        df = pd.DataFrame(records)
        # Simulate what survivorship produces: float64 column with NaN for None rows.
        df["size_metric"] = pd.to_numeric(df["size_metric"], errors="coerce")
        # Add required columns that build_resolved_account / survivorship expect.
        df["size_value"] = df["size_metric"]
        df["size_unit"] = None
        df["cluster_id"] = "src:" + df["source_id"] + ":" + df["natural_key"]
        df["name_raw"] = "Test Facility"
        df["name_normalized"] = "TEST FACILITY"
        df["address_line_1"] = "100 Main St"
        df["city"] = "Raleigh"
        df["site_state"] = "NC"
        df["state"] = "NC"
        df["zip5"] = "27601"
        df["phone"] = ""
        df["vertical"] = "healthcare"
        df["account_type"] = "hospital"
        df["ccn"] = ""
        df["npi"] = ""
        df["ein"] = None
        df["latitude"] = None
        df["longitude"] = None
        return df

    def test_build_resolved_account_nan_precondition(self):
        """
        build_resolved_account must produce NaN (not None) in size_metric for
        rows whose size_value was None — confirming the precondition that the
        DataFrame-level scrub is insufficient and only the post-to_dict scrub works.
        """
        from healthcare.healthcare_pipeline import build_resolved_account

        merged = self._make_merged_df()
        account_df = build_resolved_account(merged)

        real_row = account_df[account_df["_cluster_id"].str.contains("HC001")].iloc[0]
        assert real_row["size_metric"] == pytest.approx(200.0)

        none_row = account_df[account_df["_cluster_id"].str.contains("HC002")].iloc[0]
        assert pd.isna(none_row["size_metric"]), (
            "Expected float NaN in DataFrame for rows with no size data "
            "(precondition for the NaN→NULL scrub requirement)"
        )

    def test_upsert_resolved_account_sends_none_not_nan_to_db(self):
        """
        upsert_resolved_account must scrub NaN → Python None in the params
        dict before passing to engine.execute, so Postgres receives NULL.

        The DB execute is mocked; we capture the params dict and assert
        size_metric is None (not float NaN) for the affected rows.
        """
        from unittest.mock import MagicMock
        from healthcare.healthcare_pipeline import build_resolved_account, upsert_resolved_account

        merged = self._make_merged_df()
        account_df = build_resolved_account(merged)

        captured_rows: list[list[dict]] = []

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.side_effect = lambda sql, rows: captured_rows.append(rows)

        mock_engine = MagicMock()
        mock_engine.begin.return_value = mock_conn

        upsert_resolved_account(mock_engine, account_df)

        assert captured_rows, "engine.execute was never called"
        rows_sent = captured_rows[0]

        # The row with a real size_metric must keep the float.
        real_params = next(
            r for r in rows_sent
            if r.get("size_metric") is not None
        )
        assert real_params["size_metric"] == pytest.approx(200.0)

        # All rows that had None size_value must carry Python None (not NaN).
        # `is None` distinguishes Python None from float NaN (both truthy under pd.isna).
        none_params = [r for r in rows_sent if r.get("size_metric") is None]
        assert len(none_params) == 2, (
            f"Expected 2 rows with size_metric=None, got {len(none_params)}. "
            f"size_metric values: {[r.get('size_metric') for r in rows_sent]}"
        )
        for p in none_params:
            assert p["size_metric"] is None, (
                f"Expected Python None, got {p['size_metric']!r} "
                f"(type={type(p['size_metric']).__name__})"
            )
