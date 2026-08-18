# Location & Contact Data Ingestion — Plan of Action

**Juniper Landscaping · Cold-Lead Location Database**
Scope: 5 verticals × 5 states (FL, NC, SC, TX, PA) · Target: Cloud SQL for PostgreSQL + PostGIS
Version 1.0 · 2026-08-17

---

## 0. Read this first: four corrections to earlier guidance

Research turned up four places where my earlier advice was wrong. They change the plan materially.

**1. GNIS no longer contains cemeteries.** USGS retired all "administrative" feature classes from GNIS in 2021 — cemetery, church, hospital, park, school, and others. The replacement is the **USGS National Structures Dataset (NSD)**, which does still actively carry a `Cemetery` structure type. Use NSD, not GNIS.

**2. Florida cemetery licensing is not DBPR.** It sits with the **Department of Financial Services (CFO), Division of Funeral, Cemetery, and Consumer Services**. This matters because DBPR runs an excellent bulk-download program and DFS does not — Florida cemetery data needs a public-records request, not a download.

**3. Florida DBPR does not register HOAs.** It regulates condominiums, co-ops, timeshares, and mobile home parks under Ch. 718/719/721, not Ch. 720 homeowners associations. HB 1203 (2024) added duties for HOAs but created no registry. There is no "DBPR HOA list" to download.

**4. My blanket "no POI data" rule was too broad — this one is good news.** Esri's prohibition on permanently storing places is a restriction on *Esri's Places service*, not on POI data as a category. Foursquare separately publishes **FSQ OS Places** under **Apache 2.0**: 100M+ global POIs, 22 attributes, monthly refresh, explicitly cleared for commercial use, distributed as Parquet on S3. Same data lineage that powers Esri's paid service, released permissively by the originator. That is legally clean to store and use.

**5. Added 2026-08-17 — the Florida lodging extract is confirmed good, and it is richer than expected.** A real District 1 extract was profiled. `Number of Seats or Rental Units` is present and 100% populated, which removes the largest cost risk in the plan: **Florida's resort vertical needs no purchased data.** The file also delivers an unscoped sixth vertical (multifamily apartments) for free. It carries one serious trap — it is one row per rental unit, not per property, so summing the unit count inflates it roughly 8×. Full layout, filter rules, and yields are in §6.5.1.

The distinction to hold onto: **the license travels with the distribution channel, not the underlying facts.** Foursquare data via Esri's Places API is restricted. The same company's open release is not. Always license-check the channel you actually pull from.

One caution attached to that good news: **Overture's `base` theme is ODbL**, not CDLA-Permissive, because it is majority-OSM derived. So Overture does *not* solve the polygon problem — share-alike would attach to our lead database. Overture **Places** (permissive) is fine; Overture **base/land-use polygons** are not. Use openly-licensed government polygons instead.

---

## 1. Vertical-to-deck mapping and scope

The Market Analysis deck covers 8 end markets across 17 states. This plan covers the 5 verticals across 5 states, which map to the deck as follows:

| This plan | Deck end market | Deck national TAM |
|---|---|---|
| HOA / Residential | End Market 01 — HOA / Residential | $18.0B |
| Deathcare | End Market 02 — Deathcare | $2.1B |
| Healthcare | End Market 03 — Healthcare | $4.8B |
| Parks / Municipal | End Market 06 — Government / Municipal | $7.3B |
| Resort / Hospitality | End Market 07 — Resort / Hospitality | $5.6B |

The 5 target states are 4 of the top 6 TAM states in the deck (TX $4.2B, FL $3.1B, PA $1.8B, NC $1.3B, SC $0.6B). The architecture below is state-agnostic — adding GA, VA, MD, TN and the rest is a matter of registering new source connectors, not redesigning the pipeline.

**Deferred, by your direction:** paths-of-motion / route optimization. Nothing in this plan blocks it. The `location` table carries geometry and the `account` table carries service-area rollups, which is what a routing layer will need later.

---

## 2. Recommended build order

Sequenced by data quality per unit of effort, not by TAM. Do them in this order.

| Phase | Vertical | Why here | Est. effort |
|---|---|---|---|
| **1** | **Healthcare** | Cleanest data in the whole project. Two federal CSVs joined on one key gets you ~95% of hospitals with name, address, phone, ownership, and bed count. Build and prove the whole pipeline on the easy vertical. | 2–3 weeks |
| **2** | **HOA — Texas only** | Texas has a purpose-built statewide HOA registry (~14,800 associations). No other state has anything close. Proves the highest-value vertical fast. | 2 weeks |
| **3** | **HOA — Florida** | Sunbiz bulk corporate data via public SFTP, plus statewide parcels for common-area matching. Harder than TX but very rich. | 3–4 weeks |
| **4** | **Deathcare** | NSD spine + IRS BMF + 5 regulator lists. Many small sources, each easy. | 2–3 weeks |
| **5** | **Parks / Municipal** | Requires building the ArcGIS Hub harvester, which is reusable infrastructure. Highest engineering content. | 4–5 weeks |
| **6** | **Resort / Hospitality** | Weakest open data. Florida is strong; the other four states likely need purchased data. Do last, once you know the pipeline works. | 2 weeks + procurement |
| **7** | **HOA — NC, SC, PA** | Deliberately last. All three require either paid subscriptions or scraping. Decide with real cost data in hand. | 3–4 weeks |

Rationale for putting HOA-TX at #2 despite HOA being the hardest vertical overall: it is your largest TAM ($18B) and Texas is your largest state ($4.2B), and the Texas registry makes it unusually tractable. Getting a real HOA lead list in front of sales early buys credibility for the rest of the program.

---

## 3. Pipeline architecture

Five stages, one pattern for every source. The point of the uniformity is that adding source #40 costs the same as adding source #4.

```
  ┌──────────┐   ┌──────────┐   ┌───────────┐   ┌───────────┐   ┌──────────┐
  │ 1 EXTRACT│──▶│ 2 LAND   │──▶│ 3 STAGE   │──▶│ 4 RESOLVE │──▶│ 5 SERVE  │
  │ connector│   │ GCS raw  │   │ typed +   │   │ dedup +   │   │ canonical│
  │ per src  │   │ immutable│   │ normalized│   │ survivor  │   │ + CRM    │
  └──────────┘   └──────────┘   └───────────┘   └───────────┘   └──────────┘
   Cloud Run       GCS bucket     Cloud SQL        Cloud SQL      Cloud SQL
   Jobs            (versioned)    staging schema   canonical      + vector
                                                                  tiles
```

**Stage 1 — Extract.** One containerized connector per source, deployed as a Cloud Run Job, triggered by Cloud Scheduler on the source's own cadence (quarterly for Sunbiz, monthly for NPPES, weekly for DBPR, nightly for Hub harvest). Each connector does exactly one thing: fetch bytes, write them to GCS unmodified, emit a manifest row. No parsing, no cleaning. This keeps failures diagnosable — you can always tell whether a problem came from the source or from your code.

**Stage 2 — Land.** GCS bucket, partitioned `gs://juniper-ingest-raw/{source_id}/{ingest_date}/`, with object versioning on and a lifecycle rule to Nearline at 90 days. Raw files are **immutable and never deleted**. Two reasons this is non-negotiable: several of these sources publish no history (if Sunbiz drops a quarterly file and you overwrite it, that snapshot is gone forever), and provenance defensibility matters when the data is public-records-derived and used commercially.

Alongside each landed file, write a manifest row to `ingest.source_run`: source id, run timestamp, byte count, SHA-256, row count, connector version, and the **license string captured at fetch time**. That last field is the audit trail if anyone ever asks why you believe you were allowed to store a given record.

