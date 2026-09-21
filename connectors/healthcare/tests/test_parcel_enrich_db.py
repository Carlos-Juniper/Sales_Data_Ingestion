"""
Unit tests for the parcel enrichment DB write path (D16).

Covers:
  - upsert_enrich_parcel: SQL upsert via mocked engine (idempotency, null
    coercion, source_id binding, row-count return)
  - join_parcel: populates maintained_acres from a parcel DataFrame
  - sc_parcel_ingest --write-db with no county CSVs on disk: logs the
    data-blocked warning and does not raise an unhandled exception

All DB calls are mocked — no live Postgres required.
"""

from __future__ import annotations

import logging
import os
import sys
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from healthcare.parcel_acreage_enrich import SOURCE_ID, upsert_enrich_parcel
from healthcare.healthcare_pipeline import join_parcel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine_mock() -> tuple[MagicMock, MagicMock]:
    """Return (engine_mock, conn_mock) wired up for engine.begin() context manager."""
    mock_engine = MagicMock()
    mock_conn = MagicMock()
    mock_engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
    mock_engine.begin.return_value.__exit__ = MagicMock(return_value=False)
    return mock_engine, mock_conn


def _parcel_df(
    natural_keys: list[str],
    maintained_acres: float = 3.5,
    boundary_geojson: str | None = None,
    lookup_status: str = "ok",
) -> pd.DataFrame:
    """Build a minimal enrichment DataFrame shaped like parcel_acreage_enrich.enrich() output."""
    rows = []
    for nk in natural_keys:
        rows.append({
            "natural_key": nk,
            "state": "FL",
            "maintained_acres": maintained_acres,
            "acres_confidence": "estimated",
            "geometry_source": "parcel",
            "boundary_geojson": boundary_geojson,
            "lookup_status": lookup_status,
            "lookup_note": "",
        })
    return pd.DataFrame(rows)


