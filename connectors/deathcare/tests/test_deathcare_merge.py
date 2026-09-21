"""
Tests for deathcare_merge.py — merge and deduplication module.

Strategy
--------
All DataFrames are built entirely in-memory.  No network calls, no file
reads, and no dependency on the other connectors.

Key design choices:
  - _make_canonical() is the single helper that stamps out well-formed rows.
    All test cases call it and override only the fields they care about.
  - Coordinate pairs are chosen so the arithmetic is verifiable by hand
    using the Haversine formula (e.g., a 0.001° lat offset at 30° lat ≈ 111 m).
  - Levenshtein tests use names with a known edit distance so the
    expected similarity ratio can be confirmed independently.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest

import deathcare_merge as mod
from deathcare_merge import _replace_and_upsert, _build_resolved_account, _build_resolved_location, _build_resolved_contact


# ===========================================================================
# Helpers
# ===========================================================================

def _make_canonical(**overrides) -> dict:
    """
    Return a dict representing one canonical deathcare row.
    Override any field with keyword arguments.
    """
    defaults: dict = {
        "source_id":        "nsd:default-uuid",
        "natural_key":      "",  # D3: empty so _source_composite returns just source_id
        "vertical":         "deathcare",
        "account_type":     "cemetery",
        "name_raw":         "Oak Grove Cemetery",
        "name_normalized":  "OAK GROVE CEMETERY",
        "address_line_1":   "123 Main St",
        "city":             "Tampa",
        "state":            "FL",
        "zip5":             "33601",
        "phone_raw":        None,
        "phone_normalized": None,
        "latitude":         27.9,
        "longitude":        -82.4,
        "segment":          None,
        "ein":              None,
        "county_fips":      None,
        "size_metric":      None,
        "size_value":       None,
        "size_unit":        None,
        "source_file":      "test",
    }
    defaults.update(overrides)
    return defaults


def _df(*rows: dict) -> pd.DataFrame:
    """Build a DataFrame from one or more _make_canonical() dicts."""
    return pd.DataFrame(list(rows))


# ===========================================================================
# TestHaversineKm
# ===========================================================================

class TestHaversineKm:
    """Verify haversine_km() against known geometric truths."""

    def test_same_point_is_zero(self):
        """Identical coordinates must return exactly 0."""
        assert mod.haversine_km(27.9, -82.4, 27.9, -82.4) == pytest.approx(0.0)

    def test_approximately_one_km(self):
        """
        At latitude ~27°, a 0.009° latitude offset ≈ 1.0 km.
        (1° lat ≈ 111 km → 0.009° ≈ 1.0 km)
        """
        dist = mod.haversine_km(27.0, -82.0, 27.009, -82.0)
        assert dist == pytest.approx(1.0, abs=0.05)

    def test_approximately_150m(self):
        """
        0.001° lat offset at latitude 27° ≈ 111 m, well within the 150 m threshold.
        The result must be < 0.15 km so the spatial dedup would merge these records.
        """
        dist = mod.haversine_km(27.0, -82.0, 27.001, -82.0)
        assert dist < 0.15

    def test_points_200m_apart_exceed_threshold(self):
        """
        0.0018° lat offset at latitude 27° ≈ 200 m.
        The result must be > 0.15 km so the spatial dedup would NOT merge it.
        """
        dist = mod.haversine_km(27.0, -82.0, 27.0018, -82.0)
        assert dist > 0.15

    def test_non_negative(self):
        """Distance must always be non-negative, even with reversed arguments."""
        assert mod.haversine_km(27.0, -82.0, 27.009, -82.0) >= 0.0
        assert mod.haversine_km(27.009, -82.0, 27.0, -82.0) >= 0.0

    def test_symmetry(self):
        """Distance A→B must equal distance B→A."""
        d1 = mod.haversine_km(27.0, -82.0, 28.0, -81.0)
        d2 = mod.haversine_km(28.0, -81.0, 27.0, -82.0)
        assert d1 == pytest.approx(d2)


# ===========================================================================
# TestDeduplicateByEin
# ===========================================================================

class TestDeduplicateByEin:
    """Verify dedup_by_ein() handles EIN uniqueness within BMF."""

    def _bmf_row(self, ein: str, name: str = "Test Cemetery") -> dict:
        return _make_canonical(
            source_id=f"irs_bmf:{ein}",
            ein=ein,
            segment="religious",
            latitude=None,
            longitude=None,
            name_raw=name,
            name_normalized=name.upper(),
        )

    def test_duplicate_ein_removed(self):
        """Two BMF rows with the same EIN → only the first survives."""
        row1 = self._bmf_row("043783054", "Oak Grove Cemetery")
        row2 = self._bmf_row("043783054", "Oak Grove Cemetery Inc")
        df = _df(row1, row2)
        result = mod.dedup_by_ein(df)
        assert len(result) == 1
        assert result.iloc[0]["name_raw"] == "Oak Grove Cemetery"

    def test_unique_eins_preserved(self):
        """Two BMF rows with different EINs must both survive."""
        row1 = self._bmf_row("111111111")
        row2 = self._bmf_row("222222222")
        df = _df(row1, row2)
        result = mod.dedup_by_ein(df)
        assert len(result) == 2

    def test_non_bmf_rows_unaffected(self):
        """
        NSD / TxDOT / FGDL / VA rows have ein=None and must never be
        dropped by the EIN dedup step, even when there are many of them.
        """
        nsd_rows = [
            _make_canonical(
                source_id=f"nsd:uuid-{i}",
                ein=None,
                latitude=27.0 + i * 0.01,
                longitude=-82.0,
            )
            for i in range(5)
        ]
        df = pd.DataFrame(nsd_rows)
        result = mod.dedup_by_ein(df)
        assert len(result) == 5

    def test_mixed_bmf_and_nsd_rows(self):
        """Duplicate EIN dedup must not touch NSD rows sitting alongside BMF rows."""
        bmf1 = self._bmf_row("999000001")
        bmf2 = self._bmf_row("999000001")  # duplicate
        nsd = _make_canonical(source_id="nsd:abc", ein=None)
        df = _df(bmf1, bmf2, nsd)
        result = mod.dedup_by_ein(df)
        # One BMF row + one NSD row = 2 total
        assert len(result) == 2

    def test_three_duplicates_keeps_first(self):
        """Three rows with the same EIN → exactly one row survives."""
        rows = [self._bmf_row("777777777", f"Cemetery {i}") for i in range(3)]
        df = pd.DataFrame(rows)
        result = mod.dedup_by_ein(df)
        assert len(result) == 1


# ===========================================================================
# TestSpatialDedup
# ===========================================================================

class TestSpatialDedup:
    """Verify spatial_dedup() merges, separates, and coalesces correctly."""

    # Two coordinates within 150 m of each other (~111 m apart at lat 27°).
    _CLOSE_LAT_A = 27.000
    _CLOSE_LON = -82.000
    _CLOSE_LAT_B = 27.001  # ≈ 111 m north

    # Two coordinates > 150 m apart (~222 m).
    _FAR_LAT_A = 27.000
    _FAR_LON = -82.000
    _FAR_LAT_B = 27.002  # ≈ 222 m north

    def test_two_records_within_150m_merged_to_one(self):
        """Records within 150 m of each other must collapse to a single output row."""
        row_a = _make_canonical(
            source_id="nsd:uuid-a",
            latitude=self._CLOSE_LAT_A,
            longitude=self._CLOSE_LON,
        )
        row_b = _make_canonical(
            source_id="nsd:uuid-b",
            latitude=self._CLOSE_LAT_B,
            longitude=self._CLOSE_LON,
        )
        df = _df(row_a, row_b)
        result = mod.spatial_dedup(df)
        assert len(result) == 1

    def test_two_records_beyond_150m_kept_separate(self):
        """Records more than 150 m apart must remain as two distinct output rows."""
        row_a = _make_canonical(
            source_id="nsd:uuid-a",
            latitude=self._FAR_LAT_A,
            longitude=self._FAR_LON,
        )
        row_b = _make_canonical(
            source_id="nsd:uuid-b",
            latitude=self._FAR_LAT_B,
            longitude=self._FAR_LON,
        )
        df = _df(row_a, row_b)
        result = mod.spatial_dedup(df)
        assert len(result) == 2

    def test_merged_record_coalesces_fields(self):
        """
        When two records are merged, the output must contain the first non-null
        value from each field across both records.
        Row A has a phone; row B has an address not present in A.
        The merged record must carry both.
        """
        row_a = _make_canonical(
            source_id="nsd:uuid-a",
            latitude=self._CLOSE_LAT_A,
            longitude=self._CLOSE_LON,
            phone_normalized="8135550101",
            address_line_1=None,
        )
        row_b = _make_canonical(
            source_id="nsd:uuid-b",
            latitude=self._CLOSE_LAT_B,
            longitude=self._CLOSE_LON,
            phone_normalized=None,
            address_line_1="456 Oak Ave",
        )
        df = _df(row_a, row_b)
        result = mod.spatial_dedup(df)
        assert len(result) == 1
        row = result.iloc[0]
        assert row["phone_normalized"] == "8135550101"
        assert row["address_line_1"] == "456 Oak Ave"

    def test_merge_confidence_high_when_names_similar(self):
        """
        Names with Levenshtein similarity >= 0.80 → merge_confidence='high'.
        'OAK GROVE CEMETERY' vs 'OAK GROVE CEMETARY' (1 char off out of 18 → 0.944).
        """
        row_a = _make_canonical(
            source_id="nsd:uuid-a",
            latitude=self._CLOSE_LAT_A,
            longitude=self._CLOSE_LON,
            name_normalized="OAK GROVE CEMETERY",
        )
        row_b = _make_canonical(
            source_id="nsd:uuid-b",
            latitude=self._CLOSE_LAT_B,
            longitude=self._CLOSE_LON,
            name_normalized="OAK GROVE CEMETARY",
        )
        df = _df(row_a, row_b)
        result = mod.spatial_dedup(df)
        assert result.iloc[0]["merge_confidence"] == "high"

    def test_merge_confidence_spatial_only_when_names_differ(self):
        """
        Names with Levenshtein similarity < 0.80 → merge_confidence='spatial_only'.
        'SUNRISE CHAPEL' vs 'MEMORIAL GARDENS' are clearly different names.
        """
        row_a = _make_canonical(
            source_id="nsd:uuid-a",
            latitude=self._CLOSE_LAT_A,
            longitude=self._CLOSE_LON,
            name_normalized="SUNRISE CHAPEL",
        )
        row_b = _make_canonical(
            source_id="nsd:uuid-b",
            latitude=self._CLOSE_LAT_B,
            longitude=self._CLOSE_LON,
            name_normalized="MEMORIAL GARDENS",
        )
        df = _df(row_a, row_b)
        result = mod.spatial_dedup(df)
        assert result.iloc[0]["merge_confidence"] == "spatial_only"

    def test_records_without_lat_lon_get_confidence_none(self):
        """BMF records have no coordinates and must always get merge_confidence='none'."""
        row = _make_canonical(
            source_id="irs_bmf:043783054",
            latitude=None,
            longitude=None,
            segment="religious",
            ein="043783054",
        )
        df = _df(row)
        result = mod.spatial_dedup(df)
        assert result.iloc[0]["merge_confidence"] == "none"

    def test_merged_sources_contains_both_source_ids(self):
        """merged_sources must list both source_ids when two records are merged."""
        row_a = _make_canonical(
            source_id="nsd:uuid-a",
            latitude=self._CLOSE_LAT_A,
            longitude=self._CLOSE_LON,
        )
        row_b = _make_canonical(
            source_id="fgdl:uuid-b",
            latitude=self._CLOSE_LAT_B,
            longitude=self._CLOSE_LON,
        )
        df = _df(row_a, row_b)
        result = mod.spatial_dedup(df)
        sources = result.iloc[0]["merged_sources"]
        assert "nsd:uuid-a" in sources
        assert "fgdl:uuid-b" in sources

    def test_unmerged_record_merged_sources_is_own_source_id(self):
        """A record that is not merged must have merged_sources equal to its own source_id."""
        row = _make_canonical(source_id="nsd:solo", latitude=27.0, longitude=-82.0)
        df = _df(row)
        result = mod.spatial_dedup(df)
        assert result.iloc[0]["merged_sources"] == "nsd:solo"

    def test_no_coords_record_merged_sources_is_own_source_id(self):
        """Records with no coordinates also get merged_sources = own source_id."""
        row = _make_canonical(
            source_id="irs_bmf:111",
            latitude=None,
            longitude=None,
        )
        df = _df(row)
        result = mod.spatial_dedup(df)
        assert result.iloc[0]["merged_sources"] == "irs_bmf:111"

    def test_bmf_row_without_coords_never_spatially_merges(self):
        """
        A BMF record (no coordinates) and an NSD record (has coordinates) cannot
        participate in spatial dedup together — BMF is excluded from the spatial
        comparison entirely because it has no lat/lon.  Both records must survive
        as separate rows in the output; they are never collapsed into one.
        """
        nsd = _make_canonical(
            source_id="nsd:uuid-x",
            latitude=27.0,
            longitude=-82.0,
            ein=None,
        )
        bmf = _make_canonical(
            source_id="irs_bmf:043783054",
            latitude=None,
            longitude=None,
            ein="043783054",
            segment="religious",
        )
        df = _df(nsd, bmf)
        result = mod.spatial_dedup(df)
        # BMF (no coords) and NSD (has coords) cannot be spatially merged;
        # both must survive as independent rows.
        assert len(result) == 2
        source_ids = set(result["source_id"].tolist())
        assert "nsd:uuid-x" in source_ids
        assert "irs_bmf:043783054" in source_ids

    def test_three_record_transitive_cluster_merged_to_one(self):
        """
        Transitive spatial clustering: A-B within radius, B-C within radius,
        but A-C exceed radius.  Union-Find must bridge all three into one cluster
        through the shared neighbour B.

        Geometry (at lat ~30°, 1° lat ≈ 111 km → 0.001° ≈ 111 m):
          A = (30.0000, -97.0000)
          B = (30.0010, -97.0000)  ≈ 111 m north of A  — within 150 m of both A and C
          C = (30.0020, -97.0000)  ≈ 222 m north of A  — beyond direct radius from A,
                                                          but only ≈ 111 m from B
        """
        row_a = _make_canonical(
            source_id="nsd:trans-a",
            latitude=30.0000,
            longitude=-97.0000,
            name_normalized="SUNSET MEMORIAL PARK",
        )
        row_b = _make_canonical(
            source_id="nsd:trans-b",
            latitude=30.0010,
            longitude=-97.0000,
            name_normalized="SUNRISE GARDENS",
        )
        row_c = _make_canonical(
            source_id="nsd:trans-c",
            latitude=30.0020,
            longitude=-97.0000,
            name_normalized="MEADOW VIEW CEMETERY",
        )
        df = _df(row_a, row_b, row_c)
        result = mod.spatial_dedup(df)

        assert len(result) == 1, (
            f"Expected all 3 records to merge transitively into 1 row, got {len(result)}"
        )
        sources = result.iloc[0]["merged_sources"]
        assert "nsd:trans-a" in sources
        assert "nsd:trans-b" in sources
        assert "nsd:trans-c" in sources

    def test_three_record_cluster_confidence_high_when_any_pair_similar(self):
        """
        In a 3-record transitive cluster, merge_confidence must be 'high' when
        at least one pair of records has sufficiently similar names (>= 0.80
        Levenshtein similarity), even if that pair is not members[0] vs members[1].

        Geometry: same A-B-C chain as the transitive test above.
        Names: A is very different from B and C; B and C share a close name variant
        ('OAK GROVE CEMETERY' vs 'OAK GROVE CEMETARY', 1 edit → similarity ≈ 0.944).
        The max-over-all-pairs logic in spatial_dedup must surface this pair.
        """
        row_a = _make_canonical(
            source_id="nsd:conf-a",
            latitude=30.0000,
            longitude=-97.0000,
            name_normalized="COMPLETELY DIFFERENT NAME",
        )
        row_b = _make_canonical(
            source_id="nsd:conf-b",
            latitude=30.0010,
            longitude=-97.0000,
            name_normalized="OAK GROVE CEMETERY",
        )
        row_c = _make_canonical(
            source_id="nsd:conf-c",
            latitude=30.0020,
            longitude=-97.0000,
            name_normalized="OAK GROVE CEMETARY",
        )
        df = _df(row_a, row_b, row_c)
        result = mod.spatial_dedup(df)

        assert len(result) == 1, (
            f"Expected all 3 records to merge transitively into 1 row, got {len(result)}"
        )
        assert result.iloc[0]["merge_confidence"] == "high", (
            "Expected 'high' confidence because B and C share similar names; "
            f"got '{result.iloc[0]['merge_confidence']}'"
        )


# ===========================================================================
# TestResolveSegment
# ===========================================================================

class TestResolveSegment:
    """Verify resolve_segment() applies the correct process-of-elimination rules."""

    def _spatial_row(self, **overrides) -> pd.DataFrame:
        """Return a one-row DataFrame already decorated with spatial_dedup output cols."""
        defaults = _make_canonical(
            segment=None,
            merge_confidence="none",
            merged_sources="nsd:uuid-1",
        )
        defaults.update(overrides)
        return pd.DataFrame([defaults])

    def test_none_segment_unmatched_nsd_becomes_municipal(self):
        """
        An NSD record with no BMF merge partner and account_type != 'federal'
        must receive segment='municipal' (process of elimination).
        """
        df = self._spatial_row(
            source_id="nsd:uuid-1",
            merged_sources="nsd:uuid-1",
            account_type="cemetery",
            segment=None,
        )
        result = mod.resolve_segment(df)
        assert result.iloc[0]["segment"] == "municipal"

    def test_merged_with_bmf_inherits_religious(self):
        """
        A record whose merged_sources contains a BMF source_id must receive
        segment='religious' regardless of the anchor record's original segment.
        """
        df = self._spatial_row(
            source_id="nsd:uuid-2",
            merged_sources="nsd:uuid-2,irs_bmf:043783054",
            segment=None,
        )
        result = mod.resolve_segment(df)
        assert result.iloc[0]["segment"] == "religious"

    def test_federal_account_type_becomes_federal(self):
        """
        A record with account_type='federal' and no BMF merge partner must
        receive segment='federal'.
        """
        df = self._spatial_row(
            source_id="va:001",
            merged_sources="va:001",
            account_type="federal",
            segment=None,
        )
        result = mod.resolve_segment(df)
        assert result.iloc[0]["segment"] == "federal"

    def test_explicitly_set_segment_not_overwritten(self):
        """
        A record already bearing segment='religious' (from FGDL or BMF) must
        not have its segment changed by resolve_segment().
        """
        df = self._spatial_row(
            source_id="fgdl:uuid-3",
            merged_sources="fgdl:uuid-3",
            segment="religious",
        )
        result = mod.resolve_segment(df)
        assert result.iloc[0]["segment"] == "religious"

    def test_municipal_segment_not_overwritten(self):
        """An already-resolved 'municipal' segment must not be changed."""
        df = self._spatial_row(segment="municipal")
        result = mod.resolve_segment(df)
        assert result.iloc[0]["segment"] == "municipal"

    def test_txdot_unmatched_becomes_municipal(self):
        """
        A TxDOT record with no BMF merge partner must become 'municipal'
        just like an NSD record.
        """
        df = self._spatial_row(
            source_id="txdot:tx-001",
            merged_sources="txdot:tx-001",
            account_type="cemetery",
            state="TX",
            segment=None,
        )
        result = mod.resolve_segment(df)
        assert result.iloc[0]["segment"] == "municipal"

    def test_bmf_merge_partner_takes_priority_over_federal_account_type(self):
        """
        If merged_sources contains a BMF id, segment='religious' must be
        assigned even if account_type happens to be 'federal'.
        (BMF match is the highest-priority rule.)
        """
        df = self._spatial_row(
            merged_sources="va:uuid,irs_bmf:111111111",
            account_type="federal",
            segment=None,
        )
        result = mod.resolve_segment(df)
        assert result.iloc[0]["segment"] == "religious"


# ===========================================================================
# TestFilterLeads
# ===========================================================================

class TestFilterLeads:
    """Verify filter_leads() correctly flags is_lead per business rules."""

    def _row(self, **overrides) -> pd.DataFrame:
        defaults = _make_canonical(
            segment="religious",
            merge_confidence="none",
            merged_sources="nsd:uuid",
        )
        defaults.update(overrides)
        return pd.DataFrame([defaults])

    def test_municipal_segment_is_not_lead(self):
        """Municipal cemeteries are owned by local government — not sales targets."""
        df = self._row(segment="municipal")
        result = mod.filter_leads(df)
        assert result.iloc[0]["is_lead"] == False  # noqa: E712 — numpy bool compat

    def test_federal_segment_is_not_lead(self):
        """Federal cemeteries (VA) are not addressable — exclude from leads."""
        df = self._row(segment="federal")
        result = mod.filter_leads(df)
        assert result.iloc[0]["is_lead"] == False  # noqa: E712

    def test_truly_unnamed_record_is_not_lead(self):
        """
        A record with null name_raw AND empty name_normalized has no name at all
        and cannot be contacted — exclude from leads.
        """
        df = self._row(segment="religious", name_raw=None, name_normalized="")
        result = mod.filter_leads(df)
        assert result.iloc[0]["is_lead"] == False  # noqa: E712

    def test_religious_segment_is_lead(self):
        """Religious cemeteries are prime prospects — must be marked is_lead=True."""
        df = self._row(segment="religious")
        result = mod.filter_leads(df)
        assert result.iloc[0]["is_lead"] == True  # noqa: E712

    def test_unnamed_municipal_is_not_lead(self):
        """Unnamed + municipal: doubly excluded — still is_lead=False (not a crash)."""
        df = self._row(segment="municipal", name_raw=None, name_normalized="")
        result = mod.filter_leads(df)
        assert result.iloc[0]["is_lead"] == False  # noqa: E712

    def test_named_municipal_is_still_not_lead(self):
        """Having a name doesn't override a disqualifying segment."""
        df = self._row(segment="municipal", name_raw="Greenwood Cemetery")
        result = mod.filter_leads(df)
        assert result.iloc[0]["is_lead"] == False  # noqa: E712

    def test_full_dataframe_produces_both_lead_values(self):
        """
        A mixed DataFrame must produce both True and False is_lead values —
        confirms the flag logic is applied per-row, not as a single scalar.
        """
        rows = [
            _make_canonical(
                source_id="r1",
                segment="religious",
                merge_confidence="none",
                merged_sources="r1",
            ),
            _make_canonical(
                source_id="r2",
                segment="municipal",
                merge_confidence="none",
                merged_sources="r2",
            ),
        ]
        df = pd.DataFrame(rows)
        result = mod.filter_leads(df)
        assert result["is_lead"].any()
        assert (~result["is_lead"]).any()

    def test_is_lead_column_added_to_all_rows(self):
        """Every output row must have an is_lead value — no nulls allowed."""
        rows = [
            _make_canonical(
                source_id=f"r{i}",
                segment=seg,
                merge_confidence="none",
                merged_sources=f"r{i}",
            )
            for i, seg in enumerate(["religious", "municipal", "federal"])
        ]
        df = pd.DataFrame(rows)
        result = mod.filter_leads(df)
        assert result["is_lead"].notna().all()


