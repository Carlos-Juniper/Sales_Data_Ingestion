"""
Florida HOA name matching — association names and management-firm names.

Pure functions plus two small in-memory indexes; no database access.  Used by
hoa_fl_merge.py for every §4 link in docs/hoa-pipeline.md:

    F2/F3 managing entity  -> F1 association   (AssociationIndex)
    F7 code-009 owner name -> F1 association   (AssociationIndex, truncated=True)
    F7 / F2 "C/O <firm>"   -> F4 CAM firm      (FirmIndex)
    F1 registered agent    -> F4 CAM firm      (FirmIndex)

WHY A SEPARATE NORMALIZER
-------------------------
lib.normalize.normalize_name drops ASSOCIATION/ASSN/ASSOC outright, which is
right for healthcare and wrong here: "HUNTER'S GREEN COMMUNITY ASSOCIATION"
and "HUNTER'S GREEN" are different legal entities in Florida (the second is
usually a sub-association or a CDD).  assoc_canon() keeps the type words but
spells every variant one way, so ASSOC / ASS'N / ASSOCIATION / HOA all line
up, and assoc_core() strips them for a second, looser key.

TRUNCATION
----------
The statewide cadastral layer cuts OWN_NAME at 30 characters
("ISLAND PARK PROPERTY OWNERS AS"); callers pass truncated=True for names of
29+ characters (a trailing space is often trimmed).  The last token is
treated as a possible fragment: it is completed when it is the prefix of a
known type word, otherwise dropped, and the remainder is matched as a prefix.

Every match returns the method that produced it, so the merge can report
match rates per method and route the weak ones to review.match_queue.
"""

from __future__ import annotations

import bisect
import re
from functools import lru_cache
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional

from rapidfuzz import fuzz

# ---------------------------------------------------------------- association names

# Multi-word phrases, applied before tokenizing.  Order matters.
_PHRASES: tuple[tuple[str, str], ...] = (
    (r"\bHOME\s+OWNERS?\b", "HOMEOWNERS"),
    (r"\bH\s*/\s*O\b", "HOMEOWNERS"),
    (r"\bH\s*O\s*A\b", "HOMEOWNERS ASSN"),
    (r"\bP\s*O\s*A\b", "PROPERTY OWNERS ASSN"),
    (r"\bC\s*O\s*A\b", "CONDO ASSN"),
    (r"\bCO\s*-?\s*OP\b", "COOP"),
    (r"\bTOWN\s+HOMES?\b", "TOWNHOMES"),
    (r"\bTOWN\s+HOUSES?\b", "TOWNHOMES"),
)

# Single-token spellings -> one canonical spelling.
_TOKEN_MAP: dict[str, str] = {
    "ASSOCIATION": "ASSN", "ASSOCIATIONS": "ASSN", "ASSOC": "ASSN", "ASSN": "ASSN",
    "ASSO": "ASSN", "ASOC": "ASSN", "ASSNS": "ASSN",
    "CONDOMINIUM": "CONDO", "CONDOMINIUMS": "CONDO", "CONDOS": "CONDO",
    "CONDOMINIMUM": "CONDO", "CONDOMINUM": "CONDO", "CONDOMIUM": "CONDO",
    "HOMEOWNER": "HOMEOWNERS", "HOMEOWNERS": "HOMEOWNERS", "HOMEOWNRS": "HOMEOWNERS",
    "HOMEOWN": "HOMEOWNERS",
    "OWNER": "OWNERS", "OWNRS": "OWNERS",
    "PROP": "PROPERTY", "PROPERTIES": "PROPERTY",
    "COOPERATIVE": "COOP",
    "COMMUNITY": "COMM", "CMNTY": "COMM", "COMMUNITIES": "COMM",
    "TOWNHOMES": "TOWNHOMES", "TOWNHOME": "TOWNHOMES", "TOWNHOUSE": "TOWNHOMES",
    "TOWNHOUSES": "TOWNHOMES", "TWNHMS": "TOWNHOMES", "TH": "TOWNHOMES",
    "APARTMENTS": "APTS", "APARTMENT": "APTS", "APT": "APTS",
    "VILLAGE": "VLG", "VILLAGES": "VLG", "VILL": "VLG",
    "VILLAS": "VILLA", "ESTATE": "ESTATES", "ESTS": "ESTATES",
    "NEIGHBORHOOD": "NBHD", "NEIGHBOURHOOD": "NBHD",
    "MAINTENANCE": "MAINT", "MAINTENENCE": "MAINT",
    "SECTION": "SEC", "PHASE": "PH", "UNIT": "UNIT",
    "SAINT": "ST", "MOUNT": "MT", "FORT": "FT",
    "AVENUE": "AVE", "AV": "AVE", "STREET": "ST", "DRIVE": "DR", "ROAD": "RD",
    "BOULEVARD": "BLVD", "BUILDING": "BLDG", "BLD": "BLDG",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "CLUB": "CLUB", "RECREATION": "REC", "RECREATIONAL": "REC",
    "CENTER": "CTR", "CENTRE": "CTR",
    "HEIGHTS": "HTS", "LAKES": "LAKE", "POINTE": "POINT", "PT": "POINT",
    "ISLES": "ISLE", "ISLANDS": "ISLAND", "GARDENS": "GARDEN", "PARKS": "PARK",
    "ONE": "1", "TWO": "2", "THREE": "3", "FOUR": "4", "FIVE": "5",
    "SIX": "6", "SEVEN": "7", "EIGHT": "8", "NINE": "9", "TEN": "10",
    "FIRST": "1", "SECOND": "2", "THIRD": "3", "FOURTH": "4", "FIFTH": "5",
    "II": "2", "III": "3", "IV": "4", "VI": "6", "VII": "7", "VIII": "8", "IX": "9",
}

