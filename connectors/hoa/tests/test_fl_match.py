"""Tests for connectors/hoa/fl_match.py: the FL association and CAM-firm matchers.

Each case is a pattern seen in the Hillsborough sample (docs/hoa-pipeline.md
§6), reduced to synthetic records.
"""
from __future__ import annotations

import pytest

from connectors.hoa.fl_match import (
    AssociationIndex,
    FirmIndex,
    Match,
    assoc_canon,
    care_of_kind,
    designators,
    firm_canon,
    parse_care_of,
    types_compatible,
)


# ---------------------------------------------------------------- association side

ASSOC_ROWS = [
    ("N0001", "CAMBRIDGE HOMEOWNERS ASSOCIATION, INC.", ["33647"]),
    ("N0002", "ARAWANA, A CONDOMINIUM ASSOCIATION, INC.", ["33647"]),
    ("N0003", "BAYSHORE TOWERS BLDG L CONDOMINIUM ASSOCIATION, INC.", ["33606"]),
    ("N0004", "BAYSHORE TOWERS BLDG N CONDOMINIUM ASSOCIATION, INC.", ["33606"]),
    ("N0005", "RAINTREE VILLAGE CONDOMINIUM NO. 3 ASSOCIATION, INC.", ["33624"]),
    ("N0006", "RAINTREE VILLAGE CONDOMINIUM NO. 4 ASSOCIATION, INC.", ["33624"]),
    ("N0007", "THE BREEZE HOMEOWNERS ASSOCIATION, INC.", ["33569"]),
    ("N0008", "LUXURY ESTATES OF FISHHAWK HOMEOWNERS ASSOCIATION, INC.", ["33547"]),
    ("N0009", "STONEY CREEK HOMEOWNERS ASSOCIATION, INC.", ["32501"]),
    ("N0010", "STONEY CREEK COMMUNITY ASSOCIATION, INC.", ["34470"]),
    ("N0011", "PALM COVE CONDOMINIUM ASSOCIATION, INC.", ["33611"]),
    ("N0012", "PALM COVE HOMEOWNERS ASSOCIATION, INC.", ["33611"]),
    ("N0013", "WILLOW BEND ESTATES HOMEOWNERS ASSOCIATION, INC.", ["33558"]),
    ("N0014", "HUNTERS GREEN COMMUNITY ASSOCIATION, INC.", ["33647"]),
]
HILLS = frozenset({"33647", "33606", "33624", "33569", "33547", "33611", "33558"})


@pytest.fixture(scope="module")
def aidx() -> AssociationIndex:
    return AssociationIndex.build(ASSOC_ROWS)


def test_canon_folds_suffixes_and_glued_words():
    assert assoc_canon("Cambridge Homeowners Association, Inc.") == assoc_canon(
        "CAMBRIDGE HOMEOWNERS ASSN INC")
    assert assoc_canon("BREEZEHOMEOWNERS ASSOCIATION") == assoc_canon("BREEZE HOMEOWNERS ASSOCIATION")


def test_exact_with_zip(aidx):
    m = aidx.lookup("Cambridge Homeowners Assn Inc", ["33647"])
    assert (m.doc, m.method, m.auto) == ("N0001", "exact_zip", True)


def test_article_a_is_not_a_building_designator(aidx):
    # ", A CONDOMINIUM" is the article, not building A.
    assert designators("ARAWANA, A CONDOMINIUM ASSOCIATION, INC.") == frozenset()
    m = aidx.lookup("ARAWANA CONDOMINIUM ASSOCIATION INC", ["33647"])
    assert m.doc == "N0002" and m.auto


def test_building_designators_must_agree(aidx):
    m = aidx.lookup("BAYSHORE TOWERS BLDG L CONDOMINIUM ASSN", ["33606"])
    assert m.doc == "N0003"
    m = aidx.lookup("BAYSHORE TOWERS BLDG M CONDOMINIUM ASSN", ["33606"])
    assert m.doc not in ("N0003", "N0004")


def test_phase_number_must_agree(aidx):
    m = aidx.lookup("RAINTREE VILLAGE CONDO #3 ASSN INC", ["33624"])
    assert m.doc == "N0005"
    m = aidx.lookup("RAINTREE VILLAGE CONDO #5 ASSN INC", ["33624"])
    assert m.doc not in ("N0005", "N0006")


def test_leading_the_and_glued_homeowners(aidx):
    m = aidx.lookup("BREEZEHOMEOWNERS ASSOCIATION INC", ["33569"])
    assert m.doc == "N0007" and m.auto


@pytest.mark.parametrize("cut", [
    "LUXURY ESTATES OF FISHHAWK HOM",          # 30-char F7 cut mid-word
    "LUXURY ESTATES OF FISHHAW",
])
def test_truncated_owner_name_prefix(aidx, cut):
    m = aidx.lookup(cut, ["33547"], truncated=True)
    assert m.doc == "N0008" and m.auto