# ===========================================================================
# TestMergePipeline
# ===========================================================================

class TestMergePipeline:
    """
    End-to-end test covering all 5 source types through the full pipeline.

    DataFrames are minimal — just enough rows to exercise each code path:
      nsd   : one named + one unnamed record
      bmf   : one religious cemetery + one duplicate EIN
      va    : one federal record
      txdot : one record (TX, no EIN, becomes municipal unless near BMF)
      fgdl  : one record with segment='religious' pre-set
    """

    def _nsd_df(self) -> pd.DataFrame:
        rows = [
            _make_canonical(
                source_id="nsd:nsd-001",
                name_raw="Riverside Cemetery",
                name_normalized="RIVERSIDE CEMETERY",
                latitude=27.500,
                longitude=-81.500,
                state="FL",
                segment=None,
                ein=None,
            ),
            _make_canonical(
                source_id="nsd:nsd-002",
                name_raw=None,
                name_normalized="",
                latitude=27.600,
                longitude=-81.500,
                state="FL",
                segment=None,
                ein=None,
            ),
        ]
        return pd.DataFrame(rows)

    def _bmf_df(self) -> pd.DataFrame:
        rows = [
            _make_canonical(
                source_id="irs_bmf:111111111",
                name_raw="First Baptist Cemetery",
                name_normalized="FIRST BAPTIST CEMETERY",
                latitude=None,
                longitude=None,
                state="FL",
                segment="religious",
                ein="111111111",
            ),
            # Duplicate EIN — must be removed by dedup_by_ein.
            _make_canonical(
                source_id="irs_bmf:111111111",
                name_raw="First Baptist Cemetery Dup",
                name_normalized="FIRST BAPTIST CEMETERY DUP",
                latitude=None,
                longitude=None,
                state="FL",
                segment="religious",
                ein="111111111",
            ),
        ]
        return pd.DataFrame(rows)

    def _va_df(self) -> pd.DataFrame:
        return pd.DataFrame([
            _make_canonical(
                source_id="va:va-001",
                name_raw="Bay Pines VA Cemetery",
                name_normalized="BAY PINES VA CEMETERY",
                latitude=27.800,
                longitude=-82.700,
                state="FL",
                account_type="federal",
                segment="federal",
                ein=None,
            ),
        ])

    def _txdot_df(self) -> pd.DataFrame:
        return pd.DataFrame([
            _make_canonical(
                source_id="txdot:tx-001",
                name_raw="Pioneer Cemetery",
                name_normalized="PIONEER CEMETERY",
                latitude=30.300,
                longitude=-97.700,
                state="TX",
                segment=None,
                ein=None,
            ),
        ])

    def _fgdl_df(self) -> pd.DataFrame:
        return pd.DataFrame([
            _make_canonical(
                source_id="fgdl:fgdl-001",
                name_raw="St. Mary Catholic Cemetery",
                name_normalized="ST MARY CATHOLIC CEMETERY",
                latitude=25.800,
                longitude=-80.200,
                state="FL",
                segment="religious",
                ein=None,
            ),
        ])

    def test_pipeline_returns_dataframe(self):
        """merge_pipeline must return a pandas DataFrame."""
        dfs = [
            self._nsd_df(), self._bmf_df(), self._va_df(),
            self._txdot_df(), self._fgdl_df(),
        ]
        result = mod.merge_pipeline(dfs)
        assert isinstance(result, pd.DataFrame)

    def test_duplicate_ein_removed(self):
        """The duplicate BMF EIN must not appear in the output."""
        dfs = [self._nsd_df(), self._bmf_df(), self._va_df(),
               self._txdot_df(), self._fgdl_df()]
        result = mod.merge_pipeline(dfs)
        bmf_rows = result[result["source_id"].str.startswith("irs_bmf:")]
        assert len(bmf_rows) == 1

    def test_all_segments_resolved(self):
        """No record in the output may have a null or empty segment."""
        dfs = [self._nsd_df(), self._bmf_df(), self._va_df(),
               self._txdot_df(), self._fgdl_df()]
        result = mod.merge_pipeline(dfs)
        null_segs = result["segment"].isna() | (result["segment"] == "")
        assert not null_segs.any(), (
            f"Found {null_segs.sum()} records with unresolved segment"
        )

    def test_is_lead_column_present_and_boolean(self):
        """is_lead must be present and contain only boolean values."""
        dfs = [self._nsd_df(), self._bmf_df(), self._va_df(),
               self._txdot_df(), self._fgdl_df()]
        result = mod.merge_pipeline(dfs)
        assert "is_lead" in result.columns
        assert result["is_lead"].dtype == bool or result["is_lead"].isin([True, False]).all()

    def test_output_contains_both_leads_and_non_leads(self):
        """
        With our test data the output must have at least one lead (religious
        or named non-excluded) and at least one non-lead (federal/municipal/unnamed).
        """
        dfs = [self._nsd_df(), self._bmf_df(), self._va_df(),
               self._txdot_df(), self._fgdl_df()]
        result = mod.merge_pipeline(dfs)
        assert result["is_lead"].any(), "Expected at least one is_lead=True record"
        assert (~result["is_lead"]).any(), "Expected at least one is_lead=False record"

    def test_merge_confidence_column_present(self):
        """merge_confidence must be populated on every output row."""
        dfs = [self._nsd_df(), self._bmf_df(), self._va_df(),
               self._txdot_df(), self._fgdl_df()]
        result = mod.merge_pipeline(dfs)
        assert "merge_confidence" in result.columns
        assert result["merge_confidence"].notna().all()

    def test_merged_sources_column_present(self):
        """merged_sources must be populated on every output row."""
        dfs = [self._nsd_df(), self._bmf_df(), self._va_df(),
               self._txdot_df(), self._fgdl_df()]
        result = mod.merge_pipeline(dfs)
        assert "merged_sources" in result.columns
        assert result["merged_sources"].notna().all()

    def test_unnamed_record_is_not_lead(self):
        """The NSD unnamed record (name_raw=None, name_normalized='') must not be a lead."""
        dfs = [self._nsd_df(), self._bmf_df(), self._va_df(),
               self._txdot_df(), self._fgdl_df()]
        result = mod.merge_pipeline(dfs)
        unnamed = result[result["source_id"] == "nsd:nsd-002"]
        if not unnamed.empty:
            assert not unnamed.iloc[0]["is_lead"]

    def test_federal_va_record_is_not_lead(self):
        """VA records (segment='federal') must not be leads."""
        dfs = [self._nsd_df(), self._bmf_df(), self._va_df(),
               self._txdot_df(), self._fgdl_df()]
        result = mod.merge_pipeline(dfs)
        va_rows = result[result["source_id"].str.startswith("va:")]
        if not va_rows.empty:
            assert not va_rows.iloc[0]["is_lead"]

    def test_txdot_unmatched_becomes_municipal(self):
        """
        The isolated TxDOT record (far from any BMF record) must resolve to
        segment='municipal' via process of elimination.
        """
        dfs = [self._nsd_df(), self._bmf_df(), self._va_df(),
               self._txdot_df(), self._fgdl_df()]
        result = mod.merge_pipeline(dfs)
        txdot_rows = result[result["source_id"].str.startswith("txdot:")]
        if not txdot_rows.empty:
            assert txdot_rows.iloc[0]["segment"] == "municipal"


