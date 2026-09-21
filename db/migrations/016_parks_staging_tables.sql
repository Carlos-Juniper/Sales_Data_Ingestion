-- Parks vertical staging tables — one per source in park_layers.yaml.
-- Same 21-column canonical shape as all other staging tables
-- (source_id, natural_key, ..., geom, loaded_at).
--
-- Sources:
--   padus_parks       — PAD-US 4.1 LOC records (5-state filter), USGS national layer
--   pasda_dcnr_parks  — Pennsylvania DCNR local parks (PASDA MapServer layer 18)
--   tpwd_state_parks  — Texas State Parks Boundaries (TPWD FeatureServer)
--   fdep_state_parks  — Florida State Park Boundaries (FDEP MapServer)
--   nc_state_parks    — NC State Parks System (NCStateParks_NCDPR FeatureServer)
--   sc_state_parks    — South Carolina State Parks (SCPRT FeatureServer)

CREATE TABLE IF NOT EXISTS staging.padus_parks (
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
  PRIMARY KEY (source_id, natural_key)
);

CREATE TABLE IF NOT EXISTS staging.pasda_dcnr_parks (
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
  PRIMARY KEY (source_id, natural_key)
);

CREATE TABLE IF NOT EXISTS staging.tpwd_state_parks (
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
  PRIMARY KEY (source_id, natural_key)
);

CREATE TABLE IF NOT EXISTS staging.fdep_state_parks (
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
  PRIMARY KEY (source_id, natural_key)
);

CREATE TABLE IF NOT EXISTS staging.nc_state_parks (
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
  PRIMARY KEY (source_id, natural_key)
);

CREATE TABLE IF NOT EXISTS staging.sc_state_parks (
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
  PRIMARY KEY (source_id, natural_key)
);
