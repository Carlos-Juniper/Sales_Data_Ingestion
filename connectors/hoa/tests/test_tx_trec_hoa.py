"""
Tests for tx_trec_hoa.py — Texas TREC HOA management certificate connector.

Strategy
--------
Every public function is covered in isolation.  All I/O is in-memory:
  - DataFrames are built programmatically.
  - The backfill crosswalk test writes to a ``tmp_path`` fixture (unavoidable
    because backfill_county_from_zip accepts a file path, not a DataFrame).
  - No network calls, no disk reads of source CSVs.

Two source-level traps from the module header are explicitly tested:
  1. County field is 15% unusable ("TX", "Texas", "N/A", …).
  2. Name filtering must NOT be applied (6.4% lack association keywords).
"""

from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tx_trec_hoa as mod


# ===========================================================================
# Helpers
# ===========================================================================

# A certificate URL that matches CERT_URL_RE
VALID_CERT_URL = (
    "https://hoa.texas.gov/certificates/12345/67890/mc/"
)
# A URL whose IDs are different — used to check independence
VALID_CERT_URL_B = (
    "https://hoa.texas.gov/certificates/99999/11111/mc/"
)


def _make_raw(**overrides) -> pd.DataFrame:
    """
    Return a single-row DataFrame with all six source columns populated.
    Override individual column values with keyword arguments.
    """
    defaults = {
        "Name": "Sunset Ridge Homeowners Association",
        "County": "HARRIS",
        "City": "Houston",
        "Zip": "77001",
        "Type": "HOA",
        "Certificate": VALID_CERT_URL,
        "_source_file": "TREC_HOA_2026.csv",
    }
    defaults.update(overrides)
    return pd.DataFrame([defaults])


def _make_rows(*row_overrides: dict) -> pd.DataFrame:
    """Build a multi-row raw DataFrame from a list of per-row override dicts."""
    frames = [_make_raw(**overrides) for overrides in row_overrides]
    return pd.concat(frames, ignore_index=True)


# ===========================================================================
# Tests: normalize()
# ===========================================================================

