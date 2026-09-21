"""
Unit tests for lib.match_queue.enqueue_tier3_matches.

No live DB required — the SQLAlchemy engine is replaced with a MagicMock so
we can assert on the SQL text and parameters that would be sent, without any
network or Postgres involvement.

Covers:
  - Correct columns are passed in the INSERT statement.
  - status='pending' is always set on new pairs.
  - A second call with the same pair payload does not add a second row
    (the on-conflict semantics are validated via the SQL text; the in-process
    batch dedup is validated by inspecting how many rows were sent).
  - Pair canonicalisation: (A, B) and (B, A) are stored as the same row.
  - Intra-batch duplicate pairs are deduplicated before the DB call.
  - An empty input list produces no DB call and returns 0.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.match_queue import (
    STRATEGY_TIER3_FUZZY,
    _canonical_pair,
    enqueue_tier3_matches,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_engine() -> MagicMock:
    """Return a MagicMock that satisfies the 'with engine.begin() as conn' pattern."""
    engine = MagicMock()
    conn = MagicMock()
    engine.begin.return_value.__enter__ = MagicMock(return_value=conn)
    engine.begin.return_value.__exit__ = MagicMock(return_value=False)
    return engine, conn


def _pair(
    source_a: str = "cms_general",
    key_a: str = "KEY001",
    source_b: str = "nppes_practice_locations",
    key_b: str = "KEY002",
    score: float = 0.85,
    feature_breakdown: dict | None = None,
) -> dict:
    p = {"source_a": source_a, "key_a": key_a, "source_b": source_b, "key_b": key_b, "score": score}
    if feature_breakdown is not None:
        p["feature_breakdown"] = feature_breakdown
    return p


# ---------------------------------------------------------------------------
# _canonical_pair
# ---------------------------------------------------------------------------

class TestCanonicalPair:
    def test_already_ordered(self):
        # ("a", "1") < ("b", "2") — should stay in the same order.
        result = _canonical_pair("a_source", "001", "b_source", "002")
        assert result == ("a_source", "001", "b_source", "002")

    def test_reversed_order_is_swapped(self):
        # ("b", "2") > ("a", "1") — should be swapped.
        result = _canonical_pair("b_source", "002", "a_source", "001")
        assert result == ("a_source", "001", "b_source", "002")

    def test_same_source_keys_ordered_by_natural_key(self):
        result = _canonical_pair("cms", "ZZZ", "cms", "AAA")
        assert result == ("cms", "AAA", "cms", "ZZZ")

    def test_equal_pair_is_stable(self):
        result = _canonical_pair("cms", "KEY1", "cms", "KEY1")
        assert result == ("cms", "KEY1", "cms", "KEY1")


# ---------------------------------------------------------------------------
# enqueue_tier3_matches — basic correctness
# ---------------------------------------------------------------------------

class TestEnqueueTier3Matches:
    def test_empty_pairs_returns_zero_no_db_call(self):
        engine, conn = _make_engine()
        result = enqueue_tier3_matches(engine, [])
        assert result == 0
        conn.execute.assert_not_called()

    def test_single_pair_executes_upsert(self):
        engine, conn = _make_engine()
        pairs = [_pair(score=0.85)]
        result = enqueue_tier3_matches(engine, pairs)

        assert result == 1
        assert conn.execute.call_count == 1

        # Inspect the SQL text — must contain ON CONFLICT.
        sql_arg = conn.execute.call_args[0][0]
        sql_text = str(sql_arg)
        assert "ON CONFLICT" in sql_text
        assert "review.pending_pairs" in sql_text

    def test_status_is_always_pending(self):
        engine, conn = _make_engine()
        pairs = [_pair(score=0.80)]
        enqueue_tier3_matches(engine, pairs)

        rows_arg = conn.execute.call_args[0][1]
        # rows_arg is the list of dicts passed to executemany.
        assert isinstance(rows_arg, list)
        assert len(rows_arg) == 1
        assert rows_arg[0]["status"] == "pending"

    def test_correct_columns_sent_to_db(self):
        engine, conn = _make_engine()
        pairs = [_pair(source_a="cms_general", key_a="K001",
                       source_b="nppes_practice_locations", key_b="K002",
                       score=0.87)]
        enqueue_tier3_matches(engine, pairs)

        rows_arg = conn.execute.call_args[0][1]
        row = rows_arg[0]

        # All required columns must be present.
        for col in ("source_id_a", "natural_key_a", "source_id_b", "natural_key_b",
                    "score", "feature_breakdown", "merge_strategy", "status"):
            assert col in row, f"Missing column: {col}"

        assert row["score"] == 0.87
        assert row["merge_strategy"] == STRATEGY_TIER3_FUZZY
        assert row["status"] == "pending"

    def test_pair_is_canonicalised_before_insert(self):
        """Reversed pair (B, A) must produce same row as (A, B)."""
        engine_ab, conn_ab = _make_engine()
        engine_ba, conn_ba = _make_engine()

        pair_ab = [_pair(source_a="aaa_source", key_a="001",
                         source_b="zzz_source", key_b="999", score=0.80)]
        pair_ba = [_pair(source_a="zzz_source", key_a="999",
                         source_b="aaa_source", key_b="001", score=0.80)]

        enqueue_tier3_matches(engine_ab, pair_ab)
        enqueue_tier3_matches(engine_ba, pair_ba)

        row_ab = conn_ab.execute.call_args[0][1][0]
        row_ba = conn_ba.execute.call_args[0][1][0]

        # Both calls should produce the same canonical (a, b) ordering.
        assert (row_ab["source_id_a"], row_ab["natural_key_a"]) == (
            row_ba["source_id_a"], row_ba["natural_key_a"]
        )
        assert (row_ab["source_id_b"], row_ab["natural_key_b"]) == (
            row_ba["source_id_b"], row_ba["natural_key_b"]
        )

    def test_intra_batch_duplicates_deduplicated(self):
        """Two identical pairs in the same call must produce only one DB row."""
        engine, conn = _make_engine()
        pairs = [
            _pair(score=0.83),
            _pair(score=0.83),  # exact duplicate
        ]
        result = enqueue_tier3_matches(engine, pairs)

        assert result == 1
        rows_arg = conn.execute.call_args[0][1]
        assert len(rows_arg) == 1

    def test_intra_batch_ab_and_ba_dedup(self):
        """(A, B) and (B, A) in the same batch must collapse to one row."""
        engine, conn = _make_engine()
        pairs = [
            _pair(source_a="src_x", key_a="001", source_b="src_y", key_b="002", score=0.78),
            _pair(source_a="src_y", key_a="002", source_b="src_x", key_b="001", score=0.78),
        ]
        result = enqueue_tier3_matches(engine, pairs)
        assert result == 1
        rows_arg = conn.execute.call_args[0][1]
        assert len(rows_arg) == 1

    def test_multiple_distinct_pairs_all_sent(self):
        engine, conn = _make_engine()
        pairs = [
            _pair(key_a="K001", key_b="K002", score=0.80),
            _pair(key_a="K003", key_b="K004", score=0.77),
            _pair(key_a="K005", key_b="K006", score=0.91),
        ]
        result = enqueue_tier3_matches(engine, pairs)
        assert result == 3
        rows_arg = conn.execute.call_args[0][1]
        assert len(rows_arg) == 3

    def test_feature_breakdown_serialised_to_json_string(self):
        """feature_breakdown dict is serialised to a JSON string for the DB."""
        engine, conn = _make_engine()
        breakdown = {"name": 0.9, "address": 0.7, "spatial": 0.0}
        pairs = [_pair(score=0.82, feature_breakdown=breakdown)]
        enqueue_tier3_matches(engine, pairs)

        rows_arg = conn.execute.call_args[0][1]
        stored = rows_arg[0]["feature_breakdown"]
        # Should be a JSON string, parseable back to the original dict.
        assert isinstance(stored, str)
        assert json.loads(stored) == breakdown

    def test_no_feature_breakdown_sends_none(self):
        engine, conn = _make_engine()
        pairs = [_pair(score=0.76)]
        enqueue_tier3_matches(engine, pairs)

        rows_arg = conn.execute.call_args[0][1]
        assert rows_arg[0]["feature_breakdown"] is None

    def test_custom_merge_strategy_passed_through(self):
        engine, conn = _make_engine()
        pairs = [_pair(score=0.80)]
        enqueue_tier3_matches(engine, pairs, merge_strategy="tier3_spatial")

        rows_arg = conn.execute.call_args[0][1]
        assert rows_arg[0]["merge_strategy"] == "tier3_spatial"

    def test_returns_count_of_rows_sent(self):
        engine, conn = _make_engine()
        pairs = [_pair(key_a="A", key_b="B"), _pair(key_a="C", key_b="D")]
        result = enqueue_tier3_matches(engine, pairs)
        assert result == 2


# ---------------------------------------------------------------------------
# merge_all integration — engine wiring
# ---------------------------------------------------------------------------

class TestMergeAllEngineWiring:
    """
    Verify that merge_all(..., engine=engine) calls enqueue_tier3_matches when
    the review_queue is non-empty, and does NOT call it when engine is None.
    """

    @staticmethod
    def _build_minimal_df(n: int = 6) -> "pd.DataFrame":
        import pandas as pd

        # Build rows that are similar enough to produce Tier-3 candidates but
        # not identical (so Tier 1/2 don't absorb them all).
        rows = []
        for i in range(n):
            rows.append({
                "source_id": "cms_general",
                "natural_key": f"KEY{i:04d}",
                "name_raw": "Greenfield Hospital" if i < 3 else f"Clinic {i}",
                "name_normalized": "greenfield hospital" if i < 3 else f"clinic {i}",
                "address_line_1": f"{i} Main St",
                "city": "Columbia",
                "site_state": "SC",
                "zip5": "29201",
                "phone": "",
                "latitude": None,
                "longitude": None,
                "ccn": "",
                "npi": "",
                "segment": None,
                "ein": None,
                "county_fips": None,
                "size_metric": None,
                "size_value": None,
                "size_unit": None,
                "source_file": None,
                "vertical": "healthcare",
                "account_type": None,
            })
        return pd.DataFrame(rows)

    def test_engine_none_skips_enqueue(self):
        """With engine=None, enqueue_tier3_matches must never be called."""
        import sys
        import os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import healthcare.healthcare_merge as hm  # noqa: avoid top-level for path isolation

        df = self._build_minimal_df()
        with patch.object(hm, "enqueue_tier3_matches") as mock_enqueue:
            hm.merge_all(df, engine=None)
            mock_enqueue.assert_not_called()

    def test_engine_provided_calls_enqueue_when_queue_nonempty(self):
        """With engine provided, enqueue_tier3_matches is called iff review_queue non-empty."""
        import sys
        import os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import healthcare.healthcare_merge as hm

        df = self._build_minimal_df()
        fake_engine, _ = _make_engine()

        with patch.object(hm, "enqueue_tier3_matches", return_value=0) as mock_enqueue:
            # Stub tier3_fuzzy_merge to return a non-empty review queue so we can
            # test the wiring regardless of whether the toy data actually produces pairs.
            import pandas as pd
            fake_queue = pd.DataFrame([
                {"key_a": "K1", "key_b": "K2", "score": 0.82,
                 "source_a": "cms_general", "source_b": "nppes_practice_locations"}
            ])
            # Also stub survivorship so it doesn't need cluster_id on the patched df.
            fake_canonical = pd.DataFrame([{"cluster_id": "t3:K1", "name_normalized": "test"}])
            with patch.object(hm, "tier3_fuzzy_merge", return_value=(df, fake_queue)), \
                 patch.object(hm, "survivorship", return_value=fake_canonical):
                hm.merge_all(df, engine=fake_engine)

            mock_enqueue.assert_called_once()
            call_kwargs = mock_enqueue.call_args
            assert call_kwargs[0][0] is fake_engine

    def test_engine_provided_skips_enqueue_when_queue_empty(self):
        """With engine provided but empty review_queue, enqueue must not be called."""
        import sys
        import os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import healthcare.healthcare_merge as hm

        df = self._build_minimal_df()
        fake_engine, _ = _make_engine()

        with patch.object(hm, "enqueue_tier3_matches", return_value=0) as mock_enqueue:
            import pandas as pd
            empty_queue = pd.DataFrame(
                columns=["key_a", "key_b", "score", "source_a", "source_b"]
            )
            fake_canonical = pd.DataFrame([{"cluster_id": "t3:K1", "name_normalized": "test"}])
            with patch.object(hm, "tier3_fuzzy_merge", return_value=(df, empty_queue)), \
                 patch.object(hm, "survivorship", return_value=fake_canonical):
                hm.merge_all(df, engine=fake_engine)

            mock_enqueue.assert_not_called()
