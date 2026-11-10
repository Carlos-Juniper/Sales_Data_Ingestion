"""
FL code-009 common-area parcels (F6 owner via F7 statewide cadastral).

Source : Florida Statewide Cadastral (DOR NAL joined to county parcel polygons),
         ArcGIS FeatureServer listed in connectors/healthcare/config/parcel_layers.yaml.
License: Florida public records, Ch. 119 F.S. — free to store and use.

DOR use code 009 is "residential common elements / areas": the land an HOA or
condo association owns (ponds, entrances, amenity tracts).  The owner name
and owner mailing address are the strongest association signal we have and
the `C/O <firm>` line is the parcel -> management-firm link (§4).

Profiled 2026-11-09 for Hillsborough (CO_NO 39), docs/hoa-pipeline.md §6 step 3:
10,753 rows for 7,061 distinct PARCEL_IDs, 2,457 owner names.

TRAPS
-----
* The layer repeats a parcel once per polygon piece.  Dedupe on PARCEL_ID,
  keeping the largest piece's geometry for the centroid.
* Offset paging returns HTTP 400 past ~8,000 rows on this layer, so fetch
  returnIdsOnly first and then POST 500 objectIds at a time.  The id query
  is slow and flaky (see county_ids); it is retried.
* OWN_NAME is cut at 30 characters (`…HOMEOWNERS ASSO`).  owner_class() matches
  a truncated last token against keyword prefixes, and the merge passes
  truncated=True to the matcher.
* Association is tested before government / utility: `…WATER…` and
  `…HILLSBOROUGH CO…` in an association name were false hits the other way.

natural_key = '<CO_NO>:<PARCEL_ID>', segment = owner class.

Usage:
    python -m hoa.fl_common_area_parcels --county 39 --out fl_parcels.csv [--write-db]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable, Iterable, Iterator

import pandas as pd

from hoa.fl_load import clean, drop_empty, write_staging
from hoa.fl_match import parse_care_of
from lib.http import make_session
from lib.normalize import normalize_name
from lib.schema import build_canonical

SOURCE_ID = "fl_common_area_parcel"
LAYER_URL = ("https://services9.arcgis.com/Gh9awoU677aKree0/arcgis/rest/services/"
             "Florida_Statewide_Cadastral/FeatureServer/0")
OUT_FIELDS = ("OBJECTID,CO_NO,PARCEL_ID,DOR_UC,OWN_NAME,OWN_ADDR1,OWN_ADDR2,OWN_CITY,OWN_STATE,"
              "OWN_ZIPCD,PHY_ADDR1,PHY_CITY,PHY_ZIPCD,LND_SQFOOT")
ID_CHUNK = 500
OWN_NAME_LIMIT = 30

# ------------------------------------------------------------------ owner class

_ASSOC = re.compile(
    r"\bASS(N|OC|OCIATION|OCIATI|OCIAT|OCIA|OCI|O)?\b|\bASSN\b|\bH\s?O\s?A\b|\bPOA\b|\bCOA\b|HOMEOWNER|"
    r"\bOWNERS\b|\bCONDO|COMMUNITY|\bMASTER\b|NEIGHBORHOOD|\bCOMM AS|\bHOME ?OWN|PROP(ERTY)? OWN|"
    r"\bCOUNCIL\b|\bCOOP\b|\bVILLAGE ASS|\bH ?O\b|\bCOND\b|TOWNHOMES? (HOM|ASS|OWN)|PROPERTY O(W|$)")
_CDD = re.compile(r"\bCDD\b|COMMUNITY DEV|DEVELOPMENT DIST|\bDIST(RICT)?\b")
_COMPANY_HARD = re.compile(r"\bLLC\b|\bL L C\b|\bLTD\b|\bL ?P\b|\bTRUST(EE)?\b|\bTR\b|\bBANK\b|"
                           r"APARTMENTS? L|\bCORP")
_GOV = re.compile(r"COUNTY|CITY OF|STATE OF|BOARD OF|SCHOOL|PORT AUTHORITY|AUTHORITY|SWFWMD|"
                  r"WATER MANAGEMENT|TRUSTEES|UNITED STATES|DEPT|DEPARTMENT|TOWN OF")
# WATER only as a utility phrase: WATERFORD, WATERSIDE and BAYSWATER are
# subdivisions.
_UTIL = re.compile(r"TECO|TAMPA ELECTRIC|DUKE ENERGY|UTILIT|\bWATER (WORKS|SYSTEMS?|AUTHORITY|COMPANY|CO)\b|"
                   r"\bSEWER|RAILROAD|CSX|PEOPLES GAS|FRONTIER|VERIZON|\bPOWER\b")
_COMPANY = re.compile(r"\bLLC\b|\bINC\b|\bLTD\b|\bLP\b|HOMES|BUILDERS|LENNAR|PULTE|HORTON|"
                      r"TAYLOR MORRISON|M/I|KB HOME|MERITAGE|NVR|RYAN|CORP|COMPANY|\bCO\b|PARTNERS|"
                      r"DEVELOP|INVEST|HOLDINGS|PROPERT|REALTY|VENTURE|TRUST|BANK")
_TRUNC_KEYWORDS = ("ASSOCIATION", "HOMEOWNERS", "CONDOMINIUM", "NEIGHBORHOOD", "MASTER", "COMMUNITY",
                   "TOWNHOMES", "TOWNHOUSE", "OWNERS", "PROPERTY", "VILLAS", "HOA")
_TRUNC_LEAD = ("H", "HOMEOWNERS", "HOME", "PROPERTY", "COMMUNITY", "CONDO", "OWNERS")

OWNER_CLASSES = ("association", "cdd", "company", "government", "utility", "other")


def _truncated_association(name: str) -> bool:
    """`MIRA LAGO WEST HOMEOWNERS ASSO` — a cut last token that starts a keyword."""
    tokens = name.replace("'", "").split()
    if len(name) < OWN_NAME_LIMIT - 2 or not tokens:
        return False
    last = tokens[-1]
    if not any(k.startswith(last) for k in _TRUNC_KEYWORDS):
        return False
    return len(last) >= 3 or (len(tokens) > 1 and tokens[-2] in _TRUNC_LEAD)


def owner_class(name: str | None) -> str:
    n = (name or "").strip().upper()
    if not n:
        return "other"
    if _COMPANY_HARD.search(n):
        return "company"
    if _CDD.search(n):
        return "cdd"
    if _ASSOC.search(n) or _truncated_association(n):
        return "association"
    if _GOV.search(n):
        return "government"
    if _UTIL.search(n):
        return "utility"
    if _COMPANY.search(n):
        return "company"
    return "other"


# ------------------------------------------------------------------ fetch

Poster = Callable[[dict], dict]


def _arcgis_poster(url: str = LAYER_URL, timeout: int = 90, attempts: int = 4,
                   wait: float = 20.0) -> Poster:
    """POST to /query; retry the server's timeout answer, which is a 400 or 504."""
    session = make_session()

    def post(params: dict) -> dict:
        for attempt in range(1, attempts + 1):
            resp = session.post(url.rstrip("/") + "/query", data=params, timeout=timeout)
            body = resp.json() if resp.ok else {"error": {"code": resp.status_code}}
            if "error" not in body:
                return body
            if attempt < attempts:
                print(f"  arcgis: {body['error'].get('code')} (attempt {attempt}), retrying",
                      file=sys.stderr)
                time.sleep(wait * attempt)
        raise RuntimeError(f"ArcGIS error after {attempts} attempts: {body['error']}")
    return post


