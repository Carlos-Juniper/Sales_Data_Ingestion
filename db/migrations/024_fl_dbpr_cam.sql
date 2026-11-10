-- Florida DBPR community association management licenses (F4 firms, F5 individuals).
-- Loaded by connectors/hoa/fl_dbpr_cam.py (docs/hoa-pipeline.md §5).
--
-- Same 21-column canonical shape as every other staging table, plus attrs
-- (jsonb) for source fields the merge needs.  upsert_staging writes the
-- canonical columns; lib/db.update_staging_attrs fills attrs.
--
-- natural_key = license number.  segment = license_kind: firm (CAB) or
-- individual (CAM).  attrs: license type and status, DBA brands (';'
-- separated in the source), mailing address, county code.  Individual
-- licensees are kept for the CRM contact link only; F5 has no employer.

CREATE TABLE IF NOT EXISTS staging.fl_dbpr_cam (
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

CREATE INDEX IF NOT EXISTS idx_fl_dbpr_cam_zip5 ON staging.fl_dbpr_cam (zip5);
