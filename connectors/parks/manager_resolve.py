"""
Parks vertical — resolve each park to its governing account.

This is the join the plan singles out as the hard one (§5.4):

    "For the parks vertical, the genuinely difficult work is resolving a free-text
     manager string to a government account: 'City of Cary Parks, Recreation &
     Cultural Resources' -> GEOID 3710740. There are thousands of these strings
     and they are idiosyncratic. Budget real time. [...] This is not a weekend
     task and underestimating it is the most likely way this vertical slips."

It lives in its own module, with the string logic as pure functions, precisely so
it can be tested and tuned without re-running a harvest.

Resolution order
----------------
0. Declared agency.  The four state-park layers each declare a managing_agency in
   park_layers.yaml, so their parks are assigned with no matching at all.  This is
   not a shortcut: a state park sits inside some city's boundary, but the city does
   not maintain it, so a spatial rollup would invent a contract that doesn't exist.

1. Spatial.  Maximum-overlap against incorporated places and PA townships.  Uses
   overlap area rather than point containment because a park's representative point
   can fall outside its own boundary (crescent, coastal and multi-part parks) and
   land in the neighbouring municipality.

2. Name.  The §5.4 join, applied only to PAD-US, the one source whose manager field
   holds a governing-body name rather than a classification code.

3. County fallback.  Counties tile the state, so this always resolves.  A park in
   an unincorporated area genuinely is a county responsibility, and this guarantees
   every park gets a parent — which staging.resolved_location.account_key requires
   as NOT NULL.  Nothing is ever silently dropped.

Where spatial and name agree the result is method='spatial+name', the highest
confidence available.  Name matches in the 0.75-0.92 band are accepted but also
enqueued to review.pending_pairs for human adjudication.

Usage
-----
    python -m parks.manager_resolve --dry-run
    python -m parks.manager_resolve --write-db
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import defaultdict

import pandas as pd

from lib.keys import is_present
from lib.match import compound_name_similarity
from parks.config_loader import load_gov_config, load_layer_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("manager_resolve")

# Accept a name-only match at or above this score; queue for review below it.
# Mirrors the plan's §5.1 Tier-3 thresholds (0.92 auto-merge, 0.75 review floor).
AUTO_THRESHOLD: float = 0.92
QUEUE_THRESHOLD: float = 0.75

METHOD_DECLARED = "declared_agency"
METHOD_SPATIAL_NAME = "spatial+name"
METHOD_SPATIAL = "spatial"
METHOD_NAME = "name"
METHOD_COUNTY = "county_fallback"

# Government-unit source ids, by the entity class a manager string can name.
_PLACE_SOURCES = ("tiger_places",)
_COUSUB_SOURCES = ("tiger_cousub",)
_COUNTY_SOURCES = ("tiger_counties",)

# Leading "<Entity> of <Name>" forms.  The value is the entity class the string is
# claiming to be, which constrains which TIGER layer we match against.
_ENTITY_PREFIXES: dict[str, str] = {
    "CITY OF": "place",
    "TOWN OF": "place",
    "VILLAGE OF": "place",
    "BOROUGH OF": "place",
    "MUNICIPALITY OF": "place",
    "TOWNSHIP OF": "cousub",
    "COUNTY OF": "county",
}

# Trailing entity words, e.g. "BEAUFORT COUNTY", "BERWICK TOWNSHIP".
_ENTITY_SUFFIXES: dict[str, str] = {
    "COUNTY": "county",
    "PARISH": "county",
    "TOWNSHIP": "cousub",
    "TWP": "cousub",
    "CITY": "place",
    "TOWN": "place",
    "VILLAGE": "place",
    "BOROUGH": "place",
}

# Departmental / role vocabulary that decorates a governing body's name.
# Stripped only from the TAIL of the string, never from the middle — see
# manager_name_variants() for why.
_ROLE_WORDS: frozenset[str] = frozenset({
    "PARKS", "PARK", "RECREATION", "REC", "RECREATIONAL",
    "DEPARTMENT", "DEPT", "DIVISION", "DIV", "BUREAU", "OFFICE",
    "PUBLIC", "WORKS", "CULTURAL", "RESOURCES", "RESOURCE",
    "BOARD", "COMMISSION", "COMMISSIONERS", "AUTHORITY", "DISTRICT",
    "MUNICIPAL", "MUNICIPALITY", "GOVERNMENT", "GOVT",
    "OPEN", "SPACE", "SPACES", "CONSERVATION", "CONSERVANCY",
    "LANDS", "TRUST", "SERVICES", "SERVICE", "FACILITIES",
    "COMMUNITY", "LEISURE", "GREENWAY", "GREENWAYS", "TRAILS",
    "AND", "OF", "THE",
    # Texas counties delegate parks to commissioner precincts, so "Harris County
    # Precinct 4 Parks" is a common shape in PAD-US for the largest TX counties
    # (Harris, Dallas, Bexar, Travis).  Without stripping these the string scores
    # 0.59 against "Harrison" and 0.59 against "Harris", which is below the review
    # floor and falls through to the county fallback.  Stripping them lets it match
    # Harris County outright.
    "PRECINCT", "PCT", "WARD", "SECTOR", "ZONE", "REGION", "UNIT", "NO", "NUM",
    "PROGRAM", "PROGRAMS", "SYSTEM", "MAINTENANCE", "GROUNDS",
    "ADMINISTRATION", "MANAGEMENT", "MGMT",
})

# Which layers a given entity class may match, in PREFERENCE order.  Order is
# significant, not cosmetic: Pennsylvania has both a Berwick borough (an
# incorporated place) and a Berwick township (an MCD), with identical BASENAMEs and
# separate budgets.  Both match "BERWICK" exactly, so the only thing that can
# separate them is the entity word in the manager string — which means the class
# hint has to express a ranking, not just a permitted set.
#
# The secondary entry is a real fallback: municipal boundaries are described
# loosely in the wild, and "Township of X" occasionally refers to a place.
_ENTITY_CLASS_TO_SOURCES: dict[str, tuple[str, ...]] = {
    "place": _PLACE_SOURCES + _COUSUB_SOURCES,
    "cousub": _COUSUB_SOURCES + _PLACE_SOURCES,
    "county": _COUNTY_SOURCES,
}

# Preference order when the manager string carries no entity word at all.  A bare
# name like "Hilton Head Island" is far more likely to be a municipality than a
# county, and a county manager string almost always says "County".
_DEFAULT_SOURCE_PRIORITY: tuple[str, ...] = (
    "tiger_places", "tiger_cousub", "tiger_counties",
)

_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------- string logic

def strip_trailing_role_words(tokens: list[str]) -> list[str]:
    """
    Remove departmental vocabulary from the tail of *tokens*.

    Only the tail is stripped, never the middle.  Removing role words wherever they
    appear would destroy real place names built from the same vocabulary: "City of
    Overland Park" must not become "OVERLAND", and Park City, Menlo Park and
    District Heights have the same problem.  Because the government name comes
    first and the departmental description follows it, tail-only stripping is both
    sufficient and safe.

    Bare numerals are stripped too, since they only ever appear as the index of an
    administrative subdivision ("Precinct 4", "District 2") and never as the tail of
    a municipality's name.

    At least one token is always kept, so a manager string consisting entirely of
    role words is not erased to nothing.
    """
    trimmed = list(tokens)
    while len(trimmed) > 1 and (
        trimmed[-1] in _ROLE_WORDS or trimmed[-1].isdigit()
    ):
        trimmed.pop()
    return trimmed


def role_word_trim_sequence(tokens: list[str]) -> list[list[str]]:
    """
    Return every progressive tail-trim of *tokens*, most-trimmed first.

    Emitting only the maximal trim is not enough.  37 incorporated places in the
    five target states have a BASENAME whose last word is itself role vocabulary —
    Winter Park, Pinellas Park, Oakland Park, Avon Park, Orange Park.  For "City of
    Winter Park Parks and Recreation Department" the maximal trim is "WINTER" and
    the untouched form is the whole department title, so the correct answer
    "WINTER PARK" exists only at an intermediate trim depth.

    Returning the whole ladder costs at most len(tokens) candidates, all cheap to
    score, and score_manager_against_gov prefers the longest exact match — so
    "WINTER PARK" beats the shorter, also-exact "WINTER" when both are real places.
    """
    if not tokens:
        return []

    ladder: list[list[str]] = []
    trimmed = list(tokens)
    while len(trimmed) > 1 and (
        trimmed[-1] in _ROLE_WORDS or trimmed[-1].isdigit()
    ):
        trimmed.pop()
        ladder.append(list(trimmed))

    ladder.reverse()             # most-trimmed first
    ladder.append(list(tokens))  # and always the untouched form
    return ladder


def parse_manager_string(manager_normalized: str | None) -> tuple[list[str], str | None]:
    """
    Turn a normalized manager string into candidate place names and an entity hint.

    Returns (variants, entity_class).  *variants* are candidate names to match
    against TIGER, best guess first.  *entity_class* is "place", "cousub",
    "county" or None, and constrains which layer is searched.

    The entity hint is not decoration — it is load-bearing.  "Beaufort County" and
    "City of Beaufort" are both real South Carolina governments with the same base
    name and different budgets.  Matching on the base name alone would pick one at
    random.

    Examples
    --------
    >>> parse_manager_string("CITY OF CHARLESTON")
    (['CHARLESTON'], 'place')
    >>> parse_manager_string("BEAUFORT COUNTY")
    (['BEAUFORT', 'BEAUFORT COUNTY'], 'county')
    >>> parse_manager_string("CITY OF BAY CITY")
    (['BAY', 'BAY CITY'], 'place')
    >>> parse_manager_string("HILTON HEAD ISLAND")
    (['HILTON HEAD ISLAND'], None)
    """
    if manager_normalized is None:
        return [], None
    try:
        if pd.isna(manager_normalized):
            return [], None
    except (TypeError, ValueError):
        pass

    text = _WS_RE.sub(" ", str(manager_normalized).strip().upper())
    if not text:
        return [], None

    entity_class: str | None = None

    # Leading "<Entity> OF" — check longest prefixes first so "COUNTY OF" is not
    # shadowed by a shorter match.
    for prefix in sorted(_ENTITY_PREFIXES, key=len, reverse=True):
        if text.startswith(prefix + " "):
            entity_class = _ENTITY_PREFIXES[prefix]
            text = text[len(prefix) + 1:].strip()
            break

    tokens = text.split()
    if not tokens:
        return [], entity_class

    # Strip trailing departmental vocabulary FIRST, then look for an entity word.
    #
    # Order matters here.  The entity word is not reliably the last token:
    # "Wake County Parks, Recreation and Open Space" puts COUNTY in the middle,
    # so checking the raw tail finds SPACE, concludes there is no entity hint, and
    # then loses to the place "Wake Forest" on a fuzzy score.  Removing the role
    # words first exposes COUNTY as the tail and pins the class to county, where
    # "WAKE" matches exactly.
    trimmed = strip_trailing_role_words(tokens)

    # TIGER stores BASENAME without the entity word, so the de-suffixed form is
    # usually what matches.  It cannot be the only candidate though: 161 places
    # across the five target states have a BASENAME that itself ends in an entity
    # word — Bay City, Bridge City, Colorado City, Bunker Hill Village.  For
    # "City of Bay City" the right variant is the intact "BAY CITY"; for "Beaufort
    # County" it is the stripped "BEAUFORT".  Nothing in the string separates the
    # two cases, so both are offered and the scorer keeps whichever matches a real
    # government, preferring the longer name on a tie.
    candidates: list[list[str]] = []
    if len(trimmed) > 1 and trimmed[-1] in _ENTITY_SUFFIXES:
        if entity_class is None:
            entity_class = _ENTITY_SUFFIXES[trimmed[-1]]
        candidates.append(trimmed[:-1])
    candidates.extend(role_word_trim_sequence(tokens))

    variants: list[str] = []
    seen: set[str] = set()
    for toks in candidates:
        variant = " ".join(toks)
        if variant and variant not in seen:
            seen.add(variant)
            variants.append(variant)

    return variants, entity_class


def score_manager_against_gov(
    variants: list[str],
    candidates: list[tuple[str, str, str, int]],
) -> tuple[tuple[str, str] | None, float]:
    """
    Score manager-name *variants* against government *candidates*.

    *candidates* is a list of (gov_source_id, gov_natural_key, name_normalized,
    source_rank), where a lower rank is a more preferred layer for the entity class
    that was parsed out of the manager string.
    Returns ((gov_source_id, gov_natural_key), score) for the best match, or
    (None, 0.0) when there are no candidates.

    Most manager strings match some variant exactly once the entity prefix and
    trailing role words are handled, so the fuzzy comparison is only really paid
    for the genuine residue.

    Ties are broken deterministically, in this order:

      1. higher score
      2. longer matching variant — settles the "Bay City" family, where "City of
         Bay City" yields ["BAY", "BAY CITY"] and the more specific name is right
      3. lower source rank — settles Berwick borough vs Berwick township, where
         both names match identically and only the entity word distinguishes them

    Without an explicit tie-break the winner would depend on dict iteration order,
    which is a silent, unreproducible way to assign an account.
    """
    best: tuple[str, str] | None = None
    best_score = 0.0
    best_len = -1
    best_rank = 1 << 30

    for variant in variants:
        for source_id, natural_key, gov_name, rank in candidates:
            score = 1.0 if variant == gov_name else compound_name_similarity(variant, gov_name)
            better = (
                score > best_score
                or (score == best_score and len(variant) > best_len)
                or (score == best_score and len(variant) == best_len and rank < best_rank)
            )
            if better:
                best_score = score
                best_len = len(variant)
                best_rank = rank
                best = (source_id, natural_key)

    return best, best_score


def build_gov_index(gov_df: pd.DataFrame) -> dict[tuple[str, str], list[tuple[str, str, str]]]:
    """
    Index government units by (state, blocking_prefix) for candidate lookup.

    Blocking on the first four characters of the normalized name keeps this out of
    the 64,700 parks x 5,547 governments cross product that a naive scan would be.
    The prefix is taken from the government name, and looked up using each manager
    variant's own prefix.
    """
    index: dict[tuple[str, str], list[tuple[str, str, str]]] = defaultdict(list)
    for row in gov_df.itertuples(index=False):
        name = row.name_normalized or ""
        if not name:
            continue
        key = (row.state, name[:4])
        index[key].append((row.gov_source_id, row.gov_natural_key, name))
    return dict(index)


def candidates_for(
    gov_index: dict[tuple[str, str], list[tuple[str, str, str]]],
    state: str,
    variants: list[str],
    entity_class: str | None,
) -> list[tuple[str, str, str, int]]:
    """
    Collect blocked candidates for *variants*, ranked by *entity_class* preference.

    Each candidate is returned as (gov_source_id, gov_natural_key,
    name_normalized, source_rank).  Candidates from layers the entity class does
    not permit are excluded entirely; permitted ones carry their preference rank so
    score_manager_against_gov can break exact-match ties.
    """
    order = (
        _ENTITY_CLASS_TO_SOURCES.get(entity_class, _DEFAULT_SOURCE_PRIORITY)
        if entity_class else _DEFAULT_SOURCE_PRIORITY
    )
    ranks = {source_id: rank for rank, source_id in enumerate(order)}

    out: list[tuple[str, str, str, int]] = []
    seen: set[tuple[str, str]] = set()
    for variant in variants:
        for cand in gov_index.get((state, variant[:4]), ()):
            rank = ranks.get(cand[0])
            if rank is None:
                continue
            ident = (cand[0], cand[1])
            if ident not in seen:
                seen.add(ident)
                out.append((cand[0], cand[1], cand[2], rank))
    return out


def resolve_manager_names(
    manager_df: pd.DataFrame,
    gov_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Resolve distinct (state, manager_normalized) pairs to government units.

    Resolution is done per DISTINCT manager string rather than per park: tens of
    thousands of PAD-US rows share a few thousand manager strings, so this collapses
    the work by an order of magnitude and makes the result trivially cacheable.

    *manager_df* needs columns: state, manager_normalized.
    *gov_df* needs columns: gov_source_id, gov_natural_key, state, name_normalized.

    Returns one row per input pair with gov_source_id, gov_natural_key, score and
    entity_class (score 0.0 and null gov columns when nothing matched).
    """
    gov_index = build_gov_index(gov_df)

    rows = []
    for pair in manager_df.drop_duplicates(
        subset=["state", "manager_normalized"]
    ).itertuples(index=False):
        variants, entity_class = parse_manager_string(pair.manager_normalized)
        if not variants:
            rows.append({
                "state": pair.state,
                "manager_normalized": pair.manager_normalized,
                "gov_source_id": None,
                "gov_natural_key": None,
                "score": 0.0,
                "entity_class": entity_class,
            })
            continue

        cands = candidates_for(gov_index, pair.state, variants, entity_class)
        best, score = score_manager_against_gov(variants, cands)
        rows.append({
            "state": pair.state,
            "manager_normalized": pair.manager_normalized,
            "gov_source_id": best[0] if best else None,
            "gov_natural_key": best[1] if best else None,
            "score": score,
            "entity_class": entity_class,
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------- combine

def combine_assignments(
    parks: pd.DataFrame,
    spatial: pd.DataFrame,
    names: pd.DataFrame,
    counties: pd.DataFrame,
    declared: pd.DataFrame,
) -> pd.DataFrame:
    """
    Merge the resolution stages into one assignment per park.

    *parks* is the spine: park_source_id, park_natural_key, state, manager_normalized.
    The other frames each supply gov_source_id / gov_natural_key keyed to it.

    Precedence, highest first:
      declared agency > spatial+name agreement > spatial > name > county fallback

    Every park in *parks* appears exactly once in the output.
    """
    out = parks[["park_source_id", "park_natural_key", "state"]].copy()
    pk = ["park_source_id", "park_natural_key"]

    def _attach(frame: pd.DataFrame, suffix: str) -> None:
        if frame is None or frame.empty:
            out[f"gov_source_id_{suffix}"] = None
            out[f"gov_natural_key_{suffix}"] = None
            return
        cols = pk + ["gov_source_id", "gov_natural_key"]
        merged = frame[cols].drop_duplicates(subset=pk)
        joined = out.merge(merged, on=pk, how="left", suffixes=("", "_new"))
        out[f"gov_source_id_{suffix}"] = joined["gov_source_id"].values
        out[f"gov_natural_key_{suffix}"] = joined["gov_natural_key"].values

    _attach(declared, "declared")
    _attach(spatial, "spatial")
    _attach(counties, "county")

    # Name results are keyed by (state, manager_normalized), not by park.
    if names is not None and not names.empty:
        name_lookup = names.set_index(["state", "manager_normalized"])
        keyed = parks.set_index(["state", "manager_normalized"])
        joined = keyed.join(name_lookup, how="left").reset_index()
        joined = joined.drop_duplicates(subset=pk)
        name_map = joined.set_index(pk)
        idx = pd.MultiIndex.from_frame(out[pk])
        out["gov_source_id_name"] = name_map["gov_source_id"].reindex(idx).values
        out["gov_natural_key_name"] = name_map["gov_natural_key"].reindex(idx).values
        out["name_score"] = name_map["score"].reindex(idx).values
    else:
        out["gov_source_id_name"] = None
        out["gov_natural_key_name"] = None
        out["name_score"] = 0.0

    out["name_score"] = pd.to_numeric(out["name_score"], errors="coerce").fillna(0.0)

    # A name result below the review floor is not evidence of anything.
    weak = out["name_score"] < QUEUE_THRESHOLD
    out.loc[weak, ["gov_source_id_name", "gov_natural_key_name"]] = None

    resolved = []
    for row in out.itertuples(index=False):
        # is_present, not bool(): a pandas left-join leaves float NaN in the
        # unmatched cells, and bool(float("nan")) is True.  Using plain truthiness
        # here made every park look like it had a declared agency.
        has_declared = is_present(row.gov_source_id_declared)
        has_spatial = is_present(row.gov_source_id_spatial)
        has_name = is_present(row.gov_source_id_name)

        if has_declared:
            gov, method, score = (
                (row.gov_source_id_declared, row.gov_natural_key_declared),
                METHOD_DECLARED,
                None,
            )
        elif has_spatial and has_name and (
            row.gov_source_id_spatial == row.gov_source_id_name
            and row.gov_natural_key_spatial == row.gov_natural_key_name
        ):
            gov, method, score = (
                (row.gov_source_id_spatial, row.gov_natural_key_spatial),
                METHOD_SPATIAL_NAME,
                row.name_score,
            )
        elif has_spatial:
            gov, method, score = (
                (row.gov_source_id_spatial, row.gov_natural_key_spatial),
                METHOD_SPATIAL,
                None,
            )
        elif has_name:
            gov, method, score = (
                (row.gov_source_id_name, row.gov_natural_key_name),
                METHOD_NAME,
                row.name_score,
            )
        elif is_present(row.gov_source_id_county):
            gov, method, score = (
                (row.gov_source_id_county, row.gov_natural_key_county),
                METHOD_COUNTY,
                None,
            )
        else:
            # No county either — the park's geometry is missing or outside the
            # 5-state footprint.  Emitted with a null government so the caller can
            # count and report it rather than have it vanish from the totals.
            gov, method, score = ((None, None), None, None)

        resolved.append({
            "park_source_id": row.park_source_id,
            "park_natural_key": row.park_natural_key,
            "gov_source_id": gov[0],
            "gov_natural_key": gov[1],
            "method": method,
            "score": score,
        })

    return pd.DataFrame(resolved)


def review_candidates(combined: pd.DataFrame, names: pd.DataFrame) -> pd.DataFrame:
    """
    Select name-only matches in the review band for review.pending_pairs.

    A match that both spatial overlap and the name join agree on needs no review;
    an uncorroborated name match between the queue floor and the auto threshold is
    exactly the "human adjudication" case the plan's §5.1 describes.
    """
    if combined.empty:
        return pd.DataFrame()
    band = combined[
        (combined["method"] == METHOD_NAME)
        & combined["score"].notna()
        & (combined["score"] >= QUEUE_THRESHOLD)
        & (combined["score"] < AUTO_THRESHOLD)
    ]
    return band.copy()


# ---------------------------------------------------------------- db i/o

# Government layers a municipal park may roll up to, and the county layer used as
# the guaranteed fallback.
_MUNICIPAL_GOV_SOURCES = list(_PLACE_SOURCES + _COUSUB_SOURCES)
_COUNTY_GOV_SOURCES = list(_COUNTY_SOURCES)


def _park_union_sql(sources: list[str]) -> str:
    """
    Build a UNION ALL over the per-source park staging tables.

    Park rows live in one table per source (staging.padus_parks, etc.), so any
    cross-source query has to union them.  Source ids come from the registry and
    are validated as SQL identifiers before interpolation.
    """
    from lib.db import _assert_safe_identifier

    parts = []
    for src in sources:
        _assert_safe_identifier(src)
        parts.append(
            f"SELECT source_id, natural_key, state, geom, account_type "
            f"FROM staging.{src}"
        )
    return "\n            UNION ALL\n            ".join(parts)


def load_parks(engine, sources: list[str]) -> pd.DataFrame:
    """Load the park spine: identity, state, and the manager string."""
    from sqlalchemy import text

    sql = text(f"""
        WITH parks AS (
            {_park_union_sql(sources)}
        )
        SELECT p.source_id  AS park_source_id,
               p.natural_key AS park_natural_key,
               p.state,
               p.account_type,
               a.manager_normalized,
               (a.boundary IS NOT NULL) AS has_boundary
        FROM parks p
        LEFT JOIN staging.park_attrs a
               ON a.source_id = p.source_id
              AND a.natural_key = p.natural_key
    """)  # nosec: source ids validated in _park_union_sql
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn)
    logger.info("loaded %d park rows across %d sources", len(df), len(sources))
    return df


def load_gov_names(engine, sources: list[str]) -> pd.DataFrame:
    """Load government unit names for the §5.4 name join."""
    from sqlalchemy import text
    from lib.db import _assert_safe_identifier

    parts = []
    for src in sources:
        _assert_safe_identifier(src)
        parts.append(
            f"SELECT source_id AS gov_source_id, natural_key AS gov_natural_key, "
            f"state, name_normalized FROM staging.{src}"
        )
    sql = text("\n        UNION ALL\n        ".join(parts))  # nosec: validated above
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn)
    logger.info("loaded %d government units", len(df))
    return df


