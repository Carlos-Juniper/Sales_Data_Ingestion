"""Offline backfill of hoa_* / mgmt_* from stored enrich text.

The splitter is the parser helper. No database and no PDF bytes.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hoa.backfill_hoa_pdf_contact import (
    CONTACT_COLUMNS,
    format_report,
    plan_contact_updates,
    run_backfill,
)
from hoa.trec_certificate_parser import split_name_and_address
import hoa.backfill_hoa_pdf_contact as bf


def _row(**overrides) -> dict:
    base = {
        "source_id": "tx_trec_hoa",
        "natural_key": "123456",
        "assoc_mailing_address": None,
        "rep_name": None,
        "rep_mailing_address": None,
        "rep_phone": None,
        "rep_phone_normalized": None,
        "rep_email": None,
        "hoa_name": None,
        "hoa_mailing_address": None,
        "mgmt_name": None,
        "mgmt_mailing_address": None,
        "mgmt_phone": None,
        "mgmt_phone_normalized": None,
        "mgmt_email": None,
    }
    base.update(overrides)
    return base


class TestSplitAssocBlob:
    def test_multiline_name_and_address(self):
        name, address = split_name_and_address(
            "Heritage Oaks Homeowners Association\n"
            "PO Box 11847\n"
            "College Station, TX 77842"
        )
        assert name == "Heritage Oaks Homeowners Association"
        assert address == "PO Box 11847, College Station, TX 77842"

    def test_multiline_name_with_internal_comma(self):
        name, address = split_name_and_address(
            "Trinity Estates POA, Inc.\n"
            "PO Box 203310\n"
            "Austin, TX 78720"
        )
        assert name == "Trinity Estates POA, Inc."
        assert address == "PO Box 203310, Austin, TX 78720"

    def test_po_box_has_address_and_no_name(self):
        name, address = split_name_and_address(
            "PO Box 203310\nAustin, TX 78720"
        )
        assert name is None
        assert address == "PO Box 203310, Austin, TX 78720"

        joined_name, joined_address = split_name_and_address(
            "PO Box 203310, Austin, TX 78720"
        )
        assert joined_name is None
        assert joined_address == "PO Box 203310, Austin, TX 78720"

    def test_care_of_starts_the_address(self):
        blob = (
            "Westlake Hills HOA, c/o Spectrum Association Management, "
            "17319 San Pedro Ave, San Antonio, TX 78232"
        )
        name, address = split_name_and_address(blob)
        assert name == "Westlake Hills HOA"
        assert address == (
            "c/o Spectrum Association Management, 17319 San Pedro Ave, "
            "San Antonio, TX 78232"
        )

    def test_single_line_comma_separated(self):
        name, address = split_name_and_address(
            "Goodwin & Company, 100 Congress Ave, Suite 200, Austin, TX 78701"
        )
        assert name == "Goodwin & Company"
        assert address == "100 Congress Ave, Suite 200, Austin, TX 78701"

    def test_joined_field_5_matches_multiline_when_po_box_anchors(self):
        """The stored blob is the comma-joined field-5 text."""
        multiline = (
            "Trinity Estates POA, Inc.\n"
            "c/o Goodwin & Company\n"
            "PO Box 203310\n"
            "Austin, TX 78720"
        )
        joined = (
            "Trinity Estates POA, Inc., c/o Goodwin & Company, "
            "PO Box 203310, Austin, TX 78720"
        )
        assert split_name_and_address(multiline) == split_name_and_address(joined)

    def test_blank_blob(self):
        assert split_name_and_address(None) == (None, None)
        assert split_name_and_address("   ") == (None, None)


class TestPlanContactUpdates:
    def test_copies_mgmt_and_splits_hoa_when_null(self):
        updates = plan_contact_updates(_row(
            assoc_mailing_address=(
                "Sunrise HOA, 123 Main St, Austin, TX 78701"
            ),
            rep_name="Goodwin & Company",
            rep_mailing_address="PO Box 1, Austin, TX 78720",
            rep_phone="855.289.6007",
            rep_phone_normalized="8552896007",
            rep_email="info@goodwin-co.com",
        ))
        assert updates["hoa_name"] == "Sunrise HOA"
        assert updates["hoa_mailing_address"] == "123 Main St, Austin, TX 78701"
        assert updates["mgmt_name"] == "Goodwin & Company"
        assert updates["mgmt_mailing_address"] == "PO Box 1, Austin, TX 78720"
        assert updates["mgmt_phone"] == "855.289.6007"
        assert updates["mgmt_phone_normalized"] == "8552896007"
        assert updates["mgmt_email"] == "info@goodwin-co.com"

    def test_second_pass_is_empty(self):
        row = _row(
            assoc_mailing_address="Sunrise HOA, PO Box 1, Austin, TX 78701",
            rep_name="Goodwin & Company",
            rep_email="info@goodwin-co.com",
        )
        updates = plan_contact_updates(row)
        filled = dict(row)
        filled.update(updates)
        assert plan_contact_updates(filled) == {}

    def test_does_not_overwrite_populated_columns(self):
        updates = plan_contact_updates(_row(
            assoc_mailing_address="Other HOA, PO Box 9, Austin, TX 78701",
            rep_name="Other Mgmt",
            rep_phone="5125550100",
            hoa_name="Kept HOA",
            hoa_mailing_address="Kept Addr",
            mgmt_name="Kept Mgmt",
            mgmt_mailing_address="Kept Mgmt Addr",
            mgmt_phone="111",
            mgmt_phone_normalized="111",
            mgmt_email="kept@example.test",
        ))
        assert updates == {}

    def test_fills_only_the_null_mgmt_phone(self):
        updates = plan_contact_updates(_row(
            hoa_name="Sunrise HOA",
            hoa_mailing_address="PO Box 1",
            mgmt_name="Goodwin & Company",
            mgmt_mailing_address="PO Box 1",
            mgmt_phone=None,
            mgmt_phone_normalized="8552896007",
            mgmt_email="info@goodwin-co.com",
            rep_name="Someone Else",
            rep_phone="855.289.6007",
            rep_phone_normalized="000",
            rep_email="other@example.test",
        ))
        assert updates == {"mgmt_phone": "855.289.6007"}

    def test_blank_string_targets_are_filled(self):
        updates = plan_contact_updates(_row(
            assoc_mailing_address="Sunrise HOA, PO Box 1, Austin, TX 78701",
            hoa_name="  ",
            rep_email="info@goodwin-co.com",
            mgmt_email="",
        ))
        assert updates["hoa_name"] == "Sunrise HOA"
        assert updates["mgmt_email"] == "info@goodwin-co.com"

    def test_leaves_hoa_null_when_assoc_blob_is_blank(self):
        assert plan_contact_updates(_row(assoc_mailing_address=None)) == {}
        assert plan_contact_updates(_row(assoc_mailing_address="   ")) == {}


class TestUpdateSql:
    def test_update_leaves_enriched_at_unchanged(self):
        sql = str(bf._update_sql())
        assert "UPDATE staging.enrich_hoa_pdf_contact" in sql
        assert "enriched_at" not in sql
        assert "NULLIF(btrim(hoa_name), '') IS NULL" in sql
        assert "CAST(:mgmt_email AS text) IS NOT NULL" in sql
        for column in CONTACT_COLUMNS:
            assert f":{column}" in sql

    def test_candidate_query_reads_stored_text_only(self):
        sql = str(bf._candidate_sql())
        assert "assoc_mailing_address" in sql
        assert "rep_phone_normalized" in sql
        assert "raw_pdf" not in sql
        assert "certificate_url" not in sql


def _nulls(total: int, **overrides) -> dict:
    counts = {"rows_total": total}
    for column in CONTACT_COLUMNS:
        counts[column] = total
    counts.update(overrides)
    return counts


def _connectable(engine: MagicMock, conn: MagicMock) -> None:
    engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
    engine.connect.return_value.__exit__ = MagicMock(return_value=False)
    engine.begin.return_value.__enter__ = MagicMock(return_value=conn)
    engine.begin.return_value.__exit__ = MagicMock(return_value=False)


class TestRunBackfill:
    def _patch_reads(self, monkeypatch, rows, before):
        monkeypatch.setattr(bf, "null_counts", lambda conn, source_id=bf.SOURCE_ID: dict(before))
        monkeypatch.setattr(bf, "fetch_candidates", lambda conn, source_id=bf.SOURCE_ID: list(rows))

    def test_dry_run_does_not_open_a_write_transaction(self, monkeypatch):
        row = _row(
            assoc_mailing_address="Sunrise HOA, PO Box 1, Austin, TX 78701",
            rep_name="Goodwin & Company",
            rep_email="info@goodwin-co.com",
        )
        before = _nulls(4)
        self._patch_reads(monkeypatch, [row], before)
        engine = MagicMock()
        _connectable(engine, MagicMock())

        stats = run_backfill(engine, apply=False)

        engine.begin.assert_not_called()
        assert stats["mode"] == "dry-run"
        assert stats["rows_planned"] == 1
        assert stats["rows_updated"] == 0
        assert stats["fills"]["hoa_name"] == 1
        assert stats["fills"]["mgmt_name"] == 1
        assert stats["fills"]["mgmt_phone"] == 0
        assert stats["nulls_after"]["hoa_name"] == 3
        assert stats["nulls_after"]["mgmt_phone"] == 4
        report = format_report(stats)
        assert "dry-run, no writes" in report
        assert "hoa_name" in report
        assert "enriched_at is left unchanged." in report

    def test_apply_writes_planned_payload_and_skips_a_second_pass(self, monkeypatch):
        row = _row(
            assoc_mailing_address="Sunrise HOA, 123 Main St, Austin, TX 78701",
            rep_name="Goodwin & Company",
            rep_phone="855.289.6007",
        )
        already = _row(
            natural_key="999",
            hoa_name="Kept",
            hoa_mailing_address="Kept Addr",
            mgmt_name="Kept Mgmt",
            assoc_mailing_address="Other, PO Box 9, Austin, TX 78701",
            rep_name="Other",
        )
        before = _nulls(2)
        self._patch_reads(monkeypatch, [row, already], before)
        conn = MagicMock()
        conn.execute.return_value.rowcount = 1
        engine = MagicMock()
        _connectable(engine, conn)

        # After the write, recount one remaining null row for columns we did not fill.
        after = _nulls(2, hoa_name=1, hoa_mailing_address=1, mgmt_name=1, mgmt_phone=1)
        calls = {"n": 0}

        def _counts(conn, source_id=bf.SOURCE_ID):
            calls["n"] += 1
            if calls["n"] == 1:
                return dict(before)
            return dict(after)

        monkeypatch.setattr(bf, "null_counts", _counts)

        stats = run_backfill(engine, apply=True)

        engine.begin.assert_called_once()
        sql = str(conn.execute.call_args.args[0])
        assert "enriched_at" not in sql
        payloads = conn.execute.call_args.args[1]
        assert len(payloads) == 1
        assert payloads[0]["natural_key"] == "123456"
        assert payloads[0]["hoa_name"] == "Sunrise HOA"
        assert payloads[0]["mgmt_name"] == "Goodwin & Company"
        assert payloads[0]["mgmt_phone"] == "855.289.6007"
        assert payloads[0]["mgmt_email"] is None
        assert stats["rows_updated"] == 1
        assert stats["rows_unchanged"] == 1
        assert stats["nulls_after"]["hoa_name"] == 1

    def test_apply_with_nothing_to_fill_does_not_update(self, monkeypatch):
        before = _nulls(1, **{column: 0 for column in CONTACT_COLUMNS})
        # Candidate query would normally skip a complete row; if one slips
        # through with blank legacy sources, the plan is empty.
        self._patch_reads(monkeypatch, [_row()], before)
        engine = MagicMock()
        _connectable(engine, MagicMock())

        stats = run_backfill(engine, apply=True)

        engine.begin.assert_not_called()
        assert stats["rows_planned"] == 0
        assert stats["rows_updated"] == 0


class TestCli:
    def test_default_is_dry_run(self, monkeypatch, capsys):
        monkeypatch.setattr(bf, "run_backfill", lambda engine, *, apply: {
            "mode": "apply" if apply else "dry-run",
            "source_id": "tx_trec_hoa",
            "rows_total": 0,
            "candidates": 0,
            "rows_planned": 0,
            "rows_unchanged": 0,
            "rows_updated": 0,
            "fills": {column: 0 for column in CONTACT_COLUMNS},
            "nulls_before": _nulls(0),
            "nulls_after": _nulls(0, **{column: 0 for column in CONTACT_COLUMNS}),
        })
        monkeypatch.setattr("lib.http.get_secret", lambda name, required=False: "postgresql://example")
        monkeypatch.setattr("lib.db.get_engine", lambda: MagicMock())

        bf.main([])

        out = capsys.readouterr().out
        assert "dry-run" in out
        # The lambda saw apply=False; a write report would say "apply" only.
        assert "(apply)" not in out

    def test_apply_flag_requests_writes(self, monkeypatch, capsys):
        seen = {}

        def _run(engine, *, apply):
            seen["apply"] = apply
            stats = {
                "mode": "apply",
                "source_id": "tx_trec_hoa",
                "rows_total": 1,
                "candidates": 1,
                "rows_planned": 1,
                "rows_unchanged": 0,
                "rows_updated": 1,
                "fills": {column: 0 for column in CONTACT_COLUMNS},
                "nulls_before": _nulls(1),
                "nulls_after": _nulls(1, hoa_name=0),
            }
            stats["fills"]["hoa_name"] = 1
            return stats

        monkeypatch.setattr(bf, "run_backfill", _run)
        monkeypatch.setattr("lib.http.get_secret", lambda name, required=False: "postgresql://example")
        monkeypatch.setattr("lib.db.get_engine", lambda: MagicMock())

        bf.main(["--apply"])

        assert seen["apply"] is True
        assert "(apply)" in capsys.readouterr().out

    def test_missing_database_url_exits(self, monkeypatch):
        monkeypatch.setattr("lib.http.get_secret", lambda name, required=False: None)
        with pytest.raises(SystemExit):
            bf.main(["--dry-run"])
