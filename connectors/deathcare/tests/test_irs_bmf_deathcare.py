"""
Tests for irs_bmf_deathcare.py — IRS Exempt Organizations BMF connector.

Strategy
--------
Every public function is covered in isolation. All DataFrames are built
in-memory; no CSV reads and no network calls occur.

Key traps each have a dedicated test:

  1. OR filter trap     — subsection '13' with blank NTEE must pass;
                          NTEE 'Y50...' with non-13 subsection must pass;
                          neither condition fails the filter.
  2. EIN string trap    — EIN must survive as zero-padded string (leading zeros
                          must not be stripped by int coercion).
  3. ZIP 9-digit trap   — "01069-1507" must yield zip5 = "01069".
  4. segment='religious'— both normalize() and to_canonical() must emit
                          'religious', not None or 'unknown'.
  5. No-coordinate trap — latitude and longitude must be None throughout
                          (BMF carries no geometry data).
  6. source_id prefix   — must be 'irs_bmf:' + EIN, never 'nsd:' or bare EIN.
"""

from __future__ import annotations

import io
from unittest.mock import patch

import pandas as pd
import pytest

import irs_bmf_deathcare as mod
from lib.schema import CANONICAL_COLUMNS


# ===========================================================================
# Helpers
# ===========================================================================

def _make_raw(**overrides) -> pd.DataFrame:
    """
    Return a single-row DataFrame satisfying every field the module touches.
    Override individual column values with keyword arguments.
    """
    defaults = {
        "EIN": "043783054",
        "NAME": "Oak Grove Memorial Gardens",
        "ICO": None,
        "STREET": "100 Cemetery Rd",
        "CITY": "Charlotte",
        "STATE": "NC",
        "ZIP": "28201-1234",
        "GROUP": "0000",
        "SUBSECTION": "13",
        "AFFILIATION": "3",
        "CLASSIFICATION": "1000",
        "RULING": "196501",
        "DEDUCTIBILITY": "2",
        "FOUNDATION": "00",
        "ACTIVITY": "000000000",
        "ORGANIZATION": "1",
        "STATUS": "01",
        "TAX_PERIOD": "202312",
        "ASSET_CD": "5",
        "INCOME_CD": "3",
        "FILING_REQ_CD": "01",
        "PF_FILING_REQ_CD": "0",
        "ACCT_PD": "12",
        "ASSET_AMT": "1500000",
        "INCOME_AMT": "250000",
        "REVENUE_AMT": "300000",
        "NTEE_CD": "Y50",
        "SORT_NAME": None,
        "source_file": "https://www.irs.gov/pub/irs-soi/eo2.csv",
    }
    defaults.update(overrides)
    return pd.DataFrame([defaults])


def _make_large_raw(n: int = 2001, **row_overrides) -> pd.DataFrame:
    """
    Return a DataFrame with n rows, all target states represented, sufficient
    to pass assert_source_shape(). Each row gets a unique EIN.
    """
    states = ["FL", "TX", "NC", "SC", "PA"]
    rows = []
    for i in range(n):
        state = row_overrides.get("STATE", states[i % len(states)])
        override = {k: v for k, v in row_overrides.items() if k != "STATE"}
        row = _make_raw(
            EIN=f"{i:09d}",
            STATE=state,
            **override,
        ).iloc[0].to_dict()
        rows.append(row)
    return pd.DataFrame(rows)


# ===========================================================================
# Tests: assert_source_shape()
# ===========================================================================

