# Postgres & GCP Setup Guide

**Scope:** the shared Cloud SQL/Postgres backbone for all five verticals (hoa, deathcare, healthcare, parks, resort), per `Ingestion-Plan-of-Action.md` §3–§4. This is not healthcare-specific — one instance backs everything.

**Current state (as of this doc):** nothing writes to Postgres anywhere in the repo. Every connector (`connectors/*/`) ends by writing a local CSV via `lib/schema.py`'s `build_canonical()`. No `connectors/lib/db.py` exists. `connectors/requirements.txt` has no DB driver. This guide is the path from that state to a working Stage 1–4 pipeline against Cloud SQL.

---

## Part 1 — GCP infrastructure

Do this once. Independent of any code changes below — can happen in parallel.

1. **Enable APIs:** Cloud SQL Admin, Cloud Storage, Secret Manager, Service Networking (only if you go private-IP), Cloud Run, Cloud Scheduler.
   ```
   gcloud services enable sqladmin.googleapis.com storage.googleapis.com \
     secretmanager.googleapis.com run.googleapis.com cloudscheduler.googleapis.com
   ```

2. **Create the Cloud SQL for PostgreSQL instance.** The provisioned instance is **Postgres 18** with PostGIS 3.5.2 — match this locally (see docker-compose below). For connectivity, start simple: **Cloud SQL Auth Proxy** over the instance's public IP, run on **port 5433** locally so it never collides with the local docker Postgres on 5432 — no VPC connector needed yet, and it's what you'll also use for local dev. Switch to private IP + Serverless VPC Access only when Cloud Run Jobs need to reach it directly (Part 1, step 8).
   ```
   gcloud sql instances create ingestion-db \
     --database-version=POSTGRES_16 --tier=db-custom-2-8192 \
     --region=us-central1 --storage-auto-increase
   ```
   Volume is small (§3 estimates 300k–600k rows total) — don't over-provision.

3. **Create one database** — schemas (`staging`, `core`, `ingest`, `review`) live inside it, not as separate Cloud SQL databases.
   ```
   gcloud sql databases create ingestion --instance=ingestion-db
   ```

4. **Create the app user.** One user is fine for now given team size; split into a migration-admin user vs. a least-privilege connector user later if this becomes a real hardening concern.
   ```
   gcloud sql users create ingest_app --instance=ingestion-db --password=<generate-this>
   ```

5. **Put credentials in Secret Manager**, not `.env`, for anything that will eventually run in Cloud Run. Local dev keeps using `.env` (gitignored) pointing at `localhost` via the Auth Proxy — same pattern the repo already uses for `VA_API_KEY`.
   ```
   printf '%s' "$DB_PASSWORD" | gcloud secrets create ingest-db-password --data-file=-
   ```

6. **Create the GCS raw-landing bucket** (§3 Stage 2 — separate from Postgres, but its manifest row lands in `ingest.source_run`, so stand it up together):
   ```
   gcloud storage buckets create gs://juniper-ingest-raw --location=us-central1 \
     --uniform-bucket-level-access
   gcloud storage buckets update gs://juniper-ingest-raw --versioning
   ```
   Add a lifecycle rule to transition to Nearline at 90 days (console or a lifecycle JSON via `gcloud storage buckets update --lifecycle-file`).

7. **Service account + IAM** for connectors: `roles/cloudsql.client`, `roles/storage.objectAdmin` (scoped to the bucket), `roles/secretmanager.secretAccessor`.

8. **Defer Cloud Run Jobs + Cloud Scheduler.** That's Stage 1 deployment — do it after a connector writes to Postgres successfully end-to-end locally (Part 2), not before. Standing up scheduled jobs against a schema that doesn't exist yet just means debugging two things at once.

---

## Part 2 — Code changes

### 2.1 Local dev parity

Add a `docker-compose.yml` running a PostGIS image pinned to the same major version as the Cloud SQL instance (Postgres 18 / PostGIS 3.5). Use `imresamu/postgis:18-3.5-alpine` — the official `postgis/postgis` repo has no arm64 build (and no `18-3.4` tag), so it won't run on Apple Silicon. Note the PG18 images moved the default `PGDATA` to `/var/lib/postgresql/18/docker` and declare the volume at `/var/lib/postgresql` (with a `data -> .` symlink), so mount the named volume at `/var/lib/postgresql` — mounting the old `/var/lib/postgresql/data` path makes `initdb` see the non-empty parent and refuse. This is already handled in the committed `docker-compose.yml`. This lets migrations and the merge module get tested without live GCP access, and matches how connector unit tests already run offline (mocked HTTP, in-memory DataFrames).

### 2.2 Dependencies

Add to `connectors/requirements.txt`:
- `psycopg[binary]` — Postgres driver
- `rapidfuzz` — already imported by `connectors/lib/match.py` (healthcare Tier 2/3 matching) but missing from requirements; unrelated to Postgres but currently a broken install
- `google-cloud-secret-manager` — only needed once Cloud Run Jobs pull secrets directly; skip for now if local/CI both use the Auth Proxy + `.env`

### 2.3 Migrations

Given the codebase's existing style (no ORM, no framework abstraction beyond what's needed), use plain numbered `.sql` files under a new `db/migrations/` directory plus a small runner that tracks applied filenames in a `schema_migrations` table — not Alembic or a full migration framework.

- **`001_extensions.sql`**
  ```sql
  CREATE EXTENSION IF NOT EXISTS postgis;
  CREATE EXTENSION IF NOT EXISTS pg_trgm;
  CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;
  CREATE EXTENSION IF NOT EXISTS unaccent;
  CREATE EXTENSION IF NOT EXISTS btree_gin;
  ```

