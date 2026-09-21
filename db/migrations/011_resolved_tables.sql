-- D5: Merge output materializes here before the 3-way diff writes to core.*.
--
-- Each table mirrors the column set of the corresponding core.* table
-- (from 003_core_tables.sql) so the diff SQL is a plain JOIN/anti-join with
-- no column-name translation.  Differences from core:
--
--   1. No bigserial PK — rows are keyed by the deterministic *_key column.
--   2. No FK references — staging is not transactionally consistent with core
--      (parent_account_id, account_id FKs are satisfied only after the core
--      write phase; staging rows just carry the raw key values).
--   3. TRUNCATE + re-insert per run — these are pipeline work-tables, not audit
--      logs.  ON CONFLICT (account_key) DO UPDATE makes re-runs idempotent.
--
-- ASSUMPTION (flag for reviewer): column set mirrors 003 exactly.  If
-- healthcare_merge / deathcare_merge produce additional derived columns (e.g.
-- cluster_id, merge_confidence) those should be added here and ignored during
-- the core diff.  Add them in a follow-on migration rather than editing this
-- file once applied.

-- ---------------------------------------------------------------------------
-- staging.resolved_account
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staging.resolved_account (
  account_key       text        NOT NULL,       -- D1 deterministic key; upsert target
  vertical          text        NOT NULL,
  account_type      text,
  legal_name        text        NOT NULL,
  name_normalized   text        NOT NULL,
  dba_name          text,
  parent_account_key text,                      -- key of survivor after merge/tombstone
  mailing_address   jsonb,
  phone             text,
  email             text,
  website           text,
  status            text,                       -- active|inactive|dissolved|unknown|merged
  external_keys     jsonb,
  size_metric       numeric,
  size_metric_unit  text,
  confidence        numeric,
  PRIMARY KEY (account_key)
);

-- ---------------------------------------------------------------------------
-- staging.resolved_location
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staging.resolved_location (
  location_key      text        NOT NULL,       -- D1 deterministic key; upsert target
  account_key       text        NOT NULL,       -- FK to resolved_account.account_key
  location_name     text,
  site_address      jsonb,
  geom              geometry(Point, 4326),
  boundary          geometry(MultiPolygon, 4326),
  geocode_precision text,
  geometry_source   text,
  maintained_acres  numeric,
  acres_confidence  text,
  site_type         text,
  PRIMARY KEY (location_key)
);

-- ---------------------------------------------------------------------------
-- staging.resolved_contact
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staging.resolved_contact (
  contact_key       text        NOT NULL,       -- D1 deterministic key; upsert target
  account_key       text        NOT NULL,       -- FK to resolved_account.account_key
  full_name         text,
  role              text,
  role_rank         int,
  phone             text,
  email             text,
  address           jsonb,
  source_id         text        NOT NULL,
  is_current        boolean     DEFAULT true,
  PRIMARY KEY (contact_key)
);

-- Spatial index on resolved_location.geom so the dry-run diff query can use
-- ST_DWithin comparisons if needed.
CREATE INDEX IF NOT EXISTS idx_resolved_location_geom
  ON staging.resolved_location USING gist (geom);
