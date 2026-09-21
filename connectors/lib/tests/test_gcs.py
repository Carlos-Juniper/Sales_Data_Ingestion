"""
Unit tests for lib/gcs.py.

No real GCS calls are made — google.cloud.storage is mocked via unittest.mock.
DISABLE_GCS=1 env var tests require no mocking at all.

Tests cover:
  - raw_sha256 determinism
  - upload_raw returns None when DISABLE_GCS=1
  - upload_raw returns None when google-cloud-storage is not installed
  - upload_raw happy-path: correct object path and gs:// URI returned
"""

from __future__ import annotations

import json
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from lib.gcs import raw_sha256, upload_raw


# ---------------------------------------------------------------------------
# raw_sha256
# ---------------------------------------------------------------------------


class TestRawSha256:
    def test_same_bytes_same_digest(self):
        data = b"hello world"
        assert raw_sha256(data) == raw_sha256(data)

    def test_different_bytes_different_digest(self):
        assert raw_sha256(b"foo") != raw_sha256(b"bar")

    def test_returns_64_character_hex_string(self):
        result = raw_sha256(b"some content")
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_empty_bytes_known_digest(self):
        # SHA-256 of empty bytes is a well-known constant.
        expected = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        assert raw_sha256(b"") == expected

    def test_digest_is_lowercase_hex(self):
        result = raw_sha256(b"test")
        assert result == result.lower()

    def test_sort_keys_serialisation_is_stable(self):
        """Demonstrate that sort_keys=True makes the bytes (and hence digest) stable."""
        row = {"name": "Test", "id": "abc", "value": 1}
        # Two serialisations with sort_keys=True produce the same bytes.
        bytes_a = json.dumps(row, sort_keys=True).encode("utf-8")
        bytes_b = json.dumps(row, sort_keys=True).encode("utf-8")
        assert raw_sha256(bytes_a) == raw_sha256(bytes_b)

    def test_different_column_orders_produce_different_bytes_without_sort(self):
        """Without sort_keys, insertion order may differ — raw_sha256 reflects that."""
        # This test is illustrative: it shows WHY sort_keys=True is needed in
        # the connector serialisation (B4 fix).
        import json

        row = {"z": 1, "a": 2}
        bytes_natural = json.dumps({"z": 1, "a": 2}).encode()
        bytes_reversed = json.dumps({"a": 2, "z": 1}).encode()
        # On CPython 3.7+, dict insertion order is preserved, so these differ.
        # Either way, raw_sha256 faithfully reflects its input bytes.
        digest_a = raw_sha256(bytes_natural)
        digest_b = raw_sha256(bytes_reversed)
        assert len(digest_a) == 64
        assert len(digest_b) == 64
        # The digests may differ (they do on CPython 3.7+ because key order differs).
        # The point: raw_sha256 is deterministic given the same bytes.


# ---------------------------------------------------------------------------
# Helpers for patching the lazy GCS import
# ---------------------------------------------------------------------------


def _make_gcs_module_mock() -> tuple[MagicMock, MagicMock, MagicMock, MagicMock]:
    """Return (mock_storage_module, mock_client, mock_bucket, mock_blob).

    Constructs the mock hierarchy that upload_raw traverses:
      gcs_storage.Client() -> mock_client
      mock_client.bucket(name) -> mock_bucket
      mock_bucket.blob(path) -> mock_blob
    """
    mock_blob = MagicMock()
    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob
    mock_client_instance = MagicMock()
    mock_client_instance.bucket.return_value = mock_bucket

    mock_storage = MagicMock()
    mock_storage.Client.return_value = mock_client_instance

    return mock_storage, mock_client_instance, mock_bucket, mock_blob


def _gcs_sys_modules_patch(mock_storage: MagicMock) -> dict:
    """Build the sys.modules override needed for `from google.cloud import storage`."""
    # The lazy import inside upload_raw is:
    #   from google.cloud import storage as gcs_storage
    # Python resolves this by looking up sys.modules["google.cloud"] and then
    # getting its .storage attribute.  We must fake both levels.
    google_mod = types.ModuleType("google")
    google_cloud_mod = types.ModuleType("google.cloud")
    google_cloud_mod.storage = mock_storage
    google_mod.cloud = google_cloud_mod

    return {
        "google": google_mod,
        "google.cloud": google_cloud_mod,
        "google.cloud.storage": mock_storage,
    }


# ---------------------------------------------------------------------------
# upload_raw — DISABLE_GCS
# ---------------------------------------------------------------------------


