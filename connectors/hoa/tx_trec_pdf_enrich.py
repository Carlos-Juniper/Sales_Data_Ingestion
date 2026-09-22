"""
TX TREC management-certificate PDF OCR enrichment.

Consumes the queue CSV from ``tx_trec_hoa.build_pdf_queue()``, downloads
each certificate PDF, extracts contact fields (via pdfplumber or OCR), and
writes results to ``staging.enrich_hoa_pdf_contact``.

Parser logic lives in ``hoa.trec_certificate_parser``.
PDF extraction (pdfplumber + OCR) lives in ``lib.pdf_ocr``.

Usage:
    python -m hoa.tx_trec_pdf_enrich \\
        --queue tx_trec_pdf_queue.csv \\
        --out tx_trec_pdf_enriched.csv \\
        --write-db
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import pandas as pd
import requests
from sqlalchemy import text

from hoa.trec_certificate_parser import (
    _REVIEW_REASON_SET,
    _reasons_text,
    parse_certificate_text,
)
from lib.enrich_runner import run_enrichment
from lib.enums import ENRICH_ERROR, ENRICH_NOT_FOUND, ENRICH_OK, ENRICH_SKIPPED
from lib.gcs import raw_sha256, upload_raw
from lib.http import make_session
from lib.pdf_ocr import (
    RateLimiter,
    _DEFAULT_DPI,
    _PDFPLUMBER_MIN_CHARS,
    _extract_text_pdfplumber,
    ocr_pdf,
)

# ---------------------------------------------------------------- constants

# GCS prefix and ingest.source_run.source_id for this pass.
CONNECTOR_ID = "tx_trec_pdf_enrich"
# Enrich-table source_id. Matches staging.tx_trec_hoa so the JOIN is direct.
UPSTREAM_SOURCE_ID = "tx_trec_hoa"
CONNECTOR_VERSION = "1.0"
LICENSE = (
    "Texas public records — Tex. Prop. Code §209.004 management certificates "
    "filed with TREC; free to store and use commercially"
)

USER_AGENT = (
    "juniper-hoa-pipeline/1.0 "
    "(TX TREC management-certificate public records)"
)

_FETCH_TIMEOUT_S = 60
_MAX_PDF_BYTES = 25 * 1024 * 1024

_QUEUE_REQUIRED = {"association_id", "url"}

_STATUS_RANK = {
    ENRICH_OK: 3,
    ENRICH_NOT_FOUND: 2,
    ENRICH_ERROR: 1,
    ENRICH_SKIPPED: 0,
}

# Columns written to staging.enrich_hoa_pdf_contact. enriched_at is a DB default.
_UPSERT_COLUMNS = [
    "source_id",
    "natural_key",
    "certificate_id",
    "certificate_url",
    "assoc_mailing_address",
    "rep_name",
    "rep_mailing_address",
    "rep_phone",
    "rep_phone_normalized",
    "rep_email",
    "website",
    "field_6_raw",
    "enrich_status",
    "confidence",
    "needs_review",
    "review_reasons",
    "ocr_text_sha256",
    "raw_pdf_sha256",
    "raw_pdf_uri",
    "error_detail",
]

_TEXT_COLUMNS = [
    "assoc_mailing_address",
    "rep_name",
    "rep_mailing_address",
    "rep_phone",
    "rep_phone_normalized",
    "rep_email",
    "website",
    "field_6_raw",
    "certificate_id",
    "certificate_url",
    "review_reasons",
    "ocr_text_sha256",
    "raw_pdf_sha256",
    "raw_pdf_uri",
    "error_detail",
    "enrich_status",
]


# ---------------------------------------------------------------- fetch + ocr


def _clean_id(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    if not text_value or text_value.lower() in {"nan", "none", "<na>"}:
        return None
    if re.fullmatch(r"\d+\.0+", text_value):
        text_value = text_value.split(".", 1)[0]
    return text_value


def normalize_certificate_url(url: str) -> str:
    """Rejoin a ``#`` filename into the path and percent-encode it.

    ``urlsplit`` treats ``#`` as the fragment separator. TREC certificate
    filenames often contain a literal ``#`` (for example
    ``#7 Hyde Park -- Management Certificate.pdf``), so a raw URL is fetched
    as a truncated path and hoa.texas.gov returns 403 or 404. A non-empty
    fragment is part of the path. The path is unquoted and then quoted so
    ``#`` becomes ``%23`` without double-encoding sequences such as ``%20``.
    """
    parts = urlsplit(url)
    path = parts.path
    if parts.fragment:
        path = f"{path}#{parts.fragment}"
    encoded_path = quote(unquote(path), safe="/")
    return urlunsplit((parts.scheme, parts.netloc, encoded_path, parts.query, ""))


def fetch_pdf(
    url: str,
    session: requests.Session,
    rate_limiter: RateLimiter,
    *,
    timeout: float = _FETCH_TIMEOUT_S,
) -> dict[str, Any]:
    """Download one certificate. Always consumes one rate-limit slot.

    The URL is normalized first so a ``#`` in the filename is sent as
    ``%23`` rather than dropped as a fragment. Returns pdf_bytes only when
    the body looks like a PDF and is under the size cap. 404 is
    ``not_found``. Anything else is ``error``.
    """
    url = normalize_certificate_url(url)
    # Reserve a slot before the socket opens so concurrent workers cannot
    # burst past the configured rate. The sleep, if any, happens inside
    # acquire(); this call returns once the caller is allowed to proceed.
    rate_limiter.acquire()
    result: dict[str, Any] = {
        "pdf_bytes": None,
        "status": ENRICH_ERROR,
        "error_detail": None,
        "review_reasons": "fetch_error",
    }
    try:
        response = session.get(url, timeout=timeout)
        if response.status_code == 404:
            result["status"] = ENRICH_NOT_FOUND
            result["review_reasons"] = "http_404"
            result["error_detail"] = "HTTP 404"
            return result
        response.raise_for_status()
        data = response.content or b""
        if len(data) > _MAX_PDF_BYTES:
            result["review_reasons"] = "pdf_too_large"
            result["error_detail"] = f"pdf_too_large: {len(data)} bytes"
            return result
        if b"%PDF" not in data[:1024]:
            result["review_reasons"] = "not_pdf"
            result["error_detail"] = "response body is not a PDF"
            return result
        result["pdf_bytes"] = data
        result["status"] = ENRICH_OK
        result["error_detail"] = None
        result["review_reasons"] = None
        return result
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "no response"
        result["error_detail"] = f"HTTPError: {code}"
        return result
    except Exception as exc:
        result["error_detail"] = f"{type(exc).__name__}: {exc}"
        return result


def _blank_row(
    *,
    natural_key: str | None,
    certificate_id: str | None,
    url: str | None,
    status: str,
    reasons: list[str],
    error_detail: str | None = None,
    confidence: float | None = 0.0,
) -> dict[str, Any]:
    reason_text = _reasons_text(reasons)
    return {
        "source_id": UPSTREAM_SOURCE_ID,
        "natural_key": natural_key,
        "certificate_id": certificate_id,
        "certificate_url": url,
        "assoc_mailing_address": None,
        "rep_name": None,
        "rep_mailing_address": None,
        "rep_phone": None,
        "rep_phone_normalized": None,
        "rep_email": None,
        "website": None,
        "field_6_raw": None,
        "enrich_status": status,
        "confidence": confidence,
        "needs_review": bool(set(reasons) & _REVIEW_REASON_SET),
        "review_reasons": reason_text,
        "ocr_text_sha256": None,
        "raw_pdf_sha256": None,
        "raw_pdf_byte_count": None,
        "raw_pdf_uri": None,
        "error_detail": error_detail,
    }


def process_certificate(
    row: dict[str, Any],
    *,
    session: requests.Session,
    rate_limiter: RateLimiter,
    dpi: int = _DEFAULT_DPI,
    land_raw: bool = True,
    run_date: str,
) -> dict[str, Any]:
    """Download, land, OCR, and parse one queue row.

    Exceptions become ``enrich_status='error'`` so one bad certificate
    cannot abort the batch.
    """
    natural_key = _clean_id(row.get("association_id"))
    certificate_id = _clean_id(row.get("certificate_id"))
    url = (row.get("url") or "").strip() or None
    try:
        return _process_certificate(
            natural_key=natural_key,
            certificate_id=certificate_id,
            url=url,
            session=session,
            rate_limiter=rate_limiter,
            dpi=dpi,
            land_raw=land_raw,
            run_date=run_date,
        )
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        return _blank_row(
            natural_key=natural_key,
            certificate_id=certificate_id,
            url=url,
            status=ENRICH_ERROR,
            reasons=["fetch_error"],
            error_detail=detail[:500],
        )


def _process_certificate(
    *,
    natural_key: str | None,
    certificate_id: str | None,
    url: str | None,
    session: requests.Session,
    rate_limiter: RateLimiter,
    dpi: int,
    land_raw: bool,
    run_date: str,
) -> dict[str, Any]:
    if not natural_key:
        return _blank_row(
            natural_key=None,
            certificate_id=certificate_id,
            url=url,
            status=ENRICH_SKIPPED,
            reasons=["missing_association_id"],
            confidence=None,
        )
    if not url:
        return _blank_row(
            natural_key=natural_key,
            certificate_id=certificate_id,
            url=None,
            status=ENRICH_SKIPPED,
            reasons=["missing_url"],
            confidence=None,
        )

    fetched = fetch_pdf(url, session, rate_limiter)
    if fetched["status"] != ENRICH_OK or not fetched["pdf_bytes"]:
        return _blank_row(
            natural_key=natural_key,
            certificate_id=certificate_id,
            url=url,
            status=fetched["status"],
            reasons=[fetched["review_reasons"] or "fetch_error"],
            error_detail=fetched["error_detail"],
        )

    pdf_bytes: bytes = fetched["pdf_bytes"]
    pdf_sha = raw_sha256(pdf_bytes)
    pdf_uri = None
    if land_raw:
        pdf_uri = upload_raw(
            CONNECTOR_ID, run_date, pdf_bytes, gzip=True, suffix="pdf",
        )

    extracted_text = _extract_text_pdfplumber(pdf_bytes)
    if len(extracted_text) >= _PDFPLUMBER_MIN_CHARS:
        ocr_text = extracted_text
    else:
        try:
            ocr_text = ocr_pdf(pdf_bytes, dpi=dpi)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            row = _blank_row(
                natural_key=natural_key,
                certificate_id=certificate_id,
                url=url,
                status=ENRICH_ERROR,
                reasons=["ocr_error"],
                error_detail=detail[:500],
            )
            row["raw_pdf_sha256"] = pdf_sha
            row["raw_pdf_byte_count"] = len(pdf_bytes)
            row["raw_pdf_uri"] = pdf_uri
            return row

    parsed = parse_certificate_text(ocr_text)
    ocr_sha = raw_sha256(ocr_text.encode("utf-8")) if ocr_text else None
    parsed.update({
        "source_id": UPSTREAM_SOURCE_ID,
        "natural_key": natural_key,
        "certificate_id": certificate_id,
        "certificate_url": url,
        "ocr_text_sha256": ocr_sha,
        "raw_pdf_sha256": pdf_sha,
        "raw_pdf_byte_count": len(pdf_bytes),
        "raw_pdf_uri": pdf_uri,
        "error_detail": None,
    })
    return parsed


# ---------------------------------------------------------------- batch


def assert_queue_shape(df: pd.DataFrame) -> None:
    missing = _QUEUE_REQUIRED - set(df.columns)
    if missing:
        raise ValueError(
            f"PDF queue is missing column(s) {sorted(missing)}. "
            "Expected the CSV from tx_trec_hoa.build_pdf_queue()."
        )


def _output_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Copy the queue and attach empty enrich columns (object dtype)."""
    out = df.copy()
    for column in _TEXT_COLUMNS:
        # certificate_id is both an input (from the queue) and an output.
        # Wiping it here would make every row look uncached.
        if column == "certificate_id" and column in out.columns:
            out[column] = out[column].astype("object")
            continue
        out[column] = pd.Series([None] * len(out), index=out.index, dtype="object")
    if "natural_key" not in out.columns:
        out["natural_key"] = pd.Series([None] * len(out), index=out.index, dtype="object")
    out["source_id"] = UPSTREAM_SOURCE_ID
    out["confidence"] = pd.Series([None] * len(out), index=out.index, dtype="object")
    out["needs_review"] = pd.Series([False] * len(out), index=out.index, dtype="object")
    out["raw_pdf_byte_count"] = pd.Series([None] * len(out), index=out.index, dtype="object")
    out["enrich_status"] = ENRICH_SKIPPED
    return out


