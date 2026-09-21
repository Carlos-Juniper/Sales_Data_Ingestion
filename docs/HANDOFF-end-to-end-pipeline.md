# Handoff: End-to-end idempotent pipeline on GCP

**Audience:** the engineer/agent implementing this. Read this whole file before writing code.
**Prerequisite reading:** `Ingestion-Plan-of-Action.md` §3–§5, `postgres-gcp-setup-guide.md`.

---

## 1. Honest current state

The Postgres foundation is real and working. The pipeline is **roughly one-third built**, and the missing two-thirds is where every duplicate-prevention risk lives.

| Layer | State |
|---|---|
| GCP infra (instance, db, user, secret, bucket, SA + IAM) | ✅ Done, verified |
| `db/migrations/` 001–006 + runner | ✅ Applied, idempotent (re-run skips all 6) |
| `connectors/lib/db.py` | ✅ Works — `get_engine`, `write_source_run`, `finish_source_run`, `upsert_staging` |
| `staging.*` | ⚠️ 4 healthcare tables only. **7 missing** (5 deathcare, 1 hoa, 1 resort) |
| CMS → `staging.cms_general` | ✅ Verified E2E: 5,419 rows, idempotent across 2 runs |
| **`core.account` / `location` / `contact` / `source_record`** | ❌ **Nothing writes to them. Zero code.** |
| **GCS raw landing** (`gs://juniper-ingest-raw`) | ❌ **Nothing writes to it.** Bucket is empty |
| **`review.match_queue`** | ❌ **Nothing writes to it** |
| Deathcare → Postgres | ❌ No staging tables, no DB wiring, **no CLI entrypoints at all** |
| Cloud Run / Scheduler | ❌ Not started. **No Dockerfile, cloudbuild, or Terraform exists** |