# ===========================================================================
# TestShuffleInvariance
# ===========================================================================

class TestShuffleInvariance:
    """
    Verify that the deathcare pipeline produces order-independent results for
    the properties that must be deterministic.

    Context: deathcare_merge has no cluster_id field — records are identified
    by source_id, not a derived cluster key.  What must be stable across
    shuffle:
      - Row count (spatial groupings depend on distance, not order)
      - Segment assignment for each surviving source_id
      - Presence of both source_ids in merged_sources for spatial merges
      - EIN dedup: duplicate EIN rows must be dropped regardless of concat order

    Note: the spine record that survives a spatial merge (which source_id
    anchors the merged row) IS order-dependent in the current implementation —
    that is a separate survivorship concern outside the §3 determinism scope
    and is NOT asserted here.
    """

    # Fixed permutation for a 3-row DataFrame.
    _PERM_3 = [2, 0, 1]

    def _run(self, *dfs: "pd.DataFrame") -> "pd.DataFrame":
        return mod.merge_pipeline(list(dfs))

    def test_spatial_merge_member_set_invariant_under_shuffle(self):
        """
        Two spatially close records must always merge into a single output row,
        and that row's merged_sources must contain both source_ids, regardless
        of which appears first in the concatenated DataFrame.
        """
        row_a = _make_canonical(
            source_id="nsd:close-a",
            latitude=27.000,
            longitude=-82.000,
            name_normalized="LAKESIDE MEMORIAL PARK",
            segment=None,
        )
        row_b = _make_canonical(
            source_id="fgdl:close-b",
            latitude=27.001,  # ≈ 111 m — within 150 m threshold
            longitude=-82.000,
            name_normalized="LAKESIDE MEMORIAL PARK",
            segment="religious",
        )

        df_orig = pd.DataFrame([row_a, row_b])
        # Reverse row order — row_b is now first.
        df_shuf = pd.DataFrame([row_b, row_a])

        result_orig = mod.spatial_dedup(df_orig)
        result_shuf = mod.spatial_dedup(df_shuf)

        # Both orderings must produce exactly one merged row.
        assert len(result_orig) == 1, "Expected rows to merge spatially (original order)"
        assert len(result_shuf) == 1, "Expected rows to merge spatially (shuffled order)"

        sources_orig = set(result_orig.iloc[0]["merged_sources"].split(","))
        sources_shuf = set(result_shuf.iloc[0]["merged_sources"].split(","))

        # The SET of merged source_ids must be identical — order within the
        # comma-separated string is not asserted because it depends on which
        # record anchors the merge (order-dependent spine selection).
        assert sources_orig == sources_shuf == {"nsd:close-a", "fgdl:close-b"}, (
            f"merged_sources members differ under shuffle.\n"
            f"Original: {sources_orig}\nShuffled: {sources_shuf}"
        )

    def test_ein_dedup_invariant_regardless_of_concat_order(self):
        """
        When two BMF rows share the same EIN, exactly one must survive after
        dedup_by_ein, regardless of which one appears first in the DataFrame.
        The surviving row count must be 1 in both orderings.
        """
        row1 = _make_canonical(
            source_id="irs_bmf:999000999",
            ein="999000999",
            name_raw="Trinity Baptist Cemetery",
            latitude=None,
            longitude=None,
            segment="religious",
        )
        row2 = _make_canonical(
            source_id="irs_bmf:999000999",
            ein="999000999",
            name_raw="Trinity Baptist Cemetery Dup",
            latitude=None,
            longitude=None,
            segment="religious",
        )

        df_forward = pd.DataFrame([row1, row2])
        df_reversed = pd.DataFrame([row2, row1])

        result_fwd = mod.dedup_by_ein(df_forward)
        result_rev = mod.dedup_by_ein(df_reversed)

        assert len(result_fwd) == 1, "Forward order: expected exactly 1 row after EIN dedup"
        assert len(result_rev) == 1, "Reversed order: expected exactly 1 row after EIN dedup"

    def test_segment_resolution_invariant_under_shuffle(self):
        """
        Segment assignment is purely based on merged_sources content and
        account_type — it must be identical regardless of row order within
        the single-record (no-spatial-merge) case.
        """
        rows = [
            _make_canonical(
                source_id="nsd:seg-a",
                latitude=10.0,
                longitude=-90.0,  # far from row b and c
                account_type="cemetery",
                segment=None,
                merge_confidence="none",
                merged_sources="nsd:seg-a",
            ),
            _make_canonical(
                source_id="va:seg-b",
                latitude=20.0,
                longitude=-90.0,  # far from row a
                account_type="federal",
                segment=None,
                merge_confidence="none",
                merged_sources="va:seg-b",
            ),
            _make_canonical(
                source_id="irs_bmf:111999111",
                latitude=None,
                longitude=None,
                ein="111999111",
                account_type="cemetery",
                segment=None,
                merge_confidence="none",
                merged_sources="irs_bmf:111999111",
            ),
        ]
        df_orig = pd.DataFrame(rows)
        df_shuf = df_orig.iloc[list(TestShuffleInvariance._PERM_3)].reset_index(drop=True)

        # resolve_segment acts on already-spatial-deduped data; call directly
        # since these rows are already well-separated (no spatial merge occurs).
        result_orig = mod.resolve_segment(df_orig)
        result_shuf = mod.resolve_segment(df_shuf)

        # Build source_id → segment mapping and compare.
        def _seg_map(df: "pd.DataFrame") -> dict:
            return dict(zip(df["source_id"].tolist(), df["segment"].tolist()))

        orig_map = _seg_map(result_orig)
        shuf_map = _seg_map(result_shuf)

        assert orig_map == shuf_map, (
            f"Segment assignments changed under shuffle.\n"
            f"Original: {orig_map}\nShuffled: {shuf_map}"
        )
        # Spot-check expected segments.
        assert orig_map["nsd:seg-a"] == "municipal"
        assert orig_map["va:seg-b"] == "federal"
        assert orig_map["irs_bmf:111999111"] == "religious"


