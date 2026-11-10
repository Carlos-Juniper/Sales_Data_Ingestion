"""Tests for hoa.fl_sunbiz_assoc — synthetic fixed-width records, no real officers."""

from __future__ import annotations

import pytest

from hoa import fl_sunbiz_assoc as m


def _put(buf: list[str], span: tuple[int, int], value: str) -> None:
    a, b = span
    buf[a:b] = list(value.ljust(b - a)[: b - a])


def make_line(doc="N0000001", name="TEST LAKES HOMEOWNERS ASSOCIATION, INC.", status="A",
              filing="DOMNP", p_addr1="100 MAIN ST", p_addr2="", p_zip="33647",
              m_addr1="100 MAIN ST", m_zip="33647", fei="591234567", ra_name="ACME MANAGEMENT LLC",
              ra_kind="C", ra_zip="33602", officers=(("PRES", "P", "DOE JANE", "33647"),),
              more_than_six=" ", eol="\r\n") -> str:
    buf = [" "] * m.RECORD_LEN
    for key, val in {
        "doc": doc, "name": name, "status": status, "filing_type": filing,
        "p_addr1": p_addr1, "p_addr2": p_addr2, "p_city": "TAMPA", "p_state": "FL",
        "p_zip": p_zip, "m_addr1": m_addr1, "m_city": "TAMPA", "m_state": "FL", "m_zip": m_zip,
        "file_date": "10011987", "fei": fei, "more_than_six": more_than_six,
        "last_trx_date": "03092015", "report_year_1": "2026", "report_year_2": "2025",
        "report_year_3": "2024", "ra_name": ra_name, "ra_kind": ra_kind, "ra_addr": "1 AGENT WAY",
        "ra_city": "TAMPA", "ra_state": "FL", "ra_zip": ra_zip,
    }.items():
        _put(buf, m.F[key], val)
    for i, (title, kind, oname, ozip) in enumerate(officers):
        b = m.OFFICER_START + m.OFFICER_LEN * i
        for key, val in {"title": title, "kind": kind, "name": oname, "addr": "1 HOME RD",
                         "city": "TAMPA", "state": "FL", "zip": ozip + "1234"}.items():
            a, e = m.OFF[key]
            _put(buf, (b + a, b + e), val)
    return "".join(buf) + eol


def make_event(doc, code, name="", filed="11052026") -> str:
    buf = [" "] * 662
    _put(buf, (0, 12), doc)
    _put(buf, (12, 17), "00001")
    _put(buf, (17, 37), code)
    _put(buf, (85, 93), filed)
    _put(buf, (210, 402), name)
    return "".join(buf) + "\n"


def write(tmp_path, name, lines, encoding="latin-1"):
    p = tmp_path / name
    p.write_bytes("".join(lines).encode(encoding))
    return str(p)


# ---------------------------------------------------------------- name filter

@pytest.mark.parametrize("name", [
    "TEST LAKES HOMEOWNERS' ASSOCIATION, INC.",
    "TEST LAKES OWNERS' ASSOCIATION, INC.",
    "TEST BAY CONDOMINIUM NO. 5 ASSOCIATION, INC.",
    "TEST PARK TOWNHOUSE ASSOCIATION, INC.",
    "TEST RIDGE MASTER ASSOCIATION, INC.",
    "CHURCHILL TOWNHOMES ASSOCIATION, INC.",
    "MEMORIAL MEDICAL OFFICE CONDOMINIUM ASSOCIATION, INC.",
    "HARBOUR TEST CONDOMINIUM SERVICES ASSOCIATION, INC.",
    "TEST VILLAS ASSOCIATION, INC.",
    "TEST POA, INC.",
])
def test_name_filter_keeps_associations(name):
    assert m.is_association_name(name)


@pytest.mark.parametrize("name", [
    "TEST CONDO MANAGEMENT ASSOCIATION, INC.",
    "TEST APARTMENT ASSOCIATION, INC.",
    "COMMUNITY ASSOCIATION OF FLORIDA, INC.",
    "TEST TEMPLE LIEN HOA, INC.",
    "TEST CREEK, INC.",
    "TEST COUNTY BAR ASSOCIATION, INC.",
    "",
    None,
])
def test_name_filter_drops_non_associations(name):
    assert not m.is_association_name(name)


# ---------------------------------------------------------------- parsing