**Verified baseline:** 910 unit tests pass, 10 integration tests skip without opt-in. `docker compose` runs `imresamu/postgis:18-3.5-alpine` (PG18 + PostGIS 3.5, matching Cloud SQL's 3.5.2, native arm64).

### The core problem this document solves

`core.account`, `core.location`, and `core.contact` have **`bigserial` primary keys and no other unique constraint**. There is no natural or business key. A second pipeline run has nothing to upsert against, so it *will* insert duplicate accounts. Your stated goal — "every time we run the pipeline it's idempotent and we don't create duplicates" — is **unachievable against the §4 schema as written.** Sections 3–4 below fix that.

---

## 2. Locked design decisions

These were decided in a design review. **Do not re-litigate them**; implement them.

| # | Decision | Rationale |
|---|---|---|
| **D1** | Add `core.account.account_key text UNIQUE`, a deterministic hash of the resolved cluster's strongest external key (CCN > NPI > EIN > normalized name+zip5). Each run recomputes clusters and upserts on `account_key`. | Same input → same key → same `account_id`. Idempotent *and* ids stay stable for downstream CRM. Handles the merge-two-accounts case because keys are recomputed, not incrementally patched. |
| **D2** | On re-key or merge, **tombstone** the old row: `status='merged'`, `parent_account_id` → survivor. Rebuild consults an alias lookup before inserting. | Requires **no new tables** — §4 already has both columns. Auditable, preserves `first_seen`, never breaks a FK. |
| **D3** | Normalize deathcare `source_id` to a **constant per source**; keep the per-row id in `natural_key`. `deathcare_merge` rebuilds the `txdot:<id>` composite as `f"{source_id}:{natural_key}"` where needed. | One meaning everywhere, matches §4 (`natural_key -- source's own id`). Output stays byte-identical so most of the 78 affected assertions still pass. |
| **D4** | **Entity resolution stays in pandas.** Do not rewrite Tier 1/2/3 into SQL. | Reuses tested code; 600k rows fits in memory. §2.6 itself calls the SQL move "a real rewrite of the I/O layer." Revisit at ~10× volume. |
| **D5** | Merge output materializes to `staging.resolved_account` / `resolved_location` / `resolved_contact`, then **one SQL statement set does a 3-way diff** into core. | Makes "prove a re-run won't duplicate" a plain dry-run query. Tombstoning becomes an anti-join. |
| **D6** | Enrichment lands in **separate cache tables** — `staging.enrich_geocode`, `enrich_parcel`, `enrich_irs990` — each PK'd on `(source_id, natural_key)` and upserted. | Doubles as a cache so re-runs don't re-geocode 5,419 rows at 1 req/s. Keeps `staging` = "what the source said", enrichment = "what we derived". |
| **D7** | **Implement raw landing.** Upload real fetched bytes to `gs://juniper-ingest-raw/{source_id}/{date}/{sha256}.json.gz`; record URI in a new `ingest.source_run.raw_uri`. | Enables replay without re-hitting slow APIs. Forces the SHA-256 fix, since you must hash the bytes you upload. |
| **D8** | `core.source_record` becomes **content-addressed**: `UNIQUE (source_id, natural_key, payload_sha)` plus `first_seen_run_id` / `last_seen_run_id`. Unchanged re-run inserts **zero** rows. | §4 as written appends a full payload copy per record per run — ~219M rows/year at daily cadence, duplicating GCS. This is a deliberate, documented deviation from §4. |
| **D9** | **One Cloud Run Job per vertical**, running connectors → enrichers → merge sequentially in-process. One Scheduler trigger per vertical. | Dependency ordering is free, no orchestrator to operate. Fits the 24h job timeout easily once the enrichment cache warms. |

---

## 3. The idempotency contract

This is the acceptance criterion for the whole project. After any two consecutive runs on unchanged upstream data:

| Table | Expected delta | Mechanism |
|---|---|---|
| `staging.<source>` | **0 new rows** | `ON CONFLICT (source_id, natural_key) DO UPDATE`, `loaded_at` bumped |
| `staging.enrich_*` | **0 new rows**, 0 upstream API calls | cache hit on `(source_id, natural_key)` |
| `core.account` | **0 new rows** | `ON CONFLICT (account_key)` |
| `core.location` / `core.contact` | **0 new rows** | deterministic keys, same pattern |
| `core.source_record` | **0 new rows** | `payload_sha` unchanged → only `last_seen_run_id` bumps |
| `ingest.source_run` | **+1 row per connector** | append-only audit log — **intentionally not idempotent** |
| GCS raw objects | +1 object, or 0 if `sha256` unchanged | content-addressed object name |

**Determinism prerequisite.** Cluster ids must be reproducible or `account_key` is worthless. Current state:

| Stage | Cluster id | Deterministic? | Required fix |
|---|---|---|---|
| Tier 1 CCN/NPI | `f"ccn:{ccn}"` / `f"npi:{npi}"` | ✅ | none |
| Tier 2 | anchors to `group.index[0]`'s id → positional | ❌ | derive from own join key: `f"t2:{name_normalized}|{zip5}|{state}"` |
| Tier 3 | `f"t3:{records[root]['natural_key']}"`, union-find root | ❌ order-dependent | use `f"t3:{min(member_natural_keys)}"` |
| Singletons | `df.index.astype(str)` in `prepare()` | ❌ pure row position | `f"src:{source_id}:{natural_key}"` |

Fix all four in `healthcare_merge.py` / `deathcare_merge.py` **before** wiring core writes. Add a test that shuffles input row order and asserts identical cluster ids.

---

## 4. Migrations to write

> **STATUS 2026-09-04: this section is DONE, not pending.** Every migration listed
> below exists on disk and has been applied — including `008_account_key.sql` and
> `011_resolved_tables.sql`, so `core.account` and `staging.resolved_account`
> already exist and carry their unique key indexes. The list has since grown past
> the original plan: `013_review_pending_pairs`, `014_enrich_irs990_status`,
> `015_resolved_vertical`, `016_parks_staging_tables`, `017_parks_spine`.
>
> Read this section as a record of what was built, not as a to-do list. Nothing
> here should be re-created; new work adds a new numbered file.
> `SELECT filename FROM public.schema_migrations ORDER BY filename;` is the
> authoritative list of what is actually applied to a given database.

Numbered continuing from the applied 006. Plain `.sql` under `db/migrations/`, run by the existing `db/run_migrations.py`.

- **`007_staging_tables_remaining.sql`** — the 7 missing per-source tables, same shape as 006 (21 `CANONICAL_COLUMNS` + `geom geometry(Point,4326)` + `loaded_at`, PK `(source_id, natural_key)`):
  `staging.fgdl_cemeteries`, `staging.txdot_cemeteries`, `staging.usgs_nsd`, `staging.va_cemeteries`, `staging.irs_bmf_deathcare`, `staging.tx_trec_hoa`, `staging.fl_dbpr_lodging`.
  This migration's own header used to say parks had no connectors; that is no longer
  true. Parks staging arrived later in `016` (6 park sources) and `017` (the
  3-layer TIGERweb government spine plus the parks side tables).
- **`008_account_key.sql`** — `ALTER TABLE core.account ADD COLUMN account_key text; CREATE UNIQUE INDEX ON core.account (account_key);` Add the equivalent deterministic key to `core.location` and `core.contact`.
- **`009_source_run_raw_uri.sql`** — `ALTER TABLE ingest.source_run ADD COLUMN raw_uri text;`
- **`010_source_record_content_addressed.sql`** — add `payload_sha text NOT NULL`, `first_seen_run_id bigint`, `last_seen_run_id bigint`; drop `UNIQUE (source_id, natural_key, source_run_id)`; add `UNIQUE (source_id, natural_key, payload_sha)`.
- **`011_resolved_tables.sql`** — `staging.resolved_account`, `resolved_location`, `resolved_contact`, each carrying its deterministic key.
- **`012_enrich_tables.sql`** — `staging.enrich_geocode` (lat/lon, precision, source, match_type), `staging.enrich_parcel` (maintained_acres, boundary geometry), `staging.enrich_irs990` (phone_990, contact_name_990). All PK `(source_id, natural_key)`.

**Note:** the runner tracks by filename with **no checksum**, so editing an already-applied migration is silently ignored. Never edit 001–006; always add a new file.

---

## 5. Work by vertical

### 5.1 Healthcare

Sources: `cms_general`, `cms_nursing_home` (both ✅ wired), `nppes_practice_locations`, `va_facilities`.

1. **Rename NPPES source_id.** `nppes_practice_locations.py:39` has `SOURCE_ID = "nppes_pl"` but migration 006 created `staging.nppes_practice_locations`. Because `_ensure_staging_table` auto-creates, wiring as-is silently spawns a **second** table. Rename the constant.
2. **⚠️ Update `_SOURCE_PRIORITY`** (`healthcare_merge.py:58`). It lists `nppes_pl` — the rename breaks survivorship silently (unknown source → `len(_SOURCE_PRIORITY)`, sorts last). It also lists `"va"`, but `va_facilities.py` has **no `source_id` at all**; pick one name and use it in both places. It further lists 4 sources that **do not exist yet** (`nc_dhsr`, `pa_doh`, `fl_ahca`, `sc_dph`) — harmless, but don't mistake them for implemented.
3. **Give `va_facilities.py` a `SOURCE_ID`** and wire DB writes (needs `VA_API_KEY`, already in `.env`).
4. **Wire NPPES + VA** following the CMS pattern: sha256/byte-count → `write_source_run` → `build_canonical` → `upsert_staging` → `finish_source_run`. Add `--write-db` to each.
5. **Geocoding is mandatory, not optional.** `staging.cms_general` has `count(geom) = 0` across all 5,419 rows — CMS publishes no lat/lon, so every `ST_DWithin` on CMS data silently matches nothing. `geocode_enrich.py` must run in the pipeline, writing to `staging.enrich_geocode`. It is currently written for NPPES; generalize it.
6. **Add a merge driver.** `healthcare_merge.py` has **no entrypoint** — `merge_all(df)` is importable only. Write a driver that reads `staging.*` + `enrich_*`, runs the merge, writes `staging.resolved_*`.

### 5.2 Deathcare — the larger job

Sources: `fgdl_cemeteries`, `txdot_cemeteries`, `usgs_nsd`, `va_cemeteries`, `irs_bmf_deathcare`. Enricher: `irs_990_enrich`.

**Nothing here is runnable from a shell.** All 7 modules are libraries of `fetch()` / `normalize()` / `to_canonical()` with **no `if __name__ == "__main__"`**, and `deathcare_merge.py:21` states outright: *"No CLI entry point — import and call merge_pipeline() or individual stages."*

1. **Add `main()` + argparse to all 5 source connectors** (`--out`, `--write-db`, source-specific options). Follow `cms_provider_data.py`, but **avoid its two-pass-parse bug** — `--help` there fires before `--dataset`/`--write-db` register, so the help output is incomplete.
2. **Apply D3.** Change `source_id="txdot:" + df["natural_key_str"]` → `source_id="txdot_cemeteries"`, keeping `natural_key=df["natural_key_str"]`. Same for `fgdl:`, `nsd:`, `va:`, `irs_bmf:`.
3. **Rebuild the composite in `deathcare_merge`.** `merged_sources` is a comma-joined list of per-record ids (see lines 179, 199) and line 42 documents the IRS BMF prefix as load-bearing. Derive `f"{source_id}:{natural_key}"` at merge time so output is unchanged.
4. **Update the ~78 test assertions** referencing `txdot:` / `fgdl:` / `nsd:` / `va:` / `irs_bmf:`. Most should pass unchanged if step 3 preserves the output format; the ones asserting on *staging/canonical* values need updating.
5. **Wire DB writes** for all 5, plus `irs_990_enrich` → `staging.enrich_irs990`.
6. **Add a merge driver** for `merge_pipeline()`, same shape as healthcare's.

### 5.3 HOA / Resort / Parks

`tx_trec_hoa.py` and `fl_dbpr_lodging.py` each already have a constant `SOURCE_ID` and an entrypoint — wire them the same way once 007 lands. ~~**`parks` has no connectors at all**; treat that vertical as unstarted, not merely unwired.~~ **Superseded 2026-09-04:** parks is now the most complete vertical after healthcare — `parks/park_layers.py` (6 sources), `parks/gov_units.py` (3 TIGERweb layers), `parks/manager_resolve.py`, `parks/parks_merge.py`, `scripts/run_parks.sh`, and a Terraform job.

---

## 6. Bugs to fix (verified unfixed)

These were previously scoped and skipped. Verified still present.

1. **C1 — no intra-batch dedup** in `connectors/lib/db.py`. `ON CONFLICT DO UPDATE` with `executemany` raises `cannot affect row a second time` if one batch holds two rows sharing `(source_id, natural_key)`. The CMS run passed only because all 5,419 keys happened to be distinct. **Highest-priority fix** — it's the live idempotency risk. Dedupe on `natural_key` in `_prepare_rows` (keep last).
2. **B4 — SHA-256 is not a payload hash.** `cms_provider_data.py:353` hashes `raw.to_json(orient="records")` — a pandas re-serialization, so it varies with pandas version, column order, and NaN rendering. `byte_count` (7,339,540) is not the transferred size. Subsumed by D7: hash the real bytes you upload.
3. **B5 — no hard guard on empty `natural_key`.** `report_quality` measures it (line 271) but nothing blocks the write. Because the PK is `(source_id, natural_key)`, an all-empty regression collapses the entire dataset to **one row with no error** while the CSV count still prints correctly. Add a threshold guard that aborts the DB write.
4. **C2 — NaN cleaned only for 3 numeric columns** (`db.py:47`). A float `NaN` in any text column reaches the DB as-is.
5. **C3 — `_STAGING_COLS` is a hand-copied duplicate** of `CANONICAL_COLUMNS`. Import it instead; three places currently need lockstep edits.
6. **C4 — table name f-string-interpolated with only `.replace("-","_")`.** Colons aren't handled. Add an identifier whitelist (`^[a-z_][a-z0-9_]*$`) before interpolation. D3 removes the colon sources, but keep the assertion.
7. **`docker-compose.yml` binds `0.0.0.0:5432`.** The file's own comment says loopback; the value was never changed. Set `"127.0.0.1:5432:5432"`.
8. **`pytest.ini` is at `connectors/`, not the repo root.** Running bare `pytest` from root resolves `inifile = None`, so the `integration` marker registration is silently ignored — currently masked by `conftest.py` re-registering it. Move to root.
9. **No packaging.** Connectors need `PYTHONPATH=connectors`; a bare `python connectors/healthcare/cms_provider_data.py` fails with `ModuleNotFoundError: No module named 'lib'`. Fix before containerizing.

---

## 7. GCP deployment (Part 1 step 8)

Nothing exists yet — no Dockerfile, no cloudbuild, no Terraform.

1. **Dockerfile** — `python:3.13-slim` (match the local `.venv`), install `connectors/requirements.txt`, set `PYTHONPATH=/app/connectors` (fixes §6.9). One image for all verticals; entrypoint selects the vertical.
2. **Per-vertical entrypoint script** implementing D9: connectors → enrichers → merge → core diff, sequential, exiting non-zero on any stage failure.
3. **Cloud Run Jobs** — one per vertical, using SA `ingestion-connector@juniper-crm-498215-p5.iam.gserviceaccount.com` (already holds `cloudsql.client`, bucket-scoped `storage.objectAdmin`, secret-scoped `secretmanager.secretAccessor`).
4. **`DATABASE_URL` from Secret Manager.** Currently only the password is stored (`ingest-db-password`). Either add a full-URL secret or compose it at startup. Cloud Run reaches Cloud SQL via the built-in connector (`--set-cloudsql-instances juniper-crm-498215-p5:us-east1:juniper-postgres-prod`) — no Auth Proxy sidecar needed.
5. **Cloud Scheduler** — one trigger per vertical, cadence matched to upstream: CMS quarterly, NPPES weekly, IRS BMF monthly, state sources per publisher.
6. **Consider private IP + Serverless VPC Access** later; public IP + the Cloud SQL connector is fine to start (guide Part 1 step 2).

---

## 8. Verification

Run in order; do not skip ahead on failure.

```bash
# 1. Baseline — must stay green
.venv/bin/pytest                       # 910 passed, 10 skipped expected

# 2. Migrations
docker compose up -d postgres
.venv/bin/python db/run_migrations.py   # 007-012 apply
.venv/bin/python db/run_migrations.py   # all skip -> idempotent

# 3. Determinism (new test, gates everything else)
#    shuffle staging row order, assert identical cluster_ids and account_keys

# 4. Per-connector, twice each
PYTHONPATH=connectors .venv/bin/python connectors/<vertical>/<connector>.py --write-db
# then re-run and assert: staging rows unchanged, +1 ingest.source_run row

# 5. Full vertical pipeline, twice
#    assert the §3 idempotency contract table holds exactly

# 6. Cloud SQL via proxy on 5433 (NEVER 5432 - docker uses it, and whichever
#    is running silently answers the same DATABASE_URL)
cloud-sql-proxy --port 5433 juniper-crm-498215-p5:us-east1:juniper-postgres-prod
```

**Never point the integration tests at Cloud SQL.** They run DDL and `DROP TABLE` on `staging.test_*`. The `ALLOW_DESTRUCTIVE_DB_TESTS=1` guard exists because a `localhost` check is insufficient — the Auth Proxy also listens on `127.0.0.1`.

**Dry-run query to add** (the operational proof of D5): report insert/update/tombstone counts from the 3-way diff between `staging.resolved_*` and `core.*` *without* applying it. Run this before every production apply.

---

## 9. Suggested sequencing

1. §6 bug fixes — C1 first, it's the live idempotency defect.
2. §3 determinism fixes + the shuffle test. **Everything downstream depends on this.**
3. Migrations 007–012.
4. Healthcare: rename NPPES, fix `_SOURCE_PRIORITY`, wire VA + NPPES, generalize geocoding.
5. Core write path: `staging.resolved_*` + the 3-way SQL diff + tombstoning. **First time anything writes to `core`.**
6. D7 raw landing (GCS + `raw_uri`).
7. Deathcare: entrypoints, D3 normalization, test updates, wiring, merge driver.
8. HOA + Resort wiring.
9. §7 containerize + deploy per-vertical Jobs + Scheduler.
10. `review.match_queue` for Tier-3 pairs scoring 0.75–0.92 (§5.1) — still unimplemented, lowest priority.

## 10. Open items not decided

- **Survivorship for the 4 phantom sources** in `_SOURCE_PRIORITY` — decide whether `nc_dhsr` / `pa_doh` / `fl_ahca` / `sc_dph` are planned or should be removed.
- **`connector_version`** is hardcoded `"1.0"` in CMS. Decide whether it tracks git SHA or stays manual.
- **`license_string`** is accepted by `write_source_run` but never populated.
- ~~**`parks` vertical** — no connectors, no sources identified.~~ **Closed 2026-09-04.** 9 sources built and dry-run verified; see `docs/parks-pipeline.md`. Still open within parks: the ArcGIS Hub harvester, NCES school districts, SAM.gov procurement, medians/ROW, and mowable-turf estimation.
- **Cleanup:** the empty `ingestion-db` database still exists on the instance. `gcloud` can't drop it (not owner); run `DROP DATABASE "ingestion-db";` as the `postgres` superuser via Cloud SQL Studio. Harmless.