# Dropped entirely: legal-form suffixes and filler words.  "NO" goes because
# DBPR writes "CONDOMINIUM NO. 2" where Sunbiz writes "CONDOMINIUM 2".
_DROP: frozenset[str] = frozenset({
    "INC", "INCORPORATED", "CORP", "CORPORATION", "LLC", "NFP", "NP", "NONPROFIT",
    "THE", "OF", "A", "AN", "AT", "AND", "ON", "IN", "FOR", "NO", "NUMBER", "NBR",
    "FL", "FLA", "FLORIDA", "ET", "AL",
})

# Type words: present in the canonical name, removed from the core key.
TYPE_WORDS: frozenset[str] = frozenset({
    "ASSN", "HOMEOWNERS", "OWNERS", "PROPERTY", "CONDO", "COOP", "COMM",
    "MASTER", "NBHD", "MAINT", "RESIDENTS", "REC", "CIVIC", "COUNCIL",
    "UMBRELLA", "SERVICES", "CLUB",
})

# Full spellings a truncated last token can be the start of.  Mapped through
# _TOKEN_MAP after completion.
_TRUNC_COMPLETIONS: tuple[str, ...] = (
    "ASSOCIATION", "CONDOMINIUM", "HOMEOWNERS", "OWNERS", "PROPERTY",
    "COMMUNITY", "COOPERATIVE", "TOWNHOMES", "NEIGHBORHOOD", "MAINTENANCE",
    "INCORPORATED", "MASTER", "RESIDENTS", "VILLAGE", "APARTMENTS",
)

_NON_ALNUM = re.compile(r"[^A-Z0-9 ]")
_SPACES = re.compile(r"\s+")


# Text after the name that DBPR and the appraiser append: "... C/O STERLING
# MGMT", "... SUITE 300".
_NAME_TAIL = re.compile(r"\s+(C\s*/\s*O\b|SUITE\b|STE\b|ATTN\b).*$")
# Words glued together in the source: "BREEZEHOMEOWNERS".
_GLUED = re.compile(r"(?<=[A-Z]{3})(HOMEOWNERS|CONDOMINIUM|ASSOCIATION)\b")


def _prep(name: Optional[str]) -> str:
    if not name:
        return ""
    s = _NAME_TAIL.sub("", str(name).upper())
    s = _GLUED.sub(r" \1", s)
    s = s.replace("&", " AND ").replace("'", "").replace("’", "").replace("`", "")
    s = s.replace(".", "").replace("#", " ")
    s = _NON_ALNUM.sub(" ", s)
    for pat, rep in _PHRASES:
        s = re.sub(pat, rep, s)
    return _SPACES.sub(" ", s).strip()


def _map_tokens(tokens: Iterable[str]) -> list[str]:
    out: list[str] = []
    for t in tokens:
        t = _TOKEN_MAP.get(t, t)
        for part in t.split():
            if part not in _DROP:
                out.append(part)
    return out


def complete_fragment(fragment: str) -> Optional[str]:
    """Return the canonical type word *fragment* is the start of, or None.

    One-letter fragments are ambiguous and return None.  ``A`` is the start of
    ASSOCIATION but also a word; ``H`` could be HOMEOWNERS or a name.
    """
    if len(fragment) < 2:
        return None
    hits = {_TOKEN_MAP.get(w, w) for w in _TRUNC_COMPLETIONS if w.startswith(fragment)}
    return hits.pop() if len(hits) == 1 else None


def assoc_canon(name: Optional[str], *, truncated: bool = False) -> str:
    """Canonical association name: type words kept, spelled one way.

    With truncated=True the last raw token is a possible fragment: it is
    completed when it starts a known type word and dropped otherwise.
    """
    tokens = _prep(name).split()
    if truncated and tokens:
        last = tokens[-1]
        if last not in _TOKEN_MAP and last not in _DROP:
            done = complete_fragment(last)
            tokens = tokens[:-1] + ([done] if done else [])
    return " ".join(_map_tokens(tokens))


def fragment_dropped(name: Optional[str]) -> bool:
    """True when assoc_canon(truncated=True) threw away a cut-off last word.

    The remaining name is then known to be *shorter* than the real one, so it
    may only match as a strict prefix, never exactly: "LUXURY CONDOMINIUMS AT
    FISHHAW" is not "LUXURY CONDOMINIUMS, INC.".
    """
    tokens = _prep(name).split()
    if not tokens:
        return False
    last = tokens[-1]
    if last in _TOKEN_MAP or last in _DROP or complete_fragment(last) is not None:
        return False
    # "... ASSOCIATION I" is the start of INC: nothing of the name was lost.
    return not any(w.startswith(last) for w in _HARMLESS_TAIL)


# Filler words whose cut-off start loses nothing.  NO/NUMBER are left out:
# "RAINTREE VILLAGE CONDOMINIUM N" lost a unit number.
_HARMLESS_TAIL = ("INC", "INCORPORATED", "OF", "THE", "AND", "IN")


def assoc_core(canon: str) -> str:
    """The distinguishing part of a canonical name: type words removed."""
    return " ".join(t for t in canon.split() if t not in TYPE_WORDS)


_ARTICLE_A = re.compile(r",\s*A\s+(?=CONDO|COOP|CO-OP)")