class TestAssertSourceShape:
    def test_passes_on_well_formed_dataframe(self):
        df = _make_large_raw()
        mod.assert_source_shape(df)

    def test_raises_when_ein_column_missing(self):
        df = _make_large_raw().drop(columns=["EIN"])
        with pytest.raises(ValueError, match="EIN"):
            mod.assert_source_shape(df)

    def test_raises_when_state_column_missing(self):
        df = _make_large_raw().drop(columns=["STATE"])
        with pytest.raises(ValueError, match="STATE"):
            mod.assert_source_shape(df)

    def test_raises_when_subsection_column_missing(self):
        df = _make_large_raw().drop(columns=["SUBSECTION"])
        with pytest.raises(ValueError, match="SUBSECTION"):
            mod.assert_source_shape(df)

    def test_raises_when_ntee_cd_column_missing(self):
        df = _make_large_raw().drop(columns=["NTEE_CD"])
        with pytest.raises(ValueError, match="NTEE_CD"):
            mod.assert_source_shape(df)

    def test_raises_when_row_count_below_minimum(self):
        df = pd.concat([_make_raw()] * 100, ignore_index=True)
        with pytest.raises(ValueError, match="rows"):
            mod.assert_source_shape(df)

    def test_passes_at_exactly_minimum_rows(self):
        df = _make_large_raw(n=2000)
        mod.assert_source_shape(df)

    def test_raises_at_one_below_minimum(self):
        df = _make_large_raw(n=1999)
        with pytest.raises(ValueError):
            mod.assert_source_shape(df)

    def test_raises_when_ein_fill_rate_below_99_percent(self):
        df = _make_large_raw(n=2001)
        null_count = int(len(df) * 0.02)
        df.loc[:null_count - 1, "EIN"] = None
        with pytest.raises(ValueError, match="EIN"):
            mod.assert_source_shape(df)

    def test_raises_when_target_state_absent(self):
        """All 5 target states must be present before filtering."""
        df = _make_large_raw(n=2001, STATE="NC")
        with pytest.raises(ValueError, match="FL"):
            mod.assert_source_shape(df)

    def test_all_five_states_present_passes(self):
        df = _make_large_raw(n=2500)
        mod.assert_source_shape(df)


# ===========================================================================
# Tests: filter_cemetery()
# ===========================================================================

class TestFilterCemetery:

    def test_subsection_13_with_blank_ntee_passes(self):
        """
        OR filter — subsection '13' alone is enough to keep the row even
        when NTEE_CD is blank. ~49% of BMF cemetery records hit this path.
        """
        df = _make_raw(SUBSECTION="13", NTEE_CD="", STATE="NC")
        out = mod.filter_cemetery(df)
        assert len(out) == 1

    def test_subsection_13_with_null_ntee_passes(self):
        df = _make_raw(SUBSECTION="13", NTEE_CD=None, STATE="FL")
        out = mod.filter_cemetery(df)
        assert len(out) == 1

    def test_y50_ntee_with_non_13_subsection_passes(self):
        """
        OR filter — NTEE_CD starting with 'Y50' keeps the row regardless of
        SUBSECTION value. This catches nonprofit cemeteries with a different
        subsection code.
        """
        df = _make_raw(SUBSECTION="99", NTEE_CD="Y50AA", STATE="TX")
        out = mod.filter_cemetery(df)
        assert len(out) == 1

    def test_neither_condition_drops_row(self):
        df = _make_raw(SUBSECTION="06", NTEE_CD="E20", STATE="FL")
        out = mod.filter_cemetery(df)
        assert len(out) == 0

    def test_y50_prefix_matches_longer_codes(self):
        df = _make_raw(SUBSECTION="99", NTEE_CD="Y50ZZ", STATE="PA")
        out = mod.filter_cemetery(df)
        assert len(out) == 1

    def test_y5_without_zero_does_not_match(self):
        df = _make_raw(SUBSECTION="99", NTEE_CD="Y5A", STATE="FL")
        out = mod.filter_cemetery(df)
        assert len(out) == 0

    def test_state_filter_drops_non_target_states(self):
        df = _make_raw(SUBSECTION="13", NTEE_CD="", STATE="CA")
        out = mod.filter_cemetery(df)
        assert len(out) == 0

    def test_all_five_target_states_kept(self):
        frames = [
            _make_raw(STATE=s, EIN=f"00000000{i}")
            for i, s in enumerate(["FL", "TX", "NC", "SC", "PA"])
        ]
        df = pd.concat(frames, ignore_index=True)
        out = mod.filter_cemetery(df)
        assert set(out["STATE"].unique()) == {"FL", "TX", "NC", "SC", "PA"}

    def test_row_count_reduced_after_filter(self):
        keep = _make_raw(SUBSECTION="13", NTEE_CD="", STATE="FL", EIN="000000001")
        drop = _make_raw(SUBSECTION="06", NTEE_CD="E20", STATE="FL", EIN="000000002")
        df = pd.concat([keep, drop], ignore_index=True)
        out = mod.filter_cemetery(df)
        assert len(out) == 1

    def test_filter_writes_to_stderr(self, capsys):
        df = _make_raw(SUBSECTION="13", STATE="FL")
        mod.filter_cemetery(df)
        captured = capsys.readouterr()
        assert captured.err, "filter_cemetery must write diagnostics to stderr"