**Stage 3 — Stage.** Parse into per-source typed tables in a `staging` schema, one table per source, columns named as the source names them. Deliberately no unification yet. Normalization happens here as *added* columns, never overwrites:

- Addresses parsed with `usaddress` or `libpostal` into components, then standardized to USPS conventions.
- Geocoding via **Census Geocoder** (free, no storage restriction, batch endpoint accepts 10k records per submission). Fall back to Nominatim on a self-hosted instance for misses. **Do not** use Esri's geocoder unless you pay the $4/1k stored tier, and do not use Google's unless you have read their caching terms — Census avoids the whole question.
- `name_normalized`: casefold, strip punctuation, strip corporate suffixes (Inc, LLC, Corp, Ltd, Association, Assn), collapse whitespace.
- `geom`: PostGIS geometry, EPSG:4326.

**Stage 4 — Resolve.** Blocking, scoring, survivorship. Detailed in §5.

**Stage 5 — Serve.** Canonical tables your CRM reads directly, plus a materialized view feeding vector tiles for the map.

**Why Cloud SQL and not BigQuery:** your total volume across all five verticals in five states is on the order of 300k–600k rows. That is small. Cloud SQL for PostgreSQL with PostGIS handles it comfortably and gives you transactional updates, which you need because sales will be editing lead status against these records. Use BigQuery only if a specific matching job outgrows Postgres — the NPPES full file (~9M rows, multi-GB) is the one plausible candidate, and even that can be filtered to entity-type-2 facilities in the five states before it lands in Postgres.

**Required Postgres extensions:** `postgis`, `pg_trgm`, `fuzzystrmatch`, `unaccent`, `btree_gin`.

---

## 4. Canonical schema

Four core tables plus provenance. The central design decision is the **account / location split**, and it is not cosmetic — it is what makes the parks vertical work at all and what lets sales pursue portfolio deals.

```sql
-- The buying entity. Who signs the contract.
CREATE TABLE core.account (
  account_id        bigserial PRIMARY KEY,
  vertical          text NOT NULL,          -- hoa|deathcare|healthcare|parks|resort
  account_type      text,                   -- association|operator|health_system|
                                            -- municipality|county|school_district|
                                            -- ownership_group|single_site
  legal_name        text NOT NULL,
  name_normalized   text NOT NULL,
  dba_name          text,
  parent_account_id bigint REFERENCES core.account(account_id),  -- system rollup
  mailing_address   jsonb,                  -- parsed components
  phone             text,
  email             text,
  website           text,
  status            text,                   -- active|inactive|dissolved|unknown
  external_keys     jsonb,                  -- {ccn, npi, geoid, leaid, sunbiz_doc,
                                            --  trec_cert_id, ein, license_no}
  size_metric       numeric,                -- beds | units | acres | rooms
  size_metric_unit  text,
  confidence        numeric,                -- 0..1
  first_seen        timestamptz NOT NULL DEFAULT now(),
  last_seen         timestamptz NOT NULL DEFAULT now()
);

-- The serviceable physical site. What a crew drives to.
CREATE TABLE core.location (
  location_id       bigserial PRIMARY KEY,
  account_id        bigint REFERENCES core.account(account_id),
  location_name     text,
  site_address      jsonb,
  geom              geometry(Point, 4326),
  boundary          geometry(MultiPolygon, 4326),  -- when available
  geocode_precision text,      -- rooftop|parcel|street|zip|centroid
  geometry_source   text,      -- parcel|padus|hub|nsd|geocoded
  maintained_acres  numeric,
  acres_confidence  text,      -- measured|estimated|banded
  site_type         text,      -- park|ballfield|median|campus|cemetery|common_area
  first_seen        timestamptz NOT NULL DEFAULT now(),
  last_seen         timestamptz NOT NULL DEFAULT now()
);

-- People. Kept separate because sources disagree and roles change.
CREATE TABLE core.contact (
  contact_id     bigserial PRIMARY KEY,
  account_id     bigint NOT NULL REFERENCES core.account(account_id),
  full_name      text,
  role           text,        -- registered_agent|officer|director|facilities_dir|
                              -- parks_dir|procurement|gm|manager
  role_rank      int,         -- 1 = best guess at decision maker
  phone          text,
  email          text,
  address        jsonb,
  source_id      text NOT NULL,
  is_current     boolean DEFAULT true
);

-- Append-only provenance. One row per (source, source record, ingest run).
CREATE TABLE core.source_record (
  source_record_id bigserial PRIMARY KEY,
  source_id        text NOT NULL,
  source_run_id    bigint NOT NULL,
  natural_key      text NOT NULL,   -- source's own id
  payload          jsonb NOT NULL,  -- full original record
  account_id       bigint REFERENCES core.account(account_id),
  location_id      bigint REFERENCES core.location(location_id),
  match_score      numeric,
  match_method     text,            -- exact_key|deterministic|fuzzy|manual
  UNIQUE (source_id, natural_key, source_run_id)
);

CREATE INDEX ON core.account USING gin (name_normalized gin_trgm_ops);
CREATE INDEX ON core.location USING gist (geom);
CREATE INDEX ON core.location USING gist (boundary);
```

**Why `size_metric` is a single generic column rather than per-vertical columns:** sales prioritization is cross-vertical — a rep wants the biggest opportunities in their territory regardless of vertical. Beds, units, acres, and rooms are not comparable, so store the raw metric plus unit, then compute a derived `estimated_maintained_acres` in a separate scoring view where you can apply per-vertical conversion assumptions and revise them without a migration.

**Why `parent_account_id` self-references:** HCA owns dozens of hospitals; a management company runs dozens of HOAs; a REIT owns dozens of resorts; a city owns dozens of parks. Portfolio selling is the highest-value motion in every one of these verticals and it needs one hop.

---

## 5. Deduplication and entity resolution

This is where the project succeeds or fails. Expect to spend more time here than on all the downloading combined.

### 5.1 Match key hierarchy

Resolve in this order and stop at the first tier that fires. Higher tiers are deterministic and need no review.

**Tier 1 — Authoritative identifiers (auto-merge, no review).**

| Key | Vertical | Notes |
|---|---|---|
| CMS CCN | Healthcare | Joins Hospital General Info ↔ POS ↔ ownership files |
| Organizational NPI | Healthcare | Entity type 2 only |
| Census GEOID | Parks | TIGER Place / County — stable across years |
| NCES LEAID | Parks | School districts |
| Sunbiz document number | HOA (FL) | Stable corporate id |
| TREC certificate id | HOA (TX) | |
| EIN | Deathcare | From IRS BMF |
| State license number | Deathcare, Resort | Unique within issuing agency, not across states |

**Tier 2 — Deterministic composite (auto-merge).** `name_normalized` exact match AND ZIP5 match AND state match. High precision in practice; the failure mode is chain locations sharing a name in one ZIP, so exclude records whose normalized name matches a known chain/brand list.

**Tier 3 — Fuzzy scored (threshold-gated).** Compute a weighted score:

```
score = 0.35 · name_similarity        -- similarity() via pg_trgm, or Jaro-Winkler
      + 0.30 · address_similarity     -- on parsed components, house number weighted
      + 0.20 · spatial_proximity      -- 1.0 at 0m, decaying to 0 at 500m
      + 0.10 · phone_exact
      + 0.05 · size_metric_agreement  -- guards against merging a 40-bed and 400-bed
```

Blocking keys, to avoid an O(n²) comparison. Generate candidate pairs where **any** of these agree:
- `(state, zip5, left(name_normalized, 4))`
- `(state, normalized_street_number, metaphone(street_name))`
- `ST_DWithin(geom, geom, 500)` — geohash-7 prefix as a cheap pre-filter
- `(state, phone)` where phone is non-null

