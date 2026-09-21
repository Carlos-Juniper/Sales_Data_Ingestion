"""
Parks vertical — merge, acreage resolution, and the resolved_* write.

Reads the harvested park sources, the government spine and the park -> government
rollup, and produces staging.resolved_account / resolved_location /
resolved_contact for vertical='parks'.

Grain, per Ingestion-Plan-of-Action §6.4: the ACCOUNT is a government
(municipality, county, or state park agency) and each park is a child LOCATION.
So resolved_account row count tracks governments (~5,551), not parks (~72k).

Stages
------
1. load_sources      — union the six park staging tables + park_attrs + rollup
2. polygon_dedup     — §5.2: IoU > 0.60 AND name similarity > 0.5
3. resolve_acreage   — reconcile published vs measured acres, set acres_confidence
4. build_resolved_*  — accounts from the spine, locations from surviving parks
5. replace_and_upsert — one transaction, scoped to vertical='parks' (D15)

Contacts
--------
resolved_contact is intentionally empty for this vertical.  Plan §6.4: "park-level
data carries no contact information, so the only join that reaches a human resolves
to a government."  Shipping zero park contacts is the honest state, not a defect;
contact acquisition (SAM.gov solicitations, parks-director sourcing) is separate
work.

CLI usage
---------
    python -m parks.parks_merge --dry-run
    python -m parks.parks_merge
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

import pandas as pd

from lib.enums import SEGMENT_MUNICIPAL, SEGMENT_STATE
from lib.keys import compute_account_key, compute_location_key, is_present
from lib.match import name_similarity
from lib.normalize import normalize_name
from lib.resolved_writer import replace_and_upsert
from parks.config_loader import load_gov_config, load_layer_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("parks_merge")

VERTICAL = "parks"

# Plan §5.2, the polygon-source dedup rule:
#   "Parks especially will arrive three times — once from PAD-US, once from the
#    state layer, once from the city's own Hub layer.  Match on
#    intersection-over-union > 0.60 of boundary geometry plus name similarity
#    > 0.5."
IOU_THRESHOLD: float = 0.60
NAME_THRESHOLD: float = 0.50

# Acreage disagreement above which the two figures are treated as not corroborating.
ACRES_VARIANCE_TOLERANCE: float = 0.10

# Survivorship precedence for geometry and attributes, per plan §5.3:
#   "Geometry / boundary: County parcel -> city Hub layer -> state layer ->
#    PAD-US/NSD -> geocoded point"
# Lower number wins.  A state agency's own boundary for its own park beats the
# national aggregate's redistribution of it.
_SOURCE_PRECEDENCE: dict[str, int] = {
    "fdep_state_parks": 10,
    "nc_state_parks": 10,
    "sc_state_parks": 10,
    "tpwd_state_parks": 10,
    "pasda_dcnr_parks": 20,
    "padus_parks": 30,
}
_DEFAULT_PRECEDENCE = 99

# Sentinel gov_source_id used by manager_resolve for declared state agencies.
_AGENCY_SOURCE = "state_agency"


def _precedence(source_id: str) -> int:
    return _SOURCE_PRECEDENCE.get(source_id, _DEFAULT_PRECEDENCE)


# ---------------------------------------------------------------- load

def load_sources(engine, park_sources: list[str]) -> pd.DataFrame:
    """
    Load every park row with its attributes and its resolved government.

    One row per park, carrying identity, name, state, point coordinates, both
    acreage figures, and the rollup assignment.
    """
    from sqlalchemy import text
    from lib.db import _assert_safe_identifier

    parts = []
    for src in park_sources:
        _assert_safe_identifier(src)
        parts.append(
            f"SELECT source_id, natural_key, account_type, name_raw, "
            f"name_normalized, address_line_1, city, state, zip5, "
            f"latitude, longitude, segment, size_value "
            f"FROM staging.{src}"
        )
    union = "\n            UNION ALL\n            ".join(parts)

    sql = text(f"""
        WITH parks AS (
            {union}
        )
        SELECT p.*,
               a.acres_published,
               a.acres_computed,
               a.owner_raw,
               a.manager_raw,
               ST_AsGeoJSON(a.boundary) AS boundary_geojson,
               r.gov_source_id,
               r.gov_natural_key,
               r.method   AS rollup_method,
               r.score    AS rollup_score
        FROM parks p
        LEFT JOIN staging.park_attrs a
               ON a.source_id = p.source_id AND a.natural_key = p.natural_key
        LEFT JOIN staging.park_rollup r
               ON r.park_source_id = p.source_id AND r.park_natural_key = p.natural_key
    """)  # nosec: source ids validated above
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn)
    logger.info("loaded %d park rows", len(df))
    return df


def load_gov_spine(engine, gov_sources: list[str]) -> pd.DataFrame:
    """Load the government units that become accounts."""
    from sqlalchemy import text
    from lib.db import _assert_safe_identifier

    parts = []
    for src in gov_sources:
        _assert_safe_identifier(src)
        parts.append(
            f"SELECT source_id, natural_key, account_type, name_raw, "
            f"name_normalized, state, county_fips, latitude, longitude "
            f"FROM staging.{src}"
        )
    sql = text("\n        UNION ALL\n        ".join(parts))  # nosec: validated above
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn)
    logger.info("loaded %d government units", len(df))
    return df


def load_iou_pairs(engine, park_sources: list[str]) -> pd.DataFrame:
    """
    Find candidate duplicate park pairs by boundary intersection-over-union.

    IoU is computed as I / (A + B - I) rather than via ST_Union.  The identity is
    exact, and it avoids constructing a union geometry per pair — ST_Union is by
    far the most expensive operation available here and this runs across every
    intersecting pair among ~72k polygons.

    Only cross-source pairs are considered.  Two polygons from the SAME source that
    overlap heavily are usually a genuine sub-unit relationship (a park containing a
    named ballfield tract), not a duplicate, and that source's own natural keys
    already distinguish them.

    The tuple comparison (a.source_id, a.natural_key) < (b...) yields each pair
    once in a stable order.
    """
    from sqlalchemy import text

    sql = text("""
        SELECT a.source_id   AS a_source_id,
               a.natural_key AS a_natural_key,
               b.source_id   AS b_source_id,
               b.natural_key AS b_natural_key,
               ST_Area(ST_Intersection(a.boundary, b.boundary))
                 / NULLIF(
                     ST_Area(a.boundary) + ST_Area(b.boundary)
                       - ST_Area(ST_Intersection(a.boundary, b.boundary)),
                     0
                   ) AS iou
        FROM staging.park_attrs a
        JOIN staging.park_attrs b
          ON a.source_id <> b.source_id
         AND (a.source_id, a.natural_key) < (b.source_id, b.natural_key)
         AND ST_Intersects(a.boundary, b.boundary)
        WHERE a.boundary IS NOT NULL
          AND b.boundary IS NOT NULL
    """)
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn)

    # An empty result has no columns at all, so filtering on "iou" would raise
    # KeyError.  This is a normal state, not an error: it happens on a first run
    # against an empty staging.park_attrs, and whenever no two sources describe
    # overlapping polygons.
    if df.empty:
        logger.info("no intersecting park polygons — nothing to dedup")
        return pd.DataFrame(
            columns=[
                "a_source_id", "a_natural_key",
                "b_source_id", "b_natural_key", "iou",
            ]
        )

    df = df[df["iou"].notna() & (df["iou"] > IOU_THRESHOLD)]
    logger.info("found %d candidate pairs above IoU %.2f", len(df), IOU_THRESHOLD)
    return df


# ---------------------------------------------------------------- dedup

def polygon_dedup(parks: pd.DataFrame, pairs: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse duplicate parks that arrived from more than one source (§5.2).

    A pair merges when boundary IoU exceeds 0.60 *and* name similarity exceeds
    0.50.  Both conditions are required: geometry alone would merge a park with the
    larger preserve it sits inside, and name alone would merge every "Oak Grove
    Park" in a state.

    Connected components are resolved with union-find, so three sources describing
    one park collapse to one record.  The survivor is the member with the highest
    source precedence (§5.3), and `merged_sources` records every folded-in
    identity so provenance is preserved rather than discarded.
    """
    parks = parks.copy().reset_index(drop=True)
    parks["_ident"] = list(zip(parks["source_id"], parks["natural_key"]))

    if parks.empty:
        parks["merged_sources"] = []
        parks["merge_iou"] = []
        return parks

    name_by_ident = dict(zip(parks["_ident"], parks["name_normalized"].fillna("")))
    parent: dict[tuple, tuple] = {i: i for i in parks["_ident"]}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    best_iou: dict[tuple, float] = {}
    merged_pairs = 0
    for row in pairs.itertuples(index=False):
        a = (row.a_source_id, row.a_natural_key)
        b = (row.b_source_id, row.b_natural_key)
        if a not in parent or b not in parent:
            continue
        sim = name_similarity(name_by_ident.get(a, ""), name_by_ident.get(b, ""))
        if sim <= NAME_THRESHOLD:
            continue
        union(a, b)
        merged_pairs += 1
        for ident in (a, b):
            best_iou[ident] = max(best_iou.get(ident, 0.0), float(row.iou))

    groups: dict[tuple, list[int]] = {}
    for pos, ident in enumerate(parks["_ident"]):
        groups.setdefault(find(ident), []).append(pos)

    survivors = []
    for members in groups.values():
        if len(members) == 1:
            row = parks.iloc[members[0]].copy()
            row["merged_sources"] = f"{row['source_id']}:{row['natural_key']}"
            row["merge_iou"] = None
            survivors.append(row)
            continue

        ordered = sorted(
            members,
            key=lambda pos: (
                _precedence(parks.iloc[pos]["source_id"]),
                str(parks.iloc[pos]["natural_key"]),
            ),
        )
        base = parks.iloc[ordered[0]].copy()
        for pos in ordered[1:]:
            other = parks.iloc[pos]
            for col in parks.columns:
                if col in ("source_id", "natural_key", "_ident"):
                    continue
                if not is_present(base.get(col)) and is_present(other.get(col)):
                    base[col] = other[col]
        base["merged_sources"] = ",".join(
            f"{parks.iloc[p]['source_id']}:{parks.iloc[p]['natural_key']}"
            for p in ordered
        )
        base["merge_iou"] = best_iou.get(base["_ident"])
        survivors.append(base)

    out = pd.DataFrame(survivors).reset_index(drop=True)
    logger.info(
        "polygon_dedup: %d parks -> %d survivors (%d pairs merged)",
        len(parks), len(out), merged_pairs,
    )
    return out