# ===========================================================================
# Tests: normalize()
# ===========================================================================

class TestNormalize:

    def test_name_normalized_column_added(self):
        df = _make_raw()
        out = mod.normalize(df)
        assert "name_normalized" in out.columns

    def test_name_normalized_is_string(self):
        df = _make_raw(NAME="Sunset Memorial Park")
        out = mod.normalize(df)
        assert isinstance(out["name_normalized"].iloc[0], str)

    def test_null_name_does_not_raise(self):
        df = _make_raw(NAME=None)
        out = mod.normalize(df)
        assert out["name_normalized"].iloc[0] == ""

    def test_zip5_9_digit_with_hyphen_truncated(self):
        """
        CRITICAL: BMF ZIPs are formatted as '01069-1507'.
        normalize() must yield zip5 = '01069', not the full string.
        """
        df = _make_raw(ZIP="01069-1507")
        out = mod.normalize(df)
        assert out["zip5"].iloc[0] == "01069"

    def test_zip5_plain_5_digit_unchanged(self):
        df = _make_raw(ZIP="28201")
        out = mod.normalize(df)
        assert out["zip5"].iloc[0] == "28201"

    def test_zip5_null_returns_empty_string(self):
        df = _make_raw(ZIP=None)
        out = mod.normalize(df)
        assert out["zip5"].iloc[0] == ""

    def test_ein_preserved_as_string_with_leading_zeros(self):
        """
        CRITICAL: EIN '000000001' must not become 1 or '1'.
        BMF stores EIN as zero-padded 9-digit string with no hyphen.
        """
        df = _make_raw(EIN="000000001")
        out = mod.normalize(df)
        assert out["ein"].iloc[0] == "000000001"

    def test_segment_is_religious(self):
        """
        CRITICAL: BMF records are the nonprofit/religious cemetery segment.
        Must be 'religious', not None or 'unknown'.
        """
        df = _make_raw()
        out = mod.normalize(df)
        assert out["segment"].iloc[0] == "religious"

    def test_latitude_is_none(self):
        """
        CRITICAL: BMF has no coordinate data — latitude must be None.
        This distinguishes BMF records from USGS NSD records during merge.
        """
        df = _make_raw()
        out = mod.normalize(df)
        assert out["latitude"].iloc[0] is None

    def test_longitude_is_none(self):
        """BMF has no coordinate data — longitude must be None."""
        df = _make_raw()
        out = mod.normalize(df)
        assert out["longitude"].iloc[0] is None

    def test_phone_raw_is_none(self):
        df = _make_raw()
        out = mod.normalize(df)
        assert out["phone_raw"].iloc[0] is None

    def test_phone_normalized_is_none(self):
        df = _make_raw()
        out = mod.normalize(df)
        assert out["phone_normalized"].iloc[0] is None

    def test_county_fips_is_none(self):
        df = _make_raw()
        out = mod.normalize(df)
        assert out["county_fips"].iloc[0] is None

    def test_normalize_preserves_state_column(self):
        df = _make_raw(STATE="TX")
        out = mod.normalize(df)
        assert out["STATE"].iloc[0] == "TX"

    def test_normalize_handles_multiple_rows(self):
        df = pd.concat([
            _make_raw(STATE="FL", EIN="000000001"),
            _make_raw(STATE="TX", EIN="000000002"),
        ], ignore_index=True)
        out = mod.normalize(df)
        assert len(out) == 2


# ===========================================================================
# Tests: to_canonical()
# ===========================================================================

