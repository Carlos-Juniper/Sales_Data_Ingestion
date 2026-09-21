"""
Parks vertical — generic multi-source ArcGIS connector.

Reads park_layers.yaml and executes one harvest loop per registry entry,
landing park polygons/points into staging.<source_id> tables.

Each source's field mapping is declared in the YAML registry; adding a new
state or layer requires only a new entry there, not new Python.

Polygon → point: uses bbox_centroid() — plain min/max over the GeoJSON ring
coordinates — to populate latitude/longitude without shapely/geopandas.

Usage
-----
Dry run (CSV only):
    python -m parks.park_layers --out /tmp/parks.csv

Single source:
    python -m parks.park_layers --source padus_parks --out /tmp/padus.csv

All sources to DB:
    python -m parks.park_layers --write-db
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from lib import arcgis
from lib.enums import PARKS_TARGET_STATES, SEGMENT_MUNICIPAL, SEGMENT_STATE
from lib.normalize import normalize_name
from lib.schema import build_canonical
from lib.validate import assert_columns_present, assert_fill_rate, assert_min_rows
from parks.config_loader import LayerConfig, load_layer_config

_CONFIG_PATH = Path(__file__).parent / "config" / "park_layers.yaml"
_MIN_EXPECTED_ROWS = 10


# ---------------------------------------------------------------- geometry

def bbox_centroid(geometry: dict | None) -> tuple[float | None, float | None]:
    """
    Return (lon, lat) bounding-box center from a GeoJSON geometry object.

    Handles Point, Polygon, and MultiPolygon.  Returns (None, None) when
    geometry is absent or unrecognised — callers must handle the null case.

    No shapely/geopandas dependency: computes min/max over the raw coordinate
    lists, consistent with the repo's geometry-via-raw-PostGIS-SQL policy.
    """
    if not geometry:
        return None, None

    geom_type = geometry.get("type", "")
    all_pairs: list[list[float]] = []

    if geom_type == "Point":
        coords = geometry.get("coordinates")
        if not coords:
            return None, None
        return float(coords[0]), float(coords[1])
    elif geom_type == "Polygon":
        for ring in geometry.get("coordinates", []):
            all_pairs.extend(ring)
    elif geom_type == "MultiPolygon":
        for polygon in geometry.get("coordinates", []):
            for ring in polygon:
                all_pairs.extend(ring)
    else:
        # ESRI JSON format (f=json): geometry has "rings"/"x"/"y" instead of GeoJSON type/coordinates
        if "x" in geometry and "y" in geometry:
            return float(geometry["x"]), float(geometry["y"])
        for ring in geometry.get("rings", []):
            all_pairs.extend(ring)

    if not all_pairs:
        return None, None

    lons = [p[0] for p in all_pairs]
    lats = [p[1] for p in all_pairs]
    return (min(lons) + max(lons)) / 2.0, (min(lats) + max(lats)) / 2.0



# ---------------------------------------------------------------- esri geometry

def _ring_signed_area(ring: list) -> float:
    """
    Shoelace signed area of a coordinate ring.

    Positive = counter-clockwise, negative = clockwise, in standard x/y
    orientation.  Used only to classify rings, so the units (square degrees) and
    the planar approximation are irrelevant — only the sign matters.
    """
    total = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _point_in_ring(point: list, ring: list) -> bool:
    """Ray-casting point-in-polygon test.  Boundary cases are not significant here."""
    x, y = point[0], point[1]
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y):
            if x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
        j = i
    return inside


def esri_rings_to_geojson(geometry: dict | None) -> dict | None:
    """
    Convert an ESRI JSON polygon ({"rings": [...]}) to a GeoJSON geometry.

    Needed because two registry entries must use response_format="json":
    fdep_state_parks (the FDEP MapServer returns HTTP 500 for f=geojson) and
    nc_state_parks (its FeatureServer advertises geojson support but returns zero
    features).  Both therefore hand back ESRI rings rather than GeoJSON.

    The conversion is not a relabelling.  ESRI packs every ring of a multi-part
    polygon into one flat list and distinguishes outer rings from holes purely by
    winding order — clockwise is an outer ring, counter-clockwise is a hole.
    GeoJSON instead nests each part as [outer, hole, hole, ...].  So each hole has
    to be matched back to the outer ring that contains it.  Treating the flat ring
    list as if it were GeoJSON coordinates would silently turn every hole into
    solid ground and inflate the measured acreage — which is precisely the number
    this vertical exists to get right.

    Holes are assigned to the smallest containing outer ring, which is correct for
    the nested-island case (a lake inside an island inside a park).

    Returns None when there is no usable geometry.
    """
    if not geometry:
        return None

    rings = geometry.get("rings")
    if not rings:
        return None

    # A closed ring needs at least 4 positions (3 distinct + repeated first).
    usable = [r for r in rings if r and len(r) >= 4]
    if not usable:
        return None

    outers: list[list] = []
    holes: list[list] = []
    for ring in usable:
        if _ring_signed_area(ring) < 0:
            outers.append(ring)
        else:
            holes.append(ring)

    # Defensive: some servers emit non-ESRI winding for single-part polygons, which
    # would classify every ring as a hole and yield no geometry at all.  Promote
    # the largest ring to outer rather than dropping the feature.
    if not outers:
        largest = max(usable, key=lambda r: abs(_ring_signed_area(r)))
        outers = [largest]
        holes = [r for r in usable if r is not largest]

    polygons: list[list[list]] = [[o] for o in outers]
    for hole in holes:
        probe = hole[0]
        candidates = [
            idx for idx, poly in enumerate(polygons)
            if _point_in_ring(probe, poly[0])
        ]
        if not candidates:
            # An unattributable hole is dropped rather than promoted to a solid
            # part: over-stating acreage is the worse error for a bid.
            continue
        best = min(candidates, key=lambda idx: abs(_ring_signed_area(polygons[idx][0])))
        polygons[best].append(hole)

    # Normalize winding to the GeoJSON right-hand rule (outer CCW, holes CW).
    # PostGIS keys off ring *order* rather than winding, so this is belt-and-braces
    # for any other consumer of these payloads.
    normalized: list[list[list]] = []
    for poly in polygons:
        shell = poly[0] if _ring_signed_area(poly[0]) > 0 else poly[0][::-1]
        inner = [
            (h if _ring_signed_area(h) < 0 else h[::-1])
            for h in poly[1:]
        ]
        normalized.append([shell, *inner])

    if len(normalized) == 1:
        return {"type": "Polygon", "coordinates": normalized[0]}
    return {"type": "MultiPolygon", "coordinates": normalized}


def to_geojson_geometry(geometry: dict | None) -> dict | None:
    """
    Normalize any supported geometry payload to a GeoJSON geometry dict.

    Accepts geometry already in GeoJSON form (the f=geojson sources) and ESRI JSON
    (the f=json sources), so callers never have to branch on response_format.
    """
    if not geometry:
        return None
    if geometry.get("type") and geometry.get("coordinates") is not None:
        return geometry
    if "rings" in geometry:
        return esri_rings_to_geojson(geometry)
    if "x" in geometry and "y" in geometry:
        # A point layer has no boundary to store.
        return None
    return None


# ---------------------------------------------------------------- field helpers

def _build_out_fields(cfg: LayerConfig) -> str:
    """Build the outFields query string from the layer config."""
    fields: set[str] = {cfg.order_by_field}
    for attr in ("id_field", "name_field", "owner_field", "manager_field",
                 "area_field", "state_field", "address_field"):
        val = getattr(cfg, attr)
        if val and val != cfg.order_by_field:
            fields.add(val)
    return ",".join(sorted(fields))


# ---------------------------------------------------------------- extract

def fetch(
    cfg: LayerConfig,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """
    Download all features for one registry entry.

    Geometry is requested (return_geometry=True) so that bbox_centroid() can
    derive lat/lon for polygon layers.  Point layers also benefit since
    arcgis.feature_lonlat() extracts coordinates from Point geometry directly.
    """
    out_fields = _build_out_fields(cfg)
    rows: list[dict[str, Any]] = []
    for feature in arcgis.iter_features(
        base_url=cfg.url,
        where=cfg.where,
        out_fields=out_fields,
        order_by=cfg.order_by_field,
        return_geometry=True,
        response_format=cfg.response_format,
        session=session,
        timeout=cfg.timeout,
        extra_params=(
            {"geometryPrecision": cfg.geometry_precision}
            if cfg.geometry_precision is not None else None
        ),
    ):
        props = arcgis.feature_props(feature)
        row: dict[str, Any] = dict(props)
        row["_geometry"] = feature.get("geometry")
        row["source_file"] = cfg.url
        rows.append(row)

    df = pd.DataFrame(rows)
    if "OBJECTID" in df.columns:
        df["OBJECTID"] = pd.to_numeric(df["OBJECTID"], errors="coerce")
    return df


# ---------------------------------------------------------------- checks

def assert_source_shape(df: pd.DataFrame, cfg: LayerConfig) -> None:
    """
    Raise ValueError if the fetched DataFrame does not match the expected shape.

    Guards against API layout changes, truncated fetches, and zero-row responses
    that would silently produce empty staging tables.
    """
    required = [cfg.name_field]
    if cfg.id_field and cfg.id_field != "OBJECTID":
        required.append(cfg.id_field)
    assert_columns_present(df, required, label=cfg.source_id)
    assert_min_rows(df, _MIN_EXPECTED_ROWS, label=f"{cfg.source_id} fetch")

    if cfg.id_field and cfg.id_field in df.columns:
        assert_fill_rate(
            df, cfg.id_field, 0.90,
            label=f"{cfg.source_id}: id_field fill rate",
        )


# ---------------------------------------------------------------- transform

def normalize(df: pd.DataFrame, cfg: LayerConfig) -> pd.DataFrame:
    """
    Add derived columns used by to_canonical().

    For polygon layers, lat/lon is derived from bbox_centroid().
    For point layers, arcgis.feature_lonlat() provides coordinates directly
    from the geometry — bbox_centroid() returns the same value for Point type.
    """
    df = df.copy()

    df["name_normalized"] = df[cfg.name_field].map(normalize_name)

    def _lonlat(row) -> tuple[float | None, float | None]:
        geom = row.get("_geometry")
        if geom:
            return bbox_centroid(geom)
        return None, None

    coords = df.apply(_lonlat, axis=1)
    df["longitude"] = coords.map(lambda c: c[0])
    df["latitude"] = coords.map(lambda c: c[1])

    df["zip5"] = None

    if cfg.area_field and cfg.area_field in df.columns:
        area = pd.to_numeric(df[cfg.area_field], errors="coerce")
        if cfg.area_unit == "sqm":
            area = area / 4046.856
            unit = "acres"
        else:
            unit = cfg.area_unit
        df["size_value"] = area
        df["size_metric"] = area.where(area.notna()).map(
            lambda v: unit if pd.notna(v) else None
        )
        df["size_unit"] = df["size_metric"]
    else:
        df["size_value"] = None
        df["size_metric"] = None
        df["size_unit"] = None

    if cfg.state_field and cfg.state_field in df.columns:
        df["_state"] = df[cfg.state_field]
    else:
        df["_state"] = cfg.states[0] if len(cfg.states) == 1 else None

    # Owner and manager strings were already being requested in _build_out_fields
    # but discarded after report_quality.  They are the input to the plan's §5.4
    # manager-string -> Census GEOID join ("City of Cary Parks, Recreation &
    # Cultural Resources" -> 3710740), which is how a park finds its account when
    # spatial containment is ambiguous.  Carry them through to staging.park_attrs.
    for attr, col in (("owner_field", "owner_raw"), ("manager_field", "manager_raw")):
        field = getattr(cfg, attr)
        if field and field in df.columns:
            df[col] = df[field].astype("string").str.strip()
        else:
            df[col] = pd.NA
    # Only populate manager_normalized when manager_field actually holds a
    # governing-body name.  TPWD's PropType and NC's PK_TYPE are classification
    # codes; normalizing them would hand manager_resolve.py a column full of
    # "STATE PARK" / "SP" that fuzzy-matches place names at plausible-looking
    # scores.  manager_raw is still kept for audit either way.
    if cfg.manager_field_role == "name":
        df["manager_normalized"] = df["manager_raw"].map(
            lambda v: normalize_name(v) if pd.notna(v) else None
        )
    else:
        df["manager_normalized"] = pd.NA

    # segment was previously always None, which left lib.enums.SEGMENT_STATE as
    # dead code.  It distinguishes a state-park account (one state agency, many
    # sites) from a municipal one (one city, few sites) — a different sales motion.
    df["segment"] = (
        SEGMENT_STATE if cfg.account_type == "state_park" else SEGMENT_MUNICIPAL
    )

    return df


# ---------------------------------------------------------------- quality

def report_quality(df: pd.DataFrame, cfg: LayerConfig) -> None:
    """Log data quality metrics to stderr."""
    total = len(df)
    sys.stderr.write(f"  {cfg.source_id}: {total:,} total records\n")

    lat_pct = df["latitude"].notna().mean() if "latitude" in df.columns else 0.0
    sys.stderr.write(f"  {cfg.source_id}: non-null latitude  {lat_pct:.1%}\n")

    if cfg.area_field and "size_value" in df.columns:
        area_pct = df["size_value"].notna().mean()
        sys.stderr.write(f"  {cfg.source_id}: non-null {cfg.area_field}  {area_pct:.1%}\n")

    if cfg.manager_field and cfg.manager_field in df.columns:
        sys.stderr.write(f"  {cfg.source_id}: {cfg.manager_field} breakdown (top 5)\n")
        for val, count in df[cfg.manager_field].value_counts().head(5).items():
            sys.stderr.write(f"    {str(val):<40}  {count:>5,}\n")



# ---------------------------------------------------------------- park attrs

def _park_attr_rows(df: pd.DataFrame, cfg: LayerConfig) -> list[dict]:
    """
    Build the staging.park_attrs payload for upsert_boundaries().

    Carries three things the canonical 21-column shape has nowhere to put:
    the park boundary, an independently measured acreage, and the owner/manager
    strings.

    acres_computed is measured by PostGIS from the unsimplified geometry (see
    lib.db.upsert_boundaries), while acres_published is the source's own
    pre-calculated figure.  Keeping both is the whole point: acreage is the field
    this vertical is bought on, and two independent numbers that agree are
    defensible in a way that one number never is.  parks_merge.py compares them to
    set acres_confidence, and a source whose two figures systematically disagree
    is showing a config bug (usually a wrong area_unit) rather than bad data.
    """
    natural_key = _natural_keys(df, cfg)
    rows: list[dict] = []
    for pos, (_, row) in enumerate(df.iterrows()):
        geom = to_geojson_geometry(row.get("_geometry"))
        acres = row.get("size_value")
        rows.append({
            "source_id": cfg.source_id,
            "natural_key": natural_key.iloc[pos],
            "geojson": json.dumps(geom) if geom else None,
            "owner_raw": _or_none(row.get("owner_raw")),
            "manager_raw": _or_none(row.get("manager_raw")),
            "manager_normalized": _or_none(row.get("manager_normalized")),
            "acres_published": float(acres) if pd.notna(acres) else None,
        })
    return rows


def _or_none(val):
    """Collapse NaN/NA/empty to None so Postgres gets NULL, not the string 'nan'."""
    if val is None or val is pd.NA:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    text = str(val).strip()
    return text or None


# ---------------------------------------------------------------- canonical output

def _to_key(v) -> str:
    """Coerce one id value to a stable string key, flattening float-typed ints."""
    if pd.isna(v) or v == "":
        return ""
    try:
        return str(int(float(v)))
    except (ValueError, TypeError):
        return str(v)


def _natural_keys(df: pd.DataFrame, cfg: LayerConfig) -> pd.Series:
    """
    Compute the natural_key series for one source.

    Extracted from to_canonical() so that _park_attr_rows() derives keys the exact
    same way.  staging.park_attrs joins back to staging.<source_id> on
    (source_id, natural_key), so two independent implementations drifting apart
    would silently orphan every park attribute row — the boundaries and acreage
    would land but never join to anything.
    """
    oid_col = cfg.order_by_field if cfg.order_by_field in df.columns else "OBJECTID"
    if cfg.id_field and cfg.id_field in df.columns:
        id_keys = df[cfg.id_field].map(_to_key)
        oid_keys = df[oid_col].map(
            lambda v: f"OID:{int(float(v))}" if pd.notna(v) else ""
        )
        return id_keys.where(id_keys != "", oid_keys)
    return df[oid_col].map(_to_key)


def to_canonical(df: pd.DataFrame, cfg: LayerConfig) -> pd.DataFrame:
    """Map normalized park columns to the standard canonical output shape."""
    natural_key = _natural_keys(df, cfg)

    return build_canonical(
        df.index,
        source_id=cfg.source_id,
        natural_key=natural_key,
        vertical="parks",
        account_type=cfg.account_type,
        name_raw=df[cfg.name_field],
        name_normalized=df["name_normalized"],
        address_line_1=df[cfg.address_field] if cfg.address_field and cfg.address_field in df.columns else None,
        city=None,
        state=df.get("_state"),
        zip5=df["zip5"],
        latitude=df["latitude"],
        longitude=df["longitude"],
        segment=df["segment"] if "segment" in df.columns else None,
        ein=None,
        county_fips=None,
        size_metric=df["size_metric"],
        size_value=df["size_value"],
        size_unit=df["size_unit"],
        source_file=df["source_file"],
    )



# ---------------------------------------------------------------- acreage check

def report_acreage_variance(engine, cfg: LayerConfig) -> dict:
    """
    Compare each source's published acreage against acreage measured from its own
    geometry, and report the spread to stderr.

    Run immediately after the write so a broken source is caught at harvest time
    rather than surfacing much later as a strange acres_confidence distribution.
    The two figures are independent, so systematic disagreement is diagnostic:

      - a near-constant ratio means the wrong area_unit in the registry
        (sqft vs sqm vs acres is a factor of 43,560 / 4,047 / 1)
      - a ratio near 1 with scattered outliers is normal: sources publish
        deed acreage while we measure the mapped polygon
      - measured >> published on a minority of rows points at ESRI ring
        misassignment turning holes into solid ground

    Returns the summary dict so callers and tests can assert on it.
    """
    from sqlalchemy import text

    sql = text("""
        SELECT
          count(*)                                          AS n_rows,
          count(acres_published)                            AS n_published,
          count(acres_computed)                             AS n_computed,
          count(*) FILTER (
            WHERE acres_published > 0 AND acres_computed > 0
              AND abs(acres_computed - acres_published) / acres_published > 0.10
          )                                                 AS n_variance_over_10pct,
          round(
            (percentile_cont(0.5) WITHIN GROUP (
              ORDER BY acres_computed / NULLIF(acres_published, 0)
            ))::numeric, 4
          )                                                 AS median_ratio
        FROM staging.park_attrs
        WHERE source_id = :source_id
    """)
    with engine.connect() as conn:
        row = conn.execute(sql, {"source_id": cfg.source_id}).mappings().one()

    summary = dict(row)
    sys.stderr.write(f"  {cfg.source_id}: acreage cross-check\n")
    sys.stderr.write(
        f"    published={summary['n_published']:,}/{summary['n_rows']:,}  "
        f"computed={summary['n_computed']:,}/{summary['n_rows']:,}\n"
    )
    median = summary.get("median_ratio")
    sys.stderr.write(
        f"    median computed/published ratio: "
        f"{median if median is not None else 'n/a'}\n"
    )
    sys.stderr.write(
        f"    rows disagreeing by >10%: {summary['n_variance_over_10pct']:,}\n"
    )
    if median is not None and not (0.5 <= float(median) <= 2.0):
        sys.stderr.write(
            f"    WARNING: median ratio {median} is far from 1.0 — check "
            f"area_unit for {cfg.source_id} in park_layers.yaml\n"
        )
    return summary


# ---------------------------------------------------------------- entrypoint

def _run_one(
    cfg: LayerConfig,
    out_path: str | None,
    write_db: bool,
    session: requests.Session,
) -> None:
    """Fetch, validate, normalize, and write one registry entry."""
    sys.stderr.write(f"\n  {cfg.source_id}: fetching {cfg.url}\n")
    raw = fetch(cfg, session=session)
    assert_source_shape(raw, cfg)

    normalized = normalize(raw, cfg)

    if cfg.state_field and "_state" in normalized.columns:
        before = len(normalized)
        normalized = normalized[normalized["_state"].isin(PARKS_TARGET_STATES)]
        dropped = before - len(normalized)
        if dropped:
            sys.stderr.write(
                f"  {cfg.source_id}: filtered {dropped:,} out-of-scope state rows\n"
            )

    report_quality(normalized, cfg)
    canonical = to_canonical(normalized, cfg)

    raw_bytes = json.dumps(
        raw.drop(columns=["_geometry"], errors="ignore").to_dict(orient="records"),
        sort_keys=True,
    ).encode("utf-8")

    csv_path = out_path or f"{cfg.source_id}.csv"
    canonical.to_csv(csv_path, index=False)
    sys.stderr.write(f"  {cfg.source_id}: wrote {len(canonical):,} records -> {csv_path}\n")

    if not write_db:
        return

    from lib.db import (
        finish_source_run,
        get_engine,
        upsert_boundaries,
        upsert_staging,
        write_source_run,
    )
    from lib.gcs import raw_sha256, upload_raw
    from lib.http import get_secret

    if not get_secret("DATABASE_URL"):
        sys.exit(
            "ERROR: --write-db was given but DATABASE_URL is not set. "
            "Copy .env.example -> .env and fill it in."
        )

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
        source_run_id = write_source_run(
            engine,
            source_id=cfg.source_id,
            byte_count=byte_count,
            sha256=sha256_hex,
            connector_version="1.0",
            license_string="Public-domain state/federal GIS data",
            raw_uri=raw_uri,
        )
        upsert_staging(engine, cfg.source_id, canonical)

        # Boundary + acreage + manager strings.  area_col (not extra_cols) because
        # acres_computed must be MEASURED from the geometry — it is the independent
        # check on acres_published, which is passed through as an extra column.
        n_attrs = upsert_boundaries(
            engine,
            "park_attrs",
            _park_attr_rows(normalized, cfg),
            area_col="acres_computed",
            extra_cols=(
                "owner_raw", "manager_raw", "manager_normalized", "acres_published",
            ),
        )
        finish_source_run(engine, source_run_id, status="succeeded", row_count=len(canonical))
        sys.stderr.write(
            f"  {cfg.source_id}: wrote {len(canonical):,} rows "
            f"to staging.{cfg.source_id} and {n_attrs:,} park_attrs rows "
            f"(source_run_id={source_run_id})\n"
        )
        report_acreage_variance(engine, cfg)
    except Exception as exc:
        if source_run_id is not None:
            finish_source_run(engine, source_run_id, status="failed")
        sys.exit(f"ERROR: DB write failed for {cfg.source_id} — {exc}")


def main() -> None:
    """CLI entrypoint — harvest park layers and optionally write to DB."""
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--source",
        default=None,
        metavar="SOURCE_ID",
        help="Run only this registry entry (e.g. padus_parks). Default: run all.",
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
        help="Also write results to Postgres staging (requires DATABASE_URL).",
    )
    ap.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="Override path to park_layers.yaml.",
    )
    args = ap.parse_args()

    if args.out and not args.source:
        ap.error("--out requires --source; use --out-dir when running all sources.")

    registry = load_layer_config(args.config)

    if args.source:
        if args.source not in registry:
            available = ", ".join(sorted(registry.keys()))
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