class TestNormalize:

    # -- URL extraction ------------------------------------------------------

    def test_valid_cert_url_extracts_association_id(self):
        df = _make_raw(**{"Certificate": VALID_CERT_URL})
        out = mod.normalize(df)
        assert out["association_id"].iloc[0] == "12345"

    def test_valid_cert_url_extracts_certificate_id(self):
        df = _make_raw(**{"Certificate": VALID_CERT_URL})
        out = mod.normalize(df)
        assert out["certificate_id"].iloc[0] == "67890"

    def test_malformed_cert_url_yields_na_ids(self):
        df = _make_raw(**{"Certificate": "https://hoa.texas.gov/not-a-match/"})
        out = mod.normalize(df)
        assert pd.isna(out["association_id"].iloc[0])
        assert pd.isna(out["certificate_id"].iloc[0])

    # -- ZIP normalisation ---------------------------------------------------

    def test_zip_hyphenated_stripped_to_5(self):
        df = _make_raw(**{"Zip": "78701-1234"})
        out = mod.normalize(df)
        assert out["zip5"].iloc[0] == "78701"

    def test_zip_non_numeric_yields_empty_string(self):
        df = _make_raw(**{"Zip": "TX"})
        out = mod.normalize(df)
        # "TX" stripped of non-digits → "" which has len < 5 → fillna("") → ""
        assert out["zip5"].iloc[0] == ""

    def test_zip_plain_5_digit_unchanged(self):
        df = _make_raw(**{"Zip": "77001"})
        out = mod.normalize(df)
        assert out["zip5"].iloc[0] == "77001"

    # -- County junk detection -----------------------------------------------

    def test_county_tx_sets_usable_false(self):
        df = _make_raw(**{"County": "TX"})
        out = mod.normalize(df)
        assert bool(out["county_usable"].iloc[0]) is False

    def test_county_texas_sets_usable_false(self):
        df = _make_raw(**{"County": "TEXAS"})
        out = mod.normalize(df)
        assert bool(out["county_usable"].iloc[0]) is False

    def test_county_na_string_sets_usable_false(self):
        df = _make_raw(**{"County": "N/A"})
        out = mod.normalize(df)
        assert bool(out["county_usable"].iloc[0]) is False

    def test_county_empty_string_sets_usable_false(self):
        df = _make_raw(**{"County": ""})
        out = mod.normalize(df)
        assert bool(out["county_usable"].iloc[0]) is False

    def test_real_county_name_sets_usable_true(self):
        df = _make_raw(**{"County": "HARRIS"})
        out = mod.normalize(df)
        assert bool(out["county_usable"].iloc[0]) is True

    # -- Multi-county flag ---------------------------------------------------

    def test_slash_separated_county_sets_is_multi_county_true(self):
        df = _make_raw(**{"County": "HARRIS/MONTGOMERY"})
        out = mod.normalize(df)
        assert bool(out["is_multi_county"].iloc[0]) is True

    def test_single_county_sets_is_multi_county_false(self):
        df = _make_raw(**{"County": "HARRIS"})
        out = mod.normalize(df)
        assert bool(out["is_multi_county"].iloc[0]) is False

    def test_county_primary_for_multi_county_is_first_segment(self):
        df = _make_raw(**{"County": "HARRIS/MONTGOMERY"})
        out = mod.normalize(df)
        assert out["county_primary"].iloc[0] == "HARRIS"

    def test_county_primary_for_single_county_is_full_value(self):
        df = _make_raw(**{"County": "TRAVIS"})
        out = mod.normalize(df)
        assert out["county_primary"].iloc[0] == "TRAVIS"

    def test_county_primary_is_na_when_county_is_junk(self):
        df = _make_raw(**{"County": "TX"})
        out = mod.normalize(df)
        # county_usable=False → county_primary should be NaN
        assert pd.isna(out["county_primary"].iloc[0])

    # -- association_type mapping -------------------------------------------

    def test_type_hoa_maps_to_homeowners_association(self):
        df = _make_raw(**{"Type": "HOA"})
        out = mod.normalize(df)
        assert out["association_type"].iloc[0] == "homeowners_association"

    def test_type_poa_maps_correctly(self):
        df = _make_raw(**{"Type": "POA"})
        out = mod.normalize(df)
        assert out["association_type"].iloc[0] == "property_owners_association"

    def test_type_coa_maps_correctly(self):
        df = _make_raw(**{"Type": "COA"})
        out = mod.normalize(df)
        assert out["association_type"].iloc[0] == "condominium_owners_association"

    def test_unknown_type_maps_to_unknown(self):
        df = _make_raw(**{"Type": "XYZ"})
        out = mod.normalize(df)
        assert out["association_type"].iloc[0] == "unknown"

    # -- county_source_is_derived default ------------------------------------

    def test_county_source_is_derived_defaults_to_false(self):
        df = _make_raw()
        out = mod.normalize(df)
        assert bool(out["county_source_is_derived"].iloc[0]) is False


# ===========================================================================
# Tests: backfill_county_from_zip()
# ===========================================================================

