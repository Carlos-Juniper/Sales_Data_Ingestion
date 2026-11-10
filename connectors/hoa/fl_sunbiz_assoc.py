"""
FL Division of Corporations (Sunbiz) — non-profit corporations (F1).

Source : sftp.floridados.gov (public guest login; credentials are NOT stored here)
Files  : doc/Quarterly/Non-Profit/npcordata.zip  -> npcordata0.txt .. npcordata9.txt
         doc/cor/YYYYMMDDc.txt                   daily new filings (all entity types)
         doc/cor/Events/YYYYMMDDce.txt           daily events (status / name changes)
License: Florida public records, Ch. 119 F.S. — free to store and use.

Never scrape search.sunbiz.org: it sits behind a Cloudflare challenge and the
bulk file carries every field we need (docs/hoa-pipeline.md §6 step 1).

Profiled 2026-11-09 against the 2026-10-06 quarterly (202,308 rows, 196,741
DOMNP, all status A).

WHAT IS LOADED
--------------
Every active domestic non-profit (filing type DOMNP), not only the name-regex
hits.  The two §4 recall rules — an F7 code-009 parcel owner, or a registered
agent that is a CAM firm — have to resolve against non-profits whose names lack
an association keyword (`KIRBY CREEK, INC.`), so the merge needs the full set.
`segment` says which side of the filter a row is on:
    association_name  the revised name regex in §6 step 1 matches
    nonprofit_other   it does not; the merge keeps it only via a recall rule

FORMAT TRAPS
------------
* Fixed width, 1,440 characters, latin-1 (476 rows have non-ASCII bytes).
  Quarterly lines end CRLF; daily lines end LF with 4 trailing NUL bytes.
  Slice by position on the decoded line, never split on whitespace.
* There is no county field.  County comes from ZIP or the F7 parcel link.
* Officer and agent person names are personal data.  They are kept in attrs
  for matching and resolved_contact only, and never printed by this module.

Usage:
    python -m hoa.fl_sunbiz_assoc npcordata*.txt [--daily 2026110*c.txt]
        [--events 2026110*ce.txt] --out fl_sunbiz_assoc.csv [--write-db]
"""

from __future__ import annotations

import argparse
import glob
import re
import sys
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional

import pandas as pd

from hoa.fl_load import clean, drop_empty, write_staging
from hoa.fl_match import parse_care_of
from lib.normalize import normalize_name
from lib.schema import build_canonical

SOURCE_ID = "fl_sunbiz_assoc"
RECORD_LEN = 1440

# 0-based [start, end) slices from the Division's corporate file layout,
# checked against N22777 (Hunters Green) in the 2026-10-06 quarterly.
F = {
    "doc": (0, 12), "name": (12, 204), "status": (204, 205), "filing_type": (205, 220),
    "p_addr1": (220, 262), "p_addr2": (262, 304), "p_city": (304, 332),
    "p_state": (332, 334), "p_zip": (334, 344),
    "m_addr1": (346, 388), "m_addr2": (388, 430), "m_city": (430, 458),
    "m_state": (458, 460), "m_zip": (460, 470),
    "file_date": (472, 480), "fei": (480, 494), "more_than_six": (494, 495),
    "last_trx_date": (495, 503),
    "report_year_1": (505, 509), "report_year_2": (518, 522), "report_year_3": (531, 535),
    "ra_name": (544, 586), "ra_kind": (586, 587), "ra_addr": (587, 629),
    "ra_city": (629, 657), "ra_state": (657, 659), "ra_zip": (659, 664),
}
OFFICER_START, OFFICER_LEN, OFFICER_SLOTS = 668, 128, 6
# Offsets inside one 128-char officer block.
OFF = {"title": (0, 4), "kind": (4, 5), "name": (5, 47), "addr": (47, 89),
       "city": (89, 117), "state": (117, 119), "zip": (119, 124)}

# ---------------------------------------------------------------- name filter
# docs/hoa-pipeline.md §6 step 1, "Revised name regex".  Keep the two lists in
# sync with the doc: the precision/recall numbers there were measured on them.

ASSOC_NAME = re.compile(
    r"\bowner'?s?'?\s+assoc|\bowners?'?\s+(of\s+)?assoc|\bhome\s?owner"
    r"|\bcondo(minium)?s?\b|\bco-?op(erative)?\b.*\bassoc|\bcommunity\s+assoc"
    r"|\bneighborhood\s+assoc|\bmaster\s+assoc|\b(HOA|POA|COA)\b"
    r"|\bvillas?\b.*\bassoc|\btownho(me|use)s?\b|residents'?\s+assoc"
    r"|\bmaintenance\s+assoc|\bproperty\s+owners\b",
    re.I,
)
# Not club / church / medical / union / services: they knock out real condos.
NOT_ASSOC_NAME = re.compile(
    r"\bmanagement\b|\brealty\b|\bL\.?L\.?C\b|\bBAR\b|\balumni\b|\bchamber\b"
    r"|\bmerchants\b|\bleague\b|\bfoundation\b|\bpolitical\b|\bteachers\b"
    r"|\bnurses\b|\bphysicians\b|\bcontractors\b|\breform\b"
    r"|\bapartment\s+association\b|\bassociation\s+of\s+florida\b"
    r"|\bflorida\s+association\b|\bnational\s+association\b"
    r"|\bLIEN\s+HOA\b",
    re.I,
)