class TestToCanonical:
    def _normalized_df(self) -> pd.DataFrame:
        return mod.normalize(_make_raw())

    def test_all_expected_columns_present(self):
        out = mod.to_canonical(self._normalized_df())
        missing = set(CANONICAL_COLUMNS) - set(out.columns)
        assert not missing, f"Missing columns: {missing}"

    def test_source_id_prefixed_with_irs_bmf(self):
        """
        CRITICAL: source_id must start with 'irs_bmf:', not 'nsd:' or bare EIN.
        Prefix is the namespace key used during downstream deduplication.
        """
        df = _make_raw(EIN="043783054")
        out = mod.to_canonical(mod.normalize(df))
        assert out["source_id"].iloc[0] == "irs_bmf:043783054"

    def test_source_id_preserves_leading_zeros_in_ein(self):
        """EIN leading zeros must survive into source_id."""
        df = _make_raw(EIN="000000001")
        out = mod.to_canonical(mod.normalize(df))
        assert out["source_id"].iloc[0] == "irs_bmf:000000001"

    def test_natural_key_matches_ein(self):
        df = _make_raw(EIN="043783054")
        out = mod.to_canonical(mod.normalize(df))
        assert out["natural_key"].iloc[0] == "043783054"

    def test_vertical_is_deathcare(self):
        out = mod.to_canonical(self._normalized_df())
        assert (out["vertical"] == "deathcare").all()

    def test_account_type_is_cemetery(self):
        out = mod.to_canonical(self._normalized_df())
        assert (out["account_type"] == "cemetery").all()

    def test_segment_is_religious(self):
        """
        CRITICAL: BMF records are always 'religious' segment —
        must not be None (unlike NSD which defers to merge).
        """
        out = mod.to_canonical(self._normalized_df())
        assert (out["segment"] == "religious").all()

    def test_latitude_is_none(self):
        """
        CRITICAL: BMF has no coordinates — latitude must be None in canonical output.
        A non-None value here would feed false coordinates into the merge module.
        """
        out = mod.to_canonical(self._normalized_df())
        assert out["latitude"].iloc[0] is None

    def test_longitude_is_none(self):
        """BMF has no coordinates — longitude must be None in canonical output."""
        out = mod.to_canonical(self._normalized_df())
        assert out["longitude"].iloc[0] is None

    def test_county_fips_is_none(self):
        out = mod.to_canonical(self._normalized_df())
        assert out["county_fips"].iloc[0] is None

    def test_ein_preserved_with_leading_zeros(self):
        df = _make_raw(EIN="000000001")
        out = mod.to_canonical(mod.normalize(df))
        assert out["ein"].iloc[0] == "000000001"

    def test_zip5_maps_from_zip(self):
        df = _make_raw(ZIP="01069-1507")
        out = mod.to_canonical(mod.normalize(df))
        assert out["zip5"].iloc[0] == "01069"

    def test_state_matches_source_state(self):
        df = _make_raw(STATE="SC")
        out = mod.to_canonical(mod.normalize(df))
        assert out["state"].iloc[0] == "SC"

    def test_address_line_1_maps_from_street(self):
        df = _make_raw(STREET="99 Magnolia Ln")
        out = mod.to_canonical(mod.normalize(df))
        assert out["address_line_1"].iloc[0] == "99 Magnolia Ln"

    def test_row_count_preserved(self):
        df = pd.concat([
            _make_raw(EIN=f"00000000{i}") for i in range(5)
        ], ignore_index=True)
        out = mod.to_canonical(mod.normalize(df))
        assert len(out) == 5

    def test_source_file_contains_url(self):
        df = _make_raw(source_file="https://www.irs.gov/pub/irs-soi/eo2.csv")
        out = mod.to_canonical(mod.normalize(df))
        assert "irs.gov" in out["source_file"].iloc[0]

    def test_source_file_joins_multiple_sources(self):
        df1 = _make_raw(EIN="000000001", source_file="https://www.irs.gov/pub/irs-soi/eo2.csv")
        df2 = _make_raw(EIN="000000002", source_file="https://www.irs.gov/pub/irs-soi/eo3.csv")
        df = mod.normalize(pd.concat([df1, df2], ignore_index=True))
        out = mod.to_canonical(df)
        # Both URLs must appear in the joined source_file string
        assert "eo2.csv" in out["source_file"].iloc[0]
        assert "eo3.csv" in out["source_file"].iloc[0]


