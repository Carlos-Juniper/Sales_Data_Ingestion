"""
Match-queue writer for uncertain entity-resolution pairs.

Tier-3 fuzzy matching produces candidate pairs in the score band
[queue_threshold, auto_threshold) — high enough to be suspicious, but not
high enough to auto-merge.  This module enqueues those pairs into
review.pending_pairs for human (or rule-based) review.

WHY review.pending_pairs, NOT review.match_queue (migration 005):
    review.match_queue has NOT NULL FK columns referencing core.source_record,
    which is only populated during the core-write phase (post D5 3-way diff).
    Tier-3 runs at the merge stage, before source_records exist.
    pending_pairs stores the same semantic information using text keys
    (source_id + natural_key) available at merge time.  The core-write phase
    can promote resolved rows into review.match_queue once FKs are satisfiable.

Pair canonicalisation:
    Pairs are stored with (a, b) ordered such that
    (source_id_a, natural_key_a) <= (source_id_b, natural_key_b) lexicographically.
    This ensures (A, B) and (B, A) produced by different runs never create
    two rows in the table — the UNIQUE constraint on the ordered pair provides
    the idempotency guarantee.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# The merge strategy tag stored for healthcare Tier-3 pairs.
STRATEGY_TIER3_FUZZY = "tier3_fuzzy"

# Parks: an uncorroborated manager-string -> Census GEOID match in the review
# band. Distinct from tier3_fuzzy because the two sides of the pair are not the
# same kind of thing — a park on one side, a government on the other — so a
# reviewer needs different context to adjudicate it.
STRATEGY_PARKS_MANAGER = "parks_manager"


def _canonical_pair(
    source_id_a: str,
    natural_key_a: str,
    source_id_b: str,
    natural_key_b: str,
) -> tuple[str, str, str, str]:
    """
    Return the pair ordered so (a, b) <= (b, a) lexicographically.

    Guarantees that (X, Y) and (Y, X) produce the same DB row, so the
    UNIQUE constraint on (source_id_a, natural_key_a, source_id_b,
    natural_key_b, merge_strategy) is sufficient for full idempotency.
    """
    key_a = (source_id_a, natural_key_a)
    key_b = (source_id_b, natural_key_b)
    if key_a <= key_b:
        return source_id_a, natural_key_a, source_id_b, natural_key_b
    return source_id_b, natural_key_b, source_id_a, natural_key_a


def enqueue_tier3_matches(
    engine: Engine,
    uncertain_pairs: Sequence[dict[str, Any]],
    *,
    merge_strategy: str = STRATEGY_TIER3_FUZZY,
) -> int:
    """
    Upsert uncertain match pairs into review.pending_pairs.

    Each element of *uncertain_pairs* must contain:
        source_a    (str)   source_id of the first candidate
        key_a       (str)   natural_key of the first candidate
        source_b    (str)   source_id of the second candidate
        key_b       (str)   natural_key of the second candidate
        score       (float) similarity score in [0, 1]

    Optional per-pair key:
        feature_breakdown  (dict | None)  per-feature score breakdown (stored as jsonb)

    Pairs are canonicalised before insert so (A, B) and (B, A) land in the
    same row.  ON CONFLICT DO UPDATE bumps the score and refreshes created_at
    so a re-run with updated scores wins without creating duplicates.

    Returns:
        Number of rows passed to the database (before deduplication inside
        the batch).  The DB UNIQUE constraint handles cross-run idempotency.

    Raises:
        sqlalchemy.exc.SQLAlchemyError on any DB failure — callers should
        decide whether to abort or log-and-continue.
    """
    if not uncertain_pairs:
        logger.debug("enqueue_tier3_matches: no pairs to enqueue")
        return 0

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()

    for pair in uncertain_pairs:
        sa = str(pair["source_a"])
        ka = str(pair["key_a"])
        sb = str(pair["source_b"])
        kb = str(pair["key_b"])
        score = float(pair["score"])

        # Canonicalise the pair and deduplicate within the batch so that
        # executemany with ON CONFLICT DO UPDATE never sees two rows for the
        # same constraint key (Postgres raises "cannot affect row a second time").
        sid_a, nk_a, sid_b, nk_b = _canonical_pair(sa, ka, sb, kb)
        dedup_key = (sid_a, nk_a, sid_b, nk_b)
        if dedup_key in seen:
            logger.debug(
                "enqueue_tier3_matches: skipping intra-batch duplicate (%s:%s, %s:%s)",
                sid_a, nk_a, sid_b, nk_b,
            )
            continue
        seen.add(dedup_key)

        feature_breakdown = pair.get("feature_breakdown")
        import json as _json
        rows.append({
            "source_id_a": sid_a,
            "natural_key_a": nk_a,
            "source_id_b": sid_b,
            "natural_key_b": nk_b,
            "score": score,
            "feature_breakdown": _json.dumps(feature_breakdown) if feature_breakdown is not None else None,
            "merge_strategy": merge_strategy,
            "status": "pending",
        })

    if not rows:
        return 0

    upsert_sql = text("""
        INSERT INTO review.pending_pairs (
            source_id_a, natural_key_a,
            source_id_b, natural_key_b,
            score, feature_breakdown, merge_strategy, status
        ) VALUES (
            :source_id_a, :natural_key_a,
            :source_id_b, :natural_key_b,
            CAST(:score AS numeric),
            CAST(:feature_breakdown AS jsonb),
            :merge_strategy,
            :status
        )
        ON CONFLICT (source_id_a, natural_key_a, source_id_b, natural_key_b, merge_strategy)
        DO UPDATE SET
            score             = EXCLUDED.score,
            feature_breakdown = EXCLUDED.feature_breakdown,
            status            = CASE
                                  -- Never demote a human-resolved row back to pending.
                                  WHEN review.pending_pairs.status IN ('merged', 'rejected')
                                  THEN review.pending_pairs.status
                                  ELSE EXCLUDED.status
                                END,
            created_at        = EXCLUDED.created_at
    """)

    with engine.begin() as conn:
        conn.execute(upsert_sql, rows)

    logger.info(
        "enqueue_tier3_matches: upserted %d uncertain pairs (strategy=%r)",
        len(rows), merge_strategy,
    )
    return len(rows)