Thresholds:
- **≥ 0.92** — auto-merge
- **0.75 – 0.92** — write to `review.match_queue`, human adjudication
- **< 0.75** — treat as distinct

Calibrate these on a hand-labeled set of ~300 pairs per vertical before trusting them. The right thresholds differ by vertical: cemetery names are highly repetitive ("Oak Grove Cemetery" appears many times per state) and need spatial proximity weighted much higher, while HOA names are near-unique and tolerate a lower name threshold.

### 5.2 Spatial dedup for polygon sources

Parks especially will arrive three times — once from PAD-US, once from the state layer, once from the city's own Hub layer. Match on **intersection-over-union > 0.60** of boundary geometry plus name similarity > 0.5. Keep the geometry from the highest-precedence source, but retain all source_record rows.

### 5.3 Survivorship

Field-level, not record-level. For each canonical field, take the value from the highest-precedence source that has a non-null value, and record which source won in a `field_provenance` jsonb column. Precedence is per-field, not global — a state regulator has the best phone number, while a parcel layer has the best geometry.

| Field | Precedence (highest first) |
|---|---|
| Geometry / boundary | County parcel → city Hub layer → state layer → PAD-US/NSD → geocoded point |
| Phone / email | State regulator licensee list → CMS → FSQ OS Places → corporate registry |
| Legal name | Corporate registry → state regulator → federal file |
| Officers / agents | Corporate registry (only source) |
| Bed / unit count | State licensing (PA DOH, FL DBPR) → CMS POS → CMS Care Compare |
| Lot acreage | County parcel recorded area → building-footprint-subtracted estimate → size-band derived from bed count |
| Status (active/dissolved) | Corporate registry → regulator |

Two hard rules. **Never overwrite a human edit** — add a `manually_verified` boolean on both account and contact, and have survivorship skip those fields permanently. **Never hard-delete.** If a record disappears from a source, set `last_seen` and let a view mark it stale; a dissolved HOA is still a lead worth understanding.

### 5.4 The hard join, called out explicitly

For the parks vertical, the genuinely difficult work is resolving a free-text manager string to a government account: `"City of Cary Parks, Recreation & Cultural Resources"` → `GEOID 3710740`. There are thousands of these strings and they are idiosyncratic. Budget real time. Approach: strip role words (Parks, Recreation, Department, Division, Public Works), extract the place name, match against TIGER Place names within the same state and county, and route anything below threshold to review. This is not a weekend task and underestimating it is the most likely way this vertical slips.

---

## 6. Source catalog by vertical

Licensing note applied throughout: every source below is either US federal public domain, state public records, or an explicitly permissive open license. Anything requiring a paid subscription is flagged with cost. **Sources ruled out on licensing grounds** are listed at the end of each vertical so the decision is documented rather than rediscovered.

### 6.1 Healthcare — Phase 1

**Spine: two CMS files joined on CCN.**

| Source | URL | Access | Key fields | Cadence |
|---|---|---|---|---|
| CMS Hospital General Information | `data.cms.gov/provider-data/dataset/xubh-q36u` | CSV + DKAN API (`/provider-data/api/1`, limit max 500) | CCN, name, address, phone, hospital type, ownership, emergency services | Quarterly |
| CMS Provider of Services (POS/iQIES) | `data.cms.gov/provider-characteristics/hospitals-and-other-facilities/provider-of-services-file-hospital-non-hospital-facilities` | CSV | CCN, **bed counts**, ownership/control type, certification dates, provider subtype | 4×/year |
| CMS Hospital All Owners | `data.cms.gov/provider-characteristics/hospitals-and-other-facilities/hospital-all-owners` | CSV | PECOS owner org name, type, address — **system rollup** | Periodic |
| CMS Nursing Home Provider Info | `data.cms.gov/provider-data/dataset/4pq5-n9py` | CSV + API | Certified beds, avg residents/day, **Affiliated Entity ID** (pre-built chain key) | Monthly |
| VA Facilities API | `data.va.gov/dataset/VA-Facilities-API/gnx8-swzk` | API | VA medical centers — CC0 | Ongoing |
| NPPES / NPI | `cms.gov/medicare/regulations-guidance/administrative-simplification/data-dissemination` | Monthly full ZIP + weekly deltas; **use V2**, V1 retired 2026-03-03 | Phone enrichment; filter `ENTITY_TYPE_CODE=2` + facility taxonomies | Monthly |

**State supplements — these exist to catch senior living and medical office, which CMS largely misses.** That gap matters: large low-rise senior-living campuses are plausibly the highest grounds-spend-per-account segment in this vertical.

- **NC (best of the five):** DHSR listings at `info.ncdhhs.gov/dhsr/reports.htm` — CSV/XLSX, monthly. Plus NC OneMap Hospitals layer, which explicitly includes VA and military hospitals.
- **PA (richest size data):** `pa.gov/agencies/health/health-statistics/health-facilities/hospital-reports` — record-level CSV from 2020 forward with licensed beds, admissions, discharges, **occupancy rate**. PASDA GIS layer `pasda.psu.edu/uci/DataSummary.aspx?dataset=909`.
- **FL:** AHCA FloridaHealthFinder `quality.healthfinder.fl.gov/Facility-Search/FacilityLocateSearch` (has a Download button; must scope by type/county). FDOH FeatureServer `gis.floridahealth.gov/server/rest/services/DEMO/HealthCareFacilities/FeatureServer`.
- **SC:** DPH — note DHEC split effective 2024-07-01, licensing went to DPH. `dph.sc.gov/professionals/healthcare-quality/licensed-facilities-professionals` — "Find a Facility" with Export All to CSV per type.
- **TX (weakest):** HHSC web search UIs only; DSHS bed data is **PDF only**. Lean on CMS here and budget a public-information request.

**Size proxy:** bed count is your best ready-made signal. No CMS or NPPES source carries acreage. Derive it from county parcel layers via §6.1.2 below — it converts a bed count into estimated mowable acres, which no competitor prospecting these accounts will have.

#### 6.1.2 ArcGIS parcel extraction for healthcare acreage

**Goal:** for each hospital location, find the parcel(s) it sits on, pull the recorded lot area, and write it to `location.maintained_acres` with `acres_confidence = 'estimated'`.

**State parcel sources — all free ArcGIS FeatureServices:**

| State | Layer / Source | FeatureServer URL | Key area field |
|---|---|---|---|
| **FL** | Florida Statewide Cadastral (DOR, all 67 counties) | `services9.arcgis.com/Gh9awoU677aKree0/arcgis/rest/services/Florida_Statewide_Cadastral/FeatureServer/0` | `LND_SQFOOT` (÷ 43,560 → acres) |
| **NC** | NC OneMap Statewide Parcels (weekly refresh) | `services.nconemap.gov/secure/rest/services/NC1Map_Cadastral/FeatureServer/0` | `CALC_ACRES` |
| **TX** | County CAD layers via TxGIO hub (no statewide layer) | `txgio.texas.gov` — route by county FIPS; cap at top 15 metro counties (~80% of TX hospitals) | `LAND_ACRES` or `LEGAL_ACRES` (field name varies by county CAD) |
| **PA** | PASDA county-by-county (no statewide layer) | `pasda.psu.edu` county REST endpoints; start with Philadelphia, Allegheny, Montgomery, Bucks, Delaware | `SHAPE_Area` (sq ft, convert) or `LOTSIZE` |
| **SC** | County assessor bulk files (no statewide ArcGIS layer) | Pull per-county from county GIS portals; Charleston, Greenville, Richland cover most healthcare leads | `ACREAGE` or `CALC_ACREAGE` |

