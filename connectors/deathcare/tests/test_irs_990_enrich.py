"""
Tests for irs_990_enrich.py — ProPublica 990 enrichment connector.

Strategy
--------
All HTTP calls are mocked — no network traffic occurs.
time.sleep is patched in every enrich_ein test to keep the suite fast
and to verify the rate-limit sleep is always called.

Three classes cover the three public functions:

  TestEnrichEin      — per-EIN API call logic (ok, not_found, error, fallback)
  TestEnrich         — batch DataFrame enrichment (filtering, new columns, skipping)
  TestPrintSummary   — smoke test: does not raise; produces stderr output
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import irs_990_enrich as mod

# ---------------------------------------------------------------- constants

_PATCH_SLEEP = "irs_990_enrich.time.sleep"
_EIN = "043783054"

# ---------------------------------------------------------------- helpers


def _make_bmf_row(**overrides) -> dict:
    """
    Return a dict representing one canonical BMF output row.

    Mirrors the shape produced by irs_bmf_deathcare.to_canonical().
    Override individual column values with keyword arguments.
    """
    defaults = {
        "source_id": f"irs_bmf:{_EIN}",
        "natural_key": _EIN,
        "vertical": "deathcare",
        "account_type": "cemetery",
        "name_raw": "Oak Grove Cemetery Assoc",
        "name_normalized": "oak grove cemetery assoc",
        "address_line_1": "100 Cemetery Rd",
        "city": "Gainesville",
        "state": "FL",
        "zip5": "32601",
        "phone_raw": None,
        "phone_normalized": None,
        "latitude": None,
        "longitude": None,
        "segment": "religious",
        "ein": _EIN,
        "county_fips": None,
        "size_metric": None,
        "size_value": None,
        "size_unit": None,
        "source_file": "https://www.irs.gov/pub/irs-soi/eo3.csv",
    }
    defaults.update(overrides)
    return defaults


def _make_bmf_df(*rows: dict) -> pd.DataFrame:
    """Wrap one or more row dicts into a DataFrame."""
    if not rows:
        rows = (_make_bmf_row(),)
    return pd.DataFrame(list(rows))


def _mock_ok_response(phone: str = "3525551234", name: str = "Oak Grove Cemetery Assoc") -> MagicMock:
    """Build a mock requests.Response for a successful ProPublica API call."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "organization": {
            "ein": _EIN,
            "name": name,
            "phone": phone,
        },
        "filings_with_data": [
            {
                "tax_prd_yr": 2022,
                "principal_officer": "JANE DOE",
                "totrevenue": 150000,
            }
        ],
    }
    return resp


def _mock_404_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 404
    resp.raise_for_status = MagicMock()
    return resp


def _mock_500_response() -> MagicMock:
    import requests as req_lib
    resp = MagicMock()
    resp.status_code = 500
    resp.raise_for_status.side_effect = req_lib.HTTPError("500 Server Error")
    return resp


# ===========================================================================
# Tests: enrich_ein()
# ===========================================================================