def designators(name: Optional[str], *, truncated: bool = False) -> frozenset[str]:
    """Numbers and lone letters in a raw name: building, phase, section.

    "MARINA CLUB BLDG L" and "MARINA CLUB BLDG N" differ only here, so every
    match looser than an exact name must agree on them.  Works on the raw
    name because the canonical form drops "A", which is an article in
    "ARAWANA, A CONDOMINIUM" and a building in "CAMBRIDGE A CONDOMINIUM".
    """
    raw = _prep(_ARTICLE_A.sub(" ", str(name or "").upper())).split()
    if truncated and raw:
        raw = raw[:-1]  # the cut-off last token is a fragment, not a designator
    out = set()
    for t in _map_tokens(raw) + (["A"] if "A" in raw else []):
        if t.isdigit() or (len(t) == 1 and t not in "NSEW") or (
                any(c.isdigit() for c in t) and len(t) <= 4):
            out.add(t)
    return frozenset(out)


def types_compatible(a: str, b: str) -> bool:
    """False when one name says condo and the other HOA (or co-op)."""
    sa, sb = type_signature(a) - {"MASTER"}, type_signature(b) - {"MASTER"}
    return not sa or not sb or bool(sa & sb)


def type_signature(canon: str) -> frozenset[str]:
    """The kind of association the name says it is, for tie-breaking."""
    toks = set(canon.split())
    sig = set()
    if "CONDO" in toks:
        sig.add("CONDO")
    if "COOP" in toks:
        sig.add("COOP")
    if "HOMEOWNERS" in toks or ("PROPERTY" in toks and "OWNERS" in toks):
        sig.add("HOA")
    if "MASTER" in toks:
        sig.add("MASTER")
    return frozenset(sig)


# ---------------------------------------------------------------- association index

@dataclass(frozen=True)
class AssocRecord:
    """One F1 association as the index sees it."""
    doc: str
    name: str
    zips: frozenset[str]
    canon: str = ""
    core: str = ""
    desig: frozenset[str] = frozenset()


@dataclass
class Match:
    """Result of one lookup.  doc is None when nothing matched."""
    doc: Optional[str]
    # exact_zip | exact_county | exact | prefix_zip | prefix_county | prefix | core_zip -> auto
    # core_zip_type | fuzzy_zip | exact_review | prefix_review -> review queue
    # ambiguous | name_only_rejected | none                  -> unmatched
    method: str
    score: float = 1.0
    candidates: tuple[str, ...] = ()

    @property
    def auto(self) -> bool:
        """True when the match is safe to write without review."""
        return self.doc is not None and (self.method in AUTO_METHODS
                                         or self.method in FIRM_AUTO_METHODS)


AUTO_METHODS = frozenset({"exact_zip", "exact_county", "exact", "prefix_zip",
                          "prefix_county", "prefix", "core_zip"})
REVIEW_METHODS = frozenset({"core_zip_type", "fuzzy_zip", "exact_review", "prefix_review"})

# Ratio of the core names at or above which a same-ZIP fuzzy candidate is
# queued for review.  Calibrated on the Hillsborough sample in
# docs/hoa-pipeline.md §6 step 9.
FUZZY_REVIEW = 88.0