**Query pattern** (same FeatureServer loop used by the Hub harvester in §6.4):

```python
# Spatial lookup: find the parcel whose polygon contains the hospital point
params = {
    "geometry": f"{lon},{lat}",
    "geometryType": "esriGeometryPoint",
    "spatialRel": "esriSpatialRelIntersects",
    "inSR": "4326",
    "outFields": "PARCEL_ID,LND_SQFOOT,SHAPE_Area,OWN_NAME",
    "returnGeometry": "true",   # keep polygon → location.boundary
    "outSR": "4326",
    "f": "geojson",
}
# If point returns 0 results (geocode on road edge), fall back to 50m envelope:
# geometryType = esriGeometryEnvelope, geometry = f"{lon-0.0005},{lat-0.0005},{lon+0.0005},{lat+0.0005}"
```

**PostGIS workflow** (runs as Stage 3 enrichment after healthcare staging is complete):

```sql
-- Single-parcel case
UPDATE core.location l
SET maintained_acres = p.lnd_sqfoot / 43560.0,
    acres_confidence = 'estimated',
    boundary         = p.geom,
    geometry_source  = 'parcel'
FROM staging.parcel_fl p
WHERE ST_Contains(p.geom, l.geom)
  AND l.vertical = 'healthcare' AND l.state = 'FL';

-- Multi-parcel campus: sum area, union geometry
WITH campus AS (
  SELECT l.location_id,
         SUM(p.lnd_sqfoot) / 43560.0 AS total_acres,
         ST_Union(p.geom)             AS campus_boundary
  FROM core.location l
  JOIN staging.parcel_fl p ON ST_Intersects(p.geom, l.geom)
  WHERE l.vertical = 'healthcare' AND l.state = 'FL'
  GROUP BY l.location_id
  HAVING COUNT(*) > 1
)
UPDATE core.location l
SET maintained_acres = c.total_acres,
    boundary         = c.campus_boundary,
    acres_confidence = 'estimated',
    geometry_source  = 'parcel'
FROM campus c WHERE l.location_id = c.location_id;
```

**Connector placement:** implement as `connectors/parcel_acreage_enrich.py`, separate from the CMS connectors. It runs after `core.location` is populated for healthcare and can be re-run independently when parcel layers refresh.

**Known limitations (document in code):**
- Parcel area = total recorded lot, not net landscapable area. Building footprint subtraction is a future enhancement; set `acres_confidence = 'estimated'` until then.
- TX and PA have no statewide layer — route by `location.county_fips` to the correct county endpoint.
- SC has no ArcGIS layer — first pass is manual county assessor CSV download; automate once the pattern is proven.

**Ruled out:** HIFLD Open (shut down 2025-08-26; DataLumos archive is frozen and CMS supersedes it). The NASA-hosted HIFLD mirror has no SLA — do not depend on it.

### 6.2 HOA — Phases 2, 3, 7

No state classifies HOAs in its corporate registry, so **name pattern matching plus entity type is the only lever** everywhere except Texas.

**Texas (Phase 2) — best HOA source in the country.** Tex. Prop. Code §209.004 requires every Ch. 209 association to file a management certificate with TREC.
- Portal: `hoa.texas.gov/management-certificates-search`
- Bulk: Socrata `8auc-hzdi` at `data.texas.gov/dataset/TREC-HOA-Management-Certificates/8auc-hzdi`; CSV at `data.texas.gov/api/views/8auc-hzdi/rows.csv?accessType=DOWNLOAD`; SODA API documented at `dev.socrata.com/foundry/data.texas.gov/8auc-hzdi`. Also mirrored on data.gov.
- Free. Public records — storage and commercial use fine.
- **Verified against a real extract, 2026-08-17.** See §6.1.1 for the profile. Headline: **17,071 associations, one row each, no dedup work required** — but no street address and no contact in the CSV.
- Gap: Ch. 209 excludes condominiums. The extract does carry 2,114 `COA` rows anyway, but do not assume condo coverage is complete; supplement with TX SOS and county-clerk records.

#### 6.1.1 TREC management certificates — verified layout

Profiled `TREC_HOA_Management_Certificates_20260817.csv`, **17,071 rows, 6 columns** (`Name`, `County`, `City`, `Zip`, `Type`, `Certificate`). Row count has grown from the ~14,839 cited above, so the registry is actively filed against.

**The good news is the grain.** 17,071 rows resolve to **17,070 distinct association IDs** — exactly one duplicate in the entire file (`Stacy Estates Homeowners Association`, Allen, filed twice). After the DBPR row-per-unit mess, this file needs essentially no entity resolution against itself.

**Use the URL as the primary key.** The certificate link decomposes as `…/certificates/{association_id}/{certificate_id}/mc/{filename}.pdf`. The first segment is a stable 6-digit numeric association ID (100% numeric, 17,070 distinct); the second is a per-filing ID like `51-253` (17,071 distinct). Extract both: `association_id` is the join key and belongs in `external_keys.trec_assoc_id`; `certificate_id` tells you when a re-filing has occurred on the next refresh.

| Field | Fill | Usable as-is? |
|---|---|---|
| `Name` | 100% | Yes |
| `Type` | 100% | Yes — `POA` 14,957 / `COA` 2,114 |
| `Certificate` | 100% | Yes — all 17,071 URLs distinct |
| `City` | 99.8% | Yes, after case-folding (913 raw values → 794 normalized; `Houston` and `HOUSTON` both appear) |
| `Zip` | 99.6% | Yes, after stripping to 5 digits — 79 rows have junk (`TX`, `Travis`, `Texas`, `2008`) |
| `County` | 99.2% populated but **15.1% unusable** | **No — derive it instead** |

**Do not trust `County`.** 1,535 rows say literally `TX`, 886 say `Texas`, 138 are blank, and the remainder holds 299 distinct values where Texas has 254 counties. The junk includes `78731`, `N/A`, `BRAZOS\``, and legitimate-but-unparseable multi-county entries (`BRAZORIA/GALVESTON`, `HARRIS/MONTGOMERY`, `BEXAR & ATASCOSA`). Derive county from ZIP5 using the Census ZCTA-to-county relationship file and keep the source value only as a cross-check. Note that the multi-county values are real signal — those are large master-planned communities straddling a county line, which correlates with acreage.

**Do not filter on `Name`.** 93.6% match an association-like pattern, but the 6.4% that don't are still valid targets: `Afton Oaks Civic Club, Inc.`, `Rocky Creek Maintenance Corp.`, `Bermuda Beach Improvement Committee, Inc.`, `The Preserve of Flower Mound`. A name-based filter would discard ~1,100 real associations.

**The blocking limitation: there is no street address, and no contact of any kind.** `City` + `Zip` is the entire location signal, which geocodes to a ZIP centroid — useless for a map pin on actual turf, and useless for the routing phase later. Association mailing address, managing agent, phone, and email all live inside the linked PDF. So the CSV alone yields a *named target list with no way to contact anyone on it*. A rep can work it by searching the name, but that is not the deliverable.

**This makes the PDF pass the gate for the entire Texas HOA vertical, not an enhancement to it.** 17,071 PDFs at a polite 1 request/second is about 5 hours of wall time, trivially parallelized and cheap either way. The cost driver is entirely whether the documents carry a text layer or are scanned images: text-layer means `pdfplumber` plus per-county-form regexes and a few days of work; scanned means OCR (Tesseract or Document AI), materially worse field accuracy, and a manual review queue. **Still unresolved — see §8 item 2.** These are county-clerk-recorded instruments with no standard statewide template, so expect form variation by county even in the best case, and plan on a per-county parser registry rather than one regex set.

