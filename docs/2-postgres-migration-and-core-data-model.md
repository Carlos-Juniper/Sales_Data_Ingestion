# Handoff 37 — MySQL → Postgres Migration & Core Data-Model Reorganization

> **Scope (one feature, large):** Move the Juniper CRM off Cloud SQL for MySQL onto the
> Cloud SQL for **PostgreSQL + PostGIS** instance that already hosts the ArcGIS ingestion
> pipeline, and — in the same move — repoint the application at the ingestion pipeline's
> canonical `core.*` tables instead of its own per-vertical copies. This handoff covers the
> database, the data-access layer, the SQL dialect port, the schema reorganization, the data
> load, and the deploy wiring. It does **not** cover frontend work beyond whatever type
> changes fall out of §5, and it does **not** cover the mobile or TanStack Query work.

**Business source:** Carlos Hernandez (verbal, 2026-08-25) — the CRM and the ingestion
pipeline should share one database; the app should read canonical entities, not per-vertical
copies; the app is deployed but **has no users yet**, so there is no production data to
preserve and no migration window to negotiate.
**Depends on:** 15 (canonical properties), 34 + 35 (migration runner — this handoff rewrites it).
**Blocks:** any future ArcGIS map view in-app; the crew-routing phase 2; the mobile work.

---

## 1. Why this exists

Today there are two databases that model the same business objects and cannot talk to
each other.

**The CRM** runs on Cloud SQL for MySQL 8 (`juniper-crm-498215-p5:us-central1:juniper-prod`,
staging `…:juniper-dev` — `cloudbuild.yaml`, `cloudbuild.staging.yaml`). It owns
`crm.properties`, `crm.management_companies`, `crm.hoa_properties`,
`crm.hoa_contact_information`, plus loose `contact_name` / `contact_email` columns on
`crm.leads`.

**The ingestion pipeline** runs on Cloud SQL for PostgreSQL with PostGIS —
`juniper-crm-498215-p5:us-east1:juniper-postgres-prod`
(`ArcGIS Data Ingestion/terraform/variables.tf:31-34`). It owns
`core.account`, `core.location`, `core.contact`, and `core.source_record`
(`db/migrations/003_core_tables.sql`), fed from `staging.resolved_*`
(`db/migrations/011_resolved_tables.sql`) across five verticals.

The ingestion model is the better one. `core.contact` has `role`, `role_rank`
(1 = best guess at decision maker), `account_id` FK, and `is_current`
(`003_core_tables.sql:44-58`). `crm.hoa_contact_information` has an email, a phone, a name,
and a scrape URL (`sql/create_table/hoa_contact_information.sql`) — and it is HOA-only, so
four of the five verticals have nowhere to put a contact at all.

Because the two live on different engines, the only possible bridge is a scheduled ETL
publish job: a thing to write, a thing to schedule, a thing to monitor, and a thing that
drifts. Consolidating onto one Postgres instance deletes that job before it is ever written.

**Secondary reasons, in order of real weight:**

1. **PostGIS is a first-class ArcGIS Pro enterprise geodatabase.** Pro can register the same
   database the CRM reads. The planned in-app map view then renders live data instead of a
   periodically-exported shapefile. On MySQL that path does not exist.
2. **`jsonb` + GIN indexing** replaces MySQL's `JSON` type for `leads.raw_data`,
   `leads.score_factors`, `material_calcs.factors`, `intake_submissions.payload`,
   `scraper_runs.error_messages`, `sam_gov_opportunities.resource_links`,
   `higher_gov_opportunities.documents`, `hoa_properties.raw_data`. Indexed containment
   queries instead of full scans.
3. **pgRouting** exists for the phase-2 crew-routing app.
4. **One instance to operate, back up, patch, and secure** instead of two.

**What is _not_ a reason, and should not be written down as one:** Cloud SQL prices Postgres
and MySQL identically per vCPU and per GB. Postgres is not categorically faster than MySQL 8
for this workload. The cost saving is real but it comes from *running one instance instead of
two* — not from the engine.

---

## 2. Decisions already made

These are settled. Do not relitigate them in the implementation.

| # | Decision | Made by |
|---|----------|---------|
| D1 | The CRM moves to the existing ingestion Postgres instance. One instance, multiple schemas. | Carlos, 2026-08-25 |
| D2 | The app reads canonical entities — contacts, properties, management companies — from `core.*`. Per-vertical tables remain as **raw** tables beneath, not as things the app queries. | Carlos, 2026-08-25 |
| D3 | **No row-for-row data migration.** The app is deployed but unused; there is no transactional data worth preserving. Recreate the schema, re-seed config, re-load entities from the pipeline. See §8. | Carlos, 2026-08-25 |
| D4 | Raw government opportunities are split from qualified leads (the `crm.opportunities` table). This rides along with the migration rather than being a separate rewrite of `crm.leads`. | Carlos, 2026-08-25 |

D3 is the decision that makes this tractable. Everything downstream gets easier once you
stop trying to preserve rows: no dual-write window, no cutover script, no reconciliation
pass, no rollback-with-data-loss scenario. **Confirm D3 against the live database before
writing any code** — see §14 Q1.

---

## 3. Critical finding — `sql/` is not the schema

**Do not build the Postgres schema by translating `sql/create_table/*.sql`.** Those files
have drifted from the live database. Three confirmed divergences:

1. **`property_management_companies` does not exist in `sql/`.** The DDL file is
   `sql/create_table/management_companies.sql` and creates `crm.management_companies`.
   But the application queries `property_management_companies` in 14 places —
   `api/server.py:976, 983, 1026, 1072, 1112, 1126, 1131, 1152, 1175, 1197` and others.
   One of the two names is wrong, and the live database is the arbiter.
2. **`user_graph_tokens` has no DDL anywhere in the repo.** It is read and written by
   `api/graph.py:115, 132`, `api/server.py:620`, and `scripts/migrate_encrypt_tokens.py:35, 49`.
   It was created by hand.
