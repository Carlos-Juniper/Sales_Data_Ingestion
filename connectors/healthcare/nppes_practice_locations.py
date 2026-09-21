"""
NPPES Practice Locations — secondary physical sites for healthcare organizations.

Source  : CMS NPPES Data Dissemination (flat-file ZIP, no API/key needed)
Download: https://download.cms.gov/nppes/NPI_Files.html
License : Public domain — U.S. Department of Health & Human Services.

The main NPPES file carries one address per NPI (the primary practice location).
This connector joins the pl_ secondary file to recover satellite clinics and
additional physical sites — one output row per secondary location.

NPPES Filter Trap (see things_to_consider.md):
  Filtering to ENTITY_TYPE_CODE=2 alone is not enough. Medical groups can file
  under non-facility taxonomies and still operate multi-site ambulatory clinics.
  We expand the filter to include Ambulatory Health Care (261Q...), Hospitals
  (282...), and Nursing/Custodial Care (285...) taxonomies so MOBs and ASCs are
  not silently dropped.

natural_key = NPI + "|" + location_seq  (1-indexed per NPI, secondary sites only)

Usage:
    python nppes_practice_locations.py \\
        --main  data/NPPES_Data_Dissemination_August_2026_V2/npidata_pfile_*.csv \\
        --pl    data/NPPES_Data_Dissemination_August_2026_V2/pl_pfile_*.csv \\
        --out   nppes_practice_locations.csv
"""

from __future__ import annotations

import argparse
import datetime
import glob
import sys
from pathlib import Path

import pandas as pd

from lib.db import finish_source_run, get_engine, upsert_staging, write_source_run
from lib.enums import HEALTHCARE_TARGET_STATES
from lib.gcs import raw_sha256, upload_raw
from lib.http import get_secret
from lib.normalize import normalize_name, normalize_phone, normalize_zip
from lib.schema import build_canonical

SOURCE_ID = "nppes_practice_locations"
VERTICAL = "healthcare"

# Entity type "2" = Organization
ENTITY_TYPE_ORG = "2"

# Taxonomy prefixes that indicate health facility types we care about.
# See the NPPES Filter Trap note in the module docstring.
FACILITY_TAXONOMY_PREFIXES = ("261Q", "282", "284", "285", "286", "287")

# Taxonomy code columns in the main NPPES file.
_TAXONOMY_COLS = [f"Healthcare Provider Taxonomy Code_{i}" for i in range(1, 16)]

_MAIN_USECOLS = [
    "NPI",
    "Entity Type Code",
    "Provider Organization Name (Legal Business Name)",
] + _TAXONOMY_COLS

# Column name constants for the pl_ file.  The double-space on Address Line 2
# is intentional — it matches the raw CSV header exactly.
_PL_COL_ADDR1  = "Provider Secondary Practice Location Address- Address Line 1"
_PL_COL_ADDR2  = "Provider Secondary Practice Location Address-  Address Line 2"
_PL_COL_CITY   = "Provider Secondary Practice Location Address - City Name"
_PL_COL_STATE  = "Provider Secondary Practice Location Address - State Name"
_PL_COL_ZIP    = "Provider Secondary Practice Location Address - Postal Code"
_PL_COL_CTRY   = "Provider Secondary Practice Location Address - Country Code (If outside U.S.)"
_PL_COL_PHONE  = "Provider Secondary Practice Location Address - Telephone Number"
_PL_COL_EXT    = "Provider Secondary Practice Location Address - Telephone Extension"
_PL_COL_FAX    = "Provider Practice Location Address - Fax Number"

_PL_REQUIRED_COLS = {"NPI", _PL_COL_ADDR1, _PL_COL_STATE, _PL_COL_ZIP}


# ---------------------------------------------------------------------------
# Taxonomy filtering helpers (pure functions — testable without file I/O)
# ---------------------------------------------------------------------------


def _has_facility_taxonomy(row: pd.Series) -> bool:
    """Return True if any taxonomy column in the row starts with a facility prefix."""
    for col in _TAXONOMY_COLS:
        code = row.get(col, "")
        if code and any(code.startswith(p) for p in FACILITY_TAXONOMY_PREFIXES):
            return True
    return False