@dataclass
class AssociationIndex:
    """In-memory index of F1 associations for name (+ ZIP) lookup."""
    records: dict[str, AssocRecord] = field(default_factory=dict)
    by_canon: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    by_core: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    by_zip: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    _sorted: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def build(cls, rows: Iterable[tuple[str, str, Iterable[str]]]) -> "AssociationIndex":
        """rows: (doc_number, legal_name, zips) for each F1 record."""
        idx = cls()
        for doc, name, zips in rows:
            canon = assoc_canon(name)
            if not canon:
                continue
            z = frozenset(x[:5] for x in zips if x and len(x) >= 5)
            rec = AssocRecord(doc, name, z, canon, assoc_core(canon), designators(name))
            idx.records[doc] = rec
            idx.by_canon[canon].append(doc)
            if rec.core:
                idx.by_core[rec.core].append(doc)
            for zz in z:
                idx.by_zip[zz].append(doc)
        idx._sorted = sorted((r.canon, r.doc) for r in idx.records.values())
        return idx

    # -- helpers

    def _zip_filter(self, docs: Iterable[str], zips: frozenset[str]) -> list[str]:
        return [d for d in docs if self.records[d].zips & zips]

    def _desig_filter(self, docs: Iterable[str], qd: frozenset[str],
                      truncated: bool) -> list[str]:
        # A truncated name may have lost its trailing designator, never gained one.
        if truncated:
            return [d for d in docs if qd <= self.records[d].desig]
        return [d for d in docs if self.records[d].desig == qd]

    def _prefix(self, canon: str) -> list[str]:
        i = bisect.bisect_left(self._sorted, (canon, ""))
        out = []
        while i < len(self._sorted) and self._sorted[i][0].startswith(canon):
            c = self._sorted[i][0]
            # Prefix must end on a token boundary: "OAK" must not match "OAKS".
            if len(c) == len(canon) or c[len(canon)] == " ":
                out.append(self._sorted[i][1])
            i += 1
        return out

    def _pick(self, docs: list[str], zips: frozenset[str], canon: str,
              zip_method: str, plain_method: str) -> Optional[Match]:
        if not docs:
            return None
        zd = self._zip_filter(docs, zips) if zips else []
        if len(zd) == 1:
            return Match(zd[0], zip_method)
        pool = zd or docs
        if len(pool) > 1:
            # Prefer the candidate of the same kind (condo vs HOA vs master).
            sig = type_signature(canon)
            same = [d for d in pool if type_signature(self.records[d].canon) == sig]
            if len(same) == 1:
                return Match(same[0], zip_method if zd else plain_method, 0.95)
            return Match(None, "ambiguous", 0.0, tuple(sorted(pool)[:10]))
        return Match(pool[0], plain_method)

    def _zip_block(self, zips: frozenset[str]) -> list[str]:
        return list({d for zz in zips for d in self.by_zip.get(zz, [])})

    # -- public

    def lookup(self, name: str, zips: Iterable[str] = (), *, truncated: bool = False,
               allow_name_only: bool = True, region: frozenset[str] = frozenset()) -> Match:
        """Find the F1 association for *name*, using *zips* to break ties.

        Cascade (first hit wins):
          1. exact canonical name                       exact_zip / exact
          2. prefix of a canonical name (truncated)     prefix_zip / prefix
          3. same core name and a shared ZIP            core_zip (core_zip_type
             when one side says condo and the other HOA: review, not auto)
          4. core names within FUZZY_REVIEW in a shared ZIP   fuzzy_zip (review)
        Steps 2-4 also require the building/phase designators to agree.
        allow_name_only=False demands a ZIP agreement for every auto match.
        region is the set of ZIPs in the county the name came from; a
        name-only match whose F1 address is in it is auto (exact_county).
        """
        z = frozenset(x[:5] for x in zips if x and len(x) >= 5)
        canon = assoc_canon(name, truncated=truncated)
        if not canon:
            return Match(None, "none", 0.0)
        qd = designators(name, truncated=truncated)

        exact = self.by_canon.get(canon, [])
        # The canonical form drops "A", so keep the designator-equal subset when there is one.
        exact = [d for d in exact if self.records[d].desig == qd] or exact
        core = assoc_core(canon)
        cut = truncated and fragment_dropped(name)
        if cut:
            # The last word may be whole after all ("... ASSN OF TAMPA"): try the
            # untruncated reading exactly, and the shortened one only as a prefix.
            exact = self.by_canon.get(assoc_canon(name), [])
        m = self._name_only_guard(self._pick(exact, z, canon, "exact_zip", "exact"), region)
        if m and (m.doc is None or allow_name_only or m.method == "exact_zip"):
            return m

        if truncated and len(canon) >= 8:
            pool = self._desig_filter(self._prefix(canon), qd, True)
            if cut:
                pool = [d for d in pool if len(self.records[d].canon) > len(canon)]
            m2 = self._name_only_guard(self._pick(pool, z, canon, "prefix_zip", "prefix"), region)
            if m2 and (m2.doc is None or allow_name_only or m2.method == "prefix_zip"):
                return m2

        if not core or len(core) < 4 or not z:
            return self._reject(m)
        block = self._desig_filter(self._zip_filter(self._zip_block(z), z), qd, truncated)

        pool = [d for d in block if self.records[d].core == core
                or (truncated and self.records[d].core.startswith(core + " "))]
        if pool:
            ok = [d for d in pool if types_compatible(canon, self.records[d].canon)]
            m3 = self._pick(ok or pool, z, canon, "core_zip", "core_zip")
            if m3 and m3.doc and not ok:
                m3.method = "core_zip_type"
            if m3:
                return m3

        best, best_doc = 0.0, None
        for d in block:
            rc = self.records[d].core
            if not rc:
                continue
            s = fuzz.ratio(core, rc[:len(core)] if truncated else rc)
            if s > best:
                best, best_doc = s, d
        if best_doc and best >= FUZZY_REVIEW:
            return Match(best_doc, "fuzzy_zip", best / 100.0)
        return self._reject(m)

    def _name_only_guard(self, m: Optional[Match], region: frozenset[str]) -> Optional[Match]:
        """Grade a match that has no ZIP agreement.

        Auto when the F1 record has an address in the source county
        (*_county), or when its core name is unique statewide and more than
        one word.  Otherwise review: "STONEY CREEK" or "PARKSIDE VILLAGE"
        recur in many counties and Sunbiz may hold only a distant one.
        """
        if m and m.doc and m.method in ("exact", "prefix"):
            rec = self.records[m.doc]
            if rec.zips & region:
                m.method += "_county"
            elif len(self.by_core.get(rec.core, ())) > 1 or len(rec.core.split()) < 2:
                m.method += "_review"
        return m

    @staticmethod
    def _reject(m: Optional[Match]) -> Match:
        if m:  # exact name existed but no ZIP agreement and name-only disallowed
            return Match(None, "name_only_rejected", 0.0, (m.doc,) if m.doc else m.candidates)
        return Match(None, "none", 0.0)


# ---------------------------------------------------------------- management firms