# ===========================================================================
# Tests: report_quality()
# ===========================================================================

class TestReportQuality:
    def test_does_not_raise_on_well_formed_input(self):
        df = mod.normalize(_make_raw())
        mod.report_quality(df)

    def test_does_not_raise_with_null_name(self):
        df = mod.normalize(_make_raw(NAME=None))
        mod.report_quality(df)

    def test_produces_stderr_output_with_blank_ntee_percentage(self, capsys):
        """
        report_quality computes the fraction of subsection-13 rows whose
        NTEE_CD is blank/null and prints it as X.X%.  Feed 4 sub-13 rows
        where exactly 2 have a blank NTEE_CD => 50.0% => assert that
        literal string appears in stderr so the test is sensitive to the
        computed value, not merely to the presence of any output.
        """
        rows = [
            _make_raw(SUBSECTION="13", NTEE_CD="Y50",  EIN="000000001"),  # filled
            _make_raw(SUBSECTION="13", NTEE_CD="Y50",  EIN="000000002"),  # filled
            _make_raw(SUBSECTION="13", NTEE_CD="",     EIN="000000003"),  # blank
            _make_raw(SUBSECTION="13", NTEE_CD=None,   EIN="000000004"),  # null
        ]
        df = mod.normalize(pd.concat(rows, ignore_index=True))
        mod.report_quality(df)
        captured = capsys.readouterr()
        assert "50.0%" in captured.err, (
            "report_quality must report blank NTEE_CD rate for subsection-13 rows; "
            f"expected '50.0%' in stderr but got:\n{captured.err}"
        )

    def test_does_not_raise_on_multi_state_batch(self):
        frames = [
            _make_raw(STATE=state, EIN=f"00000000{i}")
            for i, state in enumerate(["FL", "TX", "NC", "SC", "PA"])
        ]
        df = mod.normalize(pd.concat(frames, ignore_index=True))
        mod.report_quality(df)

    def test_does_not_raise_with_blank_asset_amt(self):
        """ASSET_AMT can be blank in BMF — report_quality must handle it."""
        df = mod.normalize(_make_raw(ASSET_AMT=None))
        mod.report_quality(df)

    def test_does_not_raise_with_all_blank_ntee_cd(self):
        """~49% of subsection-13 records have blank NTEE_CD."""
        df = mod.normalize(_make_raw(NTEE_CD=None, SUBSECTION="13"))
        mod.report_quality(df)

    def test_does_not_raise_with_mixed_subsections(self):
        df1 = _make_raw(SUBSECTION="13", NTEE_CD="", EIN="000000001")
        df2 = _make_raw(SUBSECTION="99", NTEE_CD="Y50AA", EIN="000000002")
        df = mod.normalize(pd.concat([df1, df2], ignore_index=True))
        mod.report_quality(df)


# ===========================================================================
# Tests: load_raw()
# ===========================================================================

# Minimal set of IRS BMF column headers (28 columns in the real file).
# We use a reduced subset to keep fixture CSVs concise while still exercising
# the column contract that the rest of the pipeline relies on.
_BMF_HEADERS = (
    "EIN,NAME,ICO,STREET,CITY,STATE,ZIP,GROUP,SUBSECTION,AFFILIATION,"
    "CLASSIFICATION,RULING,DEDUCTIBILITY,FOUNDATION,ACTIVITY,ORGANIZATION,"
    "STATUS,TAX_PERIOD,ASSET_CD,INCOME_CD,FILING_REQ_CD,PF_FILING_REQ_CD,"
    "ACCT_PD,ASSET_AMT,INCOME_AMT,REVENUE_AMT,NTEE_CD,SORT_NAME"
)


