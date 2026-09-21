"""
Tests for tx_trec_pdf_enrich.py — TX TREC certificate PDF OCR enrichment.

OCR and HTTP are mocked. The parser is exercised on the Trinity Estates
field layout (numbered labels 5/6/7) and on the failure modes the review
queue exists for: missing anchors, label-text fallback, missing phone or
email. No tesseract binary and no network.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tx_trec_pdf_enrich as mod

# Trinity Estates / Tarrant County layout, fields 5–7 on page 1.
TRINITY_OCR = """\
MANAGEMENT CERTIFICATE

1. Name of the Subdivision
Trinity Estates

2. Name of the Association
Trinity Estates POA, Inc.

5. Name and mailing address of the Association
Trinity Estates POA, Inc., c/o Goodwin & Company, PO Box 203310, Austin, TX 78720

6. Name, mailing address, phone number & email for designated representative
Goodwin & Company
PO Box 203310, Austin, TX 78720
855.289.6007
info@goodwin-co.com

7. Website address where all dedicatory instruments can be found
https://dtrin.sites.townsq.io/
https://goodwin-co.com/

8. Other information the Association considers appropriate
Filed by the management company.
"""

SINGLE_LINE_FIELD_6 = """\
5. Name and mailing address of the Association
Trinity Estates POA, Inc., c/o Goodwin & Company, PO Box 203310, Austin, TX 78720

6. Name, mailing address, phone number & email for designated representative
Goodwin & Company, PO Box 203310, Austin, TX 78720, 855.289.6007, info@goodwin-co.com

7. Website address where all dedicatory instruments can be found
https://goodwin-co.com
"""

# Numbered labels missing; the statutory phrases are still on the page.
LABEL_FALLBACK_OCR = """\
Name and mailing address of the Association
Trinity Estates POA, Inc., PO Box 203310, Austin, TX 78720