- **`002_schemas.sql`**
  ```sql
  CREATE SCHEMA IF NOT EXISTS staging;
  CREATE SCHEMA IF NOT EXISTS core;
  CREATE SCHEMA IF NOT EXISTS ingest;
  CREATE SCHEMA IF NOT EXISTS review;
  ```

- **`003_core_tables.sql`** — the four tables + indexes verbatim from `Ingestion-Plan-of-Action.md` §4 (`core.account`, `core.location`, `core.contact`, `core.source_record`). Copy that DDL directly; it's already final.

- **`004_ingest_source_run.sql`** — the manifest table §3 Stage 2 describes but doesn't give literal DDL for:
  ```sql
  CREATE TABLE ingest.source_run (
    source_run_id     bigserial PRIMARY KEY,
    source_id         text NOT NULL,
    run_started_at    timestamptz NOT NULL DEFAULT now(),
    byte_count        bigint,
    sha256            text,
    row_count         integer,
    connector_version text,
    license_string    text,
    status            text NOT NULL DEFAULT 'running'  -- running|succeeded|failed
  );
  ```

- **`005_review_match_queue.sql`** — Tier 3 pairs scoring 0.75–0.92 per §5.1:
  ```sql
  CREATE TABLE review.match_queue (
    match_queue_id    bigserial PRIMARY KEY,
    source_record_a   bigint NOT NULL REFERENCES core.source_record(source_record_id),
    source_record_b   bigint NOT NULL REFERENCES core.source_record(source_record_id),
    score             numeric NOT NULL,
    feature_breakdown jsonb,
    status            text NOT NULL DEFAULT 'pending',  -- pending|merged|rejected
    created_at        timestamptz NOT NULL DEFAULT now(),
    resolved_at       timestamptz,
    resolved_by       text
  );
  ```

- **`006_staging_tables.sql`** — one design call to make explicitly: the plan (§3 Stage 3) says per-source tables with source-native column names. But every connector already normalizes into one shape — `CANONICAL_COLUMNS` in `connectors/lib/schema.py` — before it ever hits disk. Re-deriving native-column staging tables would mean throwing that normalization away and redoing it in SQL. **Recommendation:** make each staging table (`staging.cms_hospital_general`, `staging.va_facilities`, `staging.nppes_practice_locations`, etc.) match `CANONICAL_COLUMNS` plus a `geom geometry(Point,4326)` column, generated from `latitude`/`longitude`. This keeps Stage 3 = "what the connector already produces," and defers the plan's literal per-source-native-column intent unless a specific source turns out to need it.

### 2.4 `connectors/lib/db.py` (new)

A small shared module, matching the style of `lib/http.py` / `lib/geo.py`:
- `get_engine()` — reads `DATABASE_URL` from env and rewrites the scheme to `postgresql+psycopg://` for SQLAlchemy (we ship psycopg 3, not psycopg2; `db/run_migrations.py` still gets the bare libpq form). Locally, point it at **local docker on 5432** (`postgresql://ingest_app:...@localhost:5432/ingestion`) or at **Cloud SQL via the Auth Proxy on 5433** (`...@localhost:5433/...`); in Cloud Run: same env var, populated from Secret Manager.
- `write_source_run(...)` / `finish_source_run(...)` — insert/update the `ingest.source_run` row around each connector's fetch step.
- `upsert_staging(engine, source_id, df)` — writes a canonical DataFrame into `staging.<source_id>`, keyed on `(source_id, natural_key)`.

### 2.5 Wire the connectors

Each connector currently ends with `df.to_csv(args.out, index=False)`. Add, without removing the CSV write (keep it during the transition — cheap and useful for diffing):
1. Compute SHA-256 + byte count of the raw fetched payload before parsing.
2. `write_source_run(...)` into `ingest.source_run`.
3. `upsert_staging(...)` the canonical DataFrame into `staging.<source_id>`.

Do this for one connector first (`cms_provider_data.py --dataset general` — the most live-verified one per the handoff doc) before touching the rest.

### 2.6 Entity resolution (Stage 4)

`connectors/healthcare/healthcare_merge.py` and the Tier 2/3 scoring functions just added to `connectors/lib/match.py` currently operate on in-memory DataFrames built from local CSVs. Once staging tables exist, the natural next step — a real rewrite of the I/O layer, not a small patch — is:
- Blocking-key candidate generation and Tier 3 scoring can move partly into SQL (`similarity()` from `pg_trgm`, `ST_DWithin` on `geom`) instead of pandas, since Postgres already has the extensions for it.
- Auto-merges (≥0.92, and all Tier 1/2 matches) write into `core.account` / `core.location` / `core.source_record`.
- 0.75–0.92 pairs write into `review.match_queue` instead of being handled ad hoc.

### 2.7 Testing

Keep the existing pattern — mocked HTTP + in-memory DataFrames — for connector unit tests; they shouldn't need a live DB. Add a separate integration tier (`pytest -m integration`, or a separate test dir) that runs against the docker-compose Postgres, covering `lib/db.py`, the `ingest.source_run` writes, and the merge module's SQL once it exists.

---

## Suggested sequencing

1. GCP: Part 1 steps 1–7 (skip step 8 for now) — can start immediately, no code dependency.
2. Code: docker-compose Postgres + run migrations 001–006 locally; verify schema before touching Cloud SQL at all.
3. Code: `lib/db.py` + wire `cms_provider_data.py --dataset general` end-to-end against local Postgres.
4. Point the same connector at Cloud SQL via the Auth Proxy — confirm identical behavior.
5. Wire the remaining connectors (VA, NPPES, parcel enrich, state supplements as they land).
6. Migrate `healthcare_merge.py`'s I/O from local CSV to `staging.*` / `core.*` / `review.match_queue`.
7. Only then: Cloud Run Jobs + Cloud Scheduler (Part 1 step 8) for production cadence.