def _write_result(df: pd.DataFrame, idx: Any, result: dict[str, Any]) -> None:
    for key, value in result.items():
        if key in df.columns:
            df.at[idx, key] = value


def enrich(
    df: pd.DataFrame,
    *,
    workers: int = 1,
    sleep_s: float = 1.0,
    dpi: int = _DEFAULT_DPI,
    land_raw: bool = True,
    run_date: str | None = None,
    session: requests.Session | None = None,
    skip_keys: dict[str, str] | None = None,
) -> pd.DataFrame:
    """OCR every queue row that has an association id and a URL.

    ``skip_keys`` maps natural_key → certificate_id for associations already
    stored with ``enrich_status='ok'``. A queue row is skipped only when its
    certificate_id matches the stored one; a new filing triggers a re-fetch.
    """
    assert_queue_shape(df)
    out = _output_frame(df)
    run_date = run_date or datetime.date.today().isoformat()
    skip_keys = skip_keys or {}

    eligible: list[tuple[Any, dict[str, Any]]] = []
    for idx, row in out.iterrows():
        natural_key = _clean_id(row.get("association_id"))
        certificate_id = _clean_id(row.get("certificate_id"))
        url = str(row.get("url") or "").strip()
        out.at[idx, "natural_key"] = natural_key
        out.at[idx, "certificate_id"] = certificate_id
        out.at[idx, "certificate_url"] = url or None

        if not natural_key:
            out.at[idx, "enrich_status"] = ENRICH_SKIPPED
            out.at[idx, "review_reasons"] = "missing_association_id"
            out.at[idx, "needs_review"] = False
            continue
        if not url:
            out.at[idx, "enrich_status"] = ENRICH_SKIPPED
            out.at[idx, "review_reasons"] = "missing_url"
            out.at[idx, "needs_review"] = False
            continue
        if natural_key and skip_keys.get(natural_key) == certificate_id:
            out.at[idx, "enrich_status"] = ENRICH_SKIPPED
            out.at[idx, "review_reasons"] = "cached"
            out.at[idx, "needs_review"] = False
            continue

        eligible.append((idx, {
            "association_id": natural_key,
            "certificate_id": certificate_id,
            "url": url,
        }))

    if not eligible:
        sys.stderr.write("  hoa pdf enrich: 0 eligible rows — nothing to fetch\n")
        return out

    sys.stderr.write(f"  hoa pdf enrich: {len(eligible):,} eligible rows\n")
    owned_session = session is None
    session = session or make_session()
    # Assignment, not setdefault. requests.Session already sets
    # User-Agent: python-requests/…, and setdefault leaves that value in
    # place. hoa.texas.gov returns HTTP 403 for the default requests UA.
    session.headers["User-Agent"] = USER_AGENT
    limiter = RateLimiter(sleep_s)

    def _one(row: dict[str, Any]) -> dict[str, Any]:
        return process_certificate(
            row,
            session=session,
            rate_limiter=limiter,
            dpi=dpi,
            land_raw=land_raw,
            run_date=run_date,
        )

    try:
        results = run_enrichment(
            eligible,
            fn=_one,
            workers=workers,
            label="hoa pdf",
            progress_interval=25,
        )
    finally:
        if owned_session:
            session.close()

    for idx, result in results:
        _write_result(out, idx, result)
    return out


