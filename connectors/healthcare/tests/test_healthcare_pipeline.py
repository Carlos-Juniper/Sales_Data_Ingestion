"""
Unit tests for healthcare_pipeline.py — resolved table building and key computation.

All tests are in-memory; no live DB required. The DB write functions are
tested separately by mocking the engine. This file focuses on the pure
transformations: account_key determinism, location_key / contact_key
derivation, and the resolved DataFrame shapes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from healthcare.healthcare_pipeline import (
    build_resolved_account,
    build_resolved_contact,
    build_resolved_location,
    compute_account_key,
    compute_contact_key,
    compute_location_key,
    join_geocodes,
    join_parcel,
    upsert_resolved_account,
    upsert_resolved_location,
    upsert_resolved_contact,
    run_pipeline,
    _replace_and_upsert,
)
from lib.normalize import normalize_name, normalize_zip


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _merged_row(**overrides) -> dict:
    """Return a minimal survivor row (post-survivorship output shape)."""
    base = {
        "cluster_id": "ccn:ABC123",
        "natural_key": "CMS001",
        "source_id": "cms_general",
        "vertical": "healthcare",
        "account_type": "hospital",
        "name_raw": "General Hospital",
        "name_normalized": "GENERAL HOSPITAL",
        "address_line_1": "100 Main St",
        "city": "Raleigh",
        "site_state": "NC",
        "zip5": "27601",
        "phone": "9195550100",
        "ccn": "ABC123",
        "npi": "",
        "ein": "",
        "latitude": None,
        "longitude": None,
        "size_metric": None,
        "size_value": None,
        "size_unit": None,
        "geocode_precision": None,
        "geocode_source": None,
        "merged_source_ids": "CMS001",
    }
    base.update(overrides)
    return base


def _merged_df(*rows) -> pd.DataFrame:
    """Build a DataFrame from _merged_row() dicts."""
    return pd.DataFrame(list(rows) if rows else [_merged_row()])


# ---------------------------------------------------------------------------
# compute_account_key (D1 priority: ccn > npi > ein > name+zip5)
# ---------------------------------------------------------------------------


class TestComputeAccountKey:
    def _sha(self, text_val: str) -> str:
        return hashlib.sha256(text_val.encode("utf-8")).hexdigest()

    def test_ccn_wins_over_npi_and_ein(self):
        row = _merged_row(ccn="ABC123", npi="1234567890", ein="12-3456789")
        assert compute_account_key(row) == self._sha("ccn:ABC123")

    def test_npi_wins_when_ccn_absent(self):
        row = _merged_row(ccn="", npi="1234567890", ein="12-3456789")
        assert compute_account_key(row) == self._sha("npi:1234567890")

    def test_ein_wins_when_ccn_and_npi_absent(self):
        row = _merged_row(ccn="", npi="", ein="12-3456789")
        assert compute_account_key(row) == self._sha("ein:12-3456789")

    def test_name_zip_fallback_when_all_ids_absent(self):
        row = _merged_row(ccn="", npi="", ein="", name_normalized="GENERAL HOSPITAL", zip5="27601")
        name_norm = normalize_name("GENERAL HOSPITAL")
        zip5 = normalize_zip("27601")
        expected = self._sha(f"name:{name_norm}|zip:{zip5}")
        assert compute_account_key(row) == expected

    def test_same_ccn_always_produces_same_key(self):
        """account_key must be deterministic across two independent calls."""
        row = _merged_row(ccn="XYZ999")
        assert compute_account_key(row) == compute_account_key(row)

    def test_different_ccns_produce_different_keys(self):
        row_a = _merged_row(ccn="AAA000")
        row_b = _merged_row(ccn="BBB111")
        assert compute_account_key(row_a) != compute_account_key(row_b)

    def test_nan_ccn_falls_through_to_npi(self):
        """A float NaN in ccn must not be treated as a real value."""
        import math
        row = _merged_row(ccn=float("nan"), npi="9876543210")
        assert compute_account_key(row) == self._sha("npi:9876543210")


# ---------------------------------------------------------------------------
# compute_location_key
# ---------------------------------------------------------------------------


class TestComputeLocationKey:
    def test_same_inputs_same_key(self):
        row = _merged_row()
        k1 = compute_location_key("acct_key_abc", row)
        k2 = compute_location_key("acct_key_abc", row)
        assert k1 == k2

    def test_different_address_different_key(self):
        row_a = _merged_row(address_line_1="100 Main St", zip5="27601")
        row_b = _merged_row(address_line_1="200 Oak Ave", zip5="27601")
        assert compute_location_key("same_acct", row_a) != compute_location_key("same_acct", row_b)

    def test_different_account_key_different_location_key(self):
        row = _merged_row()
        assert compute_location_key("acct_A", row) != compute_location_key("acct_B", row)


# ---------------------------------------------------------------------------
# compute_contact_key
# ---------------------------------------------------------------------------


class TestComputeContactKey:
    def test_same_inputs_same_key(self):
        k1 = compute_contact_key("acct_key", "primary_phone", "")
        k2 = compute_contact_key("acct_key", "primary_phone", "")
        assert k1 == k2

    def test_different_role_different_key(self):
        k_a = compute_contact_key("acct_key", "primary_phone", "")
        k_b = compute_contact_key("acct_key", "billing_contact", "")
        assert k_a != k_b


# ---------------------------------------------------------------------------
# build_resolved_account
# ---------------------------------------------------------------------------


class TestBuildResolvedAccount:
    def test_returns_one_row_per_input_row(self):
        df = _merged_df(_merged_row(cluster_id="c1"), _merged_row(cluster_id="c2", natural_key="CMS002"))
        result = build_resolved_account(df)
        assert len(result) == 2

    def test_account_key_column_present(self):
        result = build_resolved_account(_merged_df())
        assert "account_key" in result.columns

    def test_account_key_is_hex_string(self):
        result = build_resolved_account(_merged_df())
        key = result.iloc[0]["account_key"]
        assert len(key) == 64  # SHA-256 hex digest
        assert all(c in "0123456789abcdef" for c in key)

    def test_cluster_id_carried_in_private_column(self):
        """_cluster_id must be preserved so build_resolved_location can join."""
        df = _merged_df(_merged_row(cluster_id="ccn:TEST"))
        result = build_resolved_account(df)
        assert "_cluster_id" in result.columns
        assert result.iloc[0]["_cluster_id"] == "ccn:TEST"

    def test_external_keys_contains_ccn_when_present(self):
        row = _merged_row(ccn="ABC123", npi="")
        result = build_resolved_account(_merged_df(row))
        ext = json.loads(result.iloc[0]["external_keys"])
        assert ext.get("ccn") == "ABC123"

    def test_external_keys_is_none_when_no_ids(self):
        row = _merged_row(ccn="", npi="", ein="")
        result = build_resolved_account(_merged_df(row))
        assert result.iloc[0]["external_keys"] is None

    def test_vertical_defaults_to_healthcare(self):
        result = build_resolved_account(_merged_df())
        assert result.iloc[0]["vertical"] == "healthcare"

    def test_status_is_active(self):
        result = build_resolved_account(_merged_df())
        assert result.iloc[0]["status"] == "active"


# ---------------------------------------------------------------------------
# build_resolved_location
# ---------------------------------------------------------------------------


class TestBuildResolvedLocation:
    def _run(self, merged_row_override: dict | None = None):
        row = _merged_row(**(merged_row_override or {}))
        merged = _merged_df(row)
        account_df = build_resolved_account(merged)
        return build_resolved_location(merged, account_df)

    def test_returns_one_row_per_input_cluster(self):
        result = self._run()
        assert len(result) == 1

    def test_location_key_column_present(self):
        result = self._run()
        assert "location_key" in result.columns

    def test_account_key_matches_resolved_account(self):
        row = _merged_row(ccn="XYZ")
        merged = _merged_df(row)
        account_df = build_resolved_account(merged)
        location_df = build_resolved_location(merged, account_df)
        assert location_df.iloc[0]["account_key"] == account_df.iloc[0]["account_key"]

    def test_latitude_longitude_carried_when_present(self):
        result = self._run({"latitude": 35.77, "longitude": -78.63})
        assert result.iloc[0]["_latitude"] == pytest.approx(35.77)
        assert result.iloc[0]["_longitude"] == pytest.approx(-78.63)

    def test_site_address_is_json_string(self):
        result = self._run()
        addr = json.loads(result.iloc[0]["site_address"])
        assert "address_line_1" in addr


# ---------------------------------------------------------------------------
# build_resolved_contact
# ---------------------------------------------------------------------------


class TestBuildResolvedContact:
    def test_row_with_phone_produces_contact(self):
        row = _merged_row(phone="9195550100")
        merged = _merged_df(row)
        account_df = build_resolved_account(merged)
        contact_df = build_resolved_contact(merged, account_df)
        assert len(contact_df) == 1

    def test_row_without_phone_produces_no_contact(self):
        row = _merged_row(phone="")
        merged = _merged_df(row)
        account_df = build_resolved_account(merged)
        contact_df = build_resolved_contact(merged, account_df)
        assert len(contact_df) == 0

    def test_contact_key_column_present(self):
        row = _merged_row(phone="9195550100")
        merged = _merged_df(row)
        account_df = build_resolved_account(merged)
        contact_df = build_resolved_contact(merged, account_df)
        assert "contact_key" in contact_df.columns

    def test_contact_account_key_matches(self):
        row = _merged_row(ccn="CCN_CON", phone="9195550100")
        merged = _merged_df(row)
        account_df = build_resolved_account(merged)
        contact_df = build_resolved_contact(merged, account_df)
        assert contact_df.iloc[0]["account_key"] == account_df.iloc[0]["account_key"]


# ---------------------------------------------------------------------------
# join_geocodes
# ---------------------------------------------------------------------------


class TestJoinGeocodes:
    def test_geocode_applied_to_row_without_lat(self):
        merged = _merged_df(_merged_row(natural_key="A|1", latitude=None))
        geocodes = pd.DataFrame({
            "source_id": ["nppes_practice_locations"],
            "natural_key": ["A|1"],
            "latitude": [25.76],
            "longitude": [-80.19],
            "precision": ["rooftop"],
            "source": ["census"],
            "match_type": ["Exact"],
        })
        result = join_geocodes(merged, geocodes)
        assert result.iloc[0]["latitude"] == pytest.approx(25.76)

    def test_existing_lat_not_overwritten(self):
        """If the row already has latitude (e.g. VA), keep it."""
        merged = _merged_df(_merged_row(natural_key="V1", latitude=28.06, longitude=-82.43))
        geocodes = pd.DataFrame({
            "source_id": ["va_facilities"],
            "natural_key": ["V1"],
            "latitude": [99.0],
            "longitude": [-99.0],
            "precision": ["street"],
            "source": ["census"],
            "match_type": ["Non_Exact"],
        })
        result = join_geocodes(merged, geocodes)
        # Must not overwrite the VA coordinate.
        assert result.iloc[0]["latitude"] == pytest.approx(28.06)

    def test_empty_geocodes_returns_df_unchanged(self):
        merged = _merged_df()
        result = join_geocodes(merged, pd.DataFrame())
        assert len(result) == len(merged)


# ---------------------------------------------------------------------------
# join_parcel
# ---------------------------------------------------------------------------


class TestJoinParcel:
    """
    Tests for join_parcel() — parcel acreage overlay onto resolved_location.

    Strategy: build a location_df with _natural_key set (as build_resolved_location
    does), supply a synthetic parcels DataFrame, and assert the expected columns
    are populated on matching rows and left None on non-matching rows.

    No live DB needed — join_parcel is a pure DataFrame transformation.
    """

    def _make_location_df(self, natural_keys: list[str]) -> pd.DataFrame:
        """Build a minimal resolved_location DataFrame with the given natural_keys."""
        rows = []
        for nk in natural_keys:
            rows.append({
                "location_key": f"loc_{nk}",
                "account_key": f"acct_{nk}",
                "location_name": "Test Facility",
                "site_address": None,
                "_latitude": None,
                "_longitude": None,
                "geocode_precision": None,
                "geometry_source": None,
                "maintained_acres": None,
                "acres_confidence": None,
                "site_type": "hospital",
                "_natural_key": nk,
            })
        return pd.DataFrame(rows)

    def _make_parcel_df(self, records: list[dict]) -> pd.DataFrame:
        """Build a synthetic staging.enrich_parcel DataFrame."""
        return pd.DataFrame(records)

    def test_matching_natural_key_populates_maintained_acres(self):
        """A parcel row whose natural_key matches must populate maintained_acres."""
        location_df = self._make_location_df(["CMS001", "CMS002"])
        parcels = self._make_parcel_df([
            {"source_id": "cms_general", "natural_key": "CMS001", "maintained_acres": 3.75},
        ])
        result = join_parcel(location_df, parcels)
        matched = result[result["_natural_key"] == "CMS001"].iloc[0]
        assert matched["maintained_acres"] == pytest.approx(3.75)
        assert matched["acres_confidence"] == "estimated"
        assert matched["geometry_source"] == "parcel"

    def test_non_matching_natural_key_left_none(self):
        """Rows with no parcel match must keep maintained_acres=None."""
        location_df = self._make_location_df(["CMS001", "CMS002"])
        parcels = self._make_parcel_df([
            {"source_id": "cms_general", "natural_key": "CMS001", "maintained_acres": 3.75},
        ])
        result = join_parcel(location_df, parcels)
        unmatched = result[result["_natural_key"] == "CMS002"].iloc[0]
        assert unmatched["maintained_acres"] is None
        assert unmatched["acres_confidence"] is None

    def test_existing_geometry_source_not_overwritten_by_parcel(self):
        """If geometry_source is already set (from geocode), parcel must not overwrite it."""
        location_df = self._make_location_df(["CMS001"])
        location_df.loc[0, "geometry_source"] = "census"
        parcels = self._make_parcel_df([
            {"source_id": "cms_general", "natural_key": "CMS001", "maintained_acres": 5.0},
        ])
        result = join_parcel(location_df, parcels)
        # geometry_source must stay 'census', not be overwritten by 'parcel'
        assert result.iloc[0]["geometry_source"] == "census"
        # maintained_acres and acres_confidence must still be populated
        assert result.iloc[0]["maintained_acres"] == pytest.approx(5.0)
        assert result.iloc[0]["acres_confidence"] == "estimated"

    def test_empty_parcels_returns_location_df_unchanged(self):
        """When parcels is empty, location_df must be returned as-is."""
        location_df = self._make_location_df(["CMS001"])
        result = join_parcel(location_df, pd.DataFrame())
        assert len(result) == len(location_df)
        assert result.iloc[0]["maintained_acres"] is None

    def test_null_maintained_acres_in_parcel_table_leaves_row_unmatched(self):
        """
        A parcel row whose maintained_acres is None/NaN must not be applied —
        only real numeric values should populate the location row.
        """
        location_df = self._make_location_df(["CMS001"])
        parcels = self._make_parcel_df([
            {"source_id": "cms_general", "natural_key": "CMS001", "maintained_acres": None},
        ])
        result = join_parcel(location_df, parcels)
        assert result.iloc[0]["maintained_acres"] is None

    def test_multiple_rows_only_matching_row_updated(self):
        """With three location rows, only the one with a parcel match gets updated."""
        location_df = self._make_location_df(["A", "B", "C"])
        parcels = self._make_parcel_df([
            {"source_id": "cms_general", "natural_key": "B", "maintained_acres": 10.0},
        ])
        result = join_parcel(location_df, parcels)
        assert result[result["_natural_key"] == "A"].iloc[0]["maintained_acres"] is None
        assert result[result["_natural_key"] == "B"].iloc[0]["maintained_acres"] == pytest.approx(10.0)
        assert result[result["_natural_key"] == "C"].iloc[0]["maintained_acres"] is None

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

        staging_df = pd.DataFrame({
            "source_id": ["cms_general"],
            "natural_key": ["CMS001"],
            "vertical": ["healthcare"],
            "account_type": ["hospital"],
            "name_raw": ["General Hospital"],
            "name_normalized": ["GENERAL HOSPITAL"],
            "address_line_1": ["100 Main St"],
            "city": ["Raleigh"],
            "state": ["NC"],
            "site_state": ["NC"],
            "zip5": ["27601"],
            "phone_raw": ["9195550100"],
            "phone_normalized": ["9195550100"],
            "phone": ["9195550100"],
            "ccn": [""],
            "npi": [""],
            "latitude": [None],
            "longitude": [None],
            "segment": [None],
            "ein": [None],
            "county_fips": [None],
            "size_metric": [None],
            "size_value": [None],
            "size_unit": [None],
            "source_file": ["xubh-q36u"],
        })
        parcel_df = pd.DataFrame({
            "source_id": ["cms_general"],
            "natural_key": ["CMS001"],
            "maintained_acres": [7.5],
        })

        with (
            patch("healthcare.healthcare_pipeline.load_all_sources", return_value=staging_df),
            patch("healthcare.healthcare_pipeline.load_geocode_cache",
                  return_value=pd.DataFrame(columns=["source_id", "natural_key", "latitude",
                                                      "longitude", "precision", "source", "match_type"])),
            patch("healthcare.healthcare_pipeline.load_parcel_cache", return_value=parcel_df),
        ):
            summary = run_pipeline(mock_engine, dry_run=True)

        # No DB writes in dry run
        mock_engine.begin.assert_not_called()
        assert "location" in summary


# ---------------------------------------------------------------------------
# upsert_resolved_account / location / contact — mock DB
# ---------------------------------------------------------------------------


class TestUpsertResolvedAccount:
    def _make_engine(self):
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.begin.return_value.__exit__ = MagicMock(return_value=False)
        return mock_engine, mock_conn

    def test_returns_row_count(self):
        engine, _ = self._make_engine()
        account_df = build_resolved_account(_merged_df(_merged_row(), _merged_row(cluster_id="c2", natural_key="B")))
        count = upsert_resolved_account(engine, account_df)
        assert count == 2

    def test_empty_df_returns_zero_no_db_call(self):
        engine, _ = self._make_engine()
        count = upsert_resolved_account(engine, pd.DataFrame())
        assert count == 0
        engine.begin.assert_not_called()


class TestUpsertResolvedLocation:
    def _make_engine(self):
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.begin.return_value.__exit__ = MagicMock(return_value=False)
        return mock_engine, mock_conn

    def test_returns_row_count(self):
        engine, _ = self._make_engine()
        merged = _merged_df()
        account_df = build_resolved_account(merged)
        location_df = build_resolved_location(merged, account_df)
        count = upsert_resolved_location(engine, location_df)
        assert count == 1

    def test_empty_df_returns_zero(self):
        engine, _ = self._make_engine()
        count = upsert_resolved_location(engine, pd.DataFrame())
        assert count == 0


class TestUpsertResolvedContact:
    def _make_engine(self):
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.begin.return_value.__exit__ = MagicMock(return_value=False)
        return mock_engine, mock_conn

    def test_returns_row_count(self):
        engine, _ = self._make_engine()
        merged = _merged_df(_merged_row(phone="9195550100"))
        account_df = build_resolved_account(merged)
        contact_df = build_resolved_contact(merged, account_df)
        count = upsert_resolved_contact(engine, contact_df)
        assert count == 1


# ---------------------------------------------------------------------------
# run_pipeline — smoke test with all DB calls mocked
# ---------------------------------------------------------------------------


class TestRunPipeline:
    def _make_engine(self):
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.begin.return_value.__exit__ = MagicMock(return_value=False)
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        return mock_engine, mock_conn

    def _staging_df(self):
        """
        DataFrame shaped like the output of load_all_sources() — includes all
        columns that merge_all() / prepare() expect, including the aliases that
        load_all_sources() adds (phone, site_state, ccn, npi).
        """
        return pd.DataFrame({
            "source_id": ["cms_general"],
            "natural_key": ["CMS001"],
            "vertical": ["healthcare"],
            "account_type": ["hospital"],
            "name_raw": ["General Hospital"],
            "name_normalized": ["GENERAL HOSPITAL"],
            "address_line_1": ["100 Main St"],
            "city": ["Raleigh"],
            # load_all_sources aliases: state → site_state, phone_normalized → phone
            "state": ["NC"],
            "site_state": ["NC"],
            "zip5": ["27601"],
            "phone_raw": ["9195550100"],
            "phone_normalized": ["9195550100"],
            "phone": ["9195550100"],       # alias added by load_all_sources
            "ccn": [""],                   # default added by load_all_sources
            "npi": [""],                   # default added by load_all_sources
            "latitude": [None],
            "longitude": [None],
            "segment": [None],
            "ein": [None],
            "county_fips": [None],
            "size_metric": [None],
            "size_value": [None],
            "size_unit": [None],
            "source_file": ["xubh-q36u"],
        })

    def test_dry_run_returns_summary_without_db_writes(self):
        """dry_run=True must return a summary dict without calling engine.begin."""
        engine, mock_conn = self._make_engine()
        staging_df = self._staging_df()
        geocode_df = pd.DataFrame(columns=[
            "source_id", "natural_key", "latitude", "longitude",
            "precision", "source", "match_type",
        ])

        with (
            patch("healthcare.healthcare_pipeline.load_all_sources", return_value=staging_df),
            patch("healthcare.healthcare_pipeline.load_geocode_cache", return_value=geocode_df),
        ):
            summary = run_pipeline(engine, dry_run=True)

        assert "account" in summary
        assert "location" in summary
        assert "contact" in summary
        # No writes in dry run
        engine.begin.assert_not_called()

    def test_pipeline_produces_nonzero_account_count(self):
        """A single staging row must produce at least one resolved_account."""
        engine, mock_conn = self._make_engine()
        staging_df = self._staging_df()
        geocode_df = pd.DataFrame(columns=[
            "source_id", "natural_key", "latitude", "longitude",
            "precision", "source", "match_type",
        ])

        with (
            patch("healthcare.healthcare_pipeline.load_all_sources", return_value=staging_df),
            patch("healthcare.healthcare_pipeline.load_geocode_cache", return_value=geocode_df),
        ):
            summary = run_pipeline(engine, dry_run=True)

        assert summary["account"] >= 1

    def test_empty_sources_returns_zero_counts(self):
        """When no staging data is found, pipeline must return zero counts."""
        engine, _ = self._make_engine()

        with (
            patch("healthcare.healthcare_pipeline.load_all_sources", return_value=pd.DataFrame()),
            patch("healthcare.healthcare_pipeline.load_geocode_cache",
                  return_value=pd.DataFrame(columns=["source_id", "natural_key", "latitude",
                                                      "longitude", "precision", "source", "match_type"])),
        ):
            summary = run_pipeline(engine, dry_run=True)

        assert summary["account"] == 0
        assert summary["location"] == 0
        assert summary["contact"] == 0


# ---------------------------------------------------------------------------
# Tier-3 review-queue engine wiring (§4.2 fix)
# ---------------------------------------------------------------------------


class TestTier3ReviewQueueWiring:
    """
    Verify that run_pipeline() passes the engine into merge_all() on a real run
    so Tier-3 uncertain pairs land in review.pending_pairs, and passes None on
    dry-run so the queue is never written.

    Strategy: patch merge_all to return a synthetic review_queue_df containing
    one in-band pair, and patch enqueue_tier3_matches (the function merge_all
    calls internally) to capture whether it was invoked.  This tests the
    engine-threading wire without re-testing merge logic.
    """

    def _make_engine(self):
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.begin.return_value.__exit__ = MagicMock(return_value=False)
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        return mock_engine, mock_conn

    def _staging_df(self):
        """Minimal single-row staging DataFrame sufficient for pipeline to run."""
        return pd.DataFrame({
            "source_id": ["cms_general"],
            "natural_key": ["CMS001"],
            "vertical": ["healthcare"],
            "account_type": ["hospital"],
            "name_raw": ["General Hospital"],
            "name_normalized": ["GENERAL HOSPITAL"],
            "address_line_1": ["100 Main St"],
            "city": ["Raleigh"],
            "state": ["NC"],
            "site_state": ["NC"],
            "zip5": ["27601"],
            "phone_raw": ["9195550100"],
            "phone_normalized": ["9195550100"],
            "phone": ["9195550100"],
            "ccn": [""],
            "npi": [""],
            "latitude": [None],
            "longitude": [None],
            "segment": [None],
            "ein": [None],
            "county_fips": [None],
            "size_metric": [None],
            "size_value": [None],
            "size_unit": [None],
            "source_file": ["xubh-q36u"],
        })

    def _synthetic_review_queue(self) -> pd.DataFrame:
        """One uncertain pair scored in the 0.75–0.92 band."""
        return pd.DataFrame({
            "key_a": ["CMS001"],
            "key_b": ["NPPES001"],
            "score": [0.83],
            "source_a": ["cms_general"],
            "source_b": ["nppes_practice_locations"],
        })

    def _canonical_df(self) -> pd.DataFrame:
        """Minimal one-row canonical output (post-survivorship shape)."""
        return pd.DataFrame({
            "cluster_id": ["src:cms_general:CMS001"],
            "natural_key": ["CMS001"],
            "source_id": ["cms_general"],
            "vertical": ["healthcare"],
            "account_type": ["hospital"],
            "name_raw": ["General Hospital"],
            "name_normalized": ["GENERAL HOSPITAL"],
            "address_line_1": ["100 Main St"],
            "city": ["Raleigh"],
            "site_state": ["NC"],
            "zip5": ["27601"],
            "phone": ["9195550100"],
            "ccn": [""],
            "npi": [""],
            "ein": [None],
            "latitude": [None],
            "longitude": [None],
            "size_metric": [None],
            "size_value": [None],
            "size_unit": [None],
            "merged_source_ids": ["CMS001"],
        })

    def _empty_geocode_df(self) -> pd.DataFrame:
        return pd.DataFrame(columns=[
            "source_id", "natural_key", "latitude", "longitude",
            "precision", "source", "match_type",
        ])

    def test_real_run_passes_engine_to_merge_all(self):
        """
        On a real run (dry_run=False), merge_all() must receive the live engine
        so enqueue_tier3_matches() is called for in-band review pairs.
        """
        engine, _ = self._make_engine()
        canonical = self._canonical_df()
        review_queue = self._synthetic_review_queue()
        staging_df = self._staging_df()

        # Capture what engine argument reaches merge_all.
        captured_engine = {}

        def fake_merge_all(df, *, engine=None):
            captured_engine["engine"] = engine
            return canonical, review_queue

        with (
            patch("healthcare.healthcare_pipeline.load_all_sources", return_value=staging_df),
            patch("healthcare.healthcare_pipeline.load_geocode_cache", return_value=self._empty_geocode_df()),
            patch("healthcare.healthcare_pipeline.merge_all", side_effect=fake_merge_all),
        ):
            run_pipeline(engine, dry_run=False)

        # The live engine — not None — must reach merge_all on a real run.
        assert captured_engine["engine"] is engine, (
            "merge_all() received None instead of the live engine on a real run; "
            "Tier-3 pairs will never be enqueued into review.pending_pairs"
        )

    def test_dry_run_passes_none_to_merge_all(self):
        """
        On dry_run=True, merge_all() must receive engine=None so it does not
        write to review.pending_pairs.
        """
        engine, _ = self._make_engine()
        canonical = self._canonical_df()
        review_queue = self._synthetic_review_queue()
        staging_df = self._staging_df()

        captured_engine = {}

        def fake_merge_all(df, *, engine=None):
            captured_engine["engine"] = engine
            return canonical, review_queue

        with (
            patch("healthcare.healthcare_pipeline.load_all_sources", return_value=staging_df),
            patch("healthcare.healthcare_pipeline.load_geocode_cache", return_value=self._empty_geocode_df()),
            patch("healthcare.healthcare_pipeline.merge_all", side_effect=fake_merge_all),
        ):
            run_pipeline(engine, dry_run=True)

        assert captured_engine["engine"] is None, (
            "merge_all() received a live engine on dry_run=True; "
            "review.pending_pairs would be written during a dry run"
        )

    def test_real_run_enqueue_called_with_in_band_pair(self):
        """
        End-to-end: on a real run with a review_queue containing an in-band pair,
        enqueue_tier3_matches() must be called with that pair.

        Patches enqueue_tier3_matches at its call site in healthcare_merge so we
        intercept without needing a live DB.
        """
        engine, _ = self._make_engine()
        canonical = self._canonical_df()
        review_queue = self._synthetic_review_queue()
        staging_df = self._staging_df()

        with (
            patch("healthcare.healthcare_pipeline.load_all_sources", return_value=staging_df),
            patch("healthcare.healthcare_pipeline.load_geocode_cache", return_value=self._empty_geocode_df()),
            # Let merge_all run its real logic but inject a controlled review_queue
            # by patching tier3_fuzzy_merge so it returns our in-band pair without
            # needing real data that would produce a fuzzy match.
            patch(
                "healthcare.healthcare_merge.tier3_fuzzy_merge",
                return_value=(canonical, review_queue),
            ),
            patch(
                "healthcare.healthcare_merge.enqueue_tier3_matches",
                return_value=1,
            ) as mock_enqueue,
        ):
            run_pipeline(engine, dry_run=False)

        # enqueue_tier3_matches must have been called exactly once.
        assert mock_enqueue.call_count == 1, (
            f"enqueue_tier3_matches called {mock_enqueue.call_count} times; expected 1"
        )
        # The pairs passed must contain our synthetic in-band pair.
        call_args = mock_enqueue.call_args
        pairs_arg = call_args.args[1] if call_args.args else call_args.kwargs.get("matches", [])
        assert any(
            p.get("key_a") == "CMS001" and p.get("key_b") == "NPPES001"
            for p in pairs_arg
        ), f"In-band pair not found in enqueue call args: {pairs_arg}"

    def test_dry_run_enqueue_never_called(self):
        """
        On dry_run=True, enqueue_tier3_matches() must NOT be called even when
        review_queue contains in-band pairs.
        """
        engine, _ = self._make_engine()
        canonical = self._canonical_df()
        review_queue = self._synthetic_review_queue()
        staging_df = self._staging_df()

        with (
            patch("healthcare.healthcare_pipeline.load_all_sources", return_value=staging_df),
            patch("healthcare.healthcare_pipeline.load_geocode_cache", return_value=self._empty_geocode_df()),
            patch(
                "healthcare.healthcare_merge.tier3_fuzzy_merge",
                return_value=(canonical, review_queue),
            ),
            patch(
                "healthcare.healthcare_merge.enqueue_tier3_matches",
                return_value=1,
            ) as mock_enqueue,
        ):
            run_pipeline(engine, dry_run=True)

        mock_enqueue.assert_not_called()


# ---------------------------------------------------------------------------
# D15: _replace_and_upsert — vertical-scoped delete-before-upsert semantics
# ---------------------------------------------------------------------------


class TestReplaceAndUpsert:
    """
    Verify _replace_and_upsert() (D15 fix) has the correct delete-before-upsert
    behaviour when called via a mocked engine.

    Strategy: intercept all conn.execute() calls in order to inspect the SQL
    that is sent.  The engine mock uses a context-manager-compatible connection
    so with engine.begin() as conn works without a live DB.

    Three properties are asserted:
      1. Out-of-vertical stale rows are deleted (DELETE WHERE vertical='healthcare').
      2. Other-vertical rows are untouched (DELETE never issues vertical='deathcare').
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
        executed: list[tuple] = []

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.side_effect = lambda sql, params=None: executed.append(
            (str(sql), params)
        )

        mock_engine = MagicMock()
        mock_engine.begin.return_value = mock_conn

        return mock_engine, executed

    def _minimal_account_df(self) -> pd.DataFrame:
        """One resolved_account row with the minimum columns _replace_and_upsert needs."""
        return build_resolved_account(_merged_df(_merged_row()))

    def _minimal_location_df(self) -> pd.DataFrame:
        merged = _merged_df(_merged_row())
        account_df = build_resolved_account(merged)
        return build_resolved_location(merged, account_df)

    def _minimal_contact_df(self) -> pd.DataFrame:
        merged = _merged_df(_merged_row(phone="9195550100"))
        account_df = build_resolved_account(merged)
        return build_resolved_contact(merged, account_df)

    # ------------------------------------------------------------------
    # Test 1: stale out-of-vertical rows are deleted
    # ------------------------------------------------------------------

    def test_delete_fires_for_own_vertical(self):
        """
        _replace_and_upsert must issue DELETE ... WHERE vertical='healthcare' for
        all three resolved tables.  This is the core D15 guarantee: stale rows
        from a prior nationwide run are swept out before the new slice is inserted.
        """
        engine, executed = self._make_engine_capturing_sql()

        _replace_and_upsert(
            engine,
            self._minimal_account_df(),
            self._minimal_location_df(),
            self._minimal_contact_df(),
            vertical="healthcare",
        )

        # Collect SQL strings that are DELETE statements.
        delete_stmts = [sql for sql, _ in executed if "DELETE" in sql.upper()]

        assert len(delete_stmts) == 3, (
            f"Expected 3 DELETE statements (account, location, contact), "
            f"got {len(delete_stmts)}: {delete_stmts}"
        )

        # Each delete must target vertical='healthcare' via the :v parameter.
        for sql, params in executed:
            if "DELETE" in sql.upper():
                assert params == {"v": "healthcare"}, (
                    f"DELETE params expected {{'v': 'healthcare'}}, got {params!r}"
                )

    # ------------------------------------------------------------------
    # Test 2: other-vertical rows are untouched
    # ------------------------------------------------------------------

    def test_other_vertical_never_deleted(self):
        """
        When vertical='healthcare', the deletes must never reference 'deathcare'.
        This ensures that running the healthcare driver does not wipe deathcare rows.
        """
        engine, executed = self._make_engine_capturing_sql()

        _replace_and_upsert(
            engine,
            self._minimal_account_df(),
            self._minimal_location_df(),
            self._minimal_contact_df(),
            vertical="healthcare",
        )

        for sql, params in executed:
            if "DELETE" in sql.upper():
                assert (params or {}).get("v") != "deathcare", (
                    "DELETE was issued with vertical='deathcare' during a "
                    "healthcare _replace_and_upsert call — this would wipe the "
                    "other vertical's data."
                )

    # ------------------------------------------------------------------
    # Test 3: dry_run=True — deletes never fire
    # ------------------------------------------------------------------

    def test_dry_run_pipeline_issues_no_deletes(self):
        """
        When run_pipeline() is called with dry_run=True, engine.begin() must
        never be called — so no DELETE statements can reach the DB.

        This uses the full run_pipeline() entry point rather than calling
        _replace_and_upsert() directly, because dry_run gating lives in
        run_pipeline().
        """
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_engine.begin.return_value = mock_conn
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        staging_df = pd.DataFrame({
            "source_id": ["cms_general"],
            "natural_key": ["CMS001"],
            "vertical": ["healthcare"],
            "account_type": ["hospital"],
            "name_raw": ["General Hospital"],
            "name_normalized": ["GENERAL HOSPITAL"],
            "address_line_1": ["100 Main St"],
            "city": ["Raleigh"],
            "state": ["NC"],
            "site_state": ["NC"],
            "zip5": ["27601"],
            "phone_raw": ["9195550100"],
            "phone_normalized": ["9195550100"],
            "phone": ["9195550100"],
            "ccn": [""],
            "npi": [""],
            "latitude": [None],
            "longitude": [None],
            "segment": [None],
            "ein": [None],
            "county_fips": [None],
            "size_metric": [None],
            "size_value": [None],
            "size_unit": [None],
            "source_file": ["xubh-q36u"],
        })

        with (
            patch("healthcare.healthcare_pipeline.load_all_sources", return_value=staging_df),
            patch("healthcare.healthcare_pipeline.load_geocode_cache",
                  return_value=pd.DataFrame(columns=[
                      "source_id", "natural_key", "latitude", "longitude",
                      "precision", "source", "match_type",
                  ])),
            patch("healthcare.healthcare_pipeline.load_parcel_cache",
                  return_value=pd.DataFrame()),
        ):
            run_pipeline(mock_engine, dry_run=True)

        # dry_run=True must not open a write transaction at all.
        mock_engine.begin.assert_not_called()