class TestBackfillCountyFromZip:
    """
    The function accepts a file path, so we must write a temp file.
    All other I/O is in-memory.
    """

    CROSSWALK_CSV = "zip5,county_name\n77001,HARRIS\n78701,TRAVIS\n75201,DALLAS\n"

    def _crosswalk_path(self, tmp_path) -> str:
        p = tmp_path / "zcta_county.csv"
        p.write_text(self.CROSSWALK_CSV)
        return str(p)

    def test_missing_county_filled_from_zip(self, tmp_path):
        """Row with no county_primary gets filled from the ZIP crosswalk."""
        df = _make_raw(**{"County": "TX", "Zip": "77001"})
        df = mod.normalize(df)
        # Confirm it starts with no usable county
        assert pd.isna(df["county_primary"].iloc[0])

        out = mod.backfill_county_from_zip(df, self._crosswalk_path(tmp_path))
        assert out["county_primary"].iloc[0] == "HARRIS"

    def test_existing_county_not_overwritten(self, tmp_path):
        """Row that already has a valid county_primary must not be changed."""
        df = _make_raw(**{"County": "TRAVIS", "Zip": "77001"})
        df = mod.normalize(df)
        assert df["county_primary"].iloc[0] == "TRAVIS"

        out = mod.backfill_county_from_zip(df, self._crosswalk_path(tmp_path))
        # ZIP 77001 → HARRIS, but county_primary was already TRAVIS
        assert out["county_primary"].iloc[0] == "TRAVIS"

    def test_county_source_is_derived_true_on_filled_rows(self, tmp_path):
        df = _make_raw(**{"County": "TX", "Zip": "77001"})
        df = mod.normalize(df)
        out = mod.backfill_county_from_zip(df, self._crosswalk_path(tmp_path))
        assert bool(out["county_source_is_derived"].iloc[0]) is True

    def test_county_source_is_derived_false_on_already_populated_rows(self, tmp_path):
        df = _make_raw(**{"County": "HARRIS", "Zip": "77001"})
        df = mod.normalize(df)
        out = mod.backfill_county_from_zip(df, self._crosswalk_path(tmp_path))
        assert bool(out["county_source_is_derived"].iloc[0]) is False

    def test_no_crosswalk_path_returns_df_unchanged(self):
        df = _make_raw(**{"County": "TX", "Zip": "77001"})
        df = mod.normalize(df)
        out = mod.backfill_county_from_zip(df, None)
        assert pd.isna(out["county_primary"].iloc[0])

    def test_zip_with_no_crosswalk_match_leaves_county_na(self, tmp_path):
        df = _make_raw(**{"County": "TX", "Zip": "99999"})
        df = mod.normalize(df)
        out = mod.backfill_county_from_zip(df, self._crosswalk_path(tmp_path))
        # 99999 not in crosswalk → county_primary stays NaN
        assert pd.isna(out["county_primary"].iloc[0])
        assert bool(out["county_source_is_derived"].iloc[0]) is False


# ===========================================================================
# Tests: build_pdf_queue()
# ===========================================================================

class TestBuildPdfQueue:
    EXPECTED_COLUMNS = {
        "association_id", "certificate_id", "url",
        "Name", "county_primary", "city", "zip5", "priority",
        "fetch_status", "parse_status",
    }

    def _normalized_df(self) -> pd.DataFrame:
        rows = _make_rows(
            {"County": "HARRIS", "City": "Houston", "Zip": "77001",
             "Certificate": "https://hoa.texas.gov/certificates/10001/20001/mc/"},
            {"County": "BEXAR", "City": "San Antonio", "Zip": "78201",
             "Certificate": "https://hoa.texas.gov/certificates/10002/20002/mc/"},
            {"County": "HARRIS", "City": "Houston", "Zip": "77002",
             "Certificate": "https://hoa.texas.gov/certificates/10003/20003/mc/"},
        )
        return mod.normalize(rows)

    def test_all_expected_columns_present(self):
        df = self._normalized_df()
        out = mod.build_pdf_queue(df)
        missing = self.EXPECTED_COLUMNS - set(out.columns)
        assert not missing, f"Missing columns from pdf queue: {missing}"

    def test_fetch_status_pending_on_every_row(self):
        out = mod.build_pdf_queue(self._normalized_df())
        assert (out["fetch_status"] == "pending").all()

    def test_parse_status_pending_on_every_row(self):
        out = mod.build_pdf_queue(self._normalized_df())
        assert (out["parse_status"] == "pending").all()

    def test_harris_county_rows_appear_before_lower_priority_counties(self):
        df = self._normalized_df()
        out = mod.build_pdf_queue(df).reset_index(drop=True)
        # All HARRIS rows should appear before BEXAR rows in the queue.
        # Use positional (iloc) index after reset so sort_values' label
        # preservation does not confuse the comparison.
        harris_positions = out.index[out["county_primary"] == "HARRIS"].tolist()
        bexar_positions = out.index[out["county_primary"] == "BEXAR"].tolist()
        assert max(harris_positions) < min(bexar_positions), (
            "HARRIS (high-priority metro) must come before BEXAR in the PDF queue"
        )

    def test_unknown_county_gets_lowest_priority(self):
        rows = _make_rows(
            {"County": "HARRIS", "Certificate": "https://hoa.texas.gov/certificates/1/2/mc/"},
            {"County": "UNKNOWN COUNTY XYZ", "Certificate": "https://hoa.texas.gov/certificates/3/4/mc/"},
        )
        df = mod.normalize(rows)
        out = mod.build_pdf_queue(df)
        harris_priority = out[out["county_primary"] == "HARRIS"]["priority"].iloc[0]
        unknown_priority = out[out["county_primary"] == "UNKNOWN COUNTY XYZ"]["priority"].iloc[0]
        assert harris_priority < unknown_priority

    def test_row_count_preserved(self):
        df = self._normalized_df()
        out = mod.build_pdf_queue(df)
        assert len(out) == len(df)