def batch_exit_code(df: pd.DataFrame) -> int:
    """1 when every row that was actually attempted came back ``error``.

    A missing tesseract/poppler install, or a host that rejects every
    download, should fail the Cloud Run job. Cached skips are not attempts.
    A mix of ok / not_found / error still exits 0; those errors are stored
    and retried on the next run because only ``ok`` rows are cached.
    """
    if df.empty or "enrich_status" not in df.columns:
        return 0
    attempted = df[df["enrich_status"] != ENRICH_SKIPPED]
    if attempted.empty:
        return 0
    if bool((attempted["enrich_status"] == ENRICH_ERROR).all()):
        return 1
    return 0


def print_summary(df: pd.DataFrame) -> None:
    total = len(df)
    if "enrich_status" not in df.columns or total == 0:
        sys.stderr.write("\n  hoa pdf enrich summary: no rows\n")
        return

    def _count(status: str) -> int:
        return int((df["enrich_status"] == status).sum())

    review = int(df["needs_review"].fillna(False).astype(bool).sum()) if "needs_review" in df.columns else 0
    sys.stderr.write("\n  hoa pdf enrich summary:\n")
    sys.stderr.write(f"    total rows       {total:>7,}\n")
    sys.stderr.write(f"    enriched (ok)    {_count(ENRICH_OK):>7,}\n")
    sys.stderr.write(f"    not_found        {_count(ENRICH_NOT_FOUND):>7,}\n")
    sys.stderr.write(f"    error            {_count(ENRICH_ERROR):>7,}\n")
    sys.stderr.write(f"    skipped          {_count(ENRICH_SKIPPED):>7,}\n")
    sys.stderr.write(f"    needs_review     {review:>7,}\n")

    if total > 0 and "rep_phone" in df.columns:
        phone_filled = df["rep_phone"].notna().sum()
        email_filled = df["rep_email"].notna().sum()
        sys.stderr.write(
            f"    rep_phone        {phone_filled:>7,}  ({100 * phone_filled / total:.1f}% filled)\n"
        )
        sys.stderr.write(
            f"    rep_email        {email_filled:>7,}  ({100 * email_filled / total:.1f}% filled)\n"
        )


