"""
Tests for lib.resolved_writer and lib.db.upsert_boundaries.

Engine is mocked throughout — these assert the SQL contract and the value
conversions, not Postgres behaviour.  Live-DB coverage lives in
connectors/tests/integration/.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from lib.db import SQM_PER_ACRE, upsert_boundaries  # noqa: E402
from lib.resolved_writer import _to_rows, replace_and_upsert  # noqa: E402


def _make_engine_mock() -> tuple[MagicMock, MagicMock]:
    """Return (engine, conn) wired for both begin() and connect() context use."""
    engine, conn = MagicMock(), MagicMock()
    engine.begin.return_value.__enter__ = MagicMock(return_value=conn)
    engine.begin.return_value.__exit__ = MagicMock(return_value=False)
    engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
    engine.connect.return_value.__exit__ = MagicMock(return_value=False)
    return engine, conn


def _capture(conn) -> list[tuple[str, object]]:
    """Collect every (sql_text, params) pair executed on the mock connection."""
    calls = []

    def _side_effect(sql, params=None):
        calls.append((str(sql), params))
        return MagicMock()

    conn.execute.side_effect = _side_effect
    return calls


def _account_df(**overrides) -> pd.DataFrame:
    row = {
        "account_key": "A1", "vertical": "parks", "account_type": "municipality",
        "legal_name": "Cary town", "name_normalized": "CARY", "dba_name": None,
        "parent_account_key": None, "mailing_address": None, "phone": None,
        "email": None, "website": None, "status": "active", "external_keys": None,
        "size_metric": 100.0, "size_metric_unit": "acres", "confidence": None,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def _location_df(**overrides) -> pd.DataFrame:
    row = {
        "location_key": "L1", "account_key": "A1", "vertical": "parks",
        "location_name": "Bond Park", "site_address": None,
        "_latitude": 35.0, "_longitude": -78.0, "_boundary_geojson": None,
        "geocode_precision": None, "geometry_source": "padus",
        "maintained_acres": 310.0, "acres_confidence": "measured",
        "site_type": "park",
    }
    row.update(overrides)
    return pd.DataFrame([row])


class TestToRows:
    def test_nan_numeric_becomes_none(self):
        """Postgres numeric accepts literal NaN, so an unscrubbed NaN lands in
        core.* as NaN rather than NULL — no error, just a poisoned value."""
        df = _account_df(size_metric=float("nan"))
        assert _to_rows(df, "resolved_account", ())[0]["size_metric"] is None

    def test_pd_na_becomes_none(self):
        """psycopg cannot adapt pandas' NA sentinel."""
        df = _account_df(legal_name=pd.NA)
        assert _to_rows(df, "resolved_account", ())[0]["legal_name"] is None

    def test_private_columns_dropped(self):
        df = _account_df()
        df["_source_id"] = "x"
        rows = _to_rows(df, "resolved_account", ("_source_id",))
        assert "_source_id" not in rows[0]

    def test_empty_frame(self):
        assert _to_rows(pd.DataFrame(), "resolved_account", ()) == []

    def test_real_values_untouched(self):
        assert _to_rows(_account_df(), "resolved_account", ())[0]["size_metric"] == 100.0