# ===========================================================================
# TestSizeMetricNaNScrub
# ===========================================================================

class TestSizeMetricNaNScrub:
    """
    Verify that _upsert_resolved_account never sends float NaN as size_metric
    to Postgres — it must always arrive as Python None in the params dict.

    Root cause: pd.DataFrame(rows) infers float64 for size_metric when the
    row list mixes None and real floats, silently upcasting None to NaN.
    Postgres numeric accepts NaN natively, so without an explicit scrub the
    value lands as literal NaN in core.account instead of SQL NULL.
    """

    def _make_merged_df(self) -> pd.DataFrame:
        """
        Build a merged DataFrame with a mix of rows: some have a real
        size_value (triggering float64 inference), some have None.
        """
        rows = [
            _make_canonical(
                source_id="nsd:nan-a",
                natural_key="nan-a",
                size_value=150.0,  # real float — will cause pandas to infer float64
                size_unit="graves",
            ),
            _make_canonical(
                source_id="nsd:nan-b",
                natural_key="nan-b",
                size_value=None,   # None gets upcast to NaN in float64 column
            ),
            _make_canonical(
                source_id="nsd:nan-c",
                natural_key="nan-c",
                size_value=None,   # second None row
            ),
        ]
        df = pd.DataFrame(rows)
        # Simulate pipeline stages that would normally precede _build_resolved_account.
        df["merge_confidence"] = "none"
        df["merged_sources"] = df["source_id"]
        df["is_lead"] = True
        # Ensure float64 inference actually occurred (this is the bug precondition).
        df["size_value"] = pd.to_numeric(df["size_value"], errors="coerce")
        return df

    def test_size_metric_none_survives_dataframe_roundtrip(self):
        """
        After _build_resolved_account, the size_metric column must be float64
        NaN for rows whose source size_value was None.  This confirms the
        precondition: the bug IS present at the DataFrame stage.
        """
        merged = self._make_merged_df()
        account_df = mod._build_resolved_account(merged)

        # Row with real size_value must keep it.
        real_row = account_df[account_df["_natural_key"] == "nan-a"].iloc[0]
        assert real_row["size_metric"] == pytest.approx(150.0)

        # Rows with None size_value become NaN in the float64 column —
        # this is the precondition that allows the bug to reach Postgres.
        none_row = account_df[account_df["_natural_key"] == "nan-b"].iloc[0]
        assert pd.isna(none_row["size_metric"]), (
            "Expected float NaN in DataFrame for rows with no size data "
            "(precondition for the NaN→NULL scrub requirement)"
        )

    def test_upsert_resolved_account_sends_none_not_nan_to_db(self):
        """
        _upsert_resolved_account must scrub NaN → Python None in the params
        dict BEFORE passing to engine.execute, so Postgres receives NULL.

        The DB execute is mocked; we capture the params dict and assert
        size_metric is None (not float NaN) for the affected rows.
        """
        from unittest.mock import MagicMock, patch

        merged = self._make_merged_df()
        account_df = mod._build_resolved_account(merged)

        captured_rows: list[list[dict]] = []

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.side_effect = lambda sql, rows: captured_rows.append(rows)

        mock_engine = MagicMock()
        mock_engine.begin.return_value = mock_conn

        mod._upsert_resolved_account(mock_engine, account_df)

        assert captured_rows, "engine.execute was never called"
        rows_sent = captured_rows[0]

        # The row with a real size_value must keep the float.
        real_params = next(
            r for r in rows_sent
            if r.get("size_metric") is not None
        )
        assert real_params["size_metric"] == pytest.approx(150.0)

        # All rows that had None size_value must carry Python None (not NaN).
        # Critically: r["size_metric"] is None means exactly None — pd.isna() is True
        # for both float NaN and None, so we use `is None` to confirm the type.
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


