"""
TX TREC management-certificate text parser.

Pure extraction logic — no I/O, no database, no HTTP.
Consumes the text extracted from a certificate PDF (via pdfplumber or OCR)
and returns structured contact fields anchored on the numbered labels from
the statutory template (Tex. Prop. Code §209.004):

  5. Name and mailing address of the Association
  6. Name, mailing address, phone number & email for designated representative
  7. Website address where all dedicatory instruments can be found

Public API: ``parse_certificate_text(text) -> dict``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from lib.enums import ENRICH_NOT_FOUND, ENRICH_OK
from lib.normalize import normalize_phone

# ---------------------------------------------------------------- constants

_MAX_FIELD_CHARS = 4000

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
        "needs_review": bool(set(reasons) & _REVIEW_REASON_SET),
        "review_reasons": _reasons_text(reasons),
    }


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
