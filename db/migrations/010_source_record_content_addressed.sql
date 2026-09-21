-- D8: Make core.source_record content-addressed.
--
-- DEVIATION FROM §4 (Ingestion-Plan-of-Action.md):
-- §4 appends one full-payload row per (source, record, run) which grows at
-- O(records × runs) — ~219M rows/year at daily cadence, duplicating GCS.
-- Instead, uniqueness is on (source_id, natural_key, payload_sha).  An
-- unchanged record in a subsequent run touches only last_seen_run_id;
-- unchanged re-runs insert zero rows.
--
-- Column notes:
--   payload_sha        — hex SHA-256 of the canonical JSON payload.  NOT NULL
--                        is safe here because core.source_record has zero rows
--                        at migration time (§1 of HANDOFF confirms nothing
--                        writes to core.* yet).
--   first_seen_run_id  — source_run_id of the run that first observed this
--                        (source_id, natural_key, payload_sha) triple.
--   last_seen_run_id   — source_run_id of the most-recent run that observed
--                        an identical payload; bumped on every re-run.
--
-- Constraint rename:
--   003_core_tables.sql declares UNIQUE (source_id, natural_key, source_run_id)
--   inline, so Postgres auto-assigned the name
--   source_record_source_id_natural_key_source_run_id_key.
--   We drop it and replace with UNIQUE (source_id, natural_key, payload_sha).
--
-- Idempotency strategy:
--   DROP CONSTRAINT IF EXISTS — safe no-op on second run once constraint is gone.
--   ADD COLUMN IF NOT EXISTS — no-op if already present.
--   CREATE UNIQUE INDEX IF NOT EXISTS with an explicit name — no-op on re-run.

ALTER TABLE core.source_record
  DROP CONSTRAINT IF EXISTS source_record_source_id_natural_key_source_run_id_key;

ALTER TABLE core.source_record
  ADD COLUMN IF NOT EXISTS payload_sha       text NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS first_seen_run_id bigint,
  ADD COLUMN IF NOT EXISTS last_seen_run_id  bigint;

-- Switch the NOT NULL default to empty-string so ADD COLUMN succeeds even if
-- somehow rows exist; application layer must always supply a real SHA-256.
-- Remove the DEFAULT after adding so future inserts without payload_sha fail
-- loudly instead of silently storing ''.
ALTER TABLE core.source_record
  ALTER COLUMN payload_sha DROP DEFAULT;

CREATE UNIQUE INDEX IF NOT EXISTS uix_source_record_content
  ON core.source_record (source_id, natural_key, payload_sha);