def filter_to_facility_orgs(df: pd.DataFrame) -> pd.DataFrame:
    """
    Reduce the full main NPPES DataFrame to organization rows that operate
    at least one health-facility taxonomy.

    Steps
    -----
    1. Keep Entity Type Code == "2" (Organizations only).
    2. Keep rows where at least one taxonomy code starts with a prefix in
       FACILITY_TAXONOMY_PREFIXES.

    Returns a copy — does not mutate the input.
    """
    # Step 1: entity filter
    org_mask = df["Entity Type Code"].str.strip() == ENTITY_TYPE_ORG
    df_orgs = df[org_mask].copy()
    print(
        f"  entity filter (type=2): {len(df):,} -> {len(df_orgs):,} rows",
        file=sys.stderr,
    )

    # Step 2: taxonomy filter
    # Using apply row-wise is readable and sufficient for ~1M org rows.
    taxonomy_mask = df_orgs.apply(_has_facility_taxonomy, axis=1)
    df_facilities = df_orgs[taxonomy_mask].copy()
    print(
        f"  taxonomy filter (facility prefixes): {len(df_orgs):,} -> {len(df_facilities):,} rows",
        file=sys.stderr,
    )

    return df_facilities


def _pick_primary_taxonomy(row: pd.Series) -> str:
    """Return the first non-empty taxonomy code across the 15 taxonomy columns."""
    for col in _TAXONOMY_COLS:
        code = row.get(col, "")
        if code:
            return code
    return ""


# ---------------------------------------------------------------------------
# Load functions
# ---------------------------------------------------------------------------


def load_main(path: str) -> pd.DataFrame:
    """
    Load the main NPPES file and return a slim DataFrame of facility organizations.

    Only the columns needed for joining and output are loaded (usecols=) to
    prevent OOM on the 9.7M-row, 330-column source file.

    Parameters
    ----------
    path:
        Absolute path to the npidata_pfile_*.csv file.

    Returns
    -------
    DataFrame with columns: NPI, Provider Organization Name (Legal Business Name),
    taxonomy_primary.
    """
    print(f"loading main NPPES file: {path}", file=sys.stderr)
    df = pd.read_csv(
        path,
        usecols=_MAIN_USECOLS,
        dtype=str,
        keep_default_na=False,
    )
    print(f"  total rows in file: {len(df):,}", file=sys.stderr)

    df = filter_to_facility_orgs(df)

    # Derive primary taxonomy before dropping the individual columns.
    df["taxonomy_primary"] = df.apply(_pick_primary_taxonomy, axis=1)

    # Drop the 15 individual taxonomy columns — they are no longer needed.
    df = df.drop(columns=_TAXONOMY_COLS)

    return df


def load_pl(path: str) -> pd.DataFrame:
    """
    Load the NPPES secondary practice location file.

    Parameters
    ----------
    path:
        Absolute path to the pl_pfile_*.csv file.

    Returns
    -------
    DataFrame with all 10 pl_ columns; dtype=str throughout.
    """
    print(f"loading pl_ file: {path}", file=sys.stderr)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    print(f"  pl_ rows loaded: {len(df):,}", file=sys.stderr)
    return df


# ---------------------------------------------------------------------------
# Shape validation
# ---------------------------------------------------------------------------


def assert_source_shape(main_df: pd.DataFrame, pl_df: pd.DataFrame) -> None:
    """
    Raise ValueError if either DataFrame is missing required columns.

    Called after load_main / load_pl so that a schema change in the upstream
    flat file surfaces immediately with a descriptive error rather than a
    cryptic KeyError later in the pipeline.
    """
    main_required = {"NPI", "Provider Organization Name (Legal Business Name)"}
    missing_main = main_required - set(main_df.columns)
    if missing_main:
        raise ValueError(
            f"main_df is missing required column(s): {missing_main}. "
            f"Columns present: {list(main_df.columns)}"
        )

    missing_pl = _PL_REQUIRED_COLS - set(pl_df.columns)
    if missing_pl:
        raise ValueError(
            f"pl_df is missing required column(s): {missing_pl}. "
            f"Columns present: {list(pl_df.columns)}"
        )


# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------


