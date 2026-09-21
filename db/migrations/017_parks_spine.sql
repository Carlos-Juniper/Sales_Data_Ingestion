-- Parks vertical, part 2: the government account spine and the polygon side tables.
--
-- Migration 016 landed the six park *source* staging tables.  This migration adds
-- what turns those parks into a sellable account list, per Ingestion-Plan-of-Action
-- §6.4: "the municipality is the account; parks are child locations."
--
-- Three canonical staging tables (same 21-column shape as every other staging
-- table) for the Census TIGERweb government units:
--   tiger_places    — incorporated places, 5 states, FUNCSTAT='A'   (~3,476 rows)
--   tiger_cousub    — PA active county subdivisions (townships)     (~1,543 rows)
--   tiger_counties  — counties, 5 states                            (   534 rows)
--
-- Row counts verified live against the TIGERweb REST endpoints 2026-09-04.
-- tiger_cousub is PA-only on purpose: Pennsylvania is the one target state where
-- townships are general-purpose governments.  In the MCD layer only COUSUBCC='T1'
-- carries FUNCSTAT='A' (townships); PA cities and boroughs come through as
-- FUNCSTAT='F' because their incorporated-place record is authoritative.  So
-- places ∪ active-MCDs is a clean union with no double counting.
--
-- Plus three side tables.  Boundaries live here rather than in the canonical
-- staging tables because lib/db.upsert_staging's 21-column contract is shared
-- infrastructure and its geom column is geometry(Point,4326).  Precedent:
-- staging.enrich_parcel already carries a boundary column this way (migration 012).

-- ---------------------------------------------------------------------------
-- Government unit staging tables — canonical 21-column shape
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS staging.tiger_places (
  source_id        text NOT NULL,
  natural_key      text NOT NULL,          -- Census GEOID (Tier-1 match key, §5.1)
  vertical         text,
  account_type     text,                   -- 'municipality'
  name_raw         text,
  name_normalized  text,
  address_line_1   text,
  city             text,
  state            text,
  zip5             text,
  phone_raw        text,
  phone_normalized text,
  latitude         numeric,                -- TIGER INTPTLAT (internal point)
  longitude        numeric,                -- TIGER INTPTLON
  geom             geometry(Point, 4326),
  segment          text,
  ein              text,
  county_fips      text,                   -- NULL for places: a place may straddle counties
  size_metric      text,
  size_value        numeric,               -- AREALAND converted to acres
  size_unit        text,
  source_file      text,
  loaded_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_id, natural_key)
);

CREATE TABLE IF NOT EXISTS staging.tiger_cousub (
  source_id        text NOT NULL,
  natural_key      text NOT NULL,
  vertical         text,
  account_type     text,                   -- 'municipality'
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
  county_fips      text,                   -- STATE || COUNTY, 5-digit
  size_metric      text,
  size_value       numeric,
  size_unit        text,
  source_file      text,
  loaded_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_id, natural_key)
);

CREATE TABLE IF NOT EXISTS staging.tiger_counties (
  source_id        text NOT NULL,
  natural_key      text NOT NULL,
  vertical         text,
  account_type     text,                   -- 'county'
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
  county_fips      text,                   -- STATE || COUNTY, 5-digit
  size_metric      text,
  size_value       numeric,
  size_unit        text,
  source_file      text,
  loaded_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_id, natural_key)
);

-- ---------------------------------------------------------------------------
-- staging.gov_unit_boundary — government polygons for the spatial rollup
-- ---------------------------------------------------------------------------
-- One row per government unit, keyed the same way as the staging tables above so
-- the join is trivial.  area_acres is computed by PostGIS from the UNSIMPLIFIED
-- geometry at write time; boundary stores the simplified version.
CREATE TABLE IF NOT EXISTS staging.gov_unit_boundary (
  source_id   text NOT NULL,
  natural_key text NOT NULL,               -- Census GEOID
  boundary    geometry(MultiPolygon, 4326),
  area_acres  numeric,
  loaded_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_id, natural_key)
);

-- GiST index is load-bearing, not an optimisation: manager_resolve.py's
-- ST_Intersects join between ~71.7K parks and ~5.5K governments is a
-- cross join without it.
CREATE INDEX IF NOT EXISTS idx_gov_unit_boundary_boundary
  ON staging.gov_unit_boundary USING gist (boundary);

-- ---------------------------------------------------------------------------
-- staging.park_attrs — park polygons, acreage cross-check, and manager strings
-- ---------------------------------------------------------------------------
-- The three things park_layers.py fetches (or can derive) but the canonical
-- 21-column shape has nowhere to put:
--
--   boundary        — the real park polygon, simplified to ~1 m
--   acres_computed  — ST_Area(unsimplified::geography)/4046.856, an INDEPENDENT
--                     measurement to cross-check acres_published against
--   owner_raw /     — PAD-US Own_Name / Loc_Mang and equivalents.  These free-text
--   manager_raw       strings are the input to the §5.4 manager→GEOID join, the
--                     hardest part of this vertical.
--
-- acres_published is copied from the source's own pre-calculated area field
-- (GIS_Acres / ACREAGE / ACRES / Area_Acres / Shape__Area→acres) so that the
-- comparison against acres_computed survives independently of the staging row.
CREATE TABLE IF NOT EXISTS staging.park_attrs (
  source_id          text NOT NULL,
  natural_key        text NOT NULL,
  owner_raw          text,
  manager_raw        text,
  manager_normalized text,
  acres_published    numeric,
  acres_computed     numeric,
  boundary           geometry(MultiPolygon, 4326),
  loaded_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_id, natural_key)
);

CREATE INDEX IF NOT EXISTS idx_park_attrs_boundary
  ON staging.park_attrs USING gist (boundary);

-- Supports the manager-name blocking pass in manager_resolve.py Stage B.
CREATE INDEX IF NOT EXISTS idx_park_attrs_manager_normalized
  ON staging.park_attrs USING gin (manager_normalized gin_trgm_ops);

-- ---------------------------------------------------------------------------
-- staging.park_rollup — the auditable park → government assignment
-- ---------------------------------------------------------------------------
-- Written by manager_resolve.py, read by parks_merge.py.  Kept as its own table
-- rather than a column on park_attrs for three reasons: the assignment is derived
-- from a different input set than the park attributes, it is the single most
-- review-worthy output of this vertical (§5.4 warns this join is where the
-- vertical is most likely to slip), and re-running the resolver must not risk
-- clobbering harvested source data.
--
-- method values:
--   spatial+name    — polygon overlap and manager-name match agree (highest confidence)
--   spatial         — polygon overlap only
--   name            — manager-name match only, score >= 0.75
--   county_fallback — no municipal match; assigned to the containing county
--
-- county_fallback is a real answer, not a failure: counties tile the state, and a
-- park in an unincorporated area genuinely is a county responsibility.  It also
-- guarantees every park gets a parent, which staging.resolved_location's NOT NULL
-- account_key requires.
CREATE TABLE IF NOT EXISTS staging.park_rollup (
  park_source_id   text NOT NULL,
  park_natural_key text NOT NULL,
  gov_source_id    text NOT NULL,
  gov_natural_key  text NOT NULL,          -- Census GEOID of the winning government
  method           text NOT NULL,
  score            numeric,                -- name-match score when method involves 'name'
  loaded_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (park_source_id, park_natural_key)
);

CREATE INDEX IF NOT EXISTS idx_park_rollup_gov
  ON staging.park_rollup (gov_source_id, gov_natural_key);

CREATE INDEX IF NOT EXISTS idx_park_rollup_method
  ON staging.park_rollup (method);
