-- Florida DBPR condominium and cooperative projects (F2, F3), one table.
-- Loaded by connectors/hoa/fl_dbpr_condo_coop.py (docs/hoa-pipeline.md §5).
--
-- Same 21-column canonical shape as every other staging table, plus attrs
-- (jsonb) for source fields the merge needs.  upsert_staging writes the
-- canonical columns; lib/db.update_staging_attrs fills attrs.
--
-- natural_key = '<program>:<project number>', program condo or coop.  attrs:
-- program, file number, managing entity name/route/ZIP (route holds the
-- C/O firm), project county, unit count.  segment = program.

CREATE TABLE IF NOT EXISTS staging.fl_dbpr_condo_coop (
  source_id        text NOT NULL,
  natural_key      text NOT NULL,
  vertical         text,
  account_type     text,
  name_raw         text,
  name_normalized  text,
  address_line_1   text,
  city             text,
  state            text,
  zip5             text,
  phone_raw        text,
  phone_normalized text,
  latitude         numeric,
  longitude        numeric,
  geom             geometry(Point, 4326),
  segment          text,
  ein              text,
  county_fips      text,
  size_metric      text,
  size_value       numeric,
  size_unit        text,
  source_file      text,
  loaded_at        timestamptz NOT NULL DEFAULT now(),
  attrs            jsonb,
  PRIMARY KEY (source_id, natural_key)
);

CREATE INDEX IF NOT EXISTS idx_fl_dbpr_condo_coop_zip5 ON staging.fl_dbpr_condo_coop (zip5);