def normalize_and_join(main_df: pd.DataFrame, pl_df: pd.DataFrame) -> pd.DataFrame:
    """
    Inner-join secondary locations to filtered organization records and normalize.

    Join strategy: inner join on NPI so that pl_ rows for NPIs that were
    filtered out (individuals, non-facility orgs) are silently dropped.

    Sequence numbering is 1-indexed per NPI to distinguish secondary locations.
    Primary location data (from the main file's own address columns) is *not*
    emitted here; this connector handles secondary sites only.

    Parameters
    ----------
    main_df:
        Output of load_main — slim org DataFrame with NPI, org name, taxonomy.
    pl_df:
        Output of load_pl — full pl_ DataFrame.

    Returns
    -------
    DataFrame with columns: natural_key, npi, location_seq, name_raw,
    address_line_1, city, site_state, zip5, taxonomy_primary.
    """
    joined = pl_df.merge(main_df, on="NPI", how="inner")

    # Assign location_seq: 1-indexed rank within each NPI group, in file order.
    # cumcount() is 0-indexed so we add 1.
    joined["location_seq"] = joined.groupby("NPI").cumcount() + 1

    # Normalize zip to 5 digits using the shared lib utility.
    joined["zip5"] = joined[_PL_COL_ZIP].map(normalize_zip)

    # Build natural_key as NPI + "|" + location_seq.
    joined["natural_key"] = joined["NPI"] + "|" + joined["location_seq"].astype(str)

    # Rename columns to canonical output names.
    out = joined.rename(columns={
        "NPI":                                                    "npi",
        "Provider Organization Name (Legal Business Name)":       "name_raw",
        _PL_COL_ADDR1:                                           "address_line_1",
        _PL_COL_CITY:                                            "city",
        _PL_COL_STATE:                                           "site_state",
    })

    # Phone: the pl_ file carries a per-location telephone number.
    # Normalize to digits-only (10+ digits) via normalize_phone so the pipeline
    # can coalesce phone across cluster members in survivorship.
    # Guard: unit-test DataFrames built without the full pl_ schema may omit
    # _PL_COL_PHONE — fall back to an empty Series so tests don't KeyError.
    if _PL_COL_PHONE in joined.columns:
        phone_raw_series = joined[_PL_COL_PHONE].fillna("").astype(str).str.strip()
    else:
        phone_raw_series = pd.Series("", index=joined.index, dtype=str)
    out["phone_raw"] = phone_raw_series
    out["phone_normalized"] = out["phone_raw"].map(normalize_phone)

    # D10: filter to the 5 target states immediately after join — before
    # report_quality(), build_canonical(), and upsert_staging().  This matters
    # for cost/time: NPPES is ~9.7M rows before any filtering, and every
    # out-of-state row would otherwise be geocoded at ~1 req/sec.
    # State column is site_state (raw 2-letter code from the pl_ file, uppercase).
    before = len(out)
    out = out[out["site_state"].isin(HEALTHCARE_TARGET_STATES)].copy()
    after = len(out)
    print(
        f"  nppes_practice_locations: state filter "
        f"({'/'.join(sorted(HEALTHCARE_TARGET_STATES))}): "
        f"{before:,} -> {after:,} rows",
        file=sys.stderr,
    )

    return out[[
        "natural_key",
        "npi",
        "location_seq",
        "name_raw",
        "address_line_1",
        "city",
        "site_state",
        "zip5",
        "taxonomy_primary",
        "phone_raw",
        "phone_normalized",
    ]]


# ---------------------------------------------------------------------------
# Quality report
# ---------------------------------------------------------------------------