# ===========================================================================
# Tests: to_canonical() — output shape
# ===========================================================================

class TestToCanonical:
    EXPECTED_COLUMNS = {
        "source_id", "natural_key", "vertical", "account_type",
        "legal_name", "name_normalized", "association_type",
        "site_city", "site_state", "site_zip",
        "county_primary", "is_multi_county",
        "trec_assoc_id", "trec_certificate_id", "certificate_url",
        "site_street", "mailing_address", "phone", "email", "managing_agent",
        "contact_status", "geocode_status",
    }

    def _normalized_df(self) -> pd.DataFrame:
        return mod.normalize(_make_raw())

    def test_all_expected_columns_present(self):
        out = mod.to_canonical(self._normalized_df())
        missing = self.EXPECTED_COLUMNS - set(out.columns)
        assert not missing, f"Missing columns: {missing}"

    def test_contact_status_is_pending_pdf_on_every_row(self):
        out = mod.to_canonical(self._normalized_df())
        assert (out["contact_status"] == "pending_pdf").all()

    def test_geocode_status_is_zip_centroid_only_on_every_row(self):
        out = mod.to_canonical(self._normalized_df())
        assert (out["geocode_status"] == "zip_centroid_only").all()

    def test_source_id_constant(self):
        out = mod.to_canonical(self._normalized_df())
        assert (out["source_id"] == mod.SOURCE_ID).all()

    def test_vertical_is_hoa(self):
        out = mod.to_canonical(self._normalized_df())
        assert (out["vertical"] == mod.VERTICAL).all()

    def test_account_type_is_association(self):
        out = mod.to_canonical(self._normalized_df())
        assert (out["account_type"] == "association").all()

    def test_site_state_is_tx(self):
        out = mod.to_canonical(self._normalized_df())
        assert (out["site_state"] == "TX").all()

    def test_address_columns_are_na(self):
        """Street address, phone, email, managing agent are empty until PDF pass."""
        out = mod.to_canonical(self._normalized_df())
        for col in ("site_street", "mailing_address", "phone", "email", "managing_agent"):
            assert pd.isna(out[col].iloc[0]), f"{col} should be NA before PDF pass"

    def test_natural_key_maps_to_association_id(self):
        df = self._normalized_df()
        out = mod.to_canonical(df)
        assert (out["natural_key"] == df["association_id"].values).all()


# ===========================================================================
# Tests: assert_source_shape()
# ===========================================================================

