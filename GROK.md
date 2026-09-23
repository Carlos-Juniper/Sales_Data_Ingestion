# Project Context for the Grok Bot

This file briefs an AI agent ("the bot") that will pick up engineering work on this
repository. Read it before touching code. It explains what the project is for, what
data each vertical pulls in, and the structural conventions every connector follows —
conventions that exist specifically so a bot (or a new engineer) can add source #40
the same way source #4 was added, without re-deriving the pattern each time.

For deeper detail than this file carries, see `Ingestion-Plan-of-Action.md` (the
original design doc — still the source of truth for *why*) and the per-vertical
handoff docs in `docs/`.

---

## 1. What this project is

**Owner:** Juniper Landscaping — a commercial landscaping company.
**Purpose:** Build a cold-lead location database for sales. The pipeline finds
properties and the organizations that control them across five commercial-landscaping
end markets, resolves duplicates across sources, and lands clean records in Postgres
for the CRM to read.

**Scope:** 5 verticals × 5 states — **FL, NC, SC, TX, PA**. The architecture is
state-agnostic; adding a 6th state or vertical is a new source connector, not a
redesign.

**The core modeling decision:** every record splits into an **account** (the buying
entity — who signs the landscaping contract) and one or more **locations** (the
physical sites a crew would actually drive to). A hospital system is one account with
many campuses; a municipality is one account with dozens of parks. This split is why
the parks vertical works at all, and it's what lets sales pitch portfolio deals
instead of one site at a time.

---

## 2. The five verticals and their sources

| Vertical | Account grain | Location grain | Status |
|---|---|---|---|
| **Healthcare** | hospital / health system / practice group | hospital, clinic, practice site | Built — all 5 states |
| **HOA** | homeowners/condo association | common areas (via parcel match) | Built — **TX only** so far; FL/NC/SC/PA deferred (need paid data or scraping) |
| **Deathcare** | cemetery operator (religious/municipal/federal/commercial) | cemetery site | Built — all 5 states |
| **Parks / Municipal** | municipality, county, or state park agency | park, ballfield, median, campus | Built — all 5 states |
| **Resort / Hospitality** | lodging operator | hotel / lodging property | Built — **FL only**; other 4 states need purchased data |

### Healthcare (`connectors/healthcare/`)
- **CMS Provider Data** — `data.cms.gov/provider-data`, public API, no key. Nursing homes + general hospital datasets. (`cms_provider_data.py`)
- **NPPES** — CMS NPI bulk flat files (main + secondary-practice-location `pl_` file), manually downloaded, no API/key. Recovers satellite clinics/ASCs/MOBs that the primary-address file misses. (`nppes_practice_locations.py`)
- **VA Facilities API** (Lighthouse) — `api.va.gov/services/va_facilities/v1/facilities`, free API key, `type=health` only (national cemeteries are deathcare's job, not this connector's). (`va_facilities.py`)
- **Geocoding enrichment** — Census Bureau batch geocoder (primary, free, no storage restriction) with Nominatim as a rate-limited fallback. (`geocode_enrich.py`)
- **Parcel acreage enrichment** — point-in-polygon lookup against state/county parcel layers to get lot size. Statewide ArcGIS layers for FL and NC; county-routed FeatureServices for TX (top-15 metro) and PA (top-10 metro, PASDA); SC has no ArcGIS layer, so `sc_parcel_ingest.py` reads manually-downloaded Charleston/Greenville/Richland county assessor CSVs and fuzzy-matches by address. (`parcel_acreage_enrich.py`, `config/parcel_layers.yaml`)
- Merge: `healthcare_merge.py` — CCN → NPI → (name_normalized, zip5, site_state) → fuzzy tier-3 scoring.

