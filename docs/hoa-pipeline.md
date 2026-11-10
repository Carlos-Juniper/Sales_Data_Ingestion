# HOA Pipeline — Texas State and Florida Plan

This document covers the HOA vertical: what Texas runs today, and the full
source catalog and build plan for Florida, the next state. NC, SC and PA stay
deferred (Phase 7, plan §6.2).

Related: `Ingestion-Plan-of-Action.md` §6.2 (HOA source catalog), §5 (match
tiers, survivorship), §8 (open items); `docs/healthcare-pipeline.md` (the
reference vertical); `docs/gap_analysis.md` (what the old
`juniper-crm-HOA-pipeline` repo did in Florida).

Status as of 2026-11-09: **Texas built. Florida not started — no FL HOA code,
no FL HOA staging tables, no FL stages in `scripts/run_hoa.sh`.**

---

## 0. Why Florida is harder than Texas

Texas has one registry: every Ch. 209 association files a TREC management
certificate, so one CSV gives one row per association with a stable id.

Florida has **no HOA registry**. Ch. 720 homeowners' associations file with
nobody except the Division of Corporations, and that file does not mark an
entity as an HOA. What Florida has instead is several partial registries, each
with its own identifier:

| Identifier | Issued by | Covers |
|---|---|---|
| Sunbiz document number | Division of Corporations | Every incorporated association (HOA, condo, co-op, POA) — but unlabeled |
| DBPR project / association number | DBPR Division of Condominiums, Timeshares & Mobile Homes | Condos (Ch. 718) and co-ops (Ch. 719) only |
| CAM firm / CAM license number | DBPR | Licensed management companies and managers |
| Parcel id | County property appraiser, compiled by FDOR | Common-area land the association owns |

A Florida HOA account is assembled by linking these four. Single-family HOAs
(Ch. 720) only ever appear in Sunbiz and the parcel roll, so the Sunbiz ↔
parcel link carries most of the vertical. Condos and co-ops also appear in
DBPR, which adds unit counts. The management firm is often the actual buyer,
so the CAM link is what makes a record sellable.

---

## 1. Texas — current state

Built and tested. `scripts/run_hoa.sh` runs four stages:

1. `hoa.tx_trec_hoa` — TREC CSV (Socrata `8auc-hzdi`, ~17k associations) →
   `staging.tx_trec_hoa`, plus a certificate PDF queue. Key: `trec_assoc_id`.
2. `hoa.tx_trec_pdf_enrich` — OCR of the scanned certificates →
   `staging.enrich_hoa_pdf_contact` (`hoa_*` and `mgmt_*` columns,
   `needs_review`). Migrations 018/019.
3. `hoa.hoa_gmaps_enrich` — Google Places fallback for phone/website, skipped
   where OCR already found a contact → `staging.enrich_hoa_gmaps`. Migration 020.
4. `lib.core_apply` → `core.*`.

Tests: `connectors/hoa/tests/`. Remaining Texas gaps are tracked in plan §8
item 2 (OCR template coverage across counties) and are not blockers for
Florida.

---

## 2. Florida source catalog

Every source below is free and a Florida public record (Ch. 119 F.S.) unless
noted. "Verify" means nobody has opened a real file yet — each one gets a
layout profile like plan §6.2.1 before a connector is written.

### 2.1 Sources we will ingest

| # | Source | Grain | What it contributes | Key | Status |
|---|---|---|---|---|---|
| F1 | **Sunbiz corporate data files (DOS SFTP bulk)** | One row per corporation | The association as a legal entity: legal name, status, filing date, FEI, principal + mailing address, registered agent, up to 6 officers with titles and addresses | `sunbiz_doc` | Profiled 2026-11-09 (§6 step 1). Sunbiz Daily API is the fallback |
| F2 | **DBPR condominium extract** | One row per condo project (phase); several projects can share one managing entity | Condo projects with **unit counts**, managing association and its `C/O` manager | DBPR project no.; managing entity no. | Profiled 2026-11-09 (§6 step 2) |
| F3 | **DBPR cooperative extract** | One row per co-op project | Same as F2 for Ch. 719 co-ops (includes resident-owned manufactured-home communities) | DBPR project no.; managing entity no. | Profiled 2026-11-09 (§6 step 2) |
| F4 | **DBPR CAM firm licensees** | One row per licensed firm (`CAB` rows of `lic38cam.csv`) | Management companies — frequently the buyer | CAB license no. | Profiled 2026-11-09 (§6 step 2) |
| F5 | **DBPR CAM individual licensees** | One row per licensed manager (`CAM` rows of `lic38cam.csv`) | The named manager. Name and mailing address only, **no employer field**, so it cannot link a person to a firm | CAM license no. | Profiled 2026-11-09 (§6 step 2) |
| F6 | **FDOR tax roll (NAL) — DOR use code 09** | One row per parcel, per county | Fallback only. F7 carries the same roll fields (see below) | Parcel id | Fallback |
| F7 | **Florida Statewide Cadastral** (ArcGIS) | One polygon per parcel (layer repeats rows; dedupe) | Code-09 common-area parcels with geometry, acreage, owner name and owner mailing address (`C/O <management firm>` when managed) | `PARCEL_ID` | Profiled 2026-11-09 (§6 step 3); already in `connectors/healthcare/config/parcel_layers.yaml` |
| F8 | **County HOA / subdivision boundary layers** | Per county, varies | HOA boundary polygons where a county publishes them (Hillsborough and Leon in volume); otherwise subdivision plat polygons to join HOA names onto | County-specific | Surveyed 2026-11-09 (§6 step 4) |
| F9 | **Google Places** | One lookup per association | Phone and website fallback | Place id | Reuse `hoa_gmaps_enrich.py` |

Source locations:

- **F1** — the Division of Corporations' free bulk files on
  `sftp.floridados.gov` (public guest login, published by DOS). Layout:
  `dos.sunbiz.org/data-definitions/cor.html`. Profile, file list and counts
  are in §6 step 1. The file to load is
  `doc/Quarterly/Non-Profit/npcordata.zip` (43 MB): active non-profits only,
  all 202k of them, so the HOA filter runs locally with no paging.

  Fallback: the **Sunbiz Daily API** (`www.sunbizdaily.com/api/v1`), which we
  already pay for. Key `SUNBIZ_API_KEY`, header `X-API-Key`. The old repo
  (`juniperlandscaping/juniper-crm-HOA-pipeline`, `scrapers/sunbiz.py`) shows
  how it works:
  - `GET /filings/?filing_type=DOMNP&state=FL&zip=<prefix>` lists active
    Florida non-profits. The old repo saw ~23.8k as of 2026-06; the bulk file
    has 196,741 active `DOMNP`, so the old listing returned about 12% of them
    (cause unknown: the paging cap or a filter on the API side).
    Pages are capped at 100 × 100, so the old code splits by ZIP prefix
    (`32`, `330`–`339`, `34`).
  - `GET /filings/<corporation_number>/` returns one filing's detail.
  - Rate limit 1,000 req/hr; the old code pauses 7.5 s per request.

  Use the API only for single-record lookups between quarterly drops (e.g. a
  new filing's officers before the next quarterly), if at all. Its detail
  payload has not been profiled: no key was available on 2026-11-09.
- **F2, F3** — `www2.myfloridalicense.com/condos-timeshares-mobile-homes/public-records/`.
  The page lists column headers per file and a ReadMe. Terminated, rejected and
  withdrawn projects are not included. Files are under
  `www2.myfloridalicense.com/sto/file_download/extracts/`: condos in five
  regional files (`Condo_NF.csv`, `condo_CE.csv`, `Condo_CW.csv` — Hillsborough
  is here — `Condo_MD.csv`, `condo_PB.csv`), co-ops in `coopmailing.csv`.
  Layouts in §6 step 2.
- **F4, F5** — `www2.myfloridalicense.com/community-association-managers-and-firms/public-records/`.
  Both are in one file, `.../sto/file_download/extracts/lic38cam.csv`. Layout in
  §6 step 2.
- **F6** — FDOR Property Tax Oversight data portal on `floridarevenue.com`;
  one NAL file per county per roll year, comma-delimited with a header row.
  Field guide: `floridarevenue.com/property/Documents/2020NALfields.pdf`. Fields
  to confirm: `CO_NO`, `PARCEL_ID`, `DOR_UC`, `OWN_NAME`, `OWN_ADDR1`,
  `OWN_CITY`, `OWN_ZIPCD`, `PHY_ADDR1`, `PHY_ZIPCD`, `LND_SQFOOT`, `S_LEGAL`.
  Use the certified final roll, not the preliminary one. Some counties use
  4-digit sub-codes (Polk: `0900` vacant common elements, `0901` improved), so
  match on the first two digits.
- **F7** — `services9.arcgis.com/Gh9awoU677aKree0/arcgis/rest/services/Florida_Statewide_Cadastral/FeatureServer/0`
  (layer `CADASTRAL_DOR`). **Verified 2026-11-09: it carries the tax roll
  fields**, including `DOR_UC`, `PA_UC`, `PARCEL_ID`, `CO_NO`, `OWN_NAME`,
  `OWN_ADDR1`, `OWN_CITY`, `OWN_ZIPCD`, `PHY_ADDR1`, `PHY_ZIPCD`, `LND_SQFOOT`,
  `NO_RES_UNT`, `S_LEGAL`. So F6 comes from F7, not 67 NAL downloads. Query
  rules found while testing:
  - `DOR_UC` is a zero-padded string: filter `DOR_UC='009'`, not `'09%'`.
  - A filter on `DOR_UC` alone is rejected; scope each query by `CO_NO`
    (Hillsborough = 39). One query set per county, paged at 2,000 rows
    (Hillsborough has 10,753 code-009 parcels).
  - Leading-wildcard `LIKE '%x%'` is rejected; use prefix `LIKE 'x%'`.
  - The layer returns duplicate rows per parcel; dedupe on `PARCEL_ID`.
  - `OWN_NAME` is cut at 30 characters and spelled inconsistently
    (`HUNTER'S GREEN COMMUNITY ASSOC`, `HUNTERS GREEN COMMUNITY ASSOCI`,
    `HUNTER'S GREEN COMM ASSOCIATIO`). Match on a normalized prefix.
- **F1 web search** (`search.sunbiz.org`) sits behind a Cloudflare challenge.
  Do not scrape it; use the bulk file.

### 2.2 Sources considered and skipped

| Source | Why skipped |
|---|---|
| DBPR mobile home park extract | Parks are mostly owned by a landlord company, not an HOA buyer. Resident-owned communities are set up as co-ops or condos and arrive through F2/F3. |
| DBPR SIRS reporting database | Lists condos that filed a structural reserve study. Shows submissions as entered, and no bulk download is confirmed. Adds nothing F2 lacks except a filing flag. Revisit only if F2 proves stale. |
| DBPR timeshare extract | Belongs to the resort vertical (plan §8 item 7), not HOA. |
| Old HOA repo code (`scrapers/dbpr.py`, `fl_county_acreage.py`, `management_company_dedup.py`) | Writes a flat MySQL schema. Copy the matching rules (DBPR name+ZIP join, `[PENDING]` management-company merge), not the code. |

---

## 3. Account model

- **Account** = the association (`vertical='hoa'`, `account_type='association'`).
  Sub-associations (e.g. "Hunters Green Parcel 10 Neighborhood Assoc") are
  their own accounts with a parent link to the master association.
  Anchor key `sunbiz_doc`, already in `DEFAULT_ACCOUNT_KEY_PRIORITY`
  (`connectors/lib/keys.py`). DBPR project number goes in `external_keys` as a
  secondary key.
- **Locations** = common-area parcels (F6/F7) and, for condos/co-ops, the
  project site. One association can own many parcels.
- **Management firm = its own account** (`account_type='management_company'`,
  anchor key `dbpr_cab_license`). Decided 2026-11-09. Its staff are contacts on
  the firm account, not on the association.
- **Firm ↔ property link** is on the location, for HOAs and condos alike:
  new column `core.location.management_account_id` → `core.account`. This
  mirrors the CRM, where `properties.management_company_id` (and
  `hoa_properties.management_company_id`) points to `management_companies.id`,
  so the cutover maps it column to column. Texas can fill it too, from the TREC
  certificate's management company (migration 019).
- **Contacts** = officers and registered agent from F1; managers from F5;
  phone/website from F9.
- `size_metric` = units (F2/F3) where known, otherwise common-area acres
  (F7 `LND_SQFOOT`).

Current manager comes from the newest source. The tax roll's `C/O` address
can lag a manager change by a year or more (see §8: the roll still names Wise
Property Management on 8 of 23 Hunters Green parcels).

---

## 4. Linking the sources

| Link | Method | Tier (plan §5.1) |
|---|---|---|
| F1 → association filter | Filing type `DOMNP` + the revised name regex in §6 step 1 (the plan §6.2 regex misses `HOMEOWNERS' ASSOCIATION`) | — (filter; Hillsborough sample ~98% precision, ~88% recall proxy) |
| F2/F3 managing entity → F1 | No Sunbiz doc number in DBPR (§7 Q3). Normalized name + ZIP5 (managing-entity or project ZIP vs F1 principal or mailing ZIP); name-only when unique; token overlap to the review queue. Hillsborough baseline: 68% unique name+ZIP, 81% with unique name-only (§6 step 2) | 2 (3 for token overlap) |
| F6 parcel owner → F1 | Normalized `OWN_NAME` vs association name, same county; boost when `OWN_ADDR1` matches F1 mailing/agent address | 2 or 3 |
| F6 → F7 | `PARCEL_ID` ↔ `PARCELNO` (normalize county formatting) | 1 |
| F1 registered agent → F4 firm | Agent name/address vs CAM firm name/address | 3 |
| F7 owner `C/O <firm>` → F4 firm | Firm name after `C/O`, normalized, vs CAB license name; boost on mailing address match | 2 |
| F5 manager → F4 firm | Not possible from DBPR: the individual record has no employer. Comes from CRM/Aspire contacts or the firm's site | — |

**Recall.** The name regex misses associations named only for the subdivision
("Sawgrass Landing, Inc."). Recover them in two ways:

1. Every F6 code-09 parcel owner that matches an active F1 non-profit is an
   association, whatever its name.
2. Every F1 non-profit whose registered agent is a known CAM firm (F4) is very
   likely an association.

Both are tested against a labeled sample before trusting them.

---

## 5. Proposed staging tables

Migrations continue at `021_`. Names are proposals.

| Migration | Change | For |
|---|---|---|
| 021 | `ALTER TABLE core.location ADD management_account_id bigint REFERENCES core.account` + index | §3 firm ↔ property link (TX and FL) |

| Migration | Table | Source |
|---|---|---|
| 022 | `staging.fl_sunbiz_assoc` (filtered to association candidates; keep raw officer block as jsonb) | F1 |
| 023 | `staging.fl_dbpr_condo_coop` (one table, `program` column `condo`/`coop`) | F2, F3 |
| 024 | `staging.fl_dbpr_cam` (firms and individuals, `license_kind` column) | F4, F5 |
| 025 | `staging.fl_common_area_parcel` | F6 (+ F7 geometry) |

F8 and F9 reuse existing patterns (`enrich_*` tables) and get their own
migration only when built.

---

## 6. Next steps

Ordered. Each step has a done-when.

**Phase 0 — verify sources (no connector code yet).**

1. ~~**Profile F1.**~~ Bulk files done 2026-11-09. **Decision: the SFTP bulk
   file is primary; the Sunbiz Daily API is an optional fallback.** The API
   comparison was not run (no `SUNBIZ_API_KEY` available), and it is not
   needed to decide: the bulk file already has every field the API was meant
   to supply (below), costs nothing, has no rate limit, and can be filtered
   locally. The API listing the old repo used returned ~23.8k non-profits
   against 196,741 in the bulk file. Profile:

   **Files** (`sftp.floridados.gov`, guest login):

   | Path | Content | Size / rows | Freshness |
   |---|---|---|---|
   | `doc/Quarterly/Non-Profit/npcordata.zip` | **Use this.** Active non-profits only. Zip of 10 files `npcordata0.txt`–`9.txt` | 43 MB zip, 291 MB text, 202,308 rows: `DOMNP` 196,741, `FORNP` 5,264, `NPREG` 303. All status `A` | Quarterly. Current drop built 2026-10-06 |
   | `doc/Quarterly/Cor/cordata.zip` | All entity types, all statuses | 1.79 GB zip | Quarterly. Not needed for HOAs |
   | `doc/cor/YYYYMMDDc.txt` | Daily file, all entity types | ~2,300 rows/day, ~60 `DOMNP`/day | Daily, Mon–Sat, history back to 2011 |
   | `doc/cor/Events/YYYYMMDDce.txt` | Daily events, 662-char records (layout in `COREVENTREADME.txt`) | ~1,900 rows/day | Daily |

   - **Record format.** Fixed width, 1,440 characters, latin-1 (476 rows
     have non-ASCII bytes, so decode latin-1 and do not `grep` the raw file).
     Quarterly lines end in CRLF. Daily lines end in LF, and the last 4
     filler bytes are NUL.
   - **Fields** (all present in every row of the Hillsborough candidate set
     unless noted): document number, name (192), status (`A`/`I`), filing
     type, principal address, mailing address, file date (`MMDDYYYY`), FEI
     (96.8%), last three annual-report years and dates, registered agent
     name, type (`P` person / `C` company) and address, and up to 6 officers,
     each with title, type, name, address and ZIP+4. A "more than six
     officers" flag is set on 4.2%. Person names are split into fixed
     last/first/middle sub-fields (e.g. `SMITH               JOHN          A`).
     There is **no county field**, so county comes from ZIP or from the F7
     parcel link.
   - **What the daily files are.** 98% of daily `DOMNP` rows are new 2026
     filings. Officer and agent changes on existing associations arrive only
     with the next quarterly. The events file carries reinstatements,
     dissolutions, amendments, name changes and agent changes (with
     addresses, no officers). So: load the quarterly, add dailies for new
     associations, and use events to flip status between quarterlies.
   - **Fill rates**, 2,378 Hillsborough candidates (old regex): at least one
     officer 99.8% (3 officers is the most common count and the median), officer
     address 100%, agent 100% (company 58%, person 42%), latest annual report
     2026 96.8% (the rest are new filings). Principal address is the same as
     the mailing address on 65.6%; 11.2% have `C/O` in one of them.
   - **Registered agents point at firms.** The top Hillsborough agents are
     management firms and HOA law firms: Home Encounter HECM (204 under 3
     spellings), Appleton Reiss (87), Westcoast Management & Realty (80),
     Vanguard Management Group (43), Rizzetta & Company (39), Bay Ridge
     Property Management (27), Bush Ross (26). This supports the §4 agent → F4
     link, after normalizing the spellings.

   **Counts.** Hillsborough = principal or mailing ZIP in the county's ZIP
   list (`335xx` Hillsborough ZIPs plus `33601`–`33694`). Border ZIPs such as
   Lutz and Odessa overlap Pasco, so treat county counts as approximate.

   | Set | Statewide | Hillsborough |
   |---|---|---|
   | Active `DOMNP` | 196,741 | 12,857 |
   | Plan §6.2 regex | 36,677 | 2,378 |
   | Revised regex (below) | 51,803 | 3,272 |
   | ZIP 33647 only, plan regex | — | 35 |

   **Revised name regex.** The plan §6.2 regex misses `HOMEOWNERS'
   ASSOCIATION` (apostrophe after the S), `OWNERS' ASSOCIATION`, `CONDOMINIUM
   NO. 5 ASSOCIATION` and `TOWNHOUSE ASSOCIATION`. Use:

   ```
   \bowner'?s?'?\s+assoc | \bowners?'?\s+(of\s+)?assoc | \bhome\s?owner
   | \bcondo(minium)?s?\b | \bco-?op(erative)?\b.*\bassoc | \bcommunity\s+assoc
   | \bneighborhood\s+assoc | \bmaster\s+assoc | \b(HOA|POA|COA)\b
   | \bvillas?\b.*\bassoc | \btownho(me|use)s?\b | residents'?\s+assoc
   | \bmaintenance\s+assoc | \bproperty\s+owners\b
   ```

   Exclude `management`, `realty`, `LLC`, `BAR`, `alumni`, `chamber`,
   `merchants`, `league`, `foundation`, `political`, `teachers`, `nurses`,
   `physicians`, `contractors`, `reform`, `apartment association`, and
   `association of florida` / `florida association` / `national
   association`. Do **not** exclude `club`, `church`, `medical` or `union`:
   they knock out real condos (`CHURCHILL TOWNHOMES`, `MEMORIAL MEDICAL OFFICE
   CONDOMINIUM`, `UNION PARK MASTER`). Do not exclude `services` either
   (`HARBOUR ISLAND COMMUNITY SERVICES ASSOCIATION`).

   - **Precision.** By eye, 59 of 60 random Hillsborough hits were
     associations (the miss, a temple named `... LIEN HOA`, is now excluded).
     A second sample of 80 had none wrong; of 30 hits only the revised regex
     finds, 28 were associations (doubtful: a dock owners' group and
     `BAYSHORE CONDOMINIUM & COMPANY`). Commercial owners' associations
     (`PROFESSIONAL PARK OWNERS ASSOCIATION`) count as hits: they own
     landscaped common area.
   - **Recall proxy.** 1,340 Hillsborough `DOMNP` have a known management
     firm or HOA law firm as agent. The revised regex catches 1,177 (88%),
     the plan regex 860 (64%). The misses are mostly
     `<subdivision> ASSOCIATION, INC.` and bare `<name>, INC.`
     (`KIRBY CREEK, INC.`, `ENCORE AT FISHHAWK RANCH ASSOCIATION, INC.`). The
     two §4 recall rules (agent is a CAM firm, owner of a code-009 parcel)
     are what recover them.
   - **Pilot.** Hunters Green is in the file: `N22777` HUNTER'S GREEN
     COMMUNITY ASSOCIATION, INC., active, filed 1987-10-01, FEI present,
     principal = mailing = 9456 Highland Oak Drive (matches Aspire and F7),
     agent Bush Ross Registered Agent Services, 6 officers (more-than-six flag
     set), 2026 annual report filed. Sub-associations also present:
     Waterforde (`N95000003989`), Wynstone (`N29548`), Parcel 3 (`N30011`),
     Fox Chase (`N28381`), Highlands condo (`N03000004881`). The manager
     (RealManage) is not in the Sunbiz record, so it still comes from F7/F4.
2. ~~**Profile F2–F5.**~~ Done 2026-11-09. All three DBPR files are clean: one
   row per key, no per-unit repetition (the lodging trap does not apply).

   **F2/F3 files** (`.../sto/file_download/extracts/`):

   | File | Rows | Units | Managing entities | Last-Modified |
   |---|---|---|---|---|
   | `Condo_NF.csv` | 2,141 | | | 2026-11-07 |
   | `condo_CE.csv` | 3,312 | | | 2026-11-07 |
   | `Condo_CW.csv` (Hillsborough) | 9,355 | | | 2026-11-07 |
   | `Condo_MD.csv` | 5,820 | | | 2026-11-07 |
   | `condo_PB.csv` | 7,352 | | | 2026-11-07 |
   | **Condo total** | **27,980** | **1,557,278** | **23,816** | weekly, Saturdays |
   | `coopmailing.csv` | 759 | 78,063 | 755 | 2026-11-07 |
   | Hillsborough condo (county 39) | 648 | 42,244 | 593 | |
   | Hillsborough co-op | 6 | 1,746 | 6 | |

   **F2/F3 layout** (header row, 16 columns; co-op names column 3 `COOP Name`
   and column 7 `Incorporated Date`):

   | # | Column | Notes |
   |---|---|---|
   | 1 | Project Number | Unique per row (0 duplicates). The key |
   | 2 | File Number | |
   | 3 | Condo Name | Project/phase name, not always the association name |
   | 4 | County | DBPR county code = FDOR code (Hillsborough 39) |
   | 5 | Street City State Zip | **One combined column**; parse ZIP from the end |
   | 6 | Units | Units in this project/phase |
   | 7 | Recorded Date | |
   | 8–9 | Primary / Secondary Status | Approved or Acknowledged; Recorded or Delinquent |
   | 10 | Managing Entity Number | `MA…`; the association. Same name and address on every row it appears |
   | 11 | Managing Entity Name | The association's legal name |
   | 12 | Managing Entity Route | `C/O <firm>` on 50% of rows statewide, 63% in Hillsborough |
   | 13–16 | Managing Entity Street, City, State, Zip | ZIP+4 on ~10% |

   - **Grain.** One row per condo project (phase). One managing entity can run
     many projects (largest: 131), so the association is the managing entity,
     and its unit total is the sum over its projects.
   - **Hillsborough counts disagree** with DBPR's own `countysummary.csv`
     (573 projects, 39,167 units for county 39). The extract is the source of
     truth; note the gap, do not reconcile it.
   - **`C/O` firm.** Top Hillsborough `C/O` firms are management companies
     (Sterling, Wise, Home Encounter, Vanguard, Westcoast, FirstService,
     Associa Gulf Coast, Bay Ridge). Spellings vary (`REALMANAGE` / `REAL
     MANAGE`), so normalize before linking to F4.
   - **Skip:** `keyfinancialindicators.csv` (keyed by name only, 401 entities,
     sparse). `countysummary.csv` is useful only as a sanity check.
   - **No Sunbiz document number** in F2 or F3 (§7 Q3).

   **F2/F3 → F1 match, Hillsborough baseline** (599 condo + co-op managing
   entities vs active `DOMNP`; normalization: `CONDOMINIUM`→`CONDO`,
   `ASSOCIATION`/`ASSOC`/`ASSN`→`ASSN`, `COOPERATIVE`→`COOP`, drop `INC`,
   `CORP`, `THE`, `OF`, `A`, `AT`, `AND`, `NO`, punctuation):

   | Rule | Matched | Share |
   |---|---|---|
   | Exact name + ZIP5 (entity or project ZIP vs F1 principal or mailing), unique | 408 | 68% |
   | Exact name only, unique (ZIP differs, usually a `C/O` firm address) | 75 | 12.5% |
   | Exact name, several candidates | 1 | 0.2% |
   | No exact name | 115 | 19% |

   Of the 115 misses, token overlap (Jaccard, same ZIP) gives 42 at ≥ 0.6,
   mostly right. Traps: address-named condos (`2526` vs `2518 W KANSAS AVE`)
   and an HOA and condo sharing a name. These go to review, not auto-match.
   The other 73 score < 0.6 and are likely inactive in Sunbiz (the bulk file
   is active-only). Phase 2 improves on this baseline.

   **F4/F5 file** `lic38cam.csv` (5.3 MB, 28,907 rows, Last-Modified
   2026-11-04, **no header**). Holds active, inactive and voluntarily
   inactive licensees; null-and-void, delinquent and involuntarily inactive
   are excluded. 22 columns:

   | # | Column | Notes |
   |---|---|---|
   | 1–2 | Board, Occupation Code | |
   | 3 | Licensee Name | Firm or person |
   | 4 | DBA | Filled on 431 of 2,070 firms |
   | 5 | Class Code | **Record type**: `CAB` firm 2,070, `CAM` person 19,575, `CRSE` 6,623, `PROV` 610, `PRE` 29 |
   | 6–11 | Addr1–3, City, State, Zip | Firm office; for `CAM`, a personal address (matching only) |
   | 12 | County Code | Office county, not service area |
   | 13 | License Number | Unique within type. The key |
   | 14–15 | Primary / Secondary Status | Firms: `C` 1,991, `C/A` 76 |
   | 16–18 | Original Licensure, Effective, Expiration Date | |
   | 19 | (blank) | |
   | 20 | Renewal Period | |
   | 21 | Alternate License No. | e.g. `CAB2307` |
   | 22 | CE Exemption | |

   - **Firms (F4).** 2,070 distinct licenses; 4 names repeat across licenses.
     68 have a Hillsborough office and 72 are out of state, so county by
     office address undercounts the firms working a county (RealManage is
     `CAB3372`, Dallas). Some associations hold a firm license themselves
     (self-managed); keep them, flag them.
   - **Managers (F5).** 19,575 distinct, 607 with a Hillsborough address. No
     employer, no phone. Matching only (§7 Q5).
   - **Not these files.** `cams.csv` (122 MB) is the **continuing-education
     file** (one row per course per licensee), not license history; it is the
     duplicate trap here. `examappl38cam.csv` is the weekly exam-applicant
     list with personal phones; do not ingest.

   **Pilot.** `PR68975` Highlands at Hunter's Green (condo, 272 units,
   9481 Highland Oaks Dr 33647) with managing entity `MA63930`, `C/O` a
   management firm; it matches F1 `N03000004881` on exact name + ZIP. The
   master HOA (`N22777`) is Ch. 720, so it is correctly absent from DBPR.
   RealManage appears as `C/O` on 113 projects statewide (4 in Hillsborough,
   2 of them in 33647) and as firm `CAB3372` in F4.