def _csv_row(**overrides) -> str:
    """
    Build a single CSV data row for a BMF record.
    Defaults produce a valid subsection-13 cemetery record.
    The comma-separated order must match _BMF_HEADERS exactly.
    """
    defaults = {
        "EIN": "043783054",
        "NAME": "Oak Grove Memorial Gardens",
        "ICO": "",
        "STREET": "100 Cemetery Rd",
        "CITY": "Charlotte",
        "STATE": "NC",
        "ZIP": "28201-1234",
        "GROUP": "0000",
        "SUBSECTION": "13",
        "AFFILIATION": "3",
        "CLASSIFICATION": "1000",
        "RULING": "196501",
        "DEDUCTIBILITY": "2",
        "FOUNDATION": "00",
        "ACTIVITY": "000000000",
        "ORGANIZATION": "1",
        "STATUS": "01",
        "TAX_PERIOD": "202312",
        "ASSET_CD": "5",
        "INCOME_CD": "3",
        "FILING_REQ_CD": "01",
        "PF_FILING_REQ_CD": "0",
        "ACCT_PD": "12",
        "ASSET_AMT": "1500000",
        "INCOME_AMT": "250000",
        "REVENUE_AMT": "300000",
        "NTEE_CD": "Y50",
        "SORT_NAME": "",
    }
    defaults.update(overrides)
    cols = _BMF_HEADERS.split(",")
    return ",".join(str(defaults[c]) for c in cols)


def _write_csv(tmp_path, filename: str, rows: list[str]) -> str:
    """Write a Latin-1 encoded CSV to tmp_path and return the file path string."""
    content = _BMF_HEADERS + "\n" + "\n".join(rows) + "\n"
    path = tmp_path / filename
    path.write_bytes(content.encode("latin-1"))
    return str(path)