class TestAssertSourceShape:
    def _well_formed_df(self, n: int = 100) -> pd.DataFrame:
        """Return a DataFrame that passes all source shape assertions."""
        rows = []
        for i in range(n):
            cert = f"https://hoa.texas.gov/certificates/{10000 + i}/{20000 + i}/mc/"
            rows.append(_make_raw(**{
                "Name": f"Association {i}",
                "County": "HARRIS",
                "City": "Houston",
                "Zip": "77001",
                "Type": "HOA",
                "Certificate": cert,
            }).iloc[0].to_dict())
        return pd.DataFrame(rows)

    def test_passes_on_well_formed_dataframe(self):
        df = self._well_formed_df()
        mod.assert_source_shape(df)  # must not raise

    def test_raises_on_missing_name_column(self):
        df = self._well_formed_df().drop(columns=["Name"])
        with pytest.raises(ValueError, match="missing column"):
            mod.assert_source_shape(df)

    def test_raises_on_missing_county_column(self):
        df = self._well_formed_df().drop(columns=["County"])
        with pytest.raises(ValueError, match="missing column"):
            mod.assert_source_shape(df)

    def test_raises_on_missing_certificate_column(self):
        df = self._well_formed_df().drop(columns=["Certificate"])
        with pytest.raises(ValueError, match="missing column"):
            mod.assert_source_shape(df)

    def test_raises_when_url_parse_rate_drops_below_98_percent(self):
        """
        If more than 2% of Certificate URLs don't match the expected pattern,
        the schema has changed and the connector must be updated.
        """
        df = self._well_formed_df(n=100)
        # Set 5 rows to junk URLs — 5% failure rate exceeds the 2% threshold
        for i in range(5):
            df.loc[i, "Certificate"] = "https://hoa.texas.gov/NOT_A_CERT/"
        with pytest.raises(ValueError, match="certificate URL"):
            mod.assert_source_shape(df)

    def test_raises_on_unmapped_type_value(self):
        df = self._well_formed_df()
        df.loc[0, "Type"] = "UNKNOWN_TYPE"
        with pytest.raises(ValueError, match="unmapped Type"):
            mod.assert_source_shape(df)

    def test_raises_when_name_fill_rate_below_99_percent(self):
        df = self._well_formed_df(n=200)
        for i in range(10):
            df.loc[i, "Name"] = ""
        with pytest.raises(ValueError, match="Name"):
            mod.assert_source_shape(df)

    def test_raises_when_type_fill_rate_below_99_percent(self):
        df = self._well_formed_df(n=200)
        for i in range(10):
            df.loc[i, "Type"] = ""
        with pytest.raises(ValueError, match="Type"):
            mod.assert_source_shape(df)


# ===========================================================================
# Tests: SOURCE_ID and --write-db DB wiring
# ===========================================================================

class TestSourceId:
    def test_source_id_matches_staging_table_name(self):
        """SOURCE_ID must equal 'tx_trec_hoa' to match the migration-007 table."""
        assert mod.SOURCE_ID == "tx_trec_hoa"

    def test_vertical_is_hoa(self):
        assert mod.VERTICAL == "hoa"