### HOA (`connectors/hoa/`)
- **TX TREC Management Certificates** — `data.texas.gov`, statewide bulk CSV, ~17k associations. Mandatory filing under Tex. Prop. Code §209.004. Gives a clean 1:1 registry (name, type, city, ZIP) but **no street address, phone, or management company**. (`tx_trec_hoa.py`)
- **TX TREC certificate PDF OCR** — separate slow pass over `build_pdf_queue()` output. Certificates are scanned images, so the connector OCRs them and anchors on the numbered field-5/6/7 labels. Contact fields land in `staging.enrich_hoa_pdf_contact` (not `core.*`). Field 5 is `hoa_name` and `hoa_mailing_address` (legacy joined blob `assoc_mailing_address` kept as a deprecated alias). Field 6 is `mgmt_name`, `mgmt_mailing_address`, `mgmt_phone`, `mgmt_phone_normalized`, and `mgmt_email` (legacy `rep_*` aliases). Low-confidence parses set `needs_review`. (`tx_trec_pdf_enrich.py`; offline fill of rows already stored is `backfill_hoa_pdf_contact.py`.)
- FL/NC/SC/PA are explicitly deferred — no state HOA registry exists comparable to TX's; FL's path runs through Sunbiz bulk corporate data + statewide parcels, not yet built.

### Deathcare (`connectors/deathcare/`)
Five sources merged into one deduplicated spine:
- **USGS National Structures Dataset (NSD)**, layer 37 — the spatial spine (GNIS retired its cemetery feature class in 2021; NSD is the replacement). Public domain, no key. (`usgs_nsd.py`)
- **IRS Exempt Organizations Business Master File (BMF)** — `irs.gov/pub/irs-soi/eo2.csv` (NC/SC/PA) and `eo3.csv` (FL/TX), public domain. Only source with EIN; drives the religious/commercial segment split. (`irs_bmf_deathcare.py`)
- **IRS Form 990 enrichment** — ProPublica Nonprofit Explorer API, per-EIN, adds phone/contact for the religious segment only (kept as a separate slow pass so the BMF connector stays fast). (`irs_990_enrich.py`)
- **VA National Cemetery Sites** — Socrata CSV export from `datahub.va.gov`, ~170 federal sites. (`va_cemeteries.py`)
- **TxDOT Texas Cemeteries** — ArcGIS FeatureServer, TX-only, ~8k records, geometry-only (no address/phone/EIN). (`txdot_cemeteries.py`)
- **FGDL / UF GeoPlan Florida Cemetery Facilities** — ArcGIS FeatureServer, FL-only, ~3.9k records. (`fgdl_cemeteries.py`)
- Merge (`deathcare_merge.py`): EIN dedup within BMF → 150m spatial dedup across sources → segment resolution by elimination → lead-qualification flagging.

### Parks / Municipal (`connectors/parks/`)
The account here is the **government body**, not the park — a municipality's contract
bundles every park, median, and facility ground it owns, so the account count tracks
governments (~5,551), not parks (~72k).
- **Government spine**: Census TIGERweb ArcGIS REST (incorporated places, PA active townships, counties across the 5 states) — chosen over TIGER/Line shapefiles because it's queryable and returns WGS84 GeoJSON directly. (`gov_units.py`, `config/gov_layers.yaml`)
- **Park layers**, one registry entry per state/agency, all public ArcGIS endpoints, no credentials needed: **PAD-US 4.1** (USGS, national protected areas), **PASDA/DCNR** (PA local parks), **TPWD** (TX state parks), **FDEP** (FL state parks), **NCDPR** (NC state parks), **SCPRT** (SC state parks). (`park_layers.py`, `config/park_layers.yaml`)
- **Manager resolution** (`manager_resolve.py`) — the hard join in this vertical: matching a free-text "managed by" string (e.g. "City of Cary Parks, Recreation & Cultural Resources") to a government account (GEOID). State-park layers declare their managing agency directly (no matching needed); everything else goes through string resolution.
- Merge (`parks_merge.py`): polygon dedup (IoU > 0.60 AND name similarity > 0.5) → acreage reconciliation (published vs. measured) → resolved_account/location built from the government spine.

### Resort / Hospitality (`connectors/resort/`)
- **FL DBPR Division of Hotels & Restaurants** — `myfloridalicense.com`, 7 district CSVs (`hrlodge1..7.csv`), weekly refresh, free, public records under Ch. 119 F.S. **One row per rental unit, not per property** — must dedup on License Number and take max/first of the unit-count field (summing inflates it ~8x). (`fl_dbpr_lodging.py`)
- Other 4 states: open data is weak here; deferred until purchased-data economics are worked out.

---