def test_truncated_short_fragment_does_not_overmatch(aidx):
    # "WILLOW" alone is too short to be a safe prefix.
    m = aidx.lookup("WILLOW", ["33558"], truncated=True)
    assert not m.auto


def test_common_name_far_away_goes_to_review(aidx):
    # STONEY CREEK recurs statewide and none is in Hillsborough: review, not auto.
    m = aidx.lookup("STONEY CREEK HOMEOWNERS ASSOCIATION INC", ["33647"], region=HILLS)
    assert m.doc == "N0009" and m.method == "exact_review" and not m.auto


def test_unique_multiword_name_out_of_county_is_auto(aidx):
    m = aidx.lookup("LUXURY ESTATES OF FISHHAWK HOMEOWNERS ASSOCIATION", [], region=frozenset({"32501"}))
    assert m.doc == "N0008" and m.method == "exact" and m.auto


def test_name_only_in_county_is_auto(aidx):
    m = aidx.lookup("WILLOW BEND ESTATES HOMEOWNERS ASSOCIATION", [], region=HILLS)
    assert m.doc == "N0013" and m.method == "exact_county" and m.auto


def test_condo_vs_hoa_same_core_picks_same_type(aidx):
    m = aidx.lookup("PALM COVE CONDOMINIUM ASSN INC", ["33611"])
    assert m.doc == "N0011"
    m = aidx.lookup("PALM COVE HOMEOWNERS ASSN INC", ["33611"])
    assert m.doc == "N0012"


def test_types_compatible():
    assert not types_compatible(assoc_canon("PALM COVE CONDOMINIUM ASSN"),
                                assoc_canon("PALM COVE HOMEOWNERS ASSN"))
    assert types_compatible(assoc_canon("PALM COVE HOMEOWNERS ASSN"),
                            assoc_canon("PALM COVE COMMUNITY ASSN"))


def test_fuzzy_same_zip_is_review_only(aidx):
    m = aidx.lookup("HUNTER GREEN COMMUNITY ASSOCIATION INC", ["33647"])
    assert m.doc == "N0014"
    if m.method == "fuzzy_zip":
        assert not m.auto


def test_unknown_name(aidx):
    m = aidx.lookup("NOWHERE LAKES HOMEOWNERS ASSOCIATION", ["33647"])
    assert m.doc is None and not m.auto


def test_name_only_disallowed_rejects_zipless_match(aidx):
    m = aidx.lookup("CAMBRIDGE HOMEOWNERS ASSOCIATION", ["33999"], allow_name_only=False)
    assert m.doc is None and m.method == "name_only_rejected"


def test_review_method_names_are_never_auto():
    for method in ("fuzzy_zip", "core_zip_type", "exact_review", "prefix_review",
                   "fuzzy_review", "firm_review"):
        assert not Match("X", method).auto, method


# ---------------------------------------------------------------- C/O parsing and kinds

@pytest.mark.parametrize("fields,expected", [
    (("C/O SENTRY MGMT INC", "2180 W SR 434"), "SENTRY MGMT INC"),
    (("C/O RIZZETTA & CO., LLC 9530 MARKETPLACE RD",), "RIZZETTA & CO., LLC"),
    (("CO GREENACRES PROPERTIES INC STE 200",), "GREENACRES PROPERTIES INC"),
    (("ATTN: AMERI-TECH COMMUNITY MANAGEMENT",), "AMERI-TECH COMMUNITY MANAGEMENT"),
    (("123 MAIN ST", "C/O REALMANAGE - TAMPA"), "REALMANAGE"),
    (("PO BOX 1234",), None),
    ((None, ""), None),
])
def test_parse_care_of(fields, expected):
    got = parse_care_of(*fields)
    assert (got.strip() if got else got) == expected


@pytest.mark.parametrize("name,kind", [
    ("SENTRY MANAGEMENT INC", "firm"),
    ("GREENACRES PROPERTIES", "firm"),
    ("BUSH ROSS PA LAW FIRM", "legal"),
    ("REGISTERED AGENTS INC", "legal"),
    ("HUNTERS GREEN HOMEOWNERS ASSN", "association"),
    ("MEADOW POINTE COMMUNITY DEVELOPMENT DISTRICT", "district"),
    ("JOHN Q PUBLIC", "person"),
    ("PROPERTY MANAGEMENT", "generic"),
])
def test_care_of_kind(name, kind):
    assert care_of_kind(name) == kind


# ---------------------------------------------------------------- firm side

