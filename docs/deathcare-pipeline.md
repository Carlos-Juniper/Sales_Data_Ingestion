# Deathcare Pipeline — End-to-End Reference

This document describes, in execution order, how the deathcare vertical moves
data from five public sources to deduplicated, lead-flagged `staging.resolved_*`
rows. It covers every connector, the canonical schema they must produce, the
five-stage merge/dedup algorithm, the account/location/contact resolution step,
and the optional IRS 990 enrichment pass.

Code lives in `connectors/deathcare/` (per-source connectors + merge) and
`connectors/lib/` (shared helpers used by every vertical).

## 1. Overview — the shape of the pipeline

```
 ┌─────────────────┐  ┌──────────────────────┐  ┌────────────────┐  ┌───────────────────┐  ┌────────────────┐
 │ usgs_nsd.py      │  │ irs_bmf_deathcare.py │  │ va_cemeteries  │  │ txdot_cemeteries  │  │ fgdl_cemeteries│
 │ (USGS NSD API)   │  │ (IRS BMF CSV)        │  │ .py (VA CSV)   │  │ .py (TxDOT API)   │  │ .py (FL API)   │
 └────────┬─────────┘  └───────────┬──────────┘  └───────┬────────┘  └─────────┬─────────┘  └───────┬────────┘
          │  canonical DataFrame (CANONICAL_COLUMNS)      │                    │                    │
          └──────────────┬─────────────────────────────────────────────────────────────────────────┘
                          ▼
                 staging.<source_id>   (5 tables, one per connector, upsert_staging)
                          │
                          ▼
              deathcare_merge.py :: run_pipeline()
       ┌──────────────────────────────────────────────────┐
       │ 1. load_sources    concat all 5 staging tables    │
       │ 2. dedup_by_ein    collapse duplicate BMF EINs    │
       │ 3. spatial_dedup   merge co-located (≤150 m)      │
       │ 4. resolve_segment fill remaining None segments   │
       │ 5. filter_leads    flag is_lead                   │
       └──────────────────────────────────────────────────┘
                          │  merged DataFrame
                          ▼
        _build_resolved_account / _location / _contact
                          │
                          ▼
   staging.resolved_account · staging.resolved_location · staging.resolved_contact

(optional, feeds off irs_bmf_deathcare canonical output before/independent of merge)
   irs_990_enrich.py → ProPublica API → staging.enrich_irs990
```

Every source connector is independent, fetches from its own API/file, and
writes into its **own** staging table (`staging.usgs_nsd`, `staging.irs_bmf_deathcare`,
`staging.va_cemeteries`, `staging.txdot_cemeteries`, `staging.fgdl_cemeteries`).
`deathcare_merge.py` is the only module that reads across all five and produces
the resolved (deduplicated) output.

## 2. The canonical schema (the contract between connectors and merge)

