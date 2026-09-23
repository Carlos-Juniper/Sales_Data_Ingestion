"""
Google Places supplemental enrichment for TX TREC HOA associations.

Takes the canonical output of ``tx_trec_hoa.py`` (which has `legal_name`,
`site_city`, `site_state`, `site_zip`, and `contact_status='pending_pdf'` on
every row — see its ``to_canonical()``), looks up each association's
phone/website via the Places API Text Search (New) endpoint using
name + city + state + zip (there is no street address at this pass — a
street address only comes from the certificate-PDF OCR pass, see
``tx_trec_pdf_enrich.py``), and — when a website is found — crawls it for a
contact email.

This is a supplemental pass that runs AFTER ``tx_trec_pdf_enrich.py`` and
defers to it: pass ``--pdf-contact-csv`` (that connector's ``--out`` CSV) and
any association it already produced a usable ``mgmt_phone``/``mgmt_email``
for is skipped here — per ``Ingestion-Plan-of-Action.md`` §5.3, a state
regulator's own filing outranks a Places API guess. Places fills the gap for
associations OCR came back empty on, plus supplies ``website``, which the
certificate often lacks. ``--pdf-contact-csv`` is optional; without it, every
``contact_status='pending_pdf'`` row is looked up (the OCR-completeness check
is simply skipped — useful for a standalone/parallel-track run, but do not
run it that way in the real pipeline once OCR exists, see ``scripts/run_hoa.sh``).

Ported vs written fresh
------------------------
``lib/google_places.py`` (Places Text Search client, ``best_match`` scorer,
rate limiter) and ``lib/website_contact.py`` (robots.txt + email crawl) port
logic from two prior-prototype repos in the ``juniperlandscaping`` org
(``juniper-crm-shared/scrapers/google_maps.py`` and ``.../scrapers/base.py``),
adapted from ``httpx``/``asyncio`` to ``requests``/``ThreadPoolExecutor`` to
match this repo's synchronous connector style — see those modules' docstrings
for the detailed diff. This file (the enrichment shape: eligibility mask,
per-row lookup, ``enrich_status`` bookkeeping, ``upsert_*``, ``print_summary``,
CLI) is written fresh, closely following
``deathcare/irs_990_enrich.py`` — the closest existing analog: a slow,
separate enrichment pass over an upstream connector's canonical CSV output.

Column naming note
-------------------
The Places lookup writes to ``gmaps_phone`` / ``gmaps_website`` /
``gmaps_contact_email`` / ``gmaps_place_id`` rather than the canonical
``phone`` / ``email`` columns that ``tx_trec_hoa.to_canonical()`` already
emits (currently always empty). Those canonical columns are reserved for the
PDF pass's authoritative extraction — per the plan of action's survivorship
rules (`Ingestion-Plan-of-Action.md` §5.3), a state regulator's own filing
should win over a Places API guess, so this connector never writes into
them directly; a later merge/survivorship step decides how the two sources
combine.

Status handling
----------------
``enrich_status`` is one of ``lib.enums.ENRICH_OK`` / ``ENRICH_NOT_FOUND`` /
``ENRICH_ERROR`` / ``ENRICH_SKIPPED``:

  - ``skipped``   — row's ``contact_status`` isn't ``'pending_pdf'`` (mirrors
                    the BMF ``segment``+``ein`` mask pattern in
                    ``irs_990_enrich.enrich()``), OR ``pdf_contact_df`` was
                    given and already carries a usable ``mgmt_phone``/
                    ``mgmt_email`` for this association's ``natural_key``.
  - ``not_found`` — Places returned zero results, OR it matched a place but
                    that place had neither a phone nor a website (there's
                    nothing to write in either case, and the enum has no
                    separate "matched but empty" bucket).
  - ``error``     — the Places request raised after retries, or the email
                    crawl/parsing raised unexpectedly; ``error_detail``
                    captures the exception type and message.
  - ``ok``        — a phone or website was found (and, when a website was
                    found, an email crawl was attempted — its result does
                    not affect the status either way).
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import pandas as pd
import requests
from sqlalchemy import text

from lib.enrich_runner import run_enrichment
from lib.enums import ENRICH_ERROR, ENRICH_NOT_FOUND, ENRICH_OK, ENRICH_SKIPPED
from lib.google_places import GooglePlacesClient
from lib.http import get_secret
from lib.website_contact import RobotsCache, extract_emails_from_website

# ---------------------------------------------------------------- constants

# Matches tx_trec_hoa.SOURCE_ID — this enricher's cache table is keyed the
# same way staging.tx_trec_hoa is keyed, (source_id, natural_key), so a JOIN
# against the upstream staging table is a straight equi-join on both columns.
SOURCE_ID = "tx_trec_hoa"

_ELIGIBLE_CONTACT_STATUS = "pending_pdf"

_REQUIRED_INPUT_COLUMNS = {
    "natural_key", "legal_name", "site_city", "site_state", "site_zip", "contact_status",
}

# Columns tx_trec_pdf_enrich.py's --out CSV must carry for the OCR-completeness
# check below. natural_key is the join key; mgmt_phone/mgmt_email are what
# "already has a real contact" means for that pass (see trec_certificate_parser.py).
PDF_CONTACT_REQUIRED_COLUMNS = {"natural_key", "mgmt_phone", "mgmt_email"}


def _has_pdf_contact(mgmt_phone: Any, mgmt_email: Any) -> bool:
    """True when the OCR pass already produced a usable phone or email."""
    return bool(
        (isinstance(mgmt_phone, str) and mgmt_phone.strip())
        or (isinstance(mgmt_email, str) and mgmt_email.strip())
    )


# ---------------------------------------------------------------- per-row lookup


def enrich_one(
    row: dict[str, Any],
    client: GooglePlacesClient,
    session: requests.Session,
    robots: RobotsCache,
) -> dict[str, Any]:
    """
    Look up one association via Google Places, then (if a website was found)
    crawl it for a contact email.

    Parameters
    ----------
    row:
        dict with at least ``natural_key``, ``legal_name``, ``site_city``,
        ``site_state``, ``site_zip`` (one record from the canonical CSV).
    client:
        Shared ``GooglePlacesClient`` — thread-safe; it rate-limits internally
        via its own ``RateLimiter`` regardless of how many worker threads
        call it concurrently.
    session:
        Shared ``requests.Session``, reused for the email crawl. Thread-safe
        for concurrent use — see the comment in ``irs_990_enrich.enrich()``
        explaining why sharing one Session's connection pool is safe and
        preferable to one per thread.
    robots:
        Shared ``RobotsCache`` so a given website's robots.txt is fetched at
        most once across all worker threads.

    Returns
    -------
    dict with keys: natural_key, phone, website, maps_place_id,
    contact_email, enrich_status, error_detail.
    """
    result: dict[str, Any] = {
        "natural_key": row["natural_key"],
        "phone": None,
        "website": None,
        "maps_place_id": None,
        "contact_email": None,
        "enrich_status": ENRICH_ERROR,
        "error_detail": None,
    }

    try:
        biz = client.lookup_business(
            name=row["legal_name"],
            city=row["site_city"],
            state=row["site_state"],
            zip_code=row["site_zip"],
        )

        if not biz["found"]:
            result["enrich_status"] = ENRICH_NOT_FOUND
            return result

        phone = biz.get("phone")
        website = biz.get("website")

        if not phone and not website:
            # Places matched *something* but returned no usable contact
            # field. See the module docstring's "Status handling" note on
            # why this collapses into not_found rather than a partial ok.
            result["enrich_status"] = ENRICH_NOT_FOUND
            return result

        result["phone"] = phone
        result["website"] = website
        result["maps_place_id"] = biz.get("place_id")

        if website:
            try:
                emails = extract_emails_from_website(website, session=session, robots=robots)
            except Exception:
                # Email crawl is a bonus on top of the phone/website lookup —
                # a broken/unreachable website must not turn an otherwise
                # successful Places match into an error row.
                emails = []
            if emails:
                result["contact_email"] = emails[0]

        result["enrich_status"] = ENRICH_OK

    except Exception as exc:
        result["error_detail"] = f"{type(exc).__name__}: {exc}"
        result["enrich_status"] = ENRICH_ERROR

    return result


# ---------------------------------------------------------------- batch enrichment


def enrich(
    df: pd.DataFrame,
    api_key: str,
    workers: int = 4,
    rate_pause: float = 0.2,
    pdf_contact_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Enrich the TX TREC HOA canonical CSV with Places phone/website/email.

    Only rows where ``contact_status == 'pending_pdf'`` AND (no usable
    contact already exists from the OCR pass) are looked up. All other rows
    receive ``enrich_status='skipped'`` with null enrichment fields.

    Parameters
    ----------
    df:
        DataFrame produced by ``tx_trec_hoa.to_canonical()``. Must contain
        ``contact_status``, ``natural_key``, ``legal_name``, ``site_city``,
        ``site_state``, ``site_zip``.
    api_key:
        Google Places API key (caller is responsible for loading it via
        ``lib.http.get_secret`` and failing early if absent — see ``main()``).
    workers:
        Thread pool size (default 4). Set to 1 to serialize for debugging.
    rate_pause:
        Minimum seconds between consecutive Places API request starts,
        shared across all worker threads (see ``lib.google_places.RateLimiter``).
    pdf_contact_df:
        Optional DataFrame from ``tx_trec_pdf_enrich.py``'s ``--out`` CSV
        (must contain ``natural_key``, ``mgmt_phone``, ``mgmt_email``). Rows
        whose ``natural_key`` already has a non-empty ``mgmt_phone`` or
        ``mgmt_email`` there are treated as already covered by the OCR pass
        and skipped here, per the survivorship rule in the module docstring.
        A ``natural_key`` absent from this frame (OCR hasn't produced a
        result for it yet, or produced an empty one) remains eligible. When
        ``None`` (the default), no OCR-completeness check is applied — see
        the module docstring's caveat about running this pass standalone.

    Returns
    -------
    The input DataFrame with six new columns appended: gmaps_phone,
    gmaps_website, gmaps_place_id, gmaps_contact_email, enrich_status,
    error_detail.
    """
    df = df.copy()

    df["gmaps_phone"] = None
    df["gmaps_website"] = None
    df["gmaps_place_id"] = None
    df["gmaps_contact_email"] = None
    df["enrich_status"] = ENRICH_SKIPPED
    df["error_detail"] = None

    eligible_mask = df["contact_status"] == _ELIGIBLE_CONTACT_STATUS

    if pdf_contact_df is not None:
        pdf_lookup = (
            pdf_contact_df[["natural_key", "mgmt_phone", "mgmt_email"]]
            .drop_duplicates(subset="natural_key", keep="last")
            .set_index("natural_key")
        )
        already_covered = df["natural_key"].map(
            lambda nk: _has_pdf_contact(
                pdf_lookup.at[nk, "mgmt_phone"] if nk in pdf_lookup.index else None,
                pdf_lookup.at[nk, "mgmt_email"] if nk in pdf_lookup.index else None,
            )
        )
        eligible_mask = eligible_mask & ~already_covered
        sys.stderr.write(
            f"  hoa_gmaps_enrich: {int(already_covered.sum()):,} rows already "
            "have an OCR-sourced contact — skipping those\n"
        )

    eligible_idx = df.index[eligible_mask].tolist()

    if not eligible_idx:
        sys.stderr.write("  hoa_gmaps_enrich: 0 eligible rows — nothing to enrich\n")
        return df

    # One shared Session for both the Places lookups and the email crawl —
    # thread-safe for concurrent use (urllib3's connection pool is internally
    # locked), same reasoning as irs_990_enrich.enrich().
    session = requests.Session()
    client = GooglePlacesClient(api_key, session=session, rate_pause=rate_pause)
    robots = RobotsCache(session=session)

    rows_to_enrich = df.loc[eligible_idx].to_dict(orient="records")
    total = len(rows_to_enrich)
    sys.stderr.write(f"  hoa_gmaps_enrich: {total:,} eligible rows\n")

    items = list(zip(eligible_idx, rows_to_enrich))
    results = run_enrichment(
        items,
        fn=lambda row: enrich_one(row, client, session, robots),
        workers=workers,
        label="hoa gmaps enrich",
        progress_interval=50,
    )

    for idx, res in results:
        df.at[idx, "gmaps_phone"] = res["phone"]
        df.at[idx, "gmaps_website"] = res["website"]
        df.at[idx, "gmaps_place_id"] = res["maps_place_id"]
        df.at[idx, "gmaps_contact_email"] = res["contact_email"]
        df.at[idx, "enrich_status"] = res["enrich_status"]
        df.at[idx, "error_detail"] = res["error_detail"]

    return df


