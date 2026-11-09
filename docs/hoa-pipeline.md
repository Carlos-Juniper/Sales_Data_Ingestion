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
| F1 | **Sunbiz corporate bulk data** | One row per corporation | The association as a legal entity: legal name, status, filing date, principal + mailing address, registered agent, up to 6 officers with titles and addresses | `sunbiz_doc` | Verify layout (§8 item 3) |
| F2 | **DBPR condominium extract** | Project rows + managing-entity rows | Condo projects with **unit counts**; managing association with fund revenue/expense | DBPR project no. | Verify |
| F3 | **DBPR cooperative extract** | Project rows + managing entity | Same as F2 for Ch. 719 co-ops (includes resident-owned manufactured-home communities) | DBPR project no. | Verify |
| F4 | **DBPR CAM firm licensees** | One row per licensed firm | Management companies — frequently the buyer | CAM firm license no. | Verify |
| F5 | **DBPR CAM individual licensees** | One row per licensed manager | The named manager. Name and city only, **no employer field**, so it cannot link a person to a firm | CAM license no. | Verified 2026-11-09 |
| F6 | **FDOR tax roll (NAL) — DOR use code 09** | One row per parcel, per county | Fallback only. F7 carries the same roll fields (see below) | Parcel id | Fallback |
| F7 | **Florida Statewide Cadastral** (ArcGIS) | One polygon per parcel | Code-09 common-area parcels with geometry, acreage, owner name and owner mailing address (`C/O <management firm>` when managed) | `PARCEL_ID` | Verified 2026-11-09; already in `connectors/healthcare/config/parcel_layers.yaml` |
| F8 | **County HOA / subdivision boundary layers** | Per county, varies | Actual HOA boundaries where a county publishes them (Volusia does: `maps.vcgov.org/gis/data/HomeOwnerAssoc.htm`) | County-specific | Survey which counties publish |
| F9 | **Google Places** | One lookup per association | Phone and website fallback | Place id | Reuse `hoa_gmaps_enrich.py` |

Source locations:

- **F1** — **Sunbiz Daily API** (`www.sunbizdaily.com/api/v1`), which we
  already pay for. Key `SUNBIZ_API_KEY`, header `X-API-Key`. The old repo
  (`juniperlandscaping/juniper-crm-HOA-pipeline`, `scrapers/sunbiz.py`) shows
  how it works:
  - `GET /filings/?filing_type=DOMNP&state=FL&zip=<prefix>` lists active
    Florida non-profits (~23.8k as of 2026-06). Pages are capped at
    100 × 100, so the old code splits by ZIP prefix (`32`, `330`–`339`, `34`).
  - `GET /filings/<corporation_number>/` returns one filing's detail. Phase 0
    must confirm it carries officers, registered agent and addresses.
  - Rate limit 1,000 req/hr; the old code pauses 7.5 s per request.

  Fallback if the API lacks a field: the state's free SFTP
  (`sftp.floridados.gov`, fixed-width, layout `dos.sunbiz.org/data-definitions/cor.html`).
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
  Do not scrape it; use the API.

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
| F1 → association filter | Non-profit entity type + the §6.2 name regex; exclude `management`, `realty`, `services`, `LLC` | — (filter, ~85–92% precision) |
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

1. **Profile F1.** Pull the Hillsborough ZIP buckets from the Sunbiz Daily
   API, fetch detail for Hunters Green and 50 others, record which fields come
   back (officers, agent, addresses), count regex hits. *Done when*
   the profile, with fill rates, is written up here.
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
6. `connectors/hoa/fl_sunbiz_assoc.py` — fixed-width parser, non-profit +
   name filter, daily-delta support. Tests from a fixture slice.
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
| Sunbiz doc number | Not stored | Not checked yet; comes with the F1 API profile | Open |

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