**Geography, for phasing.** Concentration is high: Harris 2,987 · Dallas 1,443 · Travis 1,202 · Bexar 1,118 · Collin 970 · Tarrant 868. By city: Houston 2,254 · San Antonio 1,247 · Dallas 1,225 · Austin 1,141. The four big metros are over 60% of the file, so a metro-at-a-time PDF pass delivers usable territory coverage long before the full 17k completes. Start with Harris.

**A working connector exists:** `connectors/tx_trec_hoa.py` — normalizes the CSV, extracts both IDs from the URL, flags the unusable county values, and emits both the canonical rows and a prioritized PDF fetch queue.

**Florida (Phase 3).**
- **Sunbiz bulk corporate data:** `dos.fl.gov/sunbiz/other-services/data-downloads/`. Public **SFTP** at `sftp.floridados.gov`. Fixed-width ASCII, 1,440-char records, quarterly full file split 10 ways (>1 GB) plus daily deltas. Layout: `dos.sunbiz.org/data-definitions/cor.html`. Fields: legal name, doc number, status, filing date, principal and mailing address, registered agent name + address, **up to 6 officers/directors with titles and addresses**. No phone, no email, no NAICS. Free.
- **DBPR condo/co-op extracts:** `www2.myfloridalicense.com/condos-timeshares-mobile-homes/public-records/` — e.g. `condo_CE.csv`. Carries **unit counts** on project records and fund revenue/expense on managing entities. The only unit-count field available in any of the five states.
- **DBPR licensed CAM firms:** `www2.myfloridalicense.com/community-association-managers-and-firms/public-records/`. High value — the community association manager is frequently the actual buyer, and Florida is the only one of the five states that licenses them.
- **Statewide parcels:** DOR cadastral, ~10.8M parcels, all 67 counties — `services9.arcgis.com/Gh9awoU677aKree0/arcgis/rest/services/Florida_Statewide_Cadastral/FeatureServer/0`. Query `OWN_NAME` for association-owned common area. This is how you get real service-area polygons rather than just a mailing address.

**North Carolina (Phase 7).** Start with the **free** tier: `sosnc.gov/divisions/business_registration/reports_and_listings` — downloadable listings filterable by entity type and county. Paid bulk subscription is $750–$5,200/yr (`sosnc.gov/fees/by_title/_data_subscriptions`), CSV, weekly, includes officers/directors. Parcels are strong and free: NC OneMap statewide standardized parcels, refreshed weekly.

**South Carolina (Phase 7).** Bulk corporate data requires a Tyler subscriber agreement at roughly **$12,000/yr** — hard to justify. SCDCA HOA reports are aggregate complaint statistics, not a roster. No statewide parcel layer. **Recommendation: skip the $12k.** Use county assessor bulk files for the 8–10 coastal and Upstate counties that actually matter commercially.

**Pennsylvania (Phase 7).** No bulk file and no public API. Bureau of Corporations sells custom lists at **$0.25/name**. Realistic play: a name-filtered order, or a Right-to-Know request for an extract first. No HOA registry, no statewide parcel layer (PASDA is county-by-county).

**Name-matching filter** (apply with `entity_type IN ('Domestic Non-Profit Corporation','Nonprofit Corporation')`):

```
homeowner'?s?\s+assoc | home\s?owners\s+assoc | property\s+owners\s+assoc
| condominium\s+assoc | cooperative\s+assoc | community\s+assoc
| \b(HOA|POA|COA)\b | villas?\s+at\b | \bmaster\s+assoc
| residents'?\s+assoc | townhome[s]?\s+assoc
```

Exclude names containing `management`, `realty`, `services`, `LLC` to cut management companies out of `community association` hits. Expect ~85–92% precision. **Recall is the real problem:** associations named only for the subdivision ("Sawgrass Landing, Inc.") will be missed entirely. Recover those two ways — join registered-agent addresses to known CAM firms, and match parcel owner names against the same patterns.

### 6.3 Deathcare — Phase 4

Three segments (commercial perpetual-care, religious, municipal) and **no single source covers all three**. Minimum three sources.

| Source | Covers | URL | Fields | License |
|---|---|---|---|---|
| **USGS NSD Cemeteries** — the spatial spine | All three segments | `carto.nationalmap.gov/arcgis/rest/services/structures/MapServer/37`; bulk via ScienceBase NSD collection | Name, point, state, county FIPS. **No contact info at all.** | Public domain |
| **IRS Exempt Orgs BMF** — fixes the religious gap | Religious + nonprofit | `irs.gov/charities-non-profits/exempt-organizations-business-master-file-extract-eo-bmf` | Filter NTEE **Y50** / subsection **501(c)(13)**. Name, street address, city, state, ZIP, EIN | Public domain |
| **VA National Cemetery Sites** | Federal | `datahub.va.gov/dataset/VA-National-Cemetery-Sites/fcxt-zc8r` | Name, address, phone. 157 nationally | Public domain |
| **TxDOT Texas Cemeteries** | All, TX only | `gis-txdot.opendata.arcgis.com/datasets/TXDOT::texas-cemeteries/about` | Statewide points, denser than NSD | Public agency open data |
| **FGDL / GeoPlan Cemetery Facilities** | All, FL only | `services.arcgis.com/LBbVDC0hKPAnLRpO/arcgis/rest/services/gc_cemetery_dec24/FeatureServer/0` (ArcGIS Online item `22e1fa797e6a4fedbdfab4157a116a18`; original `geodata.myflorida.com` URL is dead as of 2026-08) | Compiled from ~24 sources — denser than NSD for FL; TYPE field carries 22 cemetery types including RELIGIOUS and MUNICIPAL | Public records |

**State regulators — the only sources with phone numbers, but perpetual-care only.** The exemption is statutory, not incidental: municipal, city, church, and nonprofit cemeteries are outside these boards' jurisdiction by law. No amount of digging makes a licensee list cover churches.

- **FL:** Dept of Financial Services, Division of Funeral, Cemetery & Consumer Services — `myfloridacfo.com/division/funeralcemetery/licensing`. **Search-only, no bulk file.** Path: Ch. 119 public-records request for an electronic extract.
- **TX:** Texas Funeral Service Commission (`tfsc.texas.gov`) for funeral establishments; **Texas Dept of Banking, Special Audits** for perpetual-care cemeteries. Neither publishes a roster — request the supervised-entity list.
- **NC:** NC Cemetery Commission — `nccemetery.org/north-carolina-cemeteries/` publishes a **sortable HTML table** of perpetual-care cemeteries with name and city. Scrapeable public record.
- **SC:** LLR Perpetual Care Cemetery Board (`llr.sc.gov/cem/`) and Board of Funeral Service (`llr.sc.gov/fs/`). **LLR sells licensee rosters at roughly $10 per license type** — cheap, and paying makes provenance clean. Just buy it.
- **PA:** thinnest of the five — **PA has no cemetery board**. Funeral Directors board via PALS (`pals.pa.gov`); a current bulk licensure file could not be confirmed post-reorg. Assume scraping or a RTK request.

**Ruled out on licensing:** Find A Grave (Ancestry ToS expressly prohibits bots, crawlers, scraping; no commercial reuse) and BillionGraves (genealogical use only, no commercial purpose). Both are hard nos — same failure mode as Esri Places. Also avoid OSM-derived cemetery extracts: ODbL share-alike would attach to the CRM database.

**Coverage consequence:** municipal cemeteries are the one segment with no contact-bearing source anywhere. For landscaping they typically buy through city procurement anyway, so route them into the parks/municipal motion rather than treating them as deathcare leads.