# ---------------------------------------------------------------- db write


def upsert_enrich_hoa_gmaps(engine, df: pd.DataFrame) -> int:
    """
    Upsert enriched rows into staging.enrich_hoa_gmaps.

    PK is (source_id, natural_key), matching every other staging.enrich_*
    cache table (see db/migrations/012_enrich_tables.sql). Only rows where
    enrich_status is not 'skipped' are written — skipped rows have no
    enrichment data — following upsert_enrich_irs990() exactly.

    Returns the number of rows written.
    """
    eligible = df[df["enrich_status"] != ENRICH_SKIPPED].copy()
    if eligible.empty:
        return 0

    eligible["source_id"] = SOURCE_ID

    sql = text("""
        INSERT INTO staging.enrich_hoa_gmaps (
            source_id, natural_key,
            phone, website, maps_place_id, contact_email, enrich_status
        ) VALUES (
            :source_id, :natural_key,
            :phone, :website, :maps_place_id, :contact_email, :enrich_status
        )
        ON CONFLICT (source_id, natural_key) DO UPDATE SET
            phone         = EXCLUDED.phone,
            website       = EXCLUDED.website,
            maps_place_id = EXCLUDED.maps_place_id,
            contact_email = EXCLUDED.contact_email,
            enrich_status = EXCLUDED.enrich_status,
            enriched_at   = now()
    """)

    rows = eligible.rename(columns={
        "gmaps_phone": "phone",
        "gmaps_website": "website",
        "gmaps_place_id": "maps_place_id",
        "gmaps_contact_email": "contact_email",
    })[[
        "source_id", "natural_key", "phone", "website",
        "maps_place_id", "contact_email", "enrich_status",
    ]].to_dict(orient="records")

    with engine.begin() as conn:
        conn.execute(sql, rows)

    return len(rows)


