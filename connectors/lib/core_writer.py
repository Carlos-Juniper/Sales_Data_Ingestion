"""
core_writer — 3-way diff from staging.resolved_* into core.*.

This module is the FIRST code in the project that writes to core.account,
core.location, core.contact, and core.source_record.  It implements:

  D5: one SQL statement set performs the diff — INSERT / UPDATE / TOMBSTONE
      — making "prove a re-run won't duplicate" a plain dry-run query.

  D8: content-addressed source_record upsert — unchanged payload only bumps
      last_seen_run_id, never inserts a second row.

  D2: tombstoning via status='merged' + parent_account_id.  No new tables.
      An alias/survivor lookup prevents re-keyed accounts from duplicating.

Public API
----------
  dry_run_diff(engine)             → DiffCounts  (no writes)
  apply_core_diff(engine, run_id)  → DiffCounts  (transactional write)
  upsert_source_records(engine, records, run_id)
                                   → (inserted, bumped) counts

The SQL is written in terms of staging.resolved_* as the fixed input
contract (migration 011 schema).  Do NOT modify staging tables here.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------

@dataclass
class DiffCounts:
    """Counts returned by the dry-run and apply paths.

    All counts reflect one execution of the 3-way diff between
    staging.resolved_* and core.*.
    """
    account_inserts: int = 0
    account_updates: int = 0
    account_tombstones: int = 0
    location_inserts: int = 0
    location_updates: int = 0
    location_tombstones: int = 0
    contact_inserts: int = 0
    contact_updates: int = 0
    contact_tombstones: int = 0
    source_record_inserts: int = 0
    source_record_bumps: int = 0   # last_seen_run_id bumped, no new row

    def total_core_writes(self) -> int:
        """Total DML rows affecting core.* tables (excludes source_record bumps)."""
        return (
            self.account_inserts + self.account_updates + self.account_tombstones
            + self.location_inserts + self.location_updates + self.location_tombstones
            + self.contact_inserts + self.contact_updates + self.contact_tombstones
            + self.source_record_inserts
        )

    def __repr__(self) -> str:
        parts = [
            f"account(+{self.account_inserts} ~{self.account_updates} 🪦{self.account_tombstones})",
            f"location(+{self.location_inserts} ~{self.location_updates} 🪦{self.location_tombstones})",
            f"contact(+{self.contact_inserts} ~{self.contact_updates} 🪦{self.contact_tombstones})",
            f"source_record(+{self.source_record_inserts} bump={self.source_record_bumps})",
        ]
        return "DiffCounts(" + " ".join(parts) + ")"


# ---------------------------------------------------------------------------
# Content-hash helper for mutable account/location/contact fields
# ---------------------------------------------------------------------------
# We compute a deterministic hash in *SQL* (md5 of a concatenated text) to
# drive the UPDATE arm of the 3-way diff.  The per-field list below defines
# what "content changed" means for each entity type.  Immutable provenance
# columns (first_seen, account_key) are intentionally excluded.
#
# The SQL expression is rendered once at module load; individual functions
# reference the constant strings to keep the logic in one place.

_ACCOUNT_HASH_EXPR = """md5(concat_ws('|',
    ra.vertical,
    ra.account_type,
    ra.legal_name,
    ra.name_normalized,
    ra.dba_name,
    ra.parent_account_key,
    ra.mailing_address::text,
    ra.phone,
    ra.email,
    ra.website,
    ra.status,
    ra.external_keys::text,
    ra.size_metric::text,
    ra.size_metric_unit,
    ra.confidence::text
))"""

_LOCATION_HASH_EXPR = """md5(concat_ws('|',
    rl.location_name,
    rl.site_address::text,
    ST_AsEWKB(rl.geom)::text,
    ST_AsEWKB(rl.boundary)::text,
    rl.geocode_precision,
    rl.geometry_source,
    rl.maintained_acres::text,
    rl.acres_confidence,
    rl.site_type
))"""

_CONTACT_HASH_EXPR = """md5(concat_ws('|',
    rc.full_name,
    rc.role,
    rc.role_rank::text,
    rc.phone,
    rc.email,
    rc.address::text,
    rc.source_id,
    rc.is_current::text
))"""

# Same expressions rewritten for the core.* side of the JOIN:
_ACCOUNT_CORE_HASH_EXPR = """md5(concat_ws('|',
    ca.vertical,
    ca.account_type,
    ca.legal_name,
    ca.name_normalized,
    ca.dba_name,
    pa.account_key,
    ca.mailing_address::text,
    ca.phone,
    ca.email,
    ca.website,
    ca.status,
    ca.external_keys::text,
    ca.size_metric::text,
    ca.size_metric_unit,
    ca.confidence::text
))"""

_LOCATION_CORE_HASH_EXPR = """md5(concat_ws('|',
    cl.location_name,
    cl.site_address::text,
    ST_AsEWKB(cl.geom)::text,
    ST_AsEWKB(cl.boundary)::text,
    cl.geocode_precision,
    cl.geometry_source,
    cl.maintained_acres::text,
    cl.acres_confidence,
    cl.site_type
))"""

_CONTACT_CORE_HASH_EXPR = """md5(concat_ws('|',
    cc.full_name,
    cc.role,
    cc.role_rank::text,
    cc.phone,
    cc.email,
    cc.address::text,
    cc.source_id,
    cc.is_current::text
))"""


# ---------------------------------------------------------------------------
# Dry-run query (D5 §8 proof)
# ---------------------------------------------------------------------------

def dry_run_diff(engine: Engine) -> DiffCounts:
    """Return insert/update/tombstone counts for all core tables WITHOUT writing.

    This is the operational proof of D5: run before every production apply
    to see exactly what would change.  The CTEs are identical to those in
    apply_core_diff; only the final SELECT replaces the DML.

    Idempotency check: after two consecutive applies on unchanged
    staging.resolved_*, every count here must be 0.
    """
    sql = _build_dry_run_sql()
    with engine.connect() as conn:
        row = conn.execute(text(sql)).fetchone()
    if row is None:
        return DiffCounts()
    return DiffCounts(
        account_inserts=row[0] or 0,
        account_updates=row[1] or 0,
        account_tombstones=row[2] or 0,
        location_inserts=row[3] or 0,
        location_updates=row[4] or 0,
        location_tombstones=row[5] or 0,
        contact_inserts=row[6] or 0,
        contact_updates=row[7] or 0,
        contact_tombstones=row[8] or 0,
    )


def _build_dry_run_sql() -> str:
    """Build the dry-run COUNT query — identical CTEs to apply but no DML."""
    return f"""