# ---------------------------------------------------------------- acreage

def resolve_acreage(parks: pd.DataFrame) -> pd.DataFrame:
    """
    Reconcile each park's two acreage figures into maintained_acres + confidence.

    `acres_computed` is preferred whenever present.  It is measured from the actual
    mapped boundary on the WGS84 ellipsoid by PostGIS, which is both reproducible
    and independent of whatever a source chose to publish — and publishing errors
    are real: TPWD's Shape__Area is Web Mercator square metres, over-stating
    acreage by ~35% across Texas.

    acres_confidence is mapped onto the value set core.location.acres_confidence
    already documents (measured | estimated | banded):

      measured   — measured from real geometry
      estimated  — only the source's published figure exists, nothing to check it
      banded     — neither figure available

    Records where the two figures disagree by more than the tolerance keep the
    measured value and are counted in the report, since a systematic disagreement
    means a registry misconfiguration rather than messy data.

    Note this is TOTAL park area, not mowable turf.  A 500-acre regional park may
    hold far less maintained turf; deriving that is deferred work and any
    sales-facing figure should say so.
    """
    parks = parks.copy()
    published = pd.to_numeric(parks.get("acres_published"), errors="coerce")
    computed = pd.to_numeric(parks.get("acres_computed"), errors="coerce")

    # Fall back to the canonical staging figure when park_attrs has no published
    # value (e.g. a source harvested before park_attrs existed).
    published = published.fillna(pd.to_numeric(parks.get("size_value"), errors="coerce"))

    parks["maintained_acres"] = computed.where(computed.notna(), published)

    confidence = pd.Series("banded", index=parks.index, dtype=object)
    confidence[published.notna()] = "estimated"
    confidence[computed.notna()] = "measured"
    parks["acres_confidence"] = confidence

    both = published.notna() & computed.notna() & (published > 0)
    variance = ((computed - published).abs() / published).where(both)
    parks["acres_variance"] = variance
    parks["acres_disagrees"] = both & (variance > ACRES_VARIANCE_TOLERANCE)

    return parks


