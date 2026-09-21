"""
Parcel acreage enrichment for healthcare locations.

Reads a CSV of hospital locations that already have latitude/longitude, then
performs a spatial point-in-polygon lookup against county/statewide parcel
layers to pull the recorded lot area. Output is a flat enrichment CSV that maps
natural_key -> maintained_acres + parcel boundary GeoJSON, ready to be joined
back to core.location.

Input CSV required columns
--------------------------
  natural_key   — stable location ID (CCN for CMS hospitals)
  site_state    — 2-letter postal code (FL | NC | TX | PA | SC)
  latitude      — WGS84 decimal degrees
  longitude     — WGS84 decimal degrees

Optional columns used when present
-----------------------------------
  county_fips   — 5-digit FIPS (required for TX / PA, which have no statewide layer)
  site_county   — human-readable county name (used in progress output only)

Output CSV columns
------------------
  natural_key, state, parcel_id, maintained_acres, acres_confidence,
  geometry_source, owner_name, parcel_count, boundary_geojson,
  lookup_status, lookup_note

lookup_status values
--------------------
  ok                — one parcel found; acres written
  ok_multi_parcel   — multiple parcels found; areas summed, geometries unioned
  not_found         — spatial lookup returned 0 features even with envelope fallback
  no_geometry       — input row has no latitude/longitude (geocode still pending)
  no_area_field     — parcel found but the area field is null or zero
  state_not_supported — SC (first pass is manual CSV) or unknown state code
  county_not_configured — TX/PA county not yet in the registry (add the URL to expand coverage)
  error             — unexpected HTTP or parsing exception

Acreage note
------------
maintained_acres is the TOTAL recorded lot area — it includes building footprints,
paved surfaces, and any impervious cover. It is not net landscapable area. Set
acres_confidence = 'estimated' everywhere until building-footprint subtraction is
added as a future enhancement.

State layer coverage
--------------------
  FL  — Florida Statewide Cadastral (DOR, all 67 counties), area field LND_SQFOOT (sq ft)
  NC  — NC OneMap Statewide Parcels (weekly refresh), area field CALC_ACRES
  TX  — No statewide layer; routes by county_fips to county CAD FeatureServices.
         Top-15 metro counties cover ~80% of TX hospital locations. Add county URLs
         to TX_COUNTY_LAYERS to expand coverage.
  PA  — No statewide layer; routes by county_fips to PASDA county endpoints.
         Top-10 metro counties cover ~75% of PA hospital locations. Add county URLs
         to PA_COUNTY_LAYERS to expand coverage.
  SC  — No ArcGIS layer available. Pull county assessor CSVs manually for the first
         pass; this connector emits state_not_supported for SC rows.

Usage
-----
    python parcel_acreage_enrich.py hospitals.csv --out parcel_enrichment.csv
    python parcel_acreage_enrich.py hospitals.csv --out fl_enrichment.csv --state FL
    python parcel_acreage_enrich.py hospitals.csv --out enrichment.csv --workers 4
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yaml
from sqlalchemy import text

# connectors/lib is a shared package one level up from this vertical folder.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.arcgis import make_session, spatial_point_lookup
from lib.enrich_runner import run_enrichment
from lib.http import get_secret
from lib.normalize import normalize_name

logger = logging.getLogger(__name__)

SOURCE_ID = "parcel_acreage_enrich"
SQFT_PER_ACRE = 43_560.0

# ---------------------------------------------------------------- layer registry

_DEFAULT_CONFIG_PATH = Path(__file__).parent / "config" / "parcel_layers.yaml"


@dataclass
class LayerConfig:
    url: str
    area_field: str
    area_unit: str          # "sqft" or "acres"
    parcel_id_field: str
    owner_field: str | None = None
    out_fields: str = field(init=False)

    def __post_init__(self) -> None:
        parts = [self.parcel_id_field, self.area_field]
        if self.owner_field:
            parts.append(self.owner_field)
        self.out_fields = ",".join(parts)

    def to_acres(self, raw_value: float) -> float:
        if self.area_unit == "sqft":
            return raw_value / SQFT_PER_ACRE
        return float(raw_value)


_REQUIRED_ENTRY_FIELDS = {"url", "area_field", "area_unit", "parcel_id_field"}
_VALID_AREA_UNITS = {"sqft", "acres"}


def assert_config_shape(raw: dict) -> None:
    """
    Fail loudly if the loaded YAML is missing expected top-level keys or if
    any individual entry is missing a required field or has an invalid area_unit.

    Mirrors the assert_source_shape() convention used in the reference connectors
    (fl_dbpr_lodging.py, tx_trec_hoa.py) so layout regressions surface at load
    time with a clear message rather than silently producing wrong acreage values.
    """
    assert isinstance(raw, dict), "parcel_layers.yaml must be a YAML mapping at the top level"

    missing_top = {"statewide", "county"} - set(raw.keys())
    assert not missing_top, (
        f"parcel_layers.yaml is missing top-level section(s): {missing_top}"
    )

    # Validate statewide entries.
    for state, cfg in (raw.get("statewide") or {}).items():
        _validate_entry(cfg, context=f"statewide.{state}")

    # Validate county entries (nested one level deeper).
    for state, counties in (raw.get("county") or {}).items():
        for fips, cfg in (counties or {}).items():
            _validate_entry(cfg, context=f"county.{state}.{fips}")


def _validate_entry(cfg: dict, context: str) -> None:
    """Validate a single layer entry dict, raising ValueError on any problem."""
    missing = _REQUIRED_ENTRY_FIELDS - set(cfg.keys())
    if missing:
        raise ValueError(
            f"Layer entry {context!r} is missing required field(s): {missing}. "
            f"Required fields are: {_REQUIRED_ENTRY_FIELDS}"
        )

    unit = cfg["area_unit"]
    if unit not in _VALID_AREA_UNITS:
        raise ValueError(
            f"Layer entry {context!r} has invalid area_unit={unit!r}. "
            f"Must be one of {_VALID_AREA_UNITS}. "
            f"LayerConfig.to_acres() would silently return the raw value without conversion."
        )


def load_layer_config(config_path: str | Path | None = None) -> dict:
    path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
    with open(path) as fh:
        raw = yaml.safe_load(fh)
    assert_config_shape(raw)
    return raw


def _build_layer_maps(
    raw: dict,
) -> tuple[dict[str, LayerConfig], dict[str, dict[str, LayerConfig]]]:
    """Parse the raw YAML dict into (STATE_LAYERS, county_layers_by_state)."""

    def _entry(d: dict) -> LayerConfig:
        return LayerConfig(
            url=d["url"],
            area_field=d["area_field"],
            area_unit=d["area_unit"],
            parcel_id_field=d["parcel_id_field"],
            owner_field=d.get("owner_field"),
        )

    statewide: dict[str, LayerConfig] = {
        state: _entry(cfg)
        for state, cfg in (raw.get("statewide") or {}).items()
    }
    county: dict[str, dict[str, LayerConfig]] = {
        state: {fips: _entry(cfg) for fips, cfg in counties.items()}
        for state, counties in (raw.get("county") or {}).items()
    }
    return statewide, county


# Layer registries are intentionally NOT populated at module level.
# load_layer_config() performs file I/O; doing it on import breaks tests and
# tool scripts that don't have the YAML present.  Call _build_layer_maps() once
# inside enrich() or main() and pass the results through as parameters.

# SC: no statewide ArcGIS layer. First pass is manual county assessor CSV.
# This connector emits state_not_supported for SC rows.
# When SC is added, add an SC section under "county:" in parcel_layers.yaml.


# ---------------------------------------------------------------- layer resolution


def get_layer_config(
    state: str,
    county_fips: str | None,
    state_layers: dict[str, LayerConfig],
    county_layers: dict[str, dict[str, LayerConfig]],
) -> LayerConfig | None:
    """
    Return the LayerConfig for a given state/county combination, or None when
    not yet configured (emits county_not_configured) or unsupported (SC).
    """
    if state in state_layers:
        return state_layers[state]
    county_map = county_layers.get(state)
    if county_map is not None:
        return county_map.get(county_fips or "")
    return None  # SC and any unknown state


# ---------------------------------------------------------------- per-row lookup


@dataclass
class EnrichmentResult:
    natural_key: str
    state: str
    parcel_id: str | None = None
    maintained_acres: float | None = None
    acres_confidence: str = "estimated"
    geometry_source: str = "parcel"
    owner_name: str | None = None
    parcel_count: int = 0
    boundary_geojson: str | None = None
    lookup_status: str = "error"
    lookup_note: str = ""


def _extract_property(feature: dict[str, Any], field_name: str) -> Any:
    """Pull a field from a GeoJSON feature, case-insensitively."""
    props = feature.get("properties") or {}
    if field_name in props:
        return props[field_name]
    field_lower = field_name.lower()
    for k, v in props.items():
        if k.lower() == field_lower:
            return v
    return None


def lookup_parcel(
    natural_key: str,
    state: str,
    lat: float | None,
    lon: float | None,
    county_fips: str | None,
    session: Any,
    state_layers: dict[str, LayerConfig],
    county_layers: dict[str, dict[str, LayerConfig]],
) -> EnrichmentResult:
    result = EnrichmentResult(natural_key=natural_key, state=state)

    if lat is None or lon is None:
        result.lookup_status = "no_geometry"
        result.lookup_note = "latitude/longitude missing; geocode this row first"
        return result

    if state == "SC":
        result.lookup_status = "state_not_supported"
        result.lookup_note = "SC has no statewide ArcGIS parcel layer; pull county assessor CSVs manually"
        return result

    config = get_layer_config(state, county_fips, state_layers, county_layers)
    if config is None:
        if state in county_layers:
            # State is known but this FIPS has no entry yet — add it to the YAML.
            result.lookup_status = "county_not_configured"
            result.lookup_note = (
                f"county_fips={county_fips!r} not in the {state} county layer registry; "
                f"add the URL under county.{state} in parcel_layers.yaml to enable this county"
            )
        else:
            result.lookup_status = "state_not_supported"
            result.lookup_note = f"state={state!r} is not in the layer registry"
        return result

    try:
        features = spatial_point_lookup(
            config.url,
            lon=lon,
            lat=lat,
            out_fields=config.out_fields,
            session=session,
        )
    except requests.RequestException as exc:
        result.lookup_status = "error"
        result.lookup_note = f"network: {exc}"
        return result
    except (ValueError, KeyError) as exc:
        result.lookup_status = "error"
        result.lookup_note = f"parse: {exc}"
        return result

    if not features:
        result.lookup_status = "not_found"
        result.lookup_note = "0 features returned even with 50m envelope fallback"
        return result

    # Extract area from each parcel and sum (handles multi-parcel campuses).
    total_acres = 0.0
    parcel_ids: list[str] = []
    owner_names: list[str] = []
    geometries: list[dict] = []
    area_found = False

    for feat in features:
        raw_area = _extract_property(feat, config.area_field)
        try:
            area_val = float(raw_area)
        except (TypeError, ValueError):
            area_val = None

        if area_val and area_val > 0:
            total_acres += config.to_acres(area_val)
            area_found = True

        pid = _extract_property(feat, config.parcel_id_field)
        if pid:
            parcel_ids.append(str(pid))

        if config.owner_field:
            owner = _extract_property(feat, config.owner_field)
            if owner:
                # normalize_name strips punctuation, corporate suffixes, and
                # collapses whitespace so deduplicated owner strings join cleanly
                # and match consistently across parcels.
                owner_names.append(normalize_name(str(owner)))

        geom = feat.get("geometry")
        if geom:
            geometries.append(geom)

    if not area_found:
        result.lookup_status = "no_area_field"
        result.lookup_note = (
            f"parcel(s) found but {config.area_field!r} is null or zero on all of them"
        )
        result.parcel_count = len(features)
        result.parcel_id = "; ".join(parcel_ids) or None
        return result

    result.parcel_count = len(features)
    result.parcel_id = "; ".join(parcel_ids) or None
    result.maintained_acres = round(total_acres, 4)
    result.owner_name = "; ".join(dict.fromkeys(owner_names)) or None  # deduplicate, preserve order

    if geometries:
        if len(geometries) == 1:
            result.boundary_geojson = json.dumps(geometries[0])
        else:
            # Store as a GeometryCollection so the boundary column receives a valid
            # GeoJSON object. PostGIS will union these when loading into the boundary
            # column via ST_Union().
            result.boundary_geojson = json.dumps({
                "type": "GeometryCollection",
                "geometries": geometries,
            })

    result.lookup_status = "ok_multi_parcel" if len(features) > 1 else "ok"
    return result


# ---------------------------------------------------------------- batch runner


def enrich(
    df: pd.DataFrame,
    workers: int = 1,
    state_filter: str | None = None,
    config_path: str | Path | None = None,
) -> pd.DataFrame:
    """
    Run parcel lookups for every row in df that has latitude/longitude.

    df must contain: natural_key, site_state, latitude, longitude.
    county_fips is used when present (required for TX / PA).

    Returns a DataFrame with one row per input row, columns matching
    EnrichmentResult fields.

    Parameters
    ----------
    config_path:
        Optional path to a parcel_layers YAML. Defaults to the bundled
        config/parcel_layers.yaml. The YAML is loaded exactly once per call,
        not per row.
    """
    if state_filter:
        df = df[df["site_state"].str.upper() == state_filter.upper()]
        print(f"  state filter: {state_filter} → {len(df):,} rows", file=sys.stderr)

    # Load layer config once here — not at module level — so the module can be
    # imported in tests and tool scripts without the YAML present.
    raw_config = load_layer_config(config_path)
    state_layers, county_layers = _build_layer_maps(raw_config)

    # One shared Session is intentional: requests.Session is thread-safe for
    # concurrent .get() calls because urllib3's connection pool is internally
    # locked. Sharing avoids spinning up a new pool per thread, which would
    # defeat connection reuse and overwhelm the target server's per-IP limits.
    session = make_session()

    def _lookup_row(row: Any) -> dict[str, Any]:
        lat = _to_float(row.get("latitude"))
        lon = _to_float(row.get("longitude"))
        fips = str(row.get("county_fips", "") or "").strip().zfill(5) or None

        res = lookup_parcel(
            natural_key=str(row["natural_key"]),
            state=str(row["site_state"]).upper(),
            lat=lat,
            lon=lon,
            county_fips=fips,
            session=session,
            state_layers=state_layers,
            county_layers=county_layers,
        )
        return res.__dict__

    indexed_rows: list[tuple[Any, Any]] = list(enumerate(df.to_dict("records")))

    pairs = run_enrichment(
        indexed_rows,
        _lookup_row,
        workers=workers,
        label="parcel",
    )

    # run_enrichment preserves original order; drop the index to get plain dicts.
    results = [result for _, result in pairs]
    return pd.DataFrame(results)


def _to_float(val: Any) -> float | None:
    try:
        f = float(val)
        return f if f == f else None  # NaN check
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- summary


def print_summary(df: pd.DataFrame) -> None:
    print("\n  status breakdown:", file=sys.stderr)
    for status, n in df["lookup_status"].value_counts().items():
        print(f"    {status:<28} {n:>7,}", file=sys.stderr)

    ok = df[df["lookup_status"].isin(["ok", "ok_multi_parcel"])]
    if not ok.empty:
        print(f"\n  acreage (ok rows only):", file=sys.stderr)
        print(f"    count    {len(ok):>7,}", file=sys.stderr)
        print(f"    median   {ok['maintained_acres'].median():>7.1f} acres", file=sys.stderr)
        print(f"    p25      {ok['maintained_acres'].quantile(0.25):>7.1f} acres", file=sys.stderr)
        print(f"    p75      {ok['maintained_acres'].quantile(0.75):>7.1f} acres", file=sys.stderr)
        print(f"    max      {ok['maintained_acres'].max():>7.1f} acres", file=sys.stderr)

    multi = df[df["lookup_status"] == "ok_multi_parcel"]
    if not multi.empty:
        print(f"\n  multi-parcel campuses: {len(multi):,}", file=sys.stderr)

    not_supported = df[df["lookup_status"] == "county_not_configured"]
    if not not_supported.empty:
        counties = not_supported.apply(
            lambda r: f"  {r['state']} fips={r.get('county_fips', '?')}", axis=1
        ).unique()
        print(f"\n  counties needing URL configuration ({len(counties)}):", file=sys.stderr)
        for c in sorted(counties)[:20]:
            print(f"    {c}", file=sys.stderr)


# ---------------------------------------------------------------- DB write helper


def upsert_enrich_parcel(engine, source_id: str, enriched: pd.DataFrame) -> int:
    """
    Upsert parcel enrichment results into staging.enrich_parcel.

    PK is (source_id, natural_key) — on conflict the maintained_acres and
    boundary columns are updated and enriched_at is bumped, making re-runs
    idempotent per D6.

    Only rows with lookup_status in ('ok', 'ok_multi_parcel') have an
    actual maintained_acres value; other rows land with NULL acres/boundary
    but still occupy a slot in the cache so they are not re-fetched.

    boundary is stored as a PostGIS MultiPolygon (SRID 4326).  The enricher
    produces raw GeoJSON (Polygon or GeometryCollection); we wrap it in
    ST_Multi() so the column type is always satisfied.  NULL boundary_geojson
    rows land with NULL boundary.

    Returns the number of rows written.
    """
    if enriched.empty:
        logger.warning(
            "upsert_enrich_parcel: empty DataFrame for source_id=%r — nothing written",
            source_id,
        )
        return 0

    upsert_sql = text("""
        INSERT INTO staging.enrich_parcel
            (source_id, natural_key, maintained_acres, boundary)
        VALUES (
            :source_id,
            :natural_key,
            CAST(:maintained_acres AS numeric),
            CASE
                WHEN :boundary_geojson IS NOT NULL
                THEN ST_Multi(
                    ST_SetSRID(
                        ST_GeomFromGeoJSON(:boundary_geojson),
                        4326
                    )
                )
                ELSE NULL
            END
        )
        ON CONFLICT (source_id, natural_key) DO UPDATE SET
            maintained_acres = EXCLUDED.maintained_acres,
            boundary         = EXCLUDED.boundary,
            enriched_at      = now()
    """)

    def _clean(v: Any) -> Any:
        """Coerce NaN → None so CAST(:x AS numeric) doesn't receive a Python float NaN."""
        try:
            if v is None:
                return None
            if isinstance(v, float) and math.isnan(v):
                return None
            return v
        except (TypeError, ValueError):
            return None

    rows = []
    for _, row in enriched.iterrows():
        rows.append({
            "source_id": source_id,
            "natural_key": str(row["natural_key"]),
            "maintained_acres": _clean(row.get("maintained_acres")),
            "boundary_geojson": row.get("boundary_geojson") or None,
        })

    with engine.begin() as conn:
        conn.execute(upsert_sql, rows)

    logger.info(
        "upsert_enrich_parcel: wrote %d rows to staging.enrich_parcel (source_id=%r)",
        len(rows),
        source_id,
    )
    return len(rows)