## 3. Pipeline architecture — five stages, one pattern per source

```
 EXTRACT ──▶ LAND ──▶ STAGE ──▶ RESOLVE ──▶ SERVE
 connector    GCS      Postgres   dedup +     Postgres
 per source   raw      staging    survivor    core schema
              bucket   schema     selection   (+ CRM reads)
```

1. **Extract** — one connector per source. Fetches bytes and produces a canonical
   pandas DataFrame. Does not clean, dedup, or unify across sources — that's a later
   stage's job.
2. **Land** — raw payloads are written immutably to GCS
   (`gs://juniper-ingest-raw/{source_id}/{ingest_date}/`), and a manifest row is
   written to `ingest.source_run` (byte count, SHA-256, row count, connector version,
   **license string captured at fetch time**). Raw files are never overwritten or
   deleted — several sources (e.g. quarterly bulk files) publish no history of their
   own, and license provenance has to be defensible for commercially-used
   public-records data.
3. **Stage — one table per source, no exceptions.** Every connector writes to its own
   `staging.<source_id>` table via `lib.db.upsert_staging()`, upserted on
   `(source_id, natural_key)`. Columns follow the source's own field names/shapes plus
   a required set of canonical columns (`lib/schema.py::CANONICAL_COLUMNS`) — every
   connector's output DataFrame must satisfy `validate_canonical()` before it's
   written. Normalization (name_normalized, phone_normalized, geom) is *added* as new
   columns, never overwritten in place. This is deliberately kept per-source and
   unnormalized-until-added so a bad run in one source can never corrupt another, and
   so you can always tell whether a problem came from the source or from the code.
4. **Resolve** — each vertical's `*_merge.py` reads its staging tables, blocks/scores/
   dedups, and writes `staging.resolved_account` / `resolved_location` /
   `resolved_contact` (PKs: `account_key` / `location_key` / `contact_key`), scoped by
   `vertical`. These resolved tables are **re-materialized on every run** — they are a
   pipeline work-table, not an accumulating history.
5. **Serve** — `lib/core_writer.py` runs a 3-way diff (INSERT / UPDATE / TOMBSTONE)
   from `staging.resolved_*` into `core.account` / `core.location` / `core.contact`,
   which is what the CRM actually reads. This is the only code in the project that
   writes to `core.*`.

### Why the 3-way diff matters (don't casually change this)
- A dry-run (`dry_run_diff`) computes the exact same CTEs as the apply path with no
  writes — "will a re-run duplicate anything" is a plain query, not a guess.
- Content changes are detected via a deterministic `md5(concat_ws(...))` hash over the
  mutable fields of each entity; unchanged rows produce zero writes on a re-run
  (idempotency).
- Accounts are never hard-deleted. A row absent from the current resolved set is
  **tombstoned** (`status='merged'`, `parent_account_id` set to the survivor if one is
  identifiable) — first_seen and foreign keys stay intact.
- `core.source_record` is a content-addressed, append-only provenance table keyed on
  `(source_id, natural_key, payload_sha)`: an unchanged payload only bumps
  `last_seen_run_id`, it never inserts a duplicate row.

### Schema layers (Postgres, PostGIS)
- `ingest.source_run` — one row per connector run: provenance, byte/row counts, SHA-256, license string.
- `staging.<source_id>` — one table per source, raw-ish and typed, PK `(source_id, natural_key)`.
- `staging.resolved_account` / `resolved_location` / `resolved_contact` — per-vertical merge output, re-materialized each run.
- `core.account` / `core.location` / `core.contact` / `core.source_record` — what everything downstream (CRM, sales) reads. See `Ingestion-Plan-of-Action.md` §4 for full column definitions.
- Migrations live in `db/migrations/`, applied in numeric order.

---

## 4. Shared library conventions (`connectors/lib/`)

Every connector is expected to reuse these rather than reinventing them:
- `lib/db.py` — `get_engine()`, `upsert_staging()` (creates the staging table if
  missing, enforces the canonical column set, guards against SQL-injection via
  identifier validation, guards against a silently-empty `natural_key` collapsing
  the whole table to one row), `upsert_boundaries()` for polygon/acreage side tables,
  `write_source_run()` / `finish_source_run()` for provenance.