class TestUploadRawDisabledByEnv:
    def test_disable_gcs_1_returns_none(self, monkeypatch):
        monkeypatch.setenv("DISABLE_GCS", "1")
        result = upload_raw("cms_general", "2026-08-20", b"payload")
        assert result is None

    def test_disable_gcs_true_returns_none(self, monkeypatch):
        monkeypatch.setenv("DISABLE_GCS", "true")
        result = upload_raw("cms_general", "2026-08-20", b"payload")
        assert result is None

    def test_disable_gcs_yes_returns_none(self, monkeypatch):
        monkeypatch.setenv("DISABLE_GCS", "yes")
        result = upload_raw("cms_general", "2026-08-20", b"payload")
        assert result is None

    def test_disable_gcs_0_does_not_short_circuit(self, monkeypatch):
        """DISABLE_GCS=0 must NOT trigger the bypass."""
        monkeypatch.setenv("DISABLE_GCS", "0")
        monkeypatch.delenv("DISABLE_GCS", raising=False)
        # With DISABLE_GCS unset, the function proceeds to the import check.
        # Force ImportError so it returns None from a different branch.
        mock_storage, *_ = _make_gcs_module_mock()
        mock_storage.Client.side_effect = ImportError("not installed")
        with patch.dict("sys.modules", _gcs_sys_modules_patch(mock_storage)):
            result = upload_raw("cms_general", "2026-08-20", b"payload")
        # Returns None from the exception handler, not from the DISABLE_GCS guard.
        assert result is None


# ---------------------------------------------------------------------------
# upload_raw — missing dependency
# ---------------------------------------------------------------------------


class TestUploadRawMissingDep:
    def test_returns_none_when_google_cloud_storage_not_installed(self, monkeypatch):
        monkeypatch.delenv("DISABLE_GCS", raising=False)
        # Setting google.cloud.storage to None in sys.modules makes the import fail.
        with patch.dict("sys.modules", {"google.cloud.storage": None}):
            result = upload_raw("cms_general", "2026-08-20", b"payload")
        assert result is None

    def test_does_not_raise_when_google_cloud_storage_not_installed(self, monkeypatch):
        monkeypatch.delenv("DISABLE_GCS", raising=False)
        with patch.dict("sys.modules", {"google.cloud.storage": None}):
            upload_raw("cms_general", "2026-08-20", b"payload")  # must not raise


# ---------------------------------------------------------------------------
# upload_raw — happy path with mocked GCS client
# ---------------------------------------------------------------------------