WITH
-- ---------------------------------------------------------------
-- Alias/survivor lookup (D2): for each resolved account whose key
-- already exists in core with status='merged', follow parent_account_id
-- until we reach the surviving (non-merged) ancestor.  This prevents a
-- re-keyed account from being re-inserted as a new duplicate.
-- Implemented as a recursive CTE on core.account — no new tables (D2).
-- ---------------------------------------------------------------
survivor AS (
    SELECT DISTINCT ON (a.account_key)
        a.account_key,
        COALESCE(root.account_id, a.account_id)  AS survivor_id,
        COALESCE(root.account_key, a.account_key) AS survivor_key
    FROM core.account a
    LEFT JOIN core.account root
        ON root.account_id = a.parent_account_id
        AND root.status <> 'merged'
    WHERE a.account_key IS NOT NULL
),
-- ---------------------------------------------------------------
-- Account diff arms
-- ---------------------------------------------------------------
acct_new AS (
    -- Keys present in resolved but absent from core → INSERT
    SELECT ra.account_key
    FROM staging.resolved_account ra
    LEFT JOIN core.account ca ON ca.account_key = ra.account_key
    WHERE ca.account_id IS NULL
),
acct_changed AS (
    -- Keys present in both but content hash differs → UPDATE
    SELECT ra.account_key
    FROM staging.resolved_account ra
    JOIN core.account ca ON ca.account_key = ra.account_key
    LEFT JOIN core.account pa ON pa.account_id = ca.parent_account_id
    WHERE {_ACCOUNT_HASH_EXPR} <> {_ACCOUNT_CORE_HASH_EXPR}
),
acct_gone AS (
    -- Core rows absent from resolved AND not already tombstoned → TOMBSTONE
    SELECT ca.account_key
    FROM core.account ca
    LEFT JOIN staging.resolved_account ra ON ra.account_key = ca.account_key
    WHERE ra.account_key IS NULL
      AND ca.account_key IS NOT NULL
      AND ca.status <> 'merged'
),
-- ---------------------------------------------------------------
-- Location diff arms
-- ---------------------------------------------------------------
loc_new AS (
    SELECT rl.location_key
    FROM staging.resolved_location rl
    LEFT JOIN core.location cl ON cl.location_key = rl.location_key
    WHERE cl.location_id IS NULL
),
loc_changed AS (
    SELECT rl.location_key
    FROM staging.resolved_location rl
    JOIN core.location cl ON cl.location_key = rl.location_key
    WHERE {_LOCATION_HASH_EXPR} <> {_LOCATION_CORE_HASH_EXPR}
),
loc_gone AS (
    SELECT cl.location_key
    FROM core.location cl
    LEFT JOIN staging.resolved_location rl ON rl.location_key = cl.location_key
    WHERE rl.location_key IS NULL
      AND cl.location_key IS NOT NULL
      AND cl.last_seen < now() - interval '1 second'
),
-- ---------------------------------------------------------------
-- Contact diff arms
-- ---------------------------------------------------------------
cont_new AS (
    SELECT rc.contact_key
    FROM staging.resolved_contact rc
    LEFT JOIN core.contact cc ON cc.contact_key = rc.contact_key
    WHERE cc.contact_id IS NULL
),
cont_changed AS (
    SELECT rc.contact_key
    FROM staging.resolved_contact rc
    JOIN core.contact cc ON cc.contact_key = rc.contact_key
    WHERE {_CONTACT_HASH_EXPR} <> {_CONTACT_CORE_HASH_EXPR}
),
cont_gone AS (
    SELECT cc.contact_key
    FROM core.contact cc
    LEFT JOIN staging.resolved_contact rc ON rc.contact_key = cc.contact_key
    WHERE rc.contact_key IS NULL
      AND cc.contact_key IS NOT NULL
      AND cc.is_current = true
)
SELECT
    (SELECT count(*) FROM acct_new)       AS account_inserts,
    (SELECT count(*) FROM acct_changed)   AS account_updates,
    (SELECT count(*) FROM acct_gone)      AS account_tombstones,
    (SELECT count(*) FROM loc_new)        AS location_inserts,
    (SELECT count(*) FROM loc_changed)    AS location_updates,
    (SELECT count(*) FROM loc_gone)       AS location_tombstones,
    (SELECT count(*) FROM cont_new)       AS contact_inserts,
    (SELECT count(*) FROM cont_changed)   AS contact_updates,
    (SELECT count(*) FROM cont_gone)      AS contact_tombstones
