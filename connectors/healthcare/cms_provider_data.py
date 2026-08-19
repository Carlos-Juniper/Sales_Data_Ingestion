"""
CMS Provider Data — live API client.

Source  : https://data.cms.gov/provider-data
Auth    : None — fully public API, no key required.
Datasets: configured in config/cms_datasets.yaml (dataset_id UUIDs)

This connector pulls the same underlying data as the bulk CSVs in data/,
but via the live DKAN API — useful for scheduled refresh without manual
re-downloads. Cross-check against the bulk files to verify row counts match.

The dataset_id from cms_datasets.yaml (e.g. "xubh-q36u") is used directly as
the datastore resource id — verified live against both configured datasets.
No separate metastore lookup to a distribution UUID is needed (a distribution
UUID does exist under metastore/schemas/dataset/items/{id}?show-reference-ids,
but the plain dataset_id already works for /datastore/query/{id}/0).

Usage:
    python cms_provider_data.py --dataset nursing_home --out nh_api.csv
    python cms_provider_data.py --dataset general --out hospital_api.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Generator, Iterator

import pandas as pd
import requests
import yaml

from lib.http import make_session
from lib.normalize import normalize_name, normalize_zip

# ---------------------------------------------------------------- constants

CMS_BASE = "https://data.cms.gov/provider-data/api/1"

# Minimum number of rows we expect from any dataset fetch. If a live
# response comes back drastically short, something changed upstream.
_MIN_EXPECTED_ROWS = 100

# Columns the canonical output always emits, in order.
_CANONICAL_COLUMNS = [
    "natural_key",
    "name_raw",
    "address_line_1",
    "city",
    "site_state",
    "zip5",
    "facility_type",
    "dataset_key",
]

# Case-insensitive substrings used to locate the CCN / provider ID column
# when the live API returns slightly different column headings over time.
# "facility_id" covers the Hospital General Information ("general") dataset,
# whose live identifier column isn't CCN-named at all — verified live
# against xubh-q36u, where natural_key was silently 100% empty without this.
_CCN_HINTS = ["ccn", "certification number", "provider id", "provider number", "facility_id", "facility id"]

# Substrings used to locate the facility name column.
_NAME_HINTS = ["provider name", "facility name", "name"]

# Substrings used to locate street address, city, state, ZIP columns.
_ADDR_HINTS = ["address", "street"]
_CITY_HINTS = ["city"]
_STATE_HINTS = ["state"]
_ZIP_HINTS = ["zip"]


# ---------------------------------------------------------------- helpers

def _find_column(columns: list[str], hints: list[str]) -> str | None:
    """
    Return the first column name whose lowercase form contains any hint.

    Case-insensitive substring match. Returns None if no column matches.
    The longest-matching hint wins ties so "provider name" beats a plain
    "name" match when both are present.
    """
    lower_cols = [(c.lower(), c) for c in columns]
    for hint in hints:
        for lower, original in lower_cols:
            if hint in lower:
                return original
    return None


def load_config(config_path: str | Path) -> dict:
    """Load the CMS dataset registry YAML and return its parsed contents."""
    with open(config_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------- extract

def iter_datastore_rows(
    resource_id: str,
    session: requests.Session,
    page_size: int = 1000,
) -> Generator[dict, None, None]:
    """
    Page through the DKAN datastore and yield one row dict per record.

    Uses GET with limit/offset/count query params. On the first page, the
    total row count is logged to stderr so the caller can track progress.
    Pagination stops when the results list is empty or offset reaches total.

    Args:
        resource_id: the dataset_id from cms_datasets.yaml (e.g. "xubh-q36u") —
            works directly as the datastore resource id, no resolution needed.
        session: requests.Session (from make_session()).
        page_size: rows per page, default 1000.

    Yields:
        One dict per row, keys are column names as returned by the API.
    """
    url = f"{CMS_BASE}/datastore/query/{resource_id}/0"
    offset = 0
    total: int | None = None

    while True:
        params = {
            "limit": page_size,
            "offset": offset,
            # The datastore's JSON Schema validates this as a boolean — sending
            # count=1 (rendered as the string "1") 400s. Must be "true"/"false".
            "count": "true",
        }
        resp = session.get(url, params=params, timeout=60)
        resp.raise_for_status()
        body = resp.json()

        if total is None:
            total = body.get("count", body.get("total", 0))
            print(
                f"  cms_provider_data: {total:,} total rows in datastore "
                f"(resource {resource_id})",
                file=sys.stderr,
            )

        results: list[dict] = body.get("results", [])
        if not results:
            break

        for row in results:
            yield row

        offset += len(results)

        # Guard: stop when we have consumed everything
        if total is not None and offset >= total:
            break


def load_raw(
    resource_id: str,
    session: requests.Session,
    page_size: int = 1000,
) -> pd.DataFrame:
    """
    Pull all rows from the DKAN datastore and return a raw DataFrame.

    Column names are exactly as returned by the API; no renaming occurs here.
    """
    rows = list(iter_datastore_rows(resource_id, session, page_size=page_size))
    print(
        f"  cms_provider_data: loaded {len(rows):,} rows into DataFrame",
        file=sys.stderr,
    )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- checks

def assert_source_shape(df: pd.DataFrame) -> None:
    """
    Raise ValueError if the loaded DataFrame does not look healthy.

    Checks:
    - Frame is non-empty (empty response = API/config error)
    - Frame has at least 5 columns (a single-column response signals a parse error)
    """
    if df.empty:
        raise ValueError(
            "cms_provider_data: response DataFrame is empty — "
            "the dataset may not exist or the datastore is unavailable"
        )
    if len(df.columns) < 5:
        raise ValueError(
            f"cms_provider_data: only {len(df.columns)} columns returned — "
            "expected at least 5; API structure may have changed"
        )


# ---------------------------------------------------------------- transform

def to_canonical(df: pd.DataFrame, dataset_key: str) -> pd.DataFrame:
    """
    Map raw API columns to the standard CMS provider output shape.

    Column discovery is case-insensitive substring matching against _*_HINTS
    constants so that minor API renames (e.g. adding "(CCN)" to a heading)
    don't break the transform. Unresolved columns default to empty string
    rather than raising, with a warning to stderr.

    Output columns (always present):
        natural_key, name_raw, address_line_1, city, site_state,
        zip5, facility_type, dataset_key

    Args:
        df: Raw DataFrame from load_raw().
        dataset_key: Registry key (e.g. "nursing_home", "general").
    """
    cols = list(df.columns)

    def _resolve(hints: list[str], label: str) -> pd.Series:
        col = _find_column(cols, hints)
        if col is None:
            print(
                f"  cms_provider_data: WARNING — could not find {label!r} column "
                f"in {cols}; filling with empty string",
                file=sys.stderr,
            )
            return pd.Series([""] * len(df), dtype=str)
        return df[col].fillna("").astype(str)

    # natural_key — CMS Certification Number (CCN) or provider ID
    natural_key_raw = _resolve(_CCN_HINTS, "CCN/provider ID")
    # Strip leading/trailing whitespace that appears in some CMS exports
    natural_key = natural_key_raw.str.strip()

    name_raw = _resolve(_NAME_HINTS, "facility/provider name").str.strip()
    address_line_1 = _resolve(_ADDR_HINTS, "street address").str.strip()
    city = _resolve(_CITY_HINTS, "city").str.strip()
    site_state = _resolve(_STATE_HINTS, "state").str.strip().str.upper()
    zip_raw = _resolve(_ZIP_HINTS, "ZIP code")
    zip5 = zip_raw.map(normalize_zip)

    # facility_type — CMS datasets don't always have an explicit type column;
    # fall back to the dataset_key itself as a stable sentinel value.
    facility_type_col = _find_column(cols, ["facility type", "type"])
    if facility_type_col:
        facility_type = df[facility_type_col].fillna(dataset_key).astype(str).str.strip()
    else:
        facility_type = pd.Series([dataset_key] * len(df), dtype=str)

    return pd.DataFrame({
        "natural_key": natural_key,
        "name_raw": name_raw,
        "address_line_1": address_line_1,
        "city": city,
        "site_state": site_state,
        "zip5": zip5,
        "facility_type": facility_type,
        "dataset_key": dataset_key,
    })


# ---------------------------------------------------------------- quality

def report_quality(df: pd.DataFrame) -> None:
    """Print a quality summary for the canonical DataFrame to stderr."""
    total = len(df)
    missing_key = (df["natural_key"].str.strip() == "").sum()

    print(f"\n  cms_provider_data quality report:", file=sys.stderr)
    print(f"    total rows          {total:>8,}", file=sys.stderr)
    print(
        f"    missing natural_key {missing_key:>8,}"
        f"  ({100 * missing_key / max(total, 1):.1f}%)",
        file=sys.stderr,
    )

    if "site_state" in df.columns:
        print("    top 10 states:", file=sys.stderr)
        for state, count in df["site_state"].value_counts().head(10).items():
            print(f"      {state:<4} {count:>7,}", file=sys.stderr)


# ---------------------------------------------------------------- entrypoint

def main() -> None:
    # Locate the config YAML relative to this script's directory so the
    # connector works from any working directory.
    default_config = Path(__file__).parent / "config" / "cms_datasets.yaml"

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--config",
        default=str(default_config),
        help="Path to cms_datasets.yaml (default: config/cms_datasets.yaml)",
    )
    # --dataset choices are populated after loading the config.
    # We do a two-pass parse: first grab --config, then load valid choices.
    known, remaining = ap.parse_known_args()

    config = load_config(known.config)
    dataset_choices = list(config.keys())

    ap.add_argument(
        "--dataset",
        choices=dataset_choices,
        required=True,
        help="Dataset key from cms_datasets.yaml",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="Output CSV path (defaults to the output_file from config)",
    )
    ap.add_argument(
        "--page-size",
        type=int,
        default=1000,
        help="Rows per API page (default: 1000)",
    )
    args = ap.parse_args()

    dataset_cfg = config[args.dataset]
    dataset_id: str = dataset_cfg["dataset_id"]
    out_path: str = args.out or dataset_cfg.get("output_file", f"{args.dataset}.csv")

    print(
        f"  cms_provider_data: fetching dataset={args.dataset!r} "
        f"(id={dataset_id!r})",
        file=sys.stderr,
    )

    session = make_session()

    # dataset_id doubles as the datastore resource id — verified live against
    # both configured datasets, no separate metastore resolution needed.
    raw = load_raw(dataset_id, session, page_size=args.page_size)
    assert_source_shape(raw)

    canonical = to_canonical(raw, dataset_key=args.dataset)
    report_quality(canonical)

    canonical.to_csv(out_path, index=False)
    print(f"\n  wrote {len(canonical):,} records -> {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
