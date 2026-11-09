"""
HOA vertical — merge and the resolved_* write.

Reads the TREC association registry (staging.tx_trec_hoa) plus the two
enrichment tables keyed on it — certificate OCR (staging.enrich_hoa_pdf_contact)
and Google Places (staging.enrich_hoa_gmaps) — and produces
staging.resolved_account / resolved_location / resolved_contact for
vertical='hoa'.

Grain
-----
One ACCOUNT per registered association, keyed on the TREC association id
(lib.keys `trec_assoc_id`), with one LOCATION carrying its city/ZIP/county.
TREC publishes no street address or coordinates, so locations are point-less
until a geocode or parcel join lands.

Contacts
--------
One `management_company` contact per association whose management certificate
OCR'd with confidence >= MIN_PDF_CONFIDENCE and yielded a valid phone or email.
The OCR's free-text fields are noisy (form labels bleed into the name field), so
a management name is kept only when it passes `clean_mgmt_name`; phone and email
are format-validated.  The management firm is NOT yet split into its own account
(docs/hoa-pipeline.md, migration 021 management_account_id) — that is Florida
plan work and needs firm-name dedup the OCR text cannot support yet.

Places precedence
-----------------
Per Ingestion-Plan-of-Action §5.3 a regulator filing outranks a Places guess:
Places only fills account phone/website/email the certificate did not supply.

CLI usage
---------
    python -m hoa.hoa_merge --dry-run
    python -m hoa.hoa_merge
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys

import pandas as pd

from lib.keys import compute_account_key, compute_contact_key, compute_location_key, is_present
from lib.normalize import normalize_phone
from lib.resolved_writer import replace_and_upsert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("hoa_merge")

VERTICAL = "hoa"
SOURCE_ID = "tx_trec_hoa"
PDF_SOURCE_ID = "tx_trec_pdf_enrich"
CONTACT_ROLE = "management_company"

# Certificate OCR at or above this confidence is trusted for free text and
# contacts.  ~80% of `ok` certificates clear it; below it the extracted fields
# are mostly form-label text or page footers.
MIN_PDF_CONFIDENCE = 0.7

_ACCOUNT_KEY_PRIORITY = ("trec_assoc_id",)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

# Phrases that only appear when the OCR captured the certificate's field label
# instead of the value next to it.
_LABEL_PHRASES = (
    "name and mailing",
    "mailing address",
    "managing",
    "representative",
    "other information",
    "website",
    "telephone",
    "format",
    "dedicatory",
    "manager of the association",
)
_LABEL_SEPARATOR_RE = re.compile(r":|\.,")
_CO_RE = re.compile(r"\s+c/o\b", re.IGNORECASE)
_ASSOC_IS_RE = re.compile(r"^\s*(the\s+)?association(['’]s)?\s+is\b[\s,;:]*", re.IGNORECASE)
_CITY_STATE_RE = re.compile(r",\s*(texas|tx)\.?$", re.IGNORECASE)
_NAME_PREFIX_RE = re.compile(r"^\s*(name|company|management company)\s*:\s*", re.IGNORECASE)
_MAX_NAME_LEN = 80

# Hosts that are mail providers, placeholders, or shared resident portals —
# never an association's own site.
_GENERIC_HOSTS = frozenset({
    "email.com", "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "townsq.io", "app.townsq.io", "www.townsq.io",
})


# ---------------------------------------------------------------- cleaning

def clean_mgmt_name(val) -> str | None:
    """
    Return a usable management-company name, or None when OCR captured only a label.

    The OCR often prefixes the value with the certificate's field label
    ("...designated representative of the Association is:, RealManage, LLC"),
    so when a label phrase is present the text after the last label separator
    (":" or "., ") is taken as the candidate.
    """
    if not is_present(val):
        return None
    name = str(val).split(":")[-1]
    if any(p in name.lower() for p in _LABEL_PHRASES):
        name = _LABEL_SEPARATOR_RE.split(name)[-1]
    name = _CO_RE.split(name)[0]
    name = _ASSOC_IS_RE.sub("", name)
    name = _NAME_PREFIX_RE.sub("", name).strip(" ,;:.")
    if not name or len(name) > _MAX_NAME_LEN:
        return None
    lowered = name.lower()
    if any(p in lowered for p in _LABEL_PHRASES) or lowered in ("is", "and") or lowered.endswith(" is"):
        return None
    # Street addresses and bare "City, Texas" lines land in the name field too.
    if name[0].isdigit() or _CITY_STATE_RE.search(name):
        return None
    if not any(ch.isalpha() for ch in name):
        return None
    return name


def clean_email(val) -> str | None:
    """Return a lower-cased email when it is well-formed, else None."""
    if not is_present(val):
        return None
    email = str(val).strip().lower()
    return email if _EMAIL_RE.match(email) else None


def clean_phone(val) -> str | None:
    """Return a 10-digit US phone, else None."""
    if not is_present(val):
        return None
    digits = normalize_phone(str(val))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def clean_website(val) -> str | None:
    """Return the first plausible association website, else None."""
    if not is_present(val):
        return None
    for part in str(val).split("|"):
        url = part.strip()
        host = re.sub(r"^https?://", "", url, flags=re.IGNORECASE).split("/")[0].lower()
        if "." not in host or host in _GENERIC_HOSTS:
            continue
        return url
    return None


# ---------------------------------------------------------------- loading

def load_sources(engine) -> pd.DataFrame:
    """
    Load one row per TREC association with its best certificate and Places rows.

    The certificate join keeps only `ok` extractions; when an association has
    several certificates the most recent wins.
    """
    from sqlalchemy import text

    sql = text("""
        WITH pdf AS (
            SELECT DISTINCT ON (natural_key)
                   natural_key, certificate_id, confidence AS pdf_confidence,
                   mgmt_name, mgmt_mailing_address, mgmt_phone_normalized,
                   mgmt_email, hoa_mailing_address, website AS pdf_website
            FROM staging.enrich_hoa_pdf_contact
            WHERE source_id = :src AND enrich_status = 'ok'
            ORDER BY natural_key, enriched_at DESC
        ),
        gmaps AS (
            SELECT natural_key, phone AS gmaps_phone, website AS gmaps_website,
                   contact_email AS gmaps_email
            FROM staging.enrich_hoa_gmaps
            WHERE source_id = :src AND enrich_status = 'ok'
        )
        SELECT h.source_id, h.natural_key, h.account_type, h.name_raw,
               h.name_normalized, h.city, h.state, h.zip5, h.county_fips,
               h.latitude, h.longitude, h.size_metric AS trec_type,
               pdf.certificate_id, pdf.pdf_confidence, pdf.mgmt_name,
               pdf.mgmt_mailing_address, pdf.mgmt_phone_normalized, pdf.mgmt_email,
               pdf.hoa_mailing_address, pdf.pdf_website,
               gmaps.gmaps_phone, gmaps.gmaps_website, gmaps.gmaps_email
        FROM staging.tx_trec_hoa h
        LEFT JOIN pdf   ON pdf.natural_key   = h.natural_key
        LEFT JOIN gmaps ON gmaps.natural_key = h.natural_key
        WHERE h.source_id = :src
    """)
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params={"src": SOURCE_ID})
    logger.info("loaded %d associations from staging.%s", len(df), SOURCE_ID)
    return df


def _trusted_pdf(row) -> bool:
    conf = row.get("pdf_confidence")
    return is_present(conf) and float(conf) >= MIN_PDF_CONFIDENCE


# ---------------------------------------------------------------- builders

def build_resolved_account(hoas: pd.DataFrame) -> pd.DataFrame:
    """One resolved_account row per association."""
    rows = []
    for row in hoas.to_dict(orient="records"):
        nk = str(row["natural_key"]).strip()
        account_key = compute_account_key({"trec_assoc_id": nk}, priority=_ACCOUNT_KEY_PRIORITY)
        trusted = _trusted_pdf(row)

        ext = {"trec_assoc_id": nk}
        if is_present(row.get("certificate_id")):
            ext["trec_certificate_id"] = str(row["certificate_id"])

        mailing = {f: str(row[f]) for f in ("city", "state", "zip5") if is_present(row.get(f))}
        if trusted and is_present(row.get("hoa_mailing_address")):
            mailing["address_raw"] = str(row["hoa_mailing_address"]).strip()

        website = (clean_website(row.get("pdf_website")) if trusted else None) or clean_website(
            row.get("gmaps_website")
        )

        rows.append({
            "account_key": account_key,
            "vertical": VERTICAL,
            "account_type": row.get("account_type") if is_present(row.get("account_type")) else "association",
            "legal_name": row.get("name_raw") if is_present(row.get("name_raw")) else row.get("name_normalized"),
            "name_normalized": row.get("name_normalized"),
            "dba_name": None,
            "parent_account_key": None,
            "mailing_address": json.dumps(mailing) if mailing else None,
            "phone": clean_phone(row.get("gmaps_phone")),
            "email": clean_email(row.get("gmaps_email")),
            "website": website,
            "status": "active",
            "external_keys": json.dumps(ext),
            "size_metric": None,
            "size_metric_unit": None,
            "confidence": None,
            "_source_id": SOURCE_ID,
            "_natural_key": nk,
        })
    return pd.DataFrame(rows)


def build_resolved_location(hoas: pd.DataFrame, account_df: pd.DataFrame) -> pd.DataFrame:
    """One resolved_location row per association — city/ZIP/county, no street address."""
    if account_df.empty:
        return pd.DataFrame()
    account_map = dict(zip(account_df["_natural_key"], account_df["account_key"]))

    rows = []
    for row in hoas.to_dict(orient="records"):
        nk = str(row["natural_key"]).strip()
        account_key = account_map[nk]
        site = {f: str(row[f]) for f in ("city", "state", "zip5") if is_present(row.get(f))}
        if is_present(row.get("county_fips")):
            site["county"] = str(row["county_fips"])
        lat, lon = row.get("latitude"), row.get("longitude")

        rows.append({
            "location_key": compute_location_key(
                account_key, {"zip5": row.get("zip5")}, discriminator=f"{SOURCE_ID}:{nk}"
            ),
            "account_key": account_key,
            "vertical": VERTICAL,
            "location_name": row.get("name_raw") if is_present(row.get("name_raw")) else None,
            "site_address": json.dumps(site) if site else None,
            "_latitude": float(lat) if is_present(lat) else None,
            "_longitude": float(lon) if is_present(lon) else None,
            "geocode_precision": None,
            "geometry_source": None,
            "maintained_acres": None,
            "acres_confidence": None,
            "site_type": row.get("trec_type") if is_present(row.get("trec_type")) else None,
        })
    return pd.DataFrame(rows)


def build_resolved_contact(hoas: pd.DataFrame, account_df: pd.DataFrame) -> pd.DataFrame:
    """One management_company contact per association with a trusted certificate."""
    if account_df.empty:
        return pd.DataFrame()
    account_map = dict(zip(account_df["_natural_key"], account_df["account_key"]))

    rows = []
    for row in hoas.to_dict(orient="records"):
        if not _trusted_pdf(row):
            continue
        phone = clean_phone(row.get("mgmt_phone_normalized"))
        email = clean_email(row.get("mgmt_email"))
        if not (phone or email):
            continue
        account_key = account_map[str(row["natural_key"]).strip()]
        name = clean_mgmt_name(row.get("mgmt_name"))
        address = row.get("mgmt_mailing_address")

        rows.append({
            "contact_key": compute_contact_key(account_key, CONTACT_ROLE, ""),
            "account_key": account_key,
            "vertical": VERTICAL,
            "full_name": name,
            "role": CONTACT_ROLE,
            "role_rank": 1,
            "phone": phone,
            "email": email,
            "address": json.dumps({"address_raw": str(address).strip()}) if is_present(address) else None,
            "source_id": PDF_SOURCE_ID,
            "is_current": True,
        })
    return pd.DataFrame(rows)


def print_summary(hoas: pd.DataFrame, account_df, location_df, contact_df) -> None:
    """Coverage summary to stderr."""
    n = len(account_df)
    if not n:
        return
    with_cert = int(hoas["pdf_confidence"].notna().sum())
    trusted = int((hoas["pdf_confidence"].fillna(0) >= MIN_PDF_CONFIDENCE).sum())
    named = int(contact_df["full_name"].notna().sum()) if not contact_df.empty else 0
    sys.stderr.write(
        f"\n  HOA merge summary\n"
        f"    associations           {n:>8,}\n"
        f"    with certificate OCR   {with_cert:>8,}\n"
        f"    trusted (conf >= {MIN_PDF_CONFIDENCE})  {trusted:>8,}\n"
        f"    mgmt contacts          {len(contact_df):>8,}  ({named:,} with a firm name)\n"
        f"    account phone (Places) {int(account_df['phone'].notna().sum()):>8,}\n"
        f"    account website        {int(account_df['website'].notna().sum()):>8,}\n"
        f"    locations              {len(location_df):>8,}\n\n"
    )


# ---------------------------------------------------------------- driver

def run_pipeline(engine, dry_run: bool = False) -> dict:
    """Execute the HOA merge and write staging.resolved_* for vertical='hoa'."""
    logger.info("=== HOA merge starting (dry_run=%s) ===", dry_run)

    hoas = load_sources(engine)
    if hoas.empty:
        logger.warning("no HOA rows in staging — nothing to merge")
        return {"source_rows": 0, "account": 0, "location": 0, "contact": 0}

    account_df = build_resolved_account(hoas)
    location_df = build_resolved_location(hoas, account_df)
    contact_df = build_resolved_contact(hoas, account_df)
    print_summary(hoas, account_df, location_df, contact_df)

    if dry_run:
        logger.info("DRY RUN — staging.resolved_* not written")
        return {
            "source_rows": len(hoas),
            "account": len(account_df),
            "location": len(location_df),
            "contact": len(contact_df),
        }

    n_account, n_location, n_contact = replace_and_upsert(
        engine, account_df, location_df, contact_df, vertical=VERTICAL
    )
    logger.info("=== HOA merge complete ===")
    return {
        "source_rows": len(hoas),
        "account": n_account,
        "location": n_location,
        "contact": n_contact,
    }


def main() -> None:
    """CLI entrypoint."""
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

    from lib.db import get_engine
    from lib.http import get_secret

    if not get_secret("DATABASE_URL"):
        sys.exit(
            "ERROR: DATABASE_URL is not set. Copy .env.example -> .env and fill it in."
        )
    run_pipeline(get_engine(), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
