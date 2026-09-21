"""
Parks vertical — government-unit connector (the account spine).

Ingestion-Plan-of-Action §6.4 fixes the grain for this vertical:

    "The municipality is the account; parks are child locations.  The contract is
     awarded at governing-body level and typically bundles all parks plus medians
     plus facility grounds — there is no bid for one park."

This module harvests those accounts.  park_layers.py harvests the parks;
manager_resolve.py hangs the parks off these accounts.

Source
------
Census TIGERweb ArcGIS REST services, driven by config/gov_layers.yaml.  Three
registry entries: incorporated places (5 states), Pennsylvania active townships,
and counties (5 states) — roughly 5,553 accounts in total.

TIGERweb was chosen over the TIGER/Line shapefile downloads because it is
queryable, returns WGS84 GeoJSON, and works with the existing lib/arcgis.py
helpers — no shapefile reader and no new dependency.

Auth / License
--------------
None required.  Census TIGER data is U.S. Government public domain.

Output
------
Canonical 21-column rows into staging.<source_id>, plus polygon boundaries and
measured acreage into staging.gov_unit_boundary.  The boundaries are what make
the park -> government spatial rollup possible.

Usage
-----
Dry run (CSV only):
    python -m parks.gov_units --out-dir outputs/

Single source:
    python -m parks.gov_units --source tiger_counties --out outputs/counties.csv

All sources to DB:
    python -m parks.gov_units --write-db
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from lib import arcgis
from lib.db import SQM_PER_ACRE
from lib.enums import PARKS_TARGET_STATES
from lib.geo import STATE_FIPS_TO_ABBR
from lib.normalize import normalize_name
from lib.schema import build_canonical
from lib.validate import assert_columns_present, assert_fill_rate, assert_min_rows
from parks.config_loader import GovLayerConfig, load_gov_config

VERTICAL = "parks"

# Per-source floors.  Set well below the verified live counts (3,476 / 1,543 / 534)
# so ordinary annual TIGER revisions don't trip them, but high enough that a
# silently truncated or mis-filtered fetch does.
_MIN_EXPECTED_ROWS: dict[str, int] = {
    "tiger_places": 3000,
    "tiger_cousub": 1400,
    "tiger_counties": 500,
}
_DEFAULT_MIN_ROWS = 100


# ---------------------------------------------------------------- field helpers

def _build_out_fields(cfg: GovLayerConfig) -> str:
    """Build the outFields query string from the layer config."""
    fields: set[str] = {
        cfg.geoid_field,
        cfg.name_field,
        cfg.basename_field,
        cfg.state_fips_field,
        cfg.lat_field,
        cfg.lon_field,
        cfg.order_by_field,
    }
    if cfg.county_field:
        fields.add(cfg.county_field)
    if cfg.area_field:
        fields.add(cfg.area_field)
    return ",".join(sorted(f for f in fields if f))


# ---------------------------------------------------------------- extract

def _geometry_params(cfg: GovLayerConfig) -> dict[str, Any]:
    """
    Build the server-side geometry-reduction query parameters.

    Without these TIGERweb returns full-resolution boundaries — 250 counties is a
    30 MB response and the service intermittently 500s on it.  Reducing on the
    server is both faster and more reliable than fetching full resolution and
    simplifying locally.
    """
    params: dict[str, Any] = {}
    if cfg.geometry_precision is not None:
        params["geometryPrecision"] = cfg.geometry_precision
    if cfg.max_allowable_offset is not None:
        params["maxAllowableOffset"] = cfg.max_allowable_offset
    return params


def fetch(
    cfg: GovLayerConfig,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """
    Download all features for one registry entry, geometry included.

    Geometry is required, not optional: staging.gov_unit_boundary is what
    manager_resolve.py joins parks against, so a geometry-less fetch would leave
    every park unassignable.
    """
    rows: list[dict[str, Any]] = []
    for feature in arcgis.iter_features(
        base_url=cfg.url,
        where=cfg.where,
        out_fields=_build_out_fields(cfg),
        max_record_count=cfg.page_size,
        order_by=cfg.order_by_field,
        return_geometry=True,
        response_format=cfg.response_format,
        session=session,
        timeout=cfg.timeout,
        extra_params=_geometry_params(cfg),
    ):
        row: dict[str, Any] = dict(arcgis.feature_props(feature))
        row["_geometry"] = feature.get("geometry")
        row["source_file"] = cfg.url
        rows.append(row)

    return pd.DataFrame(rows)


# ---------------------------------------------------------------- checks

def assert_source_shape(df: pd.DataFrame, cfg: GovLayerConfig) -> None:
    """
    Raise ValueError if the fetched DataFrame doesn't match the expected shape.

    Guards against TIGERweb service reorganisations, a WHERE clause that silently
    stops matching, and truncated fetches that would produce a partial account
    spine — which is worse than none, because missing municipalities become parks
    misattributed to their county.
    """
    required = [cfg.geoid_field, cfg.name_field, cfg.state_fips_field]
    assert_columns_present(df, required, label=cfg.source_id)
    assert_min_rows(
        df,
        _MIN_EXPECTED_ROWS.get(cfg.source_id, _DEFAULT_MIN_ROWS),
        label=f"{cfg.source_id} fetch",
    )

    # GEOID is the Tier-1 match key for this vertical (plan §5.1) and the primary
    # key of every table it lands in, so it must be effectively complete.
    assert_fill_rate(df, cfg.geoid_field, 0.999, label=f"{cfg.source_id}: GEOID")

    # A geometry-less spine cannot do its job — fail loudly rather than produce
    # a table of accounts that no park can ever be attached to.
    if "_geometry" in df.columns:
        geom_fill = df["_geometry"].notna().mean()
        if geom_fill < 0.99:
            raise ValueError(
                f"{cfg.source_id}: only {geom_fill:.1%} of rows carry geometry "
                f"(need >=99%). The spatial park rollup requires boundaries."
            )


# ---------------------------------------------------------------- transform

def normalize(df: pd.DataFrame, cfg: GovLayerConfig) -> pd.DataFrame:
    """
    Add the derived columns to_canonical() and _boundary_rows() consume.

    Two mappings carry real decisions:

    name_normalized comes from BASENAME, not NAME.  NAME includes the LSAD suffix
    ("Cary town", "Wake County"); BASENAME is the bare place name ("Cary", "Wake").
    manager_resolve.py matches free-text manager strings like "City of Cary Parks,
    Recreation & Cultural Resources" against this column, and lib.normalize
    .normalize_name strips corporate suffixes but not civic ones — so matching
    against NAME would leave a stray "TOWN"/"CITY"/"COUNTY" token on one side of
    every comparison.  NAME is still kept as name_raw for display.

    latitude/longitude come from TIGER's INTPTLAT/INTPTLON "internal point", which
    is guaranteed to fall inside the polygon.  A bounding-box centre is not — for a
    coastal or horseshoe-shaped municipality it can land in the water or in the
    neighbouring town.  Values arrive sign-prefixed and zero-padded
    ("+34.1785440", "-082.3776868"); to_numeric handles both.
    """
    df = df.copy()

    df["state_abbr"] = (
        df[cfg.state_fips_field]
        .astype(str)
        .str.strip()
        .str.zfill(2)
        .map(STATE_FIPS_TO_ABBR)
    )

    df["name_display"] = df[cfg.name_field].astype(str).str.strip()
    basename = (
        df[cfg.basename_field]
        if cfg.basename_field in df.columns
        else df[cfg.name_field]
    )
    df["name_normalized"] = basename.map(normalize_name)

    df["latitude"] = pd.to_numeric(df[cfg.lat_field], errors="coerce")
    df["longitude"] = pd.to_numeric(df[cfg.lon_field], errors="coerce")

    # county_fips is only meaningful where a unit sits within exactly one county.
    # For incorporated places it is deliberately null (config sets county_field:
    # null) because a place may straddle county lines.
    if cfg.county_field and cfg.county_field in df.columns:
        df["county_fips"] = (
            df[cfg.state_fips_field].astype(str).str.strip().str.zfill(2)
            + df[cfg.county_field].astype(str).str.strip().str.zfill(3)
        )
    else:
        df["county_fips"] = None

    if cfg.area_field and cfg.area_field in df.columns:
        area = pd.to_numeric(df[cfg.area_field], errors="coerce")
        if cfg.area_unit == "sqm":
            area = area / SQM_PER_ACRE
        elif cfg.area_unit == "sqft":
            area = area / 43_560.0
        df["size_value"] = area
        df["size_unit"] = area.where(area.notna()).map(
            lambda v: "acres" if pd.notna(v) else None
        )
        # This is the unit's total LAND area, not maintained turf — it is a size
        # band for the account, never a mowable-acreage figure.  The sellable
        # acreage is SUM(child park acres), computed in parks_merge.py.
        df["size_metric"] = df["size_unit"].map(
            lambda v: "land_area_acres" if v else None
        )
    else:
        df["size_value"] = None
        df["size_unit"] = None
        df["size_metric"] = None

    return df


def filter_to_target_states(df: pd.DataFrame, cfg: GovLayerConfig) -> pd.DataFrame:
    """
    Drop out-of-scope states (D10).

    The WHERE clause already filters server-side by FIPS, so this is a
    belt-and-braces check.  D10 requires the isin() filter to run client-side
    before any DB write regardless, because a hand-edited WHERE clause in the
    registry is exactly the kind of change that silently widens scope.
    """
    before = len(df)
    out = df[df["state_abbr"].isin(PARKS_TARGET_STATES)].copy()
    dropped = before - len(out)
    if dropped:
        sys.stderr.write(
            f"  {cfg.source_id}: filtered {dropped:,} out-of-scope state rows "
            f"({before:,} -> {len(out):,})\n"
        )
    return out


# ---------------------------------------------------------------- quality

def report_quality(df: pd.DataFrame, cfg: GovLayerConfig) -> None:
    """Write data-quality metrics to stderr."""
    total = len(df)
    sys.stderr.write(f"  {cfg.source_id}: {total:,} total records\n")

    for col, label in (
        ("latitude", "non-null latitude"),
        ("name_normalized", "non-empty name_normalized"),
        ("county_fips", "non-null county_fips"),
        ("size_value", "non-null size_value"),
    ):
        if col not in df.columns:
            continue
        if col == "name_normalized":
            pct = (df[col].fillna("").astype(str).str.strip() != "").mean()
        else:
            pct = df[col].notna().mean()
        sys.stderr.write(f"  {cfg.source_id}: {label:<28} {pct:.1%}\n")

    if "state_abbr" in df.columns and total:
        sys.stderr.write(f"  {cfg.source_id}: per-state breakdown\n")
        for state, count in df["state_abbr"].value_counts().sort_index().items():
            sys.stderr.write(f"    {str(state):<6} {count:>7,}\n")


# ---------------------------------------------------------------- canonical output

def to_canonical(df: pd.DataFrame, cfg: GovLayerConfig) -> pd.DataFrame:
    """Map normalized government-unit columns to the canonical 21-column shape."""
    return build_canonical(
        df.index,
        source_id=cfg.source_id,
        natural_key=df[cfg.geoid_field].astype(str).str.strip(),
        vertical=VERTICAL,
        account_type=cfg.account_type,
        name_raw=df["name_display"],
        name_normalized=df["name_normalized"],
        state=df["state_abbr"],
        latitude=df["latitude"],
        longitude=df["longitude"],
        county_fips=df["county_fips"],
        size_metric=df["size_metric"],
        size_value=df["size_value"],
        size_unit=df["size_unit"],
        source_file=df["source_file"],
    )


# ---------------------------------------------------------------- boundaries

def _boundary_rows(df: pd.DataFrame, cfg: GovLayerConfig) -> list[dict]:
    """
    Build the staging.gov_unit_boundary payload for upsert_boundaries().

    area_acres is passed through from TIGER's AREALAND attribute (already
    converted to acres in normalize()) rather than measured from the geometry.
    AREALAND is the Census's own authoritative full-resolution land area, so it is
    both more accurate than anything we could compute and unaffected by the
    server-side generalization applied to the stored boundary.

    This is the reverse of the parks case: a park source's published acreage is
    exactly what we want to *check*, so park_attrs.acres_computed is measured from
    geometry. Here there is nothing to check — AREALAND is definitive.
    """
    rows: list[dict] = []
    for _, row in df.iterrows():
        geom = row.get("_geometry")
        acres = row.get("size_value")
        rows.append({
            "source_id": cfg.source_id,
            "natural_key": str(row[cfg.geoid_field]).strip(),
            "geojson": json.dumps(geom) if geom else None,
            "area_acres": float(acres) if pd.notna(acres) else None,
        })
    return rows


# ---------------------------------------------------------------- provenance

def build_raw_bytes(raw: pd.DataFrame) -> bytes:
    """
    Build the deterministic raw payload that gets SHA-256'd and landed in GCS (D7).

    Full coordinate arrays are replaced by a per-feature SHA-256 of the geometry
    rather than dropped. Carrying the real coordinates would make this payload
    roughly 90 MB for tiger_places alone — enough to risk an OOM in a Cloud Run
    job while building the string. Dropping geometry outright (as park_layers.py
    does today) is worse: geometry is the entire reason this connector exists, and
    a boundary revision with unchanged attributes would produce an identical run
    hash and read as "no change".

    Hashing each geometry preserves that drift detection at a few bytes per row.
    """
    records = []
    for _, row in raw.iterrows():
        rec = {
            k: v for k, v in row.items()
            if k != "_geometry" and not isinstance(v, (dict, list))
        }
        geom = row.get("_geometry")
        rec["_geom_sha256"] = (
            hashlib.sha256(
                json.dumps(geom, sort_keys=True).encode("utf-8")
            ).hexdigest()
            if geom else None
        )
        records.append(rec)
    return json.dumps(records, sort_keys=True, default=str).encode("utf-8")


# ---------------------------------------------------------------- entrypoint

def _run_one(
    cfg: GovLayerConfig,
    out_path: str,
    write_db: bool,
    session: requests.Session,
) -> None:
    """Fetch, validate, normalize, and write one registry entry."""
    sys.stderr.write(f"\n  {cfg.source_id}: fetching {cfg.url}\n")
    raw = fetch(cfg, session=session)
    assert_source_shape(raw, cfg)

    normalized = filter_to_target_states(normalize(raw, cfg), cfg)
    report_quality(normalized, cfg)

    canonical = to_canonical(normalized, cfg)
    canonical.to_csv(out_path, index=False)
    sys.stderr.write(
        f"  {cfg.source_id}: wrote {len(canonical):,} records -> {out_path}\n"
    )

    if not write_db:
        return

    from lib.db import finish_source_run, get_engine, upsert_boundaries, upsert_staging
    from lib.gcs import raw_sha256, upload_raw
    from lib.http import get_secret

    if not get_secret("DATABASE_URL"):
        sys.exit(
            "ERROR: --write-db was given but DATABASE_URL is not set. "
            "Copy .env.example -> .env and fill it in."
        )

    raw_bytes = build_raw_bytes(raw)
    sha256_hex = raw_sha256(raw_bytes)
    byte_count = len(raw_bytes)
    sys.stderr.write(
        f"  {cfg.source_id}: sha256={sha256_hex[:16]}…  bytes={byte_count:,}\n"
    )

    run_date = datetime.date.today().isoformat()
    raw_uri = upload_raw(cfg.source_id, run_date, raw_bytes)
    engine = get_engine()
    source_run_id: int | None = None
    try:
        from lib.db import write_source_run
        source_run_id = write_source_run(
            engine,
            source_id=cfg.source_id,
            byte_count=byte_count,
            sha256=sha256_hex,
            connector_version="1.0",
            license_string="U.S. Census Bureau TIGER — public domain",
            raw_uri=raw_uri,
        )
        upsert_staging(engine, cfg.source_id, canonical)

        # extra_cols, not area_col: area_acres comes from AREALAND, not from
        # measuring the (generalized) geometry.  See _boundary_rows().
        n_bounds = upsert_boundaries(
            engine,
            "gov_unit_boundary",
            _boundary_rows(normalized, cfg),
            extra_cols=("area_acres",),
        )
        finish_source_run(
            engine, source_run_id, status="succeeded", row_count=len(canonical)
        )
        sys.stderr.write(
            f"  {cfg.source_id}: wrote {len(canonical):,} rows to "
            f"staging.{cfg.source_id} and {n_bounds:,} boundaries "
            f"(source_run_id={source_run_id})\n"
        )
    except Exception as exc:
        if source_run_id is not None:
            finish_source_run(engine, source_run_id, status="failed")
        sys.exit(f"ERROR: DB write failed for {cfg.source_id} — {exc}")


def main() -> None:
    """CLI entrypoint — harvest TIGER government units and optionally write to DB."""
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--source",
        default=None,
        metavar="SOURCE_ID",
        help="Run only this registry entry (e.g. tiger_counties). Default: run all.",
    )
    ap.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="Output CSV path. Only valid with --source; use --out-dir for all sources.",
    )
    ap.add_argument(
        "--out-dir",
        default=".",
        metavar="DIR",
        help="Directory for per-source CSVs, named <source_id>.csv (default: cwd).",
    )
    ap.add_argument(
        "--write-db",
        action="store_true",
        help="Also write to Postgres staging (requires DATABASE_URL). "
             "Off by default — the CSV is always written regardless.",
    )
    ap.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="Override path to gov_layers.yaml.",
    )
    args = ap.parse_args()

    if args.out and not args.source:
        ap.error("--out requires --source; use --out-dir when running all sources.")

    registry = load_gov_config(args.config)

    if args.source:
        if args.source not in registry:
            available = ", ".join(sorted(registry))
            sys.exit(f"ERROR: unknown source '{args.source}'. Available: {available}")
        entries = {args.source: registry[args.source]}
    else:
        entries = registry

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    for source_id, cfg in entries.items():
        out_path = args.out or str(out_dir / f"{source_id}.csv")
        _run_one(cfg, out_path=out_path, write_db=args.write_db, session=session)


if __name__ == "__main__":
    main()
