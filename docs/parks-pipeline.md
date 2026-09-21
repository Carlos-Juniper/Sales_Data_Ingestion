# Parks Pipeline — How It Works

This document explains the parks/municipal vertical end to end, in the order data
actually moves: government spine → park sources → staging → park-to-government
resolution → merge/dedup → resolved tables → core write.

Related: `Ingestion-Plan-of-Action.md` §6.4 (source catalog) and §5.2–5.4
(polygon dedup, survivorship, and the manager join), `docs/healthcare-pipeline.md`
(the reference vertical), `docs/HANDOFF-end-to-end-pipeline.md` (decisions D1–D15).

---

## 0. The one thing that makes this vertical different

Every other vertical resolves one source record to one account. Parks does not.
Plan §6.4:

> **The municipality is the account; parks are child locations.** The contract is
> awarded at governing-body level and typically bundles all parks plus medians plus
> facility grounds — there is no bid for one park. The buying roles (Parks
> Director, Public Works Director, procurement officer) exist once per city, not
> once per park. And park-level data carries no contact information, so the only
> join that reaches a human resolves to a government.

So ~72,000 harvested park polygons collapse into roughly **5,500 accounts**, each
carrying its parks as child locations. Two consequences run through everything
below: the pipeline needs a *separate* account spine that no park source provides,
and it needs a way to decide which government owns each park.

---

## 1. Sources

Nine sources in two groups. All are public ArcGIS endpoints — no API key, no
token, no pre-staged flat file anywhere in this vertical.

### 1.1 The government spine — `parks/gov_units.py`

Driven by `parks/config/gov_layers.yaml`. Census TIGERweb was chosen over the
TIGER/Line shapefile downloads because it is queryable, returns WGS84 GeoJSON, and
works with the existing `lib/arcgis.py` helpers — no shapefile reader, no GDAL.

| `source_id` | Layer | Rows | `account_type` |
|---|---|---|---|
| `tiger_places` | Incorporated Places (layer 4) | 3,470 | `municipality` |
| `tiger_cousub` | County Subdivisions, PA only (layer 1) | 1,543 | `municipality` |
| `tiger_counties` | Counties (`State_County` layer 1) | 534 | `county` |

**5,547 accounts.** Row counts verified live 2026-09-04 and asserted in
`test_gov_layers_config.py`.

Three decisions worth knowing:

- **Layer 4 is incorporated-places-only**; Census Designated Places are layer 5.
  That layer choice is what satisfies the plan's warning to "drop CDPs or you will
  invent thousands of phantom accounts that have no Parks Director and cannot sign
  a contract." There is no `CLASSFP` field on TIGERweb; `FUNCSTAT='A'` is the
  filter that matters, and it drops 6 inactive incorporated places (3 NC, 3 TX).
- **`tiger_cousub` is Pennsylvania only, on purpose.** PA is the one target state
  where townships are general-purpose governments. It does not double-count
  against `tiger_places`: in PA's MCD layer only `COUSUBCC='T1'` (township) carries
  `FUNCSTAT='A'`, because cities and boroughs are represented by their
  incorporated-place record instead. 1,543 townships + 1,012 PA places = 2,555,
  against the plan's stated ~2,560 PA municipalities.
- **`name_normalized` comes from `BASENAME`, not `NAME`.** `NAME` carries the LSAD
  suffix ("Cary town", "Wake County"); `BASENAME` is the bare name. §1.3 below
  explains why that matters.

`county_fips` is deliberately null for places — a place may straddle county lines
(Dallas spans five).

### 1.2 The park layers — `parks/park_layers.py`

One generic harvest loop over `parks/config/park_layers.yaml`; adding a state or
layer is a registry entry, not new Python.

| `source_id` | Source | Rows | `account_type` |
|---|---|---|---|
| `padus_parks` | PAD-US 4.1 (USGS), `Mang_Type='LOC'` | 64,736 | `municipal_park` |
| `pasda_dcnr_parks` | PASDA / DCNR local parks (PA) | 6,325 | `municipal_park` |
| `fdep_state_parks` | FDEP park boundaries (FL) | 179 | `state_park` |
| `nc_state_parks` | NC State Parks System | 346 | `state_park` |
| `tpwd_state_parks` | TPWD state park boundaries (TX) | 116 | `state_park` |
| `sc_state_parks` | SCPRT state parks (SC) | 55 | `state_park` |