def is_association_name(name: Optional[str]) -> bool:
    """The §6 step 1 filter: association keyword and no exclusion word."""
    if not name:
        return False
    return bool(ASSOC_NAME.search(name)) and not NOT_ASSOC_NAME.search(name)


# ---------------------------------------------------------------- parsing

@dataclass
class SunbizRecord:
    fields: dict[str, str]
    officers: list[dict[str, str]]
    source_file: str


def _cut(line: str, span: tuple[int, int]) -> str:
    return line[span[0]:span[1]].strip()


def _zip5(raw: str) -> Optional[str]:
    m = re.match(r"\s*(\d{5})", raw or "")
    return m.group(1) if m else None


def _iso_date(mmddyyyy: str) -> Optional[str]:
    s = (mmddyyyy or "").strip()
    if not re.fullmatch(r"\d{8}", s) or s == "00000000":
        return None
    return f"{s[4:]}-{s[:2]}-{s[2:4]}"


def parse_line(line: str, source_file: str = "") -> Optional[SunbizRecord]:
    """Parse one fixed-width record; None for short or blank lines."""
    line = line.rstrip("\r\n\x00")
    if len(line) < OFFICER_START or not line[:12].strip():
        return None
    line = line.ljust(RECORD_LEN)
    fields = {k: _cut(line, span) for k, span in F.items()}
    officers = []
    for i in range(OFFICER_SLOTS):
        b = OFFICER_START + OFFICER_LEN * i
        block = line[b:b + OFFICER_LEN]
        o = {k: _cut(block, span) for k, span in OFF.items()}
        if o["name"]:
            o["zip"] = _zip5(o["zip"])
            officers.append(o)
    return SunbizRecord(fields, officers, source_file)


def iter_records(paths: Iterable[str], filing_types: frozenset[str] = frozenset({"DOMNP"})
                 ) -> Iterator[SunbizRecord]:
    """Yield parsed records of the wanted filing types from quarterly or daily files."""
    for path in paths:
        name = path.rsplit("/", 1)[-1]
        with open(path, "rb") as fh:
            for raw in fh:
                line = raw.decode("latin-1")
                if line[205:220].strip() not in filing_types:
                    continue
                rec = parse_line(line, name)
                if rec is not None:
                    yield rec


# Event codes that end or restore a corporation (COREVENTREADME.txt; counts from
# the 2026-11-05 file).  Name changes carry the new name in COR_EVENT_COR_NAME.
DISSOLVE_EVENTS = frozenset({"CORAPVOLDS", "CORAPVDWN", "CORAPWITH", "CORAPMER"})
REINSTATE_EVENTS = frozenset({"CORAPREIN"})
NAME_EVENTS = frozenset({"CORAPNC", "CORAPAMDNC"})


def iter_events(paths: Iterable[str]) -> Iterator[dict[str, str]]:
    """Yield {doc, code, filed, name} from 662-char daily event files, oldest first."""
    for path in sorted(paths):
        with open(path, "rb") as fh:
            for raw in fh:
                line = raw.decode("latin-1").rstrip("\r\n\x00").ljust(662)
                doc = line[0:12].strip()
                if not doc:
                    continue
                yield {
                    "doc": doc,
                    "code": line[17:37].strip(),
                    "filed": _iso_date(line[85:93]),
                    "name": line[210:402].strip(),
                }


# ---------------------------------------------------------------- shaping

def load(quarterly: list[str], daily: list[str] = (), events: list[str] = ()
         ) -> dict[str, SunbizRecord]:
    """Quarterly records overlaid by daily new filings, then events applied."""
    recs: dict[str, SunbizRecord] = {}
    for rec in iter_records(quarterly):
        recs[rec.fields["doc"]] = rec
    for rec in iter_records(sorted(daily)):
        # Dailies are mostly new filings; a later record replaces an older one.
        recs[rec.fields["doc"]] = rec
    for ev in iter_events(events):
        rec = recs.get(ev["doc"])
        if rec is None:
            continue  # Not an active DOMNP we hold (a reinstated doc needs the next quarterly).
        if ev["code"] in DISSOLVE_EVENTS:
            rec.fields["status"] = "I"
            rec.fields["status_event"] = ev["code"]
            rec.fields["status_date"] = ev["filed"]
        elif ev["code"] in REINSTATE_EVENTS:
            rec.fields["status"] = "A"
            rec.fields["status_event"] = ev["code"]
            rec.fields["status_date"] = ev["filed"]
        if ev["code"] in NAME_EVENTS and ev["name"]:
            rec.fields["name"] = ev["name"]
    return recs


def _fei(raw: str) -> Optional[str]:
    digits = re.sub(r"\D", "", raw or "")
    return digits if len(digits) == 9 and digits != "000000000" else None


