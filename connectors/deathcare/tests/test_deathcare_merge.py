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

import pandas as pd
import pytest

import deathcare_merge as mod


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
        "natural_key":      "default-uuid",
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