3. ~~**Profile F6/F7.**~~ Done 2026-11-09: F7 carries `DOR_UC`; F6 comes from
   F7 (§2.1).

   **Code-009 owner share, Hillsborough (`CO_NO=39`).** The layer returns
   10,753 rows for 7,061 distinct `PARCEL_ID`s (dedupe on `PARCEL_ID`), with
   2,457 distinct owner names. Page with `returnIdsOnly` then 500 `objectIds`
   per POST: offset paging fails with HTTP 400 past 8,000 rows.

   | Owner class (by name) | Parcels | Owners | Land area |
   |---|---|---|---|
   | Association | 82.2% | 76.4% | 79.3% |
   | CDD | 3.7% | 2.6% | |
   | Company / developer / trust | 8.6% | 14.3% | |
   | Other (no keyword) | 4.7% | 6.1% | |
   | Utility | 0.3% | | |
   | Government | 0.4% | | |

   - `OWN_NAME` is cut at 30 characters, so the classifier matches truncated
     last tokens against keyword prefixes (`…HOMEOWNERS ASS`, `…CONDOMINIU`).
     That, and checking association before government/utility (`…WATER…`,
     `…HILLSBOROUGH CO…` were false hits), moved association from 74.0% to
     82.2% of parcels and 68.3% to 76.4% of owners.
   - 43.9% of parcels have `C/O` in the owner address: the F7 → F4 link.
   - **F1 cross-check** (owner name prefix vs active `DOMNP`): 57% of
     association-named owners match (1,076 of 1,877); 51% of "other"
     (subdivision-only names); 14% of "company" (`TOWNHOMES OF … INC`). So
     associations are ~80% of code-009 owners. The 57% is the Phase 2
     starting point; most misses are abbreviations (`ASSOC`, `HOA`, `CONDO`)
     and truncation.
   - **ZIP 33647:** 734 parcels, 166 owners: 112 association, 28 other, 18
     company, 7 CDD, 1 utility.