Defined in `connectors/lib/schema.py`. Every connector's `to_canonical()`
function must emit exactly these columns (`build_canonical()` fills any column
the connector doesn't set with `None`, so gaps are the norm, not the exception):

| Column | Meaning |
|---|---|
| `source_id` | Per-source constant, e.g. `"usgs_nsd"`, `"irs_bmf_deathcare"` |
| `natural_key` | Per-row identifier stable within that source |
| `vertical` | Always `"deathcare"` for this pipeline |
| `account_type` | `"cemetery"` or `"federal"` |
| `name_raw` / `name_normalized` | Original name / uppercased, punctuation- and corp-suffix-stripped via `normalize_name()` |
| `address_line_1`, `city`, `state`, `zip5` | Postal address fields (`zip5` always a zero-padded 5-digit string or `""`) |
| `phone_raw` / `phone_normalized` | Original phone string / digits-only, blanked out if under 10 digits |
| `latitude`, `longitude` | WGS84 floats, or `None` when the source has no coordinates |
| `segment` | `"religious"` \| `"municipal"` \| `"federal"` \| `"commercial"` \| `None` (resolved later) |
| `ein` | 9-digit IRS Employer Identification Number, **only ever populated by the BMF source** |
| `county_fips` | Currently unused by any deathcare source — always `None` |
| `size_metric`, `size_value`, `size_unit` | Only populated by FGDL (`ACRES`) |
| `source_file` | URL or path the raw data was fetched from |

`validate_canonical(df)` raises if a DataFrame is missing any of these columns
— it's the fast gate check any connector can run on its own output.

### Why `source_id`/`natural_key` are split this way (the "D3" convention)

Every connector comment references "D3": prior to that design decision,
`source_id` itself carried a composite key (`f"{source}:{key}"`). Now
`source_id` is a fixed string per connector and `natural_key` carries the
row-specific identifier. This matters for two things downstream:

- `staging.<source_id>` primary key is `(source_id, natural_key)` — see
  `lib/db.py::upsert_staging`.
- `deathcare_merge.py` rebuilds the old `"source_id:natural_key"` string
  format on demand (`_source_composite()`) wherever provenance needs to look
  like the pre-D3 composite — most importantly in `merged_sources` (§4.3) and
  the BMF-prefix check in `resolve_segment` (§4.4).

## 3. The five source connectors

Each connector follows the same internal shape: `fetch/load_raw` →
`assert_source_shape` (fail fast on schema drift) → `normalize` (derive the
canonical-ready columns) → `report_quality` (stderr metrics) → `to_canonical`
(shape into `CANONICAL_COLUMNS`) → CLI writes a CSV and, with `--write-db`,
upserts into `staging.<source_id>` via `lib.db.upsert_staging`.

### 3.1 `usgs_nsd.py` — USGS National Structures Dataset, cemetery layer

- **Source**: USGS National Map ArcGIS MapServer, Layer 37 (Cemeteries), states FL/TX/NC/SC/PA.
- **Natural key**: `PERMANENT_IDENTIFIER` (stable UUID, >99% populated).
- **account_type**: always `"cemetery"`.
- **segment**: always `None` — USGS carries no ownership/type signal, so segment resolution is deferred entirely to the merge stage.
- **Coordinates**: parsed from GeoJSON point geometry (`outSR=4326`), not from a dedicated lat/lon column.
- **No** phone, EIN, or county FIPS data.
- **Guard rails**: `assert_fill_rate` on `PERMANENT_IDENTIFIER` ≥ 99%; `assert_min_rows` ≥ 25,000 (known total ≈32,748); rejects any `STATE` value outside the 5 target states.
- **Important data-quality note**: `NAME` is frequently null. Unnamed burial sites are real, valid records — the connector explicitly does **not** drop them; that decision is deferred to `filter_leads` in the merge stage (§4.5).

### 3.2 `irs_bmf_deathcare.py` — IRS Exempt Organizations Business Master File

- **Source**: two IRS SOI CSVs — `eo2.csv` (NC, SC, PA) and `eo3.csv` (FL, TX). Latin-1 encoded.
- **Natural key**: `EIN` (zero-padded 9-digit string — kept as a string throughout to preserve leading zeros).
- **Filter logic** (`filter_cemetery`): keeps a row if `SUBSECTION == "13"` **OR** `NTEE_CD` starts with `"Y50"`. This is explicitly an OR, not an AND — about 49% of subsection-13 rows have a blank `NTEE_CD`, so an AND would silently drop nearly half the target population. After the subsection/NTEE filter, rows are further restricted to the 5 target states.
- **segment**: every row that survives the filter is unconditionally set to `"religious"` — by definition, everything left after the subsection-13/Y50 filter is a nonprofit/religious cemetery.
- **No** coordinates, no phone data.
- **Guard rails**: requires all 5 target states to be present in the *raw* (pre-filter) data — if one is missing, it means one of the two source files failed to load, not that the state legitimately has zero exempt cemeteries.
- This is the **only** deathcare source that ever populates `ein`. That fact drives both `dedup_by_ein` (§4.2) and the BMF-prefix check in `resolve_segment` (§4.4).

### 3.3 `va_cemeteries.py` — VA National Cemetery Administration sites

- **Source**: Socrata CSV export (`datahub.va.gov`), ~170 rows, refreshed periodically.
- **Natural key**: `cemetery_name + "|" + state` (full state name — there's no dedicated ID field in the export).
- **Packed-field parsing**: the source packs address as `"Street, City, ST ZIPCODE"` and contact as `"Phone: NNN-NNN-NNNN, FAX: ..."` in single string columns. Regexes extract ZIP (`_RE_ZIP`), 2-letter state (`_RE_STATE_ABBR`), city and street (split around the state+ZIP suffix), and the first phone number before a comma/"Or"/end-of-string.
- **State names**: arrive as full names ("Alabama") and are converted to 2-letter codes via `STATE_NAME_TO_ABBR` in `lib/geo.py`. Unrecognized names raise immediately (protects against silent encoding corruption).
- **segment / account_type**: hardcoded to `"federal"` for every row — all 170 VA sites are federally managed and none qualify as commercial leads. Setting this at the source means the merge layer's segment-based exclusion (§4.5) works without a separate filter pass.
- Has real lat/lon coordinates, unlike BMF.

### 3.4 `txdot_cemeteries.py` — Texas Department of Transportation cemeteries

- **Source**: TxDOT ArcGIS FeatureServer, ~7,991 records, Texas only (no state filter needed).
- **Natural key**: `GID` (a double in the API; cast to a clean integer string via `int_key()` to avoid `"2.0"`-style suffixes).
- **account_type / segment**: unlike USGS NSD, `to_canonical()` here does not pass `account_type` at all, so it stays canonical `None` rather than `"cemetery"`. `segment` is also explicitly `None`.
- **No** address, phone, ZIP, EIN, or county FIPS data — geometry is the *only* location signal.
- `CITY_NM` is frequently null — the connector does not attempt to backfill it.
- `CNTY_NBR` is an integer county **code** (1–254), explicitly **not** a FIPS code — it's fetched but not mapped into `county_fips` to avoid conflating the two.

### 3.5 `fgdl_cemeteries.py` — Florida GeoPlan Cemetery Facilities

- **Source**: University of Florida GeoPlan Center ArcGIS FeatureServer (`gc_cemetery_dec24`), 3,880 Florida records.
- **Natural key**: `GCID` (integer, cast via `int_key()`).
- **Coordinates**: uses the pre-computed `LAT_DD`/`LONG_DD` WGS84 fields rather than the feature geometry, which is stored in a Florida-specific projection (WKID 3087) that the connector explicitly avoids requesting.
- **segment inference** (`_infer_segment`): the only source connector that infers segment from source data rather than leaving it `None` or hardcoding one value:
  - `"RELIGIOUS"` appears in `TYPE` → `segment = "religious"`
  - `"MUNICIPAL"` in `TYPE`, or `OPERATING == "PUBLIC"` → `segment = "municipal"`
  - otherwise → `None` (left for the merge stage to resolve)
- **size_value / size_metric / size_unit**: the only source that populates these — `ACRES` flows straight through, with `size_metric`/`size_unit` both set to the literal string `"acres"` when acreage is present.
- `ZIPCODE` arrives from the API as an integer and must be zero-padded back to 5 digits (`_zip_from_int`).

## 4. `deathcare_merge.py` — the merge/dedup pipeline

This is the heart of the request: how records from 5 independent, structurally
different sources get folded into one deduplicated set. `run_pipeline()` reads
all 5 staging tables and calls `merge_pipeline()`, which runs exactly these
five stages in order. No stage is optional and none can be reordered — each
depends on state produced by the one before it (e.g. `resolve_segment` reads
`merged_sources`, which only exists after `spatial_dedup`).

### 4.1 Stage 1 — `load_sources`: concatenate

Simple `pd.concat` of all supplied canonical DataFrames after coercing
`latitude`, `longitude`, and `size_value` to numeric dtype on each frame
individually. This per-frame coercion (rather than coercing after concat) is
deliberate: pandas emits a `FutureWarning` about dtype inference when an
all-NA column (e.g. BMF's `latitude`, which is `None` for every single row)
gets its type decided only after concatenation with other frames that do have
real floats.

Output: one big DataFrame, fresh integer index, no dedup yet.

### 4.2 Stage 2 — `dedup_by_ein`: BMF-only exact-key dedup

EINs exist **only** in the IRS BMF source (§3.2) — every other connector
leaves `ein` as `None`. Because EIN is a real-world unique identifier for a
nonprofit organization, two BMF rows sharing the same EIN are true duplicates
(the same organization ingested twice from the raw IRS file, e.g. appearing in
both `eo2.csv` and `eo3.csv`, or a stale re-run).

Mechanics:
1. Split the frame into `has_ein` (EIN present and non-empty) and everything else.
2. `drop_duplicates(subset=["ein"], keep="first")` on the `has_ein` half — first occurrence wins, which preserves row order from the source file (the IRS CSVs are alphabetical by name).
3. Recombine with the no-EIN rows, which pass through completely untouched.

This is a pure exact-match dedup — no fuzzy matching, no spatial reasoning.
It only ever removes rows *within* the BMF source; it cannot merge a BMF row
with a USGS/TxDOT/FGDL/VA row (that's Stage 3's job).

### 4.3 Stage 3 — `spatial_dedup`: geographic clustering + name-similarity confidence

This is where cross-source deduplication actually happens — records from
different sources that describe the same physical cemetery get folded into
one row.

**Split by coordinate availability.** Only rows with non-null `latitude` AND
`longitude` participate. Rows without coordinates (all of BMF, any partial
NSD/TxDOT/FGDL rows missing lat/lon) are passed straight through, stamped with
`merge_confidence = "none"` and `merged_sources` set to their own
`source_id:natural_key` composite (§2).

**Clustering algorithm** (delegated to `lib.geo.cluster_within_radius`,
radius default 150 m / 0.15 km):

1. **Grid blocking**: every spatial point is bucketed into a 0.1°×0.1° grid cell (~11 km per side at mid-latitudes). This turns what would be an O(n²) all-pairs distance comparison over ~43k spatial records into a tractable problem — the 0.1° cell is roughly 73× the 150 m merge radius, so any two points within range are guaranteed to share a cell or be in adjacent cells.
2. **3×3 neighborhood check**: to avoid missing pairs that straddle a cell boundary, each point is compared against every point in its own cell plus the 8 surrounding cells.
3. **Exact haversine distance** (`haversine_km`) is computed for every candidate pair found via blocking; pairs at or under the radius are unioned together via a path-compressed Union-Find structure.
4. **Connected components** of the Union-Find become the final clusters. Every input point ends up in exactly one cluster — singletons (no other point within 150 m) are their own one-member cluster.

**Merging a cluster into one record** (for clusters with 2+ members):
- The **first member becomes the base "spine"** row (`spatial.iloc[members[0]]`).
- Every other member is folded into the spine via `_coalesce_records`: for each canonical column, the base's existing non-null/non-empty value is kept; if the base is null/empty for that column, the other record's value fills it in. Identity/provenance columns (`source_id`, `merge_confidence`, `merged_sources`) are explicitly excluded from coalescing — the caller sets those separately.
  - Practical effect: if a USGS record (has lat/lon, no phone) and a VA-style record with a phone number land in the same cluster, the merged row keeps USGS's identity but picks up the phone.
  - EIN in particular: `_coalesce_records` is written so that an EIN from a BMF cluster member would flow into the merged spine. In practice this path is currently unreachable — BMF rows always have `latitude = None`, so they never enter `spatial_dedup`'s coordinate-based clustering at all and always flow through the `no_coords` branch untouched. See §7 for why this dormant code is intentional, not dead.
- **`merge_confidence`** for a multi-member cluster is decided by the *maximum pairwise name similarity* across every pair in the cluster (`lib.match.name_similarity`, a Levenshtein-ratio in `[0, 1]`, computed on `name_normalized`):
  - `>= 0.80` → `"high"` (the names are close enough that this looks like the same named place, not just spatial coincidence)
  - `< 0.80` → `"spatial_only"` (co-located, but names don't obviously match — could be two unrelated small burial sites 100 m apart, or one source's name is null)
  - A cluster where every member has an empty/null name always scores `0.0` (see `name_similarity`'s explicit early return — two unnamed sites have no name evidence, so they never qualify for `"high"`).
- **`merged_sources`** becomes a comma-joined list of every member's `source_id:natural_key` composite, preserving full provenance of which raw records were folded together (e.g. `"usgs_nsd:{uuid},fgdl_cemeteries:1423"`).

Singleton clusters (1 member, no nearby match) get `merge_confidence = "none"` too, same as the no-coordinate rows — `"none"` means "nothing to merge, seen only once," not an error state.

### 4.4 Stage 4 — `resolve_segment`: filling in the `None`s

Recall which sources leave `segment = None`: USGS NSD (always), TxDOT (always),
and FGDL when `_infer_segment` can't classify from `TYPE`/`OPERATING`. This
stage is a strict fallback chain applied **only** to rows where `segment` is
still null/empty after Stage 3 — an already-set segment (`religious`,
`federal`, `municipal`, `commercial`) is never overwritten:

1. Default fallback: `segment = "municipal"` — the assumption is that an
   unmatched, unclassified NSD/TxDOT record with no religious/federal signal
   is most likely a public/municipal cemetery.
2. If `account_type == "federal"` → override to `"federal"`.
3. If `merged_sources` contains the BMF prefix (`"irs_bmf:"`, which matches as
   a substring of `"irs_bmf_deathcare:..."` — see §2) → override to
   `"religious"`, since that record was spatially merged with a confirmed
   IRS-recognized religious/nonprofit cemetery.

Order matters here: rule 3 (BMF-derived `"religious"`) is applied **last** in
the code, so it takes priority over the federal override if a row somehow
matched both conditions (in practice this can't happen given current source
data, since federal-flagged rows come only from VA, which always sets its own
segment directly and never reaches this fallback logic at all).

### 4.5 Stage 5 — `filter_leads`: the `is_lead` flag

No rows are ever dropped by the merge pipeline — every source record (or
merged group of records) survives all five stages. Instead, `filter_leads`
adds a boolean `is_lead` column that downstream consumers use to separate
"this is a real prospecting target" from "this exists in the data but isn't
useful as a lead."

`is_lead = False` (disqualified) when **any** of:
- `segment == "municipal"` — publicly run cemeteries aren't commercial prospects.
- `segment == "federal"` — VA national cemeteries aren't commercial prospects.
- The record is truly unnamed: `name_raw` is null **and** `name_normalized`
  is empty after stripping whitespace. (A record with a raw name that merely
  normalizes to something odd still counts as named; this only catches sites
  with genuinely no name data at all, e.g. an anonymous USGS/TxDOT point.)

Everything else — `religious` and `commercial` segments with a real name —
gets `is_lead = True`.

### 4.6 Pipeline summary output

`print_summary()` writes (to stderr) total record count, a segment breakdown,
an `is_lead` true/false breakdown, a `merge_confidence` breakdown
(`high`/`spatial_only`/`none`), and the overall percentage of records with a
non-null normalized phone number. This runs automatically as part of
`run_pipeline()` and is the quickest way to sanity-check a merge run without
querying the DB.

## 5. From merged rows to `staging.resolved_*`

After `merge_pipeline()` produces the final DataFrame, `run_pipeline()` builds
three more DataFrames and upserts them — this is the step that turns
per-source, per-row canonical data into the vertical-agnostic account/location/
contact model the rest of the platform consumes.

### 5.1 Deterministic keys

All three resolved tables are keyed by SHA-256 hashes of a stable string
recipe, so re-running the pipeline against unchanged inputs produces identical
keys (idempotent upserts) rather than new rows every time:

- **`account_key`** (`_compute_account_key`): priority is `EIN` > `name_normalized + zip5`.
  - If `ein` is present: `sha256(f"ein:{ein}")`.
  - Otherwise: `sha256(f"name:{name_normalized}|zip:{zip5}")`.
  - (CCN/NPI, used as higher-priority keys in the healthcare vertical, don't exist for deathcare sources at all.)
- **`location_key`** (`_compute_location_key`): `sha256(f"loc:{account_key}|{address_line_1}|{zip5}")`.
- **`contact_key`** (`_compute_contact_key`): `sha256(f"contact:{account_key}|{role}|{full_name}")`.

`_is_present(val)` is the shared null/blank guard used throughout this step —
it treats `None`, NaN floats, empty strings, and the literal string `"nan"`
(a defensive check against values that already round-tripped through a CSV)
as "absent."

### 5.2 `staging.resolved_account`

One row per merged deathcare record. Notable field derivations:
- `legal_name` = `name_raw` if present, else `name_normalized`.
- `mailing_address` = JSON blob of whichever of `address_line_1/city/state/zip5` are present (omitted entirely, i.e. `NULL`, if none are).
- `external_keys` = JSON blob containing `{"ein": ...}` when EIN is present, else `NULL`.
- `size_metric` = `size_value` cast to float (FGDL acreage only; `None` for every other source).
- `_source_id` / `_natural_key` are carried as internal-only columns (not written to the DB — see the `DO UPDATE`/insert SQL, which excludes them) purely so `_build_resolved_location` and `_build_resolved_contact` can join back to the right `account_key` for each original row.

Upsert (`_upsert_resolved_account`) is a standard `INSERT ... ON CONFLICT (account_key) DO UPDATE` — a re-run with the same inputs updates the existing row in place rather than duplicating it.

### 5.3 `staging.resolved_location`

One row per merged record that successfully mapped to an `account_key` (built
by joining on `(source_id, natural_key)` against the account DataFrame — rows
that didn't produce an account for some reason are silently skipped). Notable:
- `site_address` = JSON blob of `address_line_1/city/zip5` plus `state` (built separately from the account's `mailing_address` blob, but sourced from the same columns).
- The `geom` column is computed **in SQL** at upsert time (`ST_SetSRID(ST_MakePoint(...), 4326)`) from `_latitude`/`_longitude`, not precomputed in Python — consistent with how `lib.db.upsert_staging` builds `geom` for the raw per-source staging tables.
- `site_type` is populated from `account_type` (`"cemetery"` or `"federal"` or `None`), not from `segment`.

### 5.4 `staging.resolved_contact`

**Only rows with a non-null `phone_normalized` produce a contact row** — this
table is currently deathcare's only contact signal, since none of the five
sources carry a named contact person (that gap is what `irs_990_enrich.py`,
§6, exists to partially fill for the BMF/religious segment). Every contact
row emitted here has:
- `role = "primary_phone"`, `role_rank = 1` — fixed, since deathcare has no way to distinguish contact roles at this stage.
- `full_name = None` — no name is available, contact key is computed with an empty-string name component.
- `is_current = True` always.

## 6. `irs_990_enrich.py` — optional phone/contact enrichment for religious cemeteries

This is a **separate pass**, not part of `merge_pipeline()` or `run_pipeline()`
— it's invoked independently against the canonical CSV output of
`irs_bmf_deathcare.py` (before or independent of the merge step). The doctring
explains the rationale directly: fetching Form 990 data per EIN is slow
(~0.5 s/call against the ProPublica API), so keeping it out of the BMF
connector's main path lets that connector stay fast, while enrichment only
ever needs to run for the `religious` segment anyway.

**Eligibility**: a row is only enriched if `segment == "religious"` **and**
`ein` is non-null/non-blank. Everything else gets `enrich_status = "skipped"`
with both enrichment fields left `None`.

**Per-EIN lookup** (`enrich_ein`): calls
`https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json`
(no auth required) and extracts:
1. `organization.phone` → `phone_990`, if present and non-blank.
2. `organization.name` → `contact_name_990`, if present.
3. Fallback only if `organization.name` is missing: `filings_with_data[0].principal_officer` from the most recent filing (older/short-form filers may not have this field at all, so `contact_name_990` can still end up `None`).
4. A 404 response → `status = "not_found"`. Any other exception (network error, non-2xx HTTP status, JSON parse failure) → `status = "error"`, with the specific exception type/message captured in `error_detail` for diagnostics.

A `time.sleep(sleep_s)` (default 0.5 s) fires in a `finally` block after
**every** call regardless of outcome — this guarantees the rate limit is
respected even on error paths, and means callers running this concurrently
across a thread pool don't need to coordinate their own throttling.

**Concurrency**: `enrich()` uses a thread pool (default 4 workers, via
`lib.enrich_runner.run_enrichment`) with a single shared `requests.Session` —
intentional, since `requests.Session` is thread-safe for concurrent `.get()`
calls (urllib3's connection pool is internally locked), and sharing avoids
spinning up a redundant connection pool per thread.

**Output**: `staging.enrich_irs990`, keyed on `(source_id, natural_key)`,
upserted via `upsert_enrich_irs990` — only non-`"skipped"` rows are written;
if a row was previously enriched and a later run marks it skipped for some
reason, the earlier cached enrichment is left untouched rather than
overwritten with nulls.

## 7. Key design decisions worth remembering

- **Two dedup strategies, two different guarantees.** Stage 2 (EIN dedup) is exact-match, deterministic, and scoped only to BMF. Stage 3 (spatial dedup) is proximity + fuzzy-name based, spans all sources, and produces a confidence label rather than a boolean "these are definitely the same place." Nothing here does full-blown probabilistic record linkage (contrast with the healthcare vertical's weighted `score_pair` scoring in `lib/match.py`, which this pipeline does not use).
- **BMF records never spatially merge with anything today.** Because BMF supplies no coordinates, every BMF row exits Stage 3 as its own `merge_confidence="none"` singleton. The BMF-prefix check in `resolve_segment` and the EIN-coalescing logic in `_coalesce_records` are both written as if a BMF row *could* land in a spatial cluster, but under the current 5 sources that path is unreachable — it's forward-looking scaffolding, not dead code to be deleted.
- **150 m merge radius and 0.80 name-similarity threshold are both hardcoded constants** (`spatial_dedup`'s `radius_km` default, `_NAME_SIMILARITY_THRESHOLD`) — tightening or loosening cross-source dedup means changing these two numbers, not the algorithm.
- **No rows are ever dropped.** Every stage of `merge_pipeline` is additive/transformative, never filtering. Even a fully unnamed, uncoordinated municipal-defaulted record survives to the final output — it's just flagged `is_lead=False`. This makes the pipeline safe to audit (nothing silently disappears) at the cost of the merged output being larger than "just the leads."
- **All three resolved tables use deterministic hash keys**, so `run_pipeline()` is idempotent — re-running against the same staging data reproduces the same `account_key`/`location_key`/`contact_key` values and upserts in place rather than duplicating.