class TestReplaceAndUpsert:
    def test_deletes_are_scoped_to_the_vertical(self):
        """These tables are shared, so an unscoped delete would wipe a sibling
        vertical's rows."""
        engine, conn = _make_engine_mock()
        calls = _capture(conn)
        replace_and_upsert(engine, _account_df(), _location_df(),
                           pd.DataFrame(), vertical="parks")
        deletes = [(s, p) for s, p in calls if s.strip().upper().startswith("DELETE")]
        assert len(deletes) == 3
        for _, params in deletes:
            assert params == {"v": "parks"}

    def test_never_touches_another_vertical(self):
        engine, conn = _make_engine_mock()
        calls = _capture(conn)
        replace_and_upsert(engine, _account_df(), _location_df(),
                           pd.DataFrame(), vertical="parks")
        assert not any("healthcare" in str(p) for _, p in calls)
        assert not any("deathcare" in str(p) for _, p in calls)

    def test_all_writes_share_one_transaction(self):
        """D15: a delete in its own transaction would leave the table empty on a
        mid-run failure, and the next core diff would tombstone the vertical."""
        engine, _ = _make_engine_mock()
        replace_and_upsert(engine, _account_df(), _location_df(),
                           pd.DataFrame(), vertical="parks")
        assert engine.begin.call_count == 1

    def test_children_deleted_before_parents(self):
        engine, conn = _make_engine_mock()
        calls = _capture(conn)
        replace_and_upsert(engine, _account_df(), _location_df(),
                           pd.DataFrame(), vertical="parks")
        order = [s for s, _ in calls if s.strip().upper().startswith("DELETE")]
        assert "resolved_contact" in order[0]
        assert "resolved_location" in order[1]
        assert "resolved_account" in order[2]

    def test_returns_written_counts(self):
        engine, _ = _make_engine_mock()
        counts = replace_and_upsert(engine, _account_df(), _location_df(),
                                    pd.DataFrame(), vertical="parks")
        assert counts == (1, 1, 0)

    def test_empty_frames_still_delete(self):
        """A vertical whose sources all went empty must have its stale rows
        removed, not silently retained."""
        engine, conn = _make_engine_mock()
        calls = _capture(conn)
        counts = replace_and_upsert(engine, pd.DataFrame(), pd.DataFrame(),
                                    pd.DataFrame(), vertical="parks")
        assert counts == (0, 0, 0)
        assert len([s for s, _ in calls if s.strip().upper().startswith("DELETE")]) == 3

    def test_location_sql_builds_geom_and_boundary(self):
        engine, conn = _make_engine_mock()
        calls = _capture(conn)
        replace_and_upsert(engine, _account_df(),
                           _location_df(_boundary_geojson='{"type":"Polygon"}'),
                           pd.DataFrame(), vertical="parks")
        loc_sql = next(s for s, _ in calls if "resolved_location" in s and "INSERT" in s)
        assert "ST_MakePoint" in loc_sql
        assert "ST_GeomFromGeoJSON" in loc_sql
        assert "ST_Multi" in loc_sql

    def test_latitude_cast_consistently(self):
        """Reusing a named parameter at two different deduced types raises
        'inconsistent types deduced for parameter'."""
        engine, conn = _make_engine_mock()
        calls = _capture(conn)
        replace_and_upsert(engine, _account_df(), _location_df(),
                           pd.DataFrame(), vertical="parks")
        loc_sql = next(s for s, _ in calls if "resolved_location" in s and "INSERT" in s)
        # Collapse whitespace: the SQL aligns the two CAST expressions for
        # readability, so an exact-substring assertion would be testing layout.
        flat = " ".join(loc_sql.split())
        assert "CAST(:_latitude AS numeric)::double precision" in flat
        assert "CAST(:_longitude AS numeric)::double precision" in flat

    def test_boundary_defaults_to_null_when_absent(self):
        """Callers with point-only locations need not carry the column."""
        engine, conn = _make_engine_mock()
        calls = _capture(conn)
        df = _location_df().drop(columns=["_boundary_geojson"])
        replace_and_upsert(engine, _account_df(), df, pd.DataFrame(), vertical="parks")
        params = next(p for s, p in calls if "resolved_location" in s and "INSERT" in s)
        assert params[0]["_boundary_geojson"] is None

    def test_upserts_are_idempotent(self):
        engine, conn = _make_engine_mock()
        calls = _capture(conn)
        replace_and_upsert(engine, _account_df(), _location_df(),
                           pd.DataFrame(), vertical="parks")
        for table in ("resolved_account", "resolved_location"):
            sql = next(s for s, _ in calls if table in s and "INSERT" in s)
            assert "ON CONFLICT" in sql