def report_acreage(parks: pd.DataFrame) -> None:
    """Write the acreage reconciliation summary to stderr."""
    total = len(parks)
    sys.stderr.write(f"\n  parks_merge: acreage across {total:,} parks\n")
    for level, count in parks["acres_confidence"].value_counts().sort_index().items():
        acres = parks.loc[parks["acres_confidence"] == level, "maintained_acres"].sum()
        sys.stderr.write(f"    {str(level):<10} {count:>8,}  {acres:>14,.0f} acres\n")

    missing = int(parks["maintained_acres"].isna().sum())
    if missing:
        sys.stderr.write(f"    no acreage at all: {missing:,}\n")

    disagree = int(parks["acres_disagrees"].sum())
    if disagree:
        sys.stderr.write(
            f"    published vs measured disagree >{ACRES_VARIANCE_TOLERANCE:.0%}: "
            f"{disagree:,}\n"
        )
        by_source = (
            parks[parks["acres_disagrees"]].groupby("source_id").size().sort_values(ascending=False)
        )
        for src, count in by_source.items():
            src_total = int((parks["source_id"] == src).sum())
            sys.stderr.write(f"      {src:<22} {count:>7,} / {src_total:,}\n")


# ---------------------------------------------------------------- accounts

def build_resolved_account(
    gov: pd.DataFrame,
    parks: pd.DataFrame,
    registry: dict,
) -> pd.DataFrame:
    """
    Build one resolved_account row per government that actually owns parks.

    Governments with no parks are excluded.  The spine enumerates every
    municipality and county in the five states, but an account with nothing to
    maintain is not a lead — it is noise in a CRM, and the plan is explicit about
    not inventing accounts that "cannot sign a contract".

    account_key comes from the Census GEOID, the Tier-1 key for this vertical
    (§5.1), so it is stable across TIGER vintages and across pipeline runs.
    State agencies have no GEOID and are keyed on their declared slug instead.

    size_metric is the SUM of the government's parks' acreage.  That total is the
    number a rep quotes, which makes it the account's headline metric rather than
    the unit's own land area.
    """
    acres_by_gov = (
        parks.dropna(subset=["gov_natural_key"])
        .groupby(["gov_source_id", "gov_natural_key"])["maintained_acres"]
        .agg(["sum", "count"])
        .rename(columns={"sum": "total_acres", "count": "park_count"})
    )

    rows = []

    for row in gov.itertuples(index=False):
        ident = (row.source_id, str(row.natural_key))
        if ident not in acres_by_gov.index:
            continue
        stats = acres_by_gov.loc[ident]
        geoid = str(row.natural_key)
        total_acres = stats["total_acres"]

        rows.append({
            "account_key": compute_account_key({"geoid": geoid}, priority=("geoid",)),
            "vertical": VERTICAL,
            "account_type": row.account_type,
            "legal_name": row.name_raw,
            "name_normalized": row.name_normalized or normalize_name(str(row.name_raw)),
            "dba_name": None,
            "parent_account_key": None,
            "mailing_address": json.dumps({"state": row.state}) if is_present(row.state) else None,
            "phone": None,
            "email": None,
            "website": None,
            "status": "active",
            "external_keys": json.dumps({"geoid": geoid}),
            "size_metric": float(total_acres) if pd.notna(total_acres) else None,
            "size_metric_unit": "acres",
            "confidence": None,
            "_gov_source_id": row.source_id,
            "_gov_natural_key": geoid,
        })

    # State park agencies: declared in park_layers.yaml, no TIGER counterpart.
    agencies = {
        cfg.managing_agency_slug: (cfg.managing_agency, cfg.states[0])
        for cfg in registry.values()
        if cfg.managing_agency_slug
    }
    for slug, (agency_name, state) in sorted(agencies.items()):
        ident = (_AGENCY_SOURCE, slug)
        if ident not in acres_by_gov.index:
            continue
        stats = acres_by_gov.loc[ident]
        total_acres = stats["total_acres"]
        rows.append({
            "account_key": compute_account_key({"agency": slug}, priority=("agency",)),
            "vertical": VERTICAL,
            "account_type": "state_agency",
            "legal_name": agency_name,
            "name_normalized": normalize_name(agency_name),
            "dba_name": None,
            "parent_account_key": None,
            "mailing_address": json.dumps({"state": state}),
            "phone": None,
            "email": None,
            "website": None,
            "status": "active",
            "external_keys": json.dumps({"agency_slug": slug}),
            "size_metric": float(total_acres) if pd.notna(total_acres) else None,
            "size_metric_unit": "acres",
            "confidence": None,
            "_gov_source_id": _AGENCY_SOURCE,
            "_gov_natural_key": slug,
        })

    out = pd.DataFrame(rows)
    logger.info("built %d resolved_account rows", len(out))
    return out


