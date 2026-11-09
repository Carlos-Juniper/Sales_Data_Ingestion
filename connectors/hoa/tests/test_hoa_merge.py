"""
Tests for hoa_merge.py — HOA vertical merge into staging.resolved_*.

No database: the builders are pure DataFrame -> DataFrame functions, and
run_pipeline is exercised with load_sources and replace_and_upsert patched.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from hoa import hoa_merge as mod
from lib.keys import compute_account_key


def _row(**overrides) -> dict:
    base = {
        "source_id": "tx_trec_hoa",
        "natural_key": "120420",
        "account_type": "association",
        "name_raw": "Canyon Corner Property Owners Association, Inc.",
        "name_normalized": "CANYON CORNER PROPERTY OWNERS",
        "city": "CANYON LAKE",
        "state": "TX",
        "zip5": "78133",
        "county_fips": "COMAL",
        "latitude": None,
        "longitude": None,
        "trec_type": "property_owners_association",
        "certificate_id": "10262-14666",
        "pdf_confidence": 0.9,
        "mgmt_name": "Goodwin & Company",
        "mgmt_mailing_address": "PO Box 203310, Austin, TX",
        "mgmt_phone_normalized": "8552896007",
        "mgmt_email": "Info@goodwin-co.com",
        "hoa_mailing_address": "PO Box 203310, Austin, TX 78720",
        "pdf_website": "www.canyoncornerpoa.org",
        "gmaps_phone": None,
        "gmaps_website": None,
        "gmaps_email": None,
    }
    base.update(overrides)
    return base


def _df(*rows) -> pd.DataFrame:
    return pd.DataFrame(list(rows))


# ---------------------------------------------------------------- cleaning


class TestCleanMgmtName:
    @pytest.mark.parametrize("raw,expected", [
        ("Goodwin & Company", "Goodwin & Company"),
        ("Name: Proper HOA Management, LLC", "Proper HOA Management, LLC"),
        ("information of the designated representative of the Association is:, RealManage, LLC",
         "RealManage, LLC"),
        ("association or its designated representative., C.I.A. Services, Inc.", "C.I.A. Services, Inc"),
        ("the Association is:, Preferred Association Management Company",
         "Preferred Association Management Company"),
        ("Mike Hughes c/o Acme HOA", "Mike Hughes"),
    ])
    def test_extracts_value_after_label(self, raw, expected):
        assert mod.clean_mgmt_name(raw) == expected

    @pytest.mark.parametrize("raw", [
        None, float("nan"), "", "is:",
        "Name and Mailing Address of the Person or Entity Managing the Association",
        "Manager of the Association. The telephone number for the manager of the Association",
        "1800 Augusta Drive, Suite 200",
        "Houston, Texas",
        "x" * 120,
    ])
    def test_rejects_labels_addresses_and_noise(self, raw):
        assert mod.clean_mgmt_name(raw) is None


class TestCleanContactFields:
    def test_email(self):
        assert mod.clean_email(" Info@Goodwin-Co.com ") == "info@goodwin-co.com"
        assert mod.clean_email("email.com") is None
        assert mod.clean_email(None) is None

    def test_phone(self):
        assert mod.clean_phone("(855) 289-6007") == "8552896007"
        assert mod.clean_phone("18552896007") == "8552896007"
        assert mod.clean_phone("289-6007") is None

    def test_website_skips_generic_hosts(self):
        assert mod.clean_website("email.com") is None
        assert mod.clean_website("https://app.townsq.io/login") is None
        assert mod.clean_website("gmail.com | https://goodwin-co.com/") == "https://goodwin-co.com/"
        assert mod.clean_website("www.homesteadsection4.org") == "www.homesteadsection4.org"


# ---------------------------------------------------------------- builders


class TestBuildResolvedAccount:
    def test_keyed_on_trec_id(self):
        acct = mod.build_resolved_account(_df(_row()))
        assert len(acct) == 1
        r = acct.iloc[0]
        assert r.account_key == compute_account_key(
            {"trec_assoc_id": "120420"}, priority=("trec_assoc_id",)
        )
        assert r.vertical == "hoa"
        assert json.loads(r.external_keys) == {
            "trec_assoc_id": "120420", "trec_certificate_id": "10262-14666",
        }
        assert json.loads(r.mailing_address)["address_raw"].startswith("PO Box 203310")
        assert r.website == "www.canyoncornerpoa.org"

    def test_low_confidence_certificate_text_ignored(self):
        acct = mod.build_resolved_account(_df(_row(pdf_confidence=0.4)))
        r = acct.iloc[0]
        assert "address_raw" not in json.loads(r.mailing_address)
        assert r.website is None

    def test_places_only_fills_gaps(self):
        acct = mod.build_resolved_account(_df(_row(
            gmaps_phone="(512) 555-0100", gmaps_website="https://places.example.org",
            gmaps_email="board@example.org",
        )))
        r = acct.iloc[0]
        assert r.phone == "5125550100"
        assert r.email == "board@example.org"
        # Certificate website outranks Places.
        assert r.website == "www.canyoncornerpoa.org"


class TestBuildResolvedLocation:
    def test_one_location_per_association(self):
        hoas = _df(_row(), _row(natural_key="196341", zip5="78133"))
        acct = mod.build_resolved_account(hoas)
        loc = mod.build_resolved_location(hoas, acct)
        assert len(loc) == 2
        assert loc.location_key.nunique() == 2
        site = json.loads(loc.iloc[0].site_address)
        assert site == {"city": "CANYON LAKE", "state": "TX", "zip5": "78133", "county": "COMAL"}
        assert loc.iloc[0].site_type == "property_owners_association"
        assert loc.iloc[0]._latitude is None


class TestBuildResolvedContact:
    def test_trusted_certificate_yields_mgmt_contact(self):
        hoas = _df(_row())
        con = mod.build_resolved_contact(hoas, mod.build_resolved_account(hoas))
        assert len(con) == 1
        r = con.iloc[0]
        assert r.role == "management_company"
        assert r.full_name == "Goodwin & Company"
        assert r.phone == "8552896007"
        assert r.email == "info@goodwin-co.com"
        assert r.source_id == "tx_trec_pdf_enrich"

    def test_skips_untrusted_or_empty(self):
        hoas = _df(
            _row(natural_key="1", pdf_confidence=0.5),
            _row(natural_key="2", mgmt_phone_normalized=None, mgmt_email="not-an-email"),
            _row(natural_key="3", pdf_confidence=None),
        )
        con = mod.build_resolved_contact(hoas, mod.build_resolved_account(hoas))
        assert con.empty

    def test_label_name_kept_as_null_name(self):
        hoas = _df(_row(mgmt_name="Name and Mailing Address of the Person Managing"))
        con = mod.build_resolved_contact(hoas, mod.build_resolved_account(hoas))
        assert len(con) == 1
        assert con.iloc[0].full_name is None


# ---------------------------------------------------------------- driver


class TestRunPipeline:
    def test_dry_run_does_not_write(self):
        with patch.object(mod, "load_sources", return_value=_df(_row())), \
             patch.object(mod, "replace_and_upsert") as writer:
            out = mod.run_pipeline(engine=None, dry_run=True)
        writer.assert_not_called()
        assert out == {"source_rows": 1, "account": 1, "location": 1, "contact": 1}

    def test_writes_scoped_to_hoa(self):
        with patch.object(mod, "load_sources", return_value=_df(_row())), \
             patch.object(mod, "replace_and_upsert", return_value=(1, 1, 1)) as writer:
            mod.run_pipeline(engine=None)
        assert writer.call_args.kwargs["vertical"] == "hoa"

    def test_empty_staging(self):
        with patch.object(mod, "load_sources", return_value=pd.DataFrame()), \
             patch.object(mod, "replace_and_upsert") as writer:
            out = mod.run_pipeline(engine=None)
        writer.assert_not_called()
        assert out["account"] == 0