def _location_df(natural_keys: list[str]) -> pd.DataFrame:
    """Build a minimal resolved_location-shaped DataFrame with _natural_key set."""
    rows = []
    for nk in natural_keys:
        rows.append({
            "location_key": f"loc_{nk}",
            "_natural_key": nk,
            "maintained_acres": None,
            "acres_confidence": None,
            "geometry_source": None,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# upsert_enrich_parcel — basic contract
# ---------------------------------------------------------------------------


class TestUpsertEnrichParcel:
    def test_returns_row_count(self):
        """upsert_enrich_parcel must return the count of rows passed to execute."""
        engine, conn = _make_engine_mock()
        enriched = _parcel_df(["FL-CCN-001", "FL-CCN-002"])
        count = upsert_enrich_parcel(engine, SOURCE_ID, enriched)
        assert count == 2

    def test_empty_dataframe_returns_zero_and_does_not_call_execute(self):
        """An empty DataFrame must return 0 without hitting the DB."""
        engine, conn = _make_engine_mock()
        count = upsert_enrich_parcel(engine, SOURCE_ID, pd.DataFrame())
        assert count == 0
        engine.begin.assert_not_called()

    def test_source_id_bound_from_parameter_not_dataframe(self):
        """The source_id in each row dict must come from the caller, not the DataFrame."""
        engine, conn = _make_engine_mock()
        enriched = _parcel_df(["KEY-A"])
        upsert_enrich_parcel(engine, "my_source_id", enriched)

        execute_call = conn.execute.call_args
        rows_arg = execute_call.args[1]
        assert rows_arg[0]["source_id"] == "my_source_id"

    def test_natural_key_cast_to_string(self):
        """natural_key must be a string even when the column contains numeric-like values."""
        engine, conn = _make_engine_mock()
        enriched = _parcel_df(["42"])
        upsert_enrich_parcel(engine, SOURCE_ID, enriched)

        execute_call = conn.execute.call_args
        rows_arg = execute_call.args[1]
        assert rows_arg[0]["natural_key"] == "42"
        assert isinstance(rows_arg[0]["natural_key"], str)

    def test_nan_maintained_acres_coerced_to_none(self):
        """Float NaN in maintained_acres must reach the DB row as None, not NaN."""
        import math

        engine, conn = _make_engine_mock()
        enriched = _parcel_df(["KEY-B"])
        enriched.loc[0, "maintained_acres"] = float("nan")

        upsert_enrich_parcel(engine, SOURCE_ID, enriched)

        execute_call = conn.execute.call_args
        rows_arg = execute_call.args[1]
        val = rows_arg[0]["maintained_acres"]
        # Must not be NaN (math.isnan would blow up on None, which is the correct value).
        assert val is None or (isinstance(val, float) and not math.isnan(val))
        assert val is None

    def test_null_maintained_acres_passes_through_as_none(self):
        """A row with no matching parcel has maintained_acres=None; must land as None."""
        engine, conn = _make_engine_mock()
        enriched = _parcel_df(["KEY-C"], maintained_acres=None, lookup_status="not_found")

        upsert_enrich_parcel(engine, SOURCE_ID, enriched)

        execute_call = conn.execute.call_args
        rows_arg = execute_call.args[1]
        assert rows_arg[0]["maintained_acres"] is None

    def test_null_boundary_geojson_passes_through_as_none(self):
        """Rows without geometry must have boundary_geojson=None in the DB row dict."""
        engine, conn = _make_engine_mock()
        enriched = _parcel_df(["KEY-D"], boundary_geojson=None)

        upsert_enrich_parcel(engine, SOURCE_ID, enriched)

        execute_call = conn.execute.call_args
        rows_arg = execute_call.args[1]
        assert rows_arg[0]["boundary_geojson"] is None

    def test_boundary_geojson_passed_through_when_present(self):
        """A non-null boundary_geojson string must be forwarded to the DB row dict."""
        import json

        engine, conn = _make_engine_mock()
        geojson_str = json.dumps({"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]})
        enriched = _parcel_df(["KEY-E"], boundary_geojson=geojson_str)

        upsert_enrich_parcel(engine, SOURCE_ID, enriched)

        execute_call = conn.execute.call_args
        rows_arg = execute_call.args[1]
        assert rows_arg[0]["boundary_geojson"] == geojson_str


# ---------------------------------------------------------------------------
# upsert_enrich_parcel — idempotency contract
# ---------------------------------------------------------------------------


class TestUpsertEnrichParcelIdempotency:
    """
    Verify the idempotency guarantee: loading the same (source_id, natural_key)
    twice must not raise and the second call must update the row in place.

    This is an SQL-contract test: we verify the generated SQL string contains the
    ON CONFLICT ... DO UPDATE clause so the DB handles idempotency natively.
    We use a real SQLAlchemy text() object inspection rather than a live DB
    because the actual conflict resolution is a Postgres concern, not Python.
    """

    def test_sql_contains_on_conflict_do_update(self):
        """The SQL passed to execute must use ON CONFLICT (source_id, natural_key) DO UPDATE."""
        engine, conn = _make_engine_mock()
        enriched = _parcel_df(["KEY-IDEM"])
        upsert_enrich_parcel(engine, SOURCE_ID, enriched)

        # The first positional arg to execute() is a SQLAlchemy text() object.
        execute_call = conn.execute.call_args
        sql_obj = execute_call.args[0]
        sql_text = str(sql_obj)

        assert "ON CONFLICT" in sql_text
        assert "DO UPDATE" in sql_text

    def test_sql_updates_enriched_at_on_conflict(self):
        """The conflict-update clause must bump enriched_at so cached rows are timestamped."""
        engine, conn = _make_engine_mock()
        enriched = _parcel_df(["KEY-IDEM2"])
        upsert_enrich_parcel(engine, SOURCE_ID, enriched)

        execute_call = conn.execute.call_args
        sql_obj = execute_call.args[0]
        sql_text = str(sql_obj)

        assert "enriched_at" in sql_text

    def test_second_upsert_call_also_returns_row_count(self):
        """
        Simulate two successive calls with the same row; both must return the
        row count.  The mock always succeeds — we're proving no exception is raised
        and the return value is correct, not the DB dedup (that is Postgres's job).
        """
        engine, conn = _make_engine_mock()
        enriched = _parcel_df(["KEY-IDEM3"])

        count_1 = upsert_enrich_parcel(engine, SOURCE_ID, enriched)
        count_2 = upsert_enrich_parcel(engine, SOURCE_ID, enriched)

        assert count_1 == 1
        assert count_2 == 1


# ---------------------------------------------------------------------------
# join_parcel — downstream consumer picks up the loaded row
# ---------------------------------------------------------------------------


class TestJoinParcel:
    """
    join_parcel() is the existing consumer that reads from staging.enrich_parcel.
    These tests verify that once a row is written via upsert_enrich_parcel,
    join_parcel() would correctly populate maintained_acres — using an in-memory
    parcels DataFrame that mirrors what load_parcel_cache() would return from DB.
    """

    def test_join_parcel_populates_maintained_acres_for_matched_natural_key(self):
        """After loading a parcel row, join_parcel must populate maintained_acres."""
        natural_key = "FL-CCN-123"
        parcels = pd.DataFrame([{
            "natural_key": natural_key,
            "maintained_acres": 4.75,
        }])
        location_df = _location_df([natural_key])

        result = join_parcel(location_df, parcels)

        row = result[result["_natural_key"] == natural_key].iloc[0]
        assert row["maintained_acres"] == pytest.approx(4.75)

    def test_join_parcel_sets_acres_confidence_estimated(self):
        """acres_confidence must be 'estimated' for all parcel-sourced rows (D16 constant)."""
        natural_key = "FL-CCN-456"
        parcels = pd.DataFrame([{"natural_key": natural_key, "maintained_acres": 2.0}])
        location_df = _location_df([natural_key])

        result = join_parcel(location_df, parcels)

        row = result[result["_natural_key"] == natural_key].iloc[0]
        assert row["acres_confidence"] == "estimated"

    def test_join_parcel_sets_geometry_source_parcel(self):
        """geometry_source must be 'parcel' when no geocode source is already set."""
        natural_key = "FL-CCN-789"
        parcels = pd.DataFrame([{"natural_key": natural_key, "maintained_acres": 6.0}])
        location_df = _location_df([natural_key])

        result = join_parcel(location_df, parcels)

        row = result[result["_natural_key"] == natural_key].iloc[0]
        assert row["geometry_source"] == "parcel"

    def test_join_parcel_leaves_unmatched_rows_with_none_maintained_acres(self):
        """Rows not in the parcel table must keep maintained_acres=None."""
        parcels = pd.DataFrame([{"natural_key": "FL-CCN-FOUND", "maintained_acres": 5.0}])
        location_df = _location_df(["FL-CCN-FOUND", "FL-CCN-NOT-FOUND"])

        result = join_parcel(location_df, parcels)

        not_found_row = result[result["_natural_key"] == "FL-CCN-NOT-FOUND"].iloc[0]
        assert not_found_row["maintained_acres"] is None

    def test_join_parcel_returns_all_rows_even_when_parcels_empty(self):
        """If the parcel DataFrame is empty, join_parcel returns the location_df unchanged."""
        location_df = _location_df(["KEY-A", "KEY-B"])
        empty_parcels = pd.DataFrame()

        result = join_parcel(location_df, empty_parcels)

        assert len(result) == 2

    def test_join_parcel_full_roundtrip_upsert_then_join(self):
        """
        Simulate the full D16 roundtrip:
          1. Build enrichment output the way parcel_acreage_enrich.enrich() would.
          2. Call upsert_enrich_parcel to confirm the row is built correctly.
          3. Construct an in-memory parcels DataFrame matching what load_parcel_cache
             would return from staging.enrich_parcel.
          4. Call join_parcel and assert maintained_acres is populated.

        This test doesn't hit a live DB — the upsert is mocked and the parcels
        DataFrame is built directly from the enrichment output, mirroring the
        in-memory shape that load_parcel_cache() returns.
        """
        natural_key = "FL-CCN-ROUNDTRIP"
        engine, conn = _make_engine_mock()

        # Step 1+2: Enrich output + upsert (DB mocked).
        enriched = _parcel_df([natural_key], maintained_acres=7.25)
        count = upsert_enrich_parcel(engine, SOURCE_ID, enriched)
        assert count == 1

        # Step 3: Simulate what load_parcel_cache() returns — a slim DataFrame
        # with just natural_key and maintained_acres (the columns join_parcel reads).
        parcels_from_db = pd.DataFrame([{
            "natural_key": natural_key,
            "maintained_acres": 7.25,
        }])

        # Step 4: Verify join_parcel picks it up correctly.
        location_df = _location_df([natural_key])
        result = join_parcel(location_df, parcels_from_db)

        row = result[result["_natural_key"] == natural_key].iloc[0]
        assert row["maintained_acres"] == pytest.approx(7.25)
        assert row["acres_confidence"] == "estimated"
        assert row["geometry_source"] == "parcel"


# ---------------------------------------------------------------------------
# sc_parcel_ingest --write-db with no county CSVs: data-blocked warning
# ---------------------------------------------------------------------------


class TestScParcelIngestDataBlocked:
    """
    Verify that sc_parcel_ingest main() with --write-db and no county CSVs:
      - logs the data-blocked warning at WARNING level
      - does NOT raise an unhandled exception
      - returns cleanly (does not call sys.exit with an error code)
    """

    def _run_main_with_write_db_no_csvs(self, caplog):
        """
        Invoke sc_parcel_ingest.main() with --write-db and no --county-csvs.
        A dummy --locations path is provided; we expect the function to return
        before reading the file (the data-blocked guard fires first).
        """
        import sc_parcel_ingest as spi

        argv = ["sc_parcel_ingest.py", "--locations", "/nonexistent/sc.csv", "--write-db"]
        with patch("sys.argv", argv):
            with caplog.at_level(logging.WARNING, logger="sc_parcel_ingest"):
                # Must return without raising.
                spi.main()

    def test_data_blocked_logs_warning(self, caplog):
        """A WARNING log containing 'data-blocked' must be emitted."""
        self._run_main_with_write_db_no_csvs(caplog)
        assert any("data-blocked" in record.message for record in caplog.records)

    def test_data_blocked_warning_mentions_sc(self, caplog):
        """The warning must mention SC so the reader knows which state is blocked."""
        self._run_main_with_write_db_no_csvs(caplog)
        all_messages = " ".join(r.message for r in caplog.records)
        assert "SC" in all_messages or "sc" in all_messages.lower()

    def test_data_blocked_warning_mentions_county_path(self, caplog):
        """The warning must include a path placeholder so the reader knows what to supply."""
        self._run_main_with_write_db_no_csvs(caplog)
        # The warning should reference assessor path-style tokens.
        all_messages = " ".join(r.message for r in caplog.records)
        assert "assessor" in all_messages.lower() or "path" in all_messages.lower()

    def test_data_blocked_does_not_raise(self, caplog):
        """No unhandled exception must escape — pipeline must not crash."""
        # If main() raises anything, pytest will catch it and fail this test.
        self._run_main_with_write_db_no_csvs(caplog)  # must not raise

    def test_data_blocked_does_not_call_sys_exit(self, caplog):
        """The data-blocked path must return cleanly, not call sys.exit()."""
        import sc_parcel_ingest as spi

        argv = ["sc_parcel_ingest.py", "--locations", "/nonexistent/sc.csv", "--write-db"]
        with patch("sys.argv", argv):
            with patch("sys.exit") as mock_exit:
                with caplog.at_level(logging.WARNING, logger="sc_parcel_ingest"):
                    spi.main()
                # sys.exit must not have been called from the data-blocked branch.
                mock_exit.assert_not_called()
