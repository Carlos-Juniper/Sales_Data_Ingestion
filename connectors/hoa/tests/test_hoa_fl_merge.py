"""Tests for hoa.hoa_fl_merge.resolve — synthetic staging frames, no DB, no real people."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from hoa import hoa_fl_merge as m
from lib.keys import compute_account_key


def sunbiz(doc, name, zip_="33647", seg="association_name", care_of=None, ra=None, officers=()):
    return {"natural_key": doc, "name_raw": name, "name_normalized": name.lower(), "zip5": zip_,
            "segment": seg, "address_line_1": "1 MAIN ST", "city": "TAMPA", "state": "FL",
            "attrs": {"principal": {"zip": zip_}, "mailing": {"addr1": "PO BOX 1", "zip": zip_},
                      "care_of": care_of, "registered_agent": ra or {},
                      "officers": list(officers)}}


def project(key, name, me_num, me_name, zip_="33647", units=100, route=None, seg="condo"):
    return {"natural_key": key, "name_raw": name, "zip5": zip_, "segment": seg,
            "address_line_1": "2 LAKE DR", "city": "TAMPA", "state": "FL", "size_value": units,
            "attrs": {"county": "HILLSBOROUGH", "care_of": route,
                      "managing_entity": {"number": me_num, "name": me_name, "zip": "33602"}}}


def firm(lic, name, zip_="33602", dba=(), status="Current"):
    return {"natural_key": lic, "name_raw": name, "name_normalized": name.lower(), "zip5": zip_,
            "account_type": "management_company", "address_line_1": "1 FIRM WAY", "city": "TAMPA",
            "state": "FL", "attrs": {"dba": list(dba), "primary_status": status}}


def parcel(key, owner, cls, zip_="33647", sqft=43560.0, care_of=None):
    return {"natural_key": key, "name_raw": owner, "zip5": zip_, "segment": cls,
            "address_line_1": None, "city": "TAMPA", "state": None, "size_value": sqft,
            "latitude": 28.1, "longitude": -82.4,
            "attrs": {"county_no": 39, "owner_class": cls, "care_of": care_of,
                      "owner_name_truncated": len(owner) >= 30,
                      "owner": {"name": owner, "zip": zip_},
                      "boundary": {"type": "Polygon", "coordinates": []}}}


def frames(sb=(), pr=(), cam=(), pc=()):
    return (pd.DataFrame(list(sb)), pd.DataFrame(list(pr)), pd.DataFrame(list(cam)),
            pd.DataFrame(list(pc)))


def akey(doc):
    return compute_account_key({"sunbiz_doc": doc}, priority=m.ASSOC_PRIORITY)


def fkey(lic):
    return compute_account_key({"dbpr_cab_license": lic}, priority=m.FIRM_PRIORITY)


FIRM = firm("CAB1", "TEST PROPERTY MANAGEMENT, INC.")


@pytest.fixture
def world():
    sb = [
        sunbiz("N1", "TEST LAKES HOMEOWNERS ASSOCIATION, INC.",
               care_of="TEST PROPERTY MANAGEMENT", officers=[{"title": "PRES", "name": "OFFICER ONE"}],
               ra={"name": "TEST PROPERTY MANAGEMENT, INC.", "kind": "C", "zip": "33602"}),
        sunbiz("N2", "TEST TOWERS CONDOMINIUM ASSOCIATION, INC."),
        sunbiz("N3", "TEST CREEK, INC.", seg="nonprofit_other",
               ra={"name": "TEST PROPERTY MANAGEMENT, INC.", "kind": "C", "zip": "33602"}),
        sunbiz("N4", "TEST CHURCH OF TAMPA, INC.", seg="nonprofit_other",
               ra={"name": "PERSON TWO", "kind": "P"}),
    ]
    pr = [project("condo:1", "TEST TOWERS", "ME1", "TEST TOWERS CONDOMINIUM ASSOCIATION, INC.",
                  units=80, route="C/O TEST PROPERTY MANAGEMENT"),
          project("condo:2", "TEST TOWERS II", "ME1", "TEST TOWERS CONDOMINIUM ASSOCIATION, INC.",
                  units=40),
          project("coop:3", "TEST VILLAGE", "ME9", "TEST VILLAGE COOPERATIVE, INC.", units=12,
                  seg="coop")]
    pc = [parcel("39:A", "TEST LAKES HOMEOWNERS ASSOCIAT", "association", sqft=87120.0),
          parcel("39:B", "TEST LAKES HOMEOWNERS ASSOCIAT", "association", sqft=43560.0),
          parcel("39:C", "TEST MEADOWS HOMEOWNERS ASSOC", "association",
                 care_of="TEST PROPERTY MANAGEMENT"),
          parcel("39:D", "LOT OWNERS OF TEST PLAT", "other"),
          parcel("39:E", "SOUTH TEST CDD", "cdd")]
    return m.resolve(*frames(sb, pr, [FIRM], pc))


def test_accounts_by_type(world):
    acct = world.account.set_index("account_key")
    assert set(acct["vertical"]) == {"hoa"}
    # N1, N2 by name, N3 by firm agent, ME9 DBPR-only, parcel-only TEST MEADOWS, firm CAB1.
    assert (acct["account_type"] == "association").sum() == 5
    assert acct.loc[fkey("CAB1"), "account_type"] == "management_company"
    assert akey("N4") not in acct.index  # not association-named, person agent


def test_dbpr_entity_links_to_f1_and_sums_units(world):
    a = world.account.set_index("account_key").loc[akey("N2")]
    assert a["size_metric"] == 120 and a["size_metric_unit"] == "units"
    ext = json.loads(a["external_keys"])
    assert ext["sunbiz_doc"] == "N2"
    assert ext["dbpr_managing_entity"] == ["ME1"]
    assert ext["dbpr_project"] == ["condo:1", "condo:2"]


def test_unmatched_entity_becomes_own_account(world):
    k = compute_account_key({"dbpr_managing_entity": "ME9"}, priority=m.ASSOC_PRIORITY)
    a = world.account.set_index("account_key").loc[k]
    assert a["confidence"] == m.CONFIDENCE["dbpr_only"]
    assert a["size_metric"] == 12


def test_parcels_roll_up_to_association_acres(world):
    a = world.account.set_index("account_key").loc[akey("N1")]
    assert a["size_metric"] == pytest.approx(3.0) and a["size_metric_unit"] == "acres"
    locs = world.location[world.location["account_key"] == akey("N1")]
    assert len(locs) == 2 and set(locs["site_type"]) == {"common_area_parcel"}
    assert locs["maintained_acres"].tolist() == pytest.approx([2.0, 1.0])
    assert locs["_boundary_geojson"].notna().all()


def test_skipped_owners_get_no_location(world):
    keys = set(world.location["location_name"])
    assert "LOT OWNERS OF TEST PLAT" not in keys and "SOUTH TEST CDD" not in keys
    assert world.stats["parcel_owner_to_f1"]["skip_cdd"] == 1
    assert world.stats["parcel_owner_to_f1"]["skip_lot_owners"] == 1


def test_management_firm_precedence(world):
    loc = world.location.set_index("location_name")
    # F1 C/O for N1's parcels; DBPR route for N2's projects (both projects of the entity).
    assert set(loc.loc[["TEST LAKES HOMEOWNERS ASSOCIAT"], "management_account_key"]) == {fkey("CAB1")}
    assert loc.loc["TEST TOWERS", "management_account_key"] == fkey("CAB1")
    assert loc.loc["TEST TOWERS II", "management_account_key"] == fkey("CAB1")
    # Parcel-only owner falls back to its own tax-roll C/O.
    assert loc.loc["TEST MEADOWS HOMEOWNERS ASSOC", "management_account_key"] == fkey("CAB1")
    assert pd.isna(loc.loc["TEST VILLAGE", "management_account_key"])


def test_recall_rule_2_firm_agent(world):
    assert world.stats["f1_association_by"]["firm_agent"] == 1
    a = world.account.set_index("account_key").loc[akey("N3")]
    assert a["confidence"] == m.CONFIDENCE["firm_agent"]


def test_contacts_officers_and_agent(world):
    c = world.contact[world.contact["account_key"] == akey("N1")]
    assert sorted(c["role"]) == ["PRES", "registered_agent"]
    assert c["contact_key"].is_unique


def test_keys_unique(world):
    assert world.account["account_key"].is_unique
    assert world.location["location_key"].is_unique


def test_recall_rule_1_rejects_name_only_match_for_other_owner():
    sb = [sunbiz("N1", "TEST CREEK, INC.", zip_="32801")]
    pc = [parcel("39:A", "TEST CREEK INC", "other", zip_="33647")]
    res = m.resolve(*frames(sb, [], [FIRM], pc))
    assert res.location.empty  # name-only match refused, and "other" owners get no account
    assert res.stats["parcel_owner_to_f1:other"]["recall_rejected"] == 1


def test_recall_rule_1_accepts_zip_backed_match_for_other_owner():
    sb = [sunbiz("N1", "TEST CREEK, INC.", zip_="33647", seg="nonprofit_other")]
    pc = [parcel("39:A", "TEST CREEK INC", "other", zip_="33647")]
    res = m.resolve(*frames(sb, [], [FIRM], pc))
    assert set(res.location["account_key"]) == {akey("N1")}
    a = res.account.set_index("account_key").loc[akey("N1")]
    assert a["confidence"] == m.CONFIDENCE["parcel"]


def test_summary_prints_no_names(world, capsys):
    m.print_summary(world)
    err = capsys.readouterr().err
    assert "OFFICER ONE" not in err and "PERSON TWO" not in err
    assert "dbpr_entity_to_f1" in err


def test_alias_reaches_firm_named_by_initials(monkeypatch):
    monkeypatch.setattr(m, "FIRM_ALIASES", {"TPMC": "CAB1", "GONE": "CAB404"})
    sb = [sunbiz("N1", "TEST LAKES HOMEOWNERS ASSOCIATION, INC.", care_of="TPMC")]
    res = m.resolve(*frames(sb, [], [FIRM], [parcel("39:A", "TEST LAKES HOMEOWNERS ASSOCIAT",
                                                    "association")]))
    assert set(res.location["management_account_key"]) == {fkey("CAB1")}
    # An alias to a license missing from the CAM file is dropped, not linked.
    assert m._firm_link(m.FirmIndex.build([]), "GONE", None).target is None


def test_recall_rule_2_skips_charities():
    ra = {"name": "TEST PROPERTY MANAGEMENT, INC.", "kind": "C", "zip": "33602"}
    sb = [sunbiz("N1", "TEST GRACE MINISTRIES, INC.", seg="nonprofit_other", ra=ra),
          sunbiz("N2", "TEST YACHT CLUB, INC.", seg="nonprofit_other", ra=ra)]
    res = m.resolve(*frames(sb, [], [FIRM], []))
    keys = set(res.account["account_key"])
    assert akey("N1") not in keys and akey("N2") in keys
    assert res.stats["f1_firm_agent_excluded"]["not_association_name"] == 1


@pytest.mark.parametrize("name,base", [
    ("TEST'S GREEN COMMUNITY ASSOCIATION, INC.", "TESTS GREEN"),
    ("TEST RIDGE MASTER ASSOCIATION, INC.", "TEST RIDGE"),
    ("TEST LAKES PROPERTY OWNERS ASSOCIATION, INC.", "TEST LAKES"),
    ("VILLAGE COMMUNITY ASSOCIATION, INC.", None),        # generic one-word base
    ("THE OAKS OWNERS ASSOCIATION, INC.", None),          # "THE" does not make it specific
    ("TEST GREEN HOMEOWNERS ASSOCIATION, INC.", None),    # an HOA is not a master
    ("TEST TOWERS CONDOMINIUM ASSOCIATION, INC.", None),
])
def test_master_base(name, base):
    assert m.master_base(name) == base


def test_sub_associations_link_to_master_in_same_zip3():
    sb = [sunbiz("N1", "TEST'S GREEN COMMUNITY ASSOCIATION, INC.", zip_="33647"),
          sunbiz("N2", "TEST'S GREEN PARCEL 3 NEIGHBORHOOD ASSOCIATION, INC.", zip_="33624"),
          sunbiz("N3", "WATERFORDE AT TEST'S GREEN HOMEOWNERS ASSOCIATION, INC.", zip_="33647"),
          sunbiz("N4", "PARKSIDE VILLAGE OF TESTS GREEN HOMEOWNERS ASSOCIATION, INC.", zip_="33637"),
          # Same name on another coast: no shared ZIP3, no link.
          sunbiz("N5", "TEST'S GREEN PARCEL 9 HOMEOWNERS ASSOCIATION, INC.", zip_="32780"),
          # Base mentioned but not as a sub (no AT/OF/IN, no PARCEL-style marker).
          sunbiz("N6", "TEST'S GREENWAY HOMEOWNERS ASSOCIATION, INC.", zip_="33647")]
    res = m.resolve(*frames(sb, [], [FIRM], []))
    acct = res.account.set_index("account_key")["parent_account_key"]
    for doc in ("N2", "N3", "N4"):
        assert acct[akey(doc)] == akey("N1")
    for doc in ("N1", "N5", "N6"):
        assert pd.isna(acct[akey(doc)])
    assert res.stats["sub_association_parent"]["linked"] == 3
    assert res.stats["sub_association_parent"]["no_zip_master"] == 1


def test_two_masters_in_one_zip3_leave_sub_unlinked():
    sb = [sunbiz("N1", "TEST GREEN COMMUNITY ASSOCIATION, INC."),
          sunbiz("N2", "TEST GREEN MASTER ASSOCIATION, INC."),
          sunbiz("N3", "TEST GREEN PARCEL 3 HOMEOWNERS ASSOCIATION, INC.")]
    res = m.resolve(*frames(sb, [], [FIRM], []))
    assert res.account["parent_account_key"].isna().all()
    assert res.stats["sub_association_parent"]["ambiguous"] == 1


def test_one_word_brand_care_of_reaches_its_license():
    brand = firm("CAB7", "TESTBRAND LLC", zip_="75380")  # out-of-state head office
    sb = [sunbiz("N1", "TEST LAKES HOMEOWNERS ASSOCIATION, INC.", care_of="TESTBRAND"),
          sunbiz("N2", "TEST HILLS HOMEOWNERS ASSOCIATION, INC.", care_of="PERSON THREE")]
    res = m.resolve(*frames(sb, [], [FIRM, brand], []))
    idx = m.FirmIndex.build([("CAB7", "TESTBRAND LLC", "", "75380")])
    assert m._firm_link(idx, "TESTBRAND", "33647").target == "CAB7"
    assert m._firm_link(idx, "PERSON THREE", "33647").method == "not_firm"
    assert res.stats["f1_care_of_to_cab"]


def test_registered_agent_firm_is_last_resort_manager():
    ra = {"name": "TEST PROPERTY MANAGEMENT, INC.", "kind": "C", "zip": "33602"}
    other = firm("CAB2", "OTHER TEST MANAGEMENT, INC.")
    sb = [sunbiz("N1", "TEST LAKES HOMEOWNERS ASSOCIATION, INC.", ra=ra),
          sunbiz("N2", "TEST HILLS HOMEOWNERS ASSOCIATION, INC.", ra=ra)]
    pc = [parcel("39:A", "TEST LAKES HOMEOWNERS ASSOCIAT", "association"),
          parcel("39:B", "TEST HILLS HOMEOWNERS ASSOCIAT", "association",
                 care_of="OTHER TEST MANAGEMENT")]
    res = m.resolve(*frames(sb, [], [FIRM, other], pc))
    loc = res.location.set_index("location_name")["management_account_key"]
    assert loc["TEST LAKES HOMEOWNERS ASSOCIAT"] == fkey("CAB1")   # agent fills the gap
    assert loc["TEST HILLS HOMEOWNERS ASSOCIAT"] == fkey("CAB2")   # the tax-roll C/O wins
    assert res.stats["location_firm_from"] == {"f1_agent": 1, "parcel_care_of": 1}


def test_unnamed_managing_entity_takes_project_name():
    pr = [project("condo:9", "TEST HARBOR", "ME7", None, units=10)]
    res = m.resolve(*frames([], pr, [FIRM], []))
    k = compute_account_key({"dbpr_managing_entity": "ME7"}, priority=m.ASSOC_PRIORITY)
    assert res.account.set_index("account_key").loc[k, "legal_name"] == "TEST HARBOR"


# ---------------------------------------------------------------- generic DBA / office address

PHP = firm("CAB5", "TEST HOLDINGS LLC", zip_="33716", dba=["CONDOMINIUM ASSOCIATES"])


def test_generic_dba_care_of_reaches_holder_at_its_zip():
    idx = m.FirmIndex.build([("CAB5", "TEST HOLDINGS LLC", "CONDOMINIUM ASSOCIATES", "33716")])
    lk = m._firm_link(idx, "CONDOMINIUM ASSOCIATES", "33716")
    assert (lk.target, lk.method) == ("CAB5", "full_zip")
    assert m._firm_link(idx, "CONDOMINIUM ASSOCIATES", "33647").method == "not_firm"


def test_f1_generic_dba_sets_location_firm():
    sb = [sunbiz("N1", "TEST LAKES HOMEOWNERS ASSOCIATION, INC.", care_of="CONDOMINIUM ASSOCIATES")]
    sb[0]["attrs"]["mailing"] = {"addr1": "C/O CONDOMINIUM ASSOCIATES", "addr2": "570 TEST PKWY",
                                 "zip": "33716"}
    pc = [parcel("39:A", "TEST LAKES HOMEOWNERS ASSOCIAT", "association")]
    res = m.resolve(*frames(sb, [], [FIRM, PHP], pc))
    assert res.location.iloc[0]["management_account_key"] == fkey("CAB5")


def office(lic, name, street, zip_):
    f = firm(lic, name, zip_=zip_)
    f["address_line_1"] = street
    return f


OFFICE = office("CAB8", "TEST REALTY GROUP, INC.", "2906 TEST LAKE BLVD", "33614")


@pytest.mark.parametrize("route,lic", [
    (None, "CAB8"),
    ("PERSON FOUR", "CAB8"),          # a board member at the firm's office
    ("TEST HOLDINGS CO", None),       # the address belongs to the named company
])
def test_address_firm_respects_care_of(route, lic):
    adx = m.FirmAddressIndex.build([("CAB8", "2906 TEST LAKE BLVD", "33614")])
    assert m._address_firm(adx, route, "2906 Test Lake Blvd.", "33614") == lic


def test_mail_street_skips_care_of_line():
    assert m._mail_street({"addr1": "C/O TEST MGMT", "addr2": "1 MAIN ST"}) == "1 MAIN ST"
    assert m._mail_street({"addr1": "1 MAIN ST"}) == "1 MAIN ST"
    assert m._mail_street(None) is None


def test_f1_mailing_address_at_firm_office_is_last_resort():
    sb = [sunbiz("N1", "TEST LAKES HOMEOWNERS ASSOCIATION, INC."),
          sunbiz("N2", "TEST HILLS HOMEOWNERS ASSOCIATION, INC.", care_of="TEST PROPERTY MANAGEMENT")]
    for s_ in sb:
        s_["attrs"]["mailing"] = {"addr1": "2906 Test Lake Blvd", "zip": "33614"}
    pc = [parcel("39:A", "TEST LAKES HOMEOWNERS ASSOCIAT", "association"),
          parcel("39:B", "TEST HILLS HOMEOWNERS ASSOCIAT", "association")]
    res = m.resolve(*frames(sb, [], [FIRM, OFFICE], pc))
    loc = res.location.set_index("location_name")["management_account_key"]
    assert loc["TEST LAKES HOMEOWNERS ASSOCIAT"] == fkey("CAB8")   # address only
    assert loc["TEST HILLS HOMEOWNERS ASSOCIAT"] == fkey("CAB1")   # the named firm wins
    assert res.stats["location_firm_from"] == {"address": 1, "newer_source": 1}


def test_parcel_owner_address_at_firm_office():
    pc = [parcel("39:A", "TEST LAKES HOMEOWNERS ASSOCIAT", "association"),
          parcel("39:B", "TEST HILLS HOMEOWNERS ASSOCIAT", "association", care_of="TEST HOLDINGS CO")]
    for p_ in pc:
        p_["attrs"]["owner"].update(addr1="2906 TEST LAKE BLVD", zip="33614")
    res = m.resolve(*frames([], [], [FIRM, OFFICE], pc))
    loc = res.location.set_index("location_name")["management_account_key"]
    assert loc["TEST LAKES HOMEOWNERS ASSOCIAT"] == fkey("CAB8")
    assert pd.isna(loc["TEST HILLS HOMEOWNERS ASSOCIAT"])          # C/O names another company


def test_dbpr_managing_entity_address_at_firm_office():
    pr = [project("condo:1", "TEST TOWERS", "ME1", "TEST TOWERS CONDOMINIUM ASSOCIATION, INC.")]
    pr[0]["attrs"]["managing_entity"].update(street="2906 TEST LAKE BOULEVARD", zip="33614")
    res = m.resolve(*frames([], pr, [FIRM, OFFICE], []))
    assert res.location.iloc[0]["management_account_key"] == fkey("CAB8")
    assert res.stats["dbpr_address_to_cab"] == {"address": 1}


def test_castle_group_alias_targets_a_real_license():
    assert m.FIRM_ALIASES["CASTLE GROUP"].startswith("CAB")
