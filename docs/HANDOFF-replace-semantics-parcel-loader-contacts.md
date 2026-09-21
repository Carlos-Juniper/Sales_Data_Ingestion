# Handoff: driver replace-semantics, parcel-cache loader, contact/reachability wiring

**Audience:** the engineer/agent implementing this. Read this whole file before writing code.
**Prerequisite reading:** `docs/HANDOFF-tier3-review-parcel-nan-va.md` (the prior
round — D10–D14; its §4 code changes are already implemented and green),
`docs/healthcare-pipeline.md`, `docs/deathcare-pipeline.md`,
`docs/pipeline-stats-pre-post-run.md`.

This round exists because verifying the prior handoff against the live
`juniper-postgres-prod` database surfaced three problems the prior handoff
either got wrong (D14) or didn't cover (parcel writer, contacts). All
file:line citations and every number below were pulled from the live DB and
current code on **2026-08-20** — not inferred from docs.

---

## 0. Verified data baseline (read-only pull, 2026-08-20, `core.*`)

`core.account` = 82,125 rows (34,701 deathcare + 47,424 healthcare).
`core.location` = 82,504. `core.contact` = 168.

Null / missing rates that matter (blank string counted as missing):

| Column | Missing | Note |
|---|---|---|
| `account.email` | **100%** | no source email ever lands |
| `account.website` | **100%** | never populated |
| `account.confidence` | **100%** | out of scope (deferred, see §5) |
| `account.phone` | **99.8%** | only 168 rows, all deathcare synthetic |
| `account.external_keys` | 96.7% | 2,722 have EIN; `ccn`/`npi` = **0** |
| `account.size_metric` | 57.7% null **+** 31,068 stored as literal `NaN` | real values only 3,633 (4.4%) — NaN fix from prior round not yet re-run |
| `location.maintained_acres` | **100%** | parcel cache empty — see §2 |
| `location.acres_confidence` / `boundary` | **100%** | same |
| `contact` table | 168 rows total, **0.20%** account coverage; `full_name`/`email`/`address` 100% null | all synthetic `primary_phone` — see §3 |

The single largest, most business-relevant gap for a sales/leadership report
is **reachability** (phone/email/website/contacts) — and, critically, the raw
data for it already exists in staging. It is a wiring gap, not a
source-data gap. That is §3, and it's the highest-value item here.

---

## 1. Fix driver replace-semantics (the D14 tombstone bug)

**D14 in the prior handoff is wrong.** It claimed re-running the state-narrowed
healthcare pipeline would "naturally drop out-of-state rows from
`staging.resolved_account`, and the 3-way diff tombstones them." It won't, and
here's the verified chain:

- Both merge drivers write the resolved tables with **`ON CONFLICT
  (account_key) DO UPDATE`** — pure upsert:
  `connectors/healthcare/healthcare_pipeline.py:560` and
  `connectors/deathcare/deathcare_merge.py:762` (and the `location_key` /
  `contact_key` equivalents right below each).
- **Nothing anywhere clears `staging.resolved_*`** — grepped repo-wide
  (`connectors/`, `db/`): no `TRUNCATE`, no `DELETE FROM staging.resolved*`,
  no clear step in either driver.
- The tombstone rule (`connectors/lib/core_writer.py:251-257`, the `acct_gone`
  CTE inside `_build_dry_run_sql`, used by both `dry_run_diff` at
  `core_writer.py:182` and `apply_core_diff` at `:322`) is:
  ```sql
  FROM core.account ca
  LEFT JOIN staging.resolved_account ra ON ra.account_key = ca.account_key
  WHERE ra.account_key IS NULL AND ca.status <> 'merged'
  ```
  i.e. a core row is tombstoned only when its `account_key` is **absent** from
  `staging.resolved_account`.

**Consequence:** the ~40k out-of-state healthcare rows from the last nationwide
run are still sitting in `staging.resolved_account`. Re-running the 5-state
pipeline upserts the FL/NC/TX/PA/SC subset *on top of them* and deletes
nothing — so those 40k `account_key`s still match, and the diff shows **~0
tombstones instead of ~40k**. If applied, the 5-state narrowing (D10) silently
never takes effect. `staging.resolved_*` grows monotonically and can never
represent a *deletion*.

**Decision D15:** each merge driver must **replace its own vertical's rows** in
`staging.resolved_account` / `resolved_location` / `resolved_contact` on every
run, not just upsert. The resolved tables must represent the *current* resolved
set for that vertical, so that a row disappearing upstream actually disappears
downstream and tombstones correctly.

**Fix:**
1. Add a `vertical` discriminator so each driver can scope its own delete.
   `core.account` already has a `vertical` column (verified present). Confirm
   `staging.resolved_account` carries one too (`db/migrations/011_resolved_tables.sql`);
   if not, add it in a new migration and have each driver stamp it
   (`'healthcare'` / `'deathcare'`) on every resolved row it builds.
