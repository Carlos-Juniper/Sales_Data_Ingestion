"""
FL DBPR Division of Hotels & Restaurants — public lodging establishments.

Source : https://www2.myfloridalicense.com/hotels-restaurants/lodging-public-records/
Files  : hrlodge1.csv .. hrlodge7.csv  (one per district, weekly refresh, free)
License: Florida public records, Ch. 119 F.S. — free to store and use.

Verified against a real District 1 extract on 2026-08-17 (24,604 rows, 35 cols).

TWO THINGS THIS FILE EXISTS TO GET RIGHT
----------------------------------------
1. The extract is ONE ROW PER RENTAL UNIT, not per property. License CND2300049
   (The Inn at Fisher Island) is 15 rows differing only in Address Line 2.
   Dedup on License Number.

2. "Number of Seats or Rental Units" is the LICENSE total, repeated verbatim on
   every child row. SUM() inflates it ~8x. Always take max/first per license.

Usage:
    python fl_dbpr_lodging.py hrlodge*.csv --out qualified.csv
"""

from __future__ import annotations

import argparse
import glob
import re
import sys

import pandas as pd

# ---------------------------------------------------------------- constants

SOURCE_ID = "fl_dbpr_lodging"

# Rank Code -> (canonical vertical, keep?)
#
# NAPT is conventional multifamily housing, NOT hospitality. DESCOPED 2026-08-17:
# individual residential properties fall below Juniper's contract floor, so these
# rows are tagged 'multifamily' and must be excluded from lead counts and
# sales-facing views. They are retained rather than dropped because they are
# needed to establish the license-level grain, and because re-enabling them is a
# one-line change if the portfolio-owner angle is ever pursued.
# Filter downstream with:  df[df.vertical != 'multifamily']
RANK_MAP = {
    "HOTL": ("resort", True),      # hotel            — best per-record value
    "MOTL": ("resort", True),      # motel
    "CNDO": ("resort", True),      # rental-licensed condominium
    "TAPT": ("resort", True),      # transient apartment
    "NAPT": ("multifamily", True), # nontransient apartment
    "BNB":  ("resort", False),     # bed & breakfast  — median 8 units, too small
    "DWEL": ("resort", False),     # vacation rental dwelling — private individuals
}

STATUS_ACTIVE = "20"   # 45 = expired (seen with expiry dates back to 2009)

# Below this, the grounds are too small to be worth a sales touch.
MIN_UNITS = 20

# 0% populated in the verified extract — dropped on load.
DEAD_COLUMNS = ["Filler", "Base Risk Level", "Secondary Risk Level"]

# Only ~52.8% populated. Do not partition or join on these; use Location County.
UNRELIABLE_COLUMNS = ["District", "Region"]

UNIT_COL = "Number of Seats or Rental Units"

# Identifies rows whose licensee is a genuine association rather than a
# management company or fee owner. Only ~10% of CNDO rows match — this file is
# a rental-licensing register, not an association registry.
ASSOC_PATTERN = re.compile(
    r"ASSOCIATION|ASSN|ASSOC|CONDOMINIUM|\bCOA\b|\bHOA\b|OWNERS", re.IGNORECASE
)


# ---------------------------------------------------------------- extract

def load_raw(paths: list[str]) -> pd.DataFrame:
    """Read one or more district files. Everything stays as string on the way in."""
    frames = []
    for p in paths:
        df = pd.read_csv(p, dtype=str, keep_default_na=False)
        df["_source_file"] = p.rsplit("/", 1)[-1]
        frames.append(df)
        print(f"  read {p}: {len(df):,} rows", file=sys.stderr)
    out = pd.concat(frames, ignore_index=True)
    return out.drop(columns=[c for c in DEAD_COLUMNS if c in out.columns])


# ---------------------------------------------------------------- transform

def normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["units"] = pd.to_numeric(df[UNIT_COL], errors="coerce")
    df["vertical"] = df["Rank Code"].map(lambda r: RANK_MAP.get(r, ("resort", False))[0])
    df["_keep_rank"] = df["Rank Code"].map(lambda r: RANK_MAP.get(r, ("resort", False))[1])

    # TODO: migrate to lib/normalize.normalize_name
    for col in ("Location Street Address", "Location City", "Licensee Name", "Business Name"):
        df[col + "_norm"] = (
            df[col].str.upper().str.replace(r"\s+", " ", regex=True).str.strip()
        )

    # ZIP arrives as both 33139 and 331394209 and 33139-5808.
    # TODO: migrate to lib/normalize.normalize_zip
    df["zip5"] = df["Location Zip Code"].str.replace(r"\D", "", regex=True).str[:5]
    df["is_association"] = df["Licensee Name"].str.contains(ASSOC_PATTERN, na=False)
    return df


def filter_qualified(df: pd.DataFrame, min_units: int = MIN_UNITS) -> pd.DataFrame:
    """Apply the four filters in order, logging the drop at each step."""
    steps: list[tuple[str, int]] = [("raw rows", len(df))]

    df = df[df["Primary Status Code"] == STATUS_ACTIVE]
    steps.append(("active licenses", len(df)))

    df = df[df["_keep_rank"]]
    steps.append(("commercial ranks only", len(df)))

    df = df[df["units"] >= min_units]
    steps.append((f"units >= {min_units}", len(df)))

    df = collapse_to_property(df)
    steps.append(("collapsed to property", len(df)))

    print("\n  funnel:", file=sys.stderr)
    for label, n in steps:
        print(f"    {label:<28} {n:>8,}", file=sys.stderr)
    return df