def county_ids(co_no: int, post: Poster) -> list[int]:
    """OBJECTIDs of the county's code-009 rows.

    This statewide query is the slow call: uncached it runs ~55 s server side
    and often comes back as HTTP 400 "Unable to perform query" (retried).
    CO_NO must come first in the where clause; `DOR_UC='009' AND CO_NO=39`
    never succeeds.  Fetching by objectIds afterwards takes ~3 s per 500.
    """
    where = f"CO_NO={int(co_no)} AND DOR_UC='009'"
    return sorted(post({"f": "json", "where": where, "returnIdsOnly": "true"}).get("objectIds") or [])


def load_ids(path: str) -> list[int]:
    """Object ids saved from an earlier returnIdsOnly query (fallback for its timeouts)."""
    data = json.load(open(path))
    ids = data["objectIds"] if isinstance(data, dict) else data
    return sorted(int(i) for i in ids)


def fetch_features(ids: list[int], post: Poster, *, geometry: bool = True) -> Iterator[dict]:
    """GeoJSON features for the given OBJECTIDs, ID_CHUNK per POST (duplicates included)."""
    for i in range(0, len(ids), ID_CHUNK):
        body = post({
            "f": "geojson",
            "objectIds": ",".join(map(str, ids[i: i + ID_CHUNK])),
            "outFields": OUT_FIELDS,
            "returnGeometry": str(geometry).lower(),
            "outSR": "4326",
            "geometryPrecision": "6",
        })
        yield from body.get("features") or []