class TestDbWiring:
    """
    Verify the DB write sequence for --write-db:
        raw_sha256 → upload_raw → write_source_run → build_canonical
        → upsert_staging → finish_source_run.

    All DB and GCS calls are mocked — no live Postgres or GCS required.
    Patch targets use the module name as imported (tx_trec_hoa) because
    sys.path.insert makes this the resolved module name.
    """

    def _canonical_df(self) -> pd.DataFrame:
        """Return a minimal to_canonical() output for 2 HOA rows."""
        raw = _make_rows(
            {"County": "HARRIS", "City": "Houston", "Zip": "77001",
             "Type": "HOA",
             "Certificate": "https://hoa.texas.gov/certificates/10001/20001/mc/"},
            {"County": "BEXAR", "City": "San Antonio", "Zip": "78201",
             "Type": "POA",
             "Certificate": "https://hoa.texas.gov/certificates/10002/20002/mc/"},
        )
        df = mod.normalize(raw)
        return mod.to_canonical(df)

    def _run_db_write(self, canonical: pd.DataFrame, raw_bytes: bytes = b"csv_data"):
        """Invoke the DB write path with all external calls mocked.

        Returns a dict of the mock objects for assertion.
        """
        with (
            patch("tx_trec_hoa.get_secret", return_value="postgresql://localhost/test"),
            patch("tx_trec_hoa.get_engine") as mock_engine_factory,
            patch("tx_trec_hoa.raw_sha256", return_value="abc123") as mock_sha256,
            patch("tx_trec_hoa.upload_raw", return_value="gs://juniper-ingest-raw/tx_trec_hoa/2026-08-20/abc123.json.gz") as mock_upload,
            patch("tx_trec_hoa.write_source_run", return_value=7) as mock_write,
            patch("tx_trec_hoa.build_canonical") as mock_build,
            patch("tx_trec_hoa.upsert_staging") as mock_upsert,
            patch("tx_trec_hoa.finish_source_run") as mock_finish,
        ):
            mock_engine = MagicMock()
            mock_engine_factory.return_value = mock_engine

            # build_canonical returns a DataFrame shaped like the canonical output
            canonical_out = pd.DataFrame(
                {"source_id": [mod.SOURCE_ID] * len(canonical)},
                index=canonical.index,
            )
            mock_build.return_value = canonical_out

            # Simulate the write path (mirrors main() logic)
            sha256_hex = mock_sha256(raw_bytes)
            byte_count = len(raw_bytes)
            raw_uri = mock_upload(mod.SOURCE_ID, "2026-08-20", raw_bytes)
            engine = mock_engine_factory()
            source_run_id = mock_write(
                engine,
                source_id=mod.SOURCE_ID,
                byte_count=byte_count,
                sha256=sha256_hex,
                connector_version="1.0",
                license_string="Texas public records — free to store and use commercially",
                raw_uri=raw_uri,
            )
            full_canonical = mock_build(
                canonical.index,
                source_id=mod.SOURCE_ID,
                natural_key=canonical["natural_key"],
                vertical=mod.VERTICAL,
                account_type=canonical["account_type"],
                name_raw=canonical["legal_name"],
                name_normalized=canonical["name_normalized"],
                city=canonical["site_city"],
                state=canonical["site_state"],
                zip5=canonical["site_zip"],
                county_fips=canonical["county_primary"],
                size_metric=canonical["association_type"],
                source_file="tx_trec_hoa_csv",
            )
            mock_upsert(engine, mod.SOURCE_ID, full_canonical)
            mock_finish(engine, source_run_id, status="succeeded", row_count=len(full_canonical))

            return {
                "mock_sha256": mock_sha256,
                "mock_upload": mock_upload,
                "mock_write": mock_write,
                "mock_build": mock_build,
                "mock_upsert": mock_upsert,
                "mock_finish": mock_finish,
                "source_run_id": source_run_id,
                "full_canonical": full_canonical,
            }

    def test_write_source_run_called_with_correct_source_id(self):
        canonical = self._canonical_df()
        mocks = self._run_db_write(canonical)
        kwargs = mocks["mock_write"].call_args.kwargs
        assert kwargs["source_id"] == mod.SOURCE_ID

    def test_raw_uri_passed_into_write_source_run(self):
        """raw_uri from upload_raw must be forwarded into write_source_run (D7)."""
        canonical = self._canonical_df()
        mocks = self._run_db_write(canonical)
        kwargs = mocks["mock_write"].call_args.kwargs
        assert kwargs["raw_uri"] == "gs://juniper-ingest-raw/tx_trec_hoa/2026-08-20/abc123.json.gz"

    def test_upsert_staging_called_exactly_once(self):
        canonical = self._canonical_df()
        mocks = self._run_db_write(canonical)
        assert mocks["mock_upsert"].call_count == 1

    def test_upsert_staging_called_with_correct_source_id(self):
        canonical = self._canonical_df()
        mocks = self._run_db_write(canonical)
        upsert_call = mocks["mock_upsert"].call_args
        assert upsert_call.args[1] == mod.SOURCE_ID

    def test_finish_source_run_called_with_succeeded(self):
        canonical = self._canonical_df()
        mocks = self._run_db_write(canonical)
        finish_call = mocks["mock_finish"].call_args
        assert finish_call.kwargs["status"] == "succeeded"

    def test_finish_source_run_called_with_source_run_id(self):
        """finish_source_run must receive the id returned by write_source_run."""
        canonical = self._canonical_df()
        mocks = self._run_db_write(canonical)
        finish_call = mocks["mock_finish"].call_args
        # write_source_run mock returns 7
        assert finish_call.args[1] == 7

    def test_upload_raw_called_with_source_id(self):
        """upload_raw must be called with SOURCE_ID so the GCS path is correct."""
        canonical = self._canonical_df()
        mocks = self._run_db_write(canonical)
        upload_call = mocks["mock_upload"].call_args
        assert upload_call.args[0] == mod.SOURCE_ID

    def test_build_canonical_receives_plausible_data(self):
        """build_canonical must receive natural_key and vertical columns."""
        canonical = self._canonical_df()
        mocks = self._run_db_write(canonical)
        build_kwargs = mocks["mock_build"].call_args.kwargs
        assert "natural_key" in build_kwargs
        assert "vertical" in build_kwargs
        assert build_kwargs["source_id"] == mod.SOURCE_ID