_FIRM_DROP: frozenset[str] = frozenset({
    "INC", "INCORPORATED", "LLC", "L", "C", "LC", "PLLC", "PA", "CORP", "CORPORATION",
    "CO", "COMPANY", "LTD", "LP", "LLP", "THE", "OF", "AND", "A", "GROUP", "GRP",
    "FL", "FLA", "FLORIDA", "USA", "US", "AMERICA", "SVCS", "SERVICES", "SERVICE",
    "AAMC", "AMS", "CMCA", "PCAM", "LCAM", "DBA", "ENTERPRISES", "ENTERPRISE",
    "OF", "ASSOCIATES", "ASSOC", "ASSN",
})
_FIRM_MAP: dict[str, str] = {
    "MANAGEMENT": "MGMT", "MANAGMENT": "MGMT", "MANAGEMNT": "MGMT", "MGT": "MGMT",
    "MNGT": "MGMT", "MGMNT": "MGMT", "MNGMT": "MGMT", "MGNT": "MGMT", "MANAGERS": "MGMT", "MANAGER": "MGMT",
    "MANAGE": "MGMT", "MGR": "MGMT",
    "PROPERTY": "PROP", "PROPERTIES": "PROP", "PROPERTYS": "PROP", "PROPERY": "PROP",
    "COMMUNITY": "COMM", "COMMUNITIES": "COMM", "CMNTY": "COMM",
    "ASSOCIATION": "", "REALTY": "REALTY", "REAL": "REAL",
    "CONSULTANTS": "CONSULT", "CONSULTING": "CONSULT",
    "INTERNATIONAL": "INTL", "NATIONAL": "NATL",
    "CONDOMINIUM": "CONDO", "CONDOMINIUMS": "CONDO", "REALITY": "REALTY",
    "ASSC": "", "ASSOCIATIONS": "",
    "SVS": "", "SVCS": "", "SRV": "", "SVC": "", "SW": "SOUTHWEST", "SE": "SOUTHEAST",
    "SWFL": "SOUTHWEST", "SFL": "SOUTH", "CFL": "CENTRAL", "NEFL": "NORTHEAST",
}
_FIRM_TAIL_WORDS: tuple[str, ...] = (
    "FLORIDA", "INCORPORATED", "MANAGEMENT", "PROPERTY", "PROPERTIES", "SERVICES",
    "COMPANY", "CORPORATION", "ASSOCIATION", "ASSOCIATES",
)
_CUT_SUFFIX = frozenset({"LL", "IN"})
# Misspellings of the generic words are common in C/O lines ("SEVICES",
# "PROPERT", "MANGEMENT"); they would otherwise count as distinctive.
_FIRM_SPELLCHECK: tuple[str, ...] = (
    "MANAGEMENT", "PROPERTY", "PROPERTIES", "SERVICES", "ASSOCIATION",
    "ASSOCIATES", "COMMUNITY", "CONDOMINIUM", "REALTY", "INCORPORATED",
)
# Generic words that are never enough on their own to identify a firm.
_FIRM_GENERIC: frozenset[str] = frozenset({
    "MGMT", "PROP", "COMM", "REALTY", "REAL", "ESTATE", "CONSULT", "PROFESSIONAL",
    "RESIDENTIAL", "HOA", "CONDO", "ASSOCIATION", "OFFICE",
    "SOLUTIONS", "PARTNERS", "INTL", "NATL", "BUSINESS",
})

_CO_PREFIX = re.compile(r"^\s*(C\s*/\s*O|C\s*O\s+|CARE\s+OF|ATTN:?|ATTENTION:?)\s*", re.I)
# Suffixes after the firm name in a C/O line: "C/O SENTRY MGMT INC - TAMPA", "C/O RIZZETTA & CO., LLC 9530 ..."
_CO_TAIL = re.compile(r"\s(\d{2,}\s.*|STE\b.*|SUITE\b.*|PO\s*BOX\b.*|P\s*O\s*BOX\b.*|-\s.*)$", re.I)


def parse_care_of(*fields: Optional[str]) -> Optional[str]:
    """Return the firm named after ``C/O`` in any of *fields*, or None.

    Accepts the F7 OWN_ADDR1/OWN_ADDR2 pair, the F2 Managing Entity Route, or
    an F1 address line.  Street numbers and suite text after the name are cut.
    """
    for f in fields:
        if not f:
            continue
        s = str(f).strip()
        m = _CO_PREFIX.match(s)
        if not m:
            continue
        rest = s[m.end():].strip()
        rest = _CO_TAIL.sub("", rest).strip(" ,-")
        if rest and not rest[0].isdigit():
            return rest
    return None


# Brands spelled as one word whose halves are generic on their own:
# "FIRST SERVICE" must not fall back to FIRST, "REAL MANAGE" to REAL MGMT.
_GLUED_BRANDS = re.compile(r"\b(FIRST) (SERVICE)\b|\b(REAL) (MANAGE)\b")


def firm_canon(name: Optional[str]) -> str:
    """Canonical management-firm name: legal suffixes dropped, MANAGEMENT -> MGMT."""
    out: list[str] = []
    for t in _join_initials(_GLUED_BRANDS.sub(lambda m: "".join(g for g in m.groups() if g), _prep(name)).split()):
        t = _FIRM_MAP.get(t, t)
        if t and t not in _FIRM_DROP and t not in _FIRM_GENERIC:
            w = _spellcheck(t)
            t = _FIRM_MAP.get(w, w)
        if t and t not in _FIRM_DROP:
            # Singular and plural spell one brand: GREENACRES / GREENACRE.
            if len(t) > 4 and t.endswith("S") and not t.endswith("SS"):
                t = t[:-1]
            out.append(t)
    # A C/O line cut mid-word: "... OF CENTRAL FLORID".
    if len(out) > 1 and len(out[-1]) >= 3 and any(
            w.startswith(out[-1]) and w != out[-1] for w in _FIRM_TAIL_WORDS):
        out.pop()
    # ... or cut inside the legal suffix: "... ASSOCIATION MGT LL".
    if len(out) > 1 and out[-1] in _CUT_SUFFIX:
        out.pop()
    return " ".join(out)


@lru_cache(maxsize=20000)
def _spellcheck(t: str) -> str:
    """Correct a misspelled generic word; leave anything else alone."""
    if len(t) < 5 or not t.isalpha():
        return t
    for w in _FIRM_SPELLCHECK:
        if abs(len(w) - len(t)) <= 2 and t[0] == w[0] and fuzz.ratio(t, w) >= 85:
            return w
    return t


def _join_initials(tokens: list[str]) -> list[str]:
    """Glue runs of single letters: "A R CHOICE" -> "AR CHOICE" (A.R. Choice)."""
    out: list[str] = []
    run = ""
    for t in tokens:
        if len(t) == 1 and t.isalpha():
            run += t
            continue
        if run:
            out.append(run)
            run = ""
        out.append(t)
    if run:
        out.append(run)
    return out


# Words that make a C/O line an association (a sub-association "C/O" its
# master) rather than a management firm.
_ASSOC_WORDS = re.compile(r"\b(HOMEOWNERS?|HOME OWNERS|OWNERS|CONDO(MINIUM)?S? ASS\w*|ASSOCIATION|ASSN|"
                          r"HOA|POA|COA|CDD|COMMUNITY DEVELOPMENT DISTRICT)\b")
