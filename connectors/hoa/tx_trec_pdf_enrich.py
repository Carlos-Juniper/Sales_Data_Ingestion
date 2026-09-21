"""
TX TREC management-certificate PDF OCR enrichment.

Consumes the queue CSV from ``tx_trec_hoa.build_pdf_queue()`` and extracts
the contact fields that the CSV does not carry. Those fields live only in
the county-recorded certificate PDF linked from each row.

The certificates are scanned images (CCITT fax pages, no text layer). OCR
is the primary path, not a fallback. ``pdftotext`` / ``pdfplumber`` return
nothing on the dominant format.

Target fields, anchored on the numbered labels the statute's template uses
(Tex. Prop. Code §209.004). The Trinity Estates / Tarrant County certificate
is the reference layout:

  5. Name and mailing address of the Association
  6. Name, mailing address, phone number & email for designated representative
  7. Website address where all dedicatory instruments can be found

Extraction anchors on the literal ``5.`` / ``6.`` / ``7.`` labels. A
label-phrase fallback (the words "designated representative", and the field
5 / field 7 phrases) runs only when the number itself is missing, and that
row is always flagged ``needs_review``. Template diversity across counties
is handled by confidence + a review flag, not by a silent best guess.

``review.pending_pairs`` / ``match_queue`` is an entity-resolution pair
table (two source records). A low-confidence OCR parse is not a pair, so
the review queue is ``needs_review`` + ``review_reasons`` on
``staging.enrich_hoa_pdf_contact``, following the enrich_status convention
from ``irs_990_enrich.py``.

Writes ``staging.enrich_hoa_pdf_contact`` keyed on
``(source_id='tx_trec_hoa', natural_key=association_id)``. Does not write
``core.*`` and does not go through ``upsert_staging()`` — enrich tables are
not canonical staging tables. The upsert matches ``upsert_enrich_irs990``:
hand-written ``INSERT ... ON CONFLICT``, plus a ``WHERE ... IS DISTINCT
FROM`` guard so an unchanged re-run does not touch the row.

Raw PDFs are landed immutably at
``gs://<bucket>/tx_trec_pdf_enrich/<ingest_date>/<sha256>.pdf.gz``.
One ``ingest.source_run`` row records the pass; its payload is a JSON
manifest of those object URIs (the per-PDF bytes are the objects named in
the manifest, not the manifest itself).

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
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd
import requests
from sqlalchemy import text

from lib.enrich_runner import run_enrichment
from lib.enums import ENRICH_ERROR, ENRICH_NOT_FOUND, ENRICH_OK, ENRICH_SKIPPED
from lib.gcs import raw_sha256, upload_raw
from lib.http import make_session
from lib.normalize import normalize_phone

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

# PSM 4: single column of text of variable sizes. A certificate is one
# column of numbered fields, not a single uniform block (PSM 6) and not a
# newspaper (PSM 3).
_OCR_CONFIG = "--psm 4"
_DEFAULT_DPI = 300
_FETCH_TIMEOUT_S = 60
_MAX_PDF_BYTES = 25 * 1024 * 1024
_MAX_FIELD_CHARS = 4000

_QUEUE_REQUIRED = {"association_id", "url"}

# Line-start numbered labels. OCR often reads the dot as ')' or ':'.
_NUMBERED_ANCHOR = re.compile(
    r"(?m)^[ \t]*(?P<num>[5-8])[ \t]*[\.\)\:\,][ \t]*"
)

# Used only when the numbered label is missing. Always sets needs_review.
_LABEL_FALLBACKS: dict[int, re.Pattern[str]] = {
    5: re.compile(r"name and mailing address of the association", re.I),
    6: re.compile(r"designated representative", re.I),
    7: re.compile(r"website address|dedicatory instruments", re.I),
}

_LABEL_HINTS: dict[int, re.Pattern[str]] = {
    5: re.compile(r"association", re.I),
    6: re.compile(r"designated|representative|e-?mail", re.I),
    7: re.compile(r"website|dedicatory", re.I),
    8: re.compile(r"other information|signature", re.I),
}

# Prefix stripped when the label and the value share a line. Applied to the
# text *after* the "5." token, so the pattern does not include the number.
_LABEL_STRIPS: dict[int, re.Pattern[str]] = {
    5: re.compile(
        r"^(?:name\s+and\s+mailing\s+address\s+of\s+the\s+association)\s*:?\s*",
        re.I,
    ),
    6: re.compile(
        r"^(?:name\s*,?\s*mailing\s+address\s*,?\s*phone\s+number"
        r"(?:\s*(?:&|and)\s*e-?mail)?"
        r"(?:\s+for(?:\s+the)?\s+designated\s+representative)?)\s*:?\s*",
        re.I,
    ),
    7: re.compile(
        r"^(?:website\s+address"
        r"(?:\s+where\s+all\s+dedicatory\s+instruments\s+can\s+be\s+found)?)\s*:?\s*",
        re.I,
    ),
}

_EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
# Separators allow a single OCR-inserted space around the dot/dash
# ("855. 289. 6007") without swallowing the next digit run.
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.\-]{0,2})?(?:\(\s*\d{3}\s*\)|\d{3})"
    r"[\s.\-]{0,2}\d{3}[\s.\-]{0,2}\d{4}(?!\d)"
)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
_BARE_DOMAIN_RE = re.compile(
    r"\b(?:[a-z0-9-]+\.)+(?:com|org|net|io|us|gov|info|biz)\b",
    re.I,
)
_PO_BOX_RE = re.compile(r"\bP\.?\s*O\.?\s*BOX\b", re.I)
_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_STATE_ZIP_RE = re.compile(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b")
_CITY_STATE_RE = re.compile(r",\s*[A-Z]{2}\s*$")
_STREET_RE = re.compile(r"^\d+\s+\S")
_CARE_OF_RE = re.compile(r"^(?:c/o|attn)\b", re.I)

# Reasons that put a row on the review queue. ``cached`` / ``missing_url`` /
# ``missing_association_id`` are skip reasons and are not in this set.
_REVIEW_REASON_SET = {
    "missing_field_5_anchor",
    "missing_field_6_anchor",
    "field_5_label_fallback",
    "field_6_label_fallback",
    "missing_phone",
    "missing_email",
    "anchor_order",
    "empty_ocr",
    "http_404",
    "fetch_error",
    "ocr_error",
    "not_pdf",
    "pdf_too_large",
    "value_truncated",
}

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


# ---------------------------------------------------------------- rate limit


class RateLimiter:
    """Process-wide minimum interval between acquisitions.

    Sleep happens outside the lock so one slow waiter does not block the
    bookkeeping, but the next-allowed timestamp is reserved under the lock.
    ``workers`` therefore cannot multiply the request rate: four threads
    with ``sleep_s=1`` still issue one download per second.
    """

    def __init__(
        self,
        min_interval_s: float,
        *,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.min_interval_s = max(0.0, float(min_interval_s))
        self._clock = clock or time.monotonic
        self._sleep = sleeper or time.sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        if self.min_interval_s <= 0:
            return
        with self._lock:
            now = self._clock()
            wait = self._next - now
            if wait < 0:
                wait = 0.0
            base = now if wait == 0 else self._next
            self._next = base + self.min_interval_s
        if wait > 0:
            self._sleep(wait)


# ---------------------------------------------------------------- anchors


@dataclass(frozen=True)
class _Anchor:
    num: int
    pos: int
    value_start: int
    kind: str  # "numbered" | "label"


def _line_start(text: str, index: int) -> int:
    newline = text.rfind("\n", 0, index)
    return 0 if newline < 0 else newline + 1


def _locate_anchors(text: str) -> dict[int, _Anchor]:
    found: dict[int, _Anchor] = {}
    for match in _NUMBERED_ANCHOR.finditer(text):
        num = int(match.group("num"))
        if num not in found:
            found[num] = _Anchor(num, match.start(), match.end(), "numbered")
    for num, pattern in _LABEL_FALLBACKS.items():
        if num in found:
            continue
        match = pattern.search(text)
        if match:
            # End the previous field at the start of this label's line so the
            # label words themselves are not appended to the previous value.
            found[num] = _Anchor(num, _line_start(text, match.start()), match.end(), "label")
    return found


def _has_value_signal(line: str) -> bool:
    return bool(
        _PHONE_RE.search(line)
        or _EMAIL_RE.search(line)
        or _ZIP_RE.search(line)
        or _URL_RE.search(line)
    )


def _strip_field_label(raw: str, num: int) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    lines = raw.splitlines()
    first = lines[0].strip()
    hint = _LABEL_HINTS.get(num)
    if (
        hint
        and hint.search(first)
        and not _has_value_signal(first)
        and len(lines) > 1
    ):
        return "\n".join(lines[1:]).strip()
    stripper = _LABEL_STRIPS.get(num)
    if stripper:
        stripped = stripper.sub("", raw, count=1).strip()
        if stripped != raw.strip():
            # The label was the whole block — return empty rather than the label.
            return stripped
    return raw


def _blocks_from_anchors(text: str, anchors: dict[int, _Anchor]) -> dict[int, str]:
    ordered = sorted(anchors.values(), key=lambda anchor: anchor.pos)
    blocks: dict[int, str] = {}
    for index, anchor in enumerate(ordered):
        end = ordered[index + 1].pos if index + 1 < len(ordered) else len(text)
        if anchor.value_start >= end:
            raw = ""
        else:
            raw = text[anchor.value_start:end]
        blocks[anchor.num] = _strip_field_label(raw, anchor.num)
    return blocks


def _truncate(value: str | None) -> tuple[str | None, bool]:
    if not value:
        return None, False
    if len(value) <= _MAX_FIELD_CHARS:
        return value, False
    return value[:_MAX_FIELD_CHARS], True


def _oneline(parts: list[str]) -> str | None:
    cleaned = [re.sub(r"\s+", " ", part).strip(" ,;") for part in parts]
    cleaned = [part for part in cleaned if part]
    if not cleaned:
        return None
    return ", ".join(cleaned)


def _looks_like_address(part: str) -> bool:
    text = part.strip()
    if not text:
        return False
    if _PO_BOX_RE.search(text):
        return True
    if _ZIP_RE.search(text):
        return True
    if _STATE_ZIP_RE.search(text):
        return True
    if _CITY_STATE_RE.search(text):
        return True
    if _STREET_RE.match(text):
        return True
    if _CARE_OF_RE.match(text):
        return True
    if re.fullmatch(r"[A-Z]{2}", text):
        return True
    return False


def _split_parts(cleaned: str) -> list[str]:
    lines = [line.strip(" ,;") for line in cleaned.splitlines() if line.strip(" ,;")]
    if len(lines) <= 1:
        blob = lines[0] if lines else ""
        return [part.strip(" ,;") for part in blob.split(",") if part.strip(" ,;")]
    return lines


def _name_and_address(parts: list[str]) -> tuple[str | None, str | None]:
    name_parts: list[str] = []
    addr_parts: list[str] = []
    seen_address = False
    for part in parts:
        if not seen_address and not _looks_like_address(part):
            name_parts.append(part)
        else:
            seen_address = True
            addr_parts.append(part)
    return _oneline(name_parts), _oneline(addr_parts)


def _join_assoc(block: str | None) -> str | None:
    if not block or not block.strip():
        return None
    lines = [re.sub(r"\s+", " ", line).strip(" ,;") for line in block.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return None
    return ", ".join(lines)


def _trim_url(url: str) -> str:
    return url.rstrip(").,;>'\"")


def _extract_website(block: str | None) -> str | None:
    if not block or not block.strip():
        return None
    urls: list[str] = []
    for match in _URL_RE.finditer(block):
        urls.append(_trim_url(match.group(0)))
    if not urls:
        for match in _BARE_DOMAIN_RE.finditer(block):
            urls.append(match.group(0))
    deduped: list[str] = []
    for url in urls:
        if url and url not in deduped:
            deduped.append(url)
    if not deduped:
        return None
    return " | ".join(deduped)


def _split_rep(block: str | None) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "name": None,
        "address": None,
        "phone": None,
        "phone_normalized": None,
        "email": None,
        "raw": None,
    }
    if not block or not block.strip():
        return empty
    raw = block.strip()
    email_match = _EMAIL_RE.search(raw)
    email = email_match.group(0).strip(".,;:") if email_match else None
    phone_match = _PHONE_RE.search(raw)
    phone = phone_match.group(0).strip() if phone_match else None
    phone_normalized = normalize_phone(phone) or None

    cleaned = raw
    if email_match:
        cleaned = cleaned[: email_match.start()] + " " + cleaned[email_match.end() :]
    phone_after = _PHONE_RE.search(cleaned)
    if phone_after:
        cleaned = cleaned[: phone_after.start()] + " " + cleaned[phone_after.end() :]

    name, address = _name_and_address(_split_parts(cleaned))
    raw_stored, truncated = _truncate(re.sub(r"[ \t]+", " ", raw))
    return {
        "name": name,
        "address": address,
        "phone": phone,
        "phone_normalized": phone_normalized,
        "email": email,
        "raw": raw_stored,
        "truncated": truncated,
    }


def _confidence(
    anchors: dict[int, _Anchor],
    *,
    assoc: str | None,
    rep: dict[str, Any],
    website: str | None,
    order_bad: bool,
) -> float:
    score = 0.0
    field_5 = anchors.get(5)
    field_6 = anchors.get(6)
    if field_5 is not None and field_5.kind == "numbered":
        score += 0.35
    elif field_5 is not None:
        score += 0.20
    if field_6 is not None and field_6.kind == "numbered":
        score += 0.35
    elif field_6 is not None:
        score += 0.20
    if rep.get("phone"):
        score += 0.10
    if rep.get("email"):
        score += 0.10
    if website:
        score += 0.05
    if assoc:
        score += 0.05
    if order_bad:
        score -= 0.15
    return round(max(0.0, min(1.0, score)), 2)


def _reasons_text(reasons: list[str]) -> str | None:
    if not reasons:
        return None
    # Preserve first-seen order; drop duplicates.
    seen: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.append(reason)
    return ";".join(seen)


def parse_certificate_text(text: str | None) -> dict[str, Any]:
    """Extract fields 5–7 from OCR text.

    Returns enrich_status ``ok`` when a field-5 or field-6 anchor was found
    (numbered or label fallback). ``not_found`` when neither anchor exists.
    ``needs_review`` is true whenever a review reason was recorded — missing
    numbered anchors, label fallback, missing phone/email inside field 6,
    or anchors out of numeric order.
    """
    reasons: list[str] = []
    if text is None or not str(text).strip():
        return _parse_result(
            status=ENRICH_NOT_FOUND,
            reasons=["empty_ocr"],
            confidence=0.0,
        )

    anchors = _locate_anchors(text)
    field_5 = anchors.get(5)
    field_6 = anchors.get(6)

    if field_5 is None:
        reasons.append("missing_field_5_anchor")
    elif field_5.kind == "label":
        reasons.append("missing_field_5_anchor")
        reasons.append("field_5_label_fallback")

    if field_6 is None:
        reasons.append("missing_field_6_anchor")
    elif field_6.kind == "label":
        reasons.append("missing_field_6_anchor")
        reasons.append("field_6_label_fallback")

    ordered_nums = [anchor.num for anchor in sorted(anchors.values(), key=lambda a: a.pos)]
    order_bad = ordered_nums != sorted(ordered_nums)
    if order_bad:
        reasons.append("anchor_order")

    blocks = _blocks_from_anchors(text, anchors)
    assoc, assoc_truncated = _truncate(_join_assoc(blocks.get(5)) if 5 in anchors else None)
    rep = _split_rep(blocks.get(6)) if 6 in anchors else _split_rep(None)
    website, web_truncated = _truncate(_extract_website(blocks.get(7)) if 7 in anchors else None)
    if assoc_truncated or web_truncated:
        reasons.append("value_truncated")

    if 6 in anchors:
        if not rep.get("phone"):
            reasons.append("missing_phone")
        if not rep.get("email"):
            reasons.append("missing_email")
    if rep.get("truncated"):
        reasons.append("value_truncated")

    status = ENRICH_OK if (5 in anchors or 6 in anchors) else ENRICH_NOT_FOUND
    confidence = _confidence(
        anchors,
        assoc=assoc,
        rep=rep,
        website=website,
        order_bad=order_bad,
    )
    return _parse_result(
        status=status,
        reasons=reasons,
        confidence=confidence,
        assoc=assoc,
        rep=rep,
        website=website,
    )


def _parse_result(
    *,
    status: str,
    reasons: list[str],
    confidence: float,
    assoc: str | None = None,
    rep: dict[str, Any] | None = None,
    website: str | None = None,
) -> dict[str, Any]:
    rep = rep or _split_rep(None)
    reason_text = _reasons_text(reasons)
    review_reasons = {
        reason for reason in (reason_text or "").split(";") if reason
    }
    return {
        "assoc_mailing_address": assoc,
        "rep_name": rep.get("name"),
        "rep_mailing_address": rep.get("address"),
        "rep_phone": rep.get("phone"),
        "rep_phone_normalized": rep.get("phone_normalized"),
        "rep_email": rep.get("email"),
        "website": website,
        "field_6_raw": rep.get("raw"),
        "enrich_status": status,
        "confidence": confidence,
        "needs_review": bool(review_reasons & _REVIEW_REASON_SET),
        "review_reasons": reason_text,
    }


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


def ocr_pdf(pdf_bytes: bytes, *, dpi: int = _DEFAULT_DPI) -> str:
    """Render every page and OCR it. Page 1 usually holds fields 1–8; later
    pages are still read because filings do not share a page count.

    Imports pdf2image and pytesseract lazily so unit tests of the parser
    do not require a tesseract binary or poppler.
    """
    try:
        from pdf2image import convert_from_bytes
        import pytesseract
    except ImportError as exc:
        raise RuntimeError(
            "OCR dependencies are not installed (pdf2image, pytesseract) "
            "or poppler/tesseract is missing from the image"
        ) from exc

    images = convert_from_bytes(pdf_bytes, dpi=dpi)
    if not images:
        return ""
    pages = []
    for image in images:
        page_text = pytesseract.image_to_string(
            image, lang="eng", config=_OCR_CONFIG,
        )
        pages.append(page_text or "")
    return "\n\n".join(pages).strip()


def fetch_pdf(
    url: str,
    session: requests.Session,
    rate_limiter: RateLimiter,
    *,
    timeout: float = _FETCH_TIMEOUT_S,
) -> dict[str, Any]:
    """Download one certificate. Always consumes one rate-limit slot.

    Returns pdf_bytes only when the body looks like a PDF and is under the
    size cap. 404 is ``not_found``. Anything else is ``error``.
    """
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
    review_set = {r for r in (reason_text or "").split(";") if r}
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
        "needs_review": bool(review_set & _REVIEW_REASON_SET),
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
    skip_keys: set[tuple[str, str]] | None = None,
) -> pd.DataFrame:
    """OCR every queue row that has an association id and a URL.

    ``skip_keys`` is a set of ``(natural_key, certificate_id)`` already
    stored with ``enrich_status='ok'`` for this parser generation. Those
    rows are marked ``skipped`` / ``cached`` and are not downloaded again.
    """
    assert_queue_shape(df)
    out = _output_frame(df)
    run_date = run_date or datetime.date.today().isoformat()
    skip_keys = skip_keys or set()

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
        if certificate_id and (natural_key, certificate_id) in skip_keys:
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
    session.headers.setdefault("User-Agent", USER_AGENT)
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


def load_cached_keys(engine, source_id: str = UPSTREAM_SOURCE_ID) -> set[tuple[str, str]]:
    """Associations whose current certificate already parsed as ``ok``.

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
    keys: set[tuple[str, str]] = set()
    for natural_key, certificate_id in fetched:
        if natural_key and certificate_id:
            keys.add((str(natural_key), str(certificate_id)))
    return keys


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

    skip_keys: set[tuple[str, str]] = set()
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
                skip_keys = set()

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