FIRM_ROWS = [
    ("CAB0001", "REALMANAGE LLC", None, "33607"),
    ("CAB0002", "A R CHOICE MANAGEMENT INC", None, "33511"),
    ("CAB0003", "GREENACRE PROPERTIES INC", None, "33558"),
    ("CAB0004", "CENTRAL FLORIDA COMMUNITY MANAGEMENT LLC", None, "32801"),
    ("CAB0005", "FIRSTSERVICE RESIDENTIAL FLORIDA INC", None, "33607"),
    ("CAB0006", "FIRST CHOICE ASSOCIATION MANAGEMENT INC", None, "33702"),
    ("CAB0007", "REALTY ASSOCIATES OF CAPE CORAL INC", None, "33904"),
    ("CAB0008", "UNITED PROPERTY MANAGEMENT LLC", None, "33510"),
    ("CAB0009", "UNITED COMMUNITY MANAGEMENT INC", None, "32003"),
    ("CAB0010", "SENTRY MANAGEMENT INC", None, "32779"),
    ("CAB0011", "SENTRY MANAGEMENT INC", None, "33610"),
    ("CAB0012", "WISE PROPERTY MANAGEMENT LLC", "WISE MGMT; HOMETOWN CAM", "33619"),
    ("CAB0013", "MERIDIAN MANAGEMENT GROUP INC", None, "33647"),
]


@pytest.fixture(scope="module")
def fidx() -> FirmIndex:
    return FirmIndex.build(FIRM_ROWS)


def test_firm_canon_spacing_and_spelling():
    assert firm_canon("REAL MANAGE") == firm_canon("REALMANAGE")
    assert firm_canon("FIRST SERVICE RESIDENTIAL") == firm_canon("FIRSTSERVICE RESIDENTIAL")
    assert firm_canon("SENTRY MANAGEMENT SEVICES") == firm_canon("SENTRY MANAGEMENT SERVICES")
    assert firm_canon("SENTRY MANAGMENT") == firm_canon("SENTRY MGT INC")


def test_exact_zip(fidx):
    m = fidx.lookup("REALMANAGE", "33607")
    assert (m.doc, m.auto) == ("CAB0001", True)


def test_spaced_brand(fidx):
    m = fidx.lookup("REAL MANAGE LLC", "33607")
    assert m.doc == "CAB0001" and m.auto


def test_joined_initials(fidx):
    m = fidx.lookup("AR CHOICE MGMT", "33511")
    assert m.doc == "CAB0002" and m.auto


def test_plural_folds(fidx):
    m = fidx.lookup("GREENACRES PROPERTIES INC", "33558")
    assert m.doc == "CAB0003" and m.auto


def test_cut_tail_word(fidx):
    m = fidx.lookup("CENTRAL FLORID COMMUNITY MGMT", "32801")
    assert m.doc == "CAB0004" and m.auto


def test_first_service_is_firstservice_not_first_choice(fidx):
    m = fidx.lookup("FIRST SERVICE RESIDENTIAL", "33607")
    assert m.doc == "CAB0005" and m.auto
    m = fidx.lookup("FIRST RESIDENTIAL", "33607")
    assert m.doc != "CAB0006"


def test_query_longer_prefix_is_review(fidx):
    # Our name is longer than the license: usually another business.
    m = fidx.lookup("CAPE CORAL TAX & ACCOUNTING", "33990")
    assert not m.auto


def test_single_distinctive_word_needs_zip_area(fidx):
    m = fidx.lookup("UNITED", "33510")
    assert m.doc == "CAB0008"
    m = fidx.lookup("UNITED", "34950")
    assert not m.auto


def test_branch_offices_resolve(fidx):
    assert fidx.lookup("SENTRY MANAGEMENT", "33610").doc == "CAB0011"
    m = fidx.lookup("SENTRY MANAGEMENT", "34950")
    assert m.doc in ("CAB0010", "CAB0011") and m.auto


def test_dba_is_indexed(fidx):
    m = fidx.lookup("HOMETOWN CAM", "33619")
    assert m.doc == "CAB0012" and m.auto


def test_fuzzy_at_licensee_zip_is_auto_elsewhere_review(fidx):
    m = fidx.lookup("MERIDAN MANAGEMENT", "33647")
    assert m.doc == "CAB0013" and m.method == "firm_fuzzy_zip" and m.auto
    m = fidx.lookup("MERIDAN MANAGEMENT", "32801")
    assert m.doc == "CAB0013" and m.method == "fuzzy_review" and not m.auto


def test_generic_only_never_matches(fidx):
    assert fidx.lookup("PROPERTY MANAGEMENT SERVICES", "33607").doc is None


def test_alias():
    idx = FirmIndex.build(FIRM_ROWS, aliases={"ACME HOA PROS": "CAB0010"})
    m = idx.lookup("ACME HOA PROS LLC", "")
    assert (m.doc, m.method, m.auto) == ("CAB0010", "alias", True)