### 6.4 Parks / Municipal — Phase 5

**Schema decision: the municipality is the account; parks are child locations.** Three independent reasons. The contract is awarded at governing-body level and typically bundles all parks plus medians plus facility grounds — there is no bid for one park. The buying roles (Parks Director, Public Works Director, procurement officer) exist once per city, not once per park. And park-level data carries no contact information, so the only join that reaches a human resolves to a government.

**Account ceiling: roughly 7,400** across the five states — ~5,000 municipalities + 534 counties + ~1,960 school districts. **Pennsylvania alone is ~2,560 municipalities** because townships are general-purpose governments there; it will dominate raw record count and will distort any per-state quota you set. Verify the municipality counts against 2022 Census of Governments Table 2 before publishing them.

| Source | Purpose | URL | Notes |
|---|---|---|---|
| **PAD-US 4.1** | Park polygons incl. municipal | `usgs.gov/programs/gap-analysis-project/science/pad-us-data-download`; ScienceBase `6759abcfd34edfeb8710a004` | **Includes municipal parks** — 4.1 absorbed TPL ParkServe (75k+ city parks). Fields: `Own_Type`, `Own_Name`, `Loc_Own`, `Mang_Type`, `Mang_Name`, `Loc_Mang`, `Unit_Nm`, `GIS_Acres`. Filter `Mang_Type='LOC'`. `Loc_Mang` carries the raw manager string you need. **No contact info.** Public domain |
| **TIGER/Line 2025** | Enumerate accounts | `www2.census.gov/geo/tiger/TIGER2025/PLACE/` and `/COUSUB/` | **Filter `CLASSFP` to keep incorporated (C1–C9) and drop CDPs (U1/U2)** or you will invent thousands of phantom accounts that have no Parks Director and cannot sign a contract |
| **NCES EDGE** | School districts + schools | `nces.ed.gov/programs/edge/geographic/schoollocations`; live service `nces.ed.gov/opengis/rest/services/K12_School_Locations/EDGE_GEOCODE_PUBLICSCH_2324/MapServer/0` | Current file `EDGE_GEOCODE_PUBLICSCH_2324`. LEAID as account key. Points are address geocodes — good for account, not acreage. Public domain |
| **PASDA DCNR Local Parks** | PA municipal parks | `pasda.psu.edu/uci/DataSummary.aspx?dataset=307` | **Best state-level municipal parks layer in any of the five states.** REST: `mapservices.pasda.psu.edu/server/rest/services/pasda/DCNR/MapServer` |
| **FL Outdoor Recreation Inventory** | FL rec sites | `geodata.dep.state.fl.us` — FDEP FORI | Public and private outdoor recreation sites, statewide |
| **NC1Map_Recreation** | NC rec | `services.nconemap.gov/secure/rest/services/NC1Map_Recreation/FeatureServer` | Plus NC OneMap statewide parcels (weekly) for real acreage |
| **TPWD / TxGIO** | TX state parks | `gis-tpwd.opendata.arcgis.com`; `data.geographic.texas.gov` | State parks and WMAs only — municipal parks need the Hub harvest |
| **SCDNR** | SC | `data-scdnr.opendata.arcgis.com` | **Weakest of the five.** Natural resources, not municipal grounds. Rely on PAD-US + Hub harvest |

**Ruled out on licensing:** do **not** download ParkServe directly from Trust for Public Land — TPL's terms are "as is" and trademark-protected, not an open license. Take the same polygons via PAD-US, where USGS has released them as public domain. This is the most important licensing catch in this vertical.

**The ArcGIS Hub harvester — reusable infrastructure worth building properly.** Thousands of cities and counties publish parks layers on ArcGIS Hub. **No API key or token is required for public layers.**

*Discovery.* Use the documented and stable ArcGIS Online item search rather than relying solely on the undocumented Hub v3 API:
```
https://www.arcgis.com/sharing/rest/search?f=json
  &q=parks AND type:"Feature Service" AND access:public
  &num=100&start=101&sortField=modified&sortOrder=desc
  &bbox=xmin,ymin,xmax,ymax
```
`num` maxes at 100 and `start` caps near 10,000 — **partition the crawl by state, bbox, or owning org** rather than paging one huge result set. The Hub v3 endpoint (`hub.arcgis.com/api/v3/datasets`, `page[size]` max ~100) is real but undocumented and unversioned; use it as a secondary discovery path only.

*License gating — do this before ingesting, not after.* Per-item metadata at `arcgis.com/sharing/rest/content/items/{itemId}?f=json` returns **`licenseInfo`** and **`accessInformation`**. Each Hub site also exposes a **DCAT-US 1.1 feed** at `https://<site-domain>/api/feed/dcat-us/1.1.json` carrying `license`, `accessLevel`, and `publisher` — ideal for a nightly bulk license sweep. **Policy: default-deny.** Allowlist tokens (`public domain`, `CC0`, `CC-BY`, `no restrictions`); denylist (`internal use only`, `not for redistribution`, `all rights reserved`). Treat a **missing** license as *unknown → quarantine*, never as open. Store the raw `licenseInfo` string verbatim for audit.

*Extraction.*
```
# read capabilities first — never assume maxRecordCount
.../FeatureServer/0?f=json
# count before paging
.../FeatureServer/0/query?where=1%3D1&returnCountOnly=true&f=json
# page
.../FeatureServer/0/query?where=1%3D1&outFields=*&outSR=4326
  &f=geojson&resultOffset=0&resultRecordCount=1000
  &orderByFields=OBJECTID&returnGeometry=true
```
`maxRecordCount` defaults to 2000 but owners override it. Check `advancedQueryCapabilities.supportsPagination`; when false, fall back to `returnIdsOnly=true` then batch by `objectIds`. Use `exceededTransferLimit` as the loop-continuation signal. **Always set `orderByFields`** — unordered offset paging silently duplicates and drops rows.

**Procurement monitoring.** This vertical buys by public bid, so solicitation feeds are arguably more valuable than location data. Only **SAM.gov** has a real documented API: `api.sam.gov/opportunities/v2/search` (docs `open.gsa.gov/api/get-opportunities-public-api/`), `api_key` param, `postedFrom`/`postedTo` mandatory, filter `ncode=561730` (Landscaping Services). State portals — FL VBS, NC eVP, SC SCBO, TX ESBD (`txsmartbuy.gov/esbd`, postings >$25k mandatory so recall is genuinely good), PA eMarketplace — have **no APIs**; scrape or subscribe to email alerts. Realistic caveat: none of the state portals covers city or school-district bids, and those are the actual buyers. Add a per-account `bid_portal_url` field to be populated over time, and don't promise statewide municipal bid coverage.

**Known gap:** medians and rights-of-way — often the largest single contract line — have no source here. Derive candidates from TIGER roads clipped to municipal boundaries, or state DOT route layers (TxDOT open data, PennDOT via PASDA, FDOT). Flag as a gap rather than assuming a parks layer covers it.

### 6.5 Resort / Hospitality — Phase 6

Confirmed the weakest vertical for open data. Florida is strong; the other four are weak to unusable.

**Florida — carries this vertical almost single-handedly. CONFIRMED against a real extract, 2026-08-17.** DBPR Division of Hotels & Restaurants: `www2.myfloridalicense.com/hotels-restaurants/lodging-public-records/`, program hub `www2.myfloridalicense.com/instant-public-records/`. CSV, weekly refresh, free, one file per district (`hrlodge1.csv` … `hrlodge7.csv`). See §6.5.1 below for the verified file layout and the ingestion rules it forces. **Florida's resort vertical needs no purchased data.**