3. **`leads.deleted_at` has no DDL.** It does not appear in `sql/create_table/leads.sql`,
   yet it gates soft-delete in `api/server.py:537` and every `leads` read
   (`db.py:70, 79`, plus 9 sites under `api/` — 8 in `api/server.py` including
   `:1471, 1473`, and `api/pipeline.py:79`).

This is consistent with the history recorded in Handoff 34 §1 — migrations were hand-applied
in an unknown per-environment subset before `scripts/migrate.py` existed.

**Therefore step one is:**

```bash
# against BOTH prod and staging, and diff them
mysqldump --no-data --routines --triggers \
  --host=127.0.0.1 --port=3307 -u "$MYSQL_USER" -p "$MYSQL_DB" \
  > schema-dump-prod-$(date +%F).sql
```

Reconcile that dump against `sql/` and record every divergence in
`handoffs/37-schema-drift-report.md` before authoring any Postgres DDL. Anything in the
dump that is not in `sql/` is either (a) a table the app needs, which must be carried over,
or (b) dead, which must be explicitly declared dead — not silently dropped.

---

## 4. Target architecture

One Cloud SQL for PostgreSQL instance — `juniper-crm-498215-p5:us-east1:juniper-postgres-prod`,
subject to the region decision in §14 Q2. Five schemas, with a clear ownership boundary:

| Schema | Owner | Contents | CRM access |
|--------|-------|----------|------------|
| `core` | ingestion pipeline | `account`, `location`, `contact`, `source_record` | **read**, plus a narrow set of CRM-writable columns (§5.4) |
| `staging` | ingestion pipeline | `resolved_*`, per-source raw tables | none |
| `ingest` | ingestion pipeline | run/provenance bookkeeping | none |
| `review` | ingestion pipeline | match queue, pending pairs | none |
| `crm` | the CRM app | estimates, leads, opportunities, users, config, engagement state | read/write |

Extensions are already enabled on the instance: `postgis`, `pg_trgm`, `fuzzystrmatch`,
`unaccent`, `btree_gin` (`ArcGIS Data Ingestion/db/migrations/001_extensions.sql`). Add
`citext` — §10.1 explains why.

**The ownership boundary is the important part.** The CRM must not write to `core.account`,
`core.location`, or `core.contact` — those are pipeline outputs, and the pipeline's
diff-and-upsert (`staging.resolved_* → core.*`) will overwrite anything the app puts there.
CRM-authored state about a canonical entity lives in `crm` and joins by FK. Enforce this
with a database role: the CRM's role gets `SELECT` on `core`, not `INSERT`/`UPDATE`/`DELETE`.
This is cheap now and expensive to retrofit after the first time someone writes a
`UPDATE core.location SET …` in an endpoint.

---

## 5. Canonical model mapping

### 5.1 The core three

| Today (MySQL `crm`) | Target | Notes |
|---|---|---|
| `crm.hoa_properties` | `core.location` | HOA-only. Superseded — the pipeline produces locations for all five verticals. **Drop.** |
| `crm.hoa_contact_information` | `core.contact` | HOA-only, no role model. **Drop.** |
| `crm.management_companies` (and/or `property_management_companies`) | `core.account` | A management company is a buying entity — exactly what `core.account` models. Carries `account_type`, `external_keys`, `parent_account_id` for system rollups. |
| `crm.properties` | **split** — see §5.2 | Currently mixes canonical identity with CRM engagement state. |

### 5.2 Splitting `crm.properties`

`sql/create_table/properties.sql:27-56` currently holds two unrelated kinds of column:

*Canonical identity* — `property_type`, `source_type`, `source_id`, `name`, `city`, `state`,
`zip`, `management_company_id`. All of this is `core.location` + `core.account`, and all of it
is authored by the pipeline.

*CRM engagement state* — `branch_city`, `customer_type`, `aspire_property_id`,
`aspire_sync_status`, `aspire_sync_error`, `aspire_synced_at`. None of this belongs to the
pipeline; all of it is authored by the app.

Split accordingly:

```sql
-- CRM-authored state about a pipeline-authored location.
CREATE TABLE crm.property_engagement (
  location_id        bigint PRIMARY KEY REFERENCES core.location(location_id),
  branch_city        text,
  customer_type      text,
  aspire_property_id integer,
  aspire_sync_status text NOT NULL DEFAULT 'unsynced'
                     CHECK (aspire_sync_status IN ('unsynced','pending','synced','failed')),
  aspire_sync_error  text,
  aspire_synced_at   timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);

-- Compatibility surface: the shape today's endpoints already expect.
CREATE VIEW crm.properties_v AS
SELECT l.location_id, l.location_name AS name,
       l.site_address, l.geom, l.maintained_acres,
       a.account_id AS management_company_id, a.legal_name AS management_company_name,
       e.branch_city, e.customer_type,
       e.aspire_property_id, e.aspire_sync_status, e.aspire_synced_at
FROM core.location l
LEFT JOIN core.account a ON a.account_id = l.account_id
LEFT JOIN crm.property_engagement e ON e.location_id = l.location_id;
```

The view is what lets §6 be a mechanical port instead of an endpoint rewrite. `api/properties.py`
and the property endpoints in `api/server.py` change their `FROM` target and little else in
the first pass; the deeper cleanup can follow once the migration is green.

### 5.3 Identity types

The CRM uses `VARCHAR(36)` UUID strings for primary keys. `core.*` uses `bigserial`. Do not
try to reconcile these — keep both:

- CRM-owned entities (`estimates`, `leads`, `opportunities`, `users`, config tables) keep
  UUID PKs, but as Postgres native **`uuid`**, not `varchar(36)`. Half the storage, real
  type checking, and `gen_random_uuid()` available server-side.
- References to canonical entities become **`bigint`** FKs: `account_id`, `location_id`,
  `contact_id`. Not string copies of a name.