def spatial_rollup(engine, sources: list[str], gov_sources: list[str]) -> pd.DataFrame:
    """
    Assign each park to the government whose boundary it overlaps most.

    Maximum overlap rather than point containment: a park's stored point is a
    bounding-box centre, and for crescent, coastal or multi-part parks that point
    can fall outside the park's own boundary and inside the neighbouring
    municipality.  Ranking by shared area uses the whole footprint instead.

    Overlap is ranked using planar ST_Area in degrees rather than casting the
    intersection to geography.  Ranking only needs to be monotonic among candidates
    that are, by construction, adjacent to one another, and the geography cast is
    markedly more expensive across ~72k parks.

    Parks without a boundary fall back to their point, for which the intersection
    area is zero — harmless, because incorporated places do not overlap one
    another, so a contained point has exactly one candidate.
    """
    from sqlalchemy import text

    sql = text(f"""
        WITH parks AS (
            {_park_union_sql(sources)}
        ),
        shapes AS (
            SELECT p.source_id, p.natural_key,
                   COALESCE(a.boundary, p.geom) AS shape
            FROM parks p
            LEFT JOIN staging.park_attrs a
                   ON a.source_id = p.source_id
                  AND a.natural_key = p.natural_key
            WHERE COALESCE(a.boundary, p.geom) IS NOT NULL
        )
        SELECT DISTINCT ON (s.source_id, s.natural_key)
               s.source_id   AS park_source_id,
               s.natural_key AS park_natural_key,
               g.source_id   AS gov_source_id,
               g.natural_key AS gov_natural_key
        FROM shapes s
        JOIN staging.gov_unit_boundary g
          ON g.source_id = ANY(:gov_sources)
         AND ST_Intersects(g.boundary, s.shape)
        ORDER BY s.source_id, s.natural_key,
                 ST_Area(ST_Intersection(g.boundary, s.shape)) DESC,
                 g.natural_key
    """)  # nosec: source ids validated in _park_union_sql
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params={"gov_sources": gov_sources})
    logger.info("spatial rollup matched %d parks", len(df))
    return df