# "SCHOO ASSOCIATION MANAGEMENT", "RIVER CONDO SERVICES": a firm that manages
# associations, not one.  Struck out before the association test.
_ASSOC_MGMT = re.compile(r"\b(ASSOCIATION|ASSN|ASSOC|CONDO(MINIUM)?|HOA)( AND PROPERTY)? "
                         r"(MANAGEMENT|MGMT|MGT|MANAGERS|SERVICES)\b")
_FIRM_WORDS = re.compile(r"\b(MGMT|MANAGEMENT|MANAGMENT|MANAGEMNT|MGT|MNGT|PROPERT\w*|LLC|L ?L ?C|INC|CORP|"
                         r"CO|COMPANY|GROUP|REALTY|SERVICES?|ADVISORS?|SOLUTIONS|PARTNERS|"
                         r"ENTERPRISES?|LIFESTYLES?|HOLDINGS|PA|LTD|REAL ESTATE|CAM|ASSOCIATES|"
                         r"CONSULT\w*|ACCOUNTING|ADMINISTRAT\w*)\b")


# Law firms and registered-agent services: the usual F1 registered agent,
# never the management firm.
_LEGAL_WORDS = re.compile(r"\b(LAW|LAWYERS?|LEGAL|ATTORNEYS?|ESQ|REGISTERED AGENTS?|"
                          r"CORPORATE AGENTS?|INCORPORATING|CORPORATE CREATIONS|STATUTORY AGENTS?|"
                          r"COGENCY GLOBAL|NRAI|N R A I|SKRLD|CT CORPORATION)\b")
# "BECKER AND POLIAKOFF PA": partners' surnames and a professional suffix.  A
# lone surname + PA is often a licensed manager, so the AND is required, and
# a firm word ("WHITEMAN AND ASSOCIATES PLLC") keeps it a firm.
_LAW_FIRM = re.compile(r"\bAND\b.*\b(PA|PL|PLLC|LLP|CHARTERED)$|\bLLP$")
_LAW_FIRM_NOT = re.compile(r"\b(MGMT|MANAGEMENT|MGT|PROPERT\w*|REALTY|ASSOCIATES|GROUP|"
                           r"SERVICES?|CAM|REAL ESTATE|CONSULT\w*|ACCOUNTING)\b")


def care_of_kind(name: Optional[str]) -> str:
    """Classify a C/O or agent name: firm | association | district | legal |
    person | generic.

    Only "firm" goes to FirmIndex.lookup; "association" goes to the
    association matcher (a sub-HOA mailing through its master); "district"
    is a CDD manager, which holds no CAM firm license; "legal" is a law firm
    or registered-agent service; "person" is an
    officer or owner and is never stored as a link; "generic" names no firm
    ("C/O MANAGEMENT").
    """
    if not name:
        return "generic"
    s = _prep(name)
    if re.search(r"\b(CDD|COMMUNITY DEVELOPMENT DISTRICT|DISTRICT MANAGEMENT|DISTRICT MGMT)\b", s):
        return "district"
    if _LEGAL_WORDS.search(s) or (_LAW_FIRM.search(s) and not _LAW_FIRM_NOT.search(s)):
        return "legal"
    if _ASSOC_WORDS.search(_ASSOC_MGMT.sub(" MGMT", s)):
        return "association"
    if not _FIRM_WORDS.search(s):
        return "person"
    if not firm_distinct(firm_canon(name)):
        return "generic"
    return "firm"


def firm_key(canon: str) -> str:
    """Space-free key so split and joined spellings collide."""
    return canon.replace(" ", "")


_FULL_DROP: frozenset[str] = frozenset({
    "INC", "INCORPORATED", "LLC", "L", "C", "LC", "PLLC", "PA", "CORP", "CORPORATION",
    "CO", "COMPANY", "LTD", "LP", "LLP", "THE", "OF", "AND", "A",
})
_FULL_MAP: dict[str, str] = {"ASSN": "ASSOCIATION", "ASSOC": "ASSOCIATION", "ASSC": "ASSOCIATION",
                             "SERVICE": "SERVICES", "SVCS": "SERVICES", "SVC": "SERVICES",
                             "SVS": "SERVICES", "SRV": "SERVICES"}


def _firm_full(name: Optional[str]) -> str:
    """Firm name with only legal suffixes dropped.

    firm_canon drops ASSOCIATION, so "PREMIER ASSOCIATION MANAGEMENT" and a
    DBA "PREMIER MANAGEMENT" share a key; this spelling tells them apart.
    """
    out = []
    for t in _prep(name).split():
        t = _FULL_MAP.get(t, t)
        if t not in ("ASSOCIATION", "SERVICES"):
            t = _FIRM_MAP.get(t, t)
        if t and t not in _FULL_DROP:
            out.append(t)
    return "".join(out)


def firm_distinct(canon: str) -> str:
    """The non-generic part of a firm name."""
    return " ".join(t for t in canon.split() if t not in _FIRM_GENERIC)


@dataclass(frozen=True)
class FirmRecord:
    """One spelling (licensee name or DBA) of one CAB license."""
    license_no: str
    name: str
    zip5: str
    canon: str
    key: str
    distinct: str
    full: str = ""      # _firm_full: ASSOCIATION / COMMUNITY kept
    dba: bool = False   # a DBA spelling, not the licensee's own name


FIRM_AUTO_METHODS = frozenset({"alias", "full_zip", "address", "exact", "exact_full", "distinct_full", "distinct_prefix_full", "firm_fuzzy_zip", "exact_zip", "exact_multi_branch",
                               "distinct", "distinct_zip", "distinct_multi_branch",
                               "distinct_prefix", "distinct_prefix_zip",
                               "distinct_prefix_multi_branch"})