def build_resolved_location(parks: pd.DataFrame, account_df: pd.DataFrame) -> pd.DataFrame:
    """
    Build one resolved_location row per surviving park.

    location_key carries the park's own source_id:natural_key as a discriminator.
    Parks have no street address — every park in a city would otherwise hash to the
    identical account_key + empty address + empty ZIP and collapse into a single
    location row, silently discarding the whole portfolio.

    Both geom (point) and boundary (polygon) are populated where available, so
    downstream consumers get a map pin and the real footprint.  The boundary is
    also what a future routing phase needs.
    """
    if account_df.empty:
        return pd.DataFrame()

    account_map = dict(
        zip(
            zip(account_df["_gov_source_id"], account_df["_gov_natural_key"]),
            account_df["account_key"],
        )
    )

    rows = []
    for row in parks.itertuples(index=False):
        gov_ident = (row.gov_source_id, str(row.gov_natural_key))
        account_key = account_map.get(gov_ident)
        if not account_key:
            continue

        discriminator = f"{row.source_id}:{row.natural_key}"
        site_addr = {}
        for field in ("address_line_1", "city", "state", "zip5"):
            val = getattr(row, field, None)
            if is_present(val):
                site_addr[field] = str(val)

        acres = getattr(row, "maintained_acres", None)
        lat = row.latitude
        lon = row.longitude

        rows.append({
            "location_key": compute_location_key(
                account_key,
                {"address_line_1": row.address_line_1, "zip5": row.zip5},
                discriminator=discriminator,
            ),
            "account_key": account_key,
            "vertical": VERTICAL,
            "location_name": row.name_raw if is_present(row.name_raw) else None,
            "site_address": json.dumps(site_addr) if site_addr else None,
            "_latitude": float(lat) if is_present(lat) else None,
            "_longitude": float(lon) if is_present(lon) else None,
            "_boundary_geojson": (
                row.boundary_geojson if is_present(row.boundary_geojson) else None
            ),
            "geocode_precision": "polygon_centroid" if is_present(row.boundary_geojson) else None,
            "geometry_source": (
                "padus" if row.source_id == "padus_parks" else "state_layer"
            ),
            "maintained_acres": float(acres) if is_present(acres) else None,
            "acres_confidence": row.acres_confidence,
            "site_type": "park",
        })

    out = pd.DataFrame(rows)
    logger.info("built %d resolved_location rows", len(out))
    return out