def test_parse_line_slices_every_field():
    rec = m.parse_line(make_line(p_addr1="C/O TEST PROPERTY MANAGEMENT", more_than_six="Y"), "q.txt")
    f = rec.fields
    assert f["doc"] == "N0000001"
    assert f["filing_type"] == "DOMNP"
    assert f["p_zip"] == "33647" and f["m_zip"] == "33647"
    assert f["ra_name"] == "ACME MANAGEMENT LLC" and f["ra_kind"] == "C" and f["ra_zip"] == "33602"
    assert f["more_than_six"] == "Y"
    assert rec.officers == [{"title": "PRES", "kind": "P", "name": "DOE JANE", "addr": "1 HOME RD",
                             "city": "TAMPA", "state": "FL", "zip": "33647"}]


def test_parse_line_rejects_blank_and_short():
    assert m.parse_line("") is None
    assert m.parse_line(" " * 1440) is None
    assert m.parse_line("N1 SHORT") is None


def test_daily_line_with_lf_and_nul_filler(tmp_path):
    line = make_line(eol="")[:-4] + "\x00" * 4 + "\n"
    p = write(tmp_path, "20261102c.txt", [line])
    recs = list(m.iter_records([p]))
    assert len(recs) == 1 and recs[0].fields["doc"] == "N0000001"


def test_latin1_names_survive(tmp_path):
    p = write(tmp_path, "npcordata0.txt", [make_line(name="CAFÉ TEST CONDOMINIUM ASSOCIATION, INC.")])
    rec = next(m.iter_records([p]))
    assert rec.fields["name"].startswith("CAFÉ")


def test_only_domnp_is_loaded(tmp_path):
    p = write(tmp_path, "npcordata0.txt", [
        make_line(doc="N1"), make_line(doc="F2", filing="FORNP"), make_line(doc="L3", filing="FLAL"),
    ])
    assert [r.fields["doc"] for r in m.iter_records([p])] == ["N1"]


# ---------------------------------------------------------------- load / events

def test_daily_overrides_quarterly_and_events_flip_status(tmp_path):
    q = write(tmp_path, "npcordata0.txt", [
        make_line(doc="N1", name="OLD TEST HOMEOWNERS ASSOCIATION, INC."),
        make_line(doc="N2"),
        make_line(doc="N3", name="TEST CREEK, INC."),
    ])
    d = write(tmp_path, "20261103c.txt", [make_line(doc="N4", eol="\n")])
    e = write(tmp_path, "20261105ce.txt", [
        make_event("N2", "CORAPVOLDS"),
        make_event("N1", "CORAPNC", name="NEW TEST HOMEOWNERS ASSOCIATION, INC."),
        make_event("N9", "CORAPREIN"),  # not held: ignored
    ])
    recs = m.load([q], [d], [e])
    assert set(recs) == {"N1", "N2", "N3", "N4"}
    assert recs["N2"].fields["status"] == "I"
    assert recs["N2"].fields["status_date"] == "2026-11-05"
    assert recs["N1"].fields["name"] == "NEW TEST HOMEOWNERS ASSOCIATION, INC."


def test_to_frame_canonical_and_attrs(tmp_path):
    q = write(tmp_path, "npcordata0.txt", [
        make_line(doc="N1", m_addr1="C/O TEST PROPERTY MANAGEMENT, INC.", fei="000000000"),
        make_line(doc="N2", name="TEST CREEK, INC.", p_zip="", m_zip="33602"),
    ])
    df, attrs = m.to_frame(m.load([q]))
    m.assert_source_shape(df, attrs)
    canon = m.to_canonical(df).set_index("natural_key")
    assert canon.loc["N1", "segment"] == "association_name"
    assert canon.loc["N2", "segment"] == "nonprofit_other"
    assert canon.loc["N2", "zip5"] == "33602"  # mailing ZIP when principal is blank
    assert canon.loc["N1", "ein"] is None  # all-zero FEI dropped
    assert (canon["vertical"] == "hoa").all() and (canon["account_type"] == "association").all()
    a = attrs["N1"]
    assert "TEST PROPERTY MANAGEMENT" in a["care_of"]
    assert a["registered_agent"]["kind"] == "C"
    assert a["report_years"] == ["2026", "2025", "2024"]
    assert a["file_date"] == "1987-10-01"
    assert a["officers"][0]["zip"] == "33647"
    assert "more_than_six_officers" not in a


def test_shape_check_catches_garbage():
    import pandas as pd
    with pytest.raises(ValueError):
        m.assert_source_shape(pd.DataFrame(columns=["natural_key", "zip5"]), {})