class TestEnrichEin:

    # -- successful response with phone + officer ----------------------------

    def test_ok_status_on_200_response(self):
        session = MagicMock()
        session.get.return_value = _mock_ok_response()

        with patch(_PATCH_SLEEP):
            result = mod.enrich_ein(_EIN, session)

        assert result["status"] == "ok"

    def test_phone_extracted_from_organization_phone(self):
        session = MagicMock()
        session.get.return_value = _mock_ok_response(phone="3525551234")

        with patch(_PATCH_SLEEP):
            result = mod.enrich_ein(_EIN, session)

        assert result["phone_990"] == "3525551234"

    def test_contact_name_extracted_from_organization_name(self):
        session = MagicMock()
        session.get.return_value = _mock_ok_response(name="Oak Grove Cemetery Assoc")

        with patch(_PATCH_SLEEP):
            result = mod.enrich_ein(_EIN, session)

        assert result["contact_name_990"] == "Oak Grove Cemetery Assoc"

    def test_ein_preserved_in_result(self):
        session = MagicMock()
        session.get.return_value = _mock_ok_response()

        with patch(_PATCH_SLEEP):
            result = mod.enrich_ein(_EIN, session)

        assert result["ein"] == _EIN

    # -- 404 → not_found ----------------------------------------------------

    def test_404_returns_not_found_status(self):
        session = MagicMock()
        session.get.return_value = _mock_404_response()

        with patch(_PATCH_SLEEP):
            result = mod.enrich_ein(_EIN, session)

        assert result["status"] == "not_found"

    def test_404_phone_is_none(self):
        session = MagicMock()
        session.get.return_value = _mock_404_response()

        with patch(_PATCH_SLEEP):
            result = mod.enrich_ein(_EIN, session)

        assert result["phone_990"] is None

    def test_404_contact_name_is_none(self):
        session = MagicMock()
        session.get.return_value = _mock_404_response()

        with patch(_PATCH_SLEEP):
            result = mod.enrich_ein(_EIN, session)

        assert result["contact_name_990"] is None

    # -- 500 → error ---------------------------------------------------------

    def test_500_returns_error_status(self):
        session = MagicMock()
        session.get.return_value = _mock_500_response()

        with patch(_PATCH_SLEEP):
            result = mod.enrich_ein(_EIN, session)

        assert result["status"] == "error"

    def test_500_phone_is_none(self):
        session = MagicMock()
        session.get.return_value = _mock_500_response()

        with patch(_PATCH_SLEEP):
            result = mod.enrich_ein(_EIN, session)

        assert result["phone_990"] is None

    def test_500_contact_name_is_none(self):
        session = MagicMock()
        session.get.return_value = _mock_500_response()

        with patch(_PATCH_SLEEP):
            result = mod.enrich_ein(_EIN, session)

        assert result["contact_name_990"] is None

    # -- missing phone falls back to None ------------------------------------

    def test_missing_phone_field_produces_none(self):
        """organization.phone absent → phone_990 is None (no fallback for phone)."""
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "organization": {"ein": _EIN, "name": "Oak Grove"},
            # phone key deliberately absent
            "filings_with_data": [],
        }
        session.get.return_value = resp

        with patch(_PATCH_SLEEP):
            result = mod.enrich_ein(_EIN, session)

        assert result["phone_990"] is None

    def test_empty_string_phone_produces_none(self):
        """organization.phone = '' (empty string) must not be stored."""
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "organization": {"ein": _EIN, "name": "Oak Grove", "phone": ""},
            "filings_with_data": [],
        }
        session.get.return_value = resp

        with patch(_PATCH_SLEEP):
            result = mod.enrich_ein(_EIN, session)

        assert result["phone_990"] is None

    def test_name_falls_back_to_principal_officer_when_org_name_missing(self):
        """
        When organization.name is absent, filings_with_data[0].principal_officer
        is used as the contact_name_990 fallback.
        """
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            # name key absent at org level
            "organization": {"ein": _EIN, "phone": "3525551234"},
            "filings_with_data": [
                {"tax_prd_yr": 2022, "principal_officer": "JANE DOE", "totrevenue": 150000}
            ],
        }
        session.get.return_value = resp

        with patch(_PATCH_SLEEP):
            result = mod.enrich_ein(_EIN, session)

        assert result["contact_name_990"] == "JANE DOE"

    def test_contact_name_none_when_filings_empty_and_org_name_missing(self):
        """If both organization.name and filings_with_data are absent, name stays None."""
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "organization": {"ein": _EIN, "phone": "3525551234"},
            "filings_with_data": [],
        }
        session.get.return_value = resp

        with patch(_PATCH_SLEEP):
            result = mod.enrich_ein(_EIN, session)

        assert result["contact_name_990"] is None

    # -- sleep is always called ----------------------------------------------

    def test_sleep_called_on_successful_response(self):
        """Rate-limit sleep must fire after every request, including successes."""
        session = MagicMock()
        session.get.return_value = _mock_ok_response()

        with patch(_PATCH_SLEEP) as mock_sleep:
            mod.enrich_ein(_EIN, session, sleep_s=0.5)

        mock_sleep.assert_called_once_with(0.5)

    def test_sleep_called_on_404(self):
        session = MagicMock()
        session.get.return_value = _mock_404_response()

        with patch(_PATCH_SLEEP) as mock_sleep:
            mod.enrich_ein(_EIN, session, sleep_s=0.5)

        mock_sleep.assert_called_once_with(0.5)

    def test_sleep_called_on_error(self):
        """sleep must fire even when raise_for_status raises HTTPError."""
        session = MagicMock()
        session.get.return_value = _mock_500_response()

        with patch(_PATCH_SLEEP) as mock_sleep:
            mod.enrich_ein(_EIN, session, sleep_s=0.5)

        mock_sleep.assert_called_once_with(0.5)

    def test_sleep_duration_is_passed_through(self):
        """sleep_s parameter must be forwarded to time.sleep exactly."""
        session = MagicMock()
        session.get.return_value = _mock_ok_response()

        with patch(_PATCH_SLEEP) as mock_sleep:
            mod.enrich_ein(_EIN, session, sleep_s=1.25)

        mock_sleep.assert_called_once_with(1.25)