def write_rollup(engine, df: pd.DataFrame) -> int:
    """
    Replace staging.park_rollup with *df*.

    Full replace rather than upsert: the rollup is derived state, and a park that
    stops matching must lose its old assignment rather than keep a stale one.
    Delete and insert share one transaction so a failure cannot empty the table.
    """
    from sqlalchemy import text

    rows = df[df["gov_source_id"].notna()][[
        "park_source_id", "park_natural_key",
        "gov_source_id", "gov_natural_key", "method", "score",
    ]].copy()
    rows["score"] = rows["score"].astype(object).where(rows["score"].notna(), None)
    payload = rows.to_dict(orient="records")

    sql = text("""
        INSERT INTO staging.park_rollup (
            park_source_id, park_natural_key, gov_source_id, gov_natural_key,
            method, score
        ) VALUES (
            :park_source_id, :park_natural_key, :gov_source_id, :gov_natural_key,
            :method, CAST(:score AS numeric)
        )
        ON CONFLICT (park_source_id, park_natural_key) DO UPDATE SET
            gov_source_id   = EXCLUDED.gov_source_id,
            gov_natural_key = EXCLUDED.gov_natural_key,
            method          = EXCLUDED.method,
            score           = EXCLUDED.score,
            loaded_at       = now()
    """)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM staging.park_rollup"))
        if payload:
            conn.execute(sql, payload)
    logger.info("wrote %d park_rollup rows", len(payload))
    return len(payload)


