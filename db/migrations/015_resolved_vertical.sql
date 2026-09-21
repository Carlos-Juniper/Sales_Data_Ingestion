-- D15: Add vertical discriminator to staging.resolved_location and
-- staging.resolved_contact so each driver can scope its own DELETE to its
-- own vertical slice before upserting.
--
-- staging.resolved_account already has vertical TEXT NOT NULL (migration 011).
-- The two sibling tables do not — this migration adds the column and matching
-- indexes to all three tables for consistency and query performance.
--
-- The healthcare and deathcare drivers already stamp vertical='healthcare' /
-- vertical='deathcare' on every row they write to resolved_account; they now
-- do the same on resolved_location and resolved_contact.
--
-- Without this column the DELETE ... WHERE vertical = :v scoping required by
-- D15 is impossible on location and contact rows.

ALTER TABLE staging.resolved_location
  ADD COLUMN IF NOT EXISTS vertical TEXT;

ALTER TABLE staging.resolved_contact
  ADD COLUMN IF NOT EXISTS vertical TEXT;

-- Indexes support the DELETE ... WHERE vertical = :v that each driver issues
-- before upserting.  All three tables get an index for symmetry and because
-- the delete is a table-scan without one.
CREATE INDEX IF NOT EXISTS idx_resolved_account_vertical
  ON staging.resolved_account (vertical);

CREATE INDEX IF NOT EXISTS idx_resolved_location_vertical
  ON staging.resolved_location (vertical);

CREATE INDEX IF NOT EXISTS idx_resolved_contact_vertical
  ON staging.resolved_contact (vertical);