# ===========================================================================
# Tests: enrich()
# ===========================================================================


class TestEnrich:

    # -- filtering -----------------------------------------------------------

    def test_skips_rows_where_segment_is_not_religious(self):
        """Non-religious rows must not be sent to the API and get status='skipped'."""
        df = _make_bmf_df(_make_bmf_row(segment="municipal", ein="111111111"))

        with patch("irs_990_enrich.enrich_ein") as mock_enrich:
            out = mod.enrich(df, workers=1, sleep_s=0)

        mock_enrich.assert_not_called()
        assert out["enrich_status"].iloc[0] == "skipped"

    def test_skips_rows_where_ein_is_null(self):
        """Rows with null EIN cannot be looked up and must be skipped."""
        df = _make_bmf_df(_make_bmf_row(ein=None))

        with patch("irs_990_enrich.enrich_ein") as mock_enrich:
            out = mod.enrich(df, workers=1, sleep_s=0)

        mock_enrich.assert_not_called()
        assert out["enrich_status"].iloc[0] == "skipped"

    def test_skips_rows_where_ein_is_empty_string(self):
        df = _make_bmf_df(_make_bmf_row(ein=""))

        with patch("irs_990_enrich.enrich_ein") as mock_enrich:
            out = mod.enrich(df, workers=1, sleep_s=0)

        mock_enrich.assert_not_called()
        assert out["enrich_status"].iloc[0] == "skipped"

    def test_enrich_status_skipped_for_non_religious_rows(self):
        """Municipal row is skipped; enrich_ein is called exactly once for the religious row."""
        df = _make_bmf_df(
            _make_bmf_row(segment="municipal", ein="111111111"),
            _make_bmf_row(segment="religious", ein=_EIN),
        )

        with patch("irs_990_enrich.enrich_ein") as mock_enrich:
            mock_enrich.return_value = {
                "ein": _EIN,
                "phone_990": None,
                "contact_name_990": None,
                "status": "ok",
            }
            out = mod.enrich(df, workers=1, sleep_s=0)

        mock_enrich.assert_called_once()
        # First row is municipal → skipped
        assert out["enrich_status"].iloc[0] == "skipped"

    # -- new columns are added -----------------------------------------------

    def test_phone_990_column_present_after_enrich(self):
        df = _make_bmf_df()
        session_mock = MagicMock()
        session_mock.get.return_value = _mock_ok_response()

        with patch("irs_990_enrich.requests.Session", return_value=session_mock):
            with patch(_PATCH_SLEEP):
                out = mod.enrich(df, workers=1, sleep_s=0)

        assert "phone_990" in out.columns

    def test_contact_name_990_column_present_after_enrich(self):
        df = _make_bmf_df()
        session_mock = MagicMock()
        session_mock.get.return_value = _mock_ok_response()

        with patch("irs_990_enrich.requests.Session", return_value=session_mock):
            with patch(_PATCH_SLEEP):
                out = mod.enrich(df, workers=1, sleep_s=0)

        assert "contact_name_990" in out.columns

    def test_enrich_status_column_present_after_enrich(self):
        df = _make_bmf_df()
        session_mock = MagicMock()
        session_mock.get.return_value = _mock_ok_response()

        with patch("irs_990_enrich.requests.Session", return_value=session_mock):
            with patch(_PATCH_SLEEP):
                out = mod.enrich(df, workers=1, sleep_s=0)

        assert "enrich_status" in out.columns

    def test_phone_990_populated_for_ok_row(self):
        df = _make_bmf_df()
        session_mock = MagicMock()
        session_mock.get.return_value = _mock_ok_response(phone="3525551234")

        with patch("irs_990_enrich.requests.Session", return_value=session_mock):
            with patch(_PATCH_SLEEP):
                out = mod.enrich(df, workers=1, sleep_s=0)

        assert out["phone_990"].iloc[0] == "3525551234"

    def test_enrich_status_ok_for_successful_row(self):
        df = _make_bmf_df()
        session_mock = MagicMock()
        session_mock.get.return_value = _mock_ok_response()

        with patch("irs_990_enrich.requests.Session", return_value=session_mock):
            with patch(_PATCH_SLEEP):
                out = mod.enrich(df, workers=1, sleep_s=0)

        assert out["enrich_status"].iloc[0] == "ok"

    # -- multiple EINs -------------------------------------------------------

    def test_processes_multiple_religious_rows(self):
        """All religious rows with valid EINs must be enriched."""
        ein2 = "123456789"
        df = _make_bmf_df(
            _make_bmf_row(ein=_EIN),
            _make_bmf_row(ein=ein2),
        )
        session_mock = MagicMock()
        session_mock.get.return_value = _mock_ok_response()

        with patch("irs_990_enrich.requests.Session", return_value=session_mock):
            with patch(_PATCH_SLEEP):
                out = mod.enrich(df, workers=1, sleep_s=0)

        assert (out["enrich_status"] == "ok").sum() == 2

    def test_mixed_segments_only_enriches_religious_rows(self):
        ein2 = "123456789"
        df = _make_bmf_df(
            _make_bmf_row(ein=_EIN, segment="religious"),
            _make_bmf_row(ein=ein2, segment="municipal"),
        )
        session_mock = MagicMock()
        session_mock.get.return_value = _mock_ok_response()

        with patch("irs_990_enrich.requests.Session", return_value=session_mock):
            with patch(_PATCH_SLEEP):
                out = mod.enrich(df, workers=1, sleep_s=0)

        assert (out["enrich_status"] == "ok").sum() == 1
        assert (out["enrich_status"] == "skipped").sum() == 1

    def test_row_count_preserved(self):
        """enrich() must not add or drop rows."""
        df = _make_bmf_df(
            _make_bmf_row(ein=_EIN),
            _make_bmf_row(ein="111111111", segment="municipal"),
        )
        session_mock = MagicMock()
        session_mock.get.return_value = _mock_ok_response()

        with patch("irs_990_enrich.requests.Session", return_value=session_mock):
            with patch(_PATCH_SLEEP):
                out = mod.enrich(df, workers=1, sleep_s=0)

        assert len(out) == 2

    def test_not_found_status_propagated(self):
        df = _make_bmf_df()
        session_mock = MagicMock()
        session_mock.get.return_value = _mock_404_response()

        with patch("irs_990_enrich.requests.Session", return_value=session_mock):
            with patch(_PATCH_SLEEP):
                out = mod.enrich(df, workers=1, sleep_s=0)

        assert out["enrich_status"].iloc[0] == "not_found"

    def test_original_columns_preserved(self):
        """enrich() must not clobber any existing columns."""
        df = _make_bmf_df()
        original_cols = set(df.columns)
        session_mock = MagicMock()
        session_mock.get.return_value = _mock_ok_response()

        with patch("irs_990_enrich.requests.Session", return_value=session_mock):
            with patch(_PATCH_SLEEP):
                out = mod.enrich(df, workers=1, sleep_s=0)

        assert original_cols.issubset(set(out.columns))


