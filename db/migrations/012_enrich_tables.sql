-- D6: Enrichment cache tables — one per enrichment source.
--
-- All three tables are keyed on (source_id, natural_key), matching the staging.*
-- primary key so a JOIN is trivial.  ON CONFLICT DO UPDATE makes re-runs a
-- no-op when enrichment output is unchanged, and updates cached values when
-- the upstream enricher returns something different.
--
-- enrich_geocode — output of geocode_enrich.py
--   lat/lon stored as bare numerics (mirroring staging.* latitude/longitude) AND
--   as a PostGIS point so ST_DWithin works directly on this table.
--   precision: rooftop|parcel|street|zip|centroid  (matches core.location.geocode_precision)
--   source:    provider name, e.g. "google", "census", "arcgis"
--   match_type: exact|interpolated|approximate|centroid
--
-- enrich_parcel — output of sc_parcel_ingest.py / parcel enrichment
--   maintained_acres: numeric acreage from parcel record
--   boundary: polygon from parcel record (MultiPolygon to match core.location.boundary)
--
-- enrich_irs990 — output of irs_990_enrich.py
--   phone_990:        phone number from Form 990 filing
--   contact_name_990: contact name from Form 990 filing (usually a director/officer)

-- ---------------------------------------------------------------------------
-- staging.enrich_geocode
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staging.enrich_geocode (
  source_id    text      NOT NULL,
  natural_key  text      NOT NULL,
  latitude     numeric,
  longitude    numeric,
  geom         geometry(Point, 4326),
  precision    text,                         -- rooftop|parcel|street|zip|centroid
  source       text,                         -- geocode provider, e.g. 'google'
  match_type   text,                         -- exact|interpolated|approximate|centroid
  enriched_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_id, natural_key)
);

-- ---------------------------------------------------------------------------
-- staging.enrich_parcel
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staging.enrich_parcel (
  source_id        text      NOT NULL,
  natural_key      text      NOT NULL,
  maintained_acres numeric,
  boundary         geometry(MultiPolygon, 4326),
  enriched_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_id, natural_key)
);

CREATE INDEX IF NOT EXISTS idx_enrich_parcel_boundary
  ON staging.enrich_parcel USING gist (boundary);

-- ---------------------------------------------------------------------------
-- staging.enrich_irs990
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staging.enrich_irs990 (
  source_id         text NOT NULL,
  natural_key       text NOT NULL,
  phone_990         text,
  contact_name_990  text,
  enriched_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_id, natural_key)
);
