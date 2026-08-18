"""
Text normalization shared across connectors.

Functions here are pure (str -> str); no pandas imports.
Apply with Series.map() or a list comprehension in the calling connector.
"""

from __future__ import annotations

import re

_CORP_SUFFIXES = re.compile(
    r"\b(INC|LLC|LTD|CORP|CO|PLC|LP|LLP|ASSN|ASSOC|ASSOCIATION)\b",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^A-Z0-9 ]")
_WHITESPACE = re.compile(r"\s+")
_NON_DIGIT = re.compile(r"\D")
_NON_PHONE_DIGIT = re.compile(r"[^\d]")


def normalize_name(s: str | None) -> str:
    """Casefold, strip punctuation and corporate suffixes, collapse whitespace."""
    if not s:
        return ""
    s = s.upper()
    s = _CORP_SUFFIXES.sub(" ", s)
    s = _NON_ALNUM.sub(" ", s)
    s = _WHITESPACE.sub(" ", s)
    return s.strip()


def normalize_zip(s: str | None) -> str:
    """
    Extract the first 5 digits from any ZIP / ZIP+4 / free-text string.

    Returns "" rather than raising on bad input. Result is always a string
    to prevent float round-trips from destroying leading zeros (07001 -> '7001.0').
    """
    if not s:
        return ""
    digits = _NON_DIGIT.sub("", str(s))
    return digits[:5] if len(digits) >= 5 else ""


def normalize_phone(s: str | None) -> str:
    """Strip everything but digits. Returns "" when result is under 10 digits."""
    if not s:
        return ""
    digits = _NON_PHONE_DIGIT.sub("", str(s))
    return digits if len(digits) >= 10 else ""