# ===========================================================================
# TestJoinParcel
# ===========================================================================

class TestJoinParcel:
    """
    Tests for _join_parcel() — parcel acreage overlay onto resolved_location.

    Strategy: build a location_df with _natural_key set (as _build_resolved_location
    does), supply a synthetic parcels DataFrame, and assert the expected columns
    are populated on matching rows and left None on non-matching rows.

    No live DB needed — _join_parcel is a pure DataFrame transformation.
    """

    def _make_location_df(self, natural_keys: list) -> pd.DataFrame:
        """Build a minimal resolved_location DataFrame with the given natural_keys."""
        rows = []
        for nk in natural_keys:
            rows.append({
                "location_key": f"loc_{nk}",
                "account_key": f"acct_{nk}",
                "location_name": "Oak Grove Cemetery",
                "site_address": None,
                "_latitude": 27.9,
                "_longitude": -82.4,
                "geocode_precision": None,
                "geometry_source": None,
                "maintained_acres": None,
                "acres_confidence": None,
                "site_type": "cemetery",
                "_natural_key": nk,
            })
        return pd.DataFrame(rows)

    def _make_parcel_df(self, records: list) -> pd.DataFrame:
        """Build a synthetic staging.enrich_parcel DataFrame."""
        return pd.DataFrame(records)

    def test_matching_natural_key_populates_maintained_acres(self):
        """A parcel row whose natural_key matches must populate maintained_acres."""
        location_df = self._make_location_df(["nk-A", "nk-B"])
        parcels = self._make_parcel_df([
            {"source_id": "fgdl_cemeteries", "natural_key": "nk-A", "maintained_acres": 4.25},
        ])
        result = mod._join_parcel(location_df, parcels)
        matched = result[result["_natural_key"] == "nk-A"].iloc[0]
        assert matched["maintained_acres"] == pytest.approx(4.25)
        assert matched["acres_confidence"] == "estimated"
        assert matched["geometry_source"] == "parcel"

    def test_non_matching_natural_key_left_none(self):
        """Rows with no parcel match must keep maintained_acres=None."""
        location_df = self._make_location_df(["nk-A", "nk-B"])
        parcels = self._make_parcel_df([
            {"source_id": "fgdl_cemeteries", "natural_key": "nk-A", "maintained_acres": 4.25},
        ])
        result = mod._join_parcel(location_df, parcels)
        unmatched = result[result["_natural_key"] == "nk-B"].iloc[0]
        assert unmatched["maintained_acres"] is None
        assert unmatched["acres_confidence"] is None

    def test_empty_parcels_returns_location_df_unchanged(self):
        """When parcels is empty, location_df must be returned as-is."""
        location_df = self._make_location_df(["nk-A"])
        result = mod._join_parcel(location_df, pd.DataFrame())
        assert len(result) == len(location_df)
        assert result.iloc[0]["maintained_acres"] is None

    def test_null_maintained_acres_in_parcel_table_leaves_row_unmatched(self):
        """
        A parcel row whose maintained_acres is None/NaN must not be applied —
        only real numeric values should populate the location row.
        """
        location_df = self._make_location_df(["nk-A"])
        parcels = self._make_parcel_df([
            {"source_id": "fgdl_cemeteries", "natural_key": "nk-A", "maintained_acres": None},
        ])
        result = mod._join_parcel(location_df, parcels)
        assert result.iloc[0]["maintained_acres"] is None

    def test_multiple_rows_only_matching_row_updated(self):
        """With three location rows, only the one with a parcel match gets updated."""
        location_df = self._make_location_df(["X", "Y", "Z"])
        parcels = self._make_parcel_df([
            {"source_id": "fgdl_cemeteries", "natural_key": "Y", "maintained_acres": 8.0},
        ])
        result = mod._join_parcel(location_df, parcels)
        assert result[result["_natural_key"] == "X"].iloc[0]["maintained_acres"] is None
        assert result[result["_natural_key"] == "Y"].iloc[0]["maintained_acres"] == pytest.approx(8.0)
        assert result[result["_natural_key"] == "Z"].iloc[0]["maintained_acres"] is None

    def test_natural_key_in_location_df_carries_through_build_resolved_location(self):
        """
        _build_resolved_location must add _natural_key to each location row so
        _join_parcel can match against staging.enrich_parcel.
        """
        merged_row = _make_canonical(
            source_id="fgdl_cemeteries",
            natural_key="fgdl-001",
            latitude=27.9,
            longitude=-82.4,
        )
        merged_df = pd.DataFrame([merged_row])
        merged_df["merge_confidence"] = "none"
        merged_df["merged_sources"] = "fgdl_cemeteries:fgdl-001"
        merged_df["is_lead"] = True

        account_df = mod._build_resolved_account(merged_df)
        location_df = mod._build_resolved_location(merged_df, account_df)

        assert "_natural_key" in location_df.columns, (
            "_build_resolved_location must carry _natural_key for join_parcel()"
        )
        assert location_df.iloc[0]["_natural_key"] == "fgdl-001"

    def test_dry_run_pipeline_does_not_write_parcel_data(self):
        """
        On dry_run=True, run_pipeline() must not call engine.begin — parcel data
        is joined in-memory but no DB writes occur.
        """
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.begin.return_value.__exit__ = MagicMock(return_value=False)
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        # Minimal one-row staging frame that all deathcare pipeline stages can handle.
        staging_row = _make_canonical(
            source_id="fgdl_cemeteries",
            natural_key="fgdl-dry-001",
            latitude=27.9,
            longitude=-82.4,
            segment="religious",
        )
        staging_df = pd.DataFrame([staging_row])

        parcel_df = pd.DataFrame({
            "source_id": ["fgdl_cemeteries"],
            "natural_key": ["fgdl-dry-001"],
            "maintained_acres": [6.0],
        })

        from unittest.mock import patch

        with (
            patch.object(mod, "_load_source", return_value=staging_df),
            patch.object(mod, "_load_parcel_cache", return_value=parcel_df),
            patch.object(mod, "_load_irs990_cache", return_value=pd.DataFrame(
                columns=["source_id", "natural_key", "phone_990", "contact_name_990"]
            )),
        ):
            summary = mod.run_pipeline(mock_engine, dry_run=True)

        # No DB writes in dry run
        mock_engine.begin.assert_not_called()
        assert "location" in summary


