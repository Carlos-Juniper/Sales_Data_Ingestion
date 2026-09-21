"""
IRS Form 990 enrichment for deathcare/religious cemetery records.

Takes the canonical output of irs_bmf_deathcare.py (which has `ein` and
`segment='religious'`), calls the ProPublica Nonprofit Explorer API per EIN,
and adds `phone_990` and `contact_name_990` columns.

This enrichment runs as a separate pass so the BMF connector stays fast and
pure — fetching 990 data for every EIN is slow (~0.5s per call) and only
relevant for the religious-cemetery segment.

ProPublica API
--------------
  Base URL : https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json
  Auth     : None required (public API)
  Rate     : Conservative 0.5s sleep between calls

Extraction strategy
-------------------
  1. organization.phone   — use if present and non-empty
  2. organization.name    — always use when present
  3. filings_with_data[0].principal_officer — contact name fallback when
     organization-level name is missing (rare)
  4. If filings_with_data is absent or empty, both fields stay null

Output columns added
--------------------
  phone_990        — phone string or None
  contact_name_990 — contact/officer name string or None
  enrich_status    — 'ok' | 'not_found' | 'error' | 'skipped'
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

import pandas as pd
import requests
from sqlalchemy import text

from lib.enrich_runner import run_enrichment
from lib.enums import SEGMENT_RELIGIOUS, ENRICH_OK, ENRICH_NOT_FOUND, ENRICH_ERROR, ENRICH_SKIPPED

# ---------------------------------------------------------------- constants

SOURCE_ID = "irs_990_enrich"

_BASE_URL = "https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json"


# ---------------------------------------------------------------- per-EIN lookup


def enrich_ein(
    ein: str,
    session: requests.Session,
    sleep_s: float = 0.5,
) -> dict[str, Any]:
    """
    Call the ProPublica Nonprofit Explorer API for a single EIN.

    Always sleeps ``sleep_s`` seconds after the request to respect the API's
    informal rate limit — the sleep happens even on error so callers in a
    thread pool do not need to coordinate their own throttling.

    Parameters
    ----------
    ein:
        9-digit EIN string (no hyphen), e.g. ``"043783054"``.
    session:
        Shared requests.Session (thread-safe for concurrent .get() calls).
    sleep_s:
        Seconds to sleep after every request (default 0.5).

    Returns
    -------
    dict with keys:
        ein             — the input EIN (unchanged)
        phone_990       — phone string or None
        contact_name_990 — contact/officer name string or None
        status          — ``'ok'`` | ``'not_found'`` | ``'error'``
    """
    url = _BASE_URL.format(ein=ein)
    result: dict[str, Any] = {
        "ein": ein,
        "phone_990": None,
        "contact_name_990": None,
        "status": ENRICH_ERROR,
    }

    try:
        resp = session.get(url, timeout=30)

        if resp.status_code == 404:
            result["status"] = ENRICH_NOT_FOUND
            return result

        resp.raise_for_status()

        data = resp.json()
        org = data.get("organization") or {}

        # Primary: organization.phone — use if present and non-empty.
        phone = org.get("phone")
        if phone and str(phone).strip():
            result["phone_990"] = str(phone).strip()

        # Primary: organization.name — stable org-level name.
        name = org.get("name")
        if name and str(name).strip():
            result["contact_name_990"] = str(name).strip()
        else:
            # Fallback: principal_officer from most recent filing with data.
            # This field is only present on filings that have structured data —
            # older or short-form filers may omit it entirely.
            filings = data.get("filings_with_data") or []
            if filings:
                officer = filings[0].get("principal_officer")
                if officer and str(officer).strip():
                    result["contact_name_990"] = str(officer).strip()

        result["status"] = ENRICH_OK

    except requests.HTTPError as exc:
        # Non-404 HTTP error — record the HTTP status code for diagnostics.
        if exc.response is not None:
            result["error_detail"] = (
                f"HTTPError: {exc.response.status_code}"
            )
        else:
            result["error_detail"] = "HTTPError: (no response)"
    except Exception as exc:
        # Network failure, JSON parse error, etc. — capture type and message
        # so callers can distinguish transient timeouts from parse failures.
        result["error_detail"] = f"{type(exc).__name__}: {exc}"
    finally:
        # Rate-limit sleep fires after every request, regardless of outcome.
        time.sleep(sleep_s)

    return result


# ---------------------------------------------------------------- batch enrichment


def enrich(
    df: pd.DataFrame,
    workers: int = 4,
    sleep_s: float = 0.5,
) -> pd.DataFrame:
    """
    Enrich a DataFrame with ProPublica 990 phone and contact data.

    Only rows where ``segment == 'religious'`` AND ``ein`` is not null are
    processed. All other rows receive ``enrich_status='skipped'`` with null
    enrichment fields.

    Parameters
    ----------
    df:
        DataFrame produced by ``irs_bmf_deathcare.to_canonical()``.
        Must contain ``ein`` and ``segment`` columns.
    workers:
        Thread pool size (default 4). Set to 1 to serialize for debugging.
    sleep_s:
        Seconds to sleep after each API call, passed through to ``enrich_ein``.

    Returns
    -------
    The input DataFrame with three new columns appended:
        phone_990, contact_name_990, enrich_status
    """
    df = df.copy()

    # Pre-populate enrichment columns so every row has them after merge.
    df["phone_990"] = None
    df["contact_name_990"] = None
    df["enrich_status"] = ENRICH_SKIPPED

    # Build the eligible mask: religious segment with a non-null EIN.
    eligible_mask = (
        (df["segment"] == SEGMENT_RELIGIOUS)
        & df["ein"].notna()
        & (df["ein"].astype(str).str.strip() != "")
    )
    eligible_idx = df.index[eligible_mask].tolist()

    if not eligible_idx:
        sys.stderr.write(f"  990 enrich: 0 eligible rows — nothing to enrich\n")
        return df

    # One shared Session is intentional: requests.Session is thread-safe for
    # concurrent .get() calls because urllib3's connection pool is internally
    # locked. Sharing avoids spinning up a new pool per thread, which would
    # defeat connection reuse and overwhelm the API's per-IP limits.
    session = requests.Session()

    rows_to_enrich = df.loc[eligible_idx, "ein"].tolist()
    total = len(rows_to_enrich)
    sys.stderr.write(f"  990 enrich: {total:,} eligible rows\n")

    # run_enrichment expects (idx, row) pairs and passes only `row` to fn,
    # so close over session and sleep_s in the lambda.
    items = list(zip(eligible_idx, rows_to_enrich))
    results = run_enrichment(
        items,
        fn=lambda ein: enrich_ein(ein, session, sleep_s=sleep_s),
        workers=workers,
        label="990 enrich",
        progress_interval=50,
    )

    # Write results back into the DataFrame by index position.
    for idx, res in results:
        df.at[idx, "phone_990"] = res["phone_990"]
        df.at[idx, "contact_name_990"] = res["contact_name_990"]
        df.at[idx, "enrich_status"] = res["status"]

    return df


# ---------------------------------------------------------------- summary


def upsert_enrich_irs990(engine, df: pd.DataFrame) -> int:
    """
    Upsert enriched 990 rows into staging.enrich_irs990.

    PK is (source_id, natural_key) per D6.  Only rows where enrich_status
    is not 'skipped' are written — skipped rows have no enrichment data.
    Rows that were skipped (segment != religious or no EIN) are not inserted;
    if they already exist in the cache they are left unchanged.

    Returns the number of rows written.
    """
    eligible = df[df["enrich_status"] != ENRICH_SKIPPED].copy()
    if eligible.empty:
        return 0

    sql = text("""
        INSERT INTO staging.enrich_irs990 (
            source_id, natural_key,
            phone_990, contact_name_990, enrich_status
        ) VALUES (
            :source_id, :natural_key,
            :phone_990, :contact_name_990, :enrich_status
        )
        ON CONFLICT (source_id, natural_key) DO UPDATE SET
            phone_990        = EXCLUDED.phone_990,
            contact_name_990 = EXCLUDED.contact_name_990,
            enrich_status    = EXCLUDED.enrich_status,
            enriched_at      = now()
    """)

    rows = eligible[
        ["source_id", "natural_key", "phone_990", "contact_name_990", "enrich_status"]
    ].to_dict(orient="records")

    with engine.begin() as conn:
        conn.execute(sql, rows)

    return len(rows)


def print_summary(df: pd.DataFrame) -> None:
    """
    Log enrichment summary statistics to stderr.

    Reports total rows, status breakdown, and fill rates for the two
    enriched fields.
    """
    total = len(df)
    enriched = (df["enrich_status"] == ENRICH_OK).sum()
    skipped = (df["enrich_status"] == ENRICH_SKIPPED).sum()
    not_found = (df["enrich_status"] == ENRICH_NOT_FOUND).sum()
    errors = (df["enrich_status"] == ENRICH_ERROR).sum()

    sys.stderr.write(f"\n  990 enrich summary:\n")
    sys.stderr.write(f"    total rows       {total:>7,}\n")
    sys.stderr.write(f"    enriched (ok)    {enriched:>7,}\n")
    sys.stderr.write(f"    skipped          {skipped:>7,}\n")
    sys.stderr.write(f"    not_found        {not_found:>7,}\n")
    sys.stderr.write(f"    error            {errors:>7,}\n")

    phone_filled = df["phone_990"].notna().sum()
    name_filled = df["contact_name_990"].notna().sum()

    if total > 0:
        sys.stderr.write(
            f"    phone_990        {phone_filled:>7,}  ({100 * phone_filled / total:.1f}% filled)\n"
        )
        sys.stderr.write(
            f"    contact_name_990 {name_filled:>7,}  ({100 * name_filled / total:.1f}% filled)\n"
        )


# ---------------------------------------------------------------- entrypoint


def main() -> None:
    """
    CLI entrypoint — enrich BMF canonical output via ProPublica 990 API.

    Reads the canonical BMF CSV (produced by irs_bmf_deathcare.py), enriches
    religious-segment rows with phone/contact data from the 990 API, and
    optionally upserts results into staging.enrich_irs990.
    """
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--input",
        required=True,
        metavar="CSV_PATH",
        help="Path to the canonical BMF CSV (output of irs_bmf_deathcare.py).",
    )
    ap.add_argument(
        "--out",
        default="irs_990_enriched.csv",
        help="Output CSV path (default: irs_990_enriched.csv)",
    )
    ap.add_argument(
        "--write-db",
        action="store_true",
        help="Also upsert enriched rows into staging.enrich_irs990 "
             "(requires DATABASE_URL). Off by default.",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Thread pool size for concurrent API calls (default: 4).",
    )
    ap.add_argument(
        "--sleep",
        type=float,
        default=0.5,
        help="Seconds to sleep between API calls per thread (default: 0.5).",
    )
    args = ap.parse_args()

    sys.stderr.write(f"  irs_990_enrich: reading {args.input}\n")
    canonical = pd.read_csv(args.input, dtype=str)

    # enrich() expects segment and ein columns to be present.
    if "segment" not in canonical.columns or "ein" not in canonical.columns:
        sys.exit(
            "ERROR: input CSV must contain 'segment' and 'ein' columns. "
            "Run irs_bmf_deathcare.py first to produce the canonical output."
        )

    enriched = enrich(canonical, workers=args.workers, sleep_s=args.sleep)
    print_summary(enriched)

    enriched.to_csv(args.out, index=False)
    sys.stderr.write(f"\n  wrote {len(enriched):,} rows -> {args.out}\n")

    if args.write_db:
        from lib.db import get_engine
        from lib.http import get_secret

        if not get_secret("DATABASE_URL"):
            sys.exit(
                "ERROR: --write-db was given but DATABASE_URL is not set. "
                "Copy .env.example -> .env and fill it in."
            )

        engine = get_engine()
        n_written = upsert_enrich_irs990(engine, enriched)
        sys.stderr.write(
            f"  irs_990_enrich: wrote {n_written:,} rows to staging.enrich_irs990\n"
        )


if __name__ == "__main__":
    main()