# ------------------------------------------------------------------ geometry

def _rings(geom: dict | None) -> list[list[list[float]]]:
    if not geom:
        return []
    if geom.get("type") == "Polygon":
        return geom["coordinates"][:1]
    if geom.get("type") == "MultiPolygon":
        return [poly[0] for poly in geom["coordinates"] if poly]
    return []


def _ring_area(ring: list[list[float]]) -> float:
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:]))) / 2


def centroid(geom: dict | None) -> tuple[float | None, float | None]:
    """Area-weighted centroid of the outer rings, (lon, lat).  No shapely in this repo."""
    ax = ay = at = 0.0
    for ring in _rings(geom):
        a = cx = cy = 0.0
        for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
            cross = x1 * y2 - x2 * y1
            a += cross
            cx += (x1 + x2) * cross
            cy += (y1 + y2) * cross
        if a:
            ax += cx / 3
            ay += cy / 3
            at += a
    if not at:
        pts = [p for r in _rings(geom) for p in r]
        if not pts:
            return None, None
        return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    return round(ax / at, 6), round(ay / at, 6)


def _geom_area(geom: dict | None) -> float:
    return sum(_ring_area(r) for r in _rings(geom))


def merge_geometries(geoms: Iterable[dict | None]) -> dict | None:
    """Several pieces of one parcel -> one MultiPolygon (or the single Polygon)."""
    polys = []
    for g in geoms:
        if not g:
            continue
        if g.get("type") == "Polygon":
            polys.append(g["coordinates"])
        elif g.get("type") == "MultiPolygon":
            polys.extend(g["coordinates"])
    if not polys:
        return None
    if len(polys) == 1:
        return {"type": "Polygon", "coordinates": polys[0]}
    return {"type": "MultiPolygon", "coordinates": polys}


# ------------------------------------------------------------------ frame

def _zip5(v) -> str | None:
    m = re.match(r"\s*(\d{5})", str(v or ""))
    return m.group(1) if m else None


def dedupe(features: Iterable[dict]) -> dict[str, dict]:
    """PARCEL_ID -> {'props': props of the largest piece, 'geometry': merged pieces}."""
    pieces: dict[str, list[dict]] = {}
    for f in features:
        props = f.get("properties") or f.get("attributes") or {}
        pid = clean(props.get("PARCEL_ID"))
        if pid:
            pieces.setdefault(f"{int(props['CO_NO'])}:{pid}", []).append(f)
    out = {}
    for key, fs in pieces.items():
        biggest = max(fs, key=lambda f: _geom_area(f.get("geometry")))
        unique = {json.dumps(f.get("geometry"), sort_keys=True): f.get("geometry") for f in fs}
        out[key] = {"props": biggest.get("properties") or biggest.get("attributes") or {},
                    "geometry": merge_geometries(unique.values())}
    return out