2. In each driver's `run_pipeline()`, inside the same transaction as the
   upserts and gated by the existing `dry_run` guard, `DELETE FROM
   staging.resolved_account WHERE vertical = :v` (and the location/contact
   tables) **before** the `ON CONFLICT` upserts. Net effect: full replace of
   that vertical's slice, other vertical untouched.
   - Do the delete + upsert in **one transaction** so a mid-run failure can't
     leave the table empty (which would make `core_apply` try to tombstone
     everything).
3. Do **not** switch the whole thing to `TRUNCATE` — that would wipe the other
   vertical. Scope by `vertical`.

**Alternative considered & rejected:** a one-time manual `TRUNCATE
staging.resolved_*` + full rebuild of both verticals. It produces a correct
one-shot diff but leaves the latent bug in place for every future run. D15 is
the durable fix.

**Test:** seed `staging.resolved_account` with an out-of-vertical-scope /
out-of-state `account_key`, run the driver, assert that key is gone from
`staging.resolved_account` afterward (not merely not-updated), and that the
*other* vertical's rows are untouched. Then a `core_apply.dry_run_diff` over
that state reports it as a tombstone.

**Verification (before any prod apply):** after re-running the 5-state
healthcare pipeline, `core_apply.dry_run_diff` account-tombstone count should
roughly equal (nationwide healthcare) − (5-state healthcare) ≈ tens of
thousands. If it's ~0, the delete didn't fire.

---

## 2. Build the `staging.enrich_parcel` loader (parcel cache has no writer)

**Confirmed:** `staging.enrich_parcel` is empty and **nothing in the codebase
writes it.** The only references are the *readers* added last round
(`join_parcel` at `healthcare_pipeline.py:315`, `_join_parcel` at
`deathcare_merge.py:549`) plus their tests. The producers only emit CSV:

- `connectors/healthcare/parcel_acreage_enrich.py` — **no `--write-db` flag**,
  CSV output only.
