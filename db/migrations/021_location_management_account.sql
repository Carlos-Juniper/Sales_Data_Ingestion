-- Firm <-> property link (docs/hoa-pipeline.md §3).
--
-- A management company is its own core.account (account_type
-- 'management_company'); the property it manages points at it from the
-- location.  Mirrors the CRM's properties.management_company_id, so the
-- cutover maps column to column.  Texas can fill it from the TREC
-- certificate's management company; Florida from the F7 C/O line and the
-- DBPR managing entity, matched to the DBPR CAM firm license.
--
-- staging.resolved_location carries the firm's account_key; core_writer
-- resolves it to account_id when it writes core.location.  NULL for every
-- vertical that has no management firm, so their content hashes (concat_ws
-- skips NULLs) and the core_apply diff are unchanged.

ALTER TABLE core.location
  ADD COLUMN IF NOT EXISTS management_account_id bigint REFERENCES core.account(account_id);

CREATE INDEX IF NOT EXISTS idx_location_management_account
  ON core.location (management_account_id)
  WHERE management_account_id IS NOT NULL;

ALTER TABLE staging.resolved_location
  ADD COLUMN IF NOT EXISTS management_account_key text;