# ---------------------------------------------------------------- summary


def print_summary(df: pd.DataFrame) -> None:
    """Log enrichment summary statistics to stderr — status breakdown and
    fill rates for the three enriched contact fields."""
    total = len(df)
    ok = (df["enrich_status"] == ENRICH_OK).sum()
    skipped = (df["enrich_status"] == ENRICH_SKIPPED).sum()
    not_found = (df["enrich_status"] == ENRICH_NOT_FOUND).sum()
    errors = (df["enrich_status"] == ENRICH_ERROR).sum()

    sys.stderr.write("\n  hoa gmaps enrich summary:\n")
    sys.stderr.write(f"    total rows       {total:>7,}\n")
    sys.stderr.write(f"    enriched (ok)    {ok:>7,}\n")
    sys.stderr.write(f"    skipped          {skipped:>7,}\n")
    sys.stderr.write(f"    not_found        {not_found:>7,}\n")
    sys.stderr.write(f"    error            {errors:>7,}\n")

    if total > 0:
        for label, col in (
            ("phone", "gmaps_phone"),
            ("website", "gmaps_website"),
            ("contact_email", "gmaps_contact_email"),
        ):
            filled = df[col].notna().sum()
            sys.stderr.write(
                f"    {label:<16} {filled:>7,}  ({100 * filled / total:.1f}% filled)\n"
            )


