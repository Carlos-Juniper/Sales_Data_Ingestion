-- D6 follow-up: irs_990_enrich.py writes an enrich_status column
-- ('ok'|'not_found'|'error'|'skipped') that migration 012 never added to
-- staging.enrich_irs990.

ALTER TABLE staging.enrich_irs990
  ADD COLUMN IF NOT EXISTS enrich_status text;
