-- review.pending_pairs — staging-phase uncertain match queue.
--
-- WHY THIS TABLE EXISTS INSTEAD OF review.match_queue (migration 005):
--
--   review.match_queue has NOT NULL FK columns (source_record_a/b) that
--   reference core.source_record, which is only populated during the core-write
--   phase (post D5 3-way diff).  Tier-3 fuzzy matching runs during the merge
--   stage, before any source_records exist.  Writing to match_queue at that
--   point would require either violating NOT NULL or doing a blocking lookup
--   against core — neither is acceptable.
--
--   pending_pairs stores the same semantic data using text keys (source_id +
--   natural_key) that are available at merge time.  A scheduled job (or the
--   core-write phase) can join pending_pairs against core.source_record to
--   promote resolved rows into review.match_queue.
--
-- IDEMPOTENCY:
--   ON CONFLICT (source_id_a, natural_key_a, source_id_b, natural_key_b,
--                merge_strategy) DO UPDATE — safe for re-runs.  Pair
--   canonicalisation (a < b lexicographically) is enforced in application
--   code (enqueue_tier3_matches) so (A,B) and (B,A) never produce two rows.
--
-- STATUS values: pending | merged | rejected
-- MERGE_STRATEGY values: tier3_fuzzy (healthcare) — extensible for other verticals.

CREATE TABLE IF NOT EXISTS review.pending_pairs (
  pending_pair_id  bigserial    PRIMARY KEY,
  source_id_a      text         NOT NULL,
  natural_key_a    text         NOT NULL,
  source_id_b      text         NOT NULL,
  natural_key_b    text         NOT NULL,
  score            numeric      NOT NULL CHECK (score >= 0 AND score <= 1),
  feature_breakdown jsonb,
  merge_strategy   text         NOT NULL,            -- tier3_fuzzy | ...
  status           text         NOT NULL DEFAULT 'pending',  -- pending|merged|rejected
  created_at       timestamptz  NOT NULL DEFAULT now(),
  resolved_at      timestamptz,
  resolved_by      text,

  -- Natural uniqueness: the ordered pair plus the strategy that produced it.
  UNIQUE (source_id_a, natural_key_a, source_id_b, natural_key_b, merge_strategy)
);

-- Index to support queue-worker queries that poll for pending rows by strategy.
CREATE INDEX IF NOT EXISTS idx_pending_pairs_status_strategy
  ON review.pending_pairs (status, merge_strategy);
