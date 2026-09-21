# Deathcare & Healthcare — Pre-Run vs Post-Run Stats

Internal tracking doc, not for external distribution. Purpose: capture the
state of `core.*` on `juniper-postgres-prod` before the fixes in
`docs/HANDOFF-tier3-review-parcel-nan-va.md` land, so we can diff against it
once that work is done and the pipelines are re-run. The sales/leadership
report gets built from the **post-run** column, not this one.

Pulled 2026-08-20 via `cloud-sql-proxy --port 5433 juniper-crm-498215-p5:us-east1:juniper-postgres-prod`.

## Headline counts

| Metric | Deathcare (pre) | Deathcare (post) | Healthcare (pre) | Healthcare (post) |
|---|---|---|---|---|
| Accounts (`core.account`) | 34,701 | _TBD_ | 47,424 | _TBD_ |
| Locations (`core.location`) | 35,079 | _TBD_ | 47,425 | _TBD_ |
| Contacts (`core.contact`) | 168 | _TBD_ | 0 | _TBD_ |

Healthcare's account count is expected to **drop substantially post-run** —
per decision below, the pipeline is being scoped down to FL/NC/TX/PA/SC only
(currently nationwide). `core_apply.py`'s tombstone mechanism (`status =
'merged'`) should retire the out-of-scope rows automatically on the next
`--write-db` + diff cycle; no manual delete needed.

## Deathcare detail (pre-run)

- `account_type`: cemetery 34,543 · federal 158
- State footprint: TX 12,118 · PA 9,735 · NC 4,855 · FL 4,503 · SC 3,352 (small scatter elsewhere)
- Acreage (`size_metric`, FGDL-only): 3,633 / 34,701 accounts (10%) have a real value — range 0.005–418.5 acres, avg 6.24, sum ~22,672 acres
  - ⚠️ Of the remaining 31,068, **all 31,068 are storing a literal `NaN`** in the numeric column rather than SQL `NULL` (bug — see handoff doc)
- EIN captured via IRS BMF: 2,722 / 34,701 (7.8%)
- `maintained_acres` (parcel-derived, `core.location`): 0% populated — `staging.enrich_parcel` is empty, parcel connectors have never been run
- Contacts: 168, all `primary_phone` synthetic placeholders

## Healthcare detail (pre-run)

- `account_type`: raw mix of CMS control-type strings (`For profit - Corporation`, `Acute Care Hospitals`, ...) and ~300 distinct NPPES taxonomy codes (`261QM0801X`, ...) — not currently readable by a non-technical audience, needs a taxonomy → label crosswalk before sales/leadership sees it
- Geocode coverage: rooftop 34,975 (74%) · street 9,779 (21%) · no_match 2,671 (5.6%)
- State footprint: nationwide — top states CA 4,116 · TX 3,948 · FL 3,294 · OH 2,082 · NY 2,027 · PA 1,912 · NC 1,526 · AZ 1,500 · MI 1,340 ... — **will be narrowed to FL/NC/TX/PA/SC only post-run**
- `external_keys`: 0 / 47,424 have `ccn` or `npi` captured — Tier 1 exact-key merge is not currently firing in production (matches the documented wiring gap)
- VA Facilities: **never run** — `staging.va_facilities` = 0 rows, no `ingest.source_run` entry. VA hospitals are absent from the current 47,424 accounts despite being the top-priority source in survivorship
- `maintained_acres`: 0% populated, same as deathcare
- Contacts: 0 (healthcare only emits a synthetic contact row when a phone number survives — apparently none did, or the step isn't populating)

## Pipeline health (pre-run)

- All 10 `ingest.source_run` rows for today (2026-08-20) show `status='succeeded'` — `cms_general`, `cms_nursing_home`, `nppes_practice_locations`, `fgdl_cemeteries`, `irs_bmf_deathcare`, `txdot_cemeteries`, `usgs_nsd`, `va_cemeteries`, plus the two merge driver runs (`deathcare_pipeline`, `healthcare_pipeline`). `va_facilities` has no row at all.
- `review.pending_pairs` / `review.match_queue`: both 0 rows — Tier-3 fuzzy matches (0.75–0.92 band) are not currently landing anywhere for human review
- `core.source_record`: 0 rows (out of scope for this round, noted for awareness)
- `confidence` column: unpopulated on every account, both verticals (out of scope for this round, noted for awareness)

## Post-run

Fill in once `docs/HANDOFF-tier3-review-parcel-nan-va.md` is implemented and
the full sequence (parcel connectors → VA Facilities → both pipeline drivers
with `--write-db` → `core_apply.py` diff) has run against
`juniper-postgres-prod`.

---

## Parks (pre-run, 2026-09-04)

Parks has **never been written to `core.*`** — the vertical's connectors, merge and
run script were built on 2026-09-04 and the first `--write-db` run has not happened
yet, so every pre-run count is zero by definition. What follows are the *expected*
post-run figures derived from verified dry runs, recorded here so the first real run
can be diffed against an explicit prediction rather than against nothing.

| Metric | Parks (pre) | Parks (expected post) |
|---|---|---|
| Accounts (`core.account`) | 0 | ~5,551 max, fewer after park-less governments are excluded |
| Locations (`core.location`) | 0 | ~72,000 before dedup; lower after §5.2 polygon dedup |
| Contacts (`core.contact`) | 0 | **0 — by design** |

### Account ceiling, verified live against TIGERweb

| Source | Rows | `account_type` |
|---|---|---|
| `tiger_places` | 3,470 | `municipality` |
| `tiger_cousub` (PA only) | 1,543 | `municipality` |
| `tiger_counties` | 534 | `county` |
| state agencies (synthetic) | 4 | `state_agency` |
| **total** | **5,551** | |

Cross-checks against the plan: 534 counties matches §6.4 exactly, and
1,543 PA townships + 1,012 PA places = 2,555 against the plan's stated ~2,560 PA
municipalities. The plan's ~7,400 ceiling includes ~1,960 NCES school districts,
which are deferred — hence 5,551 rather than 7,400.

**Accounts will land below 5,551.** `build_resolved_account` drops any government
with no parks attached, on the grounds that an account with nothing to maintain is
not a lead. How far below is the single most interesting number in the first run.

### Park source counts (dry-run verified)

`padus_parks` 64,736 · `pasda_dcnr_parks` 6,325 · `nc_state_parks` 346 ·
`fdep_state_parks` 179 · `tpwd_state_parks` 116 · `sc_state_parks` 55 —
**71,757 before dedup**.

The §5.2 polygon dedup (IoU > 0.60 AND name similarity > 0.5) has not yet run
against a database, so the collapse rate is unmeasured. The largest expected
reconciliation is PASDA's 6,325 PA local parks against PAD-US's 35,042 PA `LOC`
records.

### Acreage — the one metric to check first

Unlike deathcare (10% acreage coverage) and healthcare (parcel-enriched), parks
carries acreage on **100%** of rows, from two independent figures per park. Verified
computed÷published ratios: PAD-US 0.997 · PASDA 1.001 · FDEP 1.003 · NC 1.002 ·
SC 1.003 · TPWD **0.740**.

TPWD's 0.740 was a real bug caught by the cross-check on the first run — its
`Shape__Area` is Web Mercator square metres, inflating acreage ~35% across Texas.
Fixed by disabling the field and relying on the PostGIS-measured value. See
`docs/parks-pipeline.md` §2.

Note the NaN-in-numeric bug that affected 31,068 deathcare rows cannot recur here:
`lib/resolved_writer._to_rows` and `lib/db.upsert_boundaries` both scrub NaN to
None after `to_dict()`, and both paths are unit-tested for it.

**Caveat on `maintained_acres`:** it holds *total* park area, not mowable turf. Any
acreage figure that reaches sales needs that stated.