This means `crm.leads.property_id VARCHAR(36)` becomes
`crm.leads.location_id bigint REFERENCES core.location(location_id)`, and
`crm.estimates.property_id` likewise. Migration 001's "canonical properties" work
(`sql/migrations/001_canonical_properties.sql`) is what made every lead and estimate hang off
a single property id — that design is preserved, only the referent changes.

### 5.4 The one exception to read-only `core`

Sales needs to record engagement state per contact — "called, left voicemail." That is
CRM-authored data about a pipeline-authored row. Do **not** add columns to `core.contact`.
Add:

```sql
CREATE TABLE crm.contact_engagement (
  contact_id     bigint PRIMARY KEY REFERENCES core.contact(contact_id),
  contact_status text,
  last_contacted date,
  assigned_to    uuid REFERENCES crm.users(id),
  notes          text,
  updated_at     timestamptz NOT NULL DEFAULT now()
);
```

Same pattern as `crm.property_engagement`. The rule to state once and follow everywhere:
**the pipeline owns facts about the world; the CRM owns facts about our pursuit of it.**

### 5.5 Opportunities vs leads (D4)

`crm.leads` currently carries ingest-provenance columns that do not belong on a pursuit
table: `source`, `source_ref`, `raw_data`, `score`, `score_factors`, and
`UNIQUE KEY uq_source_ref (source, source_ref)` — `sql/create_table/leads.sql:3-4` (`source`,
`source_ref`), `:26-28` (`score`, `score_factors`, `raw_data`), and `:35` (the unique key).
Government scrapes are inserted directly into `leads` with `source='sam_gov'`, and are
distinguished from rep-qualified leads only by `status`.

Meanwhile `crm.sam_gov_opportunities` and `crm.higher_gov_opportunities` exist and are
**orphaned** — nothing in `api/` or `studio/` reads them (the only reference is a
string→ID mapping in `api/aspire_config.py:133-134`).

Collapse both into one table and give the boundary a real name:

```sql
CREATE TABLE crm.opportunities (
  opportunity_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind           text NOT NULL CHECK (kind IN ('bid','prospect')),
  source         text NOT NULL,          -- sam_gov | higher_gov | pipeline | manual
  source_ref     text,
  title          text,
  agency_name    text,
  posted_date    date,
  due_date       date,
  val_est_low    numeric(15,2),
  val_est_high   numeric(15,2),
  pop_city       text, pop_state char(2), pop_zip text,
  payload        jsonb NOT NULL,         -- the full original scrape record
  location_id    bigint REFERENCES core.location(location_id),
  qualified_at   timestamptz,
  qualified_by   uuid REFERENCES crm.users(id),
  lead_id        uuid REFERENCES crm.leads(id),   -- NULL until qualified
  scraped_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, source_ref)
);
```

`crm.leads` then sheds `source`, `source_ref`, `raw_data`, and `uq_source_ref`. A lead exists
because a rep decided to pursue something. `studio/src/lib/pipelineStages.ts` currently
buckets `new`, `contacted`, and `qualified` into one "Qualifying" column — unreviewed scrapes
sitting next to worked leads. With the split, unqualified opportunities leave the pipeline
board entirely and get their own review surface.

### 5.6 Tables that stay in `crm` unchanged in shape

Everything in `sql/create_table/estimating.sql` — `estimates`, `estimate_sections`,
`section_services`, `section_service_components`, `takeoff_lines`, `estimate_adjustments`,
`estimate_status_transitions`, `catalog_items`, `approval_tiers`, `margin_bands`,
`material_calcs`, `itb_projects`, `itb_scopes`, `itb_scope_status`, `intake_submissions`,
`intake_attachments` — plus `users`, `branches`, `sales_territories`, `bids`, `lead_actions`,
`scraper_runs`, `user_graph_tokens`. These are CRM-owned, have no canonical counterpart, and
change only by type mapping (§6, Appendix A).

---

## 6. Dialect port inventory

Everything below is a verified count from the current tree. Work through it as a checklist.

### 6.1 What ports for free

**Every `%s` placeholder.** 510 occurrences across 253 matching lines in `api/`
(`api/estimating.py` 135 lines, `api/server.py` 77, `api/properties.py` 15, `api/graph.py` 11,
`api/pipeline.py` 11, `api/seed_auth.py` 3, `api/authz.py` 1), plus 5 in `db.py` and 17 in
`scripts/` — 532 total. Of the 253, **11 are `logger.*` format strings, not SQL** — 9 of them
in `api/graph.py` (`:163, 272, 278, 321, 324, 368, 371, 393, 396`, leaving only 2 real SQL
lines in that file), plus `api/server.py:83` and `api/estimating.py:523`. True SQL-bearing
total is **242 lines**. `aiomysql` and `psycopg` 3 share the `pyformat`/`%s` paramstyle. **This is the
single biggest reason to choose psycopg over asyncpg** — asyncpg uses `$1`-style numbered
parameters and would force touching all 253 lines. See §7.

**Absent MySQL-isms.** Zero uses of `LAST_INSERT_ID`, `JSON_EXTRACT`, `GROUP_CONCAT`, or
`IFNULL` anywhere in `api/` or `scripts/`. Verified by grep, not assumed.

**`LIMIT`/`OFFSET`** — portable as written. `LIMIT %s OFFSET %s` at `api/server.py:406, 736,
983`; bare `LIMIT %s` at `api/properties.py:161` and `api/estimating.py:404`.

**`NOW()`** — valid in Postgres. `api/seed_auth.py:98`, `api/graph.py:117, 123`.

**`GROUP BY`** — all three sites (`api/server.py:1471, 1473`,
`scripts/load_catalog_items.py:269`) group by the only non-aggregated column, so Postgres's
strict grouping is already satisfied.

### 6.2 Mechanical rewrites — application code

