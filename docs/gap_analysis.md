# Ingestion Plan of Action vs. `juniper-crm-HOA-pipeline` — gap analysis

## Context

This is a research question, not a coding task: how much of `Ingestion-Plan-of-Action.md` (committed locally today, 2026-08-17) is already built in `github.com/juniperlandscaping/juniper-crm-HOA-pipeline`. No code changes are proposed here — this file is the findings report. I read the plan document in full, pulled the repo's file tree via `gh api`, and read its core orchestrator, all four scrapers, the dedup job, the CSV import/clean jobs, `requirements.txt`, and `Dockerfile`.

Repo commits: 2026-06-10 to 2026-06-12 — roughly **10 weeks before** today's plan document. So the repo is pre-existing HOA-only work, and the plan appears to have been written to scope a much larger 5-vertical program without fully accounting for it.

## Bottom line

**Almost none of the plan, as specified, is implemented in that repo.** The repo does real, running, HOA-only ingestion work, but on a **different architecture, different data sources, and a different schema** than the plan calls for. Treat the repo as a working prototype to migrate logic *out of*, not as partial progress toward the plan's design.

## What exists in the repo

HOA vertical only (no healthcare, deathcare, parks/municipal, or resort/hospitality — 0% of Phases 1, 4, 5, 6):

| Repo component | What it does | Plan mapping |
|---|---|---|
| `scrapers/hoa_usa.py` + `jobs/*` (ArcGIS step) via `juniper_crm_shared.scrapers.ArcGisScraper` | Scrapes county/city ArcGIS parcel layers for HOA common-area parcels, multi-state (`--states` flag) | **Not in the plan at all.** Plan's Texas approach (§6.2) is the TREC bulk CSV, not ArcGIS parcel scraping |
| `scrapers/sunbiz.py` + `jobs/sunbiz_backfill.py` | Pulls active FL non-profit corps from **sunbizdaily.com**, a third-party paid API (rate-limited 1000 req/hr, `SUNBIZ_API_KEY`) | Plan (§6.2) calls for the **free public Sunbiz SFTP bulk file**, not a paid third-party API — different source, different licensing story |
| `scrapers/dbpr.py` + `jobs/dbpr_backfill.py` | Fetches/parses a DBPR condo CSV, joins to `hoa_properties` on name+zip/city | Loosely matches plan §6.2 "DBPR condo/co-op extracts," but joins into a flat property table, not the plan's account/location schema |
| `scrapers/fl_county_acreage.py` + backfill job | Per-county (Pinellas, Polk; Sarasota/Seminole pending) ArcGIS parcel lookups for acreage | A narrow, manual version of the plan's general "geocode → join parcels → derive acreage" idea, applied ad hoc rather than as reusable infra |
| `jobs/gmaps_enrich.py` | Google Maps Places lookup + website email-scraping for management-company contact info | **Not in the plan.** The plan explicitly avoids Esri/Google geocoding spend (§3) and treats TX contact gaps as a PDF-parsing problem (§6.1.1), not a Maps-enrichment problem |
| `jobs/management_company_dedup.py` | Merges `[PENDING]` placeholder rows into real-name rows via zip + fuzzy address match (rapidfuzz) | A narrow instance of the plan's §5 resolution system — no blocking-key framework, no tiered match/score/threshold pipeline, no `review.match_queue` |
| `jobs/import_hoa_csv.py`, `jobs/clean_hoa_csv.py` | Manual CSV import/cleanup for hand-collected management-company lists | Not in the plan |
| `jobs/orchestrate_pipeline.py` | Runs arcgis → dbpr → gmaps in sequence, with run tracking (`db.start_run`/`finish_run`) | A real but much thinner version of the plan's 5-stage Extract→Land→Stage→Resolve→Serve architecture (§3) — no immutable GCS landing, no per-source manifest with SHA-256/license string |

**Infrastructure:** `aiomysql` (MySQL), `playwright` (browser scraping), containerized via Dockerfile, no GCP/GCS/PostGIS anywhere in `requirements.txt`. The plan specifies Cloud Run Jobs + GCS immutable landing + Cloud SQL for **PostgreSQL** with PostGIS (§3). These are incompatible storage/runtime choices, not a partial implementation of the same one.

**Schema:** flat `hoa_properties` + `management_companies` (+ a contacts table), not the plan's `core.account` / `core.location` / `core.contact` / `core.source_record` four-table model with `parent_account_id` rollups and field-level survivorship (§4–5).

## What the plan describes that has zero repo footprint

- Healthcare, Deathcare, Parks/Municipal, Resort/Hospitality verticals — entirely absent.
- Texas HOA via **TREC bulk CSV + certificate PDFs** (§6.1.1) — repo instead scrapes ArcGIS parcels for HOA discovery.
- Free Sunbiz **SFTP bulk file** — repo pays a third-party API instead.
- GCS raw immutable landing zone, `ingest.source_run` manifest with SHA-256 + license string, Cloud SQL/PostGIS, the tiered entity-resolution engine (§5.1), ArcGIS Hub harvester for parks (§6.4), FSQ OS Places, NSD/IRS BMF/CMS sources, SAM.gov procurement monitoring.

## The one direct overlap: local `connectors/` directory

`connectors/tx_trec_hoa.py` and `connectors/fl_dbpr_lodging.py` in this local project **are** the plan-described connectors referenced in §6.1.1 and §6.5.1 — they implement the TREC CSV and DBPR lodging logic the plan calls for. But:

- They are **untracked in local git** (`git status` shows `?? connectors/`) — never committed here.
- They do **not exist in the GitHub repo at all** (confirmed via full tree listing).

So the most plan-aligned work that exists is sitting locally, uncommitted, and separate from the pipeline repo entirely.

## Recommendation (not part of this report's action, just the honest read)

If the 5-vertical plan is the going-forward direction, the HOA-pipeline repo's *logic* (DBPR name/zip matching heuristics, the `[PENDING]`-merge dedup pattern, the FL county-by-county acreage lookups) is worth harvesting, but its schema, database engine, and Texas data source would all need to be replaced rather than extended. It is not a head start on the plan's architecture — it is a separate, narrower system solving a subset of the same problem a different way.

No code or repo changes were made — this is a read-only findings report.
