"""
FL DBPR — Community Association Management firms (F4) and managers (F5).

Source : https://www2.myfloridalicense.com/community-association-managers-and-firms/public-records/
File   : .../sto/file_download/extracts/lic38cam.csv  (no header row, 22 columns)
License: Florida public records, Ch. 119 F.S. — free to store and use.

Profiled 2026-11-09: 28,907 rows — CAM 19,575 (individual managers),
CAB 2,070 (firms), plus CRSE / PROV / PRE (courses, providers, pre-licensing
schools) which are not licensees and are dropped.  License number is unique
per type, so there is nothing to collapse.

TRAPS
-----
* No header.  Columns follow the standard DBPR licensee extract (COLUMNS).
* The firm's trading names are in the DBA column, ';'-separated.  The matcher
  (hoa/fl_match.FirmIndex) indexes them alongside the licensed name.
* An individual record has no employer.  The F5 -> F4 link cannot come from
  DBPR (docs/hoa-pipeline.md §4); individuals are loaded for contact
  enrichment only.  Their names and home addresses are personal data and are
  never printed here.

natural_key = license number (`CAB3372`, `CAM12345`), segment = firm / individual.

Usage:
    python -m hoa.fl_dbpr_cam lic38cam.csv --out fl_dbpr_cam.csv [--write-db]
"""

from __future__ import annotations

import argparse
import io
import re
import sys

import pandas as pd

from hoa.fl_load import clean, drop_empty, write_staging
from lib.normalize import normalize_name
from lib.schema import build_canonical

SOURCE_ID = "fl_dbpr_cam"

COLUMNS = [
    "board", "license_type", "name", "dba", "rank", "addr1", "addr2", "addr3", "city",
    "state", "zip", "county_code", "license_seq", "primary_status", "secondary_status",
    "original_date", "status_date", "expiry_date", "blank_18", "blank_19", "license_number",
    "blank_21",
]
KIND = {"CAB": "firm", "CAM": "individual"}


def load_raw(path: str) -> tuple[pd.DataFrame, bytes]:
    data = open(path, "rb").read()
    df = pd.read_csv(io.BytesIO(data), header=None, names=COLUMNS, dtype=str,
                     keep_default_na=False, encoding="latin-1")
    return df, data


def assert_source_shape(df: pd.DataFrame) -> None:
    if df.empty:
        raise ValueError("empty CAM extract")
    if not (df["board"] == "38").all():
        raise ValueError("board code is not 38 on every row — wrong file or layout changed")
    kept = df[df["license_type"].isin(KIND)]
    if kept.empty:
        raise ValueError("no CAB/CAM rows — layout changed")
    lic_ok = kept["license_number"].str.fullmatch(r"(CAB|CAM)\d+").mean()
    if lic_ok < 0.99:
        raise ValueError(f"license number only {lic_ok:.1%} well formed — columns shifted")
    if kept["license_number"].duplicated().any():
        raise ValueError("repeated license numbers — grain changed")


def _zip5(v: str) -> str | None:
    m = re.match(r"\s*(\d{5})", v or "")
    return m.group(1) if m else None


def _date(v: str) -> str | None:
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", (v or "").strip())
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else None


def to_frame(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, dict]]:
    df = df[df["license_type"].isin(KIND)]
    rows, attrs = [], {}
    for r in df.to_dict("records"):
        key = r["license_number"].strip()
        kind = KIND[r["license_type"]]
        rows.append({
            "natural_key": key,
            "account_type": "management_company" if kind == "firm" else "individual",
            "name_raw": clean(r["name"]),
            "name_normalized": normalize_name(r["name"]),
            "address_line_1": clean(r["addr1"]),
            "city": clean(r["city"]),
            "state": clean(r["state"]),
            "zip5": _zip5(r["zip"]),
            "segment": kind,
        })
        attrs[key] = drop_empty({
            "license_kind": kind,
            "dba": [d.strip() for d in r["dba"].split(";") if d.strip()],
            "addr2": clean(r["addr2"]),
            "addr3": clean(r["addr3"]),
            "county_code": clean(r["county_code"]),
            "primary_status": clean(r["primary_status"]),
            "secondary_status": clean(r["secondary_status"]),
            "original_date": _date(r["original_date"]),
            "status_date": _date(r["status_date"]),
            "expiry_date": _date(r["expiry_date"]),
        })
    return pd.DataFrame(rows), attrs


def to_canonical(df: pd.DataFrame) -> pd.DataFrame:
    return build_canonical(
        df.index,
        source_id=SOURCE_ID,
        natural_key=df["natural_key"],
        vertical="hoa",
        account_type=df["account_type"],
        name_raw=df["name_raw"],
        name_normalized=df["name_normalized"],
        address_line_1=df["address_line_1"],
        city=df["city"],
        state=df["state"],
        zip5=df["zip5"],
        segment=df["segment"],
        source_file="lic38cam.csv",
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="lic38cam.csv")
    ap.add_argument("--out", default="fl_dbpr_cam.csv")
    ap.add_argument("--write-db", action="store_true",
                    help="Also write to Postgres staging (requires DATABASE_URL).")
    args = ap.parse_args()

    raw_df, raw = load_raw(args.path)
    assert_source_shape(raw_df)
    df, attrs = to_frame(raw_df)
    canonical = to_canonical(df)
    canonical.to_csv(args.out, index=False)

    print(f"  {len(df):,} licensees -> {args.out}", file=sys.stderr)
    for seg, n in df["segment"].value_counts().items():
        print(f"    {seg:<10} {n:>7,}", file=sys.stderr)

    if args.write_db:
        write_staging(SOURCE_ID, canonical, attrs, raw)


if __name__ == "__main__":
    main()