def report_quality(df: pd.DataFrame) -> None:
    """
    Print basic data-quality metrics to stderr.

    Reported metrics
    ----------------
    - Total secondary-location rows
    - Distinct NPI count
    - Top 5 states by row count
    - zip5 fill rate (non-empty fraction)
    """
    total = len(df)
    distinct_npis = df["npi"].nunique()
    zip_fill_rate = (df["zip5"].str.len() == 5).mean() if total > 0 else 0.0

    print(f"\nquality report:", file=sys.stderr)
    print(f"  total secondary-location rows : {total:,}", file=sys.stderr)
    print(f"  distinct NPIs                 : {distinct_npis:,}", file=sys.stderr)
    print(f"  zip5 fill rate                : {zip_fill_rate:.1%}", file=sys.stderr)

    print(f"\n  top 5 states by row count:", file=sys.stderr)
    top_states = df["site_state"].value_counts().head(5)
    for state, count in top_states.items():
        print(f"    {state:<4} {count:>8,}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entrypoint — parse arguments, run the pipeline, write output CSV."""
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--main",
        required=True,
        metavar="GLOB",
        help="glob path to the main NPPES file (npidata_pfile_*.csv)",
    )
    ap.add_argument(
        "--pl",
        required=True,
        metavar="GLOB",
        help="glob path to the practice location file (pl_pfile_*.csv)",
    )
    ap.add_argument(
        "--out",
        default="nppes_practice_locations.csv",
        help="output CSV path (default: nppes_practice_locations.csv)",
    )
    ap.add_argument(
        "--write-db",
        action="store_true",
        help="Also write results to Postgres staging (requires DATABASE_URL). "
             "Off by default — the CSV is always written regardless.",
    )
    args = ap.parse_args()

    # Resolve globs — exactly one file must match each pattern.
    main_matches = glob.glob(args.main)
    if len(main_matches) != 1:
        print(
            f"error: --main glob matched {len(main_matches)} file(s), expected exactly 1: {args.main}",
            file=sys.stderr,
        )
        sys.exit(1)

    pl_matches = glob.glob(args.pl)
    if len(pl_matches) != 1:
        print(
            f"error: --pl glob matched {len(pl_matches)} file(s), expected exactly 1: {args.pl}",
            file=sys.stderr,
        )
        sys.exit(1)

    main_path = main_matches[0]
    pl_path = pl_matches[0]

    # --- Extract ---
    main_df = load_main(main_path)
    pl_df = load_pl(pl_path)

    # --- Validate shapes ---
    assert_source_shape(main_df, pl_df)

    # --- Transform ---
    print("\njoining and normalizing...", file=sys.stderr)
    result = normalize_and_join(main_df, pl_df)

    # B4 fix: hash actual file bytes, not a pandas re-serialisation.
    # Both source files are included to detect any upstream change in either.
    # Canonical payload = pl_file_bytes + b"\x00" + main_file_bytes (null separator
    # for determinism; order: pl_ first as the primary output driver).
    # byte_count reflects this combined payload size.
    pl_file_bytes = Path(pl_path).read_bytes()
    main_file_bytes = Path(main_path).read_bytes()
    raw_bytes = pl_file_bytes + b"\x00" + main_file_bytes
    sha256_hex = raw_sha256(raw_bytes)
    byte_count = len(raw_bytes)
    print(
        f"  nppes_practice_locations: sha256={sha256_hex[:16]}…  bytes={byte_count:,}",
        file=sys.stderr,
    )

    # --- Quality report ---
    report_quality(result)

    # --- Load ---
    result.to_csv(args.out, index=False)
    print(f"\nwrote {len(result):,} rows -> {args.out}", file=sys.stderr)

    # Write to Postgres only when explicitly requested via --write-db.
    if args.write_db:
        if not get_secret("DATABASE_URL"):
            sys.exit(
                "ERROR: --write-db was given but DATABASE_URL is not set. "
                "Copy .env.example -> .env and fill it in."
            )

        # D7: upload raw payload to GCS before writing source_run.
        # Returns None when GCS is disabled/unavailable — never raises.
        run_date = datetime.date.today().isoformat()
        raw_uri = upload_raw(SOURCE_ID, run_date, raw_bytes)

        engine = get_engine()
        source_run_id: int | None = None
        try:
            source_run_id = write_source_run(
                engine,
                source_id=SOURCE_ID,
                byte_count=byte_count,
                sha256=sha256_hex,
                connector_version="1.0",
                license_string="NPPES Data Dissemination — public domain, U.S. Department of Health & Human Services",
                raw_uri=raw_uri,
            )

            full_canonical = build_canonical(
                result.index,
                source_id=SOURCE_ID,
                natural_key=result["natural_key"],
                vertical=VERTICAL,
                account_type=result["taxonomy_primary"],
                name_raw=result["name_raw"],
                name_normalized=result["name_raw"].map(normalize_name),
                address_line_1=result["address_line_1"],
                city=result["city"],
                state=result["site_state"],
                zip5=result["zip5"],
                phone_raw=result["phone_raw"],
                phone_normalized=result["phone_normalized"],
                source_file=pl_path,
            )

            upsert_staging(engine, SOURCE_ID, full_canonical)

            finish_source_run(
                engine,
                source_run_id,
                status="succeeded",
                row_count=len(full_canonical),
            )
            print(
                f"  nppes_practice_locations: wrote {len(full_canonical):,} rows "
                f"to staging.{SOURCE_ID} (source_run_id={source_run_id})",
                file=sys.stderr,
            )
        except Exception as exc:
            if source_run_id is not None:
                finish_source_run(engine, source_run_id, status="failed")
            print(f"  nppes_practice_locations: DB write failed — {exc}", file=sys.stderr)
            raise


if __name__ == "__main__":
    main()