def to_frame(recs: dict[str, SunbizRecord]) -> tuple[pd.DataFrame, dict[str, dict]]:
    """Canonical rows plus attrs keyed by document number."""
    rows, attrs = [], {}
    for doc, rec in recs.items():
        f = rec.fields
        name_hit = is_association_name(f["name"])
        p_zip, m_zip = _zip5(f["p_zip"]), _zip5(f["m_zip"])
        rows.append({
            "natural_key": doc,
            "name_raw": f["name"],
            "name_normalized": normalize_name(f["name"]),
            "address_line_1": clean(f["p_addr1"]),
            "city": clean(f["p_city"]),
            "state": clean(f["p_state"]),
            "zip5": p_zip or m_zip,
            "segment": "association_name" if name_hit else "nonprofit_other",
            "ein": _fei(f["fei"]),
            "source_file": rec.source_file,
        })
        attrs[doc] = drop_empty({
            "filing_type": f["filing_type"],
            "status": f["status"],
            "status_event": f.get("status_event"),
            "status_date": f.get("status_date"),
            "file_date": _iso_date(f["file_date"]),
            "last_trx_date": _iso_date(f["last_trx_date"]),
            "name_hit": name_hit,
            "principal": drop_empty({"addr1": clean(f["p_addr1"]), "addr2": clean(f["p_addr2"]),
                                     "city": clean(f["p_city"]), "state": clean(f["p_state"]),
                                     "zip": p_zip}),
            "mailing": drop_empty({"addr1": clean(f["m_addr1"]), "addr2": clean(f["m_addr2"]),
                                   "city": clean(f["m_city"]), "state": clean(f["m_state"]),
                                   "zip": m_zip}),
            "care_of": parse_care_of(f["p_addr1"], f["p_addr2"], f["m_addr1"], f["m_addr2"]),
            "registered_agent": drop_empty({
                "name": clean(f["ra_name"]), "kind": clean(f["ra_kind"]),
                "addr": clean(f["ra_addr"]), "city": clean(f["ra_city"]),
                "state": clean(f["ra_state"]), "zip": _zip5(f["ra_zip"]),
            }),
            "officers": [drop_empty(o) for o in rec.officers],
            "more_than_six_officers": f["more_than_six"] == "Y" or None,
            "report_years": sorted({y for y in (f["report_year_1"], f["report_year_2"],
                                                f["report_year_3"]) if re.fullmatch(r"\d{4}", y)},
                                   reverse=True),
        })
    df = pd.DataFrame(rows)
    return df, attrs


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
        ein=df["ein"],
        source_file=df["source_file"],
    )


def assert_source_shape(df: pd.DataFrame, attrs: dict[str, dict]) -> None:
    """Fail loudly if the Division shifts the layout under us."""
    if df.empty:
        raise ValueError("no DOMNP rows parsed — wrong files or layout changed")
    bad_doc = (~df["natural_key"].str.fullmatch(r"[A-Z0-9]{1,12}")).mean()
    if bad_doc > 0.001:
        raise ValueError(f"{bad_doc:.1%} of document numbers malformed — layout changed")
    zip_fill = df["zip5"].notna().mean()
    if len(df) > 1000 and zip_fill < 0.95:
        raise ValueError(f"principal/mailing ZIP only {zip_fill:.1%} filled — layout changed")
    ra_fill = sum(1 for a in attrs.values() if a.get("registered_agent", {}).get("name")) / len(attrs)
    if len(df) > 1000 and ra_fill < 0.9:
        raise ValueError(f"registered agent only {ra_fill:.1%} filled — layout changed")


# ---------------------------------------------------------------- entrypoint

def _expand(patterns: list[str]) -> list[str]:
    return sorted({p for pat in patterns for p in (glob.glob(pat) or [pat])})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="npcordata*.txt quarterly files")
    ap.add_argument("--daily", nargs="*", default=[], help="YYYYMMDDc.txt daily filings")
    ap.add_argument("--events", nargs="*", default=[], help="YYYYMMDDce.txt daily events")
    ap.add_argument("--out", default="fl_sunbiz_assoc.csv")
    ap.add_argument("--write-db", action="store_true",
                    help="Also write to Postgres staging (requires DATABASE_URL).")
    args = ap.parse_args()

    quarterly, daily, events = _expand(args.paths), _expand(args.daily), _expand(args.events)
    recs = load(quarterly, daily, events)
    df, attrs = to_frame(recs)
    assert_source_shape(df, attrs)
    canonical = to_canonical(df)
    canonical.to_csv(args.out, index=False)

    active = sum(1 for a in attrs.values() if a.get("status") == "A")
    print(f"  {len(df):,} DOMNP ({active:,} active) -> {args.out}", file=sys.stderr)
    for seg, n in df["segment"].value_counts().items():
        print(f"    {seg:<18} {n:>8,}", file=sys.stderr)

    if args.write_db:
        raw = b"".join(open(p, "rb").read() for p in quarterly + daily + events)
        write_staging(SOURCE_ID, canonical, attrs, raw)


if __name__ == "__main__":
    main()
