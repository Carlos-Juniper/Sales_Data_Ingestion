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


def _is_empty(s) -> bool:
    """True for None, "", NaN, and pandas' pd.NA — without importing pandas.

    Callers commonly .map() these functions over a pandas "string"-dtype
    Series, whose missing entries are pd.NA. pd.NA.__bool__ deliberately
    raises TypeError (its truthiness is "ambiguous"), so a plain `if not s`
    crashes on it even though it plainly means "missing". Catching that one
    TypeError keeps this module pandas-free while still treating pd.NA as
    empty, same as None/NaN.
    """
    try:
        return not s
    except TypeError:
        return True


def normalize_name(s: str | None) -> str:
    """Casefold, strip punctuation and corporate suffixes, collapse whitespace."""
    if _is_empty(s):
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
    if _is_empty(s):
        return ""
    digits = _NON_DIGIT.sub("", str(s))
    return digits[:5] if len(digits) >= 5 else ""


def normalize_phone(s: str | None) -> str:
    """Strip everything but digits. Returns "" when result is under 10 digits."""
    if _is_empty(s):
        return ""
    digits = _NON_DIGIT.sub("", str(s))
    return digits if len(digits) >= 10 else ""


def int_key(v) -> str:
    """Convert a float-typed integer key (as returned by ArcGIS) to a clean string.
    Returns empty string for null/NaN values."""
    if v is None:
        return ""
    try:
        return str(int(v))
    except (ValueError, TypeError):
        return ""
