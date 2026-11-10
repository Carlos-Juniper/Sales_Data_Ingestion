"""Tests for hoa.fl_dbpr_condo_coop and hoa.fl_dbpr_cam — synthetic rows, no real people."""

from __future__ import annotations

import csv
import io

import pandas as pd
import pytest

from hoa import fl_dbpr_cam as cam
from hoa import fl_dbpr_condo_coop as cc

CONDO_COLS = ["Project Number", "File Number", "Condo Name", "County", "Street City State Zip",
              "Units", "Recorded Date", "Primary Status", "Secondary Status",
              "Managing Entity Number", "Managing Entity Name", "Managing Entity Route",
              "Managing Entity Street", "Managing Entity City", "Managing Entity State",
              "Managing Entity Zip"]


def condo_row(num="1001", name="TEST LAKES CONDOMINIUM", site="1 MAIN ST, TAMPA, FL 33647-1234",
              units="120", route="C/O TEST PROPERTY MANAGEMENT, INC", me="TEST LAKES CONDO ASSN INC"):
    return {"Project Number": num, "File Number": "F" + num, "Condo Name": name,
            "County": "HILLSBOROUGH", "Street City State Zip": site, "Units": units,
            "Recorded Date": "01/02/1999", "Primary Status": "Current", "Secondary Status": "",
            "Managing Entity Number": "N" + num, "Managing Entity Name": me,
            "Managing Entity Route": route, "Managing Entity Street": "PO BOX 1",
            "Managing Entity City": "TAMPA", "Managing Entity State": "FL",
            "Managing Entity Zip": "33602-0001"}


def write_csv(tmp_path, name, rows, cols):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)
    p = tmp_path / name
    p.write_bytes(buf.getvalue().encode("latin-1"))
    return str(p)


# ---------------------------------------------------------------- condo / co-op

@pytest.mark.parametrize("addr,expected", [
    ("1 MAIN ST, TAMPA, FL 33647-1234", ("1 MAIN ST", "TAMPA", "FL", "33647")),
    ("1 MAIN ST, UNIT 2, NEW PORT RICHEY, FL 34652", ("1 MAIN ST, UNIT 2", "NEW PORT RICHEY", "FL", "34652")),
    ("1 MAIN ST TAMPA 33647", ("1 MAIN ST TAMPA 33647", None, None, "33647")),
    ("", (None, None, None, None)),
])
def test_split_site(addr, expected):
    assert cc.split_site(addr) == expected


def test_condo_and_coop_files_merge_with_program(tmp_path):
    condo = write_csv(tmp_path, "Condo_CW.csv", [condo_row()], CONDO_COLS)
    coop_cols = [("COOP Name" if c == "Condo Name" else "Incorporated Date" if c == "Recorded Date" else c)
                 for c in CONDO_COLS]
    coop_row = {("COOP Name" if k == "Condo Name" else "Incorporated Date" if k == "Recorded Date" else k): v
                for k, v in condo_row(name="TEST TOWERS COOPERATIVE", route="").items()}
    coop = write_csv(tmp_path, "coopmailing.csv", [coop_row], coop_cols)
    df, raw = cc.load_raw([condo, coop])
    cc.assert_source_shape(df)
    assert list(df["program"]) == ["condo", "coop"]
    assert raw

    frame, attrs = cc.to_frame(df)
    canon = cc.to_canonical(frame).set_index("natural_key")
    # Same project number in both programs stays two rows.
    assert list(canon.index) == ["condo:1001", "coop:1001"]
    assert canon.loc["condo:1001", "size_value"] == 120
    assert canon.loc["condo:1001", "zip5"] == "33647"
    assert canon.loc["coop:1001", "segment"] == "coop"
    assert (canon["account_type"] == "association").all()
    a = attrs["condo:1001"]
    assert "TEST PROPERTY MANAGEMENT" in a["care_of"]
    assert a["managing_entity"]["zip"] == "33602"
    assert a["recorded_date"] == "01/02/1999"
    assert "care_of" not in attrs["coop:1001"]
    assert attrs["coop:1001"]["recorded_date"] == "01/02/1999"  # Incorporated Date renamed


def test_condo_grain_guard_rejects_repeated_project(tmp_path):
    p = write_csv(tmp_path, "Condo_CW.csv", [condo_row(), condo_row()], CONDO_COLS)
    df, _ = cc.load_raw([p])
    with pytest.raises(ValueError, match="repeated project"):
        cc.assert_source_shape(df)


def test_condo_guard_rejects_shifted_units(tmp_path):
    p = write_csv(tmp_path, "Condo_CW.csv", [condo_row(units="TAMPA")], CONDO_COLS)
    df, _ = cc.load_raw([p])
    with pytest.raises(ValueError, match="Units"):
        cc.assert_source_shape(df)


def test_condo_rejects_unknown_file(tmp_path):
    p = write_csv(tmp_path, "other.csv", [{"A": "1"}], ["A"])
    with pytest.raises(ValueError, match="not a DBPR project file"):
        cc.load_raw([p])


# ---------------------------------------------------------------- CAM / CAB

def cam_row(lic="CAB1", ltype="CAB", name="TEST PROPERTY MANAGEMENT, INC.", dba="TPM; TEST PM ",
            zip_="33602-1234"):
    r = {c: "" for c in cam.COLUMNS}
    r.update(board="38", license_type=ltype, name=name, dba=dba, addr1="1 FIRM WAY", city="TAMPA",
             state="FL", zip=zip_, county_code="39", primary_status="Current",
             secondary_status="Active", original_date="01/15/2005", status_date="",
             expiry_date="09/30/2027", license_number=lic)
    return r


def cam_df(rows):
    return pd.DataFrame(rows, columns=cam.COLUMNS)


def test_cam_load_raw_reads_headerless(tmp_path):
    buf = io.StringIO()
    csv.writer(buf).writerow([cam_row()[c] for c in cam.COLUMNS])
    p = tmp_path / "lic38cam.csv"
    p.write_bytes(buf.getvalue().encode("latin-1"))
    df, _ = cam.load_raw(str(p))
    assert len(df) == 1 and df.loc[0, "license_number"] == "CAB1"


def test_cam_keeps_firms_and_managers_drops_courses():
    df = cam_df([cam_row(), cam_row(lic="CAM7", ltype="CAM", name="DOE JANE", dba=""),
                 cam_row(lic="0001", ltype="CRSE", name="TEST COURSE")])
    cam.assert_source_shape(df)
    frame, attrs = cam.to_frame(df)
    canon = cam.to_canonical(frame).set_index("natural_key")
    assert list(canon.index) == ["CAB1", "CAM7"]
    assert canon.loc["CAB1", "account_type"] == "management_company"
    assert canon.loc["CAM7", "account_type"] == "individual"
    assert canon.loc["CAB1", "zip5"] == "33602"
    assert attrs["CAB1"]["dba"] == ["TPM", "TEST PM"]
    assert attrs["CAB1"]["original_date"] == "2005-01-15"
    assert "dba" not in attrs["CAM7"] and "status_date" not in attrs["CAB1"]


def test_cam_guard_rejects_shifted_columns():
    with pytest.raises(ValueError, match="well formed"):
        cam.assert_source_shape(cam_df([cam_row(lic="TAMPA")]))


def test_cam_guard_rejects_wrong_board():
    r = cam_row()
    r["board"] = "25"
    with pytest.raises(ValueError, match="board"):
        cam.assert_source_shape(cam_df([r]))


def test_cam_guard_rejects_repeated_license():
    with pytest.raises(ValueError, match="grain"):
        cam.assert_source_shape(cam_df([cam_row(), cam_row()]))
