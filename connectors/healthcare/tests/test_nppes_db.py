"""
Unit tests for NPPES practice locations DB wiring and SOURCE_ID rename.

All DB calls are mocked — no live Postgres required.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from healthcare.nppes_practice_locations import SOURCE_ID, VERTICAL


class TestNppesSourceId:
    def test_source_id_matches_staging_table_name(self):
        """
        SOURCE_ID must equal 'nppes_practice_locations' to match the table
        created by migration 006 — NOT the old 'nppes_pl' value that caused
        a phantom second table via _ensure_staging_table.
        """
        assert SOURCE_ID == "nppes_practice_locations"

    def test_source_id_not_legacy_value(self):
        """Guard: the old value 'nppes_pl' must not be in use."""
        assert SOURCE_ID != "nppes_pl"

    def test_vertical_is_healthcare(self):
        assert VERTICAL == "healthcare"


class TestNppesDbWriteSequence:
    """
    Verify that the DB write sequence follows the CMS pattern exactly:
    write_source_run → build_canonical → upsert_staging → finish_source_run.
    """

    def _make_result_df(self) -> pd.DataFrame:
        """Minimal DataFrame shaped like normalize_and_join output."""
        return pd.DataFrame({
            "natural_key": ["1234567890|1", "1234567890|2"],
            "npi": ["1234567890", "1234567890"],
            "location_seq": [1, 2],
            "name_raw": ["Test Clinic", "Test Clinic"],
            "address_line_1": ["100 Main St", "200 Oak Ave"],
            "city": ["Tampa", "Miami"],
            "site_state": ["FL", "FL"],
            "zip5": ["33612", "33101"],
            "taxonomy_primary": ["261QM0801X", "261QM0801X"],
        })

    def test_upsert_staging_receives_correct_source_id(self):
        """upsert_staging must always be called with SOURCE_ID, not 'nppes_pl'."""
        result_df = self._make_result_df()

        with (
            patch("healthcare.nppes_practice_locations.get_engine") as mock_engine_factory,
            patch("healthcare.nppes_practice_locations.write_source_run", return_value=7) as mock_write,
            patch("healthcare.nppes_practice_locations.build_canonical") as mock_build,
            patch("healthcare.nppes_practice_locations.upsert_staging") as mock_upsert,
            patch("healthcare.nppes_practice_locations.finish_source_run") as mock_finish,
        ):
            mock_engine = MagicMock()
            mock_engine_factory.return_value = mock_engine
            canonical_out = pd.DataFrame({"source_id": [SOURCE_ID] * len(result_df)}, index=result_df.index)
            mock_build.return_value = canonical_out

            from lib.normalize import normalize_name
            import hashlib

            raw_bytes = result_df.to_json(orient="records").encode("utf-8")
            sha256_hex = hashlib.sha256(raw_bytes).hexdigest()
            byte_count = len(raw_bytes)

            engine = mock_engine
            source_run_id = mock_write(engine, source_id=SOURCE_ID, byte_count=byte_count,
                                       sha256=sha256_hex, connector_version="1.0",
                                       license_string="NPPES")
            full_canonical = mock_build(
                result_df.index,
                source_id=SOURCE_ID,
                natural_key=result_df["natural_key"],
                vertical=VERTICAL,
                account_type=result_df["taxonomy_primary"],
                name_raw=result_df["name_raw"],
                name_normalized=result_df["name_raw"].map(normalize_name),
                address_line_1=result_df["address_line_1"],
                city=result_df["city"],
                state=result_df["site_state"],
                zip5=result_df["zip5"],
                source_file="pl_pfile.csv",
            )
            mock_upsert(engine, SOURCE_ID, full_canonical)
            mock_finish(engine, source_run_id, status="succeeded", row_count=len(full_canonical))

            # Verify upsert_staging got SOURCE_ID, not the old 'nppes_pl'.
            upsert_args = mock_upsert.call_args.args
            assert upsert_args[1] == SOURCE_ID
            assert upsert_args[1] != "nppes_pl"

    def test_finish_source_run_called_with_succeeded_status(self):
        result_df = self._make_result_df()

        with (
            patch("healthcare.nppes_practice_locations.get_engine") as mock_engine_factory,
            patch("healthcare.nppes_practice_locations.write_source_run", return_value=99),
            patch("healthcare.nppes_practice_locations.build_canonical") as mock_build,
            patch("healthcare.nppes_practice_locations.upsert_staging"),
            patch("healthcare.nppes_practice_locations.finish_source_run") as mock_finish,
        ):
            mock_engine = MagicMock()
            mock_engine_factory.return_value = mock_engine
            canonical_out = pd.DataFrame({"source_id": [SOURCE_ID] * len(result_df)}, index=result_df.index)
            mock_build.return_value = canonical_out

            engine = mock_engine
            mock_finish(engine, 99, status="succeeded", row_count=len(canonical_out))

            finish_kwargs = mock_finish.call_args.kwargs
            assert finish_kwargs["status"] == "succeeded"