class TestUpsertBoundaries:
    def _rows(self, n=1, geojson='{"type":"Polygon","coordinates":[]}'):
        return [
            {"source_id": "tiger_places", "natural_key": f"K{i}", "geojson": geojson}
            for i in range(n)
        ]

    def test_area_measured_from_unsimplified_geometry(self):
        """Simplification is applied only to what gets stored, so the acreage
        figure stays an honest independent check."""
        engine, conn = _make_engine_mock()
        calls = _capture(conn)
        upsert_boundaries(engine, "park_attrs", self._rows(),
                          area_col="acres_computed")
        sql = calls[0][0]
        assert "ST_Area" in sql
        assert "::geography" in sql
        assert str(SQM_PER_ACRE) in sql
        # The area expression must not be wrapped in the simplifier.
        area_expr = sql.split("ST_Area(")[1].split("END")[0]
        assert "ST_SimplifyPreserveTopology" not in area_expr

    def test_stored_geometry_is_simplified_and_multi(self):
        engine, conn = _make_engine_mock()
        calls = _capture(conn)
        upsert_boundaries(engine, "park_attrs", self._rows())
        sql = calls[0][0]
        assert "ST_SimplifyPreserveTopology" in sql
        assert "ST_Multi" in sql

    def test_makevalid_and_collectionextract_applied(self):
        """Real park polygons routinely self-intersect; without MakeValid the
        rollup's ST_Intersects raises a GEOS TopologyException mid-run."""
        engine, conn = _make_engine_mock()
        calls = _capture(conn)
        upsert_boundaries(engine, "park_attrs", self._rows())
        assert "ST_MakeValid" in calls[0][0]
        assert "ST_CollectionExtract" in calls[0][0]

    def test_simplification_can_be_disabled(self):
        engine, conn = _make_engine_mock()
        calls = _capture(conn)
        upsert_boundaries(engine, "x", self._rows(), simplify_tolerance=None)
        assert "ST_SimplifyPreserveTopology" not in calls[0][0]

    def test_null_geometry_writes_null(self):
        engine, conn = _make_engine_mock()
        calls = _capture(conn)
        upsert_boundaries(engine, "x", [{
            "source_id": "s", "natural_key": "k", "geojson": None,
        }])
        assert "CASE WHEN :geojson IS NULL THEN NULL" in calls[0][0]

    def test_duplicate_conflict_keys_collapsed(self):
        """ON CONFLICT DO UPDATE raises 'cannot affect row a second time' if one
        batch carries two rows with the same key."""
        engine, conn = _make_engine_mock()
        rows = self._rows(1) * 3
        assert upsert_boundaries(engine, "x", rows) == 1

    def test_nan_scrubbed(self):
        engine, conn = _make_engine_mock()
        calls = _capture(conn)
        upsert_boundaries(engine, "x", [{
            "source_id": "s", "natural_key": "k", "geojson": None,
            "area_acres": float("nan"),
        }], extra_cols=("area_acres",))
        assert calls[0][1][0]["area_acres"] is None

    def test_extra_cols_written(self):
        engine, conn = _make_engine_mock()
        calls = _capture(conn)
        upsert_boundaries(engine, "x", [{
            "source_id": "s", "natural_key": "k", "geojson": None, "owner_raw": "X",
        }], extra_cols=("owner_raw",))
        assert "owner_raw" in calls[0][0]

    def test_empty_rows_short_circuits(self):
        engine, _ = _make_engine_mock()
        assert upsert_boundaries(engine, "x", []) == 0
        engine.begin.assert_not_called()

    def test_upsert_is_idempotent(self):
        engine, conn = _make_engine_mock()
        calls = _capture(conn)
        upsert_boundaries(engine, "x", self._rows())
        assert "ON CONFLICT (source_id, natural_key) DO UPDATE" in calls[0][0]
        assert "loaded_at = now()" in calls[0][0]

    @pytest.mark.parametrize("bad", ["x; DROP TABLE y", "public.x", "1x", ""])
    def test_table_name_must_be_a_safe_identifier(self, bad):
        engine, _ = _make_engine_mock()
        with pytest.raises(ValueError):
            upsert_boundaries(engine, bad, self._rows())

    def test_column_names_validated_too(self):
        engine, _ = _make_engine_mock()
        with pytest.raises(ValueError):
            upsert_boundaries(engine, "x", self._rows(), area_col="a; DROP TABLE y")


class TestSqmPerAcre:
    def test_is_the_international_acre(self):
        assert SQM_PER_ACRE == 4046.8564224