Licensing catch, from the plan: **do not** take ParkServe polygons from Trust for
Public Land — their terms are "as is" and trademark-protected. PAD-US 4.1 absorbed
the same 75k+ city parks and USGS released them as public domain. That is why
`padus_parks` is the municipal source.

### 1.3 Two registry fields that encode real distinctions

`manager_field_role` — whether `manager_field` names a governing body (`name`) or
is a type code (`classification`). Only PAD-US's `Loc_Mang` is a name
("City of North Charleston", "Beaufort County"). TPWD's `PropType` is
`['SP','SNA','SHS','SP/SHS']` and NC's `PK_TYPE` is similar. Feeding classification
codes into the manager join would produce confident-looking garbage, so the
default is `classification` and only PAD-US opts in.

`managing_agency` / `managing_agency_slug` — set on all four state-park layers.
Every record in those layers is maintained by one state agency, so those parks are
assigned to that agency directly with no matching at all. This is not a shortcut:
a state park sits inside some city's boundary, but the city does not maintain it,
so a spatial rollup would invent a contract that does not exist.

---

## 2. Acreage, which is the field this vertical is bought on

Parks is the one vertical where acreage arrives pre-calculated, so the work is not
finding it but making it **defensible**. Two independent figures are stored per
park in `staging.park_attrs`:

- `acres_published` — the source's own field (`GIS_Acres`, `ACREAGE`, `ACRES`, `Area_Acres`)
- `acres_computed` — `ST_Area(boundary::geography) / 4046.8564224`, measured by
  PostGIS from the **unsimplified** geometry

Simplification (`ST_SimplifyPreserveTopology`, ~1 m) is applied only to the stored
boundary, after the area is measured, so the acreage figure is never degraded by
it. `park_layers.report_acreage_variance()` prints the median ratio and the
>10%-disagreement count immediately after each harvest.

**This cross-check earned its place on the first run.** Verified ratios
(computed ÷ published), 2026-09-04:

| Source | Median ratio | Verdict |
|---|---|---|
| `padus_parks` | 0.997 | sound |
| `pasda_dcnr_parks` | 1.001 | sound |
| `fdep_state_parks` | 1.003 | sound |
| `nc_state_parks` | 1.002 | sound |
| `sc_state_parks` | 1.003 | sound |
| `tpwd_state_parks` | **0.740** | **broken — fixed** |

TPWD was over-reporting acreage by ~35% on **every one** of its 116 records. Its
layer's native `spatialReference` is `wkid 102100` (Web Mercator), and ESRI computes
`Shape__Area` in the layer's own projection — so the value is Web Mercator square
metres, not ground square metres. Web Mercator inflates area by `1/cos²(latitude)`,
which is 1.361 at 31°N and accounts for the discrepancy exactly. `area_field` is
now `null` for TPWD and the measured value is authoritative. **Do not set it back**;
the registry comment explains why at length.

`acres_confidence` maps onto the set `core.location.acres_confidence` already
documents:

- `measured` — `acres_computed` exists (a real geodesic measurement)
- `estimated` — only a published figure exists, with nothing to check it against
- `banded` — neither

**`maintained_acres` currently holds TOTAL park area, not mowable turf.** A
500-acre regional park includes lakes, woods, ballfields and parking. Subtracting
water and building footprints is deferred work, and any sales-facing figure should
say so.

---

## 3. Park → government resolution — `parks/manager_resolve.py`

This is the join the plan singles out (§5.4):

> "The genuinely difficult work is resolving a free-text manager string to a
> government account: `"City of Cary Parks, Recreation & Cultural Resources"` →
> GEOID 3710740. There are thousands of these strings and they are idiosyncratic.
> [...] This is not a weekend task and underestimating it is the most likely way
> this vertical slips."

It lives in its own module with the string logic as pure functions, so it can be
tuned and tested without re-running a harvest. Output goes to
`staging.park_rollup`, one auditable row per park.

### 3.1 Resolution order

| Stage | `method` | Notes |
|---|---|---|
| 0. Declared agency | `declared_agency` | The four state-park layers; no matching performed |
| 1. Spatial + name agree | `spatial+name` | Highest confidence |
| 2. Spatial only | `spatial` | Maximum boundary overlap |
| 3. Name only, ≥ 0.75 | `name` | 0.75–0.92 also enqueued for review |
| 4. County fallback | `county_fallback` | Always resolves |