FIRM_REVIEW_METHODS = frozenset({"fuzzy_review", "firm_review"})
FIRM_FUZZY_REVIEW = 90.0


@dataclass
class FirmIndex:
    """In-memory index of F4 CAM firm licenses (CAB rows), name and DBA."""
    variants: list[FirmRecord] = field(default_factory=list)
    by_key: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    by_distinct: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    by_first: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    by_full: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    aliases: dict[str, str] = field(default_factory=dict)

    @classmethod
    def build(cls, rows: Iterable[tuple[str, str, Optional[str], str]],
              aliases: Optional[dict[str, str]] = None) -> "FirmIndex":
        """rows: (license_no, licensee_name, dba, zip) for each CAB record.

        aliases maps a spelling seen in the wild to the license it means, for
        brands whose license is under a different legal name.
        """
        idx = cls(aliases={firm_key(firm_canon(k)): v for k, v in (aliases or {}).items()})
        seen: set[tuple[str, str]] = set()  # (license, full spelling)
        for lic, name, dba, zip_ in rows:
            for j, n in enumerate([name, *re.split(r"\s*;\s*", dba or "")]):
                c, full = firm_canon(n), _firm_full(n)
                if not c or (lic, full) in seen:
                    continue
                seen.add((lic, full))
                rec = FirmRecord(lic, n or "", (zip_ or "")[:5], c, firm_key(c), firm_distinct(c),
                                 full, j > 0)
                i = len(idx.variants)
                idx.variants.append(rec)
                idx.by_key[rec.key].add(i)
                idx.by_full[full].add(i)
                if rec.distinct:
                    idx.by_distinct[firm_key(rec.distinct)].add(i)
                    idx.by_first[rec.distinct.split()[0]].add(i)
        return idx

    def lookup(self, name: Optional[str], zip5: str = "") -> Match:
        """Match a firm name (a C/O line, a DBPR managing entity) to a license.

        Cascade: alias -> exact key -> distinctive words -> distinctive prefix
        -> fuzzy (review only).  Generic-only names ("PROPERTY MANAGEMENT")
        never match.
        """
        c = firm_canon(name)
        d = firm_distinct(c)
        if not d:
            lic = self.full_at_zip(name, zip5)
            return Match(lic, "full_zip") if lic else Match(None, "none", 0.0)
        k = firm_key(c)
        full = _firm_full(name)
        if k in self.aliases:
            return Match(self.aliases[k], "alias")
        hits = self.by_key.get(k)
        if hits:
            return self._resolve(hits, zip5, "exact", full=full)
        # One distinctive word ("UNITED", "TOWERS") names many firms; the
        # office must then be in the same ZIP area.
        weak = len(d.split()) == 1
        hits = self.by_distinct.get(firm_key(d))
        if hits:
            return self._resolve(hits, zip5, "distinct", weak, full)
        # "SENTRY" vs "SENTRY MGMT", "RIZZETTA" vs "RIZZETTA CO": one distinctive
        # part must be a word-prefix of the other.
        dk = firm_key(d)
        if len(dk) >= _PREFIX_MIN:
            pool = {i for i in self._prefix_pool(d)
                    if _word_prefix(self.variants[i].distinct, d)}
            # A license name longer than ours ("FOSTER" -> "FOSTER ... OF SOUTH
            # FLORIDA") is the same firm qualified; ours being longer ("CAPE
            # CORAL TAX ACCOUNTING" -> "CAPE CORAL") usually names another
            # business, so those go to review.
            longer = {i for i in pool if len(firm_key(self.variants[i].distinct)) > len(dk)}
            if longer:
                weak = len(d.split()) == 1
                return self._resolve(longer, zip5, "distinct_prefix", weak, full)
            if pool:
                m = self._resolve(pool, zip5, "distinct_prefix", weak=True)
                if m.method.endswith("_zip") and any(
                        self.variants[i].zip5 == zip5[:5] for i in pool):
                    return m
                return Match(m.doc or next(iter({self.variants[i].license_no for i in pool})),
                             "firm_review", 0.7)
        best, best_i = 0.0, None
        for i, rec in enumerate(self.variants):
            if not rec.distinct or rec.distinct[0] != d[0]:
                continue
            s = fuzz.ratio(firm_key(d), firm_key(rec.distinct))
            if s > best:
                best, best_i = s, i
        if best_i is not None and best >= FIRM_FUZZY_REVIEW:
            rec = self.variants[best_i]
            # A misspelling at the licensee's own office ZIP is the licensee.
            method = "firm_fuzzy_zip" if zip5 and rec.zip5 == zip5[:5] else "fuzzy_review"
            return Match(rec.license_no, method, best / 100.0)
        return Match(None, "none", 0.0)

    def full_at_zip(self, name: Optional[str], zip5: str) -> Optional[str]:
        """The one license spelled *name* exactly (legal suffixes aside) whose
        office is at *zip5*.

        An all-generic DBA ("CONDOMINIUM ASSOCIATES") is never enough on its
        own, but at its holder's own office ZIP it is that holder.
        """
        if not zip5:
            return None
        lics = {self.variants[i].license_no for i in self.by_full.get(_firm_full(name), ())
                if self.variants[i].zip5 == zip5[:5]}
        return next(iter(lics)) if len(lics) == 1 else None

    def _prefix_pool(self, d: str) -> set[int]:
        # Candidates share the first letters; "NEWGAUGE" must find "NEW GAUGE".
        k = firm_key(d)
        out: set[int] = set()
        for first, idxs in self.by_first.items():
            if first[0] == k[0] and (k.startswith(first) or first.startswith(k[:len(first)])):
                out |= idxs
        return out

    def _resolve(self, idxs: Iterable[int], zip5: str, method: str,
                 weak: bool = False, full: str = "") -> Match:
        """Pick one license from *idxs*.

        weak: the name evidence alone is not enough, so the firm's office
        must be in the same 3-digit ZIP area as the C/O address; otherwise
        the pair goes to review.
        full: the query's _firm_full spelling; when the canonical key is shared
        by several licenses, the one spelled exactly like the query wins, and
        among those the licensee's own name beats a DBA.
        """
        idxs = set(idxs)
        lics = {self.variants[i].license_no for i in idxs}
        z5 = {self.variants[i].license_no for i in idxs
              if zip5 and self.variants[i].zip5 == zip5[:5]}
        z3 = {self.variants[i].license_no for i in idxs
              if zip5 and self.variants[i].zip5[:3] == zip5[:3]}
        if len(z5) == 1:
            return Match(next(iter(z5)), method + "_zip")
        if full and len(lics) > 1:
            same = [self.variants[i] for i in idxs if self.variants[i].full == full]
            for pick in ({r.license_no for r in same}, {r.license_no for r in same if not r.dba}):
                if len(pick) == 1:
                    return Match(next(iter(pick)), method + "_full")
        if len(lics) == 1:
            if weak and not z3:
                return Match(next(iter(lics)), "firm_review", 0.8)
            return Match(next(iter(lics)), method)
        if len(z3) == 1:
            return Match(next(iter(z3)), method + "_zip")
        # Several licenses under one brand (branch offices): any one is the
        # firm, so keep the lowest-numbered as the representative.  Branches
        # carry one spelling; "SEABREEZE COMMUNITY ASSOCIATION MANAGEMENT" and
        # "SEA BREEZE COMMUNITY MANAGEMENT SERVICES" share a key but are two firms.
        if not weak and len({self.variants[i].full for i in idxs}) == 1:
            return Match(sorted(lics)[0], method + "_multi_branch", 0.9)
        return Match(None, "ambiguous", 0.0, tuple(sorted(lics)[:10]))