def declared_agency_assignments(parks: pd.DataFrame, registry: dict) -> pd.DataFrame:
    """
    Assign parks from sources that declare a managing agency.

    The four state-park layers each name their agency in park_layers.yaml, so their
    parks need no matching.  gov_source_id is the sentinel 'state_agency' and
    gov_natural_key is the agency slug; parks_merge.py materialises the
    corresponding accounts.
    """
    declared_sources = {
        src: cfg.managing_agency_slug
        for src, cfg in registry.items()
        if cfg.managing_agency_slug
    }
    if not declared_sources:
        return pd.DataFrame(
            columns=["park_source_id", "park_natural_key", "gov_source_id", "gov_natural_key"]
        )

    subset = parks[parks["park_source_id"].isin(declared_sources)].copy()
    if subset.empty:
        return pd.DataFrame(
            columns=["park_source_id", "park_natural_key", "gov_source_id", "gov_natural_key"]
        )
    subset["gov_source_id"] = "state_agency"
    subset["gov_natural_key"] = subset["park_source_id"].map(declared_sources)
    return subset[[
        "park_source_id", "park_natural_key", "gov_source_id", "gov_natural_key"
    ]]


def enqueue_review_pairs(engine, queued: pd.DataFrame) -> int:
    """
    Write uncorroborated name matches to review.pending_pairs for adjudication.

    Only the 0.75-0.92 band reaches here: below that the match is discarded as
    weak, above it the score is treated as decisive, and a spatial+name agreement
    needs no human at all.  The pair is (park, government) rather than
    (record, record), which is why it carries its own merge_strategy.

    Failure is logged and swallowed.  The rollup itself is already committed at
    this point and is the pipeline's actual output; losing a review hint should not
    fail a run that otherwise succeeded.
    """
    if queued.empty:
        return 0

    from lib.match_queue import STRATEGY_PARKS_MANAGER, enqueue_tier3_matches

    pairs = [
        {
            "source_a": row.park_source_id,
            "key_a": row.park_natural_key,
            "source_b": row.gov_source_id,
            "key_b": row.gov_natural_key,
            "score": float(row.score),
            "feature_breakdown": {"match_basis": "manager_name"},
        }
        for row in queued.itertuples(index=False)
    ]
    try:
        n = enqueue_tier3_matches(
            engine, pairs, merge_strategy=STRATEGY_PARKS_MANAGER
        )
        logger.info("enqueued %d pairs to review.pending_pairs", n)
        return n
    except Exception as exc:
        logger.warning(
            "could not enqueue %d review pairs (%s) — rollup is unaffected",
            len(pairs), exc,
        )
        return 0