def build_resolved_contact() -> pd.DataFrame:
    """
    Return an empty contact frame.

    Parks carry no contact data at any level.  Plan §6.4: "park-level data carries
    no contact information, so the only join that reaches a human resolves to a
    government."  An empty frame is the accurate representation; fabricating
    placeholder contacts would put unusable rows in front of sales.
    """
    return pd.DataFrame(
        columns=[
            "contact_key", "account_key", "vertical", "full_name", "role",
            "role_rank", "phone", "email", "address", "source_id", "is_current",
        ]
    )


# ---------------------------------------------------------------- summary

def print_summary(
    parks: pd.DataFrame,
    account_df: pd.DataFrame,
    location_df: pd.DataFrame,
) -> None:
    """Write the merge summary to stderr."""
    sys.stderr.write("\n  parks_merge: summary\n")
    sys.stderr.write(f"    surviving parks    : {len(parks):>8,}\n")
    sys.stderr.write(f"    resolved_account   : {len(account_df):>8,}\n")
    sys.stderr.write(f"    resolved_location  : {len(location_df):>8,}\n")

    if not account_df.empty:
        sys.stderr.write("    accounts by type\n")
        for atype, count in account_df["account_type"].value_counts().sort_index().items():
            sys.stderr.write(f"      {str(atype):<16} {count:>7,}\n")
        total_acres = account_df["size_metric"].sum()
        sys.stderr.write(f"    total rolled-up acreage: {total_acres:,.0f}\n")
        top = account_df.nlargest(5, "size_metric")[["legal_name", "size_metric"]]
        sys.stderr.write("    largest accounts by acreage\n")
        for r in top.itertuples(index=False):
            sys.stderr.write(f"      {str(r.legal_name)[:34]:<34} {r.size_metric:>12,.0f}\n")

    orphans = len(parks) - len(location_df)
    if orphans:
        sys.stderr.write(
            f"    WARNING: {orphans:,} parks produced no location row — "
            f"their government is absent from the spine\n"
        )


