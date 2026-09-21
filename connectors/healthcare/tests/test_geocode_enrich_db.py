"""
Unit tests for the generalized geocode_enrich.py DB write and cache path.

Tests cover:
  - apply_cache: pre-populating lat/lon from the enrich_geocode cache
  - upsert_enrich_geocode: SQL upsert via mocked engine
  - load_cached_geocodes: reading from the cache table via mocked engine
  - Source-agnostic behavior: source_id is a runtime parameter, not hardcoded
"""

from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from unittest.mock import MagicMock, call, patch

import pandas as pd
import pytest

from healthcare.geocode_enrich import (
    apply_cache,
    load_cached_geocodes,
    upsert_enrich_geocode,
    assert_input_shape,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_df(rows: list[dict] | None = None) -> pd.DataFrame:
    """Build a minimal DataFrame with required geocode input columns."""
    if rows is None:
        rows = [{}]
    records = []
    for i, overrides in enumerate(rows, start=1):
        base = {
            "natural_key": f"key_{i}",
            "address_line_1": "100 Main St",
            "city": "Miami",
            "site_state": "FL",
            "zip5": "33101",
        }
        base.update(overrides)
        records.append(base)
    return pd.DataFrame(records)


def _enriched_df(natural_keys: list[str], lat=25.76, lon=-80.19) -> pd.DataFrame:
    """Build a geocode-enriched DataFrame suitable for upsert_enrich_geocode."""
    rows = []
    for nk in natural_keys:
        rows.append({
            "natural_key": nk,
            "latitude": lat,
            "longitude": lon,
            "geocode_precision": "rooftop",
            "geocode_source": "census",
            "geocode_match_type": "Exact",
            "geocode_address_returned": "100 MAIN ST, MIAMI, FL, 33101",
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# apply_cache
# ---------------------------------------------------------------------------


class TestApplyCache:
    def test_cached_row_gets_lat_lon_populated(self):
        """A natural_key in the cache must have its latitude set."""
        df = _make_df([{"natural_key": "A"}])
        cache = {
            "A": {
                "latitude": 25.76,
                "longitude": -80.19,
                "geocode_precision": "rooftop",
                "geocode_source": "census",
                "geocode_match_type": "Exact",
            }
        }
        result = apply_cache(df, cache)
        assert result.loc[result["natural_key"] == "A", "latitude"].iloc[0] == pytest.approx(25.76)
        assert result.loc[result["natural_key"] == "A", "longitude"].iloc[0] == pytest.approx(-80.19)

    def test_uncached_row_latitude_stays_none(self):
        """A natural_key not in the cache must have latitude=None (will be geocoded)."""
        df = _make_df([{"natural_key": "B"}])
        cache = {}
        result = apply_cache(df, cache)
        assert result.loc[result["natural_key"] == "B", "latitude"].iloc[0] is None

    def test_cache_does_not_mutate_input_df(self):
        """apply_cache must return a copy, not modify the original."""
        df = _make_df([{"natural_key": "A"}])
        cache = {"A": {"latitude": 25.76, "longitude": -80.19,
                       "geocode_precision": "rooftop", "geocode_source": "census",
                       "geocode_match_type": "Exact"}}
        _ = apply_cache(df, cache)
        assert "latitude" not in df.columns or df.iloc[0].get("latitude") is None

    def test_partial_cache_only_fills_cached_rows(self):
        """With two rows and one cached, only the cached row has lat set."""
        df = _make_df([{"natural_key": "A"}, {"natural_key": "B"}])
        cache = {"A": {"latitude": 25.76, "longitude": -80.19,
                       "geocode_precision": "rooftop", "geocode_source": "census",
                       "geocode_match_type": None}}
        result = apply_cache(df, cache)
        assert result.loc[result["natural_key"] == "A", "latitude"].iloc[0] == pytest.approx(25.76)
        b_lat = result.loc[result["natural_key"] == "B", "latitude"].iloc[0]
        assert b_lat is None

    def test_apply_cache_adds_geocode_columns_when_absent(self):
        """apply_cache must initialise geocode columns even when not in input."""
        df = _make_df([{"natural_key": "A"}])
        # Ensure no geocode columns in input.
        for col in ["latitude", "longitude", "geocode_source"]:
            if col in df.columns:
                df = df.drop(columns=[col])
        result = apply_cache(df, {})
        assert "latitude" in result.columns
        assert "geocode_source" in result.columns


# ---------------------------------------------------------------------------
# upsert_enrich_geocode
# ---------------------------------------------------------------------------


class TestUpsertEnrichGeocode:
    def test_returns_row_count(self):
        """upsert_enrich_geocode must return the count of rows written."""
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        enriched = _enriched_df(["A|1", "B|1"])
        count = upsert_enrich_geocode(mock_engine, "nppes_practice_locations", enriched)
        assert count == 2

    def test_empty_dataframe_returns_zero(self):
        """An empty DataFrame must return 0 without calling execute."""
        mock_engine = MagicMock()
        count = upsert_enrich_geocode(mock_engine, "cms_general", pd.DataFrame())
        assert count == 0
        mock_engine.begin.assert_not_called()

    def test_nan_latitude_coerced_to_none(self):
        """Float NaN in latitude must be coerced to None before the DB call."""
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        enriched = _enriched_df(["C|1"])
        enriched.loc[0, "latitude"] = float("nan")
        enriched.loc[0, "longitude"] = float("nan")

        upsert_enrich_geocode(mock_engine, "cms_general", enriched)

        execute_call = mock_conn.execute.call_args
        rows_arg = execute_call.args[1]
        # The lat/lon in the rows list must be None, not NaN.
        assert rows_arg[0]["latitude"] is None
        assert rows_arg[0]["longitude"] is None

    def test_source_id_is_set_from_parameter(self):
        """source_id in the rows list must come from the parameter, not the DataFrame."""
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        enriched = _enriched_df(["X|1"])
        upsert_enrich_geocode(mock_engine, "va_facilities", enriched)

        execute_call = mock_conn.execute.call_args
        rows_arg = execute_call.args[1]
        assert rows_arg[0]["source_id"] == "va_facilities"


# ---------------------------------------------------------------------------
# load_cached_geocodes
# ---------------------------------------------------------------------------


class TestLoadCachedGeocodes:
    def test_returns_dict_keyed_on_natural_key(self):
        """load_cached_geocodes must return a dict keyed on natural_key strings."""
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        # Simulate two rows returned from the DB.
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [
            ("key_1", 25.76, -80.19, "rooftop", "census", "Exact"),
            ("key_2", 30.27, -97.74, "street", "nominatim", None),
        ]
        mock_conn.execute.return_value = mock_result

        cache = load_cached_geocodes(mock_engine, "nppes_practice_locations")

        assert "key_1" in cache
        assert cache["key_1"]["latitude"] == pytest.approx(25.76)
        assert cache["key_1"]["geocode_source"] == "census"
        assert "key_2" in cache

    def test_empty_result_returns_empty_dict(self):
        """When the cache table has no rows for the source_id, return {}."""
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        mock_result = MagicMock()
        mock_result.fetchall.return_value = []
        mock_conn.execute.return_value = mock_result

        cache = load_cached_geocodes(mock_engine, "cms_general")
        assert cache == {}

    def test_query_filters_by_source_id(self):
        """The SQL query must bind the source_id parameter."""
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        mock_result = MagicMock()
        mock_result.fetchall.return_value = []
        mock_conn.execute.return_value = mock_result

        load_cached_geocodes(mock_engine, "va_facilities")

        execute_call = mock_conn.execute.call_args
        params = execute_call.args[1] if len(execute_call.args) > 1 else execute_call.kwargs.get("parameters", {})
        # The source_id must be passed as a bound parameter.
        assert "va_facilities" in str(execute_call)