# ---------------------------------------------------------------- entrypoint


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="CSV of hospital locations (must have natural_key, site_state, latitude, longitude)")
    ap.add_argument("--out", default="parcel_enrichment.csv", help="output CSV path")
    ap.add_argument("--state", default=None, help="process only this state (e.g. FL)")
    ap.add_argument("--workers", type=int, default=4,
                    help="parallel HTTP workers (default 4; set 1 to serialize for debugging)")
    ap.add_argument("--config", default=None, metavar="YAML",
                    help="path to a parcel_layers YAML (default: config/parcel_layers.yaml)")
    ap.add_argument(
        "--write-db",
        action="store_true",
        help=(
            "Write parcel enrichment results to staging.enrich_parcel (requires DATABASE_URL). "
            "On conflict, updates maintained_acres and boundary and bumps enriched_at."
        ),
    )
    args = ap.parse_args()

    if args.config:
        print(f"using layer config: {args.config}", file=sys.stderr)

    # Validate DATABASE_URL before doing any expensive ArcGIS work.
    if args.write_db:
        if not get_secret("DATABASE_URL"):
            sys.exit(
                "ERROR: --write-db was given but DATABASE_URL is not set. "
                "Copy .env.example -> .env and fill it in."
            )

    print(f"reading {args.input}", file=sys.stderr)
    df = pd.read_csv(args.input, dtype=str, keep_default_na=False)

    required = {"natural_key", "site_state", "latitude", "longitude"}
    missing = required - set(df.columns)
    assert not missing, f"input CSV missing required column(s): {missing}"

    print(f"  {len(df):,} rows loaded", file=sys.stderr)

    print(f"\nrunning parcel lookups (workers={args.workers})", file=sys.stderr)
    results = enrich(df, workers=args.workers, state_filter=args.state, config_path=args.config)

    results.to_csv(args.out, index=False)
    print(f"\nwrote {len(results):,} rows -> {args.out}", file=sys.stderr)
    print_summary(results)

    if args.write_db:
        from lib.db import get_engine
        engine = get_engine()
        written = upsert_enrich_parcel(engine, SOURCE_ID, results)
        print(
            f"  parcel_acreage_enrich: upserted {written:,} rows to staging.enrich_parcel",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