def print_summary(combined: pd.DataFrame, parks: pd.DataFrame) -> None:
    """Report the resolution-method mix to stderr."""
    total = len(parks)
    sys.stderr.write(f"\n  manager_resolve: {total:,} parks\n")
    sys.stderr.write("  method breakdown\n")
    counts = combined["method"].value_counts(dropna=False).sort_index()
    for method, count in counts.items():
        label = "UNRESOLVED" if pd.isna(method) else str(method)
        pct = count / total if total else 0.0
        sys.stderr.write(f"    {label:<18} {count:>8,}  {pct:>6.1%}\n")

    unresolved = int(combined["method"].isna().sum())
    if unresolved:
        sys.stderr.write(
            f"    WARNING: {unresolved:,} parks resolved to no government at all — "
            f"missing geometry or outside the 5-state footprint\n"
        )

    fallback = int((combined["method"] == METHOD_COUNTY).sum())
    if total and fallback / total > 0.5:
        sys.stderr.write(
            f"    WARNING: {fallback / total:.0%} of parks fell back to their county. "
            f"Check that the gov spine loaded and that PAD-US Loc_Mang is populated.\n"
        )


def run(engine, dry_run: bool = False) -> dict:
    """Execute the full park -> government resolution and write staging.park_rollup."""
    registry = load_layer_config()
    gov_registry = load_gov_config()
    park_sources = list(registry)
    gov_sources = [s for s in gov_registry if s in _MUNICIPAL_GOV_SOURCES]
    county_sources = [s for s in gov_registry if s in _COUNTY_GOV_SOURCES]

    parks = load_parks(engine, park_sources)
    if parks.empty:
        logger.warning("no park rows in staging — nothing to resolve")
        return {"parks": 0, "written": 0}

    declared = declared_agency_assignments(parks, registry)

    # Only municipal-park sources take part in the spatial and name joins; the
    # state-park sources are already assigned to their declared agency.
    declared_ids = set(
        zip(declared["park_source_id"], declared["park_natural_key"])
    ) if not declared.empty else set()
    municipal = parks[
        ~pd.Series(
            list(zip(parks["park_source_id"], parks["park_natural_key"])),
            index=parks.index,
        ).isin(declared_ids)
    ]

    municipal_sources = [
        s for s in park_sources if not registry[s].managing_agency_slug
    ]
    spatial = (
        spatial_rollup(engine, municipal_sources, gov_sources)
        if municipal_sources else pd.DataFrame()
    )
    counties = (
        spatial_rollup(engine, municipal_sources, county_sources)
        if municipal_sources else pd.DataFrame()
    )

    gov_names = load_gov_names(engine, list(gov_registry))
    with_manager = municipal[municipal["manager_normalized"].notna()]
    names = (
        resolve_manager_names(with_manager, gov_names)
        if not with_manager.empty else pd.DataFrame()
    )
    logger.info(
        "name join: %d distinct manager strings from %d parks",
        0 if names.empty else len(names), len(with_manager),
    )

    combined = combine_assignments(parks, spatial, names, counties, declared)
    print_summary(combined, parks)

    queued = review_candidates(combined, names)
    if not queued.empty:
        sys.stderr.write(
            f"  manager_resolve: {len(queued):,} name-only matches in the "
            f"{QUEUE_THRESHOLD}-{AUTO_THRESHOLD} review band\n"
        )

    if dry_run:
        logger.info("DRY RUN — staging.park_rollup not written, nothing enqueued")
        return {"parks": len(parks), "written": 0, "queued": len(queued)}

    written = write_rollup(engine, combined)
    n_queued = enqueue_review_pairs(engine, queued)
    return {"parks": len(parks), "written": written, "queued": n_queued}


def main() -> None:
    """CLI entrypoint."""
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve and report, but do not write staging.park_rollup.",
    )
    ap.add_argument(
        "--write-db",
        action="store_true",
        help="Write staging.park_rollup (default unless --dry-run is given).",
    )
    args = ap.parse_args()

    from lib.db import get_engine
    from lib.http import get_secret

    if not get_secret("DATABASE_URL"):
        sys.exit(
            "ERROR: DATABASE_URL is not set. Copy .env.example -> .env and fill it in."
        )

    run(get_engine(), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