# ---------------------------------------------------------------- driver

def run_pipeline(engine, dry_run: bool = False) -> dict:
    """Execute the full parks merge and write staging.resolved_* for vertical='parks'."""
    logger.info("=== Parks merge starting (dry_run=%s) ===", dry_run)

    registry = load_layer_config()
    gov_registry = load_gov_config()

    parks = load_sources(engine, list(registry))
    if parks.empty:
        logger.warning("no park rows in staging — nothing to merge")
        return {"source_rows": 0, "account": 0, "location": 0, "contact": 0}

    pairs = load_iou_pairs(engine, list(registry))
    parks = polygon_dedup(parks, pairs)
    parks = resolve_acreage(parks)
    report_acreage(parks)

    gov = load_gov_spine(engine, list(gov_registry))
    account_df = build_resolved_account(gov, parks, registry)
    location_df = build_resolved_location(parks, account_df)
    contact_df = build_resolved_contact()

    print_summary(parks, account_df, location_df)

    if dry_run:
        logger.info("DRY RUN — staging.resolved_* not written")
        return {
            "source_rows": len(parks),
            "account": len(account_df),
            "location": len(location_df),
            "contact": 0,
        }

    n_account, n_location, n_contact = replace_and_upsert(
        engine,
        account_df.drop(columns=["_gov_source_id", "_gov_natural_key"], errors="ignore"),
        location_df,
        contact_df,
        vertical=VERTICAL,
    )
    logger.info("=== Parks merge complete ===")
    return {
        "source_rows": len(parks),
        "account": n_account,
        "location": n_location,
        "contact": n_contact,
    }


def main() -> None:
    """CLI entrypoint."""
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the merge but do not write to staging.resolved_* tables.",
    )
    args = ap.parse_args()

    from lib.db import get_engine
    from lib.http import get_secret

    if not get_secret("DATABASE_URL"):
        sys.exit(
            "ERROR: DATABASE_URL is not set. Copy .env.example -> .env and fill it in."
        )
    run_pipeline(get_engine(), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