# ---------------------------------------------------------------- db


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except TypeError:
        pass
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _clean_db_value(column: str, value: Any) -> Any:
    if column == "needs_review":
        if _is_blank(value):
            return False
        return bool(value)
    if column == "confidence":
        if _is_blank(value):
            return None
        return round(float(value), 2)
    if _is_blank(value):
        return None
    return str(value).strip() if column != "needs_review" else value


def _upsert_sql() -> text:
    """INSERT ... ON CONFLICT that does not rewrite an unchanged row.

    ``enriched_at`` moves only when a payload column is actually different
    (``IS DISTINCT FROM``), so a re-run of the same OCR result is a no-op.
    """
    insert_cols = ", ".join(_UPSERT_COLUMNS)
    value_exprs = []
    for column in _UPSERT_COLUMNS:
        if column == "confidence":
            value_exprs.append("CAST(:confidence AS numeric)")
        else:
            value_exprs.append(f":{column}")
    values = ", ".join(value_exprs)
    mutable = [column for column in _UPSERT_COLUMNS if column not in {"source_id", "natural_key"}]
    assignments = ",\n            ".join(f"{column} = EXCLUDED.{column}" for column in mutable)
    distinct = "\n            OR ".join(
        f"staging.enrich_hoa_pdf_contact.{column} IS DISTINCT FROM EXCLUDED.{column}"
        for column in mutable
    )
    return text(f"""
        INSERT INTO staging.enrich_hoa_pdf_contact (
            {insert_cols}
        ) VALUES (
            {values}
        )
        ON CONFLICT (source_id, natural_key) DO UPDATE SET
            {assignments},
            enriched_at = now()
        WHERE {distinct}
    """)