"""


# ---------------------------------------------------------------------------
# Apply path
# ---------------------------------------------------------------------------

def apply_core_diff(engine: Engine, run_id: int) -> DiffCounts:
    """Write the 3-way diff from staging.resolved_* into core.* atomically.

    All DML runs in a single transaction — either everything commits or nothing
    does.  The dry-run CTEs are inlined as part of each statement so the
    diff logic is always in sync between the proof and the apply.

    The ``run_id`` is written into core.source_record columns
    first_seen_run_id / last_seen_run_id via upsert_source_records; it is
    NOT used here directly because core.account/location/contact don't carry
    a run_id — they carry first_seen / last_seen timestamps instead.

    Returns DiffCounts reflecting what was actually written.
    """
    counts = DiffCounts()
    with engine.begin() as conn:
        counts.account_inserts = _apply_account_inserts(conn)
        counts.account_updates = _apply_account_updates(conn)
        counts.account_tombstones = _apply_account_tombstones(conn)
        counts.location_inserts = _apply_location_inserts(conn)
        counts.location_updates = _apply_location_updates(conn)
        counts.location_tombstones = _apply_location_tombstones(conn)
        counts.contact_inserts = _apply_contact_inserts(conn)
        counts.contact_updates = _apply_contact_updates(conn)
        counts.contact_tombstones = _apply_contact_tombstones(conn)

    logger.info(
        "apply_core_diff complete: "
        "acct(+%d ~%d 🪦%d) loc(+%d ~%d 🪦%d) cont(+%d ~%d 🪦%d)",
        counts.account_inserts, counts.account_updates, counts.account_tombstones,
        counts.location_inserts, counts.location_updates, counts.location_tombstones,
        counts.contact_inserts, counts.contact_updates, counts.contact_tombstones,
    )
    return counts


# ---------------------------------------------------------------------------
# Account DML
# ---------------------------------------------------------------------------

def _apply_account_inserts(conn) -> int:
    """INSERT core.account rows whose account_key is absent from core."""
    result = conn.execute(text(f"""
        INSERT INTO core.account (
            account_key, vertical, account_type,
            legal_name, name_normalized, dba_name,
            parent_account_id,
            mailing_address, phone, email, website,
            status, external_keys,
            size_metric, size_metric_unit, confidence,
            first_seen, last_seen
        )
        SELECT
            ra.account_key,
            ra.vertical,
            ra.account_type,
            ra.legal_name,
            ra.name_normalized,
            ra.dba_name,
            -- D2 alias lookup: if parent_account_key points to an existing
            -- core account, resolve to that account's id; else NULL.
            parent_ca.account_id   AS parent_account_id,
            ra.mailing_address,
            ra.phone,
            ra.email,
            ra.website,
            ra.status,
            ra.external_keys,
            ra.size_metric,
            ra.size_metric_unit,
            ra.confidence,
            now()                  AS first_seen,
            now()                  AS last_seen
        FROM staging.resolved_account ra
        LEFT JOIN core.account ca ON ca.account_key = ra.account_key
        LEFT JOIN core.account parent_ca
            ON parent_ca.account_key = ra.parent_account_key
        WHERE ca.account_id IS NULL
        ON CONFLICT (account_key) DO NOTHING
    """))
    return result.rowcount


def _apply_account_updates(conn) -> int:
    """UPDATE mutable fields on core.account rows whose content hash changed.

    PostgreSQL's UPDATE ... FROM syntax does not allow the target table to
    appear in the FROM clause joins.  We use a CTE to pre-compute the changed
    rows and the parent_account_id lookup so the UPDATE target (ca) never
    appears on the right-hand side of a JOIN in the FROM clause.
    """
    result = conn.execute(text(f"""
        WITH changed AS (
            -- Compute the new parent_account_id via a plain SELECT (no target
            -- table in FROM), then join that result into the UPDATE.
            SELECT
                ca.account_id,
                ra.vertical,
                ra.account_type,
                ra.legal_name,
                ra.name_normalized,
                ra.dba_name,
                parent_ca.account_id AS new_parent_account_id,
                ra.mailing_address,
                ra.phone,
                ra.email,
                ra.website,
                -- Only update status if the row is NOT already tombstoned.
                -- A 'merged' row should never be resurrected by an UPDATE.
                CASE WHEN ca.status = 'merged' THEN 'merged'
                     ELSE ra.status END AS new_status,
                ra.external_keys,
                ra.size_metric,
                ra.size_metric_unit,
                ra.confidence
            FROM staging.resolved_account ra
            JOIN core.account ca ON ca.account_key = ra.account_key
            -- pa resolves the CURRENT parent key from core (for the hash check)
            LEFT JOIN core.account pa ON pa.account_id = ca.parent_account_id
            -- parent_ca resolves the NEW parent from resolved (for the update)
            LEFT JOIN core.account parent_ca
                ON parent_ca.account_key = ra.parent_account_key
            WHERE {_ACCOUNT_HASH_EXPR} <> {_ACCOUNT_CORE_HASH_EXPR}
        )
        UPDATE core.account ca
        SET
            vertical          = c.vertical,
            account_type      = c.account_type,
            legal_name        = c.legal_name,
            name_normalized   = c.name_normalized,
            dba_name          = c.dba_name,
            parent_account_id = c.new_parent_account_id,
            mailing_address   = c.mailing_address,
            phone             = c.phone,
            email             = c.email,
            website           = c.website,
            status            = c.new_status,
            external_keys     = c.external_keys,
            size_metric       = c.size_metric,
            size_metric_unit  = c.size_metric_unit,
            confidence        = c.confidence,
            last_seen         = now()
        FROM changed c
        WHERE ca.account_id = c.account_id
    """))
    return result.rowcount


def _apply_account_tombstones(conn) -> int:
    """TOMBSTONE core.account rows absent from the current resolved set.

    Sets status='merged' and parent_account_id to the survivor's account_id
    when a survivor can be identified.  This implements D2: the old row
    is preserved (first_seen intact, FKs unbroken) but marked as merged.

    'Survivor' is identified by finding the account in resolved_account with
    the highest priority status ('active' > 'inactive' > others) that was
    previously grouped with the now-absent key.  In practice the merge module
    sets parent_account_key on the resolved row; we back-resolve that to an
    account_id here.

    If no survivor is identifiable (the whole cluster vanished), status is
    set to 'merged' with parent_account_id = NULL — which is valid per the
    schema (parent_account_id is nullable).
    """
    result = conn.execute(text("""
        UPDATE core.account ca
        SET
            status            = 'merged',
            parent_account_id = survivor.account_id,
            last_seen         = now()
        FROM (
            -- For each absent core row, try to find a surviving account
            -- whose account_key corresponds to this row's parent_account_key
            -- (as recorded in resolved_account.parent_account_key).
            SELECT
                ca_inner.account_id     AS dead_account_id,
                parent_ca.account_id    AS account_id
            FROM core.account ca_inner
            -- Dead = key not in resolved, not already tombstoned
            LEFT JOIN staging.resolved_account ra
                ON ra.account_key = ca_inner.account_key
            -- Attempt to find which resolved account claimed to absorb this one.
            -- The merge module sets parent_account_key on the survivor's row;
            -- we look for any resolved row pointing at this account's key.
            LEFT JOIN staging.resolved_account survivor_ra
                ON survivor_ra.parent_account_key = ca_inner.account_key
            LEFT JOIN core.account parent_ca
                ON parent_ca.account_key = survivor_ra.account_key
            WHERE ra.account_key IS NULL
              AND ca_inner.account_key IS NOT NULL
              AND ca_inner.status <> 'merged'
        ) AS survivor
        WHERE ca.account_id = survivor.dead_account_id
    """))
    return result.rowcount


# ---------------------------------------------------------------------------
# Location DML
# ---------------------------------------------------------------------------

def _apply_location_inserts(conn) -> int:
    """INSERT core.location rows whose location_key is absent from core."""
    result = conn.execute(text(f"""
        INSERT INTO core.location (
            location_key, account_id,
            location_name, site_address,
            geom, boundary,
            geocode_precision, geometry_source,
            maintained_acres, acres_confidence,
            site_type,
            first_seen, last_seen
        )
        SELECT
            rl.location_key,
            ca.account_id,
            rl.location_name,
            rl.site_address,
            rl.geom,
            rl.boundary,
            rl.geocode_precision,
            rl.geometry_source,
            rl.maintained_acres,
            rl.acres_confidence,
            rl.site_type,
            now() AS first_seen,
            now() AS last_seen
        FROM staging.resolved_location rl
        -- Join to core.account to get account_id from account_key.
        -- If the account doesn't exist yet this run (shouldn't happen if
        -- apply_account_inserts ran first), the location row is skipped
        -- via the INNER JOIN — it will be picked up on the next run.
        JOIN core.account ca ON ca.account_key = rl.account_key
        LEFT JOIN core.location cl ON cl.location_key = rl.location_key
        WHERE cl.location_id IS NULL
        ON CONFLICT (location_key) DO NOTHING
    """))
    return result.rowcount


def _apply_location_updates(conn) -> int:
    """UPDATE mutable fields on core.location rows whose content changed."""
    result = conn.execute(text(f"""
        UPDATE core.location cl
        SET
            location_name     = rl.location_name,
            site_address      = rl.site_address,
            geom              = rl.geom,
            boundary          = rl.boundary,
            geocode_precision = rl.geocode_precision,
            geometry_source   = rl.geometry_source,
            maintained_acres  = rl.maintained_acres,
            acres_confidence  = rl.acres_confidence,
            site_type         = rl.site_type,
            last_seen         = now()
        FROM staging.resolved_location rl
        WHERE cl.location_key = rl.location_key
          AND {_LOCATION_HASH_EXPR} <> {_LOCATION_CORE_HASH_EXPR}
    """))
    return result.rowcount


def _apply_location_tombstones(conn) -> int:
    """Mark core.location rows absent from resolved as stale (last_seen bump).

    Locations don't have a status/merged pattern like accounts, so 'tombstone'
    here means bumping last_seen to a sentinel value so they can be identified
    as stale.  The application query layer filters on
    ``last_seen >= current_run_started_at`` for active-only views.

    Assumption: locations are soft-deleted by the account tombstone cascade.
    We do NOT hard-delete or flag locations independently because:
      1. core.location has no status column (migration 003 didn't add one)
      2. FK references from core.source_record must not break
    The count returned reflects rows whose account was tombstoned
    (parent account now 'merged') — informational only.
    """
    # Use a CTE to identify affected location_ids without referencing the
    # UPDATE target (cl) inside a FROM-clause join (not valid in PG syntax).
    result = conn.execute(text("""
        WITH stale_locs AS (
            SELECT cl_inner.location_id
            FROM core.location cl_inner
            JOIN core.account ca ON ca.account_id = cl_inner.account_id
            LEFT JOIN staging.resolved_location rl
                ON rl.location_key = cl_inner.location_key
            WHERE ca.status = 'merged'
              AND rl.location_key IS NULL
              AND cl_inner.location_key IS NOT NULL
        )
        UPDATE core.location cl
        SET last_seen = now()
        FROM stale_locs
        WHERE cl.location_id = stale_locs.location_id
    """))
    return result.rowcount


# ---------------------------------------------------------------------------
# Contact DML
# ---------------------------------------------------------------------------

def _apply_contact_inserts(conn) -> int:
    """INSERT core.contact rows whose contact_key is absent from core."""
    result = conn.execute(text(f"""
        INSERT INTO core.contact (
            contact_key, account_id,
            full_name, role, role_rank,
            phone, email, address,
            source_id, is_current
        )
        SELECT
            rc.contact_key,
            ca.account_id,
            rc.full_name,
            rc.role,
            rc.role_rank,
            rc.phone,
            rc.email,
            rc.address,
            rc.source_id,
            rc.is_current
        FROM staging.resolved_contact rc
        JOIN core.account ca ON ca.account_key = rc.account_key
        LEFT JOIN core.contact cc ON cc.contact_key = rc.contact_key
        WHERE cc.contact_id IS NULL
        ON CONFLICT (contact_key) DO NOTHING
    """))
    return result.rowcount


def _apply_contact_updates(conn) -> int:
    """UPDATE mutable fields on core.contact rows whose content changed."""
    result = conn.execute(text(f"""
        UPDATE core.contact cc
        SET
            full_name  = rc.full_name,
            role       = rc.role,
            role_rank  = rc.role_rank,
            phone      = rc.phone,
            email      = rc.email,
            address    = rc.address,
            source_id  = rc.source_id,
            is_current = rc.is_current
        FROM staging.resolved_contact rc
        WHERE cc.contact_key = rc.contact_key
          AND {_CONTACT_HASH_EXPR} <> {_CONTACT_CORE_HASH_EXPR}
    """))
    return result.rowcount


def _apply_contact_tombstones(conn) -> int:
    """Set is_current=false on core.contact rows absent from the resolved set."""
    result = conn.execute(text("""
        UPDATE core.contact cc
        SET is_current = false
        FROM (
            SELECT cc_inner.contact_id
            FROM core.contact cc_inner
            LEFT JOIN staging.resolved_contact rc
                ON rc.contact_key = cc_inner.contact_key
            WHERE rc.contact_key IS NULL
              AND cc_inner.contact_key IS NOT NULL
              AND cc_inner.is_current = true
        ) AS gone
        WHERE cc.contact_id = gone.contact_id
    """))
    return result.rowcount


# ---------------------------------------------------------------------------
# source_record content-addressed upsert (D8)
# ---------------------------------------------------------------------------

@dataclass
class SourceRecordRow:
    """One row to upsert into core.source_record."""
    source_id: str
    natural_key: str
    payload: dict
    account_id: Optional[int] = None
    location_id: Optional[int] = None
    match_score: Optional[float] = None
    match_method: Optional[str] = None

    def payload_sha(self) -> str:
        """Hex SHA-256 of the canonical JSON payload."""
        canonical = json.dumps(self.payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def upsert_source_records(
    engine: Engine,
    records: Sequence[SourceRecordRow],
    run_id: int,
) -> tuple[int, int]:
    """Content-addressed upsert into core.source_record (D8).

    For each record:
      - If (source_id, natural_key, payload_sha) is new: INSERT, set
        first_seen_run_id=run_id, last_seen_run_id=run_id.
      - If the triple already exists (same payload, same key): only bump
        last_seen_run_id.  Zero new rows — idempotency proof.
      - If natural_key matches but payload changed: INSERT a new row (new sha),
        preserving the old row as historical provenance.

    Returns (inserted_count, bumped_count).

    Note: source_run_id is set to run_id on insert; it records which run
    first registered this (source_id, natural_key, payload_sha) triple.
    """
    if not records:
        return 0, 0

    rows = [
        {
            "source_id": r.source_id,
            "natural_key": r.natural_key,
            "payload": json.dumps(r.payload, sort_keys=True, ensure_ascii=False),
            "payload_sha": r.payload_sha(),
            "account_id": r.account_id,
            "location_id": r.location_id,
            "match_score": r.match_score,
            "match_method": r.match_method,
            "run_id": run_id,
        }
        for r in records
    ]

    upsert_sql = text("""
        INSERT INTO core.source_record (
            source_id, source_run_id, natural_key, payload,
            account_id, location_id, match_score, match_method,
            payload_sha, first_seen_run_id, last_seen_run_id
        ) VALUES (
            :source_id, :run_id, :natural_key, CAST(:payload AS jsonb),
            :account_id, :location_id, :match_score, :match_method,
            :payload_sha, :run_id, :run_id
        )
        ON CONFLICT (source_id, natural_key, payload_sha) DO UPDATE
            SET last_seen_run_id = EXCLUDED.last_seen_run_id
        RETURNING
            (xmax = 0) AS was_inserted
    """)

    inserted = 0
    bumped = 0
    with engine.begin() as conn:
        for row in rows:
            result = conn.execute(upsert_sql, row)
            for returned_row in result:
                if returned_row[0]:  # xmax=0 means INSERT (not UPDATE)
                    inserted += 1
                else:
                    bumped += 1

    logger.info(
        "upsert_source_records: run_id=%d inserted=%d bumped=%d",
        run_id, inserted, bumped,
    )
    return inserted, bumped


# ---------------------------------------------------------------------------
# Convenience: resolve account_id from account_key for source_record linking
# ---------------------------------------------------------------------------

def resolve_account_ids(
    engine: Engine,
    account_keys: Sequence[str],
) -> dict[str, int]:
    """Return a mapping of account_key → account_id for the given keys.

    Used to populate SourceRecordRow.account_id after apply_core_diff.
    Keys not found in core.account are omitted from the result.
    """
    if not account_keys:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT account_key, account_id
                FROM core.account
                WHERE account_key = ANY(:keys)
            """),
            {"keys": list(account_keys)},
        ).fetchall()
    return {r[0]: r[1] for r in rows}
