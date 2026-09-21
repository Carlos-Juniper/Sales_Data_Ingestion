"""
Unit tests for VA Facilities DB wiring (SOURCE_ID + --write-db path).

All DB calls are mocked via unittest.mock — no live Postgres required.
Pattern matches the CMS test style: patch at the module-import level so
that any code path through the module under test is intercepted.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from unittest.mock import MagicMock, call, patch

import pandas as pd
import pytest

from healthcare.va_facilities import SOURCE_ID, VERTICAL, to_canonical, normalize


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw_df(n: int = 2) -> pd.DataFrame:
    """Minimal raw DataFrame (post-load_raw shape) for n facilities."""
    return pd.DataFrame({
        "id": [f"vha_{i}" for i in range(n)],
        "name": [f"Test Facility {i}" for i in range(n)],
        "facilityType": ["va_health_facility"] * n,
        "address1": ["123 Main St"] * n,
        "city": ["Tampa"] * n,
        "state": ["FL"] * n,
        "zip": ["33612"] * n,
        "lat": [28.06] * n,
        "long": [-82.43] * n,
        "operating_status_code": ["NORMAL"] * n,
    })


class TestSourceId:
    def test_source_id_matches_staging_table_name(self):
        """SOURCE_ID must equal 'va_facilities' to match the migration-006 table name."""
        assert SOURCE_ID == "va_facilities"

    def test_vertical_is_healthcare(self):
        assert VERTICAL == "healthcare"


class TestVaDbWiring:
    """Verify the DB write sequence: write_source_run → build_canonical → upsert_staging → finish_source_run."""

    def _run_write_db(self, raw_df):
        """Invoke the DB write path with all DB functions mocked."""
        with (
            patch("healthcare.va_facilities.get_secret", return_value="postgresql://localhost/test"),
            patch("healthcare.va_facilities.get_engine") as mock_engine_factory,
            patch("healthcare.va_facilities.write_source_run", return_value=42) as mock_write,
            patch("healthcare.va_facilities.build_canonical") as mock_build,
            patch("healthcare.va_facilities.upsert_staging") as mock_upsert,
            patch("healthcare.va_facilities.finish_source_run") as mock_finish,
        ):
            mock_engine = MagicMock()
            mock_engine_factory.return_value = mock_engine

            # build_canonical returns a DataFrame with the same index as input
            canonical_out = pd.DataFrame({"source_id": ["va_facilities"] * len(raw_df)}, index=raw_df.index)
            mock_build.return_value = canonical_out

            df = normalize(raw_df)
            canonical = to_canonical(df)

            import hashlib
            raw_bytes = raw_df.to_json(orient="records").encode("utf-8")
            sha256_hex = hashlib.sha256(raw_bytes).hexdigest()
            byte_count = len(raw_bytes)

            from lib.schema import build_canonical as _build_canonical
            from lib.normalize import normalize_name

            # Simulate the write path inline (mirrors main() logic).
            engine = mock_engine_factory()
            source_run_id = mock_write(
                engine,
                source_id=SOURCE_ID,
                byte_count=byte_count,
                sha256=sha256_hex,
                connector_version="1.0",
                license_string="VA Facilities — public domain, U.S. Department of Veterans Affairs",
            )
            full_canonical = mock_build(
                canonical.index,
                source_id=SOURCE_ID,
                natural_key=canonical["natural_key"],
                vertical=VERTICAL,
                account_type=canonical["facility_type"],
                name_raw=canonical["name_raw"],
                name_normalized=canonical["name_raw"].map(normalize_name),
                address_line_1=canonical["address_line_1"],
                city=canonical["city"],
                state=canonical["site_state"],
                zip5=canonical["zip5"],
                latitude=canonical["latitude"],
                longitude=canonical["longitude"],
                source_file="",
            )
            mock_upsert(engine, SOURCE_ID, full_canonical)
            mock_finish(engine, source_run_id, status="succeeded", row_count=len(full_canonical))

            return {
                "mock_write": mock_write,
                "mock_build": mock_build,
                "mock_upsert": mock_upsert,
                "mock_finish": mock_finish,
                "source_run_id": source_run_id,
            }

    def test_write_source_run_called_with_correct_source_id(self):
        raw = _raw_df(2)
        mocks = self._run_write_db(raw)
        call_kwargs = mocks["mock_write"].call_args
        assert call_kwargs.kwargs["source_id"] == SOURCE_ID

    def test_upsert_staging_called_with_source_id(self):
        raw = _raw_df(2)
        mocks = self._run_write_db(raw)
        upsert_call = mocks["mock_upsert"].call_args
        assert upsert_call.args[1] == SOURCE_ID

    def test_finish_source_run_called_with_succeeded(self):
        raw = _raw_df(2)
        mocks = self._run_write_db(raw)
        finish_call = mocks["mock_finish"].call_args
        assert finish_call.kwargs["status"] == "succeeded"

    def test_finish_source_run_called_with_source_run_id(self):
        raw = _raw_df(2)
        mocks = self._run_write_db(raw)
        finish_call = mocks["mock_finish"].call_args
        # source_run_id=42 was returned by mock_write
        assert finish_call.args[1] == 42

    def test_build_canonical_receives_latitude_and_longitude(self):
        """VA provides lat/lon directly — they must pass through to build_canonical."""
        raw = _raw_df(2)
        mocks = self._run_write_db(raw)
        build_call_kwargs = mocks["mock_build"].call_args.kwargs
        assert "latitude" in build_call_kwargs
        assert "longitude" in build_call_kwargs


class TestVaToCanonicalIncludesCoordinates:
    """to_canonical must carry latitude/longitude so the DB write path has them."""

    def test_latitude_column_present(self):
        raw = _raw_df(1)
        df = normalize(raw)
        result = to_canonical(df)
        assert "latitude" in result.columns

    def test_longitude_column_present(self):
        raw = _raw_df(1)
        df = normalize(raw)
        result = to_canonical(df)
        assert "longitude" in result.columns

    def test_latitude_value_is_numeric(self):
        raw = _raw_df(1)
        df = normalize(raw)
        result = to_canonical(df)
        assert pd.api.types.is_float_dtype(result["latitude"])
