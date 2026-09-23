-- Split staging.enrich_hoa_pdf_contact into association vs management columns.
--
-- Field 5 ("Name and mailing address of the Association") was stored as one
-- joined blob in assoc_mailing_address. Field 6 (designated representative /
-- management company) was stored as rep_*. Product columns:
--
--   hoa_name, hoa_mailing_address
--       field 5, split with the same name/address helper the parser uses
--       for field 6
--   mgmt_name, mgmt_mailing_address, mgmt_phone, mgmt_phone_normalized,
--   mgmt_email
--       field 6, copied from rep_*
--
-- assoc_mailing_address and rep_* remain in place as deprecated aliases for
-- one transition. This migration only adds columns. The parser dual-writes
-- both sets. connectors/hoa/backfill_hoa_pdf_contact.py fills the new
-- columns from rows already stored — no PDF download and no re-OCR.
--
-- enriched_at is intentionally untouched here. The backfill UPDATE also
-- leaves enriched_at in place so a column copy is not a new enrich run.

ALTER TABLE staging.enrich_hoa_pdf_contact
  ADD COLUMN IF NOT EXISTS hoa_name text,
  ADD COLUMN IF NOT EXISTS hoa_mailing_address text,
  ADD COLUMN IF NOT EXISTS mgmt_name text,
  ADD COLUMN IF NOT EXISTS mgmt_mailing_address text,
  ADD COLUMN IF NOT EXISTS mgmt_phone text,
  ADD COLUMN IF NOT EXISTS mgmt_phone_normalized text,
  ADD COLUMN IF NOT EXISTS mgmt_email text;

COMMENT ON COLUMN staging.enrich_hoa_pdf_contact.hoa_name IS
  'Field 5 association name. Split from the certificate block.';
COMMENT ON COLUMN staging.enrich_hoa_pdf_contact.hoa_mailing_address IS
  'Field 5 association mailing address. Split from the certificate block.';
COMMENT ON COLUMN staging.enrich_hoa_pdf_contact.mgmt_name IS
  'Field 6 management company / designated representative name.';
COMMENT ON COLUMN staging.enrich_hoa_pdf_contact.mgmt_mailing_address IS
  'Field 6 management company / designated representative mailing address.';
COMMENT ON COLUMN staging.enrich_hoa_pdf_contact.mgmt_phone IS
  'Field 6 phone as printed on the certificate.';
COMMENT ON COLUMN staging.enrich_hoa_pdf_contact.mgmt_phone_normalized IS
  'Field 6 phone digits, same normalization as rep_phone_normalized.';
COMMENT ON COLUMN staging.enrich_hoa_pdf_contact.mgmt_email IS
  'Field 6 email for the designated representative.';

COMMENT ON COLUMN staging.enrich_hoa_pdf_contact.assoc_mailing_address IS
  'DEPRECATED alias. Joined field-5 blob. Prefer hoa_name and hoa_mailing_address.';
COMMENT ON COLUMN staging.enrich_hoa_pdf_contact.rep_name IS
  'DEPRECATED alias of mgmt_name.';
COMMENT ON COLUMN staging.enrich_hoa_pdf_contact.rep_mailing_address IS
  'DEPRECATED alias of mgmt_mailing_address.';
COMMENT ON COLUMN staging.enrich_hoa_pdf_contact.rep_phone IS
  'DEPRECATED alias of mgmt_phone.';
COMMENT ON COLUMN staging.enrich_hoa_pdf_contact.rep_phone_normalized IS
  'DEPRECATED alias of mgmt_phone_normalized.';
COMMENT ON COLUMN staging.enrich_hoa_pdf_contact.rep_email IS
  'DEPRECATED alias of mgmt_email.';