class TestLoadRaw:
    """
    Tests for irs_bmf_deathcare.load_raw().

    Strategy: write tiny real CSV files to pytest's tmp_path fixture (no
    mocking needed — load_raw accepts arbitrary local paths, so we can pass
    temp file paths directly). This exercises the actual pd.read_csv call
    with the exact encoding="latin-1" and dtype=str contract.

    Key invariants tested:
      - EIN leading zeros are preserved as strings (dtype=str prevents int coercion)
      - All columns arrive as object/string dtype, never int or float
      - Multiple file paths are concatenated into a single DataFrame
      - source_file column reflects the originating path for each row
      - Latin-1 encoded bytes (Windows-1252 characters) do not raise
    """

    def test_happy_path_returns_dataframe(self, tmp_path):
        path = _write_csv(tmp_path, "eo2.csv", [_csv_row()])
        df = mod.load_raw(paths=[path])
        assert isinstance(df, pd.DataFrame)

    def test_single_file_yields_one_row(self, tmp_path):
        path = _write_csv(tmp_path, "eo2.csv", [_csv_row()])
        df = mod.load_raw(paths=[path])
        assert len(df) == 1

    def test_multiple_files_concatenated(self, tmp_path):
        """
        load_raw must concatenate all paths into a single DataFrame.
        Simulates loading eo2.csv (NC/SC/PA) + eo3.csv (FL/TX).
        """
        path1 = _write_csv(tmp_path, "eo2.csv", [
            _csv_row(EIN="000000001", STATE="NC"),
            _csv_row(EIN="000000002", STATE="SC"),
        ])
        path2 = _write_csv(tmp_path, "eo3.csv", [
            _csv_row(EIN="000000003", STATE="FL"),
        ])
        df = mod.load_raw(paths=[path1, path2])
        assert len(df) == 3

    def test_ein_with_leading_zeros_preserved_as_string(self, tmp_path):
        """
        CRITICAL: EIN '000000001' must arrive as the string '000000001'.
        Without dtype=str, pandas would coerce it to int 1 and strip the
        leading zeros, breaking every downstream join and dedup on EIN.
        """
        path = _write_csv(tmp_path, "eo2.csv", [_csv_row(EIN="000000001")])
        df = mod.load_raw(paths=[path])
        assert df["EIN"].iloc[0] == "000000001"

    def test_ein_nine_digit_zero_padded_string_unchanged(self, tmp_path):
        """EIN '043783054' (with embedded zeros) must survive intact as a string."""
        path = _write_csv(tmp_path, "eo2.csv", [_csv_row(EIN="043783054")])
        df = mod.load_raw(paths=[path])
        assert df["EIN"].iloc[0] == "043783054"
        assert isinstance(df["EIN"].iloc[0], str)

    def test_all_columns_are_string_dtype(self, tmp_path):
        """
        dtype=str is declared in load_raw — every column must be object (string)
        dtype. Numeric coercion here would corrupt EIN, ZIP, and AMT fields.
        """
        path = _write_csv(tmp_path, "eo2.csv", [_csv_row()])
        df = mod.load_raw(paths=[path])
        non_string_cols = [
            col for col in df.columns
            if col != "source_file" and not pd.api.types.is_object_dtype(df[col])
        ]
        assert not non_string_cols, (
            f"These columns are not object/string dtype: {non_string_cols}"
        )

    def test_zip_9_digit_with_hyphen_preserved_as_string(self, tmp_path):
        """ZIP '28201-1234' must not be split or coerced — load_raw is raw-read only."""
        path = _write_csv(tmp_path, "eo2.csv", [_csv_row(ZIP="28201-1234")])
        df = mod.load_raw(paths=[path])
        assert df["ZIP"].iloc[0] == "28201-1234"

    def test_source_file_column_reflects_originating_path(self, tmp_path):
        """
        Each row must have source_file set to the path it was read from.
        This is the traceability anchor used downstream in to_canonical().
        """
        path = _write_csv(tmp_path, "eo2.csv", [_csv_row()])
        df = mod.load_raw(paths=[path])
        assert df["source_file"].iloc[0] == path

    def test_source_file_differs_per_input_file(self, tmp_path):
        """Rows from different files must have different source_file values."""
        path1 = _write_csv(tmp_path, "eo2.csv", [_csv_row(EIN="000000001")])
        path2 = _write_csv(tmp_path, "eo3.csv", [_csv_row(EIN="000000002")])
        df = mod.load_raw(paths=[path1, path2])
        source_files = df["source_file"].unique()
        assert len(source_files) == 2

    def test_latin1_encoded_bytes_do_not_raise(self, tmp_path):
        """
        CRITICAL: IRS BMF files use Windows-1252/Latin-1 encoding.
        Characters such as \xe9 (é) appear in some organisation names.
        load_raw must read them without a UnicodeDecodeError.
        """
        # Craft a name with a Latin-1-only byte (é = 0xe9) and write raw bytes.
        headers = _BMF_HEADERS + "\n"
        row = _csv_row(NAME="Cimeti\xe8re Memorial")  # è = 0xe8, valid Latin-1
        content = (headers + row + "\n").encode("latin-1")
        path = tmp_path / "eo2_latin1.csv"
        path.write_bytes(content)
        # Must not raise UnicodeDecodeError
        df = mod.load_raw(paths=[str(path)])
        assert len(df) == 1
        assert "Cimeti" in df["NAME"].iloc[0]

    def test_required_columns_present_after_load(self, tmp_path):
        """All columns declared in _BMF_HEADERS must exist in the result."""
        path = _write_csv(tmp_path, "eo2.csv", [_csv_row()])
        df = mod.load_raw(paths=[path])
        expected = set(_BMF_HEADERS.split(","))
        missing = expected - set(df.columns)
        assert not missing, f"Missing columns after load_raw: {missing}"

    def test_multiple_rows_from_single_file(self, tmp_path):
        """Three rows in one file must produce three rows in the DataFrame."""
        rows = [
            _csv_row(EIN=f"0000000{i:02d}") for i in range(3)
        ]
        path = _write_csv(tmp_path, "eo2.csv", rows)
        df = mod.load_raw(paths=[path])
        assert len(df) == 3

    def test_result_index_is_reset_contiguous(self, tmp_path):
        """
        pd.concat with ignore_index=True must produce a 0-based contiguous index.
        A non-contiguous index would cause subtle merge/join bugs downstream.
        """
        path1 = _write_csv(tmp_path, "eo2.csv", [
            _csv_row(EIN="000000001"),
            _csv_row(EIN="000000002"),
        ])
        path2 = _write_csv(tmp_path, "eo3.csv", [
            _csv_row(EIN="000000003"),
        ])
        df = mod.load_raw(paths=[path1, path2])
        assert list(df.index) == list(range(len(df)))