def to_frame(parcels: dict[str, dict]) -> tuple[pd.DataFrame, dict[str, dict]]:
    rows, attrs = [], {}
    for key, rec in parcels.items():
        p, geom = rec["props"], rec["geometry"]
        owner = clean(p.get("OWN_NAME"))
        lon, lat = centroid(geom)
        cls = owner_class(owner)
        rows.append({
            "natural_key": key,
            "name_raw": owner,
            "name_normalized": normalize_name(owner) if owner else None,
            "address_line_1": clean(p.get("PHY_ADDR1")),
            "city": clean(p.get("PHY_CITY")),
            "zip5": _zip5(p.get("PHY_ZIPCD")),
            "latitude": lat,
            "longitude": lon,
            "segment": cls,
            "size_value": float(p["LND_SQFOOT"]) if p.get("LND_SQFOOT") else None,
        })
        attrs[key] = drop_empty({
            "county_no": int(p["CO_NO"]),
            "parcel_id": clean(p.get("PARCEL_ID")),
            "dor_uc": clean(p.get("DOR_UC")),
            "owner_class": cls,
            "owner_name_truncated": bool(owner and len(owner) >= OWN_NAME_LIMIT),
            "owner": drop_empty({
                "name": owner,
                "addr1": clean(p.get("OWN_ADDR1")),
                "addr2": clean(p.get("OWN_ADDR2")),
                "city": clean(p.get("OWN_CITY")),
                "state": clean(p.get("OWN_STATE")),
                "zip": _zip5(p.get("OWN_ZIPCD")),
            }),
            "care_of": parse_care_of(p.get("OWN_ADDR1"), p.get("OWN_ADDR2")),
            "land_sqft": p.get("LND_SQFOOT") or None,
            "boundary": geom,
        })
    return pd.DataFrame(rows), attrs


def assert_source_shape(df: pd.DataFrame) -> None:
    if df.empty:
        raise ValueError("no code-009 parcels — wrong county or layer changed")
    named = df["name_raw"].notna().mean()
    if named < 0.95:
        raise ValueError(f"OWN_NAME only {named:.1%} populated — layer changed")
    if df["natural_key"].duplicated().any():
        raise ValueError("parcel repeated after dedupe")


def to_canonical(df: pd.DataFrame) -> pd.DataFrame:
    return build_canonical(
        df.index,
        source_id=SOURCE_ID,
        natural_key=df["natural_key"],
        vertical="hoa",
        account_type="common_area_parcel",
        name_raw=df["name_raw"],
        name_normalized=df["name_normalized"],
        address_line_1=df["address_line_1"],
        city=df["city"],
        state="FL",
        zip5=df["zip5"],
        latitude=df["latitude"],
        longitude=df["longitude"],
        segment=df["segment"],
        size_metric="land_area",
        size_value=df["size_value"],
        size_unit="sqft",
        source_file="Florida_Statewide_Cadastral",
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--county", type=int, action="append", required=True,
                    help="DOR county number (CO_NO), e.g. 39 = Hillsborough; repeatable")
    ap.add_argument("--ids-file", help="JSON {'objectIds': [...]} saved from an earlier id "
                    "query; skips the slow where-query (one county only)")
    ap.add_argument("--out", default="fl_common_area_parcels.csv")
    ap.add_argument("--write-db", action="store_true",
                    help="Also write to Postgres staging (requires DATABASE_URL).")
    args = ap.parse_args()

    post = _arcgis_poster()
    features: list[dict] = []
    if args.ids_file and len(args.county) != 1:
        ap.error("--ids-file takes exactly one --county")
    for co in args.county:
        ids = load_ids(args.ids_file) if args.ids_file else county_ids(co, post)
        print(f"  county {co}: {len(ids):,} code-009 rows", file=sys.stderr)
        features.extend(fetch_features(ids, post))
    parcels = dedupe(features)
    df, attrs = to_frame(parcels)
    assert_source_shape(df)
    canonical = to_canonical(df)
    canonical.to_csv(args.out, index=False)

    print(f"  {len(features):,} rows -> {len(df):,} parcels -> {args.out}", file=sys.stderr)
    for seg, n in df["segment"].value_counts().items():
        print(f"    {seg:<12} {n:>7,} ({n / len(df):.1%})", file=sys.stderr)
    co = sum(1 for a in attrs.values() if a.get("care_of"))
    print(f"  with C/O firm: {co:,} ({co / len(df):.1%})", file=sys.stderr)

    if args.write_db:
        raw = json.dumps({"type": "FeatureCollection", "features": features}).encode()
        write_staging(SOURCE_ID, canonical, attrs, raw)


if __name__ == "__main__":
    main()
