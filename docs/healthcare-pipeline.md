# Healthcare Pipeline — How It Works

This document explains the healthcare vertical end to end, in the order data
actually moves: source connector → staging → enrichment → entity resolution
(dedup/merge) → resolved tables → core write. It also documents the exact
dedup/survivorship rules, since that's the part with the most subtle logic.

Related: `docs/HANDOFF-end-to-end-pipeline.md` (project-wide design decisions
D1–D9), `connectors/healthcare/things_to_consider.md` (known domain traps),
`docs/postgres-gcp-setup-guide.md` (DB setup).

---

## 1. Sources

Four connectors currently feed the healthcare vertical. Each is an
independent CLI script; each produces the same canonical row shape
(`CANONICAL_COLUMNS` in `connectors/lib/schema.py`) before it ever touches the
database.

| `source_id` | Connector | What it is | Key columns it uniquely contributes |
|---|---|---|---|
| `cms_general` | [cms_provider_data.py](../connectors/healthcare/cms_provider_data.py) `--dataset general` | CMS Hospital General Information (live DKAN API) | CCN (`natural_key`) |
| `cms_nursing_home` | [cms_provider_data.py](../connectors/healthcare/cms_provider_data.py) `--dataset nursing_home` | CMS Nursing Home Compare | CCN (`natural_key`) |
| `va_facilities` | [va_facilities.py](../connectors/healthcare/va_facilities.py) | VA Lighthouse Facilities API, `type=health` only | direct lat/lon from the API |
| `nppes_practice_locations` | [nppes_practice_locations.py](../connectors/healthcare/nppes_practice_locations.py) | NPPES secondary practice locations (satellite clinics/MOBs) | NPI |

A 5th input, `sc_parcel_ingest.py`, is not a facility source — it's a
county-assessor parcel-acreage **enricher** used only for South Carolina (see
§4). `parcel_acreage_enrich.py` is the general-purpose version for the other
four states.

### 1.1 CMS Provider Data (`cms_provider_data.py`)

- Hits `data.cms.gov`'s DKAN datastore API directly (no bulk-CSV download,
  no auth). The `dataset_id` in `config/cms_datasets.yaml` (a UUID like
  `xubh-q36u`) doubles as the datastore resource id.
- Paginates 1,000 rows at a time until the API's reported `count` is reached.
- Column discovery is **hint-based, not positional**: `_find_column()` does a
  case-insensitive substring search (e.g. `_CCN_HINTS = ["ccn", "certification
  number", "provider id", "provider number", "facility_id", "facility id"]`)
  so a minor CMS header rename doesn't silently break the mapping. Unresolved
  columns default to `""` with a stderr warning rather than raising.
- `natural_key` = CCN (or whatever hint column resolves for that dataset).
- Raw bytes are hashed for provenance as a **deterministic JSON
  serialization** (`json.dumps(rows, sort_keys=True, default=str)`), not a
  pandas re-serialization — this is what gets SHA-256'd and uploaded to GCS,
  so the hash is stable across pandas versions/column order.

### 1.2 VA Facilities (`va_facilities.py`)

- Calls the VA Lighthouse API with `type=health`, which excludes
  `national_cemetery`, `benefits`, and `vet_center` types (those are
  ingested separately by `connectors/deathcare/va_cemeteries.py`).
- Unlike the CMS/NPPES sources, VA facilities **carry lat/lon directly** from
  the API (`attributes.lat` / `attributes.long`) — no geocoding is needed for
  this source. This matters later: survivorship prefers VA's coordinates over
  a geocoded value when both exist (§3.5).
- `assert_source_shape()` raises if any `facilityType` value isn't in
  `_KNOWN_FACILITY_TYPES = {"va_health_facility"}` — a hard guard against the
  `type=health` filter silently letting through something new upstream.
- Sandbox vs. production VA API keys are **not interchangeable** — a sandbox
  key 401s against production. `VA_API_BASE_URL` selects the host.

### 1.3 NPPES Practice Locations (`nppes_practice_locations.py`)

This is the most involved connector because NPPES's main file only carries
**one address per NPI** (the primary practice location). Secondary sites
(satellite clinics, additional physical locations) live in a separate `pl_`
file that must be joined in.

- **Entity filter**: keep only `Entity Type Code == "2"` (Organizations, not
  individual providers).