4. ~~**Survey F8.**~~ Done 2026-11-09 (checked live). Only **Hillsborough and
   Leon** publish HOA polygons in volume. Elsewhere, use subdivision/plat
   polygons and join HOA names to them (by name or by F7 parcel overlap).
   Service names can change (Broward's is date-stamped), so the connector
   reads them from config.

   | County | HOA layer | Subdivision / plat layer |
   |---|---|---|
   | Hillsborough | `maps.hillsboroughcounty.org/arcgis/rest/services/HOA/HOANOA/FeatureServer/0` — 995 (HOA 628, condo 82, neighborhood 93, umbrella 11, plus civic/crime watch/tax district; filter on `Association_Description`). Has `Organization_Name`, `Number_of_Members`, web site. Hunters Green master (umbrella, 1,832 members) and 14 sub-associations present | `.../DSD_Viewer_Services/DSD_Viewer_Governance/MapServer/2` — 10,947 (`LEGAL_NAME`, `COMM_NAME`, `NUM_LOTS`) |
   | Leon | `services.arcgis.com/ptvDyBs1KkcwzQNJ/arcgis/rest/services/Open_Neighborhoods_Public/FeatureServer/0` — 587 (464 homeowner) | `intervector.leoncountyfl.gov/.../TLC_OverlaySubdivision_D_WM/MapServer/0` — 2,383 |
   | Sarasota | `ags3.scgov.net/server/rest/services/Hosted/NeighborhoodAssociation/FeatureServer/0` — 94; `Hosted/MasterAssociation` — 21 | `Hosted/PlatBoundary` — 5,501 |
   | Volusia | `maps5.vcgov.org/arcgis/rest/services/Open_Data/Open_Data_3/FeatureServer/2` — 436 **points** (Sunbiz records with `COR_NUMBER`, geocoded) | `Open_Data_4/FeatureServer/25` — 9,759 |
   | Orange | Neighborhood organizations `ocgis4.ocfl.net/arcgis/rest/services/AGOL_Open_Data/MapServer/34` — 1,510 (mandatory 1,222) | `.../MapServer/54` — 14,345 |
   | Broward | `services.arcgis.com/JMAJrTsHNLrSsWf5/.../BCPANeighborhoods/FeatureServer/0` — 2,118 (505 with `HOA`) | `gisweb-adapters.bcpa.net/.../BCPA_EXTERNAL_JAN26/MapServer/14` — 15,910 |
   | Miami-Dade | `HomeOwnerAssocZoningMailer_gdb/FeatureServer/0` — 12, expired; skip | `Subdpoly_gdb/FeatureServer/0` — 22,995 |
   | Palm Beach | `Ags/3/MapServer/20` — 2; skip | `OpenData/Housing_and_Community_Open_Data/MapServer/9` — 10,959 |
   | Lee | — | `services2.arcgis.com/LvWGAAhHwbCJ2GMP/.../Subdivisions_and_Condominiums/FeatureServer/0` — 7,579 |
   | Polk | — | `gis.polkfl.gov/server/rest/services/Map_Property_Appraiser/FeatureServer/3` — 6,691 |
   | Pasco | — | `maps.pascopa.com/arcgis/rest/services/Parcels/MapServer/1` — 6,262 |
   | Brevard | — | `gis.brevardfl.gov/gissrv/rest/services/Base_Map/Subdivisions_WKID2881/MapServer/0` — 4,599 (state plane; request `outSR=4326`) |
   | Lake | — | `gis.lakecountyfl.gov/lakegis/rest/services/OpenData/OpenData1/FeatureServer/8` — 4,099 |
   | Seminole | — | `gis.scpafl.org/server/rest/services/features/ext_parcels_building_sales/MapServer/17` — 4,077 |
   | Marion | — | `gis.marionfl.org/public/rest/services/General/ParcelsAndSubdivisions/MapServer/2` — 3,473 |
   | Manatee | — | `www.mymanatee.org/gisits/rest/services/opendata/General/FeatureServer/6` — 3,061 |
   | Duval, Pinellas, Osceola | none found | none confirmed (Pinellas PA returned 403) |
   | Collier, St. Lucie | unconfirmed (503 / 403) | unconfirmed |

**Matcher calibration — `connectors/hoa/fl_match.py` (done 2026-11-09).**
Built and tuned before Phase 1 on the real files (Hillsborough F1/F2/F7,
statewide F2, all 2,070 F4 `CAB` licenses). "Auto" is written without review;
"+review" adds the pairs queued to `review.match_queue`. Tests:
`connectors/hoa/tests/test_fl_match.py` (47 cases from the misses below).

| Link | Baseline | Auto | +review | Precision check |
|---|---|---|---|---|
| F2/F3 managing entity → F1 (Hillsborough, 599) | 68% name+ZIP, 81% with name-only | 83.0% | 87.5% | 60/60 sampled non-`exact_zip` auto correct. 9 of 10 sampled misses are not in F1 at all (ceiling) |
| F7 association owner → F1 (Hillsborough, 1,847) | 57% | 83.1% | 88.8% | as above |
| F7 `C/O` → F4 firm, per parcel (Hillsborough, 3,186 firm-like) | 81.0% (crude `C/O` regex) | 88.1% | 90.5% | 86 sampled non-exact auto: all plausible, 3 borderline (generic-word brands such as `EAST COAST MGMT`) |
| F7 `C/O` → F4, distinct firm names (106) | 56.0% | 57.5% | 67.0% | misses are mostly firms without a `CAB` license |
| F2 managing-entity `C/O` → F4 (statewide, 8,747 firm-like) | 78.5% | 78.7% | 84.4% | auto barely moved, but the first pass's false hits (`FIRST …`, `BUSINESS …`) are gone |
| F1 registered agent → F4 (Hillsborough HOA/condo, 1,074 firm-like agents) | 67.0% | 76.8% | 77.5% | after classing law firms and agent services as `legal` |

What moved the rates:

- **Association names.** Canonical form folds suffixes, glued words
  (`BREEZEHOMEOWNERS`), `CONDO`/`ASSN` spellings and the 30-character F7 cut
  (`ASSOCIATI`, `OF HILLSBOROU`). Cascade: exact → prefix (truncated names
  only) → same core name in a shared ZIP → fuzzy in a shared ZIP (review only).
  Building and phase designators (`BLDG L`, `NO. 3`) must agree; `, A
  CONDOMINIUM` is an article, not building A. Condo vs HOA with the same core
  goes to review. A name-only match is auto only when the F1 address is in the
  source county or the core name is unique statewide and more than one word;
  `STONEY CREEK` and the like go to review.
- **Firm names.** `MANAGEMENT`/`MGT`/`MNGT`… → `MGMT`, legal suffixes dropped,
  misspelled generic words caught by spell-check (`SEVICES`), initials joined
  (`A R` → `AR`), singular = plural, a cut tail word dropped (`FLORID`),
  brands glued (`FIRST SERVICE`, `REAL MANAGE`), every `;`-separated DBA
  indexed. Generic-only names never match. One distinctive word needs the
  office in the same 3-digit ZIP; a query longer than the license name
  (`CAPE CORAL TAX & ACCOUNTING` vs `REALTY ASSOCIATES OF CAPE CORAL`) is
  review unless the ZIP5 matches; fuzzy is auto only at the licensee's own ZIP.
- **`C/O` classification.** Each `C/O` is classed as firm, association,
  district (CDD manager), legal, person or generic, and only firm-like ones are
  looked up. Person `C/O`s (1,019 of the Hillsborough parcels) need F5/CRM
  data, not F4.

Remaining misses are coverage, not matching: CDD managers and firms that hold
no `CAB` license, associations absent from F1, and law-firm registered agents.
The registered agent is therefore a weak management signal; the `C/O` and the
DBPR managing entity carry the firm link.

**Phase 1 — load.**

5. Migrations 021–025.
6. `connectors/hoa/fl_sunbiz_assoc.py` — fixed-width parser (latin-1) for
   `npcordata`, `DOMNP` + revised name filter, daily `c.txt` files for new
   filings and `ce.txt` events for status changes. Tests from a fixture slice.
7. `connectors/hoa/fl_dbpr_condo_coop.py` and `fl_dbpr_cam.py` — follow
   `connectors/resort/fl_dbpr_lodging.py` (dedup on the license/project key,
   never sum repeated totals).
8. `connectors/hoa/fl_common_area_parcels.py` — code-09 parcels with
   geometry, reusing `lib/arcgis.py` and the FL entry in `parcel_layers.yaml`.

**Phase 2 — resolve.**

9. Implement the §4 links in an `hoa_fl_merge` step writing `staging.resolved_*`.
10. Hand-label ~300 pairs (association ↔ parcel owner, association ↔ DBPR,
    agent ↔ CAM firm) from the pilot metro and calibrate the thresholds.
    Uncertain pairs go to `review.match_queue`.

**Phase 1–2 run, 2026-11-10 (local Postgres, not prod).** Loaded F1
statewide (197,041 rows), F2/F3 (28,739), F4 (21,645) and Hillsborough F7
code-009 parcels (7,061), then ran `hoa.hoa_fl_merge` and `lib.core_apply`.
Output: 63,815 accounts, 34,823 locations (6,084 with a parcel boundary),
275,743 contacts, 3,507 pairs queued for review (906 association, 2,601 firm).

Match rates, Hillsborough scope, before → after this round of fixes:

| Link | Before | After |
|---|---|---|
| F1 `C/O` → `CAB` firm (auto) | 88.9% (6,645/7,473) | 90.1% (7,607/8,439) |
| F7 parcel `C/O` → `CAB` firm | 88.6% | 89.3% |
| F1 registered agent → `CAB` firm | 32.5% | 33.3% (13,726/41,185) |
| DBPR managing entity routed to a `CAB` firm | 55.0% | 60.2% |
| Locations with a management firm | 38.3% | 55.9% |
| Sub-association → master parent link | — | 1,685 (Hillsborough), 1,902 statewide to 546 masters |

Statewide: DBPR managing entity → F1 77.4% (19,024/24,582); DBPR entity
routed to a `CAB` firm 80.2%; 44.9% of locations carry a firm (statewide condo
projects have no parcel `C/O`).

What moved the rates:

- **One-word brands.** `C/O REALMANAGE` (449 F1 lines) was classed as a person
  and dropped. A `C/O` whose canonical key is an F4 alias, or a "person" whose
  key is an exact `CAB` license name, now reaches the firm. RealManage went
  from 1 location to 314; Associa Gulf Coast, FirstService Residential and
  MyTown also gained 100+ each.
- **Registered agent as last resort.** A location with no newer source and no
  parcel `C/O` takes the `CAB` firm that is the association's registered agent
  (auto links only). Location firm source: parcel `C/O` 1,793, newer source
  818, registered agent 875, none 2,598.
- **Sub-association parents.** A name of the form `<base> COMMUNITY / MASTER /
  UMBRELLA / PROPERTY OWNERS ASSOCIATION` is a master. A sub is
  `<sub> AT|OF|IN <base> …` or `<base> PARCEL|VILLAGE|NEIGHBORHOOD|PHASE|<n> …`
  in the same 3-digit ZIP. Generic one-word bases (`LAKES`, `OAKS`, `PARK`…)
  never qualify; two masters on one base in one ZIP3 leave the sub unlinked.
  40-row sample: ~95% correct.
- **Utility owners.** The F7 owner classifier no longer calls subdivisions such
  as `WATERFORDE …` or `… BAYSWATER …` utilities (0 utility misclassifications
  after the fix, was 24).
- **Blank DBPR entity names** take the project name, so they are no longer
  rejected by `resolved_account.legal_name NOT NULL`.

Traps found on the run:

- **`core_apply` diffs every vertical.** Any `core.account` row missing from
  `staging.resolved_*` is tombstoned, whatever its vertical. `core_apply
  --dry-run` now prints the diff for each vertical separately; confirm every
  non-`hoa` line is zero before a prod apply. The local run inserted
  `vertical=hoa` rows only.
- **`hoa_fl_merge` replaces all `vertical='hoa'` resolved rows, Texas
  included.** Run the Texas merge in the same batch, or scope the delete by
  state, before a prod apply.
- **Parents need two applies.** `parent_account_key` resolves only against
  accounts already in core. Apply 1 inserts; apply 2 sets the 1,902 parents; a
  third dry run is all zeros (idempotent).
- **Untyped NULL parameters.** The location insert's `IS NULL` checks on bind
  parameters failed on condo locations with no boundary
  (`AmbiguousParameter`); they now `CAST` first.
- **ArcGIS 504s.** The Hillsborough parcel query times out on large ID chunks.
  The fetcher retries, and `--ids-file` accepts a saved `returnIdsOnly`
  response so a rerun skips the ID query.

**Miss analysis, 2026-11-10 (Hillsborough).** We sampled the unmatched rows
of both weak links and sorted them by cause.

| Link | Before | After |
|---|---|---|
| Locations with a management firm | 55.9% (3,486 of 6,084 parcels have one) | 66.3% |
| DBPR managing entity routed to a `CAB` firm | 60.2% | 61.4% (178/290) |
| DBPR entity with no firm route, firm found by office address | — | 50/327 |
| F1 `C/O` → `CAB` firm (statewide) | 90.1% | 90.6% |

Location firm source after: parcel `C/O` 1,741, newer source 1,104,
registered agent 885, office address 409, none 1,945. 58 locations (6
associations) switched firm: the current F1 report names a different firm
than the tax roll, and the newer source wins as before.

Parcel misses by cause (2,598 parcels / 953 accounts before the fix):

| Cause | Parcels | Fixable |
|---|---|---|
| No `C/O`; registered agent is a person (self-managed) | 775 | no |
| No `C/O`; registered agent is a law firm or RA service | 620 | no |
| Parcel-only owner, no `C/O`, no F1 match | 372 | no |
| Owner mailing address is a `CAB` firm's office (one licensee there) | 340 | **fixed** (address) |
| All-generic DBA (`C/O CONDOMINIUM ASSOCIATES`) or cut suffix (`… MGT LL`) | 199 | **fixed** |
| Firm holds no `CAB` license (Continental Group, Rampart, …) | 89 | no |
| Person / board member `C/O` | 68 | no |
| Other `C/O` kinds (association, district, legal) | 61 | mostly no |
| Sub-association whose master has a firm | 27 | follow-up |
| Review-tier name match | 24 | review queue |
| `Castle Group` (licensed as Castle Management) | 23 | **fixed** (alias) |

Ceiling: about 1,985 of the 2,598 misses have no licensed firm in any of our
sources. With the sub-association and review rows resolved, locations top
out near 68% (we are at 66.3%) without a new source.

DBPR route misses: about 90 are "Sterling Management" in Sun City Center,
which holds no `CAB` license (the only Sterling licensee is in Cutler Bay); about 13 are
other unlicensed firms; about 6 are owner/developer LLCs. These are true
no-matches. The other 309 routes are persons or blank.

What changed:

- **Generic DBA at the holder's ZIP** (`full_zip`). A `C/O` made only of
  generic words never matched. It now matches when exactly one licensee spells
  it that way (legal suffixes aside) and has its office at that ZIP.
- **Cut legal suffix.** A trailing `LL` / `IN` (a 35-character field cut inside
  `LLC` / `INC`) is dropped before matching.
- **Office address** (`FirmAddressIndex`, last resort). The F1 mailing
  address, the DBPR managing-entity address or the parcel owner address
  matches a `CAB` firm's office when the street and ZIP are equal and only one
  licensee is there. The street needs a house number or a PO box. Different
  suites are different offices. The rule is skipped when the `C/O` names a
  company, since the address then belongs to that company.
- **Alias** `CASTLE GROUP` → Castle Management LLC.

Precision. Where F1 names a firm, the address rule picks the same firm
94.8% of the time (5,158/5,439 statewide); several disagreements are sister
companies. In spot checks, 22 of 25 new location matches showed visible
evidence for the firm (name, DBA or office address) and none were wrong. All
20 sampled DBPR address matches and the 7 new DBPR name matches were correct.

**Phase 3 — enrich and serve.**

11. Point `hoa_gmaps_enrich.py` at Florida associations that have no CAM firm
    phone.
12. Add Florida stages to `scripts/run_hoa.sh` behind a `HOA_STATES` switch, so
    Texas runs are unchanged.
13. **Pilot: Hunters Green, Tampa North (Hillsborough, ZIP 33647)** end to end,
    scored against the Aspire record in §8, then all code-009 parcels in
    33647, then review the output with sales before running statewide.

**Phase 4 — statewide.**

14. Run all 67 counties. Record row counts and geometry/`geocode_precision`
    coverage for the ArcGIS map work.

---

## 7. Open questions

1. ~~Management firm as account or contact.~~ Own account (§3).
2. ~~Pilot metro.~~ Hunters Green / Tampa North (§8).
3. ~~Do F2/F3 carry a Sunbiz doc number?~~ No (§6 step 2). The condo link is
   tier 2: name + ZIP, 68% unique in Hillsborough, 81% with unique name-only.
4. ~~Does F7 carry `DOR_UC`?~~ Yes (§2.1).
5. Officer addresses in F1 (board members and registered agent) are often
   home addresses. Use them for matching
   only. Do not surface them as contact addresses without a decision on
   outreach policy (CAN-SPAM/TCPA notes in the CRM workstreams plan).
6. Is the Sunbiz Daily API subscription still worth paying for? The bulk file
   covers F1. Profile the API's detail payload once the key is available, and
   keep it only if it adds something the bulk file lacks (e.g. more than six
   officers, or officer changes between quarterlies).

---

## 8. Pilot example: Hunters Green (Aspire vs public sources)

Hunters Green Community Association, Tampa 33647, branch Tampa North
Maintenance. A single-family master HOA (Ch. 720) with sub-associations, so it
is the hardest Florida case: not in the DBPR condo files, known only through
Sunbiz, the tax roll and the CAM licenses. Aspire is the ground truth. Checked
2026-11-09.

| Item | Aspire | Public sources | Result |
|---|---|---|---|
| Association | Company 378649 "Hunters Green Community Association" | F7: 23 Hillsborough parcels owned under 4 name spellings, ~590 acres, mostly `DOR_UC 009` | Found; name normalization required |
| HQ / common area address | 9456 Highland Oaks Drive, Tampa 33647 (properties 704396, 704482) | F7 owner address `9456 HIGHLAND OAK DR` on 12 parcels | Match |
| Geolocation | None on any of the three properties | F7 polygons; HQ parcel centroid ~28.1300, -82.3466 | Public fills an Aspire gap |
| Current manager | RealManage (filed twice: companies 356401 and 207797) | F4 `REALMANAGE LLC`, CAB3372, PO Box 803555 Dallas 75380; F7 `C/O REALMANAGE DALLAS 75380` on 3 parcels | Match, one firm account |
| Previous manager | Wise Property Management (371455), still linked | F4 CAB448; F7 `C/O WISE PROPERTY MANAGEMENT` on 8 parcels (~440 acres) | Both sources stale; newest source wins |
| Sub-association | Property 706672 "Wynstone @ Hunters Green", managed by New Tampa Property Management | F7 `WYNSTONE NEIGHBORHOOD ASSOC IN`, `C/O NEW TAMPA PROPERTY MANAGEMENT`; F4 CAB2519 | Match. Public also finds Parcel 3, 6, 7, 10 neighborhood associations Aspire does not have (managers incl. Unique Property Services CAB3665) |
| Named managers | 7 contacts; 2 labeled LCAM | F5 current-license file finds 2 of 7; neither of the LCAM-labeled ones | People come from CRM, not DBPR |
| Phone / email | Front desk 813-991-4818, business emails | None in F1/F4/F5/F7 | Needs F9 or CRM |
| Sunbiz doc number | Not stored | F1 `N22777`, active, agent Bush Ross, 6 officers; sub-associations Waterforde, Wynstone, Parcel 3 and Fox Chase also filed | Found in the bulk file |

Aspire is messy (here: RealManage as two companies, its staff filed under the
association, the replaced manager still linked, no geolocation), and duplicates
are common across the whole dataset. **We do not clean Aspire.** The pipeline
must expect many Aspire records for one real entity: match each pipeline
account to all of them and score the pilot on the match, not on Aspire being
tidy.

Pilot pass criteria: the pipeline produces one master association account
with the 23 parcels, a parent link from each sub-association, one RealManage
firm account, the Sunbiz doc number, and `management_account_id` on each
location pointing at RealManage (Wynstone: New Tampa Property Management).


**Pilot result, 2026-11-10.**

| Criterion | Result |
|---|---|
| One master account with the Sunbiz doc number | Pass: `N22777` |
| 23 parcels on the master | Partial: 18. The other parcels are under owner spellings that resolve to sub-associations or stay parcel-only |
| Parent link from each sub-association | Pass for every sub named `… HUNTERS GREEN …`: Parcel 3/6/7/10 neighborhoods, the `PARCEL 10 NEIGH` parcel-only accounts and the Highlands condo all link to `N22777`. Wynstone and Fox Chase do not: their names do not carry the master's |
| One RealManage firm account | Pass: CAB3372, one account |
| Master locations managed by RealManage | Partial: 2 of 18 RealManage, 5 Wise, 11 none. The tax-roll `C/O` is mixed and stale, the master has no F1 `C/O`, and its registered agent is a law firm |
| Wynstone managed by New Tampa PM | Pass: CAB2519 |

Gaps for review with sales: master-to-sub links for subs without the master's
name (Wynstone, Fox Chase) need the county HOA layer (§2.1 F8) or Aspire; the
Highlands condo's manager (Aspire: KW Property Management, CAB3320) is not in
any public source; the stale master manager needs a newer source (F9 or CRM).