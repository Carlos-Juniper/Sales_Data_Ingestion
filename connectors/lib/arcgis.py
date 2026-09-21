"""
ArcGIS FeatureServer REST client.

Public layers require no token. For organization-restricted layers pass
token= to the relevant function.

Used by:
  - parcel_acreage_enrich.py  — spatial point-in-parcel lookups
  - future parks Hub harvester — bulk feature download with pagination

Key behaviors
-------------
- Reads maxRecordCount from layer metadata before paging; never hard-codes 1000.
- OBJECTID-ordered offset pagination with exceededTransferLimit as the
  loop-continuation signal. Falls back to returnIdsOnly + ID-chunked batch
  mode when supportsPagination is False.
- Point-in-polygon spatial lookup with automatic envelope fallback when the
  geocode lands on a road boundary and returns 0 features.
- Shared Session with retry on 429 and 5xx (3 attempts, exponential backoff).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Generator
from typing import Any

import requests

from lib.http import make_session

_QUERY_PATH = "/query"
_DEFAULT_TIMEOUT = 30

# Degrees per meter at the equator. Good enough for a 50m parcel-boundary buffer
# across the five target states (FL, NC, TX, PA, SC — all below 50°N).
_DEG_PER_METER = 1.0 / 111_320.0


def get_layer_info(
    base_url: str,
    session: requests.Session | None = None,
    token: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """
    Fetch layer metadata from the FeatureServer layer endpoint.

    Relevant keys in the response: maxRecordCount, advancedQueryCapabilities
    (supportsPagination), fields.
    """
    s = session or make_session()
    params: dict[str, Any] = {"f": "json"}
    if token:
        params["token"] = token
    resp = s.get(base_url.rstrip("/"), params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _raw_query(
    base_url: str,
    params: dict[str, Any],
    session: requests.Session,
    token: str | None,
    timeout: int,
) -> dict[str, Any]:
    p = dict(params)
    p.setdefault("f", "geojson")
    if token:
        p["token"] = token
    url = base_url.rstrip("/") + _QUERY_PATH
    resp = session.get(url, params=p, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def spatial_point_lookup(
    base_url: str,
    lon: float,
    lat: float,
    out_fields: str = "*",
    envelope_fallback_m: float = 50.0,
    session: requests.Session | None = None,
    token: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """
    Return all features whose polygon contains (lon, lat).

    When the point intersects 0 features — common when a geocode lands on a
    road centerline at a parcel boundary — falls back to an envelope (bounding
    box) query using envelope_fallback_m as the half-width buffer.

    Returns a list of GeoJSON feature dicts. An empty list means no parcel was
    found even with the envelope fallback.
    """
    s = session or make_session()
    delta = envelope_fallback_m * _DEG_PER_METER

    def _run(geom_type: str, geometry: str) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "geometry": geometry,
            "geometryType": geom_type,
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326",
            "outFields": out_fields,
            "returnGeometry": "true",
            "outSR": "4326",
        }
        result = _raw_query(base_url, params, s, token, timeout)
        return result.get("features", [])

    features = _run("esriGeometryPoint", f"{lon},{lat}")
    if features:
        return features

    envelope = f"{lon - delta},{lat - delta},{lon + delta},{lat + delta}"
    return _run("esriGeometryEnvelope", envelope)


# Query keys that iter_features owns outright.  extra_params may not set these:
# overriding orderByFields, resultOffset or resultRecordCount would corrupt
# pagination in ways that show up as silently missing or duplicated rows rather
# than as an error, which is the worst possible failure mode for an ingest.
_RESERVED_QUERY_PARAMS: frozenset[str] = frozenset({
    "f", "where", "outfields", "returngeometry", "outsr",
    "resultoffset", "resultrecordcount", "orderbyfields", "objectids",
    "returnidsonly",
})


def _sanitize_extra_params(
    extra_params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Drop any reserved key from *extra_params*, warning on each one dropped."""
    if not extra_params:
        return {}
    safe: dict[str, Any] = {}
    for key, value in extra_params.items():
        if key.lower() in _RESERVED_QUERY_PARAMS:
            print(
                f"  arcgis: ignoring reserved extra_params key {key!r} — "
                f"iter_features controls this parameter",
                file=sys.stderr,
            )
            continue
        safe[key] = value
    return safe