- **Taxonomy filter** — the "NPPES Filter Trap" documented in
  `things_to_consider.md`: filtering to organizations alone is not enough,
  because medical groups can file under a non-facility taxonomy and still
  operate multi-site ambulatory clinics. The filter is deliberately widened to
  `FACILITY_TAXONOMY_PREFIXES = ("261Q", "282", "284", "285", "286", "287")` —
  Ambulatory Health Care, Hospitals, Nursing/Custodial Care — checked against
  all 15 taxonomy columns per row, not just the primary one. Skipping this
  widening silently drops medical office buildings (MOBs) and ASCs, which are
  exactly the kind of standalone-grounds property this pipeline cares about.
- **Join**: `pl_` (secondary locations) **inner-joined** to the filtered org
  list on NPI — an NPI filtered out in the entity/taxonomy step silently drops
  its secondary locations too. This connector only emits secondary sites; the
  main file's own primary address is not emitted here.
- `location_seq` = 1-indexed rank per NPI (`groupby("NPI").cumcount() + 1`),
  and `natural_key = NPI + "|" + location_seq`.
- Only `_MAIN_USECOLS` are loaded from the ~9.7M-row, 330-column main file
  (`usecols=`) to avoid OOM.

### 1.4 The canonical output contract

Every source connector ends by calling `build_canonical()`
(`connectors/lib/schema.py`), which returns a DataFrame with **exactly**
`CANONICAL_COLUMNS` — missing fields become `None`:

```
source_id, natural_key, vertical, account_type,
name_raw, name_normalized, address_line_1, city, state, zip5,
phone_raw, phone_normalized, latitude, longitude,
segment, ein, county_fips, size_metric, size_value, size_unit, source_file
```

That DataFrame is what `upsert_staging()` writes to `staging.<source_id>`.

---

## 2. Landing in staging (`staging.<source_id>`)

Handled by `upsert_staging()` in `connectors/lib/db.py`, called from each
connector's `--write-db` path. In order:

1. **Provenance row first.** `write_source_run()` inserts an
   `ingest.source_run` row (`status='running'`) recording `byte_count`,
   `sha256`, `connector_version`, `license_string`, and (if GCS is
   configured) `raw_uri` — the raw payload is uploaded to GCS *before* this
   row is written, so the URI is available to persist immediately.
2. **Table auto-creation.** `_ensure_staging_table()` runs `CREATE TABLE IF
   NOT EXISTS staging.<source_id>` with the canonical schema (same shape as
   migration `006_staging_tables.sql`) plus a `geom geometry(Point,4326)`
   column and `loaded_at timestamptz DEFAULT now()`. PK is `(source_id,
   natural_key)`.
3. **Identifier safety.** The table name is hyphen-normalized then checked
   against `^[a-z_][a-z0-9_]*$` before any f-string interpolation into SQL —
   a defense against a `source_id` ever containing a colon or other
   SQL-hazardous character.
4. **Row prep (`_prepare_rows`)**:
   - Missing canonical columns are filled with `None`.
   - `latitude`, `longitude`, `size_value` are coerced through
     `pd.to_numeric(..., errors="coerce")`.
   - **Intra-batch dedup**: rows are deduplicated on `(source_id,
     natural_key)` keeping the **last** occurrence. Without this, Postgres's
     `ON CONFLICT DO UPDATE` raises `cannot affect row a second time` the
     moment one connector run produces two rows with the same key in the same
     batch.
   - **NaN scrubbing across every column**, not just the numeric ones — a
     stray pandas float `NaN` reaching a text column via psycopg can produce
     the literal string `"nan"` in the DB, which is exactly the kind of value
     `_is_present()` (§3) has to guard against downstream.
5. **Guard against silent collapse.** Before writing, if more than 1% of rows
   have an empty/blank `natural_key`, `upsert_staging()` raises instead of
   writing. Because the PK is `(source_id, natural_key)`, an all-empty-key
   regression would otherwise silently collapse the whole dataset into a
   single row with no error.
6. **The upsert itself** is `INSERT ... ON CONFLICT (source_id, natural_key)
   DO UPDATE SET <every column>, loaded_at = now()` — computing `geom` from
   `latitude`/`longitude` via `ST_SetSRID(ST_MakePoint(...), 4326)` inline
   when both are present. Re-running a connector against unchanged upstream
   data produces **zero new rows**, only a bumped `loaded_at`.
