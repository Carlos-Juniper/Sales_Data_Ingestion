"""
Florida HOA merge — staging FL sources -> staging.resolved_* (vertical='hoa').

Implements the links in docs/hoa-pipeline.md §4 with the calibrated matchers in
hoa/fl_match.py.

Pipeline
--------
1. Load the four FL staging tables (attrs included):
     fl_sunbiz_assoc       F1   every active DOMNP (association_name / nonprofit_other)
     fl_dbpr_condo_coop    F2/F3 condo and co-op projects
     fl_dbpr_cam           F4/F5 CAB firms (F5 individuals are not used here)
     fl_common_area_parcel F7   DOR code-009 parcels
2. DBPR managing entity -> F1 (name + ZIP, county region).
3. Parcel owner -> F1.  Association-named owners take any auto match; other
   owner classes only a ZIP- or county-backed one (recall rule 1).
4. C/O lines (DBPR route, F1 principal/mailing, parcel owner address) and F1
   registered agents -> CAB firm.  A non-association-named F1 non-profit whose
   agent or C/O is a CAB firm is an association (recall rule 2).
5. Accounts: one per F1 association (key sunbiz_doc), one per DBPR managing
   entity that matched no F1 record (key dbpr_managing_entity), one per
   association-named parcel owner that matched neither (key name + owner ZIP),
   and one per CAB firm (key dbpr_cab_license).
6. Locations: each parcel and each condo/co-op project of a resolved
   association.  management_account_key is the association's newest firm
   (DBPR route, weekly > F1 annual report > tax-roll C/O), else the parcel's own,
   else a CAB firm serving as the association's registered agent.
7. Contacts: F1 officers and registered agent on the association account.
8. Review-band pairs -> review.pending_pairs.  They are NOT linked here: the
   entity stays on its own account until a reviewer approves the pair.
9. replace_and_upsert(vertical='hoa').

TRAPS
-----
* replace_and_upsert deletes every resolved_* row with vertical='hoa'.  Texas
  (tx_trec_hoa) has no resolved merge today; when it gets one, both states
  must be merged in one run or the scope narrowed, or one wipes the other.
* Officer and agent names are personal data.  They go to resolved_contact and
  nowhere else: the summary prints counts only.

Usage:
    python -m hoa.hoa_fl_merge [--dry-run] [--no-review-queue]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import pandas as pd

from hoa.fl_match import (
    AUTO_METHODS,
    FIRM_AUTO_METHODS,
    FIRM_REVIEW_METHODS,
    REVIEW_METHODS,
    AssociationIndex,
    FirmAddressIndex,
    FirmIndex,
    Match,
    care_of_kind,
    firm_canon,
    firm_key,
    parse_care_of,
)
from lib.keys import compute_account_key, compute_contact_key, compute_location_key, is_present
from lib.normalize import normalize_name

logger = logging.getLogger(__name__)

VERTICAL = "hoa"
SUNBIZ, DBPR, CAM, PARCEL = ("fl_sunbiz_assoc", "fl_dbpr_condo_coop", "fl_dbpr_cam",
                             "fl_common_area_parcel")
SOURCES = (SUNBIZ, DBPR, CAM, PARCEL)

STRATEGY_ASSOC = "hoa_fl_assoc"
STRATEGY_FIRM = "hoa_fl_firm"

# Parcel owners outside the association class need a ZIP or county
# agreement: "TEST CREEK" may be a unique F1 name and still not this parcel.
RECALL_METHODS = frozenset({"exact_zip", "exact_county", "prefix_zip", "prefix_county", "core_zip"})
SKIP_OWNER_CLASSES = frozenset({"cdd", "government", "utility"})
# Recall rule 2 (firm-managed F1 record = association) misfires on charities
# a CAM firm keeps the books for.  CLUB / MANAGEMENT / PROFESSIONAL stay in:
# condo clubs, master "management associations", office-park associations.
NOT_ASSOCIATION = re.compile(r"\b(MINISTR\w*|CHURCH|BOOSTERS?|ACADEMY|SCHOOL|LEAGUE|CHARIT\w*|"
                             r"OUTREACH|FAMILY FOUNDATION)\b")
# Reviewed C/O spellings that name a license by initials or another brand.
# Checked before the firm-kind gate: "KWPMC" reads as a person's name.
FIRM_ALIASES = {
    "KWPMC": "CAB3320",  # KW Property Management & Consulting
    "CASTLE GROUP": "CAB3838",  # The Castle Group = Castle Management LLC
}
SQFT_PER_ACRE = 43560.0

# Sub-association -> master.  A master names itself "<BASE> COMMUNITY
# ASSOCIATION" (or MASTER / UMBRELLA / [PROPERTY] OWNERS); a sub carries the
# base after AT / OF / IN, or leads with it before a PARCEL / VILLAGE / ...
# marker.  Both must share a ZIP3, which keeps "X AT WESTCHASE" from finding a
# Westchase on the other coast.
_MASTER = re.compile(r"^(?P<base>.+?) (MASTER )?(COMMUNITY|UMBRELLA|PROPERTY OWNERS|OWNERS|MASTER)"
                     r"( HOMEOWNERS| PROPERTY OWNERS)? (ASSOCIATION|ASSN|ASSOC)$")
_SUB_LEAD = r"(PARCEL|VILLAGE|NEIGHBORHOOD|SECTION|PHASE|UNIT|TRACT|POD|AREA|NO|NUMBER|\d+)\b"
_GENERIC_BASE = frozenset({"VILLAGE", "VILLAGES", "VILLAS", "COUNTRY", "ESTATES", "LAKES", "GARDENS",
                           "PALMS", "OAKS", "POINTE", "HARBOR", "ISLAND", "PLACE", "CLUB", "PARK"})


def _parent_clean(name: Optional[str]) -> str:
    s = (name or "").upper().replace("'", "").replace("@", " AT ").replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\b(INC|INCORPORATED|CORP|LLC)\b", " ", s)
    return " ".join(s.split())


def master_base(name: Optional[str]) -> Optional[str]:
    """"HUNTER'S GREEN COMMUNITY ASSOCIATION, INC." -> "HUNTERS GREEN"."""
    m = _MASTER.match(_parent_clean(name))
    if not m:
        return None
    base = re.sub(r"^THE ", "", m.group("base"))
    if " " not in base and (len(base) < 6 or base in _GENERIC_BASE):
        return None
    return base


def sub_association_parents(accounts: dict[str, tuple[str, set[str]]]
                            ) -> tuple[dict[str, str], Counter]:
    """{account_key: (name, zip5s)} -> {child_key: master_key}, outcome counts."""
    masters: dict[str, list[tuple[str, set[str]]]] = defaultdict(list)
    for k, (name, zips) in accounts.items():
        b = master_base(name)
        if b:
            masters[b].append((k, {z[:3] for z in zips}))
    parent: dict[str, str] = {}
    stats: Counter = Counter()
    for k, (name, zips) in accounts.items():
        n = _parent_clean(name)
        if master_base(name):
            continue
        toks = n.split()
        bases = {" ".join(toks[i:j]) for i in range(len(toks))
                 for j in range(i + 1, min(i + 6, len(toks)) + 1)} & masters.keys()
        bases = {b for b in bases
                 if re.search(rf"\b(AT|OF|IN) {re.escape(b)}\b", n)
                 or re.match(rf"{re.escape(b)} {_SUB_LEAD}", n)}
        if not bases:
            continue
        z3 = {z[:3] for z in zips}
        cands = {m for b in bases for m, mz in masters[b] if m != k and mz & z3}
        if len(cands) == 1:
            parent[k] = cands.pop()
            stats["linked"] += 1
        else:
            stats["no_zip_master" if not cands else "ambiguous"] += 1
    return parent, stats

ASSOC_PRIORITY = ("sunbiz_doc", "dbpr_managing_entity")
FIRM_PRIORITY = ("dbpr_cab_license",)

# How sure we are the account is an association, by what put it there.
CONFIDENCE = {"name": 1.0, "dbpr": 1.0, "parcel": 0.9, "firm_agent": 0.8,
              "dbpr_only": 0.9, "parcel_only": 0.6}

CONTACT_COLS = ["contact_key", "account_key", "vertical", "full_name", "role", "role_rank",
                "phone", "email", "address", "source_id", "is_current"]


# ---------------------------------------------------------------- load

def load_sources(engine) -> dict[str, pd.DataFrame]:
    """Read the four FL staging tables, attrs as dicts."""
    out = {}
    with engine.connect() as conn:
        for src in SOURCES:
            df = pd.read_sql(f"SELECT * FROM staging.{src}", conn)
            df["attrs"] = df["attrs"].map(lambda a: a if isinstance(a, dict)
                                          else json.loads(a) if isinstance(a, str) else {})
            out[src] = df
            logger.info("loaded staging.%s: %d rows", src, len(df))
    return out


# ---------------------------------------------------------------- helpers

def _zip5(v: Any) -> Optional[str]:
    s = str(v or "").strip()
    return s[:5] if len(s) >= 5 and s[:5].isdigit() else None


def _sunbiz_zips(a: dict) -> set[str]:
    zs = {(a.get(k) or {}).get("zip") for k in ("principal", "mailing", "registered_agent")}
    zs |= {o.get("zip") for o in a.get("officers") or ()}
    return {z for z in map(_zip5, zs) if z}


def _site(r) -> dict:
    return {f: str(getattr(r, f, None)) for f in ("address_line_1", "city", "state", "zip5")
            if is_present(getattr(r, f, None))}


def _jdump(d: Optional[dict]) -> Optional[str]:
    return json.dumps(d, sort_keys=True) if d else None


@dataclass
class Link:
    """One source entity -> resolved target, with how it was found."""
    target: Optional[str]
    method: str
    score: float = 1.0


@dataclass
class Resolution:
    account: pd.DataFrame
    location: pd.DataFrame
    contact: pd.DataFrame
    review_pairs: list[dict]
    stats: dict[str, Counter] = field(default_factory=dict)


def _firm_link(idx: FirmIndex, text: Optional[str], zip5: Optional[str]) -> Link:
    """C/O or agent text -> CAB license, firm-like text only.

    A one-word brand ("REALMANAGE", "FIRSTSERVICE RESIDENTIAL") has no firm
    word and reads as a person, so it counts as a firm only when it is a
    licensee name or DBA exactly.
    """
    if not text:
        return Link(None, "not_firm")
    kind, key = care_of_kind(text), firm_key(firm_canon(text))
    if not (kind == "firm" or key in idx.aliases or (kind == "person" and key in idx.by_key)):
        # An all-generic DBA reads as an association ("CONDOMINIUM
        # ASSOCIATES"); at its holder's own office ZIP it is that firm.
        lic = idx.full_at_zip(text, zip5 or "") if kind in ("association", "generic", "person") else None
        return Link(lic, "full_zip") if lic else Link(None, "not_firm")
    m = idx.lookup(text, zip5 or "")
    return Link(m.doc, m.method, m.score or 1.0)


def _address_firm(adx: FirmAddressIndex, route: Optional[str], street: Optional[str],
                  zip5: Optional[str]) -> Optional[str]:
    """The CAB firm whose office is this mailing address.

    Used only when the C/O names no other business: under "C/O <company>" the
    address is that company's, licensed or not.
    """
    if route and care_of_kind(route) not in ("person", "generic"):
        return None
    return adx.lookup(street, zip5)


def _mail_street(addr: Optional[dict]) -> Optional[str]:
    """Street line of an F1 address; addr1 is a C/O line when one is given."""
    addr = addr or {}
    a1 = addr.get("addr1")
    return addr.get("addr2") if parse_care_of(a1) else a1


def _assoc_link(m: Match) -> Link:
    return Link(m.doc, m.method, m.score or 1.0)


# ---------------------------------------------------------------- resolve

def resolve(sunbiz: pd.DataFrame, projects: pd.DataFrame, cam: pd.DataFrame,
            parcels: pd.DataFrame) -> Resolution:
    """Pure merge: four staging frames in, resolved frames + review pairs out."""
    stats: dict[str, Counter] = defaultdict(Counter)
    review: list[dict] = []

    # The quarterly file is active-only, but a daily event can dissolve a record.
    sb = {r.natural_key: r for r in sunbiz.itertuples(index=False)
          if (r.attrs.get("status") or "A") == "A"}
    aidx = AssociationIndex.build((d, r.name_raw, _sunbiz_zips(r.attrs)) for d, r in sb.items())
    firms = cam[cam["account_type"] == "management_company"]
    fidx = FirmIndex.build([(r.natural_key, r.name_raw, "; ".join(r.attrs.get("dba") or ()),
                             r.zip5 or "") for r in firms.itertuples(index=False)],
                           aliases={k: v for k, v in FIRM_ALIASES.items()
                                    if v in set(firms["natural_key"])})

    adx = FirmAddressIndex.build((r.natural_key, r.address_line_1, r.zip5)
                                 for r in firms.itertuples(index=False))

    def queue(strategy, src_a, key_a, src_b, key_b, link: Link):
        review.append({"source_a": src_a, "key_a": key_a, "source_b": src_b, "key_b": key_b,
                       "score": float(link.score), "merge_strategy": strategy,
                       "feature_breakdown": {"method": link.method}})

    # -- DBPR managing entities -> F1
    entities: dict[str, dict] = {}
    county_zips: dict[str, set[str]] = defaultdict(set)
    for r in projects.itertuples(index=False):
        me = r.attrs.get("managing_entity") or {}
        num = me.get("number") or f"project:{r.natural_key}"
        e = entities.setdefault(num, {"number": num, "me": me, "projects": [], "zips": set(),
                                      "county": r.attrs.get("county")})
        e["projects"].append(r)
        e["zips"] |= {z for z in (_zip5(me.get("zip")), _zip5(r.zip5)) if z}
        county_zips[r.attrs.get("county")] |= e["zips"]
    me_link: dict[str, Link] = {}
    for num, e in entities.items():
        m = aidx.lookup(e["me"].get("name") or "", e["zips"],
                        region=frozenset(county_zips[e["county"]]))
        link = _assoc_link(m)
        stats["dbpr_entity_to_f1"][m.method] += 1
        if m.doc and m.method in REVIEW_METHODS:
            queue(STRATEGY_ASSOC, DBPR, e["projects"][0].natural_key, SUNBIZ, m.doc, link)
        me_link[num] = link if m.auto else Link(None, m.method)

    # -- parcel owners -> F1 (grouped per owner name and county)
    owners: dict[tuple, dict] = {}
    pc_zips: dict[Any, set[str]] = defaultdict(set)
    for r in parcels.itertuples(index=False):
        a = r.attrs
        cls = a.get("owner_class") or r.segment
        key = (r.name_raw or "", a.get("county_no"))
        o = owners.setdefault(key, {"class": cls, "parcels": [], "zips": set(),
                                    "truncated": bool(a.get("owner_name_truncated")),
                                    "owner_zip": _zip5((a.get("owner") or {}).get("zip"))})
        o["parcels"].append(r)
        o["zips"] |= {z for z in (_zip5((a.get("owner") or {}).get("zip")), _zip5(r.zip5)) if z}
        pc_zips[a.get("county_no")] |= o["zips"]
    owner_link: dict[tuple, Link] = {}
    for key, o in owners.items():
        name, county = key
        cls = o["class"]
        # "LOT OWNERS OF ..." is the plat's placeholder for the lot owners in
        # common, not a corporation; it has no F1 record by construction.
        if cls in SKIP_OWNER_CLASSES or not name or name.startswith("LOT OWNERS"):
            reason = cls if cls in SKIP_OWNER_CLASSES or not name else "lot_owners"
            stats["parcel_owner_to_f1"][f"skip_{reason}"] += 1
            owner_link[key] = Link(None, "skipped")
            continue
        m = aidx.lookup(name, o["zips"], truncated=o["truncated"],
                        region=frozenset(pc_zips[county]))
        ok = m.auto and (cls == "association" or m.method in RECALL_METHODS)
        stats[f"parcel_owner_to_f1:{cls}"][m.method if (ok or not m.auto) else "recall_rejected"] += 1
        if m.doc and m.method in REVIEW_METHODS:
            queue(STRATEGY_ASSOC, PARCEL, o["parcels"][0].natural_key, SUNBIZ, m.doc,
                  _assoc_link(m))
        owner_link[key] = _assoc_link(m) if ok else Link(None, m.method)

    # -- F1 firm signals: agent and C/O -> CAB
    f1_firm: dict[str, Link] = {}
    f1_agent: dict[str, str] = {}  # doc -> CAB license serving as registered agent
    f1_agent_firm: set[str] = set()
    f1_addr: dict[str, str] = {}  # doc -> CAB license whose office is the F1 address
    for d, r in sb.items():
        a = r.attrs
        co = _firm_link(fidx, a.get("care_of"),
                        _zip5((a.get("mailing") or {}).get("zip")) or _zip5(r.zip5))
        ra = a.get("registered_agent") or {}
        agent = (_firm_link(fidx, ra.get("name"), _zip5(ra.get("zip")))
                 if ra.get("kind") == "C" else Link(None, "person_agent"))
        if r.segment == "association_name":
            stats["f1_care_of_to_cab"][co.method] += 1
            stats["f1_agent_to_cab"][agent.method] += 1
        for link, src_key in ((co, "care_of"), (agent, "agent")):
            if link.target and link.method in FIRM_REVIEW_METHODS:
                queue(STRATEGY_FIRM, SUNBIZ, d, CAM, link.target, link)
        if co.target and co.method not in FIRM_REVIEW_METHODS:
            f1_firm[d] = co
        if agent.target and agent.method not in FIRM_REVIEW_METHODS:
            f1_agent[d] = agent.target
        if not co.target:
            for x in ("mailing", "principal"):
                ad = a.get(x) or {}
                lic = _address_firm(adx, a.get("care_of"), _mail_street(ad), _zip5(ad.get("zip")))
                if lic:
                    f1_addr[d] = lic
                    break
        if any(lk.target and lk.method not in FIRM_REVIEW_METHODS for lk in (co, agent)):
            if r.segment != "association_name" and NOT_ASSOCIATION.search((r.name_raw or "").upper()):
                stats["f1_firm_agent_excluded"]["not_association_name"] += 1
            else:
                f1_agent_firm.add(d)

    # -- which F1 records are associations, and why
    why: dict[str, str] = {}
    for d, r in sb.items():
        if r.segment == "association_name":
            why[d] = "name"
    for lk in me_link.values():
        if lk.target:
            why.setdefault(lk.target, "dbpr")
    for lk in owner_link.values():
        if lk.target:
            why.setdefault(lk.target, "parcel")
    for d in f1_agent_firm:
        why.setdefault(d, "firm_agent")
    stats["f1_association_by"] = Counter(why.values())
    stats["f1_association_by"]["not_association"] = len(sb) - len(why)

    # -- accounts
    accounts: dict[str, dict] = {}
    assoc_firm: dict[str, Link] = {}
    agent_firm: dict[str, str] = {}  # the CAB firm is the registered agent
    addr_firm: dict[str, str] = {}  # last resort: the mailing address is a CAB firm's office
    acct_zips: dict[str, set[str]] = defaultdict(set)

    def add_account(row: dict) -> str:
        k = row["account_key"]
        if k not in accounts:
            accounts[k] = row
        return k

    def f1_account(d: str) -> str:
        r = sb[d]
        a = r.attrs
        k = compute_account_key({"sunbiz_doc": d}, priority=ASSOC_PRIORITY)
        if k not in accounts:
            add_account({
                "account_key": k, "account_type": "association",
                "legal_name": r.name_raw, "name_normalized": r.name_normalized,
                "mailing_address": a.get("mailing") or a.get("principal"),
                "status": "active", "_ext": {"sunbiz_doc": d}, "_units": {}, "_sqft": 0.0,
                "confidence": CONFIDENCE[why[d]],
            })
            if d in f1_firm:
                assoc_firm.setdefault(k, Link(f1_firm[d].target, "f1_care_of"))
            if d in f1_agent:
                agent_firm.setdefault(k, f1_agent[d])
            if d in f1_addr:
                addr_firm.setdefault(k, f1_addr[d])
            acct_zips[k] |= {z for z in (_zip5((a.get(x) or {}).get("zip"))
                                         for x in ("principal", "mailing")) if z}
        return k

    for d in why:
        f1_account(d)

    entity_account: dict[str, str] = {}
    for num, e in entities.items():
        lk = me_link[num]
        if lk.target:
            k = f1_account(lk.target)
        else:
            me = e["me"]
            # A few DBPR rows leave the entity name blank; the project names it.
            name = (me.get("name") or "").strip() or e["projects"][0].name_raw
            k = add_account({
                "account_key": compute_account_key({"dbpr_managing_entity": num},
                                                   priority=ASSOC_PRIORITY),
                "account_type": "association", "legal_name": name,
                "name_normalized": normalize_name(name or ""),
                "mailing_address": {x: me[x] for x in ("street", "city", "state", "zip") if me.get(x)},
                "status": "active", "_ext": {}, "_units": {}, "_sqft": 0.0,
                "confidence": CONFIDENCE["dbpr_only"],
            })
        acct = accounts[k]
        acct["_ext"].setdefault("dbpr_managing_entity", [])
        if num not in acct["_ext"]["dbpr_managing_entity"]:
            acct["_ext"]["dbpr_managing_entity"].append(num)
        for p in e["projects"]:
            acct["_units"][p.natural_key] = p.size_value
            acct_zips[k] |= {z for z in [_zip5(p.zip5)] if z}
        entity_account[num] = k
        route = (e["projects"][0].attrs or {}).get("care_of")
        fl = _firm_link(fidx, route, _zip5(e["me"].get("zip")))
        stats["dbpr_route_to_cab"][fl.method] += 1
        if fl.target and fl.method in FIRM_REVIEW_METHODS:
            queue(STRATEGY_FIRM, DBPR, e["projects"][0].natural_key, CAM, fl.target, fl)
        elif fl.target:
            assoc_firm[k] = Link(fl.target, "dbpr_route")  # newest source wins
        else:
            me = e["me"]
            lic = _address_firm(adx, route, me.get("street"), _zip5(me.get("zip")))
            stats["dbpr_address_to_cab"]["address" if lic else "none"] += 1
            if lic:
                addr_firm[k] = lic

    owner_account: dict[tuple, str] = {}
    for key, o in owners.items():
        lk = owner_link[key]
        if lk.target:
            owner_account[key] = f1_account(lk.target)
        elif o["class"] == "association" and key[0] and not key[0].startswith("LOT OWNERS"):
            nm = normalize_name(key[0])
            owner_account[key] = add_account({
                "account_key": compute_account_key({"name_normalized": nm, "zip5": o["owner_zip"]},
                                                   priority=()),
                "account_type": "association", "legal_name": key[0], "name_normalized": nm,
                "mailing_address": None, "status": "active", "_ext": {"parcel_owner": key[0]},
                "_units": {}, "_sqft": 0.0, "confidence": CONFIDENCE["parcel_only"],
            })

    firm_key = {r.natural_key: compute_account_key({"dbpr_cab_license": r.natural_key},
                                                   priority=FIRM_PRIORITY)
                for r in firms.itertuples(index=False)}
    for r in firms.itertuples(index=False):
        add_account({
            "account_key": firm_key[r.natural_key], "account_type": "management_company",
            "legal_name": r.name_raw, "name_normalized": r.name_normalized,
            "dba_name": "; ".join(r.attrs.get("dba") or ()) or None,
            "mailing_address": {k: v for k, v in (("address_line_1", r.address_line_1),
                                                  ("city", r.city), ("state", r.state),
                                                  ("zip5", r.zip5)) if is_present(v)},
            "status": "active" if (r.attrs.get("primary_status") or "").lower() == "current"
                      else "inactive",
            "_ext": {"dbpr_cab_license": r.natural_key}, "_units": {}, "_sqft": 0.0,
            "confidence": 1.0,
        })

    # -- locations
    locs: list[dict] = []
    for key, o in owners.items():
        k = owner_account.get(key)
        for p in o["parcels"]:
            a = p.attrs
            if k:
                accounts[k]["_sqft"] += float(p.size_value or 0)
                acct_zips[k] |= {z for z in (_zip5(p.zip5), _zip5((a.get("owner") or {}).get("zip")))
                                 if z}
            fl = _firm_link(fidx, a.get("care_of"), _zip5((a.get("owner") or {}).get("zip")))
            stats["parcel_care_of_to_cab"][fl.method] += 1
            if not k:
                continue
            if fl.target and fl.method in FIRM_REVIEW_METHODS:
                queue(STRATEGY_FIRM, PARCEL, p.natural_key, CAM, fl.target, fl)
            own_firm = fl.target if fl.target and fl.method not in FIRM_REVIEW_METHODS else None
            best = assoc_firm.get(k)
            agent = agent_firm.get(k)
            own_addr = None if fl.target else _address_firm(
                adx, a.get("care_of"), (a.get("owner") or {}).get("addr1"),
                _zip5((a.get("owner") or {}).get("zip")))
            addr = addr_firm.get(k) or own_addr
            lic = best.target if best else own_firm or agent or addr
            stats["location_firm_from"]["newer_source" if best else "parcel_care_of" if own_firm
                                        else "f1_agent" if agent else "address" if addr
                                        else "none"] += 1
            if best and own_firm and own_firm != best.target:
                stats["parcel_firm_vs_newer_source"]["disagree"] += 1
            elif best and own_firm:
                stats["parcel_firm_vs_newer_source"]["agree"] += 1
            sqft = float(p.size_value or 0)
            locs.append({
                "location_key": compute_location_key(
                    k, {"address_line_1": p.address_line_1, "zip5": p.zip5},
                    discriminator=f"{PARCEL}:{p.natural_key}"),
                "account_key": k, "location_name": p.name_raw,
                "site_address": _site(p),
                "_latitude": float(p.latitude) if is_present(p.latitude) else None,
                "_longitude": float(p.longitude) if is_present(p.longitude) else None,
                "_boundary_geojson": json.dumps(a["boundary"]) if a.get("boundary") else None,
                "geocode_precision": "parcel_centroid" if is_present(p.latitude) else None,
                "geometry_source": "fl_statewide_cadastral",
                "maintained_acres": round(sqft / SQFT_PER_ACRE, 3) if sqft else None,
                "acres_confidence": "parcel_land_area" if sqft else None,
                "site_type": "common_area_parcel",
                "management_account_key": firm_key.get(lic) if lic else None,
            })
    for num, e in entities.items():
        k = entity_account[num]
        lic = (assoc_firm.get(k) or Link(None, "")).target or agent_firm.get(k) or addr_firm.get(k)
        for p in e["projects"]:
            locs.append({
                "location_key": compute_location_key(
                    k, {"address_line_1": p.address_line_1, "zip5": p.zip5},
                    discriminator=f"{DBPR}:{p.natural_key}"),
                "account_key": k, "location_name": p.name_raw,
                "site_address": _site(p),
                "_latitude": None, "_longitude": None, "_boundary_geojson": None,
                "geocode_precision": None, "geometry_source": None,
                "maintained_acres": None, "acres_confidence": None,
                "site_type": f"{p.segment}_project",
                "management_account_key": firm_key.get(lic) if lic else None,
            })

    # -- contacts: F1 officers and agent, on the association account only
    contacts: dict[str, dict] = {}
    for d in why:
        k = compute_account_key({"sunbiz_doc": d}, priority=ASSOC_PRIORITY)
        a = sb[d].attrs
        people = [(o.get("title") or "officer", i + 1, o) for i, o in enumerate(a.get("officers") or ())]
        ra = a.get("registered_agent") or {}
        if ra.get("name"):
            people.append(("registered_agent", 99, ra))
        for role, rank, p in people:
            if not p.get("name"):
                continue
            ck = compute_contact_key(k, role, p["name"])
            contacts.setdefault(ck, {
                "contact_key": ck, "account_key": k, "vertical": VERTICAL,
                "full_name": p["name"], "role": role, "role_rank": rank, "phone": None,
                "email": None,
                "address": _jdump({f: p[f] for f in ("addr", "city", "state", "zip") if p.get(f)}),
                "source_id": SUNBIZ, "is_current": True,
            })

    # -- sub-association -> master
    parent, stats["sub_association_parent"] = sub_association_parents(
        {k: (a["legal_name"], acct_zips[k]) for k, a in accounts.items()
         if a["account_type"] == "association"})

    # -- finish account rows
    rows = []
    for k, acct in accounts.items():
        units = [u for u in acct["_units"].values() if is_present(u)]
        if units:
            size, unit = float(sum(units)), "units"
        elif acct["_sqft"]:
            size, unit = round(acct["_sqft"] / SQFT_PER_ACRE, 3), "acres"
        else:
            size, unit = None, None
        ext = dict(acct["_ext"])
        if acct["_units"]:
            ext["dbpr_project"] = sorted(acct["_units"])
        rows.append({
            "account_key": k, "vertical": VERTICAL, "account_type": acct["account_type"],
            "legal_name": acct["legal_name"], "name_normalized": acct["name_normalized"],
            "dba_name": acct.get("dba_name"), "parent_account_key": parent.get(k),
            "mailing_address": _jdump(acct.get("mailing_address")), "phone": None, "email": None,
            "website": None, "status": acct["status"], "external_keys": _jdump(ext),
            "size_metric": size, "size_metric_unit": unit, "confidence": acct["confidence"],
        })
    loc_df = pd.DataFrame(locs)
    if not loc_df.empty:
        loc_df["vertical"] = VERTICAL
        loc_df["site_address"] = loc_df["site_address"].map(_jdump)
    return Resolution(pd.DataFrame(rows), loc_df,
                      pd.DataFrame(list(contacts.values()), columns=CONTACT_COLS),
                      review, dict(stats))


# ---------------------------------------------------------------- report

def _rate(c: Counter, ok: Iterable[str]) -> str:
    ok = set(ok)
    n = sum(v for k, v in c.items() if not k.startswith("skip_") and k != "not_firm")
    hit = sum(v for k, v in c.items() if k in ok)
    return f"{hit:,}/{n:,} ({hit / n:.1%})" if n else "0/0"


def print_summary(res: Resolution) -> None:
    """Counts and match rates only — never names (officers are personal data)."""
    print("\n=== FL HOA merge ===", file=sys.stderr)
    if not res.account.empty:
        for t, n in res.account["account_type"].value_counts().items():
            print(f"  account {t:<20} {n:>8,}", file=sys.stderr)
    if not res.location.empty:
        for t, n in res.location["site_type"].value_counts().items():
            print(f"  location {t:<19} {n:>8,}", file=sys.stderr)
        mg = res.location["management_account_key"].notna().mean()
        print(f"  locations with a management firm: {mg:.1%}", file=sys.stderr)
    print(f"  contacts {len(res.contact):,}   review pairs {len(res.review_pairs):,}", file=sys.stderr)
    for name, c in sorted(res.stats.items()):
        ok = FIRM_AUTO_METHODS if "cab" in name else AUTO_METHODS
        rate = "" if name.startswith(("f1_association", "parcel_firm", "sub_association", "location_firm")) else f"  auto {_rate(c, ok)}"
        print(f"  {name}:{rate}  {dict(c.most_common())}", file=sys.stderr)


# ---------------------------------------------------------------- run

def run_pipeline(engine, dry_run: bool = False, review_queue: bool = True) -> dict:
    from lib.match_queue import enqueue_tier3_matches
    from lib.resolved_writer import replace_and_upsert

    src = load_sources(engine)
    res = resolve(src[SUNBIZ], src[DBPR], src[CAM], src[PARCEL])
    print_summary(res)
    if dry_run:
        logger.info("DRY RUN — staging.resolved_* and review queue not written")
        return {"account": len(res.account), "location": len(res.location),
                "contact": len(res.contact), "review": len(res.review_pairs)}
    n = replace_and_upsert(engine, res.account, res.location, res.contact, vertical=VERTICAL)
    queued = 0
    if review_queue:
        for strategy in (STRATEGY_ASSOC, STRATEGY_FIRM):
            pairs = [p for p in res.review_pairs if p["merge_strategy"] == strategy]
            if pairs:
                queued += enqueue_tier3_matches(engine, pairs, merge_strategy=strategy)
    return {"account": n[0], "location": n[1], "contact": n[2], "review": queued}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="Resolve and report, but write nothing.")
    ap.add_argument("--no-review-queue", action="store_true",
                    help="Do not write review-band pairs to review.pending_pairs.")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from lib.db import get_engine
    from lib.http import get_secret

    if not get_secret("DATABASE_URL"):
        sys.exit("ERROR: DATABASE_URL is not set. Copy .env.example -> .env and fill it in.")
    print(run_pipeline(get_engine(), dry_run=args.dry_run,
                       review_queue=not args.no_review_queue), file=sys.stderr)


if __name__ == "__main__":
    main()
