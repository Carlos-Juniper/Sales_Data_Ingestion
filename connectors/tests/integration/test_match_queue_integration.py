"""
Integration tests for lib.match_queue.enqueue_tier3_matches.

Requires:
    docker compose up -d postgres
    python db/run_migrations.py   (applies migration 013_review_pending_pairs.sql)
    ALLOW_DB_INTEGRATION_TESTS=1 ALLOW_DESTRUCTIVE_DB_TESTS=1 pytest -m integration

These tests write to and DROP review.test_match_queue_* tables so they must
NEVER point at Cloud SQL.  The double opt-in guards are the primary safety
mechanism; the localhost check in conftest.py is defence in depth.

Key assertions:
  1. Two distinct pairs are inserted with status='pending'.
  2. Re-running the same pairs does not increase the row count (idempotency).
  3. A reversed pair (B, A) after (A, B) is treated as the same row.
  4. status='pending' is set on initial insert.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Destructive-test guard — same pattern as test_core_writer_integration.py
# ---------------------------------------------------------------------------

def _allow_destructive() -> bool:
    return os.environ.get("ALLOW_DESTRUCTIVE_DB_TESTS", "").lower() in ("1", "true", "yes")


@pytest.fixture(autouse=True)
def require_destructive_opt_in():
    """Skip every test in this file unless ALLOW_DESTRUCTIVE_DB_TESTS=1."""
    if not _allow_destructive():
        pytest.skip(
            "match_queue integration tests write to review.pending_pairs. "
            "Set ALLOW_DESTRUCTIVE_DB_TESTS=1 and point DATABASE_URL at your "
            "local docker Postgres — NOT the Cloud SQL proxy."
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STRATEGY = "tier3_fuzzy_test"  # unique strategy tag so tests don't collide with prod data

_PAIR_A = {
    "source_a": "cms_general",
    "key_a": "TEST_MQ_KEY001",
    "source_b": "nppes_practice_locations",
    "key_b": "TEST_MQ_KEY002",
    "score": 0.85,
}

_PAIR_B = {
    "source_a": "cms_general",
    "key_a": "TEST_MQ_KEY003",
    "source_b": "va_facilities",
    "key_b": "TEST_MQ_KEY004",
    "score": 0.78,
}


def _count_pairs(conn, strategy: str) -> int:
    """Return the number of pending_pairs rows for the given test strategy."""
    from sqlalchemy import text
    return conn.execute(
        text("SELECT count(*) FROM review.pending_pairs WHERE merge_strategy = :s"),
        {"s": strategy},
    ).scalar()


def _cleanup(conn, strategy: str) -> None:
    """Remove all rows inserted by this test run."""
    from sqlalchemy import text
    conn.execute(
        text("DELETE FROM review.pending_pairs WHERE merge_strategy = :s"),
        {"s": strategy},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEnqueueTier3MatchesIntegration:

    def test_inserts_two_pairs_with_pending_status(self, engine):
        """Two distinct pairs are inserted; both have status='pending'."""
        from lib.match_queue import enqueue_tier3_matches
        from sqlalchemy import text

        pairs = [_PAIR_A, _PAIR_B]
        try:
            n = enqueue_tier3_matches(engine, pairs, merge_strategy=_STRATEGY)
            assert n == 2

            with engine.connect() as conn:
                count = _count_pairs(conn, _STRATEGY)
                assert count == 2

                rows = conn.execute(
                    text(
                        "SELECT status FROM review.pending_pairs "
                        "WHERE merge_strategy = :s"
                    ),
                    {"s": _STRATEGY},
                ).fetchall()

            statuses = [r[0] for r in rows]
            assert all(s == "pending" for s in statuses), (
                f"Expected all statuses to be 'pending', got {statuses}"
            )

        finally:
            with engine.begin() as conn:
                _cleanup(conn, _STRATEGY)

    def test_re_run_is_idempotent_count_stays_at_two(self, engine):
        """Calling enqueue_tier3_matches twice with the same pairs keeps count at 2."""
        from lib.match_queue import enqueue_tier3_matches

        pairs = [_PAIR_A, _PAIR_B]
        try:
            enqueue_tier3_matches(engine, pairs, merge_strategy=_STRATEGY)  # first run
            enqueue_tier3_matches(engine, pairs, merge_strategy=_STRATEGY)  # second run

            with engine.connect() as conn:
                count = _count_pairs(conn, _STRATEGY)

            assert count == 2, (
                f"Expected 2 rows after idempotent re-run, got {count}. "
                "ON CONFLICT upsert may not be working correctly."
            )

        finally:
            with engine.begin() as conn:
                _cleanup(conn, _STRATEGY)

    def test_reversed_pair_does_not_create_duplicate(self, engine):
        """Inserting (A, B) then (B, A) must produce only one row."""
        from lib.match_queue import enqueue_tier3_matches

        forward = [_PAIR_A]
        reversed_pair = [{
            "source_a": _PAIR_A["source_b"],
            "key_a": _PAIR_A["key_b"],
            "source_b": _PAIR_A["source_a"],
            "key_b": _PAIR_A["key_a"],
            "score": _PAIR_A["score"],
        }]

        try:
            enqueue_tier3_matches(engine, forward, merge_strategy=_STRATEGY)
            enqueue_tier3_matches(engine, reversed_pair, merge_strategy=_STRATEGY)

            with engine.connect() as conn:
                count = _count_pairs(conn, _STRATEGY)

            assert count == 1, (
                f"Expected 1 row for (A,B) + (B,A), got {count}. "
                "Pair canonicalisation is not working correctly."
            )

        finally:
            with engine.begin() as conn:
                _cleanup(conn, _STRATEGY)

    def test_score_updated_on_rerun(self, engine):
        """A second call with a different score for the same pair updates the stored score."""
        from lib.match_queue import enqueue_tier3_matches
        from sqlalchemy import text

        pair_first = [{**_PAIR_A, "score": 0.80}]
        pair_updated = [{**_PAIR_A, "score": 0.89}]

        try:
            enqueue_tier3_matches(engine, pair_first, merge_strategy=_STRATEGY)
            enqueue_tier3_matches(engine, pair_updated, merge_strategy=_STRATEGY)

            with engine.connect() as conn:
                score = conn.execute(
                    text(
                        "SELECT score FROM review.pending_pairs "
                        "WHERE merge_strategy = :s "
                        "AND source_id_a = :sa AND natural_key_a = :ka"
                    ),
                    {"s": _STRATEGY, "sa": "cms_general", "ka": "TEST_MQ_KEY001"},
                ).scalar()

            # The ON CONFLICT DO UPDATE should have overwritten to the latest score.
            assert float(score) == pytest.approx(0.89, abs=1e-6)

        finally:
            with engine.begin() as conn:
                _cleanup(conn, _STRATEGY)