7. `finish_source_run()` updates the `ingest.source_run` row to
   `status='succeeded'` (or `'failed'` on exception) with the final row count.

At the end of this stage there are up to four populated tables:
`staging.cms_general`, `staging.cms_nursing_home`, `staging.va_facilities`,
`staging.nppes_practice_locations`.

---

## 3. Geocoding enrichment (`geocode_enrich.py`)

CMS and NPPES publish **no coordinates** — only VA does. Every source except
VA needs a lat/lon before spatial matching (Tier 3, §4.4) or parcel lookup
can work at all. `geocode_enrich.py` is a generic enricher usable against any
source's staging table via `--source-id`.

Order of operations:

1. **Load the cache** (`load_cached_geocodes`) from `staging.enrich_geocode`
   for this `source_id`, and pre-populate the input rows' `latitude` from it
   (`apply_cache`). Any row that already has a non-null latitude is skipped —
   this is the idempotency mechanism: re-running the enricher after a partial
   or complete prior run costs **zero additional API calls** for already-geocoded
   rows (D6 in the handoff doc).
2. **Census batch pass (primary path)**: pending rows are chunked into
   batches of `CENSUS_BATCH_SIZE = 2000` and POSTed as a CSV to the Census
   Bureau's `geocoding.geo.census.gov` batch endpoint (`benchmark:
   Public_AR_Current`). Only rows where the response's `Match` field is
   exactly `"Match"` are kept; `No_Match` and `Tie` rows are dropped and fall
   through to the fallback. Census returns coordinates as `"lon,lat"`
   (reversed from the usual convention) — `parse_census_response` splits and
   swaps this explicitly. `match_type == "Exact"` → `geocode_precision =
   "rooftop"`, otherwise `"street"`.
3. **Nominatim fallback (secondary path)**, only for rows Census missed, only
   when `fallback_nominatim=True` (default; `--no-nominatim` disables it).
   Rate-limited to ≤ 1 req/sec (`_NOMINATIM_RATE_LIMIT_SEC = 1.1`, slept after
   *every* call including failures, so a retry burst can't violate OSM's
   usage policy). One request at a time — no batching available.
4. **Unmatched rows get explicit sentinels**, not nulls-with-missing-context:
   `geocode_precision = "no_match"`, `geocode_source = "none"`. This makes a
   "we tried and failed" row distinguishable from "we haven't tried yet" in
   the cache.
5. **Write to `staging.enrich_geocode`** (`upsert_enrich_geocode`), PK
   `(source_id, natural_key)`, `ON CONFLICT DO UPDATE` bumping
   `enriched_at`. Stores lat/lon both as bare `numeric` columns and as a
   PostGIS `geom` point, so spatial queries (`ST_DWithin`) can run directly
   against the cache table without a join-time `ST_MakePoint`.

This cache table is source-agnostic — `enrich_geocode` rows exist for
whichever `source_id` was passed on the CLI, and `healthcare_pipeline.py`
loads all of them scoped to the four healthcare source ids in one query
(§5, step 2).

---

## 4. Parcel / acreage enrichment (secondary, not part of the core merge)

Two connectors add `maintained_acres` to a location once it has coordinates.
These feed `staging.enrich_parcel`, a separate cache table from
`enrich_geocode` — **not currently joined into `healthcare_pipeline.py`**
(the pipeline driver only joins `enrich_geocode`; parcel enrichment is a
downstream/independent enhancement at this point).

- **`parcel_acreage_enrich.py`** — for FL, NC, TX, PA. Routes to a state or
  county ArcGIS FeatureServer registered in `config/parcel_layers.yaml`
  (statewide for FL/NC; per-county-FIPS for TX/PA, since neither has a
  statewide layer — only their top metro counties are registered). Runs a
  spatial point-in-polygon `spatial_point_lookup` against the layer. Sums
  area across multiple returned parcels (`ok_multi_parcel`) for
  multi-building campuses, unions their geometries into a `GeometryCollection`.
  `maintained_acres` is the **total recorded lot area including buildings and
  pavement** — not net landscapable area; `acres_confidence` is hardcoded to
  `"estimated"` until footprint subtraction is built.
- **`sc_parcel_ingest.py`** — for SC only, because SC has no ArcGIS parcel
  layer at all. Reads manually downloaded assessor CSVs for Charleston,
  Greenville, and Richland counties (`SC_COUNTY_CONFIGS`, each county with
  its own column names) and matches locations to parcels by **address fuzzy
  match** (`compound_name_similarity`, same 60/40 Jaro-Winkler/trigram blend
  used in Tier 3 matching — see §4.4) rather than spatially, since these CSVs
  have no geometry. A cheap 5-character address-prefix blocking step limits
  the O(n²) comparison before scoring; threshold 0.70.

Both share the same output contract (`natural_key, state, parcel_id,
maintained_acres, acres_confidence, geometry_source, owner_name,
parcel_count, boundary_geojson, lookup_status, lookup_note`) so they're
interchangeable per-state.

---

## 5. The pipeline driver (`healthcare_pipeline.py`)

This is the orchestrator. `run_pipeline(engine, dry_run=False)` does exactly
six steps, in order:

1. **Load all healthcare staging tables** (`load_all_sources`) — reads
   `staging.cms_general`, `staging.cms_nursing_home`,
   `staging.nppes_practice_locations`, `staging.va_facilities` (via
   `_STAGING_COLS`, a fixed column list), concatenates them into one
   DataFrame, and skips any table that isn't readable (logs a warning rather
   than failing — lets the pipeline run on a partial source set).
   - Adds three columns the merge code expects but staging doesn't carry:
     `phone` (aliased from `phone_normalized`), `site_state` (aliased from
     `state`), and `ccn`/`npi` (defaulted to `""` since staging's canonical
     schema has no dedicated CCN/NPI columns — CCN currently lives in
     `natural_key` for the CMS sources, and NPI in `natural_key` for NPPES;
     see the caveat in §6).
2. **Load the geocode cache** (`load_geocode_cache`) — one query against
   `staging.enrich_geocode` filtered to the four healthcare `source_id`s.
3. **Run `merge_all(raw)`** → `(merged, review_queue)`. This is the
   deduplication stage — see §6 for the full tier-by-tier breakdown.
4. **Join geocodes onto the merged output** (`join_geocodes`) — for each
   merged row that doesn't already have a latitude (i.e., not VA, which
   carries its own coordinates), look up `natural_key` in the geocode cache
   and copy over `latitude`, `longitude`, `geocode_precision`,
   `geocode_source`. Note the join key is the **survivor's own
   `natural_key`** (the highest-priority source's row in that cluster, per
   survivorship) — not every merged-in `natural_key`, so a geocode result
   keyed to a lower-priority cluster member is not picked up here.
5. **Build the three resolved DataFrames** — `resolved_account`,
   `resolved_location`, `resolved_contact` (§7).
6. **Upsert into `staging.resolved_*`**, skipped entirely under `--dry-run`
   (which instead prints row counts for source rows, clusters, and each
   resolved table to stderr).

`staging.resolved_*` are explicitly **pipeline work tables, not audit
tables** — per design decision D5, they are fully re-materialized every run;
a separate process (`connectors/lib/core_writer.py`, §8) does a 3-way diff
from these tables into `core.*`.

---

## 6. Deduplication and merge (`healthcare_merge.py`)

This is the heart of "how duplicates get combined." `merge_all(df)` runs four
stages in strict order — **later tiers only ever operate on rows the earlier
tiers left unmerged** — then survivorship collapses each resulting cluster
into one row.

### 6.0 `prepare()` — normalization and cluster seeding

- `name_normalized = normalize_name(name_raw)` — uppercases, strips corporate
  suffixes (`INC`, `LLC`, `CORP`, etc.), strips non-alphanumerics, collapses
  whitespace (`connectors/lib/normalize.py`).
- `phone = normalize_phone(phone)` — digits only, discarded (`""`) if fewer
  than 10 digits.
- `zip5 = normalize_zip(zip5)` — first 5 digits extracted from any ZIP/ZIP+4
  string; `""` if fewer than 5 digits found.
- `site_state` uppercased and stripped.
- **Every row starts as its own singleton cluster**:
  `cluster_id = "src:" + source_id + ":" + natural_key`. This is deliberately
  content-derived, not a row-position index — it's stable no matter how the
  input DataFrame is shuffled or concatenated, which matters because
  `account_key` (§7) is recomputed from `cluster_id` on every run and must
  land on the same value for the same underlying data regardless of source
  load order.

### 6.1 Tier 1 — exact key merges (CCN, then NPI)

- **CCN first**: every row sharing a non-empty `ccn` gets
  `cluster_id = f"ccn:{ccn}"`. CCN is the strongest identifier available —
  it's a federal regulatory/billing number.
- **NPI second, CCN-protected**: rows sharing a non-empty `npi` get
  `cluster_id = f"npi:{npi}"`, **except** rows already claimed by a `ccn:`
  cluster in the prior step — those keep their CCN assignment even if they
  also carry an NPI. This ordering encodes the priority CCN > NPI directly
  into the cluster-assignment logic, not just into survivorship.
- Note from the handoff doc's caveat: in the current pipeline wiring, `ccn`
  and `npi` are defaulted to `""` for every row by `load_all_sources()` — the
  staging canonical schema has no dedicated CCN/NPI columns, so in practice
  Tier 1 does not currently fire for the wired sources; matching those
  identifiers today would require reading them out of `natural_key`. Tier 1
  is fully implemented and unit-tested; it's a wiring gap, not a logic gap.

### 6.2 Tier 2 — exact composite match

For rows still unmerged after Tier 1: group by the exact tuple
`(name_normalized, zip5, site_state)`. Any group with 2+ rows gets merged
into `cluster_id = f"t2:{name_norm}|{zip5}|{state}"` — deliberately derived
from the join key itself (not from any member row's id), so the result is
identical regardless of input row order.

**Known chain brands are excluded** from this tier
(`KNOWN_CHAINS = {"HCA HEALTHCARE", "ASCENSION HEALTH", "COMMONSPIRIT HEALTH",
"ADVENT HEALTH", "TENET HEALTHCARE", "COMMUNITY HEALTH SYSTEMS"}`) — their
normalized name is shared across many legally and physically distinct
campuses on purpose (a system brand, not a single facility), so an exact
name+zip match on one of these names would wrongly merge unrelated hospitals
that happen to share a zip code and a system name.

### 6.3 Tier 3 — fuzzy match with blocking

For rows still unmerged after Tiers 1–2:

1. **Blocking** (`blocking_keys()` in `connectors/lib/match.py`) generates up
   to three opaque key strings per record so the O(n²) comparison space is
   pruned before any scoring happens:
   - `szn:{state}:{zip5}:{name_prefix(4)}` — always present.
   - `sph:{state}:{phone}` — only if a normalized phone exists.
   - `geo:{floor(lat)*1000 + floor(lon)}` — a coarse ~111km grid cell, only if
     lat/lon exist. This is intentionally coarse; the real spatial threshold
     is enforced later in scoring, not in blocking.
   Two records are only ever compared if they share **at least one** blocking
   key.
2. **Scoring** (`score_pair()`) — a weighted sum over five features, each in
   `[0, 1]`:

   | Feature | Weight | How it's computed |
   |---|---|---|
   | Name | 35% | `compound_name_similarity` = 0.6·Jaro-Winkler + 0.4·trigram overlap, on normalized names |
   | Address | 30% | same compound similarity, on `address_line_1`, 0.0 if either side is blank |
   | Spatial | 20% | Haversine distance between lat/lon, linearly decayed from 1.0 at 0m to 0.0 at 500m; 0.0 if either point is missing |
   | Phone | 10% | binary — 1.0 if normalized phones are equal and non-empty, else 0.0 |
   | Size | 5% | binary — 1.0 if `size_metric` values differ by <20% of the larger, else 0.0; 0.0 if either is missing |

3. **Thresholds**: `score >= 0.92` → auto-merge; `0.75 <= score < 0.92` →
   send to the review queue; `score < 0.75` → not considered a match at all.
4. **Union-Find for transitivity**: auto-merge pairs are NOT applied
   pairwise as they're found — they're first collected, then resolved through
   a union-find structure. This matters because pairwise application would
   miss transitive merges: if A~B scores 0.93 and B~C scores 0.93 but A~C
   was never evaluated (no shared blocking key) or scored below threshold,
   proper connected-component resolution still puts A, B, and C in the same
   cluster, since B bridges them.
5. **Deterministic cluster naming**: each connected component's `cluster_id`
   is `f"t3:{min(all_natural_keys_in_component)}"` — the lexicographic
   minimum of every member's `natural_key`. This, again, is chosen so the
   result doesn't depend on which row happened to be the union-find root
   (root selection is itself order-dependent).
6. **Review queue rows** (`0.75–0.92` band) carry `key_a, key_b, score,
   source_a, source_b`. When `merge_all()` is called with a live `engine`
   (the `--write-db` path; not exercised by `healthcare_pipeline.py`'s
   dry-run-capable driver, which passes `engine=None`), these are upserted
   into `review.pending_pairs` via `enqueue_tier3_matches()` — canonicalized
   so `(A,B)` and `(B,A)` from different runs collapse to one row (ordered
   lexicographically by `(source_id, natural_key)`), and a `status` of
   `'merged'`/`'rejected'` from a prior human review is never overwritten by
   a re-run's `'pending'` default.

### 6.4 Survivorship — collapsing a cluster into one row

For each `cluster_id` group (regardless of which tier produced it),
`survivorship()` builds **one canonical row**, field by field, using a fixed
source-priority order:

```
_SOURCE_PRIORITY = [
    nc_dhsr, pa_doh, fl_ahca, sc_dph,   # phantom — not yet implemented
    va_facilities,
    cms_general,
    cms_nursing_home,
    nppes_practice_locations,
]
```

(Lower index = higher priority; an unrecognized `source_id` sorts last.)

For each field, the rule is: **take the highest-priority source's non-empty
value** (`_is_present()` — rejects `None`, `NaN`, blank strings, and the
literal string `"nan"`). Specifically:

- `name_normalized`, `phone`: first non-empty value walking the
  priority-sorted rows.
- `latitude`/`longitude`: picked **together from the same row**, not
  independently — so a cluster never ends up with VA's latitude paired with
  CMS's longitude. Because `va_facilities` outranks the CMS sources in
  `_SOURCE_PRIORITY`, a merged cluster containing a VA row keeps VA's direct
  API coordinates rather than a later geocoded value.
- `size_metric`: same first-non-empty-by-priority rule (state licensing >
  CMS POS > CMS Care Compare, per the intended priority order — though the
  state sources are currently phantom).
- Every other scalar column: same first-non-empty-by-priority rule,
  defaulting to `""` if no row in the cluster has a value.
- `merged_source_ids`: a comma-joined list of every `natural_key` folded into
  the cluster — this is the audit trail showing which original records
  contributed to the survivor.

The result is one row per cluster, with the same columns as the input plus
`cluster_id` and `merged_source_ids`.

---

## 7. Building the resolved tables

`healthcare_pipeline.py` turns the merged/survivorship output into three
DataFrames matching `staging.resolved_account` / `resolved_location` /
`resolved_contact` (migration `011_resolved_tables.sql`).

### 7.1 Deterministic keys (design decision D1)

All three keys are SHA-256 hex digests, recomputed fresh every run — never
incrementally patched — so that if two previously-separate clusters merge on
a later run (e.g. a new data point pushes a Tier 3 score over 0.92), the
`account_key` changes to reflect the new merged identity rather than keeping
a stale key from one of the two original clusters.

- **`account_key`** — priority `ccn > npi > ein > normalized-name+zip5`:
  ```
  sha256("ccn:<ccn>")                          if ccn present
  sha256("npi:<npi>")                          elif npi present
  sha256("ein:<ein>")                          elif ein present
  sha256("name:<name_normalized>|zip:<zip5>")  otherwise
  ```
- **`location_key`** = `sha256("loc:<account_key>|<addr_line_1_normalized>|<zip5>")`
  — scoped under the account, so one account with multiple physical addresses
  (not the current healthcare merge's typical shape, but structurally
  supported) produces distinct location rows.
- **`contact_key`** = `sha256("contact:<account_key>|<role>|<full_name_normalized>")`.

### 7.2 `resolved_account`

One row per cluster/survivor. Notable field mappings:

- `external_keys` (jsonb) — collects whichever of `ccn`/`npi`/`ein` are
  present on the survivor row into a small dict.
- `mailing_address` (jsonb) — `address_line_1`, `city`, `site_state` (falls
  back to `state` if `site_state` is absent), `zip5`.
- `legal_name` = `name_raw` if present, else falls back to
  `name_normalized`.
- `status` is hardcoded to `"active"` — the merge pipeline doesn't currently
  produce inactive/dissolved/merged statuses; tombstoning to `status='merged'`
  happens later, in `core.account` (§8), not here.
- `_cluster_id` is carried as a side-channel column (dropped before the DB
  write) purely so `resolved_location`/`resolved_contact` can join back to
  the right `account_key` without recomputing it.

### 7.3 `resolved_location`

One row per cluster (i.e., healthcare currently produces at most one
location per resolved account — the survivor's own physical
address+geocode). `geom` is computed at write time from `_latitude`/
`_longitude` via the same `ST_SetSRID(ST_MakePoint(...), 4326)` pattern used
in staging. `geocode_precision`/`geometry_source` come straight from the
geocode join in step 4 of the driver.

### 7.4 `resolved_contact`

Healthcare sources don't supply named contact people, so this table gets **at
most one synthetic contact row per account**, only when a phone number
survived — `role = "primary_phone"`, `full_name = None`. This is explicitly a
structural placeholder ("prevents the contact table from being empty and
allows the 3-way diff to function") rather than a real contact record.

### 7.5 Writing resolved tables

Each `upsert_resolved_*` function does a straightforward `INSERT ... ON
CONFLICT (<key>) DO UPDATE SET <every column>` against its table — full
re-materialization semantics, matching D5: **the resolved tables are meant to
be blown away and rebuilt in shape every run**, not accumulated.

---

## 8. Downstream: the 3-way diff into `core.*`

Not part of `healthcare_pipeline.py` itself, but it's the next and final
stage the healthcare data goes through, implemented once for all verticals in
`connectors/lib/core_writer.py` (driven by `connectors/lib/core_apply.py`).
It reads `staging.resolved_account/location/contact` (the fixed contract from
migration 011) and applies:

- **Content-hash change detection.** For each entity type, a `md5(concat_ws('|',
  <every mutable field>))` expression is computed on both the `staging.resolved_*`
  side and the `core.*` side; a row is only UPDATEd if the hash differs, so an
  unchanged survivor produces zero writes.
- **Inserts** for `account_key`/`location_key`/`contact_key` values not yet in
  `core.*`.
- **Tombstones** (design decision D2) for `core.*` rows whose key no longer
  appears in `staging.resolved_*` — status flips to `'merged'`, with
  `parent_account_id` pointed at the surviving row, rather than deleting
  anything. No new tables are needed for this; `core.account` already has
  both columns.
- **Content-addressed `source_record` upsert** (D8): `UNIQUE (source_id,
  natural_key, payload_sha)` means an unchanged payload on a re-run only
  bumps `last_seen_run_id` rather than inserting a duplicate copy.
- Exposes `dry_run_diff(engine)` (no writes — just counts) and
  `apply_core_diff(engine, run_id)` (transactional write), so "will this run
  create duplicates" is answerable with a read-only query before ever
  touching `core.*`.

---

## 9. Known gaps and caveats (as of this writing)

- **CCN/NPI are not actually wired into Tier 1 today.** `load_all_sources()`
  defaults `ccn`/`npi` to `""` for every row because the staging canonical
  schema has no dedicated columns for them — CCN and NPI currently only exist
  inside `natural_key`. Tier 1 logic is implemented and tested against
  synthetic data, but won't fire on the real staging tables until this
  wiring gap closes.
- **The four phantom state sources** (`nc_dhsr`, `pa_doh`, `fl_ahca`,
  `sc_dph`) are listed in `_SOURCE_PRIORITY` for future use but have no
  connector implementation yet. Their presence is harmless (an unrecognized
  `source_id` just sorts last), but they should not be mistaken for
  implemented sources.
- **Parcel enrichment (`enrich_parcel`) is not joined into
  `healthcare_pipeline.py`** — only `enrich_geocode` is. `maintained_acres`
  does not currently appear in `resolved_location` from a full pipeline run;
  it would need to be added as a join step alongside `join_geocodes()`.
- **The CMS "many CCNs per physical site" problem** (documented in
  `things_to_consider.md`): CMS can issue separate CCNs for a single
  hospital campus's acute care, psych, and rehab units. Tier 1's CCN merge
  will treat these as separate clusters (by design — CCN is the strongest
  identifier), so if a true physical-site rollup ever becomes a requirement,
  a "group by address+zip after CCN merge" pass would need to be added
  on top of this pipeline; it does not exist today.
- **`resolved_location` is 1:1 with the cluster**, not 1:many — the healthcare
  merge does not currently emit multiple physical locations per resolved
  account, even though the schema (location_key scoped under account_key)
  supports it structurally.