# ---------------------------------------------------------------------------
# D17a: Phone carry — survivorship coalesce and contact/account wiring
# ---------------------------------------------------------------------------


class TestD17aPhoneCarry:
    """
    Verify that phone_normalized survives the survivorship merge and
    reaches both resolved_account.phone and resolved_contact.phone.

    These tests work at the build_resolved_* layer (post-survivorship shape)
    because that is where the wiring gap from staging to resolved tables lives.
    The merged DataFrame passed in represents what survivorship produces; we
    assert the resolved outputs carry phone correctly.
    """

    def test_survivor_with_phone_populates_account_phone(self):
        """Survivor row carrying phone lands in resolved_account.phone."""
        row = _merged_row(phone="9195550199")
        merged = _merged_df(row)
        account_df = build_resolved_account(merged)
        assert account_df.iloc[0]["phone"] == "9195550199"

    def test_survivor_with_phone_produces_contact_with_phone(self):
        """Survivor row carrying phone produces a resolved_contact with that phone."""
        row = _merged_row(phone="9195550199")
        merged = _merged_df(row)
        account_df = build_resolved_account(merged)
        contact_df = build_resolved_contact(merged, account_df)
        assert len(contact_df) == 1
        assert contact_df.iloc[0]["phone"] == "9195550199"

    def test_cluster_phone_coalesced_via_survivorship(self):
        """
        Cluster where the surviving row (highest-priority source) lacks a phone
        but another member has one: after survivorship coalesces phone across
        the cluster, the resolved_contact must carry the non-null phone.

        Simulates the survivorship output directly — in the real pipeline,
        healthcare_merge.survivorship() iterates ranked members and picks the
        first non-empty phone from any cluster member.  We verify that when
        survivorship has done its job correctly, the resolved builders honour it.
        """
        # Survivorship already ran: the winner has phone filled from a lower-ranked
        # member.  This is the state build_resolved_account / build_resolved_contact
        # receive.  Passing phone="8135550177" simulates a cluster where survivorship
        # coalesced the phone from a secondary source row.
        row = _merged_row(
            cluster_id="ccn:COALESCE",
            ccn="COALESCE",
            phone="8135550177",   # coalesced from another cluster member
        )
        merged = _merged_df(row)
        account_df = build_resolved_account(merged)
        contact_df = build_resolved_contact(merged, account_df)

        # Both resolved tables must carry the coalesced phone.
        assert account_df.iloc[0]["phone"] == "8135550177", (
            "resolved_account.phone must be populated from the coalesced survivor phone"
        )
        assert len(contact_df) == 1, "A contact must be emitted when phone is present"
        assert contact_df.iloc[0]["phone"] == "8135550177", (
            "resolved_contact.phone must equal the coalesced survivor phone"
        )
        assert contact_df.iloc[0]["account_key"] == account_df.iloc[0]["account_key"], (
            "contact must link to the same account"
        )

    def test_cluster_with_no_phone_anywhere_produces_no_contact(self):
        """
        When no cluster member has a phone, build_resolved_contact must produce
        zero rows — no empty-phone contact is ever emitted.
        """
        row = _merged_row(phone="")
        merged = _merged_df(row)
        account_df = build_resolved_account(merged)
        contact_df = build_resolved_contact(merged, account_df)

        assert len(contact_df) == 0, (
            "No contact must be emitted when phone is absent for the entire cluster"
        )
        # account_df is still produced (account exists, just unreachable by phone)
        assert len(account_df) == 1

    def test_none_phone_produces_no_contact(self):
        """phone=None (not just empty string) must also yield no contact."""
        row = _merged_row(phone=None)
        merged = _merged_df(row)
        account_df = build_resolved_account(merged)
        contact_df = build_resolved_contact(merged, account_df)
        assert len(contact_df) == 0

    def test_account_phone_is_none_when_no_phone(self):
        """resolved_account.phone must be None (not empty string) when phone absent."""
        row = _merged_row(phone="")
        merged = _merged_df(row)
        account_df = build_resolved_account(merged)
        assert account_df.iloc[0]["phone"] is None

    def test_multiple_clusters_only_phoned_ones_get_contacts(self):
        """
        Two clusters: one with phone, one without.  Only the phoned cluster
        produces a contact row — the other gets an account but no contact.
        """
        row_with_phone = _merged_row(
            cluster_id="ccn:PHONED",
            ccn="PHONED",
            natural_key="NP001",
            phone="7045550188",
        )
        row_no_phone = _merged_row(
            cluster_id="ccn:SILENT",
            ccn="SILENT",
            natural_key="NP002",
            phone="",
        )
        merged = _merged_df(row_with_phone, row_no_phone)
        account_df = build_resolved_account(merged)
        contact_df = build_resolved_contact(merged, account_df)

        assert len(account_df) == 2, "Both clusters produce accounts"
        assert len(contact_df) == 1, "Only the phoned cluster produces a contact"
        assert contact_df.iloc[0]["phone"] == "7045550188"

    def test_contact_role_is_primary_phone(self):
        """Healthcare contacts always use role='primary_phone'."""
        row = _merged_row(phone="9195550100")
        merged = _merged_df(row)
        account_df = build_resolved_account(merged)
        contact_df = build_resolved_contact(merged, account_df)
        assert contact_df.iloc[0]["role"] == "primary_phone"
