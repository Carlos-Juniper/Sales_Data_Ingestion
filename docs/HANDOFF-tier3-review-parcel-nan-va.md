# Handoff: Tier-3 review wiring, parcel acreage, NaN fix, VA Facilities, 5-state healthcare scope

**Audience:** the engineer/agent implementing this. Read this whole file before writing code.
**Prerequisite reading:** `docs/healthcare-pipeline.md`, `docs/deathcare-pipeline.md`,
`docs/HANDOFF-end-to-end-pipeline.md` (D1–D9, prior design decisions — do not
re-litigate those), `docs/pipeline-stats-pre-post-run.md` (the production
baseline this work is meant to move).

---

## 1. Why this exists

A summary-statistics report for sales/leadership was requested off the
deathcare + healthcare pipeline output. Pulling real numbers from
`juniper-postgres-prod` (`core.*`) surfaced six data-quality issues material
enough that sending the report before fixing them would misrepresent the
data. This handoff scopes exactly what to fix, in what order, before the
pipelines are re-run and the report gets built from the post-run numbers.

Everything below was verified against the live production database and the
current code — file:line citations are real, not inferred from docs.

---

## 2. Locked decisions (this round)

Continuing the `D#` numbering from `docs/HANDOFF-end-to-end-pipeline.md`.
**Do not re-litigate these** — they were decided in conversation with the
project owner; implement them.

