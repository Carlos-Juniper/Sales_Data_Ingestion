"""
FL DBPR Division of Condominiums, Timeshares and Mobile Homes — condo and co-op
projects (F2, F3).

Source : https://www2.myfloridalicense.com/condominiums-timeshares-and-mobile-homes/public-records/
Files  : Condo_NF.csv, condo_CE.csv, Condo_CW.csv, Condo_MD.csv, condo_PB.csv
         (condos, one per region) and coopmailing.csv (co-ops). Weekly refresh.
License: Florida public records, Ch. 119 F.S. — free to store and use.

Profiled 2026-11-09: 27,980 condo + 759 co-op rows, one row per project
(Project Number unique in every file, so no collapse is needed — the guard
below fails loudly if that ever changes).

GRAIN AND TRAPS
---------------
* One row = one PROJECT (a recorded condo or co-op).  The Managing Entity is
  the association that runs it; one entity can run several projects
  (phases, towers).  `Units` belongs to the project.  Never sum it per row
  group in this connector; the merge sums distinct projects per association.
* There is no Sunbiz document number.  The association link is name + ZIP
  (docs/hoa-pipeline.md §4, matcher in hoa/fl_match.py).
* `Managing Entity Route` is the C/O line: the management firm when the
  association is managed (`C/O SENTRY MANAGEMENT, INC`).
* The project address is one free-text field ("STREET, CITY, FL 33528-1234").

natural_key = '<program>:<Project Number>', segment = program (condo / coop).

Usage:
    python -m hoa.fl_dbpr_condo_coop Condo_*.csv condo_*.csv coopmailing.csv \
        --out fl_dbpr_condo_coop.csv [--write-db]
"""

from __future__ import annotations

import argparse
import glob
import io
import re
import sys

import pandas as pd

from hoa.fl_load import clean, drop_empty, write_staging
from hoa.fl_match import parse_care_of
from lib.normalize import normalize_name
from lib.schema import build_canonical

SOURCE_ID = "fl_dbpr_condo_coop"

REQUIRED = ("Project Number", "County", "Street City State Zip", "Units", "Primary Status",
            "Managing Entity Number", "Managing Entity Name", "Managing Entity Route",
            "Managing Entity Street", "Managing Entity City", "Managing Entity State",
            "Managing Entity Zip")

_ADDR_TAIL = re.compile(r",\s*([^,]+?),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?\s*$")


def load_raw(paths: list[str]) -> tuple[pd.DataFrame, bytes]:
    """Read every file verbatim (all columns as str); program from the name column."""
    frames, raw = [], b""
    for path in paths:
        data = open(path, "rb").read()
        raw += data
        df = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, encoding="latin-1")
        if "COOP Name" in df.columns:
            df = df.rename(columns={"COOP Name": "Project Name", "Incorporated Date": "Recorded Date"})
            df["program"] = "coop"
        elif "Condo Name" in df.columns:
            df = df.rename(columns={"Condo Name": "Project Name"})
            df["program"] = "condo"
        else:
            raise ValueError(f"{path}: neither 'Condo Name' nor 'COOP Name' — not a DBPR project file")
        df["source_file"] = path.rsplit("/", 1)[-1]
        frames.append(df)
    return pd.concat(frames, ignore_index=True), raw


def assert_source_shape(df: pd.DataFrame) -> None:
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"missing column(s) {missing} — DBPR layout changed")
    key = df["program"] + ":" + df["Project Number"].str.strip()
    if key.duplicated().any():
        raise ValueError(f"{key.duplicated().sum()} repeated project numbers — grain changed, "
                         "collapse before loading (never sum Units)")
    units_ok = df["Units"].str.strip().str.fullmatch(r"\d+").mean()
    if units_ok < 0.99:
        raise ValueError(f"Units only {units_ok:.1%} numeric — layout changed")
    me = (df["Managing Entity Name"].str.strip() != "").mean()
    if me < 0.99:
        raise ValueError(f"Managing Entity Name only {me:.1%} populated — layout changed")