| Construct | Sites | Change |
|---|---|---|
| `CURRENT_TIMESTAMP()` | 21 occurrences on 16 lines — `api/server.py` ×13 lines, `api/pipeline.py` ×2 (`:100, :261`), `db.py:62`. Several lines carry two (`api/server.py:436, 775, 1033, 1362`). | Drop the parens: `CURRENT_TIMESTAMP`. Postgres rejects the function-call form. Use a global replace, not a per-line count check. |
| `ON DUPLICATE KEY UPDATE x = VALUES(x)` | `api/graph.py:118-123`, `api/estimating.py:2367`, `scripts/load_catalog_items.py:235-245` | `ON CONFLICT (<key>) DO UPDATE SET x = EXCLUDED.x`. **The conflict target must be named explicitly** — MySQL infers it from any unique key, Postgres does not. |
| `ON DUPLICATE KEY UPDATE id = id` (no-op upsert idiom) | `api/properties.py:209`, `api/pipeline.py:147` | `ON CONFLICT (<key>) DO NOTHING`. |
| `is_draft = 1` | `api/estimating.py:1362, 1394, 1414` | `is_draft = true`. Postgres will not coerce integer to boolean once the column is `boolean` (Appendix A). Audit every `TINYINT(1)` column's comparison sites, not just these three. |
| `LOWER(email) = %s` | `api/seed_auth.py:86`, `api/server.py:1549` | Correct today and still correct — but see §10.1, because the *rest* of the codebase relies on MySQL's case-insensitive default collation without saying so. |

### 6.3 Mechanical rewrites — DDL

| Construct | Count in `sql/` | Change |
|---|---|---|
| **Inline `KEY` / `INDEX` / `UNIQUE KEY` clauses inside `CREATE TABLE`** | **82 lines** | **The single largest mechanical DDL item in the port.** Postgres rejects these outright. `UNIQUE KEY` becomes a table-level `UNIQUE` constraint; plain `KEY`/`INDEX` becomes a separate `CREATE INDEX` statement after the table. `PRIMARY KEY (...)` is fine as-is. |
| Backtick identifiers | 312 lines across 25 `.sql` files | Delete, or use double quotes. Prefer deleting — all identifiers are already lowercase. (A repo-wide grep returns 336; the extra 24 are in `sql/migrations/README.md`, which is prose.) |
| `ENGINE=InnoDB` | 33 | Delete. |
| `DEFAULT CHARSET=utf8mb4` | 33 | Delete. |
| `COLLATE=utf8mb4_unicode_ci` | 33 | Delete — but read §10.1 first, because deleting it is what changes comparison behavior. |
| `ENUM(...)` | 30 | `text` + `CHECK (col IN (...))`. **Do not use Postgres native enums** — adding a value later requires `ALTER TYPE`, which has transaction restrictions and no `IF NOT EXISTS`, and the app already validates these values in Pydantic. A `CHECK` constraint is dropped and re-added in one migration. |
| `ON UPDATE CURRENT_TIMESTAMP` | 9 rows across 7 files (8 distinct tables — `properties` appears in both `properties.sql:55` and `migrations/001:55`) | Postgres has no column-level equivalent. Write **one** shared trigger function and attach it per table (§6.4). |
| `TINYINT(1)` | 8 | `boolean`. Then fix comparison sites (§6.2). The other 2 of the 10 total `TINYINT` hits are numeric — see §10.3. |
| `ADD COLUMN … AFTER <col>` | 18 | No Postgres equivalent; column order is not controllable. Drop the `AFTER` clause. |
| `ALTER TABLE … ADD INDEX/KEY` | 12 | Split into a separate `CREATE INDEX`. |
| `MODIFY COLUMN` | 4 (`migrations/005:19`, `012:28, 29`, `013`) | `ALTER COLUMN … TYPE` / `SET`/`DROP NOT NULL` / `SET DEFAULT` — Postgres needs one clause per change. |
| `DROP TRIGGER IF EXISTS crm.trg_…` | 1 — `estimating.sql:84` | Postgres syntax is `DROP TRIGGER IF EXISTS trg_… ON crm.estimates` — the table, not a schema-qualified trigger name. |
| `AUTO_INCREMENT` | 2 — `lead_actions.sql:2`, `hoa_contact_information.sql:2` | `bigint GENERATED ALWAYS AS IDENTITY`. `hoa_contact_information` is being dropped (§5.1), so this is really one site. |
| `INSERT IGNORE` | 1 — `sql/migrations/002:43` | `ON CONFLICT DO NOTHING`. Moot under D3 (§8) — 002 is a backfill that will not run. |
| `JSON` | 8 columns | `jsonb`. Add GIN indexes on the ones actually queried by content. |
| `DELIMITER $$` / `SIGNAL SQLSTATE` trigger | 1 — `sql/create_table/estimating.sql:86-95` | Rewrite as plpgsql (§6.4). |

### 6.4 The two trigger rewrites

**Updated-at.** Replaces all 9 `ON UPDATE CURRENT_TIMESTAMP` clauses:

```sql
CREATE OR REPLACE FUNCTION crm.set_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

-- then, per table:
CREATE TRIGGER trg_<table>_updated_at BEFORE UPDATE ON crm.<table>
FOR EACH ROW EXECUTE FUNCTION crm.set_updated_at();
```

**`estimate_type` immutability.** `sql/create_table/estimating.sql:86-95` today:

```sql
CREATE TRIGGER crm.trg_estimates_type_immutable
BEFORE UPDATE ON crm.estimates FOR EACH ROW
BEGIN
    IF NEW.estimate_type <> OLD.estimate_type THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'estimate_type is immutable …';
    END IF;
END$$
```

becomes:

```sql
CREATE OR REPLACE FUNCTION crm.estimates_type_immutable() RETURNS trigger AS $$
BEGIN
  IF NEW.estimate_type IS DISTINCT FROM OLD.estimate_type THEN
    RAISE EXCEPTION 'estimate_type is immutable and cannot be changed after creation'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_estimates_type_immutable BEFORE UPDATE ON crm.estimates
FOR EACH ROW EXECUTE FUNCTION crm.estimates_type_immutable();
```