def collapse_to_property(df: pd.DataFrame) -> pd.DataFrame:
    """
    One row per License Number.

    Address Line 2 is the only field that varies within a license group, so the
    unit rows are collapsed into a count and the parent attributes are taken
    from the first row. units uses max(), never sum().
    """
    if df.empty:
        return df

    agg = {c: "first" for c in df.columns if c != "License Number"}
    agg["units"] = "max"  # the one that matters

    out = df.groupby("License Number", as_index=False).agg(agg)
    out["unit_rows_in_source"] = (
        df.groupby("License Number").size().reindex(out["License Number"]).values
    )
    return out


def to_canonical(df: pd.DataFrame) -> pd.DataFrame:
    """Map to the core.account / core.location shape from §4 of the plan."""
    return pd.DataFrame({
        "source_id": SOURCE_ID,
        "natural_key": df["License Number"],
        "vertical": df["vertical"],
        "account_type": "single_site",
        "legal_name": df["Licensee Name"],
        "name_normalized": df["Licensee Name_norm"],
        "dba_name": df["Business Name"],
        "location_name": df["Business Name"],
        "site_street": df["Location Street Address"],
        "site_city": df["Location City"],
        "site_state": df["Location State Code"],
        "site_zip": df["zip5"],
        "site_county": df["Location County"],
        "phone": df["Primary Phone Number"].where(
            df["Primary Phone Number"].str.strip() != "", df["Secondary Phone Number"]
        ),
        "mailing_street": df["Mailing Street Address"],
        "mailing_city": df["Mailing City"],
        "mailing_state": df["Mailing State Code"],
        "mailing_zip": df["Mailing Zip Code"],
        "size_metric": df["units"],
        "size_metric_unit": "rental_units",
        "license_no": df["License Number"],
        "license_class": df["Rank Code"],
        "license_expiry": df["License Expiry Date"],
        "last_inspection": df["Last Inspection Date"],
        "is_association": df["is_association"],
        "unit_rows_in_source": df["unit_rows_in_source"],
        # No lat/long in the source. Location address is 100% populated, so the
        # free Census Geocoder covers this with no Esri stored-geocode spend.
        "geocode_status": "pending",
    })


# ---------------------------------------------------------------- checks

def assert_source_shape(df: pd.DataFrame) -> None:
    """Fail loudly if DBPR changes the layout out from under us."""
    assert UNIT_COL in df.columns, f"missing size field {UNIT_COL!r} — layout changed"

    fill = (df[UNIT_COL].str.strip() != "").mean()
    assert fill > 0.99, f"{UNIT_COL} only {fill:.1%} populated (expected 100%)"

    for col in ("Licensee Name", "Location Street Address", "License Number"):
        f = (df[col].str.strip() != "").mean()
        assert f > 0.99, f"{col} only {f:.1%} populated"

    assert "Primary Status Code" in df.columns, (
        "missing column 'Primary Status Code' — layout changed; filter_qualified will KeyError"
    )

    unknown = set(df["Rank Code"].unique()) - set(RANK_MAP)
    assert not unknown, f"unmapped Rank Code(s): {unknown} — route them before loading"


# ---------------------------------------------------------------- entrypoint

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("paths", nargs="+", help="hrlodge*.csv district files")
    ap.add_argument("--out", default="fl_dbpr_qualified.csv")
    ap.add_argument("--min-units", type=int, default=MIN_UNITS)
    ap.add_argument("--include-multifamily", action="store_true",
                    help="retain NAPT rows; descoped by default (below contract floor)")
    args = ap.parse_args()

    paths = sorted({p for pat in args.paths for p in glob.glob(pat)}) or args.paths
    print(f"loading {len(paths)} district file(s)", file=sys.stderr)

    raw = load_raw(paths)
    assert_source_shape(raw)

    qualified = to_canonical(filter_qualified(normalize(raw), args.min_units))

    if not args.include_multifamily:
        n_before = len(qualified)
        qualified = qualified[qualified["vertical"] != "multifamily"]
        print(f"\n  descoped multifamily: dropped {n_before - len(qualified):,} NAPT rows "
              "(pass --include-multifamily to retain)", file=sys.stderr)

    qualified.to_csv(args.out, index=False)

    print(f"\n  wrote {len(qualified):,} qualified leads -> {args.out}", file=sys.stderr)
    print("\n  by vertical:", file=sys.stderr)
    for v, n in qualified["vertical"].value_counts().items():
        print(f"    {v:<14} {n:>7,}", file=sys.stderr)
    print("\n  by license class:", file=sys.stderr)
    for c, n in qualified["license_class"].value_counts().items():
        print(f"    {c:<14} {n:>7,}", file=sys.stderr)
    print(f"\n  median size: {qualified['size_metric'].median():.0f} units", file=sys.stderr)


if __name__ == "__main__":
    main()
