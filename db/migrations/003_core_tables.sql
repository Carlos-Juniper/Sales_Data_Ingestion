-- The buying entity. Who signs the contract.
CREATE TABLE core.account (
  account_id        bigserial PRIMARY KEY,
  vertical          text NOT NULL,          -- hoa|deathcare|healthcare|parks|resort
  account_type      text,                   -- association|operator|health_system|
                                            -- municipality|county|school_district|
                                            -- ownership_group|single_site
  legal_name        text NOT NULL,
  name_normalized   text NOT NULL,
  dba_name          text,
  parent_account_id bigint REFERENCES core.account(account_id),  -- system rollup
  mailing_address   jsonb,                  -- parsed components
  phone             text,
  email             text,
  website           text,
  status            text,                   -- active|inactive|dissolved|unknown
  external_keys     jsonb,                  -- {ccn, npi, geoid, leaid, sunbiz_doc,
                                            --  trec_cert_id, ein, license_no}
  size_metric       numeric,                -- beds | units | acres | rooms
  size_metric_unit  text,
  confidence        numeric,                -- 0..1
  first_seen        timestamptz NOT NULL DEFAULT now(),
  last_seen         timestamptz NOT NULL DEFAULT now()
);

-- The serviceable physical site. What a crew drives to.
CREATE TABLE core.location (
  location_id       bigserial PRIMARY KEY,
  account_id        bigint REFERENCES core.account(account_id),
  location_name     text,
  site_address      jsonb,
  geom              geometry(Point, 4326),
  boundary          geometry(MultiPolygon, 4326),  -- when available
  geocode_precision text,      -- rooftop|parcel|street|zip|centroid
  geometry_source   text,      -- parcel|padus|hub|nsd|geocoded
  maintained_acres  numeric,
  acres_confidence  text,      -- measured|estimated|banded
  site_type         text,      -- park|ballfield|median|campus|cemetery|common_area
  first_seen        timestamptz NOT NULL DEFAULT now(),
  last_seen         timestamptz NOT NULL DEFAULT now()
);

-- People. Kept separate because sources disagree and roles change.
CREATE TABLE core.contact (
  contact_id     bigserial PRIMARY KEY,
  account_id     bigint NOT NULL REFERENCES core.account(account_id),
  full_name      text,
  role           text,        -- registered_agent|officer|director|facilities_dir|
                              -- parks_dir|procurement|gm|manager
  role_rank      int,         -- 1 = best guess at decision maker
  phone          text,
  email          text,
  address        jsonb,
  source_id      text NOT NULL,
  is_current     boolean DEFAULT true
);

-- Append-only provenance. One row per (source, source record, ingest run).
CREATE TABLE core.source_record (
  source_record_id bigserial PRIMARY KEY,
  source_id        text NOT NULL,
  source_run_id    bigint NOT NULL,
  natural_key      text NOT NULL,   -- source's own id
  payload          jsonb NOT NULL,  -- full original record
  account_id       bigint REFERENCES core.account(account_id),
  location_id      bigint REFERENCES core.location(location_id),
  match_score      numeric,
  match_method     text,            -- exact_key|deterministic|fuzzy|manual
  UNIQUE (source_id, natural_key, source_run_id)
);

CREATE INDEX ON core.account USING gin (name_normalized gin_trgm_ops);
CREATE INDEX ON core.location USING gist (geom);
CREATE INDEX ON core.location USING gist (boundary);
