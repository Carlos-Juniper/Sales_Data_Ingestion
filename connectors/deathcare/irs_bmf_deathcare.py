"""
IRS Exempt Organizations Business Master File (BMF) — deathcare/cemetery layer.

Source  : https://www.irs.gov/pub/irs-soi/eo2.csv  (NC, SC, PA)
          https://www.irs.gov/pub/irs-soi/eo3.csv  (FL, TX)
License : Public domain — IRS Statistics of Income, no use restrictions.

Verified column names from live data on 2026-08-18 (28 columns, Latin-1 encoding):
  EIN, NAME, ICO, STREET, CITY, STATE, ZIP, GROUP, SUBSECTION, AFFILIATION,
  CLASSIFICATION, RULING, DEDUCTIBILITY, FOUNDATION, ACTIVITY, ORGANIZATION,
  STATUS, TAX_PERIOD, ASSET_CD, INCOME_CD, FILING_REQ_CD, PF_FILING_REQ_CD,
  ACCT_PD, ASSET_AMT, INCOME_AMT, REVENUE_AMT, NTEE_CD, SORT_NAME

Filter logic uses OR, not AND: SUBSECTION == '13' OR NTEE_CD LIKE 'Y50%'.
~49% of subsection-13 records have blank NTEE_CD — AND would silently drop them.

No coordinates in this source. No phone data. EIN is the natural key (zero-padded
9-digit string, no hyphen). ZIP is 9-digit with hyphen; normalize_zip handles it.
"""

from __future__ import annotations

import argparse
import datetime
import sys

import pandas as pd

from lib.enums import SEGMENT_RELIGIOUS
from lib.normalize import normalize_name, normalize_zip
from lib.schema import build_canonical
from lib.validate import assert_columns_present, assert_fill_rate, assert_min_rows

# ---------------------------------------------------------------- constants

# D3: source_id is a constant per source; the per-row id lives in natural_key.
SOURCE_ID = "irs_bmf_deathcare"

_DEFAULT_PATHS = [
    "https://www.irs.gov/pub/irs-soi/eo2.csv",
    "https://www.irs.gov/pub/irs-soi/eo3.csv",
]

_TARGET_STATES = {"FL", "TX", "NC", "SC", "PA"}

_REQUIRED_COLUMNS = ["EIN", "NAME", "STATE", "ZIP", "SUBSECTION", "NTEE_CD"]

_MIN_RAW_ROWS = 2_000


# ---------------------------------------------------------------- extract

def load_raw(paths: list[str] | None = None) -> pd.DataFrame:
    """
    Read and concatenate all IRS BMF CSV files into one raw DataFrame.

    Accepts local file paths (for testing) or URLs. Encoding must be Latin-1 —
    the IRS files contain Windows-1252 characters not valid in UTF-8.
    All columns are kept as strings to preserve EIN leading zeros and avoid
    silent coercion of blank AMT fields to NaN floats.
    """
    sources = paths if paths is not None else _DEFAULT_PATHS
    frames = []
    for path in sources:
        df = pd.read_csv(path, dtype=str, encoding="latin-1")
        df["source_file"] = path
        frames.append(df)
    combined = pd.concat(frames, ignore_index=True)
    return combined


# ---------------------------------------------------------------- checks

def assert_source_shape(df: pd.DataFrame) -> None:
    """
    Raise ValueError if the loaded data does not match the known source shape.

    Guards against: CSV layout changes, wrong encoding producing garbage columns,
    truncated downloads, and missing target states that would cause silent data loss.
    """
    assert_columns_present(df, _REQUIRED_COLUMNS, label="BMF")
    assert_fill_rate(
        df, "EIN", 0.99,
        label="EIN is the natural key — a low fill rate means the layout changed",
    )
    assert_min_rows(df, _MIN_RAW_ROWS, label="BMF returned")

    # All 5 target states must appear in the raw data (before any filtering).
    # eo2 covers NC/SC/PA and eo3 covers FL/TX — if a state is absent the
    # wrong file was loaded or the IRS reorganised the regional split.
    present_states = set(df["STATE"].dropna().unique())
    missing_states = _TARGET_STATES - present_states
    if missing_states:
        raise ValueError(
            f"BMF data missing expected target states: {sorted(missing_states)}. "
            "Check that both eo2.csv and eo3.csv were loaded."
        )


# ---------------------------------------------------------------- transform

