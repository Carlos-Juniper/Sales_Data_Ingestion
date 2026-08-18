"""
TX TREC — HOA/POA/COA management certificates.

Source : https://data.texas.gov/dataset/TREC-HOA-Management-Certificates/8auc-hzdi
CSV    : https://data.texas.gov/api/views/8auc-hzdi/rows.csv?accessType=DOWNLOAD
Portal : https://hoa.texas.gov/management-certificates-search
Statute: Tex. Prop. Code Sec. 209.004 — filing is mandatory for Ch. 209 associations.
License: Texas public records — free to store and use commercially.

Verified against a real extract on 2026-08-17: 17,071 rows, 6 columns.

WHAT THIS CONNECTOR DOES AND DOES NOT GIVE YOU
----------------------------------------------
Gives you : a clean 1:1 statewide registry of 17,071 associations with a stable
            numeric association ID, name, type, city, and ZIP. Almost no
            self-dedup needed (exactly 1 duplicate ID in the whole file).

Does NOT give you: street address, mailing address, managing agent, phone, or
            email. All of it lives inside the linked county-recorded PDF. The
            CSV alone is a target list with no way to contact anyone on it, so
            stage 2 (`build_pdf_queue`) is the gate for this vertical, not a
            nice-to-have.

Two field-level traps, both handled below:
  * `County` is 15.1% unusable — 1,535 rows say "TX", 886 say "Texas", and the
    rest spans 299 distinct values against Texas's 254 real counties. Derive
    county from ZIP5 instead; keep the source value only as a cross-check.
  * `Name` must NOT be filtered on. 6.4% of names lack any association keyword
    ("Afton Oaks Civic Club", "Rocky Creek Maintenance Corp.") and are still
    valid targets. A name filter silently drops ~1,100 associations.

Usage:
    python tx_trec_hoa.py TREC_HOA_Management_Certificates_*.csv \
        --out tx_hoa.csv --queue pdf_queue.csv
"""

from __future__ import annotations

import argparse
import glob
import re
import sys

import pandas as pd

from lib.normalize import normalize_name, normalize_zip

SOURCE_ID = "tx_trec_hoa"
VERTICAL = "hoa"

CERT_URL_RE = re.compile(
    r"/certificates/(?P<association_id>\d+)/(?P<certificate_id>[^/]+)/(?P<kind>[^/]+)/"
)

# Values that appear in the County column but carry no county information.
COUNTY_JUNK = {"TX", "TEXAS", "N/A", "NA", "", "NONE"}

# Metro-first ordering for the PDF pass. The four big metros are >60% of the
# file, so working them first yields usable territory coverage long before the
# full 17k completes.
COUNTY_PRIORITY = [
    "HARRIS", "DALLAS", "TRAVIS", "BEXAR", "COLLIN", "TARRANT",
    "WILLIAMSON", "DENTON", "MONTGOMERY", "FORT BEND",
]

TYPE_MAP = {
    "POA": "property_owners_association",
    "HOA": "homeowners_association",
    "COA": "condominium_owners_association",
}


# ---------------------------------------------------------------- extract

def load_raw(paths: list[str]) -> pd.DataFrame:
    frames = []
    for p in paths:
        df = pd.read_csv(p, dtype=str, keep_default_na=False)
        df["_source_file"] = p.rsplit("/", 1)[-1]
        frames.append(df)
        print(f"  read {p}: {len(df):,} rows", file=sys.stderr)
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------- transform

def normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    ids = df["Certificate"].str.extract(CERT_URL_RE)
    df["association_id"] = ids["association_id"]
    df["certificate_id"] = ids["certificate_id"]

    df["name_normalized"] = df["Name"].map(normalize_name)

    df["city_normalized"] = df["City"].map(normalize_name)

    # 99.5% yield. The 79 failures hold junk like "TX", "Travis", "2008".
    # Kept as a string throughout — a float round-trip turns 78133 into 78133.0
    # and silently destroys any leading zero.
    df["zip5"] = df["Zip"].map(normalize_zip)

    county_raw = df["County"].str.upper().str.strip()
    df["county_source"] = county_raw
    df["county_usable"] = ~county_raw.isin(COUNTY_JUNK) & ~county_raw.str.fullmatch(r"\d+")
    # Multi-county entries are real signal: master-planned communities straddling
    # a line, which correlates with acreage. Keep the flag, don't discard.
    df["is_multi_county"] = county_raw.str.contains(r"[/&]|\bAND\b", regex=True, na=False)
    df["county_primary"] = (
        county_raw.where(df["county_usable"])
        .str.split(r"[/&]|\bAND\b", regex=True).str[0].str.strip()
    )
    # ~15% of rows end up with no county from the source field. Backfill from
    # ZIP5 using the Census ZCTA-to-county relationship file, which is the
    # authoritative crosswalk and free:
    #   https://www.census.gov/geographies/reference-files/time-series/geo/relationship-files.html
    # Load it as {zip5: county_name} and pass it in via --zcta-crosswalk.
    df["county_source_is_derived"] = False

    df["association_type"] = df["Type"].str.upper().map(TYPE_MAP).fillna("unknown")
    return df


def report_quality(df: pd.DataFrame) -> None:
    print("\n  quality:", file=sys.stderr)
    n = len(df)
    print(f"    rows                        {n:>8,}", file=sys.stderr)
    print(f"    distinct association_id     {df['association_id'].nunique():>8,}", file=sys.stderr)
    dup = n - df["association_id"].nunique()
    print(f"    duplicate ids               {dup:>8,}", file=sys.stderr)
    has_zip = df["zip5"] != ""
    print(f"    zip5 resolved               {has_zip.sum():>8,}"
          f"  ({100 * has_zip.mean():.1f}%)", file=sys.stderr)
    print(f"    county usable               {df['county_usable'].sum():>8,}"
          f"  ({100 * df['county_usable'].mean():.1f}%)", file=sys.stderr)
    print(f"    multi-county (large MPCs)   {df['is_multi_county'].sum():>8,}", file=sys.stderr)
    print("\n    by type:", file=sys.stderr)
    for t, c in df["association_type"].value_counts().items():
        print(f"      {t:<34} {c:>7,}", file=sys.stderr)


def to_canonical(df: pd.DataFrame) -> pd.DataFrame:
    """
    Map to core.account per Sec. 4 of the plan.

    Deliberately absent: street address, phone, email, managing agent. Those
    columns are created by the PDF pass, not by this connector. They are emitted
    as empty here so the downstream schema is stable either way.
    """
    return pd.DataFrame({
        "source_id": SOURCE_ID,
        "natural_key": df["association_id"],
        "vertical": VERTICAL,
        "account_type": "association",
        "legal_name": df["Name"],
        "name_normalized": df["name_normalized"],
        "association_type": df["association_type"],
        "site_city": df["city_normalized"],
        "site_state": "TX",
        "site_zip": df["zip5"],
        "county_primary": df["county_primary"],
        "is_multi_county": df["is_multi_county"],
        "trec_assoc_id": df["association_id"],
        "trec_certificate_id": df["certificate_id"],
        "certificate_url": df["Certificate"],
        # Populated by the PDF pass. See build_pdf_queue().
        "site_street": pd.NA,
        "mailing_address": pd.NA,
        "phone": pd.NA,
        "email": pd.NA,
        "managing_agent": pd.NA,
        "contact_status": "pending_pdf",
        # ZIP centroid only until the PDF yields a street address. Do not pin
        # this on a sales map as though it were the property.
        "geocode_status": "zip_centroid_only",
    })


# ---------------------------------------------------------------- pdf queue

