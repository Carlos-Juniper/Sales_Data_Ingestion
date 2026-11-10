-- Florida code-009 common-area parcels (F6 NAL owner + F7 statewide parcel geometry).
-- Loaded by connectors/hoa/fl_common_area_parcels.py (docs/hoa-pipeline.md §5).
--
-- Same 21-column canonical shape as every other staging table, plus attrs
-- (jsonb) for source fields the merge needs.  upsert_staging writes the
-- canonical columns; lib/db.update_staging_attrs fills attrs.
--
-- natural_key = '<county number>:<parcel id>'.  attrs: owner name and
-- mailing lines, the C/O firm parsed from them, owner class from §6 step 3
-- (association, cdd, company, other), land square feet, and the boundary
-- as GeoJSON for the merge (resolved_location.boundary).

CREATE TABLE IF NOT EXISTS staging.fl_common_area_parcel (
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

CREATE INDEX IF NOT EXISTS idx_fl_common_area_parcel_zip5 ON staging.fl_common_area_parcel (zip5);
