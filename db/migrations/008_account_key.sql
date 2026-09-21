-- D1: Add deterministic upsert keys to core.account, core.location, core.contact.
--
-- account_key — hash of the cluster's strongest external key:
--   CCN > NPI > EIN > normalized_name+zip5 (computed by healthcare_merge /
--   deathcare_merge before writing to staging.resolved_account).
--
-- location_key — hash of (account_key, address_line_1, city, state, zip5).
--   Uniquely identifies a physical site for a given account.
--
-- contact_key  — hash of (account_key, full_name, role).
--   Uniquely identifies a person+role pair for a given account.
--
-- All three are text columns holding the hex-encoded SHA-256 digest (or any
-- deterministic string the application layer agrees on).  NULLABLE on add
-- because existing rows (currently zero in all three tables) would otherwise
-- need a backfill.  The application must never insert/upsert with NULL.
--
-- Idempotency: ADD COLUMN IF NOT EXISTS (PG11+) and CREATE UNIQUE INDEX IF
-- NOT EXISTS guarantee a second runner invocation is a no-op.

ALTER TABLE core.account
  ADD COLUMN IF NOT EXISTS account_key text;

CREATE UNIQUE INDEX IF NOT EXISTS uix_account_account_key
  ON core.account (account_key);

ALTER TABLE core.location
  ADD COLUMN IF NOT EXISTS location_key text;

CREATE UNIQUE INDEX IF NOT EXISTS uix_location_location_key
  ON core.location (location_key);

ALTER TABLE core.contact
  ADD COLUMN IF NOT EXISTS contact_key text;

CREATE UNIQUE INDEX IF NOT EXISTS uix_contact_contact_key
  ON core.contact (contact_key);
