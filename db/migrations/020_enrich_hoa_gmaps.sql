-- D6 extension: Google Places supplemental enrichment for the HOA vertical.
--
-- enrich_hoa_gmaps -- output of hoa_gmaps_enrich.py
--   Looks up each TX TREC HOA association's phone/website via the Places API
--   Text Search (New) endpoint, using name + city + state + zip -- there is
--   no street address available at this pass, since a street address only
--   comes from the certificate-PDF OCR pass (staging.enrich_hoa_pdf_contact,
--   018/019_enrich_hoa_pdf_contact*.sql).
--
--   Runs AFTER that OCR pass and defers to it: per
--   Ingestion-Plan-of-Action.md Sec.5.3, a state regulator's own filing
--   outranks a Places API guess, so hoa_gmaps_enrich.py skips any
--   association staging.enrich_hoa_pdf_contact already produced a usable
--   mgmt_phone/mgmt_email for. Places fills the gap for associations OCR
--   came back empty on, and supplies `website`, which the certificate often
--   lacks.
--
--   phone / website / maps_place_id: from the best-matched Places Text
--     Search result (see connectors/lib/google_places.py::best_match).
--   contact_email: first mailto: address found while crawling the returned
--     website's /contact, /contact-us, /about, or homepage (see
--     connectors/lib/website_contact.py) -- only attempted when a website
--     was found.
--   enrich_status: ok|not_found|error|skipped (lib/enums.py). 'not_found'
--     covers both zero Places results and a matched place with neither
--     phone nor website populated -- nothing usable to write either way.
--
-- Same (source_id, natural_key) PK convention as every other staging.enrich_*
-- table (see 012_enrich_tables.sql, 014_enrich_irs990_status.sql,
-- 018_enrich_hoa_pdf_contact.sql) so a JOIN against staging.tx_trec_hoa is a
-- plain equi-join on both columns. ON CONFLICT DO UPDATE makes re-runs a
-- no-op when Places returns the same result, and refreshes cached values
-- when it doesn't.
--
-- Numbered 020 (not 018) because 018/019 were taken by the TREC PDF OCR
-- pass, which landed on main after this table was first drafted.

CREATE TABLE IF NOT EXISTS staging.enrich_hoa_gmaps (
  source_id      text NOT NULL,
  natural_key    text NOT NULL,
  phone          text,
  website        text,
  maps_place_id  text,
  contact_email  text,
  enrich_status  text,
  enriched_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_id, natural_key)
);