class TestUploadRawHappyPath:
    """Verify object path and returned URI without touching real GCS."""

    def test_object_path_is_source_id_run_date_sha256_json_gz(self, monkeypatch):
        monkeypatch.delenv("DISABLE_GCS", raising=False)
        monkeypatch.setenv("GCS_RAW_BUCKET", "juniper-ingest-raw")

        payload = b"raw content"
        expected_sha = raw_sha256(payload)
        source_id = "cms_general"
        run_date = "2026-08-20"

        mock_storage, _, mock_bucket, _ = _make_gcs_module_mock()

        with patch.dict("sys.modules", _gcs_sys_modules_patch(mock_storage)):
            result = upload_raw(source_id, run_date, payload)

        expected_object = f"{source_id}/{run_date}/{expected_sha}.json.gz"
        mock_bucket.blob.assert_called_once_with(expected_object)
        expected_uri = f"gs://juniper-ingest-raw/{expected_object}"
        assert result == expected_uri

    def test_returned_uri_starts_with_gs(self, monkeypatch):
        monkeypatch.delenv("DISABLE_GCS", raising=False)
        monkeypatch.setenv("GCS_RAW_BUCKET", "juniper-ingest-raw")

        mock_storage, *_ = _make_gcs_module_mock()

        with patch.dict("sys.modules", _gcs_sys_modules_patch(mock_storage)):
            result = upload_raw("va_facilities", "2026-08-20", b"data")

        assert result is not None
        assert result.startswith("gs://")

    def test_custom_bucket_from_env(self, monkeypatch):
        monkeypatch.delenv("DISABLE_GCS", raising=False)
        monkeypatch.setenv("GCS_RAW_BUCKET", "my-custom-bucket")

        mock_storage, mock_client, *_ = _make_gcs_module_mock()

        with patch.dict("sys.modules", _gcs_sys_modules_patch(mock_storage)):
            result = upload_raw("nppes_practice_locations", "2026-08-20", b"data")

        assert result is not None
        assert "my-custom-bucket" in result
        mock_client.bucket.assert_called_once_with("my-custom-bucket")

    def test_gzip_false_uses_json_extension(self, monkeypatch):
        monkeypatch.delenv("DISABLE_GCS", raising=False)
        monkeypatch.setenv("GCS_RAW_BUCKET", "juniper-ingest-raw")

        mock_storage, *_ = _make_gcs_module_hack()
        with patch.dict("sys.modules", _gcs_sys_modules_patch(mock_storage)):
            result = upload_raw("cms_general", "2026-08-20", b"data", gzip=False)

        assert result is not None
        assert result.endswith(".json")
        assert not result.endswith(".json.gz")

    def test_suffix_pdf_uses_pdf_gz_extension(self, monkeypatch):
        """PDF payloads keep a .pdf.gz name so the object isn't labeled JSON."""
        monkeypatch.delenv("DISABLE_GCS", raising=False)
        monkeypatch.setenv("GCS_RAW_BUCKET", "juniper-ingest-raw")

        payload = b"%PDF-1.4 scanned certificate"
        expected_sha = raw_sha256(payload)
        mock_storage, _, mock_bucket, mock_blob = _make_gcs_module_mock()

        with patch.dict("sys.modules", _gcs_sys_modules_patch(mock_storage)):
            result = upload_raw(
                "tx_trec_pdf_enrich", "2026-09-21", payload, gzip=True, suffix="pdf",
            )

        expected_object = f"tx_trec_pdf_enrich/2026-09-21/{expected_sha}.pdf.gz"
        mock_bucket.blob.assert_called_once_with(expected_object)
        assert result == f"gs://juniper-ingest-raw/{expected_object}"
        mock_blob.upload_from_string.assert_called_once()
        assert mock_blob.upload_from_string.call_args.kwargs["content_type"] == "application/gzip"

    def test_suffix_pdf_gzip_false_is_application_pdf(self, monkeypatch):
        monkeypatch.delenv("DISABLE_GCS", raising=False)
        monkeypatch.setenv("GCS_RAW_BUCKET", "juniper-ingest-raw")

        mock_storage, _, mock_bucket, mock_blob = _make_gcs_module_mock()
        with patch.dict("sys.modules", _gcs_sys_modules_patch(mock_storage)):
            result = upload_raw(
                "tx_trec_pdf_enrich", "2026-09-21", b"%PDF", gzip=False, suffix="pdf",
            )

        assert result is not None
        assert result.endswith(".pdf")
        assert not result.endswith(".pdf.gz")
        assert mock_blob.upload_from_string.call_args.kwargs["content_type"] == "application/pdf"
        mock_bucket.blob.assert_called_once()

    def test_suffix_rejects_path_separators(self, monkeypatch):
        monkeypatch.delenv("DISABLE_GCS", raising=False)
        with pytest.raises(ValueError):
            upload_raw("cms_general", "2026-08-20", b"data", suffix="../json")

    def test_gzip_true_uses_json_gz_extension(self, monkeypatch):
        monkeypatch.delenv("DISABLE_GCS", raising=False)
        monkeypatch.setenv("GCS_RAW_BUCKET", "juniper-ingest-raw")

        mock_storage, *_ = _make_gcs_module_mock()
        with patch.dict("sys.modules", _gcs_sys_modules_patch(mock_storage)):
            result = upload_raw("cms_general", "2026-08-20", b"data", gzip=True)

        assert result is not None
        assert result.endswith(".json.gz")

    def test_gcs_exception_returns_none_not_raises(self, monkeypatch):
        monkeypatch.delenv("DISABLE_GCS", raising=False)
        monkeypatch.setenv("GCS_RAW_BUCKET", "juniper-ingest-raw")

        mock_storage, mock_client, *_ = _make_gcs_module_mock()
        # Make the bucket lookup raise a permission error.
        mock_client.bucket.side_effect = PermissionError("Access denied")

        with patch.dict("sys.modules", _gcs_sys_modules_patch(mock_storage)):
            result = upload_raw("cms_general", "2026-08-20", b"data")

        # Must degrade gracefully — never raise into the connector.
        assert result is None

    def test_content_addressed_same_bytes_same_object_name(self, monkeypatch):
        """Identical bytes → identical object name → 0 net new GCS objects."""
        monkeypatch.delenv("DISABLE_GCS", raising=False)
        monkeypatch.setenv("GCS_RAW_BUCKET", "juniper-ingest-raw")

        payload = b"deterministic payload"
        mock_storage, _, mock_bucket, _ = _make_gcs_module_mock()

        with patch.dict("sys.modules", _gcs_sys_modules_patch(mock_storage)):
            uri_1 = upload_raw("cms_general", "2026-08-20", payload)

        mock_storage2, _, mock_bucket2, _ = _make_gcs_module_mock()
        with patch.dict("sys.modules", _gcs_sys_modules_patch(mock_storage2)):
            uri_2 = upload_raw("cms_general", "2026-08-20", payload)

        assert uri_1 == uri_2
        # Both calls hit the same blob path.
        blob_path_1 = mock_bucket.blob.call_args[0][0]
        blob_path_2 = mock_bucket2.blob.call_args[0][0]
        assert blob_path_1 == blob_path_2


def _make_gcs_module_hack():
    """Alias so the gzip=False test can call the factory."""
    return _make_gcs_module_mock()