def backfill_county_from_zip(df: pd.DataFrame, crosswalk_path: str | None) -> pd.DataFrame:
    """Fill county_primary from ZIP5 where the source County field was junk."""
    if not crosswalk_path:
        return df
    xw = pd.read_csv(crosswalk_path, dtype=str, keep_default_na=False)
    zip_col = next(c for c in xw.columns if "zcta" in c.lower() or "zip" in c.lower())
    cty_col = next(c for c in xw.columns if "county" in c.lower() and "fips" not in c.lower())
    m = dict(zip(xw[zip_col].str[:5], xw[cty_col].str.upper().str.replace(" COUNTY", "")))

    missing = df["county_primary"].isna() | (df["county_primary"] == "")
    filled = df.loc[missing, "zip5"].map(m)
    df.loc[missing, "county_primary"] = filled
    df.loc[missing & filled.notna(), "county_source_is_derived"] = True
    print(f"    county backfilled from ZIP     {filled.notna().sum():>8,}", file=sys.stderr)
    return df


def build_pdf_queue(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ordered work queue for the certificate fetch/parse pass.

    Metro counties first so territory coverage lands early. 17,071 PDFs at a
    polite 1 req/sec is ~5 hours, so ordering matters for time-to-first-value,
    not for total cost.
    """
    rank = {c: i for i, c in enumerate(COUNTY_PRIORITY)}
    q = df.assign(
        priority=df["county_primary"].map(rank).fillna(len(COUNTY_PRIORITY)).astype(int)
    ).sort_values(["priority", "county_primary", "Name"])

    out = q[[
        "association_id", "certificate_id", "Certificate",
        "Name", "county_primary", "city_normalized", "zip5", "priority",
    ]].rename(columns={"Certificate": "url", "city_normalized": "city"})
    out["fetch_status"] = "pending"
    out["parse_status"] = "pending"
    return out


# ---------------------------------------------------------------- checks

def assert_source_shape(df: pd.DataFrame) -> None:
    expected = {"Name", "County", "City", "Zip", "Type", "Certificate"}
    missing = expected - set(df.columns)
    if missing:
        raise ValueError(f"missing column(s) {missing} — Socrata layout changed")

    for col in ("Name", "Type", "Certificate"):
        fill = (df[col].str.strip() != "").mean()
        if not fill > 0.99:
            raise ValueError(f"{col} only {fill:.1%} populated")

    parsed = df["Certificate"].str.extract(CERT_URL_RE)["association_id"].notna().mean()
    if not parsed > 0.98:
        raise ValueError(
            f"only {parsed:.1%} of certificate URLs match the expected "
            "/certificates/{id}/{cert}/mc/ shape — URL scheme changed"
        )

    unknown = set(df["Type"].str.upper().unique()) - set(TYPE_MAP)
    if unknown:
        raise ValueError(f"unmapped Type value(s): {unknown}")


# ---------------------------------------------------------------- entrypoint

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--out", default="tx_trec_hoa.csv")
    ap.add_argument("--queue", default="tx_trec_pdf_queue.csv")
    ap.add_argument("--zcta-crosswalk", default=None,
                    help="Census ZCTA-to-county relationship file, to backfill county from ZIP")
    args = ap.parse_args()

    paths = sorted({p for pat in args.paths for p in glob.glob(pat)}) or args.paths
    raw = load_raw(paths)
    assert_source_shape(raw)

    df = normalize(raw)
    report_quality(df)
    df = backfill_county_from_zip(df, args.zcta_crosswalk)

    to_canonical(df).to_csv(args.out, index=False)
    queue = build_pdf_queue(df)
    queue.to_csv(args.queue, index=False)

    print(f"\n  wrote {len(df):,} accounts -> {args.out}", file=sys.stderr)
    print(f"  wrote {len(queue):,} pdf jobs -> {args.queue}", file=sys.stderr)
    print("\n  top counties in queue order:", file=sys.stderr)
    for c, n in queue["county_primary"].value_counts().head(10).items():
        print(f"    {c:<14} {n:>6,}", file=sys.stderr)
    print("\n  NOTE: no contact data until the PDF pass runs. "
          "contact_status='pending_pdf' on every row.", file=sys.stderr)


if __name__ == "__main__":
    main()