Note `IS DISTINCT FROM` rather than `<>` — the column is `NOT NULL` so it does not matter
today, but `<>` is NULL-blind and this is the kind of guard that should not quietly stop
firing if the column ever becomes nullable.

This trigger is also what makes `_build_opportunity_input`'s maintenance fallback
(`api/estimating.py:299`, `DEFAULT_SERVICE_LINE` at `:289-292`) unreachable dead code —
`estimate_type` is `NOT NULL` at insert and immutable thereafter. Preserve the guarantee;
the dead branch can be cleaned up separately.

---

## 7. Driver swap

### 7.1 `db.py` — the one chokepoint

`db.py` is 95 lines and every query in the application funnels through its `query()` and
`execute()`. Swap the driver here and nothing above it needs to know.

**Use `psycopg` 3 with `psycopg_pool.AsyncConnectionPool`.** Not asyncpg. The reason is
§6.1: psycopg keeps the `%s` paramstyle, so all 253 SQL-bearing lines port untouched;
asyncpg's `$1` style would require rewriting every one of them. The ingestion repo already
depends on `psycopg[binary]>=3.1` (`ArcGIS Data Ingestion/pyproject.toml:12`), so this is
also the driver the team already runs.

Changes inside `db.py`:

- `aiomysql.create_pool(...)` (`db.py:32`) → `AsyncConnectionPool(conninfo=..., open=False)`
  plus an explicit `await pool.open()`; `close_pool()` (`db.py:90-95`) becomes
  `await pool.close()`.
- `cursorclass=aiomysql.DictCursor` (`db.py:18`) → `row_factory=psycopg.rows.dict_row`.
- Env vars `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DB` / `MYSQL_HOST` / `MYSQL_PORT` /
  `MYSQL_SOCKET_PATH` → `PGUSER` / `PGPASSWORD` / `PGDATABASE` / `PGHOST` / `PGPORT`.
  The Cloud SQL Postgres Unix socket is `/cloudsql/<instance>/.s.PGSQL.5432`, and libpq
  takes the **directory** as `PGHOST` — so the socket case becomes
  `PGHOST=/cloudsql/<instance>` rather than a separate `MYSQL_SOCKET_PATH` branch. That
  branch (`db.py:_cfg()`) collapses.
- Set `search_path` on connection: `options='-c search_path=crm,core,public'`. This is what
  lets unqualified table names in existing queries keep resolving. Set it explicitly rather
  than relying on the role default, so a role change cannot silently reroute queries.
- Keep the `params if params else None` guard in `query()` and `execute()`
  (`db.py:40-43, 51`). The reason behind it — a literal `%` in the SQL raising "not enough
  arguments for format string" — applies identically to psycopg.

`requirements.txt` currently pins **no MySQL driver at all** — `aiomysql` and `pymysql` are
imported (`db.py:7`, `scripts/migrate.py:30`) but unpinned, meaning the container is
building with whatever a transitive dependency happens to drag in. Fix this on the way
through: add `psycopg[binary]>=3.1` and `psycopg_pool>=3.2` explicitly.

### 7.2 `scripts/migrate.py` — a real rewrite

The migration runner is the one file where the port is not mechanical, because its entire
detection layer is built on MySQL introspection. Handoff 34's design is sound; the
implementation is engine-bound.

| Current | Line | Postgres equivalent |
|---|---|---|
| `import pymysql` | `:30-31` | `import psycopg` |
| `GET_LOCK(%s, %s)` | `:132` | `pg_advisory_lock(hashtext('juniper_crm_migrations')::bigint)` — takes a bigint, not a name. Use `pg_try_advisory_lock` with a retry loop to preserve the 10s timeout semantics. |
| `RELEASE_LOCK(%s)` | `:137` | `pg_advisory_unlock(...)` with the same key. |
| `INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE()` | `:193-194` | `information_schema.tables WHERE table_schema = 'crm'`. Note Postgres stores identifiers **lowercase**; MySQL returns column names uppercase — the `row["cnt"]` accessors survive, the `row["COLUMN_TYPE"]` ones do not. |
| `INFORMATION_SCHEMA.COLUMNS` | `:91-92`, `:204` | Same treatment. |
| `enum_values()` reading `COLUMN_TYPE` | `:219-225` | **No equivalent, and no longer needed** — ENUMs become `text` + `CHECK` (§6.3). `detect_007` depends on this; rewrite it to inspect the `CHECK` constraint in `pg_constraint`, or better, delete it (see below). |
| `RENAME TABLE a TO b` | `:102, :415` | `ALTER TABLE a RENAME TO b`. |
| `schema_migrations.detected TINYINT(1)` | `:42` | `boolean`. |

**Then delete most of it.** Under D3 the entire per-migration detection layer —
`detect_001` through `detect_013`, `check_003_gate` (`:259`), `apply_004`'s three-way branch
(`:397-425`), the 002/003 hazard logic (`scripts/migrate.py:235-257`) — exists solely to reconcile against
databases where migrations 001–012 were hand-applied in an unknown subset. A freshly created
Postgres database is a known clean slate. Collapse `sql/migrations/001…013` into a single
baseline `crm` DDL and keep the runner's *good* parts: the tracking table, the checksum
guard, the advisory lock, the dry-run mode, and the CI wiring from Handoff 35.

This is the largest simplification available in this whole handoff. Take it.

---

## 8. Data load — not a data migration

Under D3, nothing is copied row-for-row from MySQL. Instead:

**Load 1 — config and reference data, from source.** These are generated, not hand-entered,
so regenerate rather than migrate:

- `catalog_items` — `python scripts/load_catalog_items.py` against the Aspire kit workbook
  (the script's output is `INSERT … ON CONFLICT` after the §6.2 rewrite).
- `approval_tiers`, `margin_bands`, `material_calcs`, `itb_scopes` — seed inserts, currently
  spread across `sql/create_table/estimating.sql` and `sql/migrations/007`. Consolidate into
  one `crm` seed file.
- `branches`, `sales_territories` — sourced from Aspire (`api/aspire_config.py`). Re-pull.

**Load 2 — canonical entities, from the pipeline.** This is the whole point of the exercise:
`core.account`, `core.location`, and `core.contact` are already populated by the ingestion
pipeline. The CRM does not import them; it queries them. There is no load step.

**Load 3 — users.** `crm.users` is small and identity-linked (Entra). Re-seed via
`api/seed_auth.py`. Do **not** carry over `user_graph_tokens` — those are OAuth refresh
tokens, encrypted against a KMS key (`scripts/migrate_encrypt_tokens.py`), and users
re-consenting is both cheaper and cleaner than migrating encrypted secrets between databases.

**Load 4 — nothing else.** Estimates, leads, opportunities, bids, intake submissions,
attachments, status transitions: all start empty. **This is the assumption that must be
verified before anything else** (§14 Q1). If any estimate carries a non-null
`aspire_opportunity_id` (`sql/create_table/estimating.sql:64`), that row has synced to
Aspire, that ID is an external reference that cannot be regenerated, and D3 does not hold
for it. `handoffs/ASPIRE-PROD-SYNC-GATE.md` and
`handoffs/35-dry-run-report-prod-2026-08-24.md` suggest prod has been exercised at least in
dry-run; confirm what that left behind.

The verification query, to run against prod MySQL before writing any code:

```sql
SELECT 'estimates'  t, COUNT(*) n, SUM(aspire_opportunity_id IS NOT NULL) synced FROM estimates
UNION ALL SELECT 'leads', COUNT(*), NULL FROM leads WHERE deleted_at IS NULL
UNION ALL SELECT 'intake_submissions', COUNT(*), NULL FROM intake_submissions
UNION ALL SELECT 'bids', COUNT(*), NULL FROM bids
UNION ALL SELECT 'intake_attachments', COUNT(*), NULL FROM intake_attachments
UNION ALL SELECT 'estimate_status_transitions', COUNT(*), NULL FROM estimate_status_transitions;
```

Non-zero `synced` on estimates blocks D3 and turns this into a real migration. Every other
non-zero count is a judgment call for Carlos.

GCS attachment objects are unaffected — they live in `juniper-crm-attachments-prod`
(`cloudbuild.yaml`) and are referenced by key, not stored in the database. If
`intake_attachments` starts empty, the objects are orphaned but harmless; decide whether to
purge the bucket.

---

## 9. Deploy wiring

`cloudbuild.yaml` (prod) and `cloudbuild.staging.yaml` both need:

- `--add-cloudsql-instances=` → the Postgres instance connection name.
- `MYSQL_SOCKET_PATH=/cloudsql/<instance>` → `PGHOST=/cloudsql/<instance>` in
  `--set-env-vars`.
- `--set-secrets`: `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DB` → `PGUSER` / `PGPASSWORD` /
  `PGDATABASE`. Create new Secret Manager secrets rather than repurposing the existing ones;
  the staging build reads `MYSQL_*_STAGING` variants (`cloudbuild.staging.yaml`
  `availableSecrets`) and both sets need to change together.
- `scripts/run_migrations_ci.sh` — the staging build's `run-migrations` step
  (`cloudbuild.staging.yaml`, `id: run-migrations`) shells out to this. It starts a Cloud SQL
  Auth Proxy for MySQL on **3307** (`scripts/run_migrations_ci.sh:20`, `MYSQL_PORT=3307` — not
  the 3306 default); it needs the Postgres proxy on 5432.
- Prod currently has **no** migration step (compare the two files). Handoff 35 deliberately
  gated prod wiring on staging proving the pipeline. That gate still applies — do not add the
  prod step as part of this handoff.

Both instances live in project `juniper-crm-498215-p5`, so the existing Cloud Run service
account's `roles/cloudsql.client` grant carries over unchanged. The **region** does not —
see §14 Q2, which must be resolved before P1.

---

## 10. Behavioral gotchas

These are the ones that pass code review, pass a smoke test, and fail in week three.

### 10.1 Collation — the big one

Every `CREATE TABLE` in `sql/` explicitly declares `COLLATE=utf8mb4_unicode_ci` (33 lines) —
this is not an implicit default the port might overlook, it is written down, and it is
**case-insensitive and accent-insensitive**. Postgres's default collation is
**case-sensitive**, and there is no per-column drop-in replacement. Every string equality
comparison in the codebase silently changes behavior.

Two sites already call `LOWER(email)` explicitly (`api/seed_auth.py:86`,
`api/server.py:1549`) — those are safe. The risk is everywhere that *doesn't*: status
strings, branch names, `sales_territories.id` (a `"City, ST"` string used as a join key
against `users.branch_id`, `estimates.branch`, and `properties.branch_city` —
`sql/create_table/sales_territories.sql:22`), management company name matching, and
`account_key` / `contact_key` deterministic keys.

Mitigation, in order:
1. Make `users.email` and any other email column **`citext`** (add the extension).
2. For the `"City, ST"` branch key, either normalize on write or add a `CHECK` that the value
   is already normalized. A `"Fort Myers, FL"` vs `"Fort myers, FL"` mismatch will produce an
   empty branch scope, not an error — silent wrong answers.
3. Grep every `WHERE <text_col> = %s` and decide, per site, whether case matters. There are
   253 candidate lines; most are ID or status comparisons where exactness is desired.

### 10.2 `DATETIME` → `timestamptz`

MySQL `DATETIME` is timezone-naive; the application stores whatever the session produced.
Cloud Run runs UTC, so in practice everything is already UTC — but the type does not say so.
Use `timestamptz` in Postgres, and set the database timezone to UTC explicitly. Date-only
columns (`site_walk_date`, `due_back_date`, `anticipated_close_date`, `service_start_date`,
`last_contacted`, `bid_deadline`) stay `date` — they are calendar dates, not instants, and
making them `timestamptz` would introduce a timezone bug where none exists today.

The SLA clock on `estimates.due_back_date` (`sql/create_table/estimating.sql:47`) is the one
to watch: it drives queue urgency, and an off-by-one from a timezone conversion is a
business-visible bug.

### 10.3 Booleans

Covered in §6.2 and §6.3, repeated here because it is the most likely source of a runtime
error after the port: once a column is `boolean`, `WHERE is_draft = 1` raises
`operator does not exist: boolean = integer`. It will not silently coerce. That is good — it
fails loudly at the three known sites (`api/estimating.py:1362, 1394, 1414`) rather than
returning wrong rows — but every `TINYINT(1)` column needs its comparison sites audited, not
just the ones grep found for `is_draft`. The full list — **8 distinct columns, 9 DDL
locations** (`intake_submissions.is_draft` and `branches.active` are each declared twice,
once in `sql/create_table/` and once in a migration):
`estimates.notify_bm_rd_on_return` (`estimating.sql:53`), `catalog_items.active`
(`estimating.sql:146`), `itb_scope_status.rebid` (`estimating.sql:285`),
`intake_submissions.is_draft` (`estimating.sql:326` and `migrations/005:20`),
`branches.active` (`branches.sql:21` and `migrations/004:65`),
`higher_gov_opportunities.sole_source` (`higher_gov_opportunities.sql:11`), and
`schema_migrations.detected` (`scripts/migrate.py:42`).

Separately, `leads.priority TINYINT` and `leads.score TINYINT UNSIGNED`
(`sql/create_table/leads.sql:22, 26`) are numeric, not boolean — they become `smallint`
with a range `CHECK`, per Appendix A. Do not let a blanket `TINYINT → boolean` sed catch them.

### 10.4 Transactions

`db.py:_cfg()` sets `autocommit=True`. The psycopg pool defaults to autocommit **off**, which
means a connection that is never explicitly committed will roll back on return to the pool —
writes silently vanish. Set `autocommit=True` on the pool's connection configuration to
preserve current semantics, or introduce explicit transactions deliberately. Do not leave
this to the default.

Related: multi-statement writes that today rely on autocommit and are therefore *not* atomic
(estimate creation writing to `estimates` + `estimate_sections` + `section_services`) will
still not be atomic. Making them atomic is a real improvement Postgres makes easy, but it is
a behavior change — scope it separately, do not smuggle it into the port.

---

## 11. Build order

Each phase should be a separate PR. Do not attempt this as one change.

| Phase | Work | Gate before proceeding |
|---|---|---|
| **P0** | Schema drift audit (§3) + data verification query (§8) + the region decision (§14 Q2). Produce `37-schema-drift-report.md`. | Carlos confirms D3 holds and picks a region. Nothing else starts until both are answered. |
| **P1** | Author the Postgres `crm` schema: one baseline DDL file, types per Appendix A, both triggers (§6.4), the `core` mapping (§5), the read-only role (§4). Apply to a local Postgres + PostGIS container. | Schema applies clean from empty; `docker-compose.yml` in the ingestion repo is a starting point for the local instance. |
| **P2** | Driver swap in `db.py` (§7.1) + `requirements.txt`. Nothing else. | App boots against local Postgres; `/health` green. |
| **P3** | Dialect port of `api/` (§6.2). Mechanical, high-volume, low-risk. | Every endpoint returns 200 against the local instance with seed data. |
| **P4** | Rewrite `scripts/migrate.py` (§7.2) and collapse `sql/migrations/001…013` into the P1 baseline. | `python -m scripts.migrate --dry-run` and a real apply both work against an empty database, twice in a row (idempotency). |
| **P5** | Repoint reads at `core.*` via `crm.properties_v` and the engagement tables (§5.2, §5.4). This is where `api/properties.py`, the property endpoints in `api/server.py`, and their frontend types change. | Property and contact data visible in the app originates from the pipeline, not from a CRM copy. |
| **P6** | `crm.opportunities` split (§5.5) + `pipelineStages.ts` update. | Unqualified scrapes no longer appear on the pipeline board. |
| **P7** | Deploy wiring (§9), staging only. | Staging deploys green, migration step passes, smoke test passes. |
| **P8** | Prod cutover. Separate handoff, gated per §9. | — |

P5 and P6 are the ones with frontend blast radius. P2–P4 are invisible to the user and
should be boring.

---

## 12. Acceptance criteria

- [ ] `37-schema-drift-report.md` exists and accounts for `property_management_companies`,
      `user_graph_tokens`, and `leads.deleted_at` — each either carried over or explicitly
      declared dead.
- [ ] `requirements.txt` pins `psycopg[binary]` and `psycopg_pool`, and no MySQL driver is
      imported anywhere in the tree.
- [ ] `grep -rn "aiomysql\|pymysql\|MYSQL_" api/ scripts/ db.py cloudbuild*.yaml` returns nothing.
- [ ] `grep -rn "CURRENT_TIMESTAMP()\|ON DUPLICATE KEY\|INSERT IGNORE\|ENGINE=InnoDB\|utf8mb4\|COLLATE=\|AUTO_INCREMENT\|TINYINT\|MODIFY COLUMN\|ADD INDEX\|DELIMITER" api/ scripts/ sql/` returns nothing.
- [ ] No inline `KEY` / `INDEX` / `UNIQUE KEY` clauses remain inside any `CREATE TABLE`
      (82 lines in the MySQL DDL — see §6.3); every one has become a separate
      `CREATE INDEX` / table-level `UNIQUE` constraint.
- [ ] No `ADD COLUMN … AFTER <col>` remains (18 lines) — Postgres has no column ordering.
- [ ] `python -m scripts.migrate` applied twice against an empty database produces identical
      end state and reports the second run as fully already-applied.
- [ ] The CRM database role has `SELECT` but not `INSERT`/`UPDATE`/`DELETE` on `core`;
      an attempted write from an endpoint raises a permission error, verified by a test.
- [ ] No CRM table duplicates a `core.*` entity: `hoa_properties`,
      `hoa_contact_information`, and the CRM-side management-company table are gone.
- [ ] Every `TINYINT(1)` comparison site listed in §10.3 has been converted and has a test.
- [ ] `estimate_type` immutability is enforced — a test that attempts the update gets an
      exception, matching MySQL's prior `SIGNAL SQLSTATE '45000'` behavior.
- [ ] `updated_at` advances on UPDATE for all previously-`ON UPDATE CURRENT_TIMESTAMP`
      tables — 9 DDL lines covering **8 distinct tables** (`properties` is declared in both
      `sql/create_table/properties.sql:55` and `sql/migrations/001…:55`) — verified by test,
      not by inspection.
- [ ] Autocommit semantics match the previous behavior (§10.4) — a write via `db.execute()`
      is durable without an explicit commit.
- [ ] Staging deploys green with the migration step passing.

---

## 13. Rollback

Rollback is unusually cheap here, and that is worth stating explicitly so nobody
over-engineers a safety net.

The MySQL instance is not modified by any phase of this work — it is read from in P0 and
otherwise untouched. Until P8, prod continues to serve from MySQL off the unchanged
`cloudbuild.yaml`. Rollback at any phase before P8 is: revert the branch. Rollback at P8 is:
redeploy the previous revision, which still points at the MySQL instance.

Do not delete the MySQL instance at cutover. Keep it running, and take a final export to GCS,
for at least 30 days. Then delete it — an idle Cloud SQL instance nobody remembers is a
recurring bill and an unpatched attack surface.

---

## 14. Open questions for Carlos

**Q1 — blocks everything.** Does D3 actually hold? Run the §8 verification query against
prod. Specifically: has any estimate synced to Aspire and received an
`aspire_opportunity_id`? If yes, that ID cannot be regenerated and the "start empty" plan
needs an exception path for those rows.

**Q2 — region mismatch, needs a decision before P1.** The two instances are in the same
project but **different regions**. The CRM's Cloud Run services and MySQL instance are in
`us-central1` (`cloudbuild.yaml`: `--region=us-central1`,
`…:us-central1:juniper-prod`). The ingestion Postgres instance is in `us-east1`
(`terraform/variables.tf:34`: `juniper-crm-498215-p5:us-east1:juniper-postgres-prod`).

Cross-region works — the Cloud SQL Auth Proxy will connect — but it adds roughly 25–35ms of
round-trip latency to **every query**, and the CRM issues many small queries per request.
On an endpoint doing a dozen sequential queries that is a third of a second of pure network
time added to each page load. It also incurs cross-region egress charges, which cuts against
the consolidation savings.

Three options, in order of preference:

1. **Move Cloud Run to `us-east1`.** Cheapest fix if nothing else pins the services to
   `us-central1`. Requires updating both cloudbuild files, the Artifact Registry host
   (`us-central1-docker.pkg.dev`), the GCS attachment buckets, and `CORS_EXTRA_ORIGINS`.
2. **Move the Postgres instance to `us-central1`.** Requires a dump/restore of the ingestion
   database and a terraform change; the pipeline is batch and can tolerate the outage.
3. **Accept the latency.** Only defensible if measured and found acceptable — measure before
   choosing this, do not assume.

This question did not exist before I checked the terraform, and it is the kind of thing that
surfaces at P7 and costs a week. Decide it at P0.

**Q3.** Is the ingestion instance sized for a second workload? It was provisioned for batch
ETL — bursty, high-throughput, tolerant of latency. The CRM is the opposite: small queries,
latency-sensitive, needs connection headroom. Check `max_connections` against Cloud Run's
concurrency settings before assuming one instance suffices. A read replica for the CRM is the
escape hatch if it does not.

**Q4.** `property_management_companies` vs `management_companies` — which name is live? (§3.1)

**Q5.** Should `crm.opportunities` (§5.5) keep the full flattened SAM.gov / HigherGov column
set, or just the queryable subset plus `payload jsonb`? The flattened version is 40+ columns
of mostly-null; the jsonb version needs a GIN index and slightly more verbose queries. I lean
toward the subset shown in §5.5.

---

## Appendix A — type mapping

| MySQL | Postgres | Notes |
|---|---|---|
| `VARCHAR(36)` (UUID PKs) | `uuid` | `gen_random_uuid()` for defaults. |
| `VARCHAR(n)` (other) | `text` | Postgres gains nothing from a length cap; use `CHECK (length(col) <= n)` only where the limit is a real business rule. |
| `CHAR(2)` (state) | `char(2)` | Keep. |
| `TEXT` | `text` | — |
| `TINYINT(1)` | `boolean` | See §10.3. |
| `TINYINT` / `TINYINT UNSIGNED` (`leads.priority`, `leads.score`) | `smallint` + `CHECK (col >= 0)` | Postgres has no unsigned types. |
| `INT` / `INT UNSIGNED` | `integer` / `bigint` + `CHECK (col >= 0)` | — |
| `BIGINT` | `bigint` | — |
| `SMALLINT` (`hoa_properties.units`) | `integer` | Table is being dropped; the field maps to `core.account.size_metric`. |
| `DECIMAL(p,s)` | `numeric(p,s)` | Identical semantics. |
| `DATETIME` | `timestamptz` | §10.2. |
| `DATE` | `date` | §10.2 — do not promote to timestamptz. |
| `JSON` | `jsonb` | Add GIN indexes where queried by content. |
| `ENUM(...)` | `text` + `CHECK` | §6.3 — not native enums. |
| `AUTO_INCREMENT` | `GENERATED ALWAYS AS IDENTITY` | Not `serial`; identity is the modern form. |
| email columns | `citext` | §10.1. |
| lat/lng pairs (`leads.lat`, `leads.lng` `DECIMAL(9,6)`) | `geometry(Point, 4326)` | The whole reason for the move. Keep the numeric columns too if the API contract exposes them. |