Name, mailing address, phone number & email for designated representative
Goodwin & Company
PO Box 203310, Austin, TX 78720
855.289.6007
info@goodwin-co.com
"""


def _queue_row(**overrides) -> dict:
    base = {
        "association_id": "123456",
        "certificate_id": "51-253",
        "url": "https://data.texas.gov/certificates/123456/51-253/mc/cert.pdf",
        "Name": "Trinity Estates POA",
        "county_primary": "TARRANT",
        "city": "FORT WORTH",
        "zip5": "76101",
        "priority": "5",
        "fetch_status": "pending",
        "parse_status": "pending",
    }
    base.update(overrides)
    return base


def _queue_df(*rows: dict) -> pd.DataFrame:
    if not rows:
        rows = (_queue_row(),)
    return pd.DataFrame(list(rows))


def _pdf_ok(body: bytes = b"%PDF-1.4 fake") -> dict:
    return {
        "pdf_bytes": body,
        "status": "ok",
        "error_detail": None,
        "review_reasons": None,
    }


# ===========================================================================
# Parser
# ===========================================================================


class TestParseTrinity:
    def test_field_5_association_mailing_address(self):
        parsed = mod.parse_certificate_text(TRINITY_OCR)
        assert parsed["assoc_mailing_address"] == (
            "Trinity Estates POA, Inc., c/o Goodwin & Company, "
            "PO Box 203310, Austin, TX 78720"
        )

    def test_field_6_rep_name_address_phone_email(self):
        parsed = mod.parse_certificate_text(TRINITY_OCR)
        assert parsed["rep_name"] == "Goodwin & Company"
        assert parsed["rep_mailing_address"] == "PO Box 203310, Austin, TX 78720"
        assert parsed["rep_phone"] == "855.289.6007"
        assert parsed["rep_phone_normalized"] == "8552896007"
        assert parsed["rep_email"] == "info@goodwin-co.com"

    def test_field_7_keeps_both_urls(self):
        parsed = mod.parse_certificate_text(TRINITY_OCR)
        assert parsed["website"] == (
            "https://dtrin.sites.townsq.io/ | https://goodwin-co.com/"
        )

    def test_clean_template_is_ok_and_not_queued(self):
        parsed = mod.parse_certificate_text(TRINITY_OCR)
        assert parsed["enrich_status"] == "ok"
        assert parsed["needs_review"] is False
        assert parsed["review_reasons"] is None
        assert parsed["confidence"] == 1.0

    def test_reparse_is_deterministic(self):
        assert mod.parse_certificate_text(TRINITY_OCR) == mod.parse_certificate_text(TRINITY_OCR)

    def test_single_line_field_6_splits_name_from_address(self):
        parsed = mod.parse_certificate_text(SINGLE_LINE_FIELD_6)
        assert parsed["rep_name"] == "Goodwin & Company"
        assert parsed["rep_mailing_address"] == "PO Box 203310, Austin, TX 78720"
        assert parsed["rep_email"] == "info@goodwin-co.com"
        assert parsed["needs_review"] is False

    def test_parenthesized_phone_and_spaced_dots(self):
        text = TRINITY_OCR.replace("855.289.6007", "(855) 289-6007")
        parsed = mod.parse_certificate_text(text)
        assert parsed["rep_phone_normalized"] == "8552896007"

        spaced = TRINITY_OCR.replace("855.289.6007", "855. 289. 6007")
        parsed_spaced = mod.parse_certificate_text(spaced)
        assert parsed_spaced["rep_phone_normalized"] == "8552896007"

    def test_ocr_punctuation_variants_still_anchor(self):
        text = (
            TRINITY_OCR
            .replace("5.", "5)", 1)
            .replace("6.", "6:", 1)
            .replace("7.", "7,", 1)
        )
        parsed = mod.parse_certificate_text(text)
        assert parsed["rep_email"] == "info@goodwin-co.com"
        assert parsed["needs_review"] is False


class TestParseReviewFlags:
    def test_empty_text_is_not_found_and_queued(self):
        parsed = mod.parse_certificate_text("   ")
        assert parsed["enrich_status"] == "not_found"
        assert parsed["needs_review"] is True
        assert parsed["review_reasons"] == "empty_ocr"
        assert parsed["confidence"] == 0.0

    def test_none_text_is_not_found(self):
        parsed = mod.parse_certificate_text(None)
        assert parsed["enrich_status"] == "not_found"
        assert parsed["rep_phone"] is None

    def test_missing_anchors_is_not_found(self):
        parsed = mod.parse_certificate_text("This page has no numbered fields at all.\n")
        assert parsed["enrich_status"] == "not_found"
        assert parsed["needs_review"] is True
        assert "missing_field_5_anchor" in parsed["review_reasons"]
        assert "missing_field_6_anchor" in parsed["review_reasons"]

    def test_missing_phone_is_ok_but_queued(self):
        text = TRINITY_OCR.replace("855.289.6007", "")
        parsed = mod.parse_certificate_text(text)
        assert parsed["enrich_status"] == "ok"
        assert parsed["rep_phone"] is None
        assert parsed["rep_email"] == "info@goodwin-co.com"
        assert parsed["needs_review"] is True
        assert "missing_phone" in parsed["review_reasons"]
        assert parsed["confidence"] < 1.0

    def test_missing_email_is_ok_but_queued(self):
        text = TRINITY_OCR.replace("info@goodwin-co.com", "")
        parsed = mod.parse_certificate_text(text)
        assert parsed["enrich_status"] == "ok"
        assert parsed["rep_email"] is None
        assert parsed["needs_review"] is True
        assert "missing_email" in parsed["review_reasons"]

    def test_missing_field_6_anchor_does_not_invent_a_rep(self):
        text = """\
5. Name and mailing address of the Association
Trinity Estates POA, Inc., PO Box 203310, Austin, TX 78720

