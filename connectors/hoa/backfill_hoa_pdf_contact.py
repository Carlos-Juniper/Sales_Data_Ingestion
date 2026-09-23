"""Fill hoa_* / mgmt_* on staging.enrich_hoa_pdf_contact from stored text.

No PDF download and no OCR. ``mgmt_*`` is copied from the deprecated
``rep_*`` columns. ``hoa_name`` / ``hoa_mailing_address`` are split from
``assoc_mailing_address`` with
``trec_certificate_parser.split_name_and_address`` — the same helper the
parser uses on field 5 and field 6.

Only null or blank targets are written. A second run finds nothing to fill
and does not issue an UPDATE. ``enriched_at`` is left unchanged, so the
copy is not recorded as a new enrich pass.

The stored association value is the comma-joined field-5 blob, so a city
token that was its own line (``Austin, TX 78701`` joined into
``..., Austin, TX 78701``) can stay with the name when no PO Box, c/o, or
street number marks the address. A later certificate re-parse uses the
original line breaks. This script does not re-OCR.

Apply migration 019 first, then:

    python db/run_migrations.py
    python -m hoa.backfill_hoa_pdf_contact --dry-run
    python -m hoa.backfill_hoa_pdf_contact --apply
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Mapping

from sqlalchemy import text

from hoa.trec_certificate_parser import split_name_and_address

# Enrich-table source_id. Matches staging.tx_trec_hoa / the PDF enricher.
SOURCE_ID = "tx_trec_hoa"

CONTACT_COLUMNS = (
    "hoa_name",
    "hoa_mailing_address",
    "mgmt_name",
    "mgmt_mailing_address",
    "mgmt_phone",
    "mgmt_phone_normalized",
    "mgmt_email",
)

# New column ← deprecated alias. Copy, do not re-normalize.
MGMT_FROM_REP = (
    ("mgmt_name", "rep_name"),
    ("mgmt_mailing_address", "rep_mailing_address"),
    ("mgmt_phone", "rep_phone"),
    ("mgmt_phone_normalized", "rep_phone_normalized"),
    ("mgmt_email", "rep_email"),
)


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def plan_contact_updates(row: Mapping[str, Any]) -> dict[str, str]:
    """Columns to fill on one stored row.

    Returns ``{}`` when every target is already populated or the legacy
    source is blank. Populated targets are left as they are.
    """
    updates: dict[str, str] = {}
    for new_col, old_col in MGMT_FROM_REP:
        if _is_blank(row.get(new_col)) and not _is_blank(row.get(old_col)):
            updates[new_col] = str(row.get(old_col)).strip()

    need_name = _is_blank(row.get("hoa_name"))
    need_address = _is_blank(row.get("hoa_mailing_address"))
    if need_name or need_address:
        source = row.get("assoc_mailing_address")
        name, address = split_name_and_address(None if _is_blank(source) else str(source))
        if need_name and name:
            updates["hoa_name"] = name
        if need_address and address:
            updates["hoa_mailing_address"] = address
    return updates


def _is_blank_sql(column: str) -> str:
    return f"NULLIF(btrim({column}), '') IS NULL"


def _candidate_sql():
    selected = (
        "source_id",
        "natural_key",
        "assoc_mailing_address",
        *(old for _, old in MGMT_FROM_REP),
        *CONTACT_COLUMNS,
    )
    columns = ",\n            ".join(selected)
    where = "\n            OR ".join(_is_blank_sql(column) for column in CONTACT_COLUMNS)
    return text(f"""
        SELECT
            {columns}
        FROM staging.enrich_hoa_pdf_contact
        WHERE source_id = :source_id
          AND (
            {where}
          )
    """)


def _null_count_sql():
    filters = ",\n            ".join(
        f"count(*) FILTER (WHERE {_is_blank_sql(column)})::int AS {column}"
        for column in CONTACT_COLUMNS
    )
    return text(f"""
        SELECT
            count(*)::int AS rows_total,
            {filters}
        FROM staging.enrich_hoa_pdf_contact
        WHERE source_id = :source_id
    """)


def _update_sql():
    """Fill blank contact columns. ``enriched_at`` is omitted on purpose."""
    assignments = []
    guards = []
    for column in CONTACT_COLUMNS:
        assignments.append(
            f"{column} = CASE\n"
            f"                WHEN {_is_blank_sql(column)} THEN :{column}\n"
            f"                ELSE {column}\n"
            f"            END"
        )
        guards.append(
            f"({_is_blank_sql(column)} AND CAST(:{column} AS text) IS NOT NULL)"
        )
    set_clause = ",\n            ".join(assignments)
    where_change = "\n            OR ".join(guards)
    return text(f"""
        UPDATE staging.enrich_hoa_pdf_contact
        SET
            {set_clause}
        WHERE source_id = :source_id
          AND natural_key = :natural_key
          AND (
            {where_change}
          )
    """)


def _as_dicts(result) -> list[dict[str, Any]]:
    return [dict(row) for row in result.mappings()]


def null_counts(conn, source_id: str = SOURCE_ID) -> dict[str, int]:
    row = conn.execute(_null_count_sql(), {"source_id": source_id}).mappings().one()
    return {key: int(row[key]) for key in ("rows_total", *CONTACT_COLUMNS)}


def fetch_candidates(conn, source_id: str = SOURCE_ID) -> list[dict[str, Any]]:
    result = conn.execute(_candidate_sql(), {"source_id": source_id})
    return _as_dicts(result)


def _payload(row: Mapping[str, Any], updates: Mapping[str, str]) -> dict[str, Any]:
    payload: dict[str, Any] = {column: updates.get(column) for column in CONTACT_COLUMNS}
    payload["source_id"] = row["source_id"]
    payload["natural_key"] = row["natural_key"]
    return payload


def run_backfill(engine, *, apply: bool, source_id: str = SOURCE_ID) -> dict[str, Any]:
    """Plan fills from current rows. Write them only when ``apply`` is true."""
    with engine.connect() as conn:
        before = null_counts(conn, source_id)
        candidates = fetch_candidates(conn, source_id)

    fills = {column: 0 for column in CONTACT_COLUMNS}
    payloads: list[dict[str, Any]] = []
    for row in candidates:
        updates = plan_contact_updates(row)
        if not updates:
            continue
        payloads.append(_payload(row, updates))
        for column in updates:
            fills[column] += 1

    projected = {
        "rows_total": before["rows_total"],
        **{column: before[column] - fills[column] for column in CONTACT_COLUMNS},
    }
    stats: dict[str, Any] = {
        "mode": "apply" if apply else "dry-run",
        "source_id": source_id,
        "rows_total": before["rows_total"],
        "candidates": len(candidates),
        "rows_planned": len(payloads),
        "rows_unchanged": len(candidates) - len(payloads),
        "rows_updated": 0,
        "fills": fills,
        "nulls_before": before,
        "nulls_after": projected,
    }

    if apply and payloads:
        with engine.begin() as conn:
            result = conn.execute(_update_sql(), payloads)
            rowcount = result.rowcount
            if isinstance(rowcount, int) and rowcount >= 0:
                stats["rows_updated"] = rowcount
            else:
                stats["rows_updated"] = len(payloads)
        with engine.connect() as conn:
            stats["nulls_after"] = null_counts(conn, source_id)
    return stats


def format_report(stats: Mapping[str, Any]) -> str:
    """Human-readable counts for dry-run and apply."""
    mode = stats["mode"]
    if mode == "apply":
        header = "hoa pdf contact column backfill (apply)"
    else:
        header = "hoa pdf contact column backfill (dry-run, no writes)"
    lines = [
        header,
        f"  source_id                  {stats['source_id']}",
        f"  rows in table              {stats['rows_total']:>7,}",
        f"  candidate rows             {stats['candidates']:>7,}",
        f"  rows to update             {stats['rows_planned']:>7,}",
        f"  rows with nothing to fill  {stats['rows_unchanged']:>7,}",
    ]
    if mode == "apply":
        lines.append(f"  rows updated               {stats['rows_updated']:>7,}")
    lines.append("  column                     fill   still null")
    after = stats["nulls_after"]
    for column in CONTACT_COLUMNS:
        lines.append(
            f"  {column:<26} {stats['fills'][column]:>7,} {after[column]:>11,}"
        )
    lines.append("  enriched_at is left unchanged.")
    if mode != "apply":
        lines.append("  Apply with: python -m hoa.backfill_hoa_pdf_contact --apply")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Report how many rows would change. This is the default.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Fill blank hoa_* / mgmt_* columns on staging.enrich_hoa_pdf_contact.",
    )
    args = parser.parse_args(argv)

    from lib.db import get_engine
    from lib.http import get_secret

    if not get_secret("DATABASE_URL"):
        sys.exit(
            "ERROR: DATABASE_URL is not set. "
            "Copy .env.example -> .env and fill it in."
        )
    stats = run_backfill(get_engine(), apply=bool(args.apply))
    sys.stdout.write(format_report(stats))


if __name__ == "__main__":
    main()
