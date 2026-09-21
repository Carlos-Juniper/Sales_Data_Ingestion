"""
Parks layer config loader — reads park_layers.yaml and validates each entry.

Mirrors the load_layer_config / assert_config_shape pattern from
connectors/healthcare/parcel_acreage_enrich.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_REQUIRED_KEYS = {"url", "where", "states", "name_field", "account_type"}
_OPTIONAL_KEYS = {
    "state_field", "id_field", "owner_field", "manager_field",
    "area_field", "area_unit", "response_format", "order_by_field", "timeout",
    "address_field", "geometry_precision",
    "manager_field_role", "managing_agency", "managing_agency_slug",
}

# What manager_field actually contains.  Defaults to "classification" because that
# is the safe assumption: a classification code fed into the §5.4 manager-name
# join produces confident-looking garbage, whereas ignoring a real name merely
# falls back to the spatial join.
_MANAGER_FIELD_ROLES = {"name", "classification"}

_DEFAULT_CONFIG_PATH = Path(__file__).parent / "config" / "park_layers.yaml"


@dataclass
class LayerConfig:
    source_id: str
    url: str
    where: str
    states: list[str]
    name_field: str
    account_type: str
    state_field: str | None = None
    id_field: str | None = None
    owner_field: str | None = None
    manager_field: str | None = None
    area_field: str | None = None
    area_unit: str | None = None
    address_field: str | None = None
    response_format: str = "geojson"
    order_by_field: str = "OBJECTID"
    timeout: int = 30
    # Coordinate rounding, in decimal places.  6 dp is ~0.1 m — far below any
    # surveying tolerance, so acreage measured from the result is unaffected,
    # while the payload shrinks substantially.
    #
    # Deliberately NO maxAllowableOffset counterpart here, unlike GovLayerConfig:
    # park_attrs.acres_computed is measured off this geometry as an independent
    # check on each source's published acreage, so server-side generalization
    # would corrupt the very number it exists to verify.
    geometry_precision: int | None = 6

    # Whether manager_field names a governing body ("name") or is a type code
    # ("classification").  Only PAD-US's Loc_Mang is a name; TPWD's PropType and
    # NC's PK_TYPE are classifications and must never reach the name join.
    manager_field_role: str = "classification"

    # Set when every record in the source is managed by one declared agency, as is
    # true of all four state-park layers.  When present the park's account is that
    # agency and no manager resolution is attempted at all — a state park sits
    # inside some city's boundary but the city does not maintain it, so rolling it
    # up spatially would invent a contract that does not exist.
    managing_agency: str | None = None
    managing_agency_slug: str | None = None


def assert_config_shape(source_id: str, entry: dict[str, Any]) -> None:
    """Raise ValueError if a registry entry is missing required keys."""
    missing = _REQUIRED_KEYS - entry.keys()
    if missing:
        raise ValueError(
            f"park_layers.yaml entry '{source_id}' missing required keys: {sorted(missing)}"
        )
    if not entry.get("url", "").startswith("http"):
        raise ValueError(
            f"park_layers.yaml entry '{source_id}': url must be a valid HTTP(S) URL"
        )
    if not entry.get("states"):
        raise ValueError(
            f"park_layers.yaml entry '{source_id}': states list must not be empty"
        )
    role = entry.get("manager_field_role", "classification")
    if role not in _MANAGER_FIELD_ROLES:
        raise ValueError(
            f"park_layers.yaml entry '{source_id}': manager_field_role must be one of "
            f"{sorted(_MANAGER_FIELD_ROLES)}, got {role!r}"
        )
    if role == "name" and not entry.get("manager_field"):
        raise ValueError(
            f"park_layers.yaml entry '{source_id}': manager_field_role='name' "
            f"requires a manager_field"
        )
    if bool(entry.get("managing_agency")) != bool(entry.get("managing_agency_slug")):
        raise ValueError(
            f"park_layers.yaml entry '{source_id}': managing_agency and "
            f"managing_agency_slug must be set together"
        )


def load_layer_config(
    path: str | Path | None = None,
) -> dict[str, LayerConfig]:
    """
    Load and validate park_layers.yaml.

    Returns a dict keyed by source_id.  Raises ValueError on any structural
    problem so callers catch config errors before making network calls.
    """
    config_path = Path(path) if path else _DEFAULT_CONFIG_PATH
    with open(config_path, "r") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    result: dict[str, LayerConfig] = {}
    for source_id, entry in raw.items():
        assert_config_shape(source_id, entry)
        result[source_id] = LayerConfig(
            source_id=source_id,
            url=entry["url"],
            where=entry.get("where", "1=1"),
            states=list(entry["states"]),
            name_field=entry["name_field"],
            account_type=entry["account_type"],
            state_field=entry.get("state_field") or None,
            id_field=entry.get("id_field") or None,
            owner_field=entry.get("owner_field") or None,
            manager_field=entry.get("manager_field") or None,
            area_field=entry.get("area_field") or None,
            area_unit=entry.get("area_unit") or None,
            address_field=entry.get("address_field") or None,
            response_format=entry.get("response_format") or "geojson",
            order_by_field=entry.get("order_by_field") or "OBJECTID",
            timeout=int(entry.get("timeout") or 30),
            geometry_precision=(
                None if entry.get("geometry_precision", 6) is None
                else int(entry.get("geometry_precision", 6))
            ),
            manager_field_role=entry.get("manager_field_role") or "classification",
            managing_agency=entry.get("managing_agency") or None,
            managing_agency_slug=entry.get("managing_agency_slug") or None,
        )
    return result


# ---------------------------------------------------------------- government units

# TIGERweb field names are uniform across the three layers we harvest, so every
# field mapping below has a working default and a YAML entry only needs to declare
# url / where / states / account_type.  The defaults are still overridable because
# the Census occasionally reorganises a service.
_GOV_REQUIRED_KEYS = {"url", "where", "states", "account_type"}
_GOV_OPTIONAL_KEYS = {
    "name_field", "basename_field", "geoid_field", "state_fips_field",
    "county_field", "lat_field", "lon_field", "area_field", "area_unit",
    "response_format", "order_by_field", "page_size", "timeout",
    "geometry_precision", "max_allowable_offset",
}

_GOV_ACCOUNT_TYPES = {"municipality", "county"}

_DEFAULT_GOV_CONFIG_PATH = Path(__file__).parent / "config" / "gov_layers.yaml"


@dataclass
class GovLayerConfig:
    """
    One TIGERweb government-unit layer.

    Deliberately a sibling of LayerConfig rather than a reuse of it: the two share
    only url/where/states, and the fields that differ carry different *semantics*.
    Most importantly LayerConfig.state_field holds a 2-letter abbreviation that
    park_layers uses directly, whereas TIGER's STATE field is a 2-digit FIPS code
    that must go through lib.geo.STATE_FIPS_TO_ABBR first.  Sharing one dataclass
    would mean one field with two meanings.

    Coordinates come from INTPTLAT/INTPTLON (TIGER's published "internal point",
    guaranteed to fall inside the polygon) rather than from a geometry centroid —
    a bbox centre is not reliably inside a coastal or crescent-shaped boundary.
    """
    source_id: str
    url: str
    where: str
    states: list[str]
    account_type: str
    name_field: str = "NAME"
    basename_field: str = "BASENAME"
    geoid_field: str = "GEOID"
    state_fips_field: str = "STATE"
    county_field: str | None = None
    lat_field: str = "INTPTLAT"
    lon_field: str = "INTPTLON"
    area_field: str | None = "AREALAND"
    area_unit: str = "sqm"
    response_format: str = "geojson"
    order_by_field: str = "GEOID"
    page_size: int | None = None
    timeout: int = 90
    # Server-side geometry reduction.  TIGERweb serves full-resolution boundaries
    # and returns HTTP 500 on large geometry pages; 250 counties at full precision
    # is a 30 MB response.  geometryPrecision=6 (~0.1 m) plus a ~3 m
    # maxAllowableOffset brings that to 5.8 MB.
    #
    # Generalizing the stored boundary costs nothing here because acreage is taken
    # from TIGER's authoritative AREALAND attribute, never measured off this
    # geometry.  (parks.park_layers is the opposite case and must not generalize.)
    geometry_precision: int | None = 6
    max_allowable_offset: float | None = 3e-5


def assert_gov_config_shape(source_id: str, entry: dict[str, Any]) -> None:
    """Raise ValueError if a gov_layers.yaml entry is malformed."""
    missing = _GOV_REQUIRED_KEYS - entry.keys()
    if missing:
        raise ValueError(
            f"gov_layers.yaml entry '{source_id}' missing required keys: {sorted(missing)}"
        )
    unknown = entry.keys() - _GOV_REQUIRED_KEYS - _GOV_OPTIONAL_KEYS
    if unknown:
        raise ValueError(
            f"gov_layers.yaml entry '{source_id}' has unknown keys: {sorted(unknown)}"
        )
    if not str(entry.get("url", "")).startswith("http"):
        raise ValueError(
            f"gov_layers.yaml entry '{source_id}': url must be a valid HTTP(S) URL"
        )
    if not entry.get("states"):
        raise ValueError(
            f"gov_layers.yaml entry '{source_id}': states list must not be empty"
        )
    account_type = entry.get("account_type")
    if account_type not in _GOV_ACCOUNT_TYPES:
        raise ValueError(
            f"gov_layers.yaml entry '{source_id}': account_type must be one of "
            f"{sorted(_GOV_ACCOUNT_TYPES)}, got {account_type!r}"
        )
    area_unit = entry.get("area_unit", "sqm")
    if area_unit not in ("sqm", "acres", "sqft"):
        raise ValueError(
            f"gov_layers.yaml entry '{source_id}': area_unit must be sqm|acres|sqft, "
            f"got {area_unit!r}"
        )


def load_gov_config(
    path: str | Path | None = None,
) -> dict[str, GovLayerConfig]:
    """
    Load and validate gov_layers.yaml.

    Returns a dict keyed by source_id.  Raises ValueError on any structural
    problem so config errors surface before any network call is made.
    """
    config_path = Path(path) if path else _DEFAULT_GOV_CONFIG_PATH
    with open(config_path, "r") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    result: dict[str, GovLayerConfig] = {}
    for source_id, entry in raw.items():
        assert_gov_config_shape(source_id, entry)
        page_size = entry.get("page_size")
        result[source_id] = GovLayerConfig(
            source_id=source_id,
            url=entry["url"],
            where=entry.get("where", "1=1"),
            states=list(entry["states"]),
            account_type=entry["account_type"],
            name_field=entry.get("name_field") or "NAME",
            basename_field=entry.get("basename_field") or "BASENAME",
            geoid_field=entry.get("geoid_field") or "GEOID",
            state_fips_field=entry.get("state_fips_field") or "STATE",
            county_field=entry.get("county_field") or None,
            lat_field=entry.get("lat_field") or "INTPTLAT",
            lon_field=entry.get("lon_field") or "INTPTLON",
            # get(key, DEFAULT) not get(key) or DEFAULT: an absent key takes the
            # TIGER standard name, while an explicit `area_field: null` disables
            # area entirely.  Those are different intents.
            area_field=entry.get("area_field", "AREALAND") or None,
            area_unit=entry.get("area_unit") or "sqm",
            response_format=entry.get("response_format") or "geojson",
            order_by_field=entry.get("order_by_field") or "GEOID",
            page_size=int(page_size) if page_size else None,
            timeout=int(entry.get("timeout") or 90),
            geometry_precision=(
                None if entry.get("geometry_precision", 6) is None
                else int(entry.get("geometry_precision", 6))
            ),
            max_allowable_offset=(
                None if entry.get("max_allowable_offset", 3e-5) is None
                else float(entry.get("max_allowable_offset", 3e-5))
            ),
        )
    return result
