"""
Tests for hoa_gmaps_enrich.py — Google Places supplemental HOA enrichment.

Strategy
--------
All network calls (GooglePlacesClient, extract_emails_from_website) are
mocked — no HTTP traffic occurs. time.sleep is never reached because the
client/crawler are replaced outright rather than exercised through their
real HTTP paths.

Three classes cover the three public functions, following
deathcare/irs_990_enrich.py's test layout exactly:

  TestEnrichOne     — per-row lookup logic (ok, not_found, error, email crawl)
  TestEnrich        — batch DataFrame enrichment (eligibility mask, columns)
  TestPrintSummary  — smoke test: does not raise; produces stderr output

A fourth class, TestEnrichConcurrent, exercises the ThreadPoolExecutor branch
the same way TestEnrichConcurrent does in test_irs_990_enrich.py.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import hoa_gmaps_enrich as mod

# ---------------------------------------------------------------- helpers


def _make_canonical_row(**overrides) -> dict:
    """
    Return a dict representing one canonical tx_trec_hoa row.

    Mirrors the shape produced by tx_trec_hoa.to_canonical(). Override
    individual column values with keyword arguments.
    """
    defaults = {
        "source_id": "tx_trec_hoa",
        "natural_key": "123456",
        "vertical": "hoa",
        "account_type": "association",
        "legal_name": "Sunset Ridge Homeowners Association",
        "name_normalized": "SUNSET RIDGE HOMEOWNERS",
        "association_type": "homeowners_association",
        "site_city": "Houston",
        "site_state": "TX",
        "site_zip": "77001",
        "county_primary": "HARRIS",
        "is_multi_county": False,
        "trec_assoc_id": "123456",
        "trec_certificate_id": "51-253",
        "certificate_url": "https://hoa.texas.gov/certificates/123456/51-253/mc/x.pdf",
        "site_street": None,
        "mailing_address": None,
        "phone": None,
        "email": None,
        "managing_agent": None,
        "contact_status": "pending_pdf",
        "geocode_status": "zip_centroid_only",
    }
    defaults.update(overrides)
    return defaults


def _make_canonical_df(*rows: dict) -> pd.DataFrame:
    if not rows:
        rows = (_make_canonical_row(),)
    return pd.DataFrame(list(rows))


def _mock_places_client(lookup_result: dict | None = None, side_effect=None) -> MagicMock:
    client = MagicMock()
    if side_effect is not None:
        client.lookup_business.side_effect = side_effect
    else:
        client.lookup_business.return_value = lookup_result or {
            "phone": "+18325551234",
            "website": "https://sunsetridgehoa.example.com",
            "place_id": "abc123",
            "found": True,
        }
    return client


# ===========================================================================
# Tests: enrich_one()
# ===========================================================================


class TestEnrichOne:

    def _row(self, **overrides):
        row = _make_canonical_row(**overrides)
        return row

    def test_ok_status_when_phone_and_website_found(self):
        client = _mock_places_client()
        session = MagicMock()
        robots = MagicMock()

        with patch("hoa_gmaps_enrich.extract_emails_from_website", return_value=[]):
            result = mod.enrich_one(self._row(), client, session, robots)

        assert result["enrich_status"] == "ok"

    def test_phone_and_website_populated_on_ok(self):
        client = _mock_places_client()
        session = MagicMock()
        robots = MagicMock()

        with patch("hoa_gmaps_enrich.extract_emails_from_website", return_value=[]):
            result = mod.enrich_one(self._row(), client, session, robots)

        assert result["phone"] == "+18325551234"
        assert result["website"] == "https://sunsetridgehoa.example.com"
        assert result["maps_place_id"] == "abc123"

    def test_natural_key_preserved(self):
        client = _mock_places_client()
        session = MagicMock()
        robots = MagicMock()

        with patch("hoa_gmaps_enrich.extract_emails_from_website", return_value=[]):
            result = mod.enrich_one(self._row(natural_key="999999"), client, session, robots)

        assert result["natural_key"] == "999999"

    def test_not_found_when_places_returns_zero_results(self):
        client = _mock_places_client(lookup_result={
            "phone": None, "website": None, "place_id": None, "found": False,
        })
        session = MagicMock()
        robots = MagicMock()

        result = mod.enrich_one(self._row(), client, session, robots)

        assert result["enrich_status"] == "not_found"
        assert result["phone"] is None
        assert result["website"] is None

    def test_not_found_when_matched_place_has_no_phone_or_website(self):
        client = _mock_places_client(lookup_result={
            "phone": None, "website": None, "place_id": "abc123", "found": True,
        })
        session = MagicMock()
        robots = MagicMock()

        result = mod.enrich_one(self._row(), client, session, robots)

        assert result["enrich_status"] == "not_found"

    def test_error_status_when_lookup_raises(self):
        client = _mock_places_client(side_effect=RuntimeError("Places API 401"))
        session = MagicMock()
        robots = MagicMock()

        result = mod.enrich_one(self._row(), client, session, robots)

        assert result["enrich_status"] == "error"
        assert "RuntimeError" in result["error_detail"]
        assert "Places API 401" in result["error_detail"]

    def test_error_status_captures_exception_message(self):
        client = _mock_places_client(side_effect=ValueError("bad response shape"))
        session = MagicMock()
        robots = MagicMock()

        result = mod.enrich_one(self._row(), client, session, robots)

        assert result["error_detail"] == "ValueError: bad response shape"

    def test_email_crawl_invoked_when_website_found(self):
        client = _mock_places_client()
        session = MagicMock()
        robots = MagicMock()

        with patch("hoa_gmaps_enrich.extract_emails_from_website", return_value=["board@example.com"]) as mock_crawl:
            result = mod.enrich_one(self._row(), client, session, robots)

        mock_crawl.assert_called_once_with(
            "https://sunsetridgehoa.example.com", session=session, robots=robots,
        )
        assert result["contact_email"] == "board@example.com"

    def test_email_crawl_skipped_when_no_website(self):
        client = _mock_places_client(lookup_result={
            "phone": "+18325551234", "website": None, "place_id": "abc123", "found": True,
        })
        session = MagicMock()
        robots = MagicMock()

        with patch("hoa_gmaps_enrich.extract_emails_from_website") as mock_crawl:
            result = mod.enrich_one(self._row(), client, session, robots)

        mock_crawl.assert_not_called()
        assert result["contact_email"] is None
        assert result["enrich_status"] == "ok"

    def test_email_crawl_exception_does_not_fail_the_row(self):
        """A broken website must not turn an otherwise-successful match into an error."""
        client = _mock_places_client()
        session = MagicMock()
        robots = MagicMock()

        with patch("hoa_gmaps_enrich.extract_emails_from_website", side_effect=RuntimeError("dns failure")):
            result = mod.enrich_one(self._row(), client, session, robots)

        assert result["enrich_status"] == "ok"
        assert result["contact_email"] is None

    def test_email_crawl_no_emails_found_leaves_contact_email_none(self):
        client = _mock_places_client()
        session = MagicMock()
        robots = MagicMock()

        with patch("hoa_gmaps_enrich.extract_emails_from_website", return_value=[]):
            result = mod.enrich_one(self._row(), client, session, robots)

        assert result["contact_email"] is None

    def test_lookup_called_with_name_city_state_zip(self):
        client = _mock_places_client()
        session = MagicMock()
        robots = MagicMock()

        with patch("hoa_gmaps_enrich.extract_emails_from_website", return_value=[]):
            mod.enrich_one(self._row(), client, session, robots)

        client.lookup_business.assert_called_once_with(
            name="Sunset Ridge Homeowners Association",
            city="Houston",
            state="TX",
            zip_code="77001",
        )


# ===========================================================================
# Tests: enrich()
# ===========================================================================


class TestEnrich:

    # -- filtering -----------------------------------------------------------

    def test_skips_rows_where_contact_status_is_not_pending_pdf(self):
        df = _make_canonical_df(_make_canonical_row(contact_status="active"))

        with patch("hoa_gmaps_enrich.GooglePlacesClient") as mock_client_cls:
            out = mod.enrich(df, api_key="fake-key", workers=1)

        mock_client_cls.return_value.lookup_business.assert_not_called()
        assert out["enrich_status"].iloc[0] == "skipped"

    def test_zero_eligible_rows_returns_all_skipped(self):
        df = _make_canonical_df(
            _make_canonical_row(natural_key="1", contact_status="active"),
            _make_canonical_row(natural_key="2", contact_status="active"),
        )

        out = mod.enrich(df, api_key="fake-key", workers=1)

        assert (out["enrich_status"] == "skipped").all()

    def test_mixed_contact_status_only_enriches_pending_pdf_rows(self):
        df = _make_canonical_df(
            _make_canonical_row(natural_key="1", contact_status="pending_pdf"),
            _make_canonical_row(natural_key="2", contact_status="active"),
        )
        client = _mock_places_client()

        with patch("hoa_gmaps_enrich.GooglePlacesClient", return_value=client):
            with patch("hoa_gmaps_enrich.extract_emails_from_website", return_value=[]):
                out = mod.enrich(df, api_key="fake-key", workers=1)

        assert (out["enrich_status"] == "ok").sum() == 1
        assert (out["enrich_status"] == "skipped").sum() == 1

    # -- new columns are added -----------------------------------------------

    def test_new_columns_present_after_enrich(self):
        df = _make_canonical_df()
        client = _mock_places_client()

        with patch("hoa_gmaps_enrich.GooglePlacesClient", return_value=client):
            with patch("hoa_gmaps_enrich.extract_emails_from_website", return_value=[]):
                out = mod.enrich(df, api_key="fake-key", workers=1)

        for col in (
            "gmaps_phone", "gmaps_website", "gmaps_place_id",
            "gmaps_contact_email", "enrich_status", "error_detail",
        ):
            assert col in out.columns

    def test_original_columns_preserved(self):
        df = _make_canonical_df()
        original_cols = set(df.columns)
        client = _mock_places_client()

        with patch("hoa_gmaps_enrich.GooglePlacesClient", return_value=client):
            with patch("hoa_gmaps_enrich.extract_emails_from_website", return_value=[]):
                out = mod.enrich(df, api_key="fake-key", workers=1)

        assert original_cols.issubset(set(out.columns))

    def test_row_count_preserved(self):
        df = _make_canonical_df(
            _make_canonical_row(natural_key="1"),
            _make_canonical_row(natural_key="2", contact_status="active"),
        )
        client = _mock_places_client()

        with patch("hoa_gmaps_enrich.GooglePlacesClient", return_value=client):
            with patch("hoa_gmaps_enrich.extract_emails_from_website", return_value=[]):
                out = mod.enrich(df, api_key="fake-key", workers=1)

        assert len(out) == 2

    def test_gmaps_phone_populated_for_ok_row(self):
        df = _make_canonical_df()
        client = _mock_places_client()

        with patch("hoa_gmaps_enrich.GooglePlacesClient", return_value=client):
            with patch("hoa_gmaps_enrich.extract_emails_from_website", return_value=[]):
                out = mod.enrich(df, api_key="fake-key", workers=1)

        assert out["gmaps_phone"].iloc[0] == "+18325551234"

    def test_client_constructed_with_api_key_and_rate_pause(self):
        df = _make_canonical_df()

        with patch("hoa_gmaps_enrich.GooglePlacesClient") as mock_client_cls:
            mock_client_cls.return_value.lookup_business.return_value = {
                "phone": None, "website": None, "place_id": None, "found": False,
            }
            mod.enrich(df, api_key="my-secret-key", workers=1, rate_pause=0.5)

        _, kwargs = mock_client_cls.call_args
        assert mock_client_cls.call_args[0][0] == "my-secret-key"
        assert kwargs["rate_pause"] == 0.5


# ===========================================================================
# Tests: enrich() — pdf_contact_df survivorship gate
# ===========================================================================


class TestEnrichPdfContactGate:
    """
    The OCR pass (tx_trec_pdf_enrich.py) outranks Places per plan §5.3.
    pdf_contact_df, when given, must suppress a Places lookup for any
    natural_key it already has a real mgmt_phone/mgmt_email for.
    """

    def _pdf_contact_df(self, **overrides) -> pd.DataFrame:
        row = {"natural_key": "123456", "mgmt_phone": "8325551234", "mgmt_email": None}
        row.update(overrides)
        return pd.DataFrame([row])

    def test_skips_row_already_covered_by_pdf_phone(self):
        df = _make_canonical_df()
        pdf_df = self._pdf_contact_df(mgmt_phone="8325551234", mgmt_email=None)

        with patch("hoa_gmaps_enrich.GooglePlacesClient") as mock_client_cls:
            out = mod.enrich(df, api_key="fake-key", workers=1, pdf_contact_df=pdf_df)

        mock_client_cls.return_value.lookup_business.assert_not_called()
        assert out["enrich_status"].iloc[0] == "skipped"

    def test_skips_row_already_covered_by_pdf_email(self):
        df = _make_canonical_df()
        pdf_df = self._pdf_contact_df(mgmt_phone=None, mgmt_email="board@sunsetridgehoa.example.com")

        with patch("hoa_gmaps_enrich.GooglePlacesClient") as mock_client_cls:
            out = mod.enrich(df, api_key="fake-key", workers=1, pdf_contact_df=pdf_df)

        mock_client_cls.return_value.lookup_business.assert_not_called()
        assert out["enrich_status"].iloc[0] == "skipped"

    def test_enriches_row_absent_from_pdf_contact_df(self):
        df = _make_canonical_df()
        pdf_df = self._pdf_contact_df(natural_key="999999")  # different association
        client = _mock_places_client()

        with patch("hoa_gmaps_enrich.GooglePlacesClient", return_value=client):
            with patch("hoa_gmaps_enrich.extract_emails_from_website", return_value=[]):
                out = mod.enrich(df, api_key="fake-key", workers=1, pdf_contact_df=pdf_df)

        assert out["enrich_status"].iloc[0] == "ok"

    def test_enriches_row_present_in_pdf_contact_df_but_empty(self):
        # OCR ran and produced a row, but neither phone nor email — still eligible.
        df = _make_canonical_df()
        pdf_df = self._pdf_contact_df(mgmt_phone="", mgmt_email=None)
        client = _mock_places_client()

        with patch("hoa_gmaps_enrich.GooglePlacesClient", return_value=client):
            with patch("hoa_gmaps_enrich.extract_emails_from_website", return_value=[]):
                out = mod.enrich(df, api_key="fake-key", workers=1, pdf_contact_df=pdf_df)

        assert out["enrich_status"].iloc[0] == "ok"

    def test_none_pdf_contact_df_applies_no_gate(self):
        df = _make_canonical_df()
        client = _mock_places_client()

        with patch("hoa_gmaps_enrich.GooglePlacesClient", return_value=client):
            with patch("hoa_gmaps_enrich.extract_emails_from_website", return_value=[]):
                out = mod.enrich(df, api_key="fake-key", workers=1, pdf_contact_df=None)

        assert out["enrich_status"].iloc[0] == "ok"

    def test_contact_status_gate_still_applies_alongside_pdf_gate(self):
        # Not pending_pdf AND not covered by OCR — still skipped on contact_status alone.
        df = _make_canonical_df(_make_canonical_row(contact_status="active"))
        pdf_df = self._pdf_contact_df(natural_key="999999")

        with patch("hoa_gmaps_enrich.GooglePlacesClient") as mock_client_cls:
            out = mod.enrich(df, api_key="fake-key", workers=1, pdf_contact_df=pdf_df)

        mock_client_cls.return_value.lookup_business.assert_not_called()
        assert out["enrich_status"].iloc[0] == "skipped"

    def test_mixed_batch_only_uncovered_pending_rows_enriched(self):
        df = _make_canonical_df(
            _make_canonical_row(natural_key="1", contact_status="pending_pdf"),
            _make_canonical_row(natural_key="2", contact_status="pending_pdf"),
            _make_canonical_row(natural_key="3", contact_status="active"),
        )
        pdf_df = pd.DataFrame([
            {"natural_key": "1", "mgmt_phone": "8325551234", "mgmt_email": None},
            {"natural_key": "2", "mgmt_phone": None, "mgmt_email": None},
        ])
        client = _mock_places_client()

        with patch("hoa_gmaps_enrich.GooglePlacesClient", return_value=client):
            with patch("hoa_gmaps_enrich.extract_emails_from_website", return_value=[]):
                out = mod.enrich(df, api_key="fake-key", workers=1, pdf_contact_df=pdf_df)

        by_key = out.set_index("natural_key")["enrich_status"]
        assert by_key["1"] == "skipped"  # OCR already covered it
        assert by_key["2"] == "ok"       # pending, OCR row present but empty
        assert by_key["3"] == "skipped"  # not pending_pdf at all


# ===========================================================================
# Tests: enrich() — concurrent (workers > 1) path
# ===========================================================================


class TestEnrichConcurrent:
    """
    Mirrors TestEnrichConcurrent in test_irs_990_enrich.py: futures complete
    in arbitrary order, but each result must land on the row whose
    natural_key triggered it.
    """

    _KEY_A, _KEY_B, _KEY_C, _KEY_D = "1001", "1002", "1003", "1004"

    _RESULTS_BY_KEY = {
        _KEY_A: {"phone": "1110001", "website": "https://a.example.com", "place_id": "pa", "found": True},
        _KEY_B: {"phone": "1110002", "website": "https://b.example.com", "place_id": "pb", "found": True},
        _KEY_C: {"phone": None, "website": None, "place_id": None, "found": False},
        _KEY_D: {"phone": "1110004", "website": None, "place_id": "pd", "found": True},
    }

    @pytest.fixture()
    def enriched_df(self) -> pd.DataFrame:
        df = _make_canonical_df(
            _make_canonical_row(natural_key=self._KEY_A),
            _make_canonical_row(natural_key=self._KEY_B),
            _make_canonical_row(natural_key=self._KEY_C),
            _make_canonical_row(natural_key=self._KEY_D),
        )

        client = MagicMock()

        def _lookup_side_effect(name, city, state, zip_code=""):
            # zip_code doubles as the natural_key carrier in this fixture so
            # each thread's response is deterministic per row.
            return self._RESULTS_BY_KEY[zip_code]

        client.lookup_business.side_effect = _lookup_side_effect

        # Encode natural_key into site_zip so the mocked lookup can dispatch
        # deterministically per row without needing access to natural_key.
        df["site_zip"] = df["natural_key"]

        with patch("hoa_gmaps_enrich.GooglePlacesClient", return_value=client):
            with patch("hoa_gmaps_enrich.extract_emails_from_website", return_value=[]):
                out = mod.enrich(df, api_key="fake-key", workers=2)

        return out

    def test_row_count_unchanged_after_concurrent_enrich(self, enriched_df):
        assert len(enriched_df) == 4

    def test_phone_matches_per_natural_key(self, enriched_df):
        assert enriched_df.loc[enriched_df["natural_key"] == self._KEY_A, "gmaps_phone"].iloc[0] == "1110001"
        assert enriched_df.loc[enriched_df["natural_key"] == self._KEY_B, "gmaps_phone"].iloc[0] == "1110002"
        assert enriched_df.loc[enriched_df["natural_key"] == self._KEY_D, "gmaps_phone"].iloc[0] == "1110004"

    def test_not_found_status_for_row_with_no_places_result(self, enriched_df):
        assert enriched_df.loc[enriched_df["natural_key"] == self._KEY_C, "enrich_status"].iloc[0] == "not_found"

    def test_ok_status_for_rows_with_results(self, enriched_df):
        for key in (self._KEY_A, self._KEY_B, self._KEY_D):
            assert enriched_df.loc[enriched_df["natural_key"] == key, "enrich_status"].iloc[0] == "ok"

    def test_original_columns_preserved_after_concurrent_enrich(self, enriched_df):
        expected_original_cols = set(_make_canonical_row().keys())
        assert expected_original_cols.issubset(set(enriched_df.columns))


# ===========================================================================
# Tests: print_summary()
# ===========================================================================


class TestPrintSummary:

    def _enriched_df(self) -> pd.DataFrame:
        rows = [
            _make_canonical_row(natural_key="1"),
            _make_canonical_row(natural_key="2", contact_status="active"),
        ]
        df = pd.DataFrame(rows)
        df["gmaps_phone"] = [None, None]
        df["gmaps_website"] = [None, None]
        df["gmaps_place_id"] = [None, None]
        df["gmaps_contact_email"] = [None, None]
        df["enrich_status"] = ["ok", "skipped"]
        df["error_detail"] = [None, None]
        return df

    def test_does_not_raise_on_well_formed_input(self):
        df = self._enriched_df()
        mod.print_summary(df)

    def test_stderr_contains_correct_counts_per_status(self, capsys):
        rows = [
            _make_canonical_row(natural_key="1"),
            _make_canonical_row(natural_key="2"),
            _make_canonical_row(natural_key="3"),
            _make_canonical_row(natural_key="4", contact_status="active"),
        ]
        df = pd.DataFrame(rows)
        df["gmaps_phone"] = ["8325551234", "8325555678", None, None]
        df["gmaps_website"] = [None, None, None, None]
        df["gmaps_place_id"] = [None, None, None, None]
        df["gmaps_contact_email"] = [None, None, None, None]
        df["enrich_status"] = ["ok", "ok", "not_found", "skipped"]
        df["error_detail"] = [None, None, None, None]

        mod.print_summary(df)
        err = capsys.readouterr().err

        assert "total rows" in err and "4" in err
        assert "enriched (ok)" in err and "2" in err
        assert "not_found" in err and "1" in err
        assert "skipped" in err and "1" in err

    def test_does_not_raise_when_all_rows_skipped(self):
        df = pd.DataFrame([_make_canonical_row(contact_status="active")])
        df["gmaps_phone"] = None
        df["gmaps_website"] = None
        df["gmaps_place_id"] = None
        df["gmaps_contact_email"] = None
        df["enrich_status"] = "skipped"
        df["error_detail"] = None
        mod.print_summary(df)

    def test_does_not_raise_on_empty_dataframe(self):
        df = pd.DataFrame(columns=[
            "gmaps_phone", "gmaps_website", "gmaps_place_id",
            "gmaps_contact_email", "enrich_status", "error_detail",
        ])
        mod.print_summary(df)
