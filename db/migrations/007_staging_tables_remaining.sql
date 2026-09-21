-- Remaining per-source staging tables — same shape as 006 (21 CANONICAL_COLUMNS
-- + geom + loaded_at, PK (source_id, natural_key)).
--
-- Verticals covered here:
--   deathcare : fgdl_cemeteries, txdot_cemeteries, usgs_nsd,
--               va_cemeteries, irs_bmf_deathcare
--   hoa       : tx_trec_hoa
--   resort    : fl_dbpr_lodging
--
-- parks was not represented when this migration was written; its staging tables
-- arrived later in 016 (six park sources) and 017 (the TIGERweb government spine).

-- FGDL Cemeteries (connectors/healthcare/fgdl_cemeteries.py — deathcare vertical)
CREATE TABLE IF NOT EXISTS staging.fgdl_cemeteries (
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

-- TxDOT Cemeteries (connectors/healthcare/txdot_cemeteries.py — deathcare vertical)
CREATE TABLE IF NOT EXISTS staging.txdot_cemeteries (
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

-- USGS National Structures Dataset — cemeteries
-- (connectors/healthcare/usgs_nsd.py — deathcare vertical)
CREATE TABLE IF NOT EXISTS staging.usgs_nsd (
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

-- VA National Cemetery Administration
-- (connectors/healthcare/va_cemeteries.py — deathcare vertical)
CREATE TABLE IF NOT EXISTS staging.va_cemeteries (
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

-- IRS Business Master File — deathcare subset
-- (connectors/healthcare/irs_bmf_deathcare.py — deathcare vertical)
CREATE TABLE IF NOT EXISTS staging.irs_bmf_deathcare (
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

-- Texas TREC HOA registrations
-- (connectors/hoa/tx_trec_hoa.py — hoa vertical)
CREATE TABLE IF NOT EXISTS staging.tx_trec_hoa (
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

-- Florida DBPR licensed lodging (resorts/hotels)
-- (connectors/resort/fl_dbpr_lodging.py — resort vertical)
CREATE TABLE IF NOT EXISTS staging.fl_dbpr_lodging (
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
