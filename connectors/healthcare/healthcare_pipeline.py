"""
Healthcare pipeline driver — reads staging tables, runs merge, writes resolved tables.

Reads from:
  staging.cms_general, staging.cms_nursing_home,
  staging.nppes_practice_locations, staging.va_facilities,
  staging.enrich_geocode, staging.enrich_parcel

Writes to (upsert on PK):
  staging.resolved_account   PK: account_key
  staging.resolved_location  PK: location_key
  staging.resolved_contact   PK: contact_key

The resolved tables are pipeline work-tables per D5: they are re-materialized
each run, and the 3-way diff against core.* (a separate agent) reads from them.

account_key — deterministic hash per D1:
    sha256( strongest external key among { ccn, npi, ein, normalized_name+zip5 } )
    encoded as hex. Recomputed each run so merging two clusters updates the key.
    Priority: ccn > npi > ein > name_normalized+zip5.

location_key — sha256( account_key + "|" + address_line_1_normalized + "|" + zip5 )
contact_key  — sha256( account_key + "|" + role + "|" + full_name_normalized )

Usage:
    PYTHONPATH=connectors python connectors/healthcare/healthcare_pipeline.py
    PYTHONPATH=connectors python connectors/healthcare/healthcare_pipeline.py --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import get_engine
from lib.http import get_secret
from lib.normalize import normalize_name, normalize_zip

from healthcare.healthcare_merge import merge_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("healthcare_pipeline")

# ---------------------------------------------------------------------------
# Healthcare source tables to read from staging
# ---------------------------------------------------------------------------

_HEALTHCARE_SOURCES = [
    "cms_general",
    "cms_nursing_home",
    "nppes_practice_locations",
    "va_facilities",
]

# Columns to SELECT from each staging source (must exist in staging schema).
_STAGING_COLS = [
    "source_id",
    "natural_key",
    "vertical",
    "account_type",
    "name_raw",
    "name_normalized",
    "address_line_1",
    "city",
    "state",
    "zip5",
    "phone_raw",
    "phone_normalized",
    "latitude",
    "longitude",
    "segment",
    "ein",
    "county_fips",
    "size_metric",
    "size_value",
    "size_unit",
    "source_file",
]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def _load_source(engine, source_id: str) -> pd.DataFrame:
    """Load one staging table into a DataFrame. Returns empty DataFrame if table missing."""
    col_list = ", ".join(_STAGING_COLS)
    sql = text(f"SELECT {col_list} FROM staging.{source_id}")  # nosec: source_id is a known constant
    try:
        with engine.connect() as conn:
            df = pd.read_sql(sql, conn)
        logger.info("loaded %d rows from staging.%s", len(df), source_id)
        return df
    except Exception as exc:
        logger.warning("staging.%s not readable (%s) — skipping", source_id, exc)
        return pd.DataFrame(columns=_STAGING_COLS)


def load_all_sources(engine) -> pd.DataFrame:
    """
    Read all healthcare staging tables and concatenate into one DataFrame.

    Adds columns required by the merge pipeline that may not be in every
    staging table: ccn, npi, phone, site_state.
    """
    frames = [_load_source(engine, src) for src in _HEALTHCARE_SOURCES]
    frames = [f for f in frames if not f.empty]

    if not frames:
        logger.warning("No healthcare staging data found — nothing to merge")
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)

    # Aliases required by healthcare_merge internals:
    # - phone: merge uses "phone", staging stores "phone_normalized"
    # - site_state: merge uses "site_state", staging stores "state"
    # - ccn / npi: not in staging schema; default to empty string
    df["phone"] = df.get("phone_normalized", pd.Series("", index=df.index)).fillna("")
    df["site_state"] = df.get("state", pd.Series("", index=df.index)).fillna("")
    if "ccn" not in df.columns:
        df["ccn"] = ""
    if "npi" not in df.columns:
        df["npi"] = ""

    logger.info("total rows across all healthcare sources: %d", len(df))
    return df


def load_geocode_cache(engine) -> pd.DataFrame:
    """
    Load staging.enrich_geocode for healthcare sources.

    Returns a DataFrame keyed on (source_id, natural_key) with lat/lon/precision
    columns so the merge driver can join geocode results onto the merged output.
    """
    sql = text("""
        SELECT source_id, natural_key, latitude, longitude, precision, source, match_type
        FROM staging.enrich_geocode
        WHERE source_id = ANY(:source_ids)
    """)
    try:
        with engine.connect() as conn:
            df = pd.read_sql(sql, conn, params={"source_ids": _HEALTHCARE_SOURCES})
        logger.info("loaded %d geocode cache rows", len(df))
        return df
    except Exception as exc:
        logger.warning("staging.enrich_geocode not readable (%s) — proceeding without geocodes", exc)
        return pd.DataFrame(columns=["source_id", "natural_key", "latitude", "longitude",
                                     "precision", "source", "match_type"])


def load_parcel_cache(engine) -> pd.DataFrame:
    """
    Load staging.enrich_parcel for healthcare sources.

    Returns a DataFrame keyed on natural_key with maintained_acres so the
    merge driver can join parcel results onto the resolved location output.

    staging.enrich_parcel has no acres_confidence or geometry_source columns —
    those are fixed constants supplied at join time ('estimated' and 'parcel'
    respectively, matching parcel_acreage_enrich.py's ParcelResult defaults).
    """
    sql = text("""
        SELECT source_id, natural_key, maintained_acres
        FROM staging.enrich_parcel
        WHERE source_id = ANY(:source_ids)
    """)
    try:
        with engine.connect() as conn:
            df = pd.read_sql(sql, conn, params={"source_ids": _HEALTHCARE_SOURCES})
        logger.info("loaded %d parcel cache rows", len(df))
        return df
    except Exception as exc:
        logger.warning("staging.enrich_parcel not readable (%s) — proceeding without parcel data", exc)
        return pd.DataFrame(columns=["source_id", "natural_key", "maintained_acres"])


# ---------------------------------------------------------------------------
# Deterministic key computation (D1)
# ---------------------------------------------------------------------------


def _sha256_key(text_val: str) -> str:
    """Return the hex SHA-256 digest of a UTF-8 string."""
    return hashlib.sha256(text_val.encode("utf-8")).hexdigest()


def _is_present(val) -> bool:
    """True only when val carries real data — not null, NaN, blank, or 'nan'."""
    if val is None:
        return False
    if isinstance(val, float) and math.isnan(val):
        return False
    s = str(val).strip()
    return s != "" and s.lower() != "nan"


def compute_account_key(row: dict) -> str:
    """
    Compute the deterministic account_key per D1.

    Priority: ccn > npi > ein > normalize(name)+zip5.
    The input row comes from the survivorship output so field names match the
    canonical column set.
    """
    # CCN — CMS Certification Number (strongest regulatory identifier)
    ccn = row.get("ccn", "")
    if _is_present(ccn):
        return _sha256_key(f"ccn:{str(ccn).strip()}")

    # NPI — National Provider Identifier
    npi = row.get("npi", "")
    if _is_present(npi):
        return _sha256_key(f"npi:{str(npi).strip()}")

    # EIN — Employer Identification Number
    ein = row.get("ein", "")
    if _is_present(ein):
        return _sha256_key(f"ein:{str(ein).strip()}")

    # Fallback: normalized name + zip5
    name_norm = normalize_name(str(row.get("name_normalized", "") or row.get("name_raw", "")))
    zip5 = normalize_zip(str(row.get("zip5", "")))
    return _sha256_key(f"name:{name_norm}|zip:{zip5}")


def compute_location_key(account_key: str, row: dict) -> str:
    """
    Compute the deterministic location_key.

    Derived from account_key + normalized address + zip5 so one account with
    multiple addresses produces distinct location rows.
    """
    addr = normalize_name(str(row.get("address_line_1", "")))
    zip5 = normalize_zip(str(row.get("zip5", "")))
    return _sha256_key(f"loc:{account_key}|{addr}|{zip5}")


def compute_contact_key(account_key: str, role: str, full_name: str) -> str:
    """
    Compute the deterministic contact_key.

    Derived from account_key + role + normalized name so the same contact
    at the same account in the same role is stable across runs.
    """
    norm_name = normalize_name(full_name)
    return _sha256_key(f"contact:{account_key}|{role}|{norm_name}")


# ---------------------------------------------------------------------------
# Enrichment join
# ---------------------------------------------------------------------------


def join_geocodes(merged: pd.DataFrame, geocodes: pd.DataFrame) -> pd.DataFrame:
    """
    Overlay geocode results onto the merged output.

    For rows that already have a latitude from VA (which provides coordinates
    directly), geocode values are left unchanged.  For others, we join from
    enrich_geocode on natural_key (the survivor's natural_key from merged_source_ids).

    Assumption: merged output has a 'natural_key' column carrying the survivor's
    own natural_key (from survivorship). In practice the survivor natural_key is
    the highest-priority source's key, which is what we join on.
    """
    if geocodes.empty or "natural_key" not in merged.columns:
        return merged

    geo_indexed = geocodes.set_index("natural_key")[
        ["latitude", "longitude", "precision", "source", "match_type"]
    ]

    merged = merged.copy()
    if "latitude" not in merged.columns:
        merged["latitude"] = None
    if "longitude" not in merged.columns:
        merged["longitude"] = None
    if "geocode_precision" not in merged.columns:
        merged["geocode_precision"] = None
    if "geocode_source" not in merged.columns:
        merged["geocode_source"] = None

    for idx, row in merged.iterrows():
        # Skip rows that already have coordinates (e.g. from VA direct lat/lon).
        if _is_present(row.get("latitude")):
            continue
        nk = str(row.get("natural_key", ""))
        if nk in geo_indexed.index:
            geo_row = geo_indexed.loc[nk]
            merged.at[idx, "latitude"] = geo_row["latitude"]
            merged.at[idx, "longitude"] = geo_row["longitude"]
            merged.at[idx, "geocode_precision"] = geo_row["precision"]
            merged.at[idx, "geocode_source"] = geo_row["source"]

    return merged


def join_parcel(location_df: pd.DataFrame, parcels: pd.DataFrame) -> pd.DataFrame:
    """
    Overlay parcel acreage results onto the resolved_location DataFrame.

    Joins staging.enrich_parcel onto location_df by the survivor row's own
    natural_key (carried as the private column _natural_key by
    build_resolved_location).  For matched rows, populates:
      - maintained_acres  : numeric acreage from the parcel record
      - acres_confidence  : fixed 'estimated' (no footprint subtraction yet)
      - geometry_source   : fixed 'parcel' (only when not already set by geocode)

    Rows with no parcel match are left with maintained_acres=None and
    acres_confidence=None (the build_resolved_location defaults).

    staging.enrich_parcel schema (migration 012):
      source_id, natural_key, maintained_acres, boundary, enriched_at
    acres_confidence and geometry_source are constants supplied here because
    they are not stored in the staging table (parcel_acreage_enrich.py's
    ParcelResult dataclass uses 'estimated' / 'parcel' as defaults).
    """
    if parcels.empty or "_natural_key" not in location_df.columns:
        return location_df

    # Index by natural_key for O(1) lookup — keep first match when a key
    # appears in multiple source rows (shouldn't happen in practice, but safe).
    parcel_indexed = parcels.drop_duplicates(subset=["natural_key"]).set_index("natural_key")[
        ["maintained_acres"]
    ]

    location_df = location_df.copy()
    # Ensure columns exist (they're added by build_resolved_location, but guard defensively).
    if "maintained_acres" not in location_df.columns:
        location_df["maintained_acres"] = None
    if "acres_confidence" not in location_df.columns:
        location_df["acres_confidence"] = None
    if "geometry_source" not in location_df.columns:
        location_df["geometry_source"] = None

    for idx, row in location_df.iterrows():
        # Use _natural_key, which build_resolved_location carries for this join.
        nk = str(row.get("_natural_key", ""))
        if nk in parcel_indexed.index:
            acres = parcel_indexed.loc[nk, "maintained_acres"]
            if _is_present(acres):
                location_df.at[idx, "maintained_acres"] = float(acres)
                location_df.at[idx, "acres_confidence"] = "estimated"
                # Only set geometry_source from parcel if not already populated
                # by a geocode join (geocode_source takes priority).
                if not _is_present(row.get("geometry_source")):
                    location_df.at[idx, "geometry_source"] = "parcel"

    matched = location_df["maintained_acres"].notna().sum()
    logger.info("join_parcel: %d / %d location rows matched parcel data", matched, len(location_df))
    return location_df


# ---------------------------------------------------------------------------
# Resolved table builders
# ---------------------------------------------------------------------------


def build_resolved_account(merged: pd.DataFrame) -> pd.DataFrame:
    """
    Build the resolved_account DataFrame from the survivorship output.

    Columns mirror staging.resolved_account (migration 011).
    account_key is computed per D1 for each cluster survivor row.
    """
    rows = []
    for _, row in merged.iterrows():
        account_key = compute_account_key(row.to_dict())

        # external_keys: collect whichever identifiers are present.
        ext: dict = {}
        if _is_present(row.get("ccn")):
            ext["ccn"] = str(row["ccn"]).strip()
        if _is_present(row.get("npi")):
            ext["npi"] = str(row["npi"]).strip()
        if _is_present(row.get("ein")):
            ext["ein"] = str(row["ein"]).strip()

        # mailing_address as JSON for the account (physical address of survivor).
        mailing: dict = {}
        for field in ["address_line_1", "city", "site_state", "zip5"]:
            val = row.get(field) or row.get("state") if field == "site_state" else row.get(field)
            if _is_present(val):
                mailing[field] = str(val)

        name_raw = str(row.get("name_raw", "")) if _is_present(row.get("name_raw")) else ""
        name_norm = str(row.get("name_normalized", "")) if _is_present(row.get("name_normalized")) else normalize_name(name_raw)

        size_metric_raw = row.get("size_value")
        size_metric = None
        if _is_present(size_metric_raw):
            try:
                size_metric = float(size_metric_raw)
            except (ValueError, TypeError):
                pass

        rows.append({
            "account_key": account_key,
            "vertical": str(row.get("vertical", "healthcare")),
            "account_type": str(row.get("account_type", "")) if _is_present(row.get("account_type")) else None,
            "legal_name": name_raw or name_norm,
            "name_normalized": name_norm,
            "dba_name": None,
            "parent_account_key": None,
            "mailing_address": json.dumps(mailing) if mailing else None,
            "phone": str(row.get("phone", "")) if _is_present(row.get("phone")) else None,
            "email": None,
            "website": None,
            "status": "active",
            "external_keys": json.dumps(ext) if ext else None,
            "size_metric": size_metric,
            "size_metric_unit": str(row.get("size_unit", "")) if _is_present(row.get("size_unit")) else None,
            "confidence": None,
            # Carry cluster_id for debugging; not a resolved_account column —
            # stored in a side-channel not written to DB.
            "_cluster_id": str(row.get("cluster_id", "")),
        })

    df = pd.DataFrame(rows)
    return df


def build_resolved_location(merged: pd.DataFrame, account_df: pd.DataFrame) -> pd.DataFrame:
    """
    Build the resolved_location DataFrame.

    One location per resolved account (the survivor's physical address + geocode).
    location_key is deterministic from account_key + address + zip5.
    """
    # Build a lookup from cluster_id → account_key.
    # account_df has _cluster_id carried from build_resolved_account.
    cluster_to_key = dict(zip(account_df["_cluster_id"], account_df["account_key"]))

    rows = []
    for _, row in merged.iterrows():
        cluster_id = str(row.get("cluster_id", ""))
        account_key = cluster_to_key.get(cluster_id)
        if not account_key:
            continue

        location_key = compute_location_key(account_key, row.to_dict())

        # site_address JSON.
        site_addr: dict = {}
        for field in ["address_line_1", "city", "zip5"]:
            val = row.get(field)
            if _is_present(val):
                site_addr[field] = str(val)
        state_val = row.get("site_state") or row.get("state")
        if _is_present(state_val):
            site_addr["state"] = str(state_val)

        lat = row.get("latitude")
        lon = row.get("longitude")

        rows.append({
            "location_key": location_key,
            "account_key": account_key,
            "vertical": "healthcare",
            "location_name": str(row.get("name_raw", "")) if _is_present(row.get("name_raw")) else None,
            "site_address": json.dumps(site_addr) if site_addr else None,
            # geom is computed by the DB UPSERT SQL using lat/lon.
            "_latitude": float(lat) if _is_present(lat) else None,
            "_longitude": float(lon) if _is_present(lon) else None,
            "geocode_precision": str(row.get("geocode_precision", "")) if _is_present(row.get("geocode_precision")) else None,
            "geometry_source": str(row.get("geocode_source", "")) if _is_present(row.get("geocode_source")) else None,
            "maintained_acres": None,
            "acres_confidence": None,
            "site_type": str(row.get("account_type", "")) if _is_present(row.get("account_type")) else None,
            # Carry natural_key as a private column so join_parcel() can match
            # against staging.enrich_parcel without a full merged-df re-scan.
            "_natural_key": str(row.get("natural_key", "")),
        })

    return pd.DataFrame(rows)


def build_resolved_contact(merged: pd.DataFrame, account_df: pd.DataFrame) -> pd.DataFrame:
    """
    Build the resolved_contact DataFrame.

    Healthcare sources generally don't supply named contact people, so this
    produces at most one contact per account when a phone number is present.
    The role is "primary_phone" — a structural placeholder that prevents the
    contact table from being empty and allows the 3-way diff to function.
    """
    cluster_to_key = dict(zip(account_df["_cluster_id"], account_df["account_key"]))

    rows = []
    for _, row in merged.iterrows():
        cluster_id = str(row.get("cluster_id", ""))
        account_key = cluster_to_key.get(cluster_id)
        if not account_key:
            continue

        phone = str(row.get("phone", "")) if _is_present(row.get("phone")) else None
        if not phone:
            continue

        role = "primary_phone"
        full_name = ""
        contact_key = compute_contact_key(account_key, role, full_name)

        rows.append({
            "contact_key": contact_key,
            "account_key": account_key,
            "vertical": "healthcare",
            "full_name": None,
            "role": role,
            "role_rank": 1,
            "phone": phone,
            "email": None,
            "address": None,
            "source_id": str(row.get("source_id", "")),
            "is_current": True,
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# DB write helpers
# ---------------------------------------------------------------------------


def upsert_resolved_account(engine, df: pd.DataFrame) -> int:
    """Upsert staging.resolved_account. Returns row count written."""
    if df.empty:
        return 0

    sql = text("""
        INSERT INTO staging.resolved_account (
            account_key, vertical, account_type, legal_name, name_normalized,
            dba_name, parent_account_key, mailing_address, phone, email,
            website, status, external_keys, size_metric, size_metric_unit, confidence
        ) VALUES (
            :account_key, :vertical, :account_type, :legal_name, :name_normalized,
            :dba_name, :parent_account_key,
            CAST(:mailing_address AS jsonb), :phone, :email,
            :website, :status,
            CAST(:external_keys AS jsonb),
            CAST(:size_metric AS numeric), :size_metric_unit,
            CAST(:confidence AS numeric)
        )
        ON CONFLICT (account_key) DO UPDATE SET
            vertical          = EXCLUDED.vertical,
            account_type      = EXCLUDED.account_type,
            legal_name        = EXCLUDED.legal_name,
            name_normalized   = EXCLUDED.name_normalized,
            dba_name          = EXCLUDED.dba_name,
            parent_account_key= EXCLUDED.parent_account_key,
            mailing_address   = EXCLUDED.mailing_address,
            phone             = EXCLUDED.phone,
            email             = EXCLUDED.email,
            website           = EXCLUDED.website,
            status            = EXCLUDED.status,
            external_keys     = EXCLUDED.external_keys,
            size_metric       = EXCLUDED.size_metric,
            size_metric_unit  = EXCLUDED.size_metric_unit,
            confidence        = EXCLUDED.confidence
    """)

    rows = df.drop(columns=["_cluster_id"], errors="ignore").to_dict(orient="records")
    # Scrub size_metric: pandas float64 upcasts Python None to NaN in the DataFrame,
    # and Postgres numeric natively accepts NaN — so NaN lands as literal NaN in
    # core.account instead of SQL NULL.  Replace any NaN with real Python None here,
    # after to_dict() has moved values out of the numpy dtype, before the SQL execute.
    for row in rows:
        if pd.isna(row.get("size_metric")):
            row["size_metric"] = None
    with engine.begin() as conn:
        conn.execute(sql, rows)
    logger.info("upserted %d rows to staging.resolved_account", len(rows))
    return len(rows)


def upsert_resolved_location(engine, df: pd.DataFrame) -> int:
    """Upsert staging.resolved_location. Returns row count written."""
    if df.empty:
        return 0

    sql = text("""
        INSERT INTO staging.resolved_location (
            location_key, account_key, vertical, location_name, site_address,
            geom, geocode_precision, geometry_source,
            maintained_acres, acres_confidence, site_type
        ) VALUES (
            :location_key, :account_key, :vertical, :location_name,
            CAST(:site_address AS jsonb),
            CASE
                WHEN :_latitude IS NOT NULL AND :_longitude IS NOT NULL
                THEN ST_SetSRID(
                    ST_MakePoint(
                        CAST(:_longitude AS double precision),
                        CAST(:_latitude AS double precision)
                    ), 4326)
                ELSE NULL
            END,
            :geocode_precision, :geometry_source,
            CAST(:maintained_acres AS numeric), :acres_confidence, :site_type
        )
        ON CONFLICT (location_key) DO UPDATE SET
            account_key       = EXCLUDED.account_key,
            vertical          = EXCLUDED.vertical,
            location_name     = EXCLUDED.location_name,
            site_address      = EXCLUDED.site_address,
            geom              = EXCLUDED.geom,
            geocode_precision = EXCLUDED.geocode_precision,
            geometry_source   = EXCLUDED.geometry_source,
            maintained_acres  = EXCLUDED.maintained_acres,
            acres_confidence  = EXCLUDED.acres_confidence,
            site_type         = EXCLUDED.site_type
    """)

    rows = df.drop(columns=["_natural_key"], errors="ignore").to_dict(orient="records")
    with engine.begin() as conn:
        conn.execute(sql, rows)
    logger.info("upserted %d rows to staging.resolved_location", len(rows))
    return len(rows)


def upsert_resolved_contact(engine, df: pd.DataFrame) -> int:
    """Upsert staging.resolved_contact. Returns row count written."""
    if df.empty:
        return 0

    sql = text("""
        INSERT INTO staging.resolved_contact (
            contact_key, account_key, vertical, full_name, role, role_rank,
            phone, email, address, source_id, is_current
        ) VALUES (
            :contact_key, :account_key, :vertical, :full_name, :role, :role_rank,
            :phone, :email, CAST(:address AS jsonb), :source_id, :is_current
        )
        ON CONFLICT (contact_key) DO UPDATE SET
            account_key = EXCLUDED.account_key,
            vertical    = EXCLUDED.vertical,
            full_name   = EXCLUDED.full_name,
            role        = EXCLUDED.role,
            role_rank   = EXCLUDED.role_rank,
            phone       = EXCLUDED.phone,
            email       = EXCLUDED.email,
            address     = EXCLUDED.address,
            source_id   = EXCLUDED.source_id,
            is_current  = EXCLUDED.is_current
    """)

    rows = df.to_dict(orient="records")
    with engine.begin() as conn:
        conn.execute(sql, rows)
    logger.info("upserted %d rows to staging.resolved_contact", len(rows))
    return len(rows)


# ---------------------------------------------------------------------------
# D15: Atomic replace helper
# ---------------------------------------------------------------------------


def _replace_and_upsert(
    engine,
    account_df: pd.DataFrame,
    location_df: pd.DataFrame,
    contact_df: pd.DataFrame,
    vertical: str,
) -> tuple[int, int, int]:
    """Delete this vertical's existing resolved rows then upsert the fresh set.

    All three deletes and all three upserts share a single transaction so a
    mid-run failure cannot leave any table empty.  The other vertical's rows
    are never touched because every DELETE is scoped by ``WHERE vertical = :v``.

    Returns (n_account, n_location, n_contact) row counts written.
    """
    # Pre-build the upsert SQL objects (same as the standalone helpers above).
    acct_sql = text("""
        INSERT INTO staging.resolved_account (
            account_key, vertical, account_type, legal_name, name_normalized,
            dba_name, parent_account_key, mailing_address, phone, email,
            website, status, external_keys, size_metric, size_metric_unit, confidence
        ) VALUES (
            :account_key, :vertical, :account_type, :legal_name, :name_normalized,
            :dba_name, :parent_account_key,
            CAST(:mailing_address AS jsonb), :phone, :email,
            :website, :status,
            CAST(:external_keys AS jsonb),
            CAST(:size_metric AS numeric), :size_metric_unit,
            CAST(:confidence AS numeric)
        )
        ON CONFLICT (account_key) DO UPDATE SET
            vertical          = EXCLUDED.vertical,
            account_type      = EXCLUDED.account_type,
            legal_name        = EXCLUDED.legal_name,
            name_normalized   = EXCLUDED.name_normalized,
            dba_name          = EXCLUDED.dba_name,
            parent_account_key= EXCLUDED.parent_account_key,
            mailing_address   = EXCLUDED.mailing_address,
            phone             = EXCLUDED.phone,
            email             = EXCLUDED.email,
            website           = EXCLUDED.website,
            status            = EXCLUDED.status,
            external_keys     = EXCLUDED.external_keys,
            size_metric       = EXCLUDED.size_metric,
            size_metric_unit  = EXCLUDED.size_metric_unit,
            confidence        = EXCLUDED.confidence
    """)
    loc_sql = text("""
        INSERT INTO staging.resolved_location (
            location_key, account_key, vertical, location_name, site_address,
            geom, geocode_precision, geometry_source,
            maintained_acres, acres_confidence, site_type
        ) VALUES (
            :location_key, :account_key, :vertical, :location_name,
            CAST(:site_address AS jsonb),
            CASE
                WHEN :_latitude IS NOT NULL AND :_longitude IS NOT NULL
                THEN ST_SetSRID(
                    ST_MakePoint(
                        CAST(:_longitude AS double precision),
                        CAST(:_latitude AS double precision)
                    ), 4326)
                ELSE NULL
            END,
            :geocode_precision, :geometry_source,
            CAST(:maintained_acres AS numeric), :acres_confidence, :site_type
        )
        ON CONFLICT (location_key) DO UPDATE SET
            account_key       = EXCLUDED.account_key,
            vertical          = EXCLUDED.vertical,
            location_name     = EXCLUDED.location_name,
            site_address      = EXCLUDED.site_address,
            geom              = EXCLUDED.geom,
            geocode_precision = EXCLUDED.geocode_precision,
            geometry_source   = EXCLUDED.geometry_source,
            maintained_acres  = EXCLUDED.maintained_acres,
            acres_confidence  = EXCLUDED.acres_confidence,
            site_type         = EXCLUDED.site_type
    """)
    con_sql = text("""
        INSERT INTO staging.resolved_contact (
            contact_key, account_key, vertical, full_name, role, role_rank,
            phone, email, address, source_id, is_current
        ) VALUES (
            :contact_key, :account_key, :vertical, :full_name, :role, :role_rank,
            :phone, :email, CAST(:address AS jsonb), :source_id, :is_current
        )
        ON CONFLICT (contact_key) DO UPDATE SET
            account_key = EXCLUDED.account_key,
            vertical    = EXCLUDED.vertical,
            full_name   = EXCLUDED.full_name,
            role        = EXCLUDED.role,
            role_rank   = EXCLUDED.role_rank,
            phone       = EXCLUDED.phone,
            email       = EXCLUDED.email,
            address     = EXCLUDED.address,
            source_id   = EXCLUDED.source_id,
            is_current  = EXCLUDED.is_current
    """)

    # Scrub NaN → None on size_metric before the round-trip through to_dict().
    acct_rows = account_df.drop(columns=["_cluster_id"], errors="ignore").to_dict(orient="records")
    for row in acct_rows:
        if pd.isna(row.get("size_metric")):
            row["size_metric"] = None

    loc_rows = location_df.drop(columns=["_natural_key"], errors="ignore").to_dict(orient="records")
    con_rows = contact_df.to_dict(orient="records")

    with engine.begin() as conn:
        # Delete this vertical's stale rows BEFORE upserting the fresh set.
        # Scoped to :vertical so the other vertical's rows are untouched.
        conn.execute(
            text("DELETE FROM staging.resolved_contact WHERE vertical = :v"),
            {"v": vertical},
        )
        conn.execute(
            text("DELETE FROM staging.resolved_location WHERE vertical = :v"),
            {"v": vertical},
        )
        conn.execute(
            text("DELETE FROM staging.resolved_account WHERE vertical = :v"),
            {"v": vertical},
        )
        logger.info(
            "deleted existing staging.resolved_* rows for vertical=%s", vertical
        )

        # Upsert the fresh resolved set inside the same transaction.
        if acct_rows:
            conn.execute(acct_sql, acct_rows)
        if loc_rows:
            conn.execute(loc_sql, loc_rows)
        if con_rows:
            conn.execute(con_sql, con_rows)

    logger.info(
        "upserted vertical=%s: account=%d, location=%d, contact=%d",
        vertical, len(acct_rows), len(loc_rows), len(con_rows),
    )
    return len(acct_rows), len(loc_rows), len(con_rows)


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------


def run_pipeline(engine, dry_run: bool = False) -> dict:
    """
    Execute the full healthcare merge pipeline.

    Steps:
      1. Load all healthcare staging rows.
      2. Load geocode cache from staging.enrich_geocode.
      3. Load parcel cache from staging.enrich_parcel.
      4. Run merge_all() → (canonical, review_queue).
      5. Join geocode results onto merged output.
      6. Build resolved_account / resolved_location / resolved_contact DataFrames.
      7. Join parcel acreage onto resolved_location.
      8. Upsert into staging.resolved_* (skipped when dry_run=True).

    Returns a summary dict with row counts for each table.
    """
    logger.info("=== Healthcare pipeline starting (dry_run=%s) ===", dry_run)

    # 1. Load sources
    raw = load_all_sources(engine)
    if raw.empty:
        logger.warning("No data to merge — pipeline complete with zero output rows")
        return {"source_rows": 0, "account": 0, "location": 0, "contact": 0}

    logger.info("source rows loaded: %d", len(raw))

    # 2. Load geocode cache
    geocodes = load_geocode_cache(engine)

    # 3. Load parcel cache
    parcels = load_parcel_cache(engine)

    # 4. Merge
    # Pass engine only on a real run so Tier-3 pairs are enqueued into
    # review.pending_pairs; on dry-run we intentionally pass None to match
    # the same guard that skips staging.resolved_* upserts below.
    logger.info("running merge_all()...")
    merged, review_queue = merge_all(raw, engine=engine if not dry_run else None)
    logger.info(
        "merge complete: %d clusters, %d review_queue rows",
        len(merged), len(review_queue),
    )

    # 5. Join geocodes onto merged output
    merged = join_geocodes(merged, geocodes)

    # 6. Build resolved DataFrames
    account_df = build_resolved_account(merged)
    location_df = build_resolved_location(merged, account_df)
    contact_df = build_resolved_contact(merged, account_df)

    # 7. Join parcel acreage onto resolved_location
    location_df = join_parcel(location_df, parcels)

    logger.info(
        "resolved rows built: account=%d, location=%d, contact=%d",
        len(account_df), len(location_df), len(contact_df),
    )

    if dry_run:
        logger.info("DRY RUN — no writes to staging.resolved_*")
        parcel_matched = location_df["maintained_acres"].notna().sum() if not location_df.empty else 0
        print("\nDry-run summary (no DB writes):", file=sys.stderr)
        print(f"  source rows       : {len(raw):>8,}", file=sys.stderr)
        print(f"  clusters (merged) : {len(merged):>8,}", file=sys.stderr)
        print(f"  resolved_account  : {len(account_df):>8,}", file=sys.stderr)
        print(f"  resolved_location : {len(location_df):>8,}", file=sys.stderr)
        print(f"  resolved_contact  : {len(contact_df):>8,}", file=sys.stderr)
        print(f"  review_queue      : {len(review_queue):>8,}", file=sys.stderr)
        print(f"  parcel_matched    : {parcel_matched:>8,}", file=sys.stderr)
        return {
            "source_rows": len(raw),
            "account": len(account_df),
            "location": len(location_df),
            "contact": len(contact_df),
        }

    # 8. Replace this vertical's slice and upsert — all in one transaction (D15).
    # The delete + upserts share a single engine.begin() so a mid-run failure
    # cannot leave the tables empty: either the whole replace commits or the
    # previous data is rolled back and remains intact.
    n_account, n_location, n_contact = _replace_and_upsert(
        engine, account_df, location_df, contact_df, vertical="healthcare"
    )

    logger.info("=== Healthcare pipeline complete ===")
    print("\nPipeline summary:", file=sys.stderr)
    print(f"  source rows       : {len(raw):>8,}", file=sys.stderr)
    print(f"  clusters (merged) : {len(merged):>8,}", file=sys.stderr)
    print(f"  resolved_account  : {n_account:>8,}", file=sys.stderr)
    print(f"  resolved_location : {n_location:>8,}", file=sys.stderr)
    print(f"  resolved_contact  : {n_contact:>8,}", file=sys.stderr)
    print(f"  review_queue      : {len(review_queue):>8,}", file=sys.stderr)

    return {
        "source_rows": len(raw),
        "account": n_account,
        "location": n_location,
        "contact": n_contact,
    }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entrypoint — run the healthcare merge pipeline."""
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

    if not get_secret("DATABASE_URL"):
        sys.exit(
            "ERROR: DATABASE_URL is not set. "
            "Copy .env.example -> .env and fill it in."
        )

    engine = get_engine()
    run_pipeline(engine, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