- `lib/schema.py` — `CANONICAL_COLUMNS` and `validate_canonical()`, the fixed contract
  every connector's output DataFrame must satisfy before `upsert_staging()`.
- `lib/core_writer.py` — the 3-way diff described above; the only writer of `core.*`.
- `lib/arcgis.py` — shared ArcGIS FeatureServer/MapServer REST query helpers (paging,
  geometry extraction) used by every ArcGIS-backed connector (deathcare's FGDL/TxDOT,
  all of parks, healthcare's parcel layers).
- `lib/match.py` — fuzzy matching / scoring helpers shared across vertical merges.
- `lib/normalize.py` — name/phone/ZIP normalization helpers.
- `lib/enums.py` — closed value sets (segments, enrich status, merge confidence,
  per-vertical target-state sets) as plain string constants, not `Enum` subclasses,
  so they drop straight into DataFrame cells and dict keys.
- `lib/geo.py`, `lib/gcs.py`, `lib/http.py`, `lib/keys.py`, `lib/pipeline_run.py`,
  `lib/validate.py`, `lib/enrich_runner.py`, `lib/match_queue.py`,
  `lib/resolved_writer.py` — geometry helpers, raw-landing GCS client, HTTP/secret
  helpers, `account_key`/`location_key` hashing, run-tracking CLI, canonical
  validation, generic enrichment runner, human-review queue, and the
  `staging.resolved_*` writer, respectively.

**Add a new source by:** writing one connector module that outputs a `CANONICAL_COLUMNS`
DataFrame and calls `upsert_staging()`. Do not touch `core_writer.py`, and do not add
ad hoc tables outside the `staging.<source_id>` / `staging.resolved_*` / `core.*`
pattern.

---

## 5. Infra & running it

- **Cloud provider:** GCP, project `juniper-crm-498215-p5`, region `us-east1`.
- **Compute:** one Docker image (`Dockerfile`) for all verticals; the entrypoint
  script is selected by the `VERTICAL` env var (`scripts/run_{vertical}.sh`),
  deployed as **Cloud Run Jobs** triggered by Cloud Scheduler on each source's own
  refresh cadence.
- **Database:** Cloud SQL for PostgreSQL + PostGIS (`juniper-postgres-prod`), reached
  via the Cloud SQL Auth Proxy locally on port **5433** (local dev Postgres via
  docker-compose runs on 5432, so the two never collide). `DATABASE_URL` is stored
  bare (`postgresql://...`); `lib/db.get_engine()` rewrites it to
  `postgresql+psycopg://` for SQLAlchemy/psycopg3, while `db/run_migrations.py` needs
  the bare form for libpq.
- **Raw landing:** GCS bucket `juniper-ingest-raw`, immutable, versioned, Nearline at
  90 days. `DISABLE_GCS=1` for local dev/CI — connectors degrade gracefully rather
  than raising when GCS/ADC is unavailable.
- **CI/build:** `cloudbuild.yaml` builds and pushes to Artifact Registry
  (`us-east1-docker.pkg.dev/juniper-crm-498215-p5/ingestion/connector`), tagged by
  commit SHA plus `latest`.
- **IaC:** `terraform/`.
- Required Postgres extensions: `postgis`, `pg_trgm`, `fuzzystrmatch`, `unaccent`,
  `btree_gin`.
- Config: copy `.env.example` to `.env` (gitignored). Per-vertical flat-file inputs
  (NPPES, HOA, resort CSVs) are supplied via glob-pattern env vars since those sources
  require manual download — no live API exists for them.

## 6. Tests & docs

- Tests live under each connector's `tests/` subdirectory plus `connectors/tests/` for
  shared-lib/integration coverage; run via `pytest` (see `pytest.ini`, `conftest.py`).
- `docs/` holds deeper per-vertical write-ups and handoff notes
  (`healthcare-pipeline.md`, `deathcare-pipeline.md`, `parks-pipeline.md`, the Postgres
  migration/data-model doc, and prior HANDOFF-*.md notes) — check there before
  re-deriving a vertical's design from scratch.
- `market-analysis/` holds the TAM/deck analysis that motivated vertical and state
  selection — background context, not pipeline code.
