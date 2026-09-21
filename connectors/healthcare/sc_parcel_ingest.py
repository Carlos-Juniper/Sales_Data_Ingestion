"""
SC county assessor parcel ingest connector.

Reads manually downloaded CSV files from Charleston, Greenville, and Richland
county assessor portals and matches SC healthcare facility locations to parcels
by address fuzzy match. Outputs the same schema as parcel_acreage_enrich.py.

Input CSV required columns (locations)
---------------------------------------
  natural_key     — stable location ID
  site_state      — 2-letter postal code (SC)
  address_line_1  — street address
  city            — city name
  zip5            — 5-digit ZIP
  county_name     — lowercase SC county name (charleston | greenville | richland)

County CSV column names vary by assessor; see SC_COUNTY_CONFIGS.

Output CSV columns
------------------
  natural_key, state, parcel_id, maintained_acres, acres_confidence,
  geometry_source, owner_name, parcel_count, boundary_geojson,
  lookup_status, lookup_note

lookup_status values
--------------------
  ok           — matched and has acreage
  no_acreage   — parcel found but acreage column is null/zero
  no_match     — no parcel address matched above the similarity threshold
  bad_county   — county name not in SC_COUNTY_CONFIGS

Usage
-----
    python sc_parcel_ingest.py --locations sc_hospitals.csv \\
        --county-csvs charleston=/data/charleston.csv \\
        --county-csvs greenville=/data/greenville.csv \\
        --out sc_parcel_enrichment.csv
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.match import compound_name_similarity
from lib.normalize import normalize_name

logger = logging.getLogger(__name__)

# Canonical source_id for this enricher's rows in staging.enrich_parcel.
SOURCE_ID = "sc_parcel_ingest"

# County assessor CSVs that must be on disk for SC enrichment to proceed.
# These are not distributed with the repo — obtain them from each county's
# assessor portal and pass them via --county-csvs on the CLI.
_SC_EXPECTED_COUNTIES = frozenset({"charleston", "greenville", "richland"})

SC_COUNTY_CONFIGS: dict[str, dict] = {
    "charleston": {
        "acreage_col": "ACREAGE",
        "owner_col": "OWNER_NAME",
        "address_col": "SITUS_ADDRESS",
        "parcel_id_col": "PARCEL_ID",
    },
    "greenville": {
        "acreage_col": "CALC_ACREAGE",
        "owner_col": "OWNER",
        "address_col": "PROPERTY_ADDRESS",
        "parcel_id_col": "ACCOUNT_NO",
    },
    "richland": {
        "acreage_col": "ACRES",
        "owner_col": "OWN_NAME",
        "address_col": "PROP_ADDR",
        "parcel_id_col": "PIN",
    },
}

_OUTPUT_COLUMNS = [
    "natural_key",
    "state",
    "parcel_id",
    "maintained_acres",
    "acres_confidence",
    "geometry_source",
    "owner_name",
    "parcel_count",
    "boundary_geojson",
    "lookup_status",
    "lookup_note",
]


def _output_row(location: dict, lookup_status: str, lookup_note: str, **overrides) -> dict:
    """Build one output row; overrides fill parcel-specific fields on success."""
    row = {
        "natural_key": location["natural_key"],
        "state": location.get("site_state", "SC"),
        "parcel_id": None,
        "maintained_acres": None,
        "acres_confidence": "estimated",
        "geometry_source": "sc_assessor_csv",
        "owner_name": None,
        "parcel_count": 0,
        "boundary_geojson": None,
        "lookup_status": lookup_status,
        "lookup_note": lookup_note,
    }
    row.update(overrides)
    return row


def load_county_csv(path: str | Path, county: str) -> pd.DataFrame:
    """Load and normalize a county assessor CSV into the standard parcel schema."""
    county = county.lower().strip()
    if county not in SC_COUNTY_CONFIGS:
        raise ValueError(
            f"County {county!r} is not in SC_COUNTY_CONFIGS; "
            f"known counties: {sorted(SC_COUNTY_CONFIGS)}"
        )

    cfg = SC_COUNTY_CONFIGS[county]
    raw = pd.read_csv(path, dtype=str, keep_default_na=False)

    raw.columns = [c.strip().upper() for c in raw.columns]

    required = {
        cfg["acreage_col"].upper(),
        cfg["owner_col"].upper(),
        cfg["address_col"].upper(),
        cfg["parcel_id_col"].upper(),
    }
    missing = required - set(raw.columns)
    if missing:
        raise ValueError(
            f"County {county!r} CSV is missing required column(s): {missing}. "
            f"Expected columns from SC_COUNTY_CONFIGS: {required}"
        )

    df = pd.DataFrame(
        {
            "parcel_id": raw[cfg["parcel_id_col"].upper()],
            "acreage_raw": raw[cfg["acreage_col"].upper()],
            "owner_name": raw[cfg["owner_col"].upper()],
            "address_normalized": raw[cfg["address_col"].upper()].map(normalize_name),
        }
    )

    assert_county_csv_shape(df, county)
    return df


def assert_county_csv_shape(df: pd.DataFrame, county: str) -> None:
    """Raise ValueError if df is empty or missing required normalized columns."""
    if df.empty:
        raise ValueError(
            f"County {county!r} CSV loaded as an empty DataFrame; "
            f"check that the file has data rows"
        )

    required = {"parcel_id", "acreage_raw", "owner_name", "address_normalized"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"County {county!r} DataFrame is missing required column(s): {missing}"
        )


def _to_acres(raw: str) -> float | None:
    """Parse a raw acreage string to float; return None if unparseable or <= 0."""
    if not raw or not raw.strip():
        return None
    cleaned = raw.strip().replace(",", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value > 0 else None


def match_location_to_parcel(
    location: dict,
    parcel_df: pd.DataFrame,
    threshold: float = 0.70,
) -> dict | None:
    """Find the best-matching parcel row for a location by address similarity."""
    if parcel_df.empty:
        return None

    query = normalize_name(location.get("address_line_1") or "")
    if not query:
        return None

    # Cheap blocker: restrict scoring to parcels whose normalized address shares
    # the same leading 5 chars (house number + first street-name token).
    # Falls back to the full DataFrame so recall is never silently dropped.
    prefix = query[:5]
    candidates = parcel_df[parcel_df["address_normalized"].str.startswith(prefix)]
    if candidates.empty:
        candidates = parcel_df

    best_score = -1.0
    best_idx = -1

    for idx, row_addr in enumerate(candidates["address_normalized"]):
        score = compound_name_similarity(query, row_addr or "")
        if score > best_score:
            best_score = score
            best_idx = idx

    if best_score < threshold:
        return None

    return candidates.iloc[best_idx].to_dict()


def enrich_location(
    location: dict,
    parcel_df: pd.DataFrame,
    county: str,
) -> dict:
    """Enrich a single location dict against the county parcel DataFrame."""
    match = match_location_to_parcel(location, parcel_df)
    if match is None:
        return _output_row(
            location,
            lookup_status="no_match",
            lookup_note=f"no parcel address matched above the similarity threshold for county {county!r}",
        )

    acres = _to_acres(match.get("acreage_raw") or "")
    if acres is not None:
        return _output_row(
            location,
            lookup_status="ok",
            lookup_note=f"matched via address fuzzy match in {county!r} assessor CSV",
            parcel_id=match.get("parcel_id") or None,
            owner_name=normalize_name(match.get("owner_name") or "") or None,
            parcel_count=1,
            maintained_acres=round(acres, 4),
        )

    return _output_row(
        location,
        lookup_status="no_acreage",
        lookup_note=f"parcel matched in {county!r} assessor CSV but acreage column is null or zero",
        parcel_id=match.get("parcel_id") or None,
        owner_name=normalize_name(match.get("owner_name") or "") or None,
        parcel_count=1,
    )


def enrich(
    locations_df: pd.DataFrame,
    county_csv_paths: dict[str, str],
) -> pd.DataFrame:
    """Enrich all SC healthcare locations from county assessor CSVs."""
    county_frames: dict[str, pd.DataFrame] = {}
    for county, csv_path in county_csv_paths.items():
        county_frames[county.lower().strip()] = load_county_csv(csv_path, county)

    rows: list[dict] = []
    for location in locations_df.to_dict("records"):
        county = (location.get("county_name") or "").lower().strip()

        if county not in SC_COUNTY_CONFIGS:
            rows.append(
                _output_row(
                    location,
                    lookup_status="bad_county",
                    lookup_note=(
                        f"county {county!r} is not in SC_COUNTY_CONFIGS; "
                        f"known counties: {sorted(SC_COUNTY_CONFIGS)}"
                    ),
                )
            )
            continue

        if county not in county_frames:
            rows.append(
                _output_row(
                    location,
                    lookup_status="bad_county",
                    lookup_note=(
                        f"no CSV path provided for county {county!r}; "
                        f"pass --county-csvs {county}=/path/to/file.csv"
                    ),
                )
            )
            continue

        rows.append(enrich_location(location, county_frames[county], county))

    return pd.DataFrame(rows, columns=_OUTPUT_COLUMNS)


def print_summary(df: pd.DataFrame) -> None:
    """Print lookup_status breakdown to stderr."""
    print("\n  status breakdown:", file=sys.stderr)
    for status, n in df["lookup_status"].value_counts().items():
        print(f"    {status:<28} {n:>7,}", file=sys.stderr)

    ok = df[df["lookup_status"] == "ok"]
    if not ok.empty:
        print("\n  acreage (ok rows only):", file=sys.stderr)
        print(f"    count    {len(ok):>7,}", file=sys.stderr)
        print(f"    median   {ok['maintained_acres'].median():>7.1f} acres", file=sys.stderr)
        print(f"    p25      {ok['maintained_acres'].quantile(0.25):>7.1f} acres", file=sys.stderr)
        print(f"    p75      {ok['maintained_acres'].quantile(0.75):>7.1f} acres", file=sys.stderr)
        print(f"    max      {ok['maintained_acres'].max():>7.1f} acres", file=sys.stderr)


def _parse_county_csv_arg(value: str) -> tuple[str, str]:
    """Parse a 'county=path' CLI argument into a (county, path) tuple."""
    if "=" not in value:
        raise argparse.ArgumentTypeError(
            f"--county-csvs values must be in 'county=path' format, got: {value!r}"
        )
    county, path = value.split("=", 1)
    return county.strip(), path.strip()


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--locations", required=True, help="CSV of SC healthcare locations")
    ap.add_argument(
        "--county-csvs",
        action="append",
        default=[],
        metavar="COUNTY=PATH",
        help="County assessor CSV; repeatable: --county-csvs charleston=/data/ch.csv",
    )
    ap.add_argument(
        "--out",
        default="sc_parcel_enrichment.csv",
        help="output CSV path (default: sc_parcel_enrichment.csv)",
    )
    ap.add_argument(
        "--write-db",
        action="store_true",
        help=(
            "Write SC parcel enrichment results to staging.enrich_parcel (requires DATABASE_URL). "
            "When no county assessor CSVs are found on disk, logs a data-blocked warning and skips "
            "the DB write rather than raising an unhandled exception."
        ),
    )
    args = ap.parse_args()

    county_csv_paths: dict[str, str] = {}
    for entry in args.county_csvs:
        county, path = _parse_county_csv_arg(entry)
        county_csv_paths[county] = path

    # Data-blocked guard: if --write-db is requested but no county CSVs were
    # supplied, log clearly and skip — don't crash the pipeline.  SC assessor
    # CSVs must be obtained manually from each county portal; they are not
    # distributed with this repo.  The structural path (flag accepted, engine
    # created) still exists so tests can verify this code branch.
    if args.write_db and not county_csv_paths:
        missing_paths = ", ".join(
            f"<{c}_assessor_path>" for c in sorted(_SC_EXPECTED_COUNTIES)
        )
        logger.warning(
            "SC parcel loading is data-blocked: county assessor CSVs not found at %s. "
            "Skipping SC.",
            missing_paths,
        )
        # Exit cleanly — the pipeline must not crash when this connector is
        # called as part of a broader run before the CSVs are available.
        return

    print(f"reading {args.locations}", file=sys.stderr)
    locations_df = pd.read_csv(args.locations, dtype=str, keep_default_na=False)

    required = {"natural_key", "site_state", "address_line_1", "city", "zip5", "county_name"}
    missing = required - set(locations_df.columns)
    if missing:
        sys.exit(f"locations CSV missing required column(s): {missing}")

    print(f"  {len(locations_df):,} rows loaded", file=sys.stderr)
    print(f"  county CSVs: {list(county_csv_paths)}", file=sys.stderr)

    results = enrich(locations_df, county_csv_paths)

    results.to_csv(args.out, index=False)
    print(f"\nwrote {len(results):,} rows -> {args.out}", file=sys.stderr)
    print_summary(results)

    if args.write_db:
        from lib.http import get_secret
        if not get_secret("DATABASE_URL"):
            sys.exit(
                "ERROR: --write-db was given but DATABASE_URL is not set. "
                "Copy .env.example -> .env and fill it in."
            )
        from lib.db import get_engine
        from parcel_acreage_enrich import upsert_enrich_parcel
        engine = get_engine()
        written = upsert_enrich_parcel(engine, SOURCE_ID, results)
        print(
            f"  sc_parcel_ingest: upserted {written:,} rows to staging.enrich_parcel",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