**Spatial uses maximum overlap, not point containment.** A park's stored point is a
bounding-box centre, and for crescent, coastal or multi-part parks that point can
fall outside the park's own boundary and inside the neighbouring municipality.
Ranking by shared area uses the whole footprint. Candidates are pruned by
`ST_Intersects` against the GiST indexes on both boundary columns.

**The county fallback is a real answer, not a failure.** Counties tile the state
with no gaps, so every park resolves to at least a county — and a park in an
unincorporated area genuinely *is* a county responsibility. It also guarantees
every park has a parent, which `staging.resolved_location.account_key` requires as
NOT NULL. Nothing is ever silently dropped: a park that resolves to nothing at all
is still emitted with a null method and counted loudly in the summary.

Name matches in the 0.75–0.92 band are accepted *and* written to
`review.pending_pairs` with `merge_strategy='parks_manager'` for human
adjudication. Thresholds mirror the plan's §5.1 Tier-3 values.

### 3.2 The string logic, and the four traps in it

`parse_manager_string()` returns candidate place names plus an entity-class hint.
Every step exists because of a case found in live data.

**Trap 1 — the entity hint is load-bearing.** "Beaufort County" and "City of
Beaufort" are both real South Carolina governments with the same base name and
different budgets. Matching on the base name alone picks one at random. Leading
(`CITY OF`, `TOWNSHIP OF`) and trailing (`COUNTY`, `TOWNSHIP`) entity words set a
class that constrains which TIGER layer is searched.

**Trap 2 — the entity word is not reliably the last token.** "Wake County Parks,
Recreation and Open Space" puts `COUNTY` in the middle. Checking the raw tail finds
`SPACE`, concludes there is no hint, and then loses to the place "Wake Forest" on a
fuzzy score. So role words are stripped *first*, which exposes `COUNTY` as the
tail.

**Trap 3 — stripping the entity word destroys 161 real place names.** Bay City,
Bridge City, Colorado City, Bunker Hill Village, Brookside Village — 161 places
across the five states have a `BASENAME` that itself ends in an entity word. For
"City of Bay City" the correct variant is the intact `BAY CITY`; for "Beaufort
County" it is the stripped `BEAUFORT`. Nothing in the string distinguishes them, so
**both** become candidates and the scorer keeps whichever matches a real
government.

**Trap 4 — greedy role-word stripping overshoots for 37 more.** Winter Park,
Pinellas Park, Oakland Park, Avon Park, Orange Park all end in role vocabulary. For
"City of Winter Park Parks and Recreation Department" the maximal trim is `WINTER`
and the untouched form is the whole department title, so the correct answer
`WINTER PARK` exists only at an intermediate depth.
`role_word_trim_sequence()` therefore emits **every** progressive trim.

Ties are broken deterministically — higher score, then longer matching variant,
then lower source rank. The rank is what separates Berwick borough from Berwick
township in PA, where both names match identically and only the entity word
distinguishes them. Without an explicit tie-break the winner would depend on dict
iteration order, which is a silent and unreproducible way to assign an account.

Texas commissioner precincts ("Harris County Precinct 4 Parks") are handled by
treating `PRECINCT` and bare numerals as role vocabulary.

Resolution runs per **distinct** `(state, manager_normalized)` pair, not per park —
tens of thousands of PAD-US rows share a few thousand manager strings.

---

## 4. Merge — `parks/parks_merge.py`

### 4.1 Polygon dedup (§5.2)

> "Parks especially will arrive three times — once from PAD-US, once from the state
> layer, once from the city's own Hub layer. Match on intersection-over-union
> > 0.60 of boundary geometry plus name similarity > 0.5."

Both conditions are required. Geometry alone merges a park with the larger preserve
containing it; name alone merges every "Oak Grove Park" in a state. IoU is computed
as `I / (A + B − I)` rather than via `ST_Union` — the identity is exact and avoids
constructing a union geometry for every intersecting pair among ~72k polygons.

Only **cross-source** pairs are considered: two heavily overlapping polygons from
one source are usually a genuine sub-unit relationship, not a duplicate. Connected
components are resolved with union-find, so three sources describing one park
collapse to one record. `merged_sources` records every folded-in identity.

Survivorship precedence follows §5.3 — state layer (10) > state inventory (20) >
PAD-US (30). The largest expected reconciliation is PASDA's 6,325 PA local parks
against PAD-US's 35,042 PA `LOC` records.

### 4.2 What gets written

