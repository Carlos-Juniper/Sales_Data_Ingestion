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
| F2 | **DBPR condominium extract** | Project rows + managing-entity rows | Condo projects with **unit counts**; managing association with fund revenue/expense | DBPR project no. | Verify |
| F3 | **DBPR cooperative extract** | Project rows + managing entity | Same as F2 for Ch. 719 co-ops (includes resident-owned manufactured-home communities) | DBPR project no. | Verify |
| F4 | **DBPR CAM firm licensees** | One row per licensed firm | Management companies — frequently the buyer | CAM firm license no. | Verify |
| F5 | **DBPR CAM individual licensees** | One row per licensed manager | The named manager. Name and city only, **no employer field**, so it cannot link a person to a firm | CAM license no. | Verified 2026-11-09 |
| F6 | **FDOR tax roll (NAL) — DOR use code 09** | One row per parcel, per county | Fallback only. F7 carries the same roll fields (see below) | Parcel id | Fallback |
| F7 | **Florida Statewide Cadastral** (ArcGIS) | One polygon per parcel | Code-09 common-area parcels with geometry, acreage, owner name and owner mailing address (`C/O <management firm>` when managed) | `PARCEL_ID` | Verified 2026-11-09; already in `connectors/healthcare/config/parcel_layers.yaml` |
| F8 | **County HOA / subdivision boundary layers** | Per county, varies | Actual HOA boundaries where a county publishes them (Volusia does: `maps.vcgov.org/gis/data/HomeOwnerAssoc.htm`) | County-specific | Survey which counties publish |
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
  withdrawn projects are not included.
- **F4, F5** — `www2.myfloridalicense.com/community-association-managers-and-firms/public-records/`.
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
| F2/F3 managing entity → F1 | Exact Sunbiz doc number if the DBPR file carries one (verify); else normalized name + ZIP5 | 1 or 2 |
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
2. **Profile F2–F5.** F4/F5 partly done: `lic38cam.csv` (5 MB, 28,907 rows)
   mixes record types `CAB` (firm, 2,070), `CAM` (person, 19,575) and course
   providers (`CRSE`, `PROV`, `PRE`), so filter on type. It holds current
   licenses only; `cams.csv` (122 MB) is the full history. Download each DBPR file, record columns, row grain,
   duplicate behavior (the lodging extract was one row per unit — check for the
   same trap), and whether F2/F3 carry a Sunbiz doc number. *Done when* each
   has a layout table here.
3. ~~**Profile F6/F7.**~~ Done 2026-11-09: F7 carries `DOR_UC`; F6 comes from
   F7 (§2.1). Still to do: the share of code-009 owner names that look like
   associations (Hillsborough 33647 sample: 37 distinct owners, roughly half
   associations, the rest utilities, developers and trusts).
4. **Survey F8.** List which Florida counties publish an HOA or subdivision
   boundary layer. *Done when* there is a county list with URLs.

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
3. Do F2/F3 carry a Sunbiz doc number? This decides whether the condo link is
   tier 1 or tier 2.
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
| Named managers | 7 contacts; 2 labeled LCAM (Peterson, Finnerty) | F5 current-license file finds 2 of 7 (Chiorando, Lusk); none of the LCAM-labeled ones | People come from CRM, not DBPR |
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