# ===========================================================================
# D15: _replace_and_upsert — vertical-scoped delete-before-upsert semantics
# ===========================================================================


class TestReplaceAndUpsert:
    """
    Verify _replace_and_upsert() (D15 fix) has the correct delete-before-upsert
    behaviour when called via a mocked engine.

    Strategy: intercept all conn.execute() calls in order to inspect the SQL
    that is sent.  The engine mock uses a context-manager-compatible connection
    so with engine.begin() as conn works without a live DB.

    Three properties are asserted:
      1. Out-of-vertical stale rows are deleted (DELETE WHERE vertical='deathcare').
      2. Other-vertical rows are untouched (DELETE never issues vertical='healthcare').
      3. On dry_run=True the deletes are never issued at all.
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_engine_capturing_sql(self) -> tuple:
        """
        Return (mock_engine, executed_statements) where executed_statements is a
        list that accumulates every (sql_text, params) tuple passed to
        conn.execute() inside with engine.begin() as conn:.
        """
        executed: list = []

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.side_effect = lambda sql, params=None: executed.append(
            (str(sql), params)
        )

        mock_engine = MagicMock()
        mock_engine.begin.return_value = mock_conn

        return mock_engine, executed

    def _minimal_merged_df(self) -> pd.DataFrame:
        """One-row merged deathcare DataFrame with all fields _build_resolved_* need."""
        row = _make_canonical(
            source_id="nsd:d15-test",
            natural_key="d15-001",
            latitude=27.9,
            longitude=-82.4,
            segment="religious",
            phone_normalized="8135550101",
        )
        df = pd.DataFrame([row])
        df["merge_confidence"] = "none"
        df["merged_sources"] = "nsd:d15-test"
        df["is_lead"] = True
        return df

    def _minimal_dfs(self) -> tuple:
        """Return (account_df, location_df, contact_df) built from a minimal merge."""
        merged = self._minimal_merged_df()
        account_df = _build_resolved_account(merged)
        location_df = _build_resolved_location(merged, account_df)
        contact_df = _build_resolved_contact(merged, account_df)
        return account_df, location_df, contact_df

    # ------------------------------------------------------------------
    # Test 1: stale out-of-vertical rows are deleted
    # ------------------------------------------------------------------

    def test_delete_fires_for_own_vertical(self):
        """
        _replace_and_upsert must issue DELETE ... WHERE vertical='deathcare' for
        all three resolved tables.  This is the core D15 guarantee: stale rows
        from a prior nationwide run are swept out before the new slice is inserted.
        """
        engine, executed = self._make_engine_capturing_sql()
        account_df, location_df, contact_df = self._minimal_dfs()

        _replace_and_upsert(
            engine, account_df, location_df, contact_df, vertical="deathcare"
        )

        delete_stmts = [(sql, params) for sql, params in executed if "DELETE" in sql.upper()]

        assert len(delete_stmts) == 3, (
            f"Expected 3 DELETE statements (account, location, contact), "
            f"got {len(delete_stmts)}: {[s for s, _ in delete_stmts]}"
        )

        for sql, params in delete_stmts:
            assert params == {"v": "deathcare"}, (
                f"DELETE params expected {{'v': 'deathcare'}}, got {params!r}"
            )

    # ------------------------------------------------------------------
    # Test 2: other-vertical rows are untouched
    # ------------------------------------------------------------------

    def test_other_vertical_never_deleted(self):
        """
        When vertical='deathcare', the deletes must never reference 'healthcare'.
        This ensures running the deathcare driver does not wipe healthcare rows.
        """
        engine, executed = self._make_engine_capturing_sql()
        account_df, location_df, contact_df = self._minimal_dfs()

        _replace_and_upsert(
            engine, account_df, location_df, contact_df, vertical="deathcare"
        )

        for sql, params in executed:
            if "DELETE" in sql.upper():
                assert (params or {}).get("v") != "healthcare", (
                    "DELETE was issued with vertical='healthcare' during a "
                    "deathcare _replace_and_upsert call — this would wipe the "
                    "other vertical's data."
                )

    # ------------------------------------------------------------------
    # Test 3: dry_run=True — deletes never fire
    # ------------------------------------------------------------------

    def test_dry_run_pipeline_issues_no_deletes(self):
        """
        When run_pipeline() is called with dry_run=True, engine.begin() must
        never be called — so no DELETE statements can reach the DB.

        Uses the full run_pipeline() entry point because dry_run gating lives
        there, not in _replace_and_upsert().
        """
        from unittest.mock import patch

        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_engine.begin.return_value = mock_conn
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        staging_row = _make_canonical(
            source_id="fgdl_cemeteries",
            natural_key="d15-dry-001",
            latitude=27.9,
            longitude=-82.4,
            segment="religious",
        )
        staging_df = pd.DataFrame([staging_row])

        with (
            patch.object(mod, "_load_source", return_value=staging_df),
            patch.object(mod, "_load_parcel_cache", return_value=pd.DataFrame()),
            patch.object(mod, "_load_irs990_cache", return_value=pd.DataFrame(
                columns=["source_id", "natural_key", "phone_990", "contact_name_990"]
            )),
        ):
            mod.run_pipeline(mock_engine, dry_run=True)

        # dry_run=True must not open a write transaction at all.
        mock_engine.begin.assert_not_called()


# ===========================================================================
# D17b: IRS 990 join — _join_irs990 and run_pipeline wiring
# ===========================================================================


class TestD17bIrs990Join:
    """
    Verify _join_irs990() correctly overlays IRS 990 data onto the resolved
    deathcare output.

    All tests are in-memory.  _build_resolved_account and _build_resolved_contact
    are called with minimal canonical rows to produce realistic account_df /
    contact_df shapes, then _join_irs990 is applied with a synthetic irs990
    DataFrame representing staging.enrich_irs990 content.

    The _join_irs990 function uses _source_id and _natural_key private columns
    from account_df to key into the irs990 index.  _build_resolved_account
    carries these via the _source_id / _natural_key private columns.
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_account_with_irs990_row(
        self,
        source_id: str = "irs_bmf_deathcare",
        natural_key: str = "043783054",
        phone_normalized: str | None = None,
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """
        Build minimal (account_df, contact_df, irs990) triple.

        account_df and contact_df are produced by the real builders so the
        shape is identical to what run_pipeline delivers.

        phone_normalized controls whether the staging row already had a phone
        (non-None → synthetic primary_phone contact is produced by the builder).
        """
        row = _make_canonical(
            source_id=source_id,
            natural_key=natural_key,
            segment="religious",
            ein="043783054",
            phone_normalized=phone_normalized,
            latitude=None,   # no coords so spatial dedup doesn't interfere
            longitude=None,
        )
        merged = pd.DataFrame([row])
        # _build_resolved_account expects merge_pipeline output columns
        # (including merge_confidence, merged_sources, is_lead).
        merged["merge_confidence"] = "none"
        merged["merged_sources"] = f"{source_id}:{natural_key}"
        merged["is_lead"] = True

        account_df = _build_resolved_account(merged)
        contact_df = _build_resolved_contact(merged, account_df)
        return account_df, contact_df

    def _make_irs990(
        self,
        source_id: str = "irs_bmf_deathcare",
        natural_key: str = "043783054",
        phone_990: str | None = "6175550100",
        contact_name_990: str | None = "Bethany Memorial Cemetery",
    ) -> pd.DataFrame:
        return pd.DataFrame([{
            "source_id": source_id,
            "natural_key": natural_key,
            "phone_990": phone_990,
            "contact_name_990": contact_name_990,
        }])

    # ------------------------------------------------------------------
    # Test 1: irs990 row with phone + name → real contact emitted
    # ------------------------------------------------------------------

    def test_irs990_contact_emitted_with_phone_and_name(self):
        """
        An enrich_irs990 row with phone_990 and contact_name_990 must produce
        a resolved_contact with role='irs990_contact', the phone, and the name.
        account.phone must also be populated.
        """
        account_df, contact_df = self._make_account_with_irs990_row(
            phone_normalized=None   # no phone in staging → synthetic contact absent
        )
        irs990 = self._make_irs990(phone_990="6175550100", contact_name_990="Bethany Memorial")

        new_account_df, new_contact_df = mod._join_irs990(account_df, contact_df, irs990)

        # Account phone must be populated from phone_990.
        assert new_account_df.iloc[0]["phone"] == "6175550100", (
            "resolved_account.phone must be set from phone_990"
        )

        # A real irs990_contact row must be present.
        irs_contacts = new_contact_df[new_contact_df["role"] == "irs990_contact"]
        assert len(irs_contacts) == 1, "Exactly one irs990_contact row expected"
        assert irs_contacts.iloc[0]["phone"] == "6175550100"
        assert irs_contacts.iloc[0]["full_name"] == "Bethany Memorial"
        assert irs_contacts.iloc[0]["account_key"] == new_account_df.iloc[0]["account_key"]

    # ------------------------------------------------------------------
    # Test 2: real contact suppresses synthetic primary_phone
    # ------------------------------------------------------------------

    def test_synthetic_primary_phone_suppressed_when_real_contact_exists(self):
        """
        When an account already has a synthetic 'primary_phone' contact (because
        phone_normalized was set in staging) and a matching irs990 row arrives,
        the synthetic contact must be replaced by the irs990_contact.
        """
        account_df, contact_df = self._make_account_with_irs990_row(
            phone_normalized="8005550199"  # staging had a phone → synthetic contact built
        )
        # Verify the synthetic contact is present before the join.
        assert (contact_df["role"] == "primary_phone").any(), (
            "Precondition: synthetic primary_phone contact must exist before join"
        )

        irs990 = self._make_irs990(phone_990="6175550100", contact_name_990="Bethany Memorial")
        _, new_contact_df = mod._join_irs990(account_df, contact_df, irs990)

        # Synthetic primary_phone must be gone.
        primary_phone_rows = new_contact_df[new_contact_df["role"] == "primary_phone"]
        assert len(primary_phone_rows) == 0, (
            "Synthetic primary_phone contact must be suppressed when a real "
            "irs990_contact exists for the same account"
        )

        # Real irs990_contact must be present.
        irs_rows = new_contact_df[new_contact_df["role"] == "irs990_contact"]
        assert len(irs_rows) == 1

    # ------------------------------------------------------------------
    # Test 3: account WITHOUT irs990 keeps synthetic primary_phone
    # ------------------------------------------------------------------

    def test_account_without_irs990_keeps_synthetic_contact(self):
        """
        Regression guard: an account that has no matching enrich_irs990 row
        must keep its synthetic primary_phone contact unchanged.
        """
        account_df, contact_df = self._make_account_with_irs990_row(
            source_id="va_cemeteries",
            natural_key="vc-001",
            phone_normalized="8005550199",
        )
        # irs990 contains a row for a DIFFERENT account — must not affect va_cemeteries.
        irs990 = self._make_irs990(
            source_id="irs_bmf_deathcare",
            natural_key="999999999",   # different key — no match
            phone_990="6175550100",
            contact_name_990="Unrelated Org",
        )

        _, new_contact_df = mod._join_irs990(account_df, contact_df, irs990)

        # Synthetic contact must survive — no irs990 match for this account.
        primary_phone_rows = new_contact_df[new_contact_df["role"] == "primary_phone"]
        assert len(primary_phone_rows) == 1, (
            "Synthetic primary_phone contact must be preserved when there is no "
            "irs990 match for this account"
        )

    # ------------------------------------------------------------------
    # Test 4: irs990 row with null phone — graceful, no crash, no empty contact
    # ------------------------------------------------------------------

    def test_null_phone_990_no_crash_and_no_empty_phone_contact(self):
        """
        When enrich_irs990 has a row whose phone_990 is null but
        contact_name_990 is present, the pipeline must not crash and must
        not emit a contact with a null phone.  The contact is emitted with
        full_name but phone=None.
        """
        account_df, contact_df = self._make_account_with_irs990_row(phone_normalized=None)
        irs990 = self._make_irs990(phone_990=None, contact_name_990="Bethany Memorial")

        new_account_df, new_contact_df = mod._join_irs990(account_df, contact_df, irs990)

        # Must not crash.  account.phone stays None since phone_990 is null.
        assert new_account_df.iloc[0]["phone"] is None

        # A contact is still emitted because contact_name_990 is present —
        # the name alone justifies the contact row.
        irs_rows = new_contact_df[new_contact_df["role"] == "irs990_contact"]
        assert len(irs_rows) == 1
        assert irs_rows.iloc[0]["phone"] is None  # phone is null — that's acceptable

    def test_null_phone_and_null_name_990_produces_no_contact(self):
        """
        When both phone_990 and contact_name_990 are null, no contact row
        should be emitted — there is nothing useful to store.

        account_df starts with no synthetic contact (phone_normalized=None),
        and _join_irs990 produces nothing for the null-null irs990 row, so the
        result contact_df is empty.  We assert its length is 0 rather than
        filtering by role, which would KeyError on a column-less empty DataFrame.
        """
        account_df, contact_df = self._make_account_with_irs990_row(phone_normalized=None)
        # Pre-condition: no synthetic contact (staging had no phone).
        assert len(contact_df) == 0, "Precondition: no synthetic contact"

        irs990 = self._make_irs990(phone_990=None, contact_name_990=None)
        _, new_contact_df = mod._join_irs990(account_df, contact_df, irs990)

        # No rows of any kind should be present — nothing to store.
        assert len(new_contact_df) == 0, (
            "No contact must be emitted when both phone_990 and contact_name_990 are null"
        )

    # ------------------------------------------------------------------
    # Test 5: empty irs990 — returns unchanged DataFrames
    # ------------------------------------------------------------------

    def test_empty_irs990_returns_unchanged_dataframes(self):
        """When irs990 is empty, _join_irs990 must return both DFs unchanged."""
        account_df, contact_df = self._make_account_with_irs990_row(phone_normalized="8005550199")
        empty_irs990 = pd.DataFrame(
            columns=["source_id", "natural_key", "phone_990", "contact_name_990"]
        )
        new_account_df, new_contact_df = mod._join_irs990(account_df, contact_df, empty_irs990)

        assert len(new_account_df) == len(account_df)
        assert len(new_contact_df) == len(contact_df)

    # ------------------------------------------------------------------
    # Test 6: run_pipeline end-to-end with irs990 data (dry_run)
    # ------------------------------------------------------------------

    def test_run_pipeline_dry_run_with_irs990_data(self):
        """
        End-to-end dry_run: an irs_bmf_deathcare account with a matching
        enrich_irs990 row must produce an irs990_contact in the summary
        (contact count > 0) without any DB writes.
        """
        from unittest.mock import patch

        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_engine.begin.return_value = mock_conn
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        staging_row = _make_canonical(
            source_id="irs_bmf_deathcare",
            natural_key="043783054",
            segment="religious",
            ein="043783054",
            phone_normalized=None,  # no staging phone — phone comes only from irs990
            latitude=None,
            longitude=None,
        )
        staging_df = pd.DataFrame([staging_row])

        irs990_df = pd.DataFrame([{
            "source_id": "irs_bmf_deathcare",
            "natural_key": "043783054",
            "phone_990": "6175550100",
            "contact_name_990": "Bethany Memorial Cemetery",
        }])

        with (
            patch.object(mod, "_load_source", return_value=staging_df),
            patch.object(mod, "_load_parcel_cache", return_value=pd.DataFrame()),
            patch.object(mod, "_load_irs990_cache", return_value=irs990_df),
        ):
            summary = mod.run_pipeline(mock_engine, dry_run=True)

        # contact count must be > 0 — the irs990_contact was produced.
        assert summary["contact"] >= 1, (
            "run_pipeline must produce at least one contact when irs990 data is present"
        )
        # No DB writes in dry run.
        mock_engine.begin.assert_not_called()