| Table | Grain | Notes |
|---|---|---|
| `resolved_account` | one government | `account_key = sha256("geoid:<GEOID>")`, the Tier-1 key from §5.1 |
| `resolved_location` | one surviving park | both `geom` and `boundary` populated |
| `resolved_contact` | **empty** | see below |

Two details:

- **Governments with no parks are excluded.** The spine enumerates every
  municipality and county in five states, but an account with nothing to maintain
  is not a lead — it is noise in a CRM.
- **`size_metric` is the SUM of the government's parks' acreage**, in acres. That
  total is the number a rep quotes, so it is the account's headline metric rather
  than the unit's own land area.
- **`location_key` carries the park's `source_id:natural_key` as a discriminator.**
  Parks have no street address, so every park in a city would otherwise hash to the
  same account key + empty address + empty ZIP and collapse into a single location
  row, silently discarding the whole portfolio.

**`resolved_contact` is intentionally empty.** Plan §6.4: park-level data carries no
contact information at any level. Shipping zero park contacts is the honest state,
not a defect — fabricating placeholder contacts would put unusable rows in front of
sales. Contact acquisition is separate, deferred work.

---

## 5. Running it

`scripts/run_parks.sh`, dispatched by the Dockerfile on `VERTICAL=parks`. Five
stages, with a real pipeline run-id via `lib/pipeline_run.py` (not the `--run-id 0`
sentinel that `run_hoa.sh` and `run_resort.sh` still pass):

```
1/5  python -m parks.gov_units      --write-db   # spine: 5,547 accounts + boundaries
2/5  python -m parks.park_layers    --write-db   # 6 sources: ~72k parks + park_attrs
3/5  python -m parks.manager_resolve --write-db  # -> staging.park_rollup
4/5  python -m parks.parks_merge                 # -> staging.resolved_*
5/5  python -m lib.core_apply --run-id "$RUN_ID" # -> core.*
```

Stage 1 must precede stage 3: the spatial rollup joins against
`staging.gov_unit_boundary`, and an empty spine would send every park to the county
fallback.

Scheduled **quarterly** (`0 6 1 1,4,7,10 *`), not monthly. Every upstream source
revises annually at best, so a monthly schedule would spend a 24-hour job slot
re-downloading identical polygons.

### Local run

```bash
docker compose up -d postgres && python db/run_migrations.py
DISABLE_GCS=1 PARKS_OUT_DIR=outputs ./scripts/run_parks.sh
```

Dry runs need no database:

```bash
python -m parks.gov_units   --out-dir outputs/
python -m parks.park_layers --out-dir outputs/
python -m parks.manager_resolve --dry-run
python -m parks.parks_merge     --dry-run
```

---

## 6. Schema

Migration `016` declares the six park staging tables; `017` adds the spine and
three side tables:

| Table | Purpose |
|---|---|
| `staging.tiger_places` / `_cousub` / `_counties` | Government units, canonical 21-column shape |
| `staging.gov_unit_boundary` | Government polygons + `area_acres` from `AREALAND` |
| `staging.park_attrs` | Park boundary, both acreage figures, owner/manager strings |
| `staging.park_rollup` | The auditable park → government assignment |

Boundaries live in side tables rather than the canonical staging tables because
`lib/db.upsert_staging`'s 21-column contract is shared infrastructure and its
`geom` column is `geometry(Point,4326)`. Precedent: `staging.enrich_parcel` already
carries a `boundary` column this way.

`gov_unit_boundary.area_acres` comes from TIGER's authoritative `AREALAND`
attribute, **not** measured from the geometry — the stored boundary is generalized
server-side (~3 m) to keep response sizes workable, and AREALAND is both more
accurate and unaffected by that. Parks are the opposite case, and must not be
generalized before measurement.

---

## 7. Known gaps

Named so nothing silently disappears:

- **ArcGIS Hub harvester** (§6.4) with default-deny license gating. Without it,
  municipal park coverage is PAD-US + PASDA only.
- **NCES EDGE school districts** (~1,960 accounts, LEAID as Tier-1 key). Their
  absence is why the ceiling here is 5,547 rather than the plan's ~7,400.
- **SAM.gov procurement monitoring** (`ncode=561730`). The plan calls solicitation
  feeds "arguably more valuable than location data" for this vertical.
- **Medians and rights-of-way** — often the largest single contract line, with no
  source. The plan suggests deriving candidates from TIGER roads clipped to
  municipal boundaries.
- **Mowable-turf estimation.** See §2.
- **Contacts.** See §4.2.