def filter_cemetery(df: pd.DataFrame) -> pd.DataFrame:
    """
    Apply subsection/NTEE filter and restrict to the 5 target states.

    OR filter is intentional: subsection-13 records with blank NTEE_CD (~49%)
    must be kept. Using AND would silently discard nearly half the target records.
    """
    rows_before = len(df)
    sys.stderr.write(f"  bmf: {rows_before:,} rows before filter\n")

    is_sub13 = df["SUBSECTION"] == "13"
    is_y50 = df["NTEE_CD"].str.startswith("Y50", na=False)
    df_filtered = df[is_sub13 | is_y50].copy()

    rows_after_ntee = len(df_filtered)
    sys.stderr.write(
        f"  bmf: {rows_after_ntee:,} rows after subsection/NTEE filter "
        f"(dropped {rows_before - rows_after_ntee:,})\n"
    )

    df_filtered = df_filtered[df_filtered["STATE"].isin(_TARGET_STATES)].copy()

    rows_after_state = len(df_filtered)
    sys.stderr.write(
        f"  bmf: {rows_after_state:,} rows after state filter "
        f"(dropped {rows_after_ntee - rows_after_state:,})\n"
    )

    return df_filtered


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Add derived columns used by to_canonical() and the deathcare merge module."""
    df = df.copy()

    df["name_normalized"] = df["NAME"].map(normalize_name)
    df["zip5"] = df["ZIP"].map(normalize_zip)

    # BMF has no phone data and no coordinate data.
    df["phone_raw"] = None
    df["phone_normalized"] = None
    df["latitude"] = None
    df["longitude"] = None

    # All BMF records in this filter are nonprofit/religious cemeteries by definition.
    df["segment"] = SEGMENT_RELIGIOUS

    df["county_fips"] = None

    # Preserve EIN as string to keep leading zeros (e.g., "043783054").
    df["ein"] = df["EIN"]

    return df


# ---------------------------------------------------------------- quality

def report_quality(df: pd.DataFrame) -> None:
    """Log data quality metrics to stderr."""
    total = len(df)
    sys.stderr.write(f"  bmf: {total:,} total records\n")

    sys.stderr.write("  bmf: rows by state\n")
    state_counts = df["STATE"].value_counts().sort_index()
    for state, count in state_counts.items():
        sys.stderr.write(f"    {state}  {count:>7,}\n")

    sys.stderr.write("  bmf: SUBSECTION breakdown\n")
    sub_counts = df["SUBSECTION"].value_counts().sort_index()
    for sub, count in sub_counts.items():
        sys.stderr.write(f"    {sub}  {count:>7,}\n")

    sys.stderr.write("  bmf: NTEE_CD top values\n")
    ntee_counts = df["NTEE_CD"].value_counts(dropna=False).head(10)
    for ntee, count in ntee_counts.items():
        sys.stderr.write(f"    {str(ntee):<12}  {count:>7,}\n")

    sub13 = df[df["SUBSECTION"] == "13"]
    if len(sub13) > 0:
        blank_ntee_pct = (~sub13["NTEE_CD"].fillna("").astype(bool)).mean()
        sys.stderr.write(
            f"  bmf: subsection-13 with blank NTEE_CD  {blank_ntee_pct:.1%}\n"
        )

    asset_filled = df["ASSET_AMT"].fillna("").astype(bool).mean()
    sys.stderr.write(f"  bmf: non-blank ASSET_AMT  {asset_filled:.1%}\n")


# ---------------------------------------------------------------- canonical output

def to_canonical(df: pd.DataFrame) -> pd.DataFrame:
    """Map normalized BMF columns to the standard deathcare output shape."""
    # D3: source_id is the constant SOURCE_ID; natural_key carries the EIN.
    source_file = ", ".join(df["source_file"].dropna().unique().tolist())
    return build_canonical(
        df.index,
        source_id=        SOURCE_ID,
        natural_key=      df["EIN"],
        vertical=         "deathcare",
        account_type=     "cemetery",
        name_raw=         df["NAME"],
        name_normalized=  df["name_normalized"],
        address_line_1=   df["STREET"],
        city=             df["CITY"],
        state=            df["STATE"],
        zip5=             df["zip5"],
        segment=          df["segment"],
        ein=              df["ein"],
        source_file=      source_file,
    )


# ---------------------------------------------------------------- entrypoint


def main() -> None:
    """CLI entrypoint — load IRS BMF deathcare data and optionally write to DB."""
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--out",
        default="irs_bmf_deathcare.csv",
        help="Output CSV path (default: irs_bmf_deathcare.csv)",
    )
    ap.add_argument(
        "--write-db",
        action="store_true",
        help="Also write results to Postgres staging (requires DATABASE_URL). "
             "Off by default — the CSV is always written regardless.",
    )
    ap.add_argument(
        "--input",
        nargs="+",
        default=None,
        metavar="PATH_OR_URL",
        help="Local CSV paths or URLs to load (default: eo2.csv and eo3.csv from IRS). "
             "Example: --input /data/eo2.csv /data/eo3.csv",
    )
    args = ap.parse_args()

    paths = args.input or _DEFAULT_PATHS
    sys.stderr.write(f"  irs_bmf_deathcare: loading {paths}\n")

    raw = load_raw(paths=paths)
    assert_source_shape(raw)

    filtered = filter_cemetery(raw)
    normalized = normalize(filtered)
    report_quality(normalized)
    canonical = to_canonical(normalized)

    import json as _json
    raw_bytes = _json.dumps(
        raw.to_dict(orient="records"), sort_keys=True
    ).encode("utf-8")

    canonical.to_csv(args.out, index=False)
    sys.stderr.write(f"\n  wrote {len(canonical):,} records -> {args.out}\n")

    if args.write_db:
        from lib.db import get_engine, write_source_run, upsert_staging, finish_source_run
        from lib.gcs import raw_sha256, upload_raw
        from lib.http import get_secret

        if not get_secret("DATABASE_URL"):
            sys.exit(
                "ERROR: --write-db was given but DATABASE_URL is not set. "
                "Copy .env.example -> .env and fill it in."
            )

        sha256_hex = raw_sha256(raw_bytes)
        byte_count = len(raw_bytes)
        sys.stderr.write(
            f"  irs_bmf_deathcare: sha256={sha256_hex[:16]}…  bytes={byte_count:,}\n"
        )

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
                license_string="IRS Statistics of Income — public domain",
                raw_uri=raw_uri,
            )

            upsert_staging(engine, SOURCE_ID, canonical)

            finish_source_run(
                engine,
                source_run_id,
                status="succeeded",
                row_count=len(canonical),
            )
            sys.stderr.write(
                f"  irs_bmf_deathcare: wrote {len(canonical):,} rows "
                f"to staging.{SOURCE_ID} (source_run_id={source_run_id})\n"
            )
        except Exception as exc:
            if source_run_id is not None:
                finish_source_run(engine, source_run_id, status="failed")
            sys.exit(f"ERROR: DB write failed — {exc}")


if __name__ == "__main__":
    main()