7. Website address where all dedicatory instruments can be found
https://example.com
"""
        parsed = mod.parse_certificate_text(text)
        assert parsed["enrich_status"] == "ok"
        assert parsed["rep_name"] is None
        assert parsed["rep_phone"] is None
        assert parsed["needs_review"] is True
        assert "missing_field_6_anchor" in parsed["review_reasons"]
        # Phone/email flags apply only when a field-6 block exists.
        assert "missing_phone" not in (parsed["review_reasons"] or "")

    def test_label_fallback_extracts_but_stays_on_the_review_queue(self):
        parsed = mod.parse_certificate_text(LABEL_FALLBACK_OCR)
        assert parsed["enrich_status"] == "ok"
        assert parsed["rep_name"] == "Goodwin & Company"
        assert parsed["rep_email"] == "info@goodwin-co.com"
        assert "Trinity Estates POA, Inc." in parsed["assoc_mailing_address"]
        assert parsed["needs_review"] is True
        assert "field_5_label_fallback" in parsed["review_reasons"]
        assert "field_6_label_fallback" in parsed["review_reasons"]
        assert "missing_field_5_anchor" in parsed["review_reasons"]
        # Label fallback scores below a clean numbered template.
        assert parsed["confidence"] < 1.0
        # The field-6 label line must not be glued onto the association address.
        assert "designated representative" not in parsed["assoc_mailing_address"].lower()

    def test_anchors_out_of_order_are_flagged(self):
        text = """\
6. Name, mailing address, phone number & email for designated representative
Goodwin & Company
855.289.6007
info@goodwin-co.com