#### 6.5.1 DBPR lodging extract — verified layout and ingestion rules

Profiled `hrlodge1.csv` (District 1 = Miami-Dade + Monroe), 24,604 rows, 35 columns, 7.3 MB.

**The size field exists and is perfect.** `Number of Seats or Rental Units` is **100% populated, zero blanks, zero nulls**. This was the highest-leverage open question in v1.0 and the answer is yes. Also 100% populated: licensee name, location street address, city, state, ZIP, county, license number, status, expiry. Location address never needs a fallback.

**The critical trap: the file is one row per rental unit, not one row per property.** 24,604 rows collapse to 13,447 licenses and 15,055 distinct street addresses. Worst offender: 658 rows share `121 NE 5TH ST, MIAMI`. Example — license `CND2300049`, The Inn at Fisher Island, is 15 identical rows differing only in `Location Address Line 2` (UNIT 101, UNIT 102, …).

Two consequences, both of which will bite a naive loader:

1. **Dedup on `License Number`, and on nothing else.** Address is the tempting key and it is wrong: a single resort can occupy many street numbers. License `CND5400003` (Ocean Pointe Suites, Key Largo) spans **102 distinct street addresses** — 500 Burton Dr, 501 Burton Dr, and so on — for one 103-unit property. Deduping by street would turn that resort into 102 separate leads pointing at the same front desk. On the qualified set the three candidate grains give 2,490 (license), 3,161 (street+city), and 3,329 (license+street); the license count is the only one that matches reality.
2. **Never `SUM(units)`.** `Number of Seats or Rental Units` is the *license total*, repeated verbatim on every child row — Fisher Island shows `15` fifteen times. Summing it inflates 15 units to 225. Take `MAX()` or `FIRST()` per license number. Within a license group, `units`, `Licensee Name`, `Business Name`, status, and expiry are all invariant (1.00 distinct values each); only the address lines vary.

**Required filters, in order:**

| Filter | Rule | Rows remaining (District 1) |
|---|---|---|
| — | raw | 24,604 |
| Active only | `Primary Status Code = '20'` | 21,567 (status `45` = expired; expiry dates as old as 2009) |
| Commercial classes only | drop `DWEL` and `BNB` | 16,323 — `DWEL` licensees are private individuals (`ABBOTT JOAN E`, `BALLAROTTO JEROME & ALICE`), not accounts |
| Minimum size | `units >= 20` | 7,919 |
| Collapse to property | distinct on `License Number` | **2,490** |

**Result: 24,604 raw rows → 2,490 qualified commercial leads** for Miami-Dade + Monroe — a 10:1 reduction. Median 65 units. By class: NAPT 1,731 · HOTL 488 · MOTL 149 · CNDO 91 · TAPT 31. Split by vertical: **multifamily 1,731, resort 759**.

The `CNDO` collapse from 11,914 rows to 91 properties is the row-per-unit effect at full strength and is the single best argument for getting the grain right before this reaches a sales rep.

**`Rank Code` is the vertical router** — this one field assigns each record to the right vertical, so parse it before anything else:

| Code | Meaning | Rows | Median units | Disposition |
|---|---|---|---|---|
| `CNDO` | Condominium (rental-licensed) | 11,914 | 22 | Resort vertical |
| `NAPT` | Nontransient apartment | 5,888 | 12 | **Multifamily — new vertical, see below** |
| `DWEL` | Vacation rental dwelling | 5,744 | 6 | **Discard** — single-family, individual owners |
| `HOTL` | Hotel | 558 | 90 | Resort vertical — best per-record value |
| `MOTL` | Motel | 236 | 27 | Resort vertical |
| `TAPT` | Transient apartment | 201 | 6 | Resort vertical, low priority |
| `BNB` | Bed & breakfast | 63 | 8 | Discard — too small |

**`NAPT` — multifamily. Descoped 2026-08-17, keep the code path.** `NAPT` (nontransient apartment) is conventional multifamily housing, not hospitality — 5,888 rows in this district collapsing to **1,731 qualified complexes, versus 759 for all resort classes combined**. Licensees are ownership LLCs and partnerships (`KENDALL HOUSE APARTMENTS PTNSH`, `GRAHAM COMPANIES (THE)`, `JADE GARDENS APTS LTD`).

**Decision: out of scope.** Juniper sells large-scale grounds contracts, and individual residential properties are below the contract floor. The one case that would clear the bar is winning a corporate owner that holds hundreds of properties, which is a named-account pursuit rather than a data-ingestion problem.

The connector still tags these rows `vertical = 'multifamily'` rather than dropping them, for two reasons: the filtering is a one-line change if the portfolio-owner angle is ever pursued, and the rows are needed anyway to establish the license-level grain. **Excluded from lead counts and from all sales-facing views.** The relevant resort figure for Florida District 1 is therefore **759, not 2,490.**

**What `CNDO` is *not*.** It does not fill the Florida condo/HOA-association gap. Only **10.1%** of `CNDO` licensee names look like an association (`ASSOCIATION|ASSN|ASSOC|CONDOMINIUM|COA|HOA|OWNERS`); the rest are management companies, fee owners, and operators (`RITZ CARLTON HOTEL CO`, `SPOTTSWOOD MANAGEMENT INC`, `2201 COLLINS FEE LLC`). The 1,202 association-named rows are worth cross-matching against Sunbiz and the DBPR condo extracts, but this file is a *rental-licensing* register, not an association registry. Correction 3 in §0 still stands.

**Two smaller gotchas:**
- **No coordinates.** No lat/long, no geometry. Geocoding is mandatory — but since location address is 100% populated, the free Census Geocoder handles it with no Esri stored-geocode spend.
- **`District` and `Region` are only 52.8% populated** — do not partition or join on them. `Location County` is 100% populated; use it. `Filler`, `Base Risk Level`, and `Secondary Risk Level` are 0% populated in this file — ignore all three.

**Scale estimate.** District 1 is Miami-Dade + Monroe, the densest lodging market in the state, so it is not representative. Seven districts at a heavily discounted average suggests roughly **10,000–16,000 qualified Florida records** — call it an order of magnitude, not a forecast, and re-estimate once districts 2–7 are profiled. Pulling the other six files is a same-day task.

**A working connector already exists:** `connectors/fl_dbpr_lodging.py`. It implements every rule above, routes `Rank Code` to the right vertical, emits the §4 canonical shape, prints the funnel at each filter step, and asserts on the source layout so a DBPR schema change fails loudly instead of silently corrupting counts. Run it against all seven districts with `python fl_dbpr_lodging.py 'hrlodge*.csv'`.

**Texas — legally closed. Do not spend time here.** **SB 1086 (85th Leg., 2017)** prohibits state agencies from publishing information identifying taxable receipts of an individual business from hotel-occupancy-tax records. The property-level Hotel Data Search was removed for exactly this reason; access now requires an authorized login to the Comptroller's SIFT portal under Tax Code §156.155. The `data.texas.gov` "Local Hotel Occupancy Tax Reporting" datasets (`qik7-ypfg`, `ifh4-9tpn`, `er34-v24h`) are **municipal-level reporting with no property names**. The revenue-signal-per-property idea is dead. Texas also has **no statewide hotel license** — regulation is by municipal ordinance. Avoid third-party resellers of this data given the statute.

**NC:** DHHS Environmental Health lodging inspections, `ehs.dph.ncdhhs.gov/faf/isf/index.htm` — name and address, **no room count**. Bulk export unconfirmed.
**SC:** no lodging licensee list found on dph.sc.gov; SCDOR does not publish accommodations-tax licensees. Treat as no-source.
**PA:** no lodging licence. Dept of Agriculture retail food inspections catch hotel *restaurants* — weak name/address signal only.