# Shortest distinctive key a prefix match may rest on: "SENTRY" yes,
# "FIRST" (First Choice / First Residential / FirstService) no.
_PREFIX_MIN = 6


def _word_prefix(a: str, b: str) -> bool:
    """True when the shorter of a, b ends on a word boundary of the longer.

    Compared without spaces, so "NEWGAUGE" is a prefix of "NEW GAUGE CITRUS
    PARK"; "TITAN" is not a prefix of "TITANIUM".
    """
    ka, kb = firm_key(a), firm_key(b)
    short, long_ = (ka, b) if len(ka) <= len(kb) else (kb, a)
    if len(short) < _PREFIX_MIN:
        return False
    acc = ""
    for w in long_.split():
        acc += w
        if acc == short:
            return True
        if len(acc) >= len(short):
            return False
    return False


# ---------------------------------------------------------------- firm offices

# "Suite #950" arrives as "SUITE STE 950": take the value after the last unit word.
_UNIT = re.compile(r"\b(?:(?:SUITE|STE|UNIT|APT|BLDG|BUILDING|OFFICE|FL|FLOOR)\b\s*)+(\w*)")
_STREET_MAP: dict[str, str] = {
    "STREET": "ST", "AVENUE": "AVE", "AV": "AVE", "BOULEVARD": "BLVD", "DRIVE": "DR", "ROAD": "RD",
    "HIGHWAY": "HWY", "PARKWAY": "PKWY", "COURT": "CT", "LANE": "LN", "PLACE": "PL",
    "CIRCLE": "CIR", "TRAIL": "TRL", "TERRACE": "TER", "SQUARE": "SQ", "WAY": "WAY",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}


def street_key(street: Optional[str]) -> tuple[str, str]:
    """(street, unit) for comparing a mailing address with a firm's office.

    "3018 N. US Highway 301, Suite #950" -> ("3018 N US HWY 301", "950").
    "P.O. Box 2878" -> ("PO BOX 2878", "").
    """
    s = str(street or "").upper().replace("#", " STE ")
    s = re.sub(r"[^A-Z0-9 ]", " ", s.replace(".", ""))
    s = re.sub(r"\b(POST OFFICE|P O)\b", "PO", " ".join(s.split()))
    unit = ""
    m = _UNIT.search(s)
    if m and not s.startswith("PO BOX"):
        unit, s = m.group(1), s[:m.start()]
    return " ".join(_STREET_MAP.get(t, t) for t in s.split()), unit


@dataclass
class FirmAddressIndex:
    """CAB firm offices by street and ZIP: a mailing address that is a firm's
    office names the firm when the C/O line does not."""
    by_street: dict[tuple[str, str], set[tuple[str, str]]] = field(
        default_factory=lambda: defaultdict(set))

    @classmethod
    def build(cls, rows: Iterable[tuple[str, Optional[str], Optional[str]]]) -> "FirmAddressIndex":
        """rows: (license_no, address_line_1, zip) for each CAB record."""
        idx = cls()
        for lic, street, zip_ in rows:
            st, unit = street_key(street)
            if st and zip_:
                idx.by_street[(st, str(zip_)[:5])].add((lic, unit))
        return idx

    def lookup(self, street: Optional[str], zip5: Optional[str]) -> Optional[str]:
        """The one license with its office at this address, or None.

        The street must carry a number or be a PO box, and an office building
        shared by several licensees ("570 CARILLON PKWY") names none of them.
        Two different suites are two offices.
        """
        st, unit = street_key(street)
        if not zip5 or not re.match(r"(\d|PO BOX \w)", st):
            return None
        hits = {lic for lic, u in self.by_street.get((st, zip5[:5]), ())
                if not (unit and u and u != unit)}
        return next(iter(hits)) if len(hits) == 1 else None