| # | Decision | Rationale |
|---|---|---|
| **D10** | Healthcare ingestion narrows to **FL, NC, TX, PA, SC only** (matching deathcare's existing footprint), filtered as early as possible — at each connector's row-building step, before geocoding — not just at merge time. | These 5 states are "the only areas of concern for now" per the business owner. Filtering pre-geocode also avoids wasted Nominatim calls (rate-limited ~1/sec) and cuts NPPES's ~9.7M-row scan down substantially. |
| **D11** | Tier-3 review-queue wiring in `healthcare_merge.py`'s `merge_all()` must be **always-on** for every real (`--write-db`) healthcare pipeline run — no separate opt-in flag. | Matches the tier's intended purpose: ambiguous (0.75–0.92) fuzzy matches should always be human-reviewable, not silently auto-merged or dropped. |
| **D12** | Parcel acreage (`maintained_acres`) stays scoped to the **5 states already registered** in `connectors/healthcare/config/parcel_layers.yaml` (FL/NC statewide, TX/PA per-county) plus SC's assessor-CSV connector. **No new ArcGIS layers researched this round.** | Explicit scope decision — this becomes moot for healthcare once D10 lands anyway, since there will be no accounts outside these 5 states left to enrich. |
| **D13** | The `size_metric` NaN-vs-NULL bug gets fixed in `deathcare_merge.py` (confirmed live corruption) **and** defensively in `healthcare_merge.py` (same DataFrame-construction pattern, not yet triggered live but latent — healthcare's `size_metric` is currently 100% NULL from every wired source, so the bug is dormant, not absent). | Same root cause, same fix shape; cheap to close off before it becomes live once any healthcare source starts populating `size_value`. |
| **D14** | The now out-of-scope nationwide healthcare accounts (everything outside FL/NC/TX/PA/SC) are retired via `core_apply.py`'s existing **tombstone** mechanism (`status='merged'`), not a manual `DELETE`. | No new code needed — D2 in the original handoff already built this. Re-running the narrowed pipeline naturally drops those rows out of `staging.resolved_account`, and the 3-way diff tombstones them. |

---

## 3. Current state (verified 2026-08-20 against `juniper-postgres-prod`)

Full detail in `docs/pipeline-stats-pre-post-run.md`. Short version:

| Issue | Verified fact |
|---|---|
| VA Facilities never run | `staging.va_facilities` = 0 rows, no `ingest.source_run` entry for `va_facilities` at all |
| CCN/NPI wiring gap is live | 0 / 47,424 healthcare accounts have `ccn`/`npi` in `external_keys` — **not addressed this round**, see §7 |
| `size_metric` NaN corruption | 31,068 / 34,701 (89%) deathcare accounts store literal `NaN` instead of SQL `NULL` |
| `maintained_acres` unpopulated | `staging.enrich_parcel` is **completely empty** (0 rows) — the parcel connectors have never been run, not merely unjoined |
| Tier-3 review queue never fed | `review.pending_pairs` / `review.match_queue` both 0 rows |
| Healthcare scope is nationwide | Real accounts exist in CA, OH, NY, AZ, MI, etc. — 47,424 total vs. an eventual 5-state subset |

---

## 4. Work items

### 4.1 Fix the `size_metric` NaN bug

**Root cause (traced, deathcare):** `connectors/deathcare/deathcare_merge.py`
`_build_resolved_account` correctly computes `size_metric = None` per-row for
clusters with no size data (`_is_present()` logic, ~lines 468–475 and
539–545). The corruption happens at **line 568**,
`return pd.DataFrame(rows)` — because the row dicts mix `None` and real
`float` values for `size_metric`, pandas infers `float64` for that column and
silently upcasts every `None` to `NaN`. `_upsert_resolved_account`
(lines 657–699) then serializes via `.to_dict(orient="records")` (line 695)
straight into a parameterized `CAST(:size_metric AS numeric)` (line 674) —
this path never calls `upsert_staging()` in `connectors/lib/db.py`, which is
the only place the documented NaN→NULL scrub actually runs. Postgres
`numeric` natively supports `NaN`, so it lands unchanged in `core.account`.

**Fix:** scrub `size_metric` back to a real `None` **after** it leaves the
DataFrame (a `.where()`/`.astype(object)` call on a `float64` column just
re-produces `NaN` — the column has to stop being a numpy float dtype before
serialization). Simplest correct shape: iterate the row dicts produced by
`.to_dict(orient="records")` and replace any `pd.isna(row["size_metric"])`
with `None` immediately before the SQL execute — same idea `upsert_staging()`
already applies, just needs to run on this path too.

**Also do defensively in `healthcare_merge.py`:** find the equivalent
resolved-account-building function and add the same scrub before its
DataFrame/dict conversion, even though `size_metric` is currently always
`None` from every wired healthcare source (no live corruption yet — this
closes the class of bug off, not a currently-observable symptom).

**Test:** a cluster list with a mix of `None` and real float `size_metric`
values, asserting the resolved output keeps `None` as `None` end-to-end
through the DB write (not just in the DataFrame) — for both verticals.

### 4.2 Wire Tier-3 review queue for healthcare (deathcare has no Tier-3 — do not add one)

**Confirmed:** `connectors/deathcare/deathcare_merge.py` has **no fuzzy-match
tier at all** — no `merge_all()`, no `pending_pairs`/`review_queue`/`tier3`
concept anywhere in the file (grep returns zero hits). Its pipeline
(`merge_pipeline()`, line 399) is deterministic only: EIN dedup + a 150m
spatial merge (lines 412–417). `review.pending_pairs` being empty is not "no
pairs happened to score in-band" — deathcare structurally cannot produce a
review pair. **Don't build one; that's new scope, not a fix** (see §7 if the
business wants this later).

**Healthcare is the real, one-line bug:**
`connectors/healthcare/healthcare_merge.py:366` defines
`merge_all(df, *, engine: Optional[Engine] = None)`, which only enqueues to
`review.pending_pairs` when `engine is not None` (lines 394–405; thresholds
`queue_threshold=0.75` / `auto_threshold=0.92` at lines 178–179, 213, 215).
`connectors/healthcare/healthcare_pipeline.py:610` calls
`merged, review_queue = merge_all(raw)` — **engine is never passed**, in
dry-run or otherwise. Confirmed: the CLI (`main()`, lines ~674–692) has
**only `--dry-run`** (lines 678–682) — there is no write-mode flag at all,
and `run_pipeline(engine, dry_run=args.dry_run)` never threads that engine
into the `merge_all()` call at line 610.

**Fix:**
1. Change line 610 to `merged, review_queue = merge_all(raw, engine=engine)`,
   gated so review-queue writes only happen when `dry_run=False` (mirror
   whatever pattern `run_pipeline()` already uses to skip the
   `staging.resolved_*` upserts under `--dry-run`).
2. `healthcare_pipeline.py`'s CLI has no way to request a real run today —
   confirm/add whatever flag distinguishes "real run" from `--dry-run`
   (today the absence of `--dry-run` already implies a real run per
   `dry_run=args.dry_run`; just make sure the engine reaches `merge_all`
   on that path).

No backfill needed — the next full `--write-db`-equivalent healthcare run
(already planned in §5) populates `review.pending_pairs` naturally.

### 4.3 Narrow healthcare ingestion to FL/NC/TX/PA/SC

**Confirmed: no state filter exists anywhere in the healthcare path today** —
`cms_provider_data.py`, `nppes_practice_locations.py`, `healthcare_pipeline.py`,
and `healthcare_merge.py` are all filter-free (only a `site_state` output
column, never used to restrict rows).

`connectors/healthcare/va_facilities.py:77` already defines
`_TARGET_STATES = ("FL", "TX", "NC", "SC", "PA")` — but it's currently used
**only** for a printed breakdown inside `report_quality()` (lines 239–241),
never as an actual `.isin()` filter. Rows outside these states are still
fetched and written today.

**Fix:**
1. Turn `va_facilities.py`'s existing `_TARGET_STATES` into a real filter on
   the fetched rows before `upsert_staging()`, not just a reporting label.
2. Add the equivalent constant + `.isin()` filter to `cms_provider_data.py`
   and `nppes_practice_locations.py`. Model: `irs_bmf_deathcare.py`'s
   `_TARGET_STATES` set + `.isin()` filter (~lines 44, 122) is the closest
   existing pattern to copy — filter on the state field as early as
   possible in row-building, **before** geocoding or `upsert_staging()` runs
   (this matters for cost/time, not just correctness — Nominatim is
   rate-limited ~1 req/sec, and NPPES's main file is ~9.7M rows before any
   filtering).
3. Don't duplicate the 5-state tuple four times — put one shared constant
   (e.g. `HEALTHCARE_TARGET_STATES`) somewhere both `va_facilities.py` and
   the other two connectors can import, and have `va_facilities.py`'s
   existing `_TARGET_STATES` become an alias/import of it rather than a
   fourth independent copy.
4. After this lands and connectors are re-run, healthcare accounts outside
   these 5 states drop out of `staging.resolved_account` on the next
   `healthcare_pipeline.py` run — `core_apply.py`'s existing diff tombstones
   the corresponding `core.account` rows automatically (D14). Confirm the
   tombstone count in the dry-run diff roughly matches the expected
   nationwide-minus-5-state delta before applying for real.

### 4.4 Populate and join parcel acreage (`maintained_acres`)

**Confirmed: `staging.enrich_parcel` is completely empty (0 rows)** — the
parcel connectors have never been run against this data at all, for either
vertical. This is not just the documented "not joined into
`healthcare_pipeline.py`" gap — the enrichment cache itself doesn't exist
yet.

**Fix:**
1. Run `parcel_acreage_enrich.py` for FL/NC/TX/PA (per the layers already
   registered in `connectors/healthcare/config/parcel_layers.yaml`) and
   `sc_parcel_ingest.py` for SC, against **both** verticals' resolved
   accounts — deathcare's `maintained_acres` is also 0% populated today, not
   just healthcare's.
2. Join `staging.enrich_parcel` into both `healthcare_pipeline.py` and
   `deathcare_merge.py`, the same way `staging.enrich_geocode` is already
   joined (see `join_geocodes()`, healthcare driver step 4) — add an
   equivalent `join_parcel()` step that populates `core.location`'s
   `maintained_acres` / `acres_confidence` / `geometry_source`, keyed the
   same way (the survivor row's own `natural_key`).
3. Per D12, this stays scoped to the 5 already-registered states. Once D10
   lands, healthcare will have no accounts outside these states left
   anyway, so this isn't a separate coverage gap to track.

---

## 5. Rerun sequence (production, via `cloud-sql-proxy --port 5433 juniper-crm-498215-p5:us-east1:juniper-postgres-prod`)

Do not skip ahead on failure.

1. Implement all of §4. Run `.venv/bin/pytest` green before touching prod.
2. Add/confirm regression tests: NaN scrub (both verticals), 5-state filter
   (each of the 3 connectors that needs one), Tier-3 engine wiring (a
   synthetic fixture with a pair scoring 0.75–0.92, asserting it lands in
   `review.pending_pairs`).
3. Re-run each healthcare source connector with its write-mode flag, now
   state-filtered: `cms_general`, `cms_nursing_home`,
   `nppes_practice_locations`.
4. Run `va_facilities.py --write-db` for the first time. It's fully wired
   already (`SOURCE_ID` matches `staging.va_facilities`, `VA_API_KEY` is in
   `.env`, and `SOURCE_URL` **defaults to the production host** — no
   `VA_API_BASE_URL` override needed or present). This is a "run it" gap,
   not a code gap.
5. Run `geocode_enrich.py` for any new/changed healthcare rows (VA carries
   its own coordinates and skips this).
6. Run `parcel_acreage_enrich.py` (FL/NC/TX/PA) and `sc_parcel_ingest.py`
   (SC) against both verticals' resolved accounts.
7. Run `healthcare_pipeline.py`'s write-mode path — now passing a live
   engine into `merge_all()` (§4.2) and with `join_parcel()` wired into the
   driver (§4.4).
8. Run `deathcare_merge.py`'s `run_pipeline()` write-mode path — same
   driver, now also joining parcel data (§4.4). No Tier-3 change applies
   here (§4.2).
9. Run `core_apply.py`'s `dry_run_diff(engine)` first — inspect
   insert/update/tombstone counts. Specifically confirm the tombstone count
   roughly matches the nationwide-minus-5-state healthcare delta before
   applying anything.
10. Run `core_apply.py`'s `apply_core_diff(engine, run_id)` for real.
11. Re-run the same summary-stat queries used to build the "pre" column in
    `docs/pipeline-stats-pre-post-run.md`, fill in "post" — **only then**
    build the sales/leadership-facing report from the post-run numbers.

---

## 6. Verification checklist

- [ ] `.venv/bin/pytest` green
- [ ] `core_apply.py` dry-run diff inspected before every apply
- [ ] `select count(*) from core.account where size_metric::text = 'NaN'` → `0` for both verticals
- [ ] `review.pending_pairs` non-empty after step 7 of §5 — **or**, if it's
      still empty, confirm that's because zero pairs actually fell in the
      0.75–0.92 band on this run (check the count of Tier-3-eligible
      comparisons), not because the engine wiring silently failed again
- [ ] `maintained_acres` coverage non-zero for at least FGDL-registered FL
      cemeteries and any healthcare accounts with a matched parcel
- [ ] Healthcare `core.account` state breakdown shows only FL/NC/TX/PA/SC
      (plus whatever small scatter deathcare already tolerates from bad
      source data)

---

## 7. Out of scope this round (explicitly deferred)

- `core.source_record` audit trail (currently 0 rows) — not addressed.
- `confidence` score population (currently 0 rows, both verticals) — not addressed.
- Tier 1 CCN/NPI wiring gap (`docs/healthcare-pipeline.md` §9) — real,
  documented, unrelated to this handoff's scope; not touched here.
- Expanding parcel/ArcGIS layer coverage beyond FL/NC/TX/PA/SC — moot once
  D10 lands; healthcare has no accounts outside these states left to enrich.
- Adding a fuzzy-match/uncertain-band tier to deathcare — its merge is
  deterministic by design (EIN + spatial only); adding a Tier-3 equivalent
  is new scope, not a fix, and wasn't requested.

## 8. Open items not decided

- Where the shared 5-state constant should live (`connectors/lib/` vs. a
  healthcare-local module) — implementing agent's call; just don't keep it
  as four independent copies.
- Whether `va_facilities.py`'s `report_quality()` breakdown becomes the
  enforcement point (filtering using the same constant it already prints
  against) or gets refactored into a shared helper alongside the other two
  connectors' new filters.
- Whether the business ever wants a deterministic-only "site rollup" pass
  for deathcare (the CMS "many CCNs per physical site" analog doesn't apply
  there, but multiple cemetery sources reporting the same physical site
  under slightly different names/addresses outside the 150m spatial-merge
  radius could still double-count — not raised as a problem, just noting
  the parallel).