- `connectors/healthcare/sc_parcel_ingest.py` — same, CSV only (and its SC
  county assessor input CSVs aren't on disk anyway).

So `join_parcel()` (correct as written) reads an empty table forever and
`maintained_acres` stays 100% null. The prior round built the consumer against
a producer→staging link that doesn't exist.

**Decision D16:** add a loader that writes the parcel enrichers' output into
`staging.enrich_parcel`, so the already-built join has data. Keep scope to the
5 registered states (D12) — no new ArcGIS layers.

**Fix (pick the smaller of these — implementer's call, see §6):**
- **(a)** Add a `--write-db` path to `parcel_acreage_enrich.py` (and
  `sc_parcel_ingest.py`) that upserts rows into `staging.enrich_parcel` via the
  shared `upsert_staging()` helper in `connectors/lib/db.py`, keyed on
  `(source_id, natural_key)` (the enrich tables' PK per
  `db/migrations/012_enrich_tables.sql`). This mirrors how `geocode_enrich.py`
  already writes `staging.enrich_geocode`.
- **(b)** A thin separate `parcel_cache_load.py` that reads the enricher CSVs
  and upserts them. Only if you don't want to touch the enrichers.

`staging.enrich_parcel` columns (migration 012): `source_id`, `natural_key`,
`maintained_acres`, `boundary GEOMETRY(MultiPolygon,4326)`, `enriched_at`.
`acres_confidence` / `geometry_source` are **not** stored — `join_parcel`
supplies them as the constants `'estimated'` / `'parcel'` (matching
`parcel_acreage_enrich.py`'s `ParcelResult` defaults). Don't add columns the
join doesn't read.

**Test:** loader upserts a CSV row into `staging.enrich_parcel`; a second load
of the same `(source_id, natural_key)` updates rather than duplicates
(idempotency, D6). Then `join_parcel` populates `maintained_acres` for that
`natural_key`.

**Note:** SC (`sc_parcel_ingest.py`) also needs the county assessor CSVs, which
are not on disk. Loading SC is blocked on obtaining those; FL/NC/TX/PA can
proceed via `parcel_acreage_enrich.py`. Flag SC as data-blocked, don't silently
skip it.

---

## 3. Wire contact / reachability data (the highest-value gap)

**The data exists in staging and is being thrown away.** Verified:

- **Every** healthcare source table in staging has `phone_normalized`
  (`cms_general`, `cms_nursing_home`, `nppes_practice_locations`,
  `va_facilities`) — yet healthcare produces **0 contacts and 0
  `account.phone`** in `core`.
- `staging.enrich_irs990` carries **`phone_990` and `contact_name_990`** for
  the 2,722 religious-segment orgs — yet **`enrich_irs990` is never referenced
  in `deathcare_merge.py`** (grep returns zero hits). The enrichment connector
  runs, writes the cache, and the merge never joins it. Real named contacts
  with phones, dropped.
- The only 168 `core.contact` rows are synthetic `primary_phone` placeholders
  from geometry-only cemetery sources (`va_cemeteries` 158, `usgs_nsd` 7,
  `fgdl_cemeteries` 2, `txdot_cemeteries` 1) — `full_name`/`email`/`address`
  all 100% null.
- Target columns already exist: `staging.resolved_account` has `email`,
  `phone`, `website`; `staging.resolved_contact` has `email`, `phone`.

**Decision D17:** carry phone (and any available email/website/contact-name)
from staging through the merge into `resolved_account` and `resolved_contact`
for both verticals. This is a wiring fix, not new data acquisition.

### 3a. Healthcare — verify + fix the phone carry
`healthcare_pipeline.py` *does* have the plumbing already:
`:134` maps `df["phone"] = df.get("phone_normalized", …)`, `:423` sets
`account.phone` from it, and `build_resolved_contact` (`:494-516`) emits one
`primary_phone` contact per account when a phone survives. **But
`healthcare_pipeline.py` is an untracked, never-run-against-prod file** — the
current prod 0-contacts reflect an older version.

- Re-running the current pipeline (§ prior handoff's rerun sequence) may
  populate healthcare contacts/phone for the first time. **Verify it actually
  does** on a dry-run count before assuming it's fixed.
- If it still yields 0, trace whether `phone_normalized` survives the
  survivorship merge (the surviving row for a cluster may not be the one
  carrying the phone). If so, make phone survivorship independent — take a
  non-null phone from *any* row in the cluster, not just the survivor.
- Emit `email` / `website` into `resolved_account` if any healthcare source
  exposes them (VA facilities and NPPES may; check the connectors' output
  columns). Today both are 100% null.

### 3b. Deathcare — join `enrich_irs990` into the merge
This is a genuine missing join:
- In `deathcare_merge.py`'s `run_pipeline()`, add a step (mirroring how
  `staging.enrich_geocode` / the new parcel cache are loaded and joined) that
  loads `staging.enrich_irs990` and joins it onto the resolved output by
  `(source_id, natural_key)` — the enrich tables' PK.
- Use `phone_990` to populate `resolved_account.phone` / a real
  `resolved_contact` row, and `contact_name_990` to populate the contact's
  `full_name` (role something like `'irs990_contact'` rather than the synthetic
  `primary_phone`). This turns 2,722 empty religious-org accounts into ones with
  a named, phoned contact.
- Keep the existing synthetic-phone contacts only where no real contact exists;
  prefer the IRS-990 real contact when both are present (survivorship: real >
  synthetic).

**Test (both):** a fixture cluster whose staging source row carries
`phone_normalized` (healthcare) / a `enrich_irs990` row carrying `phone_990` +
`contact_name_990` (deathcare) produces a `resolved_contact` with that phone
(and name, for deathcare) and a populated `resolved_account.phone` — end to
end through the upsert. Assert the synthetic placeholder is *not* emitted when
a real contact exists.

**Verification (prod, post-rerun):** `core.contact` coverage rises well above
0.20%; `account.phone` non-null count rises from 168 into the thousands
(healthcare) and the 2,722 EIN-bearing deathcare accounts gain named contacts.

---

## 4. Ordering & rerun notes

- **D15 (replace-semantics) must land and be verified before any `core_apply`
  apply** — otherwise the 5-state narrowing and any contact/parcel changes
  won't reflect deletions correctly.
- The prior round's `--dry-run`-first discipline still holds: run
  `core_apply.dry_run_diff` and inspect insert/update/**tombstone** counts
  before every apply.
- Contact and parcel wiring (§2, §3) don't affect the account tombstone count,
  but they do add UPDATEs (populating previously-null columns). Expect a large
  UPDATE count on the first post-fix apply — that's the reachability data
  finally landing, not churn.
- Geocoding can be skipped for the narrowing preview (the 5-state subset is
  already in the `enrich_geocode` cache), but must run for any brand-new rows.

---

## 5. Out of scope this round (still deferred)

- `account.confidence` population (100% null) — still deferred.
- `core.source_record` audit trail (0 rows) — still deferred.
- Tier-1 `ccn`/`npi` external-key wiring (100% null) — real and documented
  (`docs/healthcare-pipeline.md` §9), but separate from this handoff.
- `dba_name` (100% null), `account.email`/`website` beyond whatever a source
  already exposes — no new acquisition this round.
- New ArcGIS parcel layers beyond the 5 registered states (D12).

---

## 6. Open items (implementer's call)

- **D15:** whether `staging.resolved_*` already has a `vertical` column or needs
  a migration to add one; and whether to scope the delete by `vertical` vs. by
  `source_id ANY(...)` (both work — `vertical` is cleaner).
- **D16:** loader as a `--write-db` flag on the existing parcel enrichers (a)
  vs. a standalone `parcel_cache_load.py` (b). Prefer (a) for consistency with
  `geocode_enrich.py`.
- **D16/SC:** obtaining the Charleston/Greenville/Richland assessor CSVs — SC
  parcel loading is data-blocked until then.
- **D17 healthcare:** whether the never-run `healthcare_pipeline.py` already
  fixes contacts on first real run, or whether phone survivorship needs the
  "any row in cluster" change. Decide after the first dry-run count.
- **D17 deathcare:** the role label for IRS-990-derived contacts and the
  precedence rule vs. the synthetic `primary_phone` placeholder.