# ===========================================================================
# Tests: print_summary()
# ===========================================================================


class TestPrintSummary:

    def _enriched_df(self) -> pd.DataFrame:
        """Build a minimal enriched DataFrame for summary tests."""
        rows = [
            _make_bmf_row(ein=_EIN),
            _make_bmf_row(ein="111111111", segment="municipal"),
        ]
        df = pd.DataFrame(rows)
        df["phone_990"] = [None, None]
        df["contact_name_990"] = [None, None]
        df["enrich_status"] = ["ok", "skipped"]
        return df

    def test_does_not_raise_on_well_formed_input(self):
        """print_summary must not raise on a normal enriched DataFrame."""
        df = self._enriched_df()
        # Must not raise
        mod.print_summary(df)

    def test_stderr_contains_correct_counts_per_status(self, capsys):
        """
        Feed a known DataFrame: 2 ok, 1 not_found, 1 skipped (4 total).
        Assert that the stderr output contains the labeled count line for each
        status bucket exactly as print_summary formats them.
        """
        rows = [
            _make_bmf_row(ein=_EIN,         segment="religious"),   # ok
            _make_bmf_row(ein="111111111",   segment="religious"),   # ok
            _make_bmf_row(ein="222222222",   segment="religious"),   # not_found
            _make_bmf_row(ein="333333333",   segment="municipal"),   # skipped
        ]
        df = pd.DataFrame(rows)
        df["phone_990"]        = [None,         None, None, None]
        df["contact_name_990"] = [None,         None, None, None]
        df["enrich_status"]    = ["ok", "ok", "not_found", "skipped"]

        mod.print_summary(df)
        err = capsys.readouterr().err

        # Each label must appear alongside its exact integer count.
        assert "total rows" in err and "4" in err, (
            "Expected 'total rows' and count 4 in stderr"
        )
        assert "enriched (ok)" in err and "2" in err, (
            "Expected 'enriched (ok)' and count 2 in stderr"
        )
        assert "not_found" in err and "1" in err, (
            "Expected 'not_found' and count 1 in stderr"
        )
        assert "skipped" in err and "1" in err, (
            "Expected 'skipped' and count 1 in stderr"
        )

    def test_does_not_raise_when_all_rows_skipped(self):
        df = pd.DataFrame([_make_bmf_row(segment="municipal")])
        df["phone_990"] = None
        df["contact_name_990"] = None
        df["enrich_status"] = "skipped"
        mod.print_summary(df)

    def test_does_not_raise_when_all_rows_not_found(self):
        df = pd.DataFrame([_make_bmf_row()])
        df["phone_990"] = None
        df["contact_name_990"] = None
        df["enrich_status"] = "not_found"
        mod.print_summary(df)

    def test_does_not_raise_on_empty_dataframe(self):
        """Empty DataFrame edge case must not cause a ZeroDivisionError."""
        df = pd.DataFrame(columns=["phone_990", "contact_name_990", "enrich_status"])
        mod.print_summary(df)