def iter_features(
    base_url: str,
    where: str = "1=1",
    out_fields: str = "*",
    max_record_count: int | None = None,
    order_by: str = "OBJECTID",
    return_geometry: bool = True,
    response_format: str = "geojson",
    session: requests.Session | None = None,
    token: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    extra_params: dict[str, Any] | None = None,
) -> Generator[dict[str, Any], None, None]:
    """
    Paginate through all features in a layer, yielding one feature dict at a time.

    Always sets orderByFields — unordered offset paging silently duplicates and
    drops rows when the server re-sorts between pages.

    When the layer reports supportsPagination=False, falls back to fetching all
    OBJECTIDs first and querying in ID-range chunks.

    response_format: "geojson" (default) or "json". Use "json" for services that
    claim geojson support but silently return 0 features — the result dicts use
    "attributes" instead of "properties", handled transparently by feature_props().

    extra_params: additional query parameters merged into every page request.
    Cannot override the pagination-critical keys (f, where, outFields,
    returnGeometry, outSR, resultOffset, resultRecordCount, orderByFields) —
    those are dropped with a warning, because silently overriding orderByFields
    or resultOffset would corrupt paging.

    The parameters this exists for are server-side geometry reduction:

        geometryPrecision   — decimal places to round coordinates to
        maxAllowableOffset  — server-side generalization, in outSR units

    Census TIGERweb serves full-resolution boundaries: 250 counties with geometry
    is a 30 MB response, and the service returns HTTP 500 on large pages. Setting
    geometryPrecision=6 with maxAllowableOffset=0.00003 (~3 m) brings the same
    page down to 5.8 MB. Reducing on the server is strictly better than fetching
    full resolution and simplifying afterwards.
    """
    s = session or make_session()

    if max_record_count is None:
        info = get_layer_info(base_url, session=s, token=token, timeout=timeout)
        max_record_count = info.get("maxRecordCount") or 1000
        supports_pagination = (
            info.get("advancedQueryCapabilities", {}).get("supportsPagination", True)
        )
    else:
        supports_pagination = True

    safe_extra = _sanitize_extra_params(extra_params)

    if supports_pagination:
        yield from _iter_offset(
            base_url, where, out_fields, max_record_count, order_by,
            return_geometry, response_format, s, token, timeout, safe_extra,
        )
    else:
        yield from _iter_by_ids(
            base_url, where, out_fields, max_record_count,
            return_geometry, response_format, s, token, timeout, safe_extra,
        )


def _iter_offset(
    base_url: str,
    where: str,
    out_fields: str,
    page_size: int,
    order_by: str,
    return_geometry: bool,
    response_format: str,
    session: requests.Session,
    token: str | None,
    timeout: int,
    extra_params: dict[str, Any] | None = None,
) -> Generator[dict[str, Any], None, None]:
    offset = 0
    while True:
        params: dict[str, Any] = {
            **(extra_params or {}),
            "f": response_format,
            "where": where,
            "outFields": out_fields,
            "returnGeometry": str(return_geometry).lower(),
            "outSR": "4326",
            "resultOffset": offset,
            "resultRecordCount": page_size,
            "orderByFields": order_by,
        }
        result = _raw_query(base_url, params, session, token, timeout)
        features = result.get("features", [])
        # Yield first so the caller receives every feature from this page before
        # we decide whether to continue.
        yield from features
        # exceededTransferLimit is an Esri-JSON-only field — a geojson response
        # (our default _raw_query format) never includes it, so it comes back
        # as None here rather than False. Treat None as "unknown" and fall back
        # to the page-was-full heuristic instead of silently stopping after
        # page 1, which was truncating every layer larger than one page.
        exceeded = result.get("exceededTransferLimit")
        if exceeded is None:
            exceeded = len(features) >= page_size
        if not exceeded:
            break
        # Advance offset by the number of features just received so the next
        # request starts exactly where this one left off.
        offset += len(features)
        # Guard against an infinite loop: a well-behaved server never sets
        # exceededTransferLimit=True with 0 features, but if it does the offset
        # would not advance and we would loop forever.  Break and warn instead.
        if not features:
            print(
                "  arcgis: exceededTransferLimit=true but 0 features returned; stopping",
                file=sys.stderr,
            )
            break


def _iter_by_ids(
    base_url: str,
    where: str,
    out_fields: str,
    chunk_size: int,
    return_geometry: bool,
    response_format: str,
    session: requests.Session,
    token: str | None,
    timeout: int,
    extra_params: dict[str, Any] | None = None,
) -> Generator[dict[str, Any], None, None]:
    """Fallback: fetch all OBJECTIDs first, then query in chunks."""
    id_result = _raw_query(
        base_url,
        {"where": where, "returnIdsOnly": "true"},
        session, token, timeout,
    )
    object_ids: list[int] = id_result.get("objectIds") or []
    print(f"  arcgis: pagination not supported; fetching {len(object_ids):,} IDs in chunks of {chunk_size}", file=sys.stderr)

    for i in range(0, len(object_ids), chunk_size):
        chunk = object_ids[i : i + chunk_size]
        id_filter = ",".join(str(oid) for oid in chunk)
        params: dict[str, Any] = {
            **(extra_params or {}),
            "f": response_format,
            "objectIds": id_filter,
            "outFields": out_fields,
            "returnGeometry": str(return_geometry).lower(),
            "outSR": "4326",
        }
        result = _raw_query(base_url, params, session, token, timeout)
        yield from result.get("features", [])


def feature_props(feature: dict) -> dict:
    return feature.get("properties") or feature.get("attributes") or {}


def feature_lonlat(feature: dict) -> tuple[float | None, float | None]:
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates")
    if coords and len(coords) >= 2:
        return float(coords[0]), float(coords[1])
    return None, None
