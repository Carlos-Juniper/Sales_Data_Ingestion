-- D6: Enrichment cache for TX TREC management-certificate PDF OCR.
--
-- Output of connectors/hoa/tx_trec_pdf_enrich.py. Keyed on
-- (source_id, natural_key) like staging.enrich_irs990 / enrich_geocode /
-- enrich_parcel so the JOIN to staging.tx_trec_hoa is:
--
--   USING (source_id, natural_key)
--
-- source_id is the upstream connector id ('tx_trec_hoa'), not the enrich
-- connector id. natural_key is the TREC association_id. certificate_id is
-- the filing id from the certificate URL; a new filing updates the same
-- association row rather than inserting a second one.
--
-- ON CONFLICT DO UPDATE rewrites the cached parse when the OCR output
-- changes, and the WHERE ... IS DISTINCT FROM clause makes an unchanged
-- re-run a real no-op (enriched_at stays put).
--
-- enrich_status: ok | not_found | error | skipped
-- needs_review:  true when a numbered field anchor was missing, a label-text
--                fallback was used, field 6 had no phone or email match, or
--                the anchors were out of order. That is the review queue —
--                SELECT ... WHERE needs_review ORDER BY confidence.

-- ---------------------------------------------------------------------------
-- staging.enrich_hoa_pdf_contact
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staging.enrich_hoa_pdf_contact (
  source_id              text NOT NULL,
  natural_key            text NOT NULL,
  certificate_id         text,
  certificate_url        text,
  assoc_mailing_address  text,
  rep_name               text,
  rep_mailing_address    text,
  rep_phone              text,
  rep_phone_normalized   text,
  rep_email              text,
  website                text,
  field_6_raw            text,
  enrich_status          text,
  confidence             numeric,
  needs_review           boolean NOT NULL DEFAULT false,
  review_reasons         text,
  ocr_text_sha256        text,
  raw_pdf_sha256         text,
  raw_pdf_uri            text,
  error_detail           text,
  enriched_at            timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_id, natural_key)
);

-- Review queue: low-confidence / flagged parses, cheapest rows first.
CREATE INDEX IF NOT EXISTS idx_enrich_hoa_pdf_contact_review
  ON staging.enrich_hoa_pdf_contact (source_id, confidence)
  WHERE needs_review;