def split_site(addr: str) -> tuple[str | None, str | None, str | None, str | None]:
    """'1 MAIN ST, TAMPA, FL 33647-1234' -> (street, city, state, zip5)."""
    addr = (addr or "").strip()
    m = _ADDR_TAIL.search(addr)
    if not m:
        z = re.search(r"(\d{5})(?:-\d{4})?\s*$", addr)
        return clean(addr), None, None, z.group(1) if z else None
    return clean(addr[: m.start()]), clean(m.group(1)), m.group(2), m.group(3)


def _zip5(v: str) -> str | None:
    m = re.match(r"\s*(\d{5})", v or "")
    return m.group(1) if m else None


def to_frame(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, dict]]:
    rows, attrs = [], {}
    for r in df.to_dict("records"):
        key = f"{r['program']}:{r['Project Number'].strip()}"
        street, city, state, zip5 = split_site(r["Street City State Zip"])
        units = r["Units"].strip()
        rows.append({
            "natural_key": key,
            "name_raw": clean(r["Project Name"]),
            "name_normalized": normalize_name(r["Project Name"]),
            "address_line_1": street,
            "city": city,
            "state": state or "FL",
            "zip5": zip5,
            "segment": r["program"],
            "size_value": int(units) if units.isdigit() else None,
            "source_file": r["source_file"],
        })
        attrs[key] = drop_empty({
            "program": r["program"],
            "project_number": clean(r["Project Number"]),
            "file_number": clean(r.get("File Number")),
            "county": clean(r["County"]),
            "recorded_date": clean(r.get("Recorded Date")),
            "primary_status": clean(r["Primary Status"]),
            "secondary_status": clean(r.get("Secondary Status")),
            "managing_entity": drop_empty({
                "number": clean(r["Managing Entity Number"]),
                "name": clean(r["Managing Entity Name"]),
                "route": clean(r["Managing Entity Route"]),
                "street": clean(r["Managing Entity Street"]),
                "city": clean(r["Managing Entity City"]),
                "state": clean(r["Managing Entity State"]),
                "zip": _zip5(r["Managing Entity Zip"]),
            }),
            "care_of": parse_care_of(r["Managing Entity Route"]),
        })
    return pd.DataFrame(rows), attrs


def to_canonical(df: pd.DataFrame) -> pd.DataFrame:
    return build_canonical(
        df.index,
        source_id=SOURCE_ID,
        natural_key=df["natural_key"],
        vertical="hoa",
        account_type="association",
        name_raw=df["name_raw"],
        name_normalized=df["name_normalized"],
        address_line_1=df["address_line_1"],
        city=df["city"],
        state=df["state"],
        zip5=df["zip5"],
        segment=df["segment"],
        size_metric="units",
        size_value=df["size_value"],
        size_unit="units",
        source_file=df["source_file"],
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="DBPR condo region files and coopmailing.csv")
    ap.add_argument("--out", default="fl_dbpr_condo_coop.csv")
    ap.add_argument("--write-db", action="store_true",
                    help="Also write to Postgres staging (requires DATABASE_URL).")
    args = ap.parse_args()

    paths = sorted({p for pat in args.paths for p in (glob.glob(pat) or [pat])})
    raw_df, raw = load_raw(paths)
    assert_source_shape(raw_df)
    df, attrs = to_frame(raw_df)
    canonical = to_canonical(df)
    canonical.to_csv(args.out, index=False)

    print(f"  {len(df):,} projects -> {args.out}", file=sys.stderr)
    for seg, n in df["segment"].value_counts().items():
        print(f"    {seg:<6} {n:>7,}", file=sys.stderr)
    co = sum(1 for a in attrs.values() if a.get("care_of"))
    print(f"  with C/O firm: {co:,} ({co / max(len(df), 1):.1%})", file=sys.stderr)

    if args.write_db:
        write_staging(SOURCE_ID, canonical, attrs, raw)


if __name__ == "__main__":
    main()