5. Name and mailing address of the Association
Trinity Estates POA, Inc., PO Box 203310, Austin, TX 78720
"""
        parsed = mod.parse_certificate_text(text)
        assert "anchor_order" in parsed["review_reasons"]
        assert parsed["needs_review"] is True


# ===========================================================================
# Fetch
# ===========================================================================


class TestFetchPdf:
    def test_ok_pdf_returns_bytes_and_consumes_one_slot(self):
        session = MagicMock()
        response = MagicMock()
        response.status_code = 200
        response.content = b"%PDF-1.4\nbody"
        response.raise_for_status = MagicMock()
        session.get.return_value = response
        limiter = MagicMock()

        fetched = mod.fetch_pdf("https://example.test/a.pdf", session, limiter)

        assert fetched["status"] == "ok"
        assert fetched["pdf_bytes"].startswith(b"%PDF")
        limiter.acquire.assert_called_once()
        session.get.assert_called_once()

    def test_404_is_not_found(self):
        session = MagicMock()
        response = MagicMock()
        response.status_code = 404
        session.get.return_value = response
        limiter = MagicMock()

        fetched = mod.fetch_pdf("https://example.test/missing.pdf", session, limiter)

        assert fetched["status"] == "not_found"
        assert fetched["pdf_bytes"] is None
        assert fetched["review_reasons"] == "http_404"
        limiter.acquire.assert_called_once()

    def test_http_500_is_error(self):
        session = MagicMock()
        response = MagicMock()
        response.status_code = 500
        response.raise_for_status.side_effect = requests.HTTPError("500")
        response.raise_for_status.side_effect.response = response
        session.get.return_value = response

        fetched = mod.fetch_pdf("https://example.test/a.pdf", session, MagicMock())

        assert fetched["status"] == "error"
        assert "HTTPError" in fetched["error_detail"]

    def test_html_body_is_not_pdf(self):
        session = MagicMock()
        response = MagicMock()
        response.status_code = 200
        response.content = b"<html>not a certificate</html>"
        response.raise_for_status = MagicMock()
        session.get.return_value = response

        fetched = mod.fetch_pdf("https://example.test/a.pdf", session, MagicMock())

        assert fetched["status"] == "error"
        assert fetched["review_reasons"] == "not_pdf"

    def test_oversize_body_is_rejected(self):
        session = MagicMock()
        response = MagicMock()
        response.status_code = 200
        response.content = b"%PDF" + b"x" * (mod._MAX_PDF_BYTES + 1)
        response.raise_for_status = MagicMock()
        session.get.return_value = response

        fetched = mod.fetch_pdf("https://example.test/a.pdf", session, MagicMock())

        assert fetched["review_reasons"] == "pdf_too_large"
        assert fetched["pdf_bytes"] is None


class TestRateLimiter:
    def test_second_acquire_waits_the_interval(self):
        now = {"t": 0.0}
        slept: list[float] = []

        def clock() -> float:
            return now["t"]

        def sleeper(seconds: float) -> None:
            slept.append(seconds)
            now["t"] += seconds

        limiter = mod.RateLimiter(1.0, clock=clock, sleeper=sleeper)
        limiter.acquire()
        limiter.acquire()
        limiter.acquire()

        assert slept == [1.0, 1.0]

    def test_zero_interval_does_not_sleep(self):
        sleeper = MagicMock()
        limiter = mod.RateLimiter(0, sleeper=sleeper)
        limiter.acquire()
        sleeper.assert_not_called()


# ===========================================================================
# process_certificate / enrich
# ===========================================================================


class TestProcessCertificate:
    def _run(self, **kwargs):
        defaults = dict(
            row=_queue_row(),
            session=MagicMock(),
            rate_limiter=MagicMock(),
            land_raw=True,
            run_date="2026-09-21",
        )
        defaults.update(kwargs)
        return mod.process_certificate(**defaults)

    def test_ok_path_parses_and_lands_pdf(self):
        with (
            patch.object(mod, "fetch_pdf", return_value=_pdf_ok(b"%PDF-1.4 trinity")),
            patch.object(mod, "ocr_pdf", return_value=TRINITY_OCR),
            patch.object(mod, "upload_raw", return_value="gs://juniper-ingest-raw/tx_trec_pdf_enrich/2026-09-21/abc.pdf.gz") as upload,
        ):
            result = self._run()

        assert result["enrich_status"] == "ok"
        assert result["source_id"] == "tx_trec_hoa"
        assert result["natural_key"] == "123456"
        assert result["rep_email"] == "info@goodwin-co.com"
        assert result["needs_review"] is False
        assert result["raw_pdf_uri"].endswith(".pdf.gz")
        assert result["raw_pdf_sha256"]
        assert result["ocr_text_sha256"]
        upload.assert_called_once()
        assert upload.call_args.args[0] == mod.CONNECTOR_ID
        assert upload.call_args.kwargs["suffix"] == "pdf"
        assert upload.call_args.kwargs["gzip"] is True

    def test_land_raw_false_does_not_upload(self):
        with (
            patch.object(mod, "fetch_pdf", return_value=_pdf_ok()),
            patch.object(mod, "ocr_pdf", return_value=TRINITY_OCR),
            patch.object(mod, "upload_raw") as upload,
        ):
            result = self._run(land_raw=False)

        upload.assert_not_called()
        assert result["raw_pdf_uri"] is None
        assert result["raw_pdf_sha256"]

    def test_404_does_not_ocr_or_upload(self):
        with (
            patch.object(mod, "fetch_pdf", return_value={
                "pdf_bytes": None,
                "status": "not_found",
                "error_detail": "HTTP 404",
                "review_reasons": "http_404",
            }),
            patch.object(mod, "ocr_pdf") as ocr,
            patch.object(mod, "upload_raw") as upload,
        ):
            result = self._run()

        ocr.assert_not_called()
        upload.assert_not_called()
        assert result["enrich_status"] == "not_found"
        assert result["needs_review"] is True

    def test_ocr_failure_keeps_the_landed_pdf(self):
        with (
            patch.object(mod, "fetch_pdf", return_value=_pdf_ok()),
            patch.object(mod, "ocr_pdf", side_effect=RuntimeError("tesseract missing")),
            patch.object(mod, "upload_raw", return_value="gs://bucket/a.pdf.gz"),
        ):
            result = self._run()

        assert result["enrich_status"] == "error"
        assert "ocr_error" in result["review_reasons"]
        assert result["needs_review"] is True
        assert result["raw_pdf_uri"] == "gs://bucket/a.pdf.gz"
        assert "tesseract missing" in result["error_detail"]

    def test_floatish_association_id_is_normalized(self):
        with (
            patch.object(mod, "fetch_pdf", return_value=_pdf_ok()),
            patch.object(mod, "ocr_pdf", return_value=TRINITY_OCR),
            patch.object(mod, "upload_raw", return_value=None),
        ):
            result = self._run(row=_queue_row(association_id="123456.0"))

        assert result["natural_key"] == "123456"


class TestEnrich:
    def test_blank_url_is_skipped_without_fetch(self):
        df = _queue_df(_queue_row(url=""))
        with patch.object(mod, "process_certificate") as process:
            out = mod.enrich(df, sleep_s=0, land_raw=False)
        process.assert_not_called()
        assert out["enrich_status"].iloc[0] == "skipped"
        assert out["review_reasons"].iloc[0] == "missing_url"
        assert out["needs_review"].iloc[0] is False

    def test_blank_association_id_is_skipped(self):
        df = _queue_df(_queue_row(association_id=""))
        with patch.object(mod, "process_certificate") as process:
            out = mod.enrich(df, sleep_s=0, land_raw=False)
        process.assert_not_called()
        assert out["review_reasons"].iloc[0] == "missing_association_id"

    def test_cached_certificate_is_not_refetched(self):
        df = _queue_df(_queue_row())
        with patch.object(mod, "process_certificate") as process:
            out = mod.enrich(
                df,
                sleep_s=0,
                land_raw=False,
                skip_keys={("123456", "51-253")},
            )
        process.assert_not_called()
        assert out["enrich_status"].iloc[0] == "skipped"
        assert out["review_reasons"].iloc[0] == "cached"

    def test_changed_certificate_id_is_not_a_cache_hit(self):
        df = _queue_df(_queue_row(certificate_id="99-1"))
        with patch.object(mod, "process_certificate", return_value={
            "source_id": "tx_trec_hoa",
            "natural_key": "123456",
            "certificate_id": "99-1",
            "certificate_url": "https://example.test/a.pdf",
            "enrich_status": "ok",
            "needs_review": False,
            "review_reasons": None,
            "confidence": 1.0,
            "rep_email": "info@goodwin-co.com",
        }) as process:
            out = mod.enrich(
                df,
                sleep_s=0,
                land_raw=False,
                skip_keys={("123456", "51-253")},
            )
        process.assert_called_once()
        assert out["enrich_status"].iloc[0] == "ok"
        assert out["rep_email"].iloc[0] == "info@goodwin-co.com"

    def test_workers_keep_results_on_the_right_row(self):
        df = _queue_df(
            _queue_row(association_id="111", certificate_id="a", url="https://example.test/a.pdf"),
            _queue_row(association_id="222", certificate_id="b", url="https://example.test/b.pdf"),
            _queue_row(association_id="333", certificate_id="c", url="https://example.test/c.pdf"),
        )

        def _fake(row, **kwargs):  # noqa: ARG001
            return {
                "source_id": "tx_trec_hoa",
                "natural_key": row["association_id"],
                "certificate_id": row["certificate_id"],
                "certificate_url": row["url"],
                "enrich_status": "ok",
                "needs_review": False,
                "review_reasons": None,
                "confidence": 1.0,
                "rep_email": f"{row['association_id']}@example.test",
            }

        with patch.object(mod, "process_certificate", side_effect=_fake):
            out = mod.enrich(df, workers=2, sleep_s=0, land_raw=False)

        assert list(out["rep_email"]) == [
            "111@example.test",
            "222@example.test",
            "333@example.test",
        ]
        assert list(out["natural_key"]) == ["111", "222", "333"]

    def test_missing_queue_column_raises(self):
        with pytest.raises(ValueError, match="association_id"):
            mod.enrich(pd.DataFrame([{"url": "https://example.test"}]), sleep_s=0)

    def test_county_and_limit_filters(self):
        df = _queue_df(
            _queue_row(association_id="1", county_primary="HARRIS"),
            _queue_row(association_id="2", county_primary="Dallas"),
            _queue_row(association_id="3", county_primary="HARRIS"),
        )
        filtered = mod._filter_queue(df, county="harris", limit=1)
        assert list(filtered["association_id"]) == ["1"]


class TestOcrPdf:
    def test_pages_are_concatenated(self, monkeypatch):
        import types

        fake_pdf2image = types.ModuleType("pdf2image")
        fake_pdf2image.convert_from_bytes = lambda *args, **kwargs: ["page-a", "page-b"]
        fake_tess = types.ModuleType("pytesseract")

        def _image_to_string(image, **kwargs):
            assert kwargs["config"] == "--psm 4"
            return {"page-a": "5. Association", "page-b": "6. Representative"}[image]

        fake_tess.image_to_string = _image_to_string
        monkeypatch.setitem(sys.modules, "pdf2image", fake_pdf2image)
        monkeypatch.setitem(sys.modules, "pytesseract", fake_tess)

        text = mod.ocr_pdf(b"%PDF-1.4")
        assert text == "5. Association\n\n6. Representative"


# ===========================================================================
# DB upsert
# ===========================================================================


def _mock_engine():
    engine = MagicMock()
    conn = MagicMock()
    engine.begin.return_value.__enter__ = MagicMock(return_value=conn)
    engine.begin.return_value.__exit__ = MagicMock(return_value=False)
    engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
    engine.connect.return_value.__exit__ = MagicMock(return_value=False)
    return engine, conn


class TestUpsert:
    def _ok_frame(self) -> pd.DataFrame:
        return pd.DataFrame([{
            "source_id": "tx_trec_hoa",
            "natural_key": "123456",
            "certificate_id": "51-253",
            "certificate_url": "https://example.test/a.pdf",
            "assoc_mailing_address": "PO Box 1, Austin, TX 78720",
            "rep_name": "Goodwin & Company",
            "rep_mailing_address": "PO Box 1, Austin, TX 78720",
            "rep_phone": "855.289.6007",
            "rep_phone_normalized": "8552896007",
            "rep_email": "info@goodwin-co.com",
            "website": "https://goodwin-co.com/",
            "field_6_raw": "Goodwin & Company",
            "enrich_status": "ok",
            "confidence": 1.0,
            "needs_review": False,
            "review_reasons": None,
            "ocr_text_sha256": "abc",
            "raw_pdf_sha256": "def",
            "raw_pdf_uri": "gs://bucket/def.pdf.gz",
            "error_detail": None,
        }])

    def test_sql_is_conflict_update_and_unchanged_rows_are_a_no_op(self):
        sql = str(mod._upsert_sql())
        assert "INSERT INTO staging.enrich_hoa_pdf_contact" in sql
        assert "ON CONFLICT (source_id, natural_key) DO UPDATE" in sql
        assert "IS DISTINCT FROM" in sql
        assert "enriched_at = now()" in sql
        for column in mod._UPSERT_COLUMNS:
            if column in {"source_id", "natural_key"}:
                continue
            assert f"staging.enrich_hoa_pdf_contact.{column} IS DISTINCT FROM EXCLUDED.{column}" in sql

    def test_returns_row_count_and_stamps_upstream_source_id(self):
        engine, conn = _mock_engine()
        count = mod.upsert_enrich_hoa_pdf_contact(engine, self._ok_frame())
        assert count == 1
        rows = conn.execute.call_args.args[1]
        assert rows[0]["source_id"] == "tx_trec_hoa"
        assert rows[0]["natural_key"] == "123456"
        assert rows[0]["needs_review"] is False

    def test_skipped_rows_are_not_written(self):
        engine, conn = _mock_engine()
        frame = self._ok_frame()
        frame["enrich_status"] = "skipped"
        assert mod.upsert_enrich_hoa_pdf_contact(engine, frame) == 0
        conn.execute.assert_not_called()

    def test_nan_text_becomes_none(self):
        engine, conn = _mock_engine()
        frame = self._ok_frame()
        frame.loc[0, "rep_phone"] = float("nan")
        frame.loc[0, "confidence"] = float("nan")
        mod.upsert_enrich_hoa_pdf_contact(engine, frame)
        rows = conn.execute.call_args.args[1]
        assert rows[0]["rep_phone"] is None
        assert rows[0]["confidence"] is None

    def test_duplicate_natural_key_is_collapsed(self):
        engine, conn = _mock_engine()
        low = self._ok_frame()
        low["confidence"] = 0.2
        low["rep_email"] = "low@example.test"
        high = self._ok_frame()
        high["confidence"] = 0.9
        high["rep_email"] = "high@example.test"
        frame = pd.concat([low, high], ignore_index=True)
        count = mod.upsert_enrich_hoa_pdf_contact(engine, frame)
        assert count == 1
        rows = conn.execute.call_args.args[1]
        assert rows[0]["rep_email"] == "high@example.test"

    def test_empty_natural_key_is_dropped(self):
        engine, conn = _mock_engine()
        frame = self._ok_frame()
        frame.loc[0, "natural_key"] = ""
        assert mod.upsert_enrich_hoa_pdf_contact(engine, frame) == 0
        conn.execute.assert_not_called()

    def test_load_cached_keys(self):
        engine, conn = _mock_engine()
        result = MagicMock()
        result.fetchall.return_value = [("123456", "51-253"), ("999", None)]
        conn.execute.return_value = result
        keys = mod.load_cached_keys(engine)
        assert keys == {("123456", "51-253")}
        sql = str(conn.execute.call_args.args[0])
        assert "enrich_status = 'ok'" in sql
        assert "enrich_hoa_pdf_contact" in sql


class TestBatchExitCode:
    def test_all_errors_fail_the_process(self):
        df = pd.DataFrame({"enrich_status": ["error", "error"]})
        assert mod.batch_exit_code(df) == 1

    def test_mix_of_ok_and_error_exits_zero(self):
        df = pd.DataFrame({"enrich_status": ["ok", "error", "not_found"]})
        assert mod.batch_exit_code(df) == 0

    def test_cached_skips_alone_exit_zero(self):
        df = pd.DataFrame({"enrich_status": ["skipped", "skipped"]})
        assert mod.batch_exit_code(df) == 0


class TestManifest:
    def test_manifest_lists_downloaded_pdfs_and_license(self):
        frame = pd.DataFrame([{
            "natural_key": "123456",
            "certificate_id": "51-253",
            "raw_pdf_sha256": "abc",
            "raw_pdf_byte_count": 12,
            "raw_pdf_uri": "gs://juniper-ingest-raw/tx_trec_pdf_enrich/2026-09-21/abc.pdf.gz",
            "enrich_status": "ok",
        }, {
            "natural_key": "999",
            "certificate_id": "1",
            "raw_pdf_sha256": None,
            "raw_pdf_byte_count": None,
            "raw_pdf_uri": None,
            "enrich_status": "skipped",
        }])
        raw = mod.build_manifest_bytes(frame)
        import json
        payload = json.loads(raw)
        assert payload["license"] == mod.LICENSE
        assert payload["connector"] == "tx_trec_pdf_enrich"
        assert payload["upstream_source_id"] == "tx_trec_hoa"
        assert payload["pdfs"] == [{
            "natural_key": "123456",
            "certificate_id": "51-253",
            "sha256": "abc",
            "byte_count": 12,
            "uri": "gs://juniper-ingest-raw/tx_trec_pdf_enrich/2026-09-21/abc.pdf.gz",
        }]
        # Same rows → same bytes, so the source_run object name is stable.
        assert mod.build_manifest_bytes(frame) == raw

    def test_write_enrich_run_records_license_and_upserts(self):
        engine, _conn = _mock_engine()
        frame = pd.DataFrame([{
            "source_id": "tx_trec_hoa",
            "natural_key": "123456",
            "certificate_id": "51-253",
            "certificate_url": "https://example.test/a.pdf",
            "assoc_mailing_address": None,
            "rep_name": None,
            "rep_mailing_address": None,
            "rep_phone": None,
            "rep_phone_normalized": None,
            "rep_email": None,
            "website": None,
            "field_6_raw": None,
            "enrich_status": "error",
            "confidence": 0.0,
            "needs_review": True,
            "review_reasons": "ocr_error",
            "ocr_text_sha256": None,
            "raw_pdf_sha256": "abc",
            "raw_pdf_uri": None,
            "raw_pdf_byte_count": 4,
            "error_detail": "ocr_error: boom",
        }])
        with (
            patch.object(mod, "upload_raw", return_value="gs://bucket/manifest.json.gz") as upload,
            patch("lib.db.write_source_run", return_value=42) as write_run,
            patch("lib.db.finish_source_run") as finish,
        ):
            run_id, written = mod.write_enrich_run(engine, frame, run_date="2026-09-21")

        assert run_id == 42
        assert written == 1
        assert upload.call_args.kwargs["suffix"] == "json"
        assert upload.call_args.args[0] == mod.CONNECTOR_ID
        assert write_run.call_args.kwargs["license_string"] == mod.LICENSE
        assert write_run.call_args.kwargs["source_id"] == mod.CONNECTOR_ID
        assert write_run.call_args.kwargs["raw_uri"] == "gs://bucket/manifest.json.gz"
        finish.assert_called_once()
        assert finish.call_args.kwargs["status"] == "succeeded"
        assert finish.call_args.kwargs["row_count"] == 1


# ===========================================================================
# Wiring
# ===========================================================================


class TestWiring:
    def test_run_hoa_places_pdf_enrich_between_csv_and_core_apply(self):
        script = Path(__file__).resolve().parents[3].joinpath("scripts", "run_hoa.sh").read_text()
        csv_at = script.index("hoa.tx_trec_hoa")
        pdf_at = script.index("hoa.tx_trec_pdf_enrich")
        core_at = script.index("lib.core_apply")
        assert csv_at < pdf_at < core_at
        assert "--queue" in script
        assert "HOA_PDF_SLEEP" in script

    def test_dockerfile_installs_ocr_system_deps(self):
        dockerfile = Path(__file__).resolve().parents[3].joinpath("Dockerfile").read_text()
        assert "tesseract-ocr" in dockerfile
        assert "poppler-utils" in dockerfile

    def test_requirements_include_ocr_libraries(self):
        requirements = Path(__file__).resolve().parents[3].joinpath("connectors", "requirements.txt").read_text()
        assert "pytesseract" in requirements
        assert "pdf2image" in requirements

    def test_migration_follows_enrich_table_convention(self):
        sql = Path(__file__).resolve().parents[3].joinpath(
            "db", "migrations", "018_enrich_hoa_pdf_contact.sql",
        ).read_text()
        assert "CREATE TABLE IF NOT EXISTS staging.enrich_hoa_pdf_contact" in sql
        assert "PRIMARY KEY (source_id, natural_key)" in sql
        assert "enriched_at" in sql
        assert "needs_review" in sql
        assert "WHERE needs_review" in sql
