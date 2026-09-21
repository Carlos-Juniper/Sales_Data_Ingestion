"""
Deterministic entity keys (D1) — one implementation for all verticals.

Every resolved row is addressed by a content-derived SHA-256 key rather than a
serial id, so a re-run of the pipeline upserts onto the same row instead of
inserting a duplicate.  `core.account.account_key`, `core.location.location_key`
and `core.contact.contact_key` all carry UNIQUE indexes on these values.

Because the keys ARE the identity, the derivation is a compatibility surface: any
change to how a key is built re-identifies every affected row, which the core
3-way diff sees as "old account disappeared, new account appeared" and handles by
tombstoning one and inserting the other.  Treat edits here as a data migration.

--------------------------------------------------------------------------------
Known divergence between the two pre-existing verticals
--------------------------------------------------------------------------------
healthcare_pipeline.py and deathcare_merge.py grew their own copies of these
functions, and they are NOT byte-identical:

    key            healthcare                        deathcare
    account_key    normalize_name / normalize_zip    raw string values
    location_key   normalize_name(address) + zip     raw address + raw zip
    contact_key    normalized full_name              raw full_name

So the same logical record resolves to different keys depending on which vertical
processed it.  `normalize_parts` exists to reproduce either behaviour exactly:
pass True for healthcare semantics, False for deathcare semantics.  Both callers
still use their own inlined copies today; porting them here is a deliberate
key-migration, not a refactor, and needs a before/after diff against a populated
core.account.  New verticals should pass normalize_parts=True — normalizing is
the more correct behaviour, since it makes the key insensitive to punctuation and
casing noise that varies run to run.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Sequence

from lib.normalize import normalize_name, normalize_zip

# External identifier fields, in the order they win when several are present.
# Federal/state-issued identifiers outrank derived ones, and the more specific
# identifier outranks the more general.  Sourced from the plan's §5.1 Tier-1 table
# plus core.account.external_keys.
#
#   ccn            CMS Certification Number      healthcare
#   npi            Organizational NPI            healthcare
#   geoid          Census GEOID                  parks
#   leaid          NCES district id              parks (school districts, deferred)
#   ein            IRS Employer Id               deathcare
#   sunbiz_doc     FL corporate document no.     hoa
#   trec_assoc_id  TREC association id           hoa
#   license_no     State license number          deathcare, resort
DEFAULT_ACCOUNT_KEY_PRIORITY: tuple[str, ...] = (
    "ccn", "npi", "geoid", "leaid", "ein", "sunbiz_doc", "trec_assoc_id", "license_no",
)


def sha256_key(text_val: str) -> str:
    """Return the hex SHA-256 digest of a UTF-8 string."""
    return hashlib.sha256(text_val.encode("utf-8")).hexdigest()


def is_present(val: Any) -> bool:
    """
    True only when *val* carries real data.

    Rejects None, float NaN, the empty/whitespace string, and the literal string
    "nan".  That last case is not hypothetical: a pandas float column holding None
    becomes NaN, and str(NaN) is "nan", so a naive truthiness check treats missing
    data as the four-character string "nan" and happily hashes it into a key.
    """
    if val is None:
        return False
    if isinstance(val, float) and math.isnan(val):
        return False
    text = str(val).strip()
    return text != "" and text.lower() != "nan"


def compute_account_key(
    row: dict,
    *,
    priority: Sequence[str] = DEFAULT_ACCOUNT_KEY_PRIORITY,
    normalize_parts: bool = True,
) -> str:
    """
    Compute the deterministic account_key for one resolved row.

    Returns the hash of the first present external identifier in *priority*,
    falling back to normalized name + ZIP5.  Restrict *priority* to the identifiers
    a vertical actually carries: a vertical whose rows have no `ccn` column is
    unaffected by `ccn` being listed, but keeping the list tight documents intent
    and guards against a stray column silently taking over key derivation.

    See the module docstring on *normalize_parts*.
    """
    for field in priority:
        val = row.get(field)
        if is_present(val):
            return sha256_key(f"{field}:{str(val).strip()}")

    raw_name = row.get("name_normalized") or row.get("name_raw") or ""
    raw_zip = row.get("zip5") or ""
    if normalize_parts:
        name = normalize_name(str(raw_name))
        zip5 = normalize_zip(str(raw_zip))
    else:
        name = str(raw_name)
        zip5 = str(raw_zip)
    return sha256_key(f"name:{name}|zip:{zip5}")


def compute_location_key(
    account_key: str,
    row: dict,
    *,
    normalize_parts: bool = True,
    discriminator: str | None = None,
) -> str:
    """
    Compute the deterministic location_key.

    Derived from account_key + address + ZIP5 so that one account with several
    addresses yields distinct location rows.

    *discriminator* covers the case where address is not distinguishing.  Parks
    need it: a city's parks share one municipal account and almost all of them
    have no street address at all, so address+zip is identical (and empty) across
    every park in the city and all of them would collapse onto a single
    location_key.  Passing the park's own natural_key keeps them distinct.
    """
    raw_addr = row.get("address_line_1") or ""
    raw_zip = row.get("zip5") or ""
    if normalize_parts:
        addr = normalize_name(str(raw_addr))
        zip5 = normalize_zip(str(raw_zip))
    else:
        addr = str(raw_addr)
        zip5 = str(raw_zip)

    base = f"loc:{account_key}|{addr}|{zip5}"
    if discriminator is not None:
        base = f"{base}|{discriminator}"
    return sha256_key(base)


def compute_contact_key(
    account_key: str,
    role: str,
    full_name: str,
    *,
    normalize_parts: bool = True,
) -> str:
    """Compute the deterministic contact_key from account_key + role + name."""
    name = normalize_name(str(full_name or "")) if normalize_parts else str(full_name or "")
    return sha256_key(f"contact:{account_key}|{role}|{name}")