**Golf — the company's stated spend proxy.**
- USGS is a dead end: golf course is **not** an NSD structure type and never was a GNIS class.
- **FL:** FGDL `gc_golfbnd` — Golf Course Facility Boundaries, **1,124 polygons** with name and address (`geoplan.ufl.edu/agol/metadata/htm/gc_golfbnd.htm`). Caveat: **2015 vintage**, ~10 years stale, but polygons give real acreage.
- **PA:** PASDA has a Philadelphia Parks & Rec golf layer (city only).
- **NC/TX:** no statewide golf layer found.
- **National Golf Foundation** is the authoritative census: 29,000+ courses, 71,000 contacts with decision-maker names, daily verification. **Executive membership ~$7,500/yr includes database licensing**; the $395 and $650 tiers do not.

**The POI option, now that licensing is clear.** **FSQ OS Places** (Apache 2.0, 100M+ POIs, 22 attributes, monthly, Parquet on S3 — `opensource.foursquare.com/os-places/`) covers hotels, resorts, and golf courses across all five states with name, address, and category, and is explicitly cleared for commercial use with attribution. Preserve `NOTICE.txt` and attribute Foursquare. This is the cheapest way to get baseline coverage in NC/SC/PA where government data fails. It carries no room count, so pair it with a size source.

**Ruled out on licensing:** TripAdvisor, Yelp, Google Places, Booking.com (all prohibit scraping/storage). State tourism directories — VISIT FLORIDA and equivalents are partner-submitted, membership-gated marketing content, not licensable public records. **Overture `base`/land-use polygons** — ODbL share-alike. **STR (CoStar) and Kalibri Labs** — benchmarking products built on confidential contributed data; they will not license for outbound prospecting.

**SEC EDGAR for REIT property schedules:** public domain with a documented API, but Schedule III tables are unstructured, inconsistent per filer, and give city/state rather than street addresses. **Not worth a parser.** Narrow exception: run ~20 named REITs and operators manually, once, to build an ownership-group target list for portfolio selling, then match those names against the DBPR records. A day of manual work, not a pipeline.

**Honest recommendation: buy for this vertical.** It is the weakest open-data vertical and the one where per-account value most justifies paying. NGF Executive (~$7,500/yr) for golf across all five states with decision-maker contacts, plus a filtered hotel/resort list with room counts and GM contacts for **TX, NC, SC, PA only** (Florida you get free) — expect low thousands of dollars. Specify independent + resort + full-service, **100+ rooms**, exclude economy and limited-service chains, and **sample before paying**.

---

## 7. Cost summary

| Item | Cost | Phase | Verdict |
|---|---|---|---|
| All federal sources (CMS, NPPES, IRS BMF, NSD, PAD-US, TIGER, NCES, VA, SAM.gov) | **$0** | 1–5 | Take |
| TREC HOA certificates (TX) | **$0** | 2 | Take |
| Sunbiz bulk + DBPR extracts + FL parcels | **$0** | 3, 6 | Take |
| PASDA, NC OneMap, TxDOT, TPWD, FDEP, ArcGIS Hub harvest | **$0** | 4, 5 | Take |
| FSQ OS Places (Apache 2.0) | **$0** | 6 | Take |
| SC LLR cemetery/funeral rosters | **~$10 per license type** | 4 | Take — trivial cost, clean provenance |
| NC SOS bulk subscription | **$750–$5,200/yr** | 7 | Try free listings tier first |
| PA Bureau of Corporations custom list | **$0.25/name** | 7 | Name-filtered order; try RTK request first |
| NGF Executive membership | **~$7,500/yr** | 6 | Recommend — golf is the stated spend proxy |
| Hotel/resort list, TX/NC/SC/PA only | **low thousands** | 6 | Recommend — sample first |
| SC SOS bulk (Tyler subscriber agreement) | **~$12,000/yr** | 7 | **Skip.** Use county assessor files instead |
| TX SOS master unload | **~$1,350–1,750** | 7 | Only if condo gap proves material |
| GCP runtime (Cloud Run, Cloud SQL, GCS at this volume) | **low hundreds/mo** | all | — |

Realistic year-one external data spend if you take the recommendations: **roughly $8,000–10,000** (revised down 2026-08-17), nearly all of it NGF plus a hotel list for **TX, NC, and PA only** — Florida is confirmed free, and SC is small enough to defer. Everything else costs nothing.

---

## 8. Open items to verify before building

Every URL and claim above was checked against live sources, but these specific items could not be confirmed without opening a file or a login and should be resolved before committing engineering time. Listed in priority order.

1. ~~**FL DBPR lodging extract — does the rental-unit count actually appear in the downloadable file**~~ — **RESOLVED 2026-08-17. Yes, and it is 100% populated.** Verified against a real District 1 extract; full layout, filter rules, and the row-per-unit trap are documented in §6.5.1. Florida's resort vertical needs no purchased data. Remaining sub-task: pull districts 2–7 and confirm the layout is identical across files.
2. **TREC certificate PDFs — are they machine-parseable** (text-layer PDFs) or scanned images requiring OCR? **Now the #1 open item, and promoted from "swings the estimate" to "gates the vertical."** The 2026-08-17 extract confirmed the CSV carries no street address and no contact of any kind (§6.1.1), so the PDFs are the only path to a usable Texas HOA list. Certificate URLs are in hand for all 17,071 associations. **Action: download ~20 certificates spanning Harris, Dallas, Travis, and Bexar** — county recording practice varies, so a single-county sample will mislead — **and check for a text layer.** Text layer means `pdfplumber` plus per-county regexes, a few days. Scanned means OCR, worse accuracy, and a review queue.
3. **Sunbiz SFTP** — confirm credentials and current file layout against `dos.sunbiz.org/data-definitions/cor.html` before writing the fixed-width parser.
4. **NSD cemetery record counts** per state — run `returnCountOnly=true` against the MapServer layer rather than estimating.
5. **PA BPOA bulk licensure file** — could not confirm it survived the pa.gov reorganization. Check `data.pa.gov` for a BPOA licenses dataset; if absent, plan on PALS scraping or an RTK request.
6. **FL AHCA and TX HHSC** — confirm whether a true bulk file exists or only a paged UI export.
7. **FL DBPR timeshare extract** — check whether a timeshare/vacation-plan file sits alongside the condo extracts. Small, high-value list.
8. **Municipality counts** for FL, NC, TX, PA against 2022 Census of Governments Table 2. Only SC (271) is confirmed.
9. **ArcGIS Hub v3 API** — treat as undocumented and unversioned; confirm the AGO item-search path works as primary discovery before depending on Hub v3.

---

## 9. What to do first

Concretely, week one:

1. Stand up Cloud SQL Postgres with `postgis`, `pg_trgm`, `fuzzystrmatch`, `unaccent`. Create the `staging`, `core`, `ingest`, and `review` schemas and the four core tables from §4.
2. Create the raw GCS bucket with versioning on, and the `ingest.source_run` manifest table.
3. Build **one** connector end to end — CMS Hospital General Information. It is a single CSV with a clean primary key. Getting one source through all five stages validates the architecture before you have 40 connectors to refactor.
4. Join it to CMS POS on CCN. You will have every hospital in the five states with name, address, phone, ownership, and bed count. That is a real, usable lead list from two files, and it is the artifact to show the sales team to fund the rest.
5. In parallel, resolve open items 1 and 2 from §8 — they are quick and they de-risk the two phases after this one.

The pattern established in step 3 is the pattern for all 40-odd sources. Get it right once.