def _row_rank(row: dict[str, Any]) -> tuple[int, float]:
    status = row.get("enrich_status") or ""
    confidence = row.get("confidence")
    try:
        score = float(confidence) if confidence is not None else 0.0
    except (TypeError, ValueError):
        score = 0.0
    return (_STATUS_RANK.get(status, 0), score)


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per (source_id, natural_key). Higher status, then confidence, wins.

    Postgres rejects an INSERT ... ON CONFLICT batch that touches the same
    conflict key twice ("cannot affect row a second time").
    """
    chosen: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["source_id"], row["natural_key"])
        current = chosen.get(key)
        if current is None or _row_rank(row) >= _row_rank(current):
            chosen[key] = row
    return list(chosen.values())


def upsert_enrich_hoa_pdf_contact(engine, df: pd.DataFrame) -> int:
    """Upsert parsed rows into staging.enrich_hoa_pdf_contact.

    Skipped rows are not written, so a cache hit cannot clobber a previous
    ``ok`` parse. Returns the number of rows passed to the database after
    intra-batch dedupe.
    """
    if df.empty or "enrich_status" not in df.columns:
        return 0

    eligible = df[df["enrich_status"] != ENRICH_SKIPPED]
    if eligible.empty:
        return 0

    rows: list[dict[str, Any]] = []
    for _, record in eligible.iterrows():
        natural_key = _clean_db_value("natural_key", record.get("natural_key"))
        if not natural_key:
            continue
        payload = {
            column: _clean_db_value(column, record.get(column))
            for column in _UPSERT_COLUMNS
        }
        payload["source_id"] = UPSTREAM_SOURCE_ID
        payload["natural_key"] = natural_key
        rows.append(payload)

    rows = _dedupe_rows(rows)
    if not rows:
        return 0

    with engine.begin() as conn:
        conn.execute(_upsert_sql(), rows)
    return len(rows)


def load_cached_keys(engine, source_id: str = UPSTREAM_SOURCE_ID) -> dict[str, str]:
    """Associations whose current certificate already parsed as ``ok``.

    Returns a dict mapping natural_key → certificate_id. The cache check in
    enrich() skips a queue row only when its certificate_id matches the stored
    one — so a new filing for the same association is always re-fetched.

    ``error`` and ``not_found`` are not cached: a later run retries them.
    ``needs_review`` rows are cached — re-OCR will not fix a template the
    parser already flagged, and the quarterly job should not rescan them
    until ``certificate_id`` changes or the caller passes ``--no-skip-cached``.
    """
    sql = text("""
        SELECT natural_key, certificate_id
        FROM staging.enrich_hoa_pdf_contact
        WHERE source_id = :source_id
          AND enrich_status = 'ok'
          AND certificate_id IS NOT NULL
    """)
    with engine.connect() as conn:
        result = conn.execute(sql, {"source_id": source_id})
        fetched = result.fetchall()
    cached: dict[str, str] = {}
    for natural_key, certificate_id in fetched:
        if natural_key and certificate_id:
            cached[str(natural_key)] = str(certificate_id)
    return cached


def build_manifest_bytes(df: pd.DataFrame) -> bytes:
    """JSON manifest of PDFs actually downloaded on this pass.

    This is the object recorded on ``ingest.source_run``. Each PDF is its
    own content-addressed object; the manifest points at them.
    """
    files = []
    if not df.empty and "raw_pdf_sha256" in df.columns:
        for _, row in df.iterrows():
            sha = row.get("raw_pdf_sha256")
            if _is_blank(sha):
                continue
            byte_count = row.get("raw_pdf_byte_count")
            try:
                if byte_count is None or pd.isna(byte_count):
                    byte_count_out = None
                else:
                    byte_count_out = int(byte_count)
            except (TypeError, ValueError):
                byte_count_out = None
            files.append({
                "natural_key": None if _is_blank(row.get("natural_key")) else str(row.get("natural_key")),
                "certificate_id": None if _is_blank(row.get("certificate_id")) else str(row.get("certificate_id")),
                "sha256": str(sha),
                "byte_count": byte_count_out,
                "uri": None if _is_blank(row.get("raw_pdf_uri")) else str(row.get("raw_pdf_uri")),
            })
    payload = {
        "connector": CONNECTOR_ID,
        "upstream_source_id": UPSTREAM_SOURCE_ID,
        "license": LICENSE,
        "pdfs": files,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def write_enrich_run(engine, df: pd.DataFrame, *, run_date: str) -> tuple[int, int]:
    """Land the manifest, open a source_run, upsert, and finish the run.

    Returns ``(source_run_id, rows_written)``.
    """
    from lib.db import finish_source_run, write_source_run

    manifest = build_manifest_bytes(df)
    raw_uri = upload_raw(CONNECTOR_ID, run_date, manifest, gzip=True, suffix="json")
    source_run_id = write_source_run(
        engine,
        source_id=CONNECTOR_ID,
        byte_count=len(manifest),
        sha256=raw_sha256(manifest),
        connector_version=CONNECTOR_VERSION,
        license_string=LICENSE,
        raw_uri=raw_uri,
    )
    try:
        written = upsert_enrich_hoa_pdf_contact(engine, df)
    except Exception:
        finish_source_run(engine, source_run_id, status="failed")
        raise
    finish_source_run(engine, source_run_id, status="succeeded", row_count=written)
    return source_run_id, written


# ---------------------------------------------------------------- entrypoint


def _filter_queue(df: pd.DataFrame, *, county: str | None, limit: int) -> pd.DataFrame:
    out = df
    if county:
        if "county_primary" not in out.columns:
            raise ValueError("--county was given but the queue has no county_primary column")
        wanted = county.strip().upper()
        out = out[out["county_primary"].astype(str).str.strip().str.upper() == wanted]
    if limit > 0:
        out = out.head(limit)
    return out.reset_index(drop=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--queue",
        required=True,
        help="Queue CSV from tx_trec_hoa.py --queue (association_id, certificate_id, url, priority).",
    )
    parser.add_argument(
        "--out",
        default="tx_trec_pdf_enriched.csv",
        help="Output CSV path (default: tx_trec_pdf_enriched.csv).",
    )
    parser.add_argument(
        "--write-db",
        action="store_true",
        help="Upsert into staging.enrich_hoa_pdf_contact (requires DATABASE_URL).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Thread pool size. Downloads stay at --sleep req/sec regardless (default: 1).",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="Minimum seconds between PDF downloads, shared across workers (default: 1.0).",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=_DEFAULT_DPI,
        help="Rasterization DPI for OCR (default: 300, matching the fax scans).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most this many queue rows after filtering (0 = all).",
    )
    parser.add_argument(
        "--county",
        default=None,
        help="Only process rows whose county_primary matches (e.g. HARRIS).",
    )
    parser.add_argument(
        "--skip-cached",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip certificates already stored as enrich_status=ok (default: on). "
             "Requires --write-db. --no-skip-cached reprocesses everything.",
    )
    parser.add_argument(
        "--land-raw",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Land each PDF via upload_raw (default: on). No-ops when DISABLE_GCS=1.",
    )
    args = parser.parse_args(argv)

    if args.workers < 1:
        sys.exit("ERROR: --workers must be >= 1")
    if args.sleep < 0:
        sys.exit("ERROR: --sleep must be >= 0")
    if args.limit < 0:
        sys.exit("ERROR: --limit must be >= 0")
    if args.dpi < 72:
        sys.exit("ERROR: --dpi must be >= 72")

    sys.stderr.write(f"  tx_trec_pdf_enrich: reading {args.queue}\n")
    queue = pd.read_csv(args.queue, dtype=str, keep_default_na=False)
    assert_queue_shape(queue)
    queue = _filter_queue(queue, county=args.county, limit=args.limit)
    sys.stderr.write(f"  tx_trec_pdf_enrich: {len(queue):,} rows after filters\n")

    skip_keys: dict[str, str] = {}
    engine = None
    if args.write_db:
        from lib.db import get_engine
        from lib.http import get_secret

        if not get_secret("DATABASE_URL"):
            sys.exit(
                "ERROR: --write-db was given but DATABASE_URL is not set. "
                "Copy .env.example -> .env and fill it in."
            )
        engine = get_engine()
        if args.skip_cached:
            try:
                skip_keys = load_cached_keys(engine)
                sys.stderr.write(
                    f"  tx_trec_pdf_enrich: {len(skip_keys):,} cached ok certificates\n"
                )
            except Exception as exc:
                sys.stderr.write(
                    f"  tx_trec_pdf_enrich: cache lookup failed ({exc}); processing all rows\n"
                )
                skip_keys = {}

    run_date = datetime.date.today().isoformat()
    enriched = enrich(
        queue,
        workers=args.workers,
        sleep_s=args.sleep,
        dpi=args.dpi,
        land_raw=args.land_raw,
        run_date=run_date,
        skip_keys=skip_keys,
    )
    print_summary(enriched)
    enriched.to_csv(args.out, index=False)
    sys.stderr.write(f"\n  wrote {len(enriched):,} rows -> {args.out}\n")

    if args.write_db and engine is not None:
        source_run_id, written = write_enrich_run(engine, enriched, run_date=run_date)
        sys.stderr.write(
            f"  tx_trec_pdf_enrich: wrote {written:,} rows to "
            f"staging.enrich_hoa_pdf_contact (source_run_id={source_run_id})\n"
        )

    code = batch_exit_code(enriched)
    if code:
        sys.stderr.write(
            "  tx_trec_pdf_enrich: every attempted row failed — "
            "check tesseract, poppler, and the certificate host\n"
        )
        sys.exit(code)


if __name__ == "__main__":
    main()