# ---------------------------------------------------------------- entrypoint


def main() -> None:
    """
    CLI entrypoint — enrich tx_trec_hoa canonical output via the Places API.

    Reads the canonical CSV (produced by tx_trec_hoa.py --out), enriches
    pending_pdf rows with phone/website/email from Google Places, and
    optionally upserts results into staging.enrich_hoa_gmaps.
    """
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--input",
        required=True,
        metavar="CSV_PATH",
        help="Path to the canonical TX TREC HOA CSV (output of tx_trec_hoa.py --out).",
    )
    ap.add_argument(
        "--pdf-contact-csv",
        default=None,
        metavar="CSV_PATH",
        help="Path to tx_trec_pdf_enrich.py's --out CSV. When given, associations "
             "it already found a usable mgmt_phone/mgmt_email for are skipped here "
             "(the OCR filing outranks a Places guess — see module docstring). "
             "Optional but strongly recommended once the OCR pass has run.",
    )
    ap.add_argument(
        "--out",
        default="hoa_gmaps_enriched.csv",
        help="Output CSV path (default: hoa_gmaps_enriched.csv)",
    )
    ap.add_argument(
        "--write-db",
        action="store_true",
        help="Also upsert enriched rows into staging.enrich_hoa_gmaps "
             "(requires DATABASE_URL). Off by default.",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Thread pool size for concurrent Places API calls (default: 4).",
    )
    ap.add_argument(
        "--rate-pause",
        type=float,
        default=0.2,
        help="Minimum seconds between consecutive Places API request starts, "
             "shared across all worker threads (default: 0.2).",
    )
    args = ap.parse_args()

    sys.stderr.write(f"  hoa_gmaps_enrich: reading {args.input}\n")
    canonical = pd.read_csv(args.input, dtype=str)

    missing = _REQUIRED_INPUT_COLUMNS - set(canonical.columns)
    if missing:
        sys.exit(
            f"ERROR: input CSV missing required column(s): {sorted(missing)}. "
            "Run tx_trec_hoa.py first to produce the canonical output."
        )

    # Fail clearly and early — before any lookups run — rather than letting
    # the first Places request raise a confusing 401/403 deep in a worker
    # thread. Checked unconditionally (not just under --write-db) because
    # every invocation of this connector's main() performs live lookups.
    api_key = get_secret("GOOGLE_MAPS_API_KEY")
    if not api_key:
        sys.exit(
            "ERROR: GOOGLE_MAPS_API_KEY is not set. Copy .env.example -> .env "
            "and fill in a Places API key "
            "(https://developers.google.com/maps/documentation/places/web-service/text-search)."
        )

    pdf_contact_df = None
    if args.pdf_contact_csv:
        sys.stderr.write(f"  hoa_gmaps_enrich: reading {args.pdf_contact_csv}\n")
        pdf_contact_df = pd.read_csv(args.pdf_contact_csv, dtype=str)
        missing_pdf = PDF_CONTACT_REQUIRED_COLUMNS - set(pdf_contact_df.columns)
        if missing_pdf:
            sys.exit(
                f"ERROR: --pdf-contact-csv missing required column(s): {sorted(missing_pdf)}. "
                "Run tx_trec_pdf_enrich.py first to produce that CSV."
            )

    enriched = enrich(
        canonical,
        api_key=api_key,
        workers=args.workers,
        rate_pause=args.rate_pause,
        pdf_contact_df=pdf_contact_df,
    )
    print_summary(enriched)

    enriched.to_csv(args.out, index=False)
    sys.stderr.write(f"\n  wrote {len(enriched):,} rows -> {args.out}\n")

    if args.write_db:
        from lib.db import get_engine

        if not get_secret("DATABASE_URL"):
            sys.exit(
                "ERROR: --write-db was given but DATABASE_URL is not set. "
                "Copy .env.example -> .env and fill it in."
            )

        engine = get_engine()
        n_written = upsert_enrich_hoa_gmaps(engine, enriched)
        sys.stderr.write(
            f"  hoa_gmaps_enrich: wrote {n_written:,} rows to staging.enrich_hoa_gmaps\n"
        )


if __name__ == "__main__":
    main()