# ===========================================================================
# Tests: enrich() — concurrent (workers > 1) path
# ===========================================================================


class TestEnrichConcurrent:
    """
    Exercises the ThreadPoolExecutor branch inside run_enrichment().

    The key invariant under test: futures complete in arbitrary order, but each
    result must land on the DataFrame row whose EIN triggered it — no index
    scrambling.  We drive this by giving every EIN a distinct mock response and
    then asserting that each row carries exactly the values we prescribed for
    its own EIN.
    """

    # EINs used across this class — kept distinct to catch cross-row pollution.
    _EIN_A = "111111111"
    _EIN_B = "222222222"
    _EIN_C = "333333333"
    _EIN_D = "444444444"

    # Per-EIN expected payloads returned by the mock.
    _RESULTS_BY_EIN: dict[str, dict] = {
        _EIN_A: {"ein": _EIN_A, "phone_990": "5555550001", "contact_name_990": "Alice Corp",  "status": "ok"},
        _EIN_B: {"ein": _EIN_B, "phone_990": "5555550002", "contact_name_990": "Bob Inc",     "status": "ok"},
        _EIN_C: {"ein": _EIN_C, "phone_990": None,         "contact_name_990": "Charlie LLC", "status": "ok"},
        _EIN_D: {"ein": _EIN_D, "phone_990": "5555550004", "contact_name_990": None,           "status": "ok"},
    }

    @pytest.fixture()
    def enriched_df(self) -> pd.DataFrame:
        """
        Four religious rows with distinct EINs, enriched via workers=2.

        enrich_ein is patched with a side_effect that dispatches on the EIN
        argument, simulating a live API that returns different data per org.
        """
        df = _make_bmf_df(
            _make_bmf_row(segment="religious", ein=self._EIN_A),
            _make_bmf_row(segment="religious", ein=self._EIN_B),
            _make_bmf_row(segment="religious", ein=self._EIN_C),
            _make_bmf_row(segment="religious", ein=self._EIN_D),
        )

        def _dispatch(ein, session, sleep_s=0.5):  # noqa: ARG001
            return self._RESULTS_BY_EIN[ein]

        with patch("irs_990_enrich.enrich_ein", side_effect=_dispatch):
            out = mod.enrich(df, workers=2, sleep_s=0)

        return out

    def test_all_rows_are_enriched(self, enriched_df):
        """Every row must receive enrich_status='ok' — none silently dropped."""
        assert (enriched_df["enrich_status"] == "ok").all()

    def test_phone_990_populated_for_every_row_that_has_a_phone(self, enriched_df):
        """Rows where the mock returned a phone must have it; others stay None."""
        # EIN_A, EIN_B, EIN_D have phones; EIN_C does not.
        assert enriched_df.loc[enriched_df["ein"] == self._EIN_A, "phone_990"].iloc[0] == "5555550001"
        assert enriched_df.loc[enriched_df["ein"] == self._EIN_B, "phone_990"].iloc[0] == "5555550002"
        assert enriched_df.loc[enriched_df["ein"] == self._EIN_C, "phone_990"].iloc[0] is None
        assert enriched_df.loc[enriched_df["ein"] == self._EIN_D, "phone_990"].iloc[0] == "5555550004"

    def test_contact_name_990_matches_per_ein_payload(self, enriched_df):
        """Each row's contact_name_990 must come from that row's EIN, not a neighbour's."""
        assert enriched_df.loc[enriched_df["ein"] == self._EIN_A, "contact_name_990"].iloc[0] == "Alice Corp"
        assert enriched_df.loc[enriched_df["ein"] == self._EIN_B, "contact_name_990"].iloc[0] == "Bob Inc"
        assert enriched_df.loc[enriched_df["ein"] == self._EIN_C, "contact_name_990"].iloc[0] == "Charlie LLC"
        assert enriched_df.loc[enriched_df["ein"] == self._EIN_D, "contact_name_990"].iloc[0] is None

    def test_row_count_unchanged_after_concurrent_enrich(self, enriched_df):
        """workers=2 must not add or drop rows compared with the input."""
        assert len(enriched_df) == 4

    def test_original_columns_preserved_after_concurrent_enrich(self, enriched_df):
        """Pre-existing columns must not be clobbered by the write-back loop."""
        expected_original_cols = set(_make_bmf_row().keys())
        assert expected_original_cols.issubset(set(enriched_df.columns))
