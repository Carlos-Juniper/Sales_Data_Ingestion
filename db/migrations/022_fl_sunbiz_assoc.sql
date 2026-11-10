-- Florida Sunbiz domestic non-profit corporations (F1), every active DOMNP.
-- segment = 'association_name' for the §6 step 1 name-regex hits and
-- 'nonprofit_other' for the rest, which the §4 recall rules (code-009 parcel
-- owner, CAM-firm registered agent) still need to resolve against.
-- Loaded by connectors/hoa/fl_sunbiz_assoc.py (docs/hoa-pipeline.md §5).
--
-- Same 21-column canonical shape as every other staging table, plus attrs
-- (jsonb) for source fields the merge needs.  upsert_staging writes the
-- canonical columns; lib/db.update_staging_attrs fills attrs.
--
-- natural_key = Sunbiz document number.  attrs holds the fields the canonical
-- shape has no column for: filing type, status and date, FEI, mailing
-- address, registered agent (name, kind, ZIP), officers (title, name, ZIP,
-- as a list), the C/O firm parsed from the principal/mailing address, and
-- last annual-report year.  Officer names are personal data: they feed
-- resolved_contact only and are never copied into docs or logs.

CREATE TABLE IF NOT EXISTS staging.fl_sunbiz_assoc (
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

CREATE INDEX IF NOT EXISTS idx_fl_sunbiz_assoc_zip5 ON staging.fl_sunbiz_assoc (zip5);
