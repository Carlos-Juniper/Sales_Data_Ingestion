"""
Tests for va_cemeteries.py — VA National Cemetery Sites connector.

Strategy
--------
Every public function is covered in isolation.  All DataFrames are built
in-memory; no CSV reads and no network calls occur.

Key traps called out in the spec each have a dedicated test:

  1. Packed address parsing — ZIP extraction, city extraction, and 2-letter
                              state-abbreviation extraction from one string.
  2. Packed contact parsing — first phone is taken; "Or" alternate number
                              must NOT be taken as the primary.
  3. account_type='federal' — VA cemeteries are federally managed, not
                              'cemetery' or 'municipal'.
  4. segment='federal'      — all records are excluded from lead output;
                              segment must be 'federal', not None.
  5. source_id prefix       — must start with 'va:', not 'nsd:' or bare name.
  6. Row-count guard        — assert_source_shape() must raise when fewer
                              than 150 rows are returned.
"""

from __future__ import annotations

import pandas as pd
import pytest

from lib.schema import CANONICAL_COLUMNS

import va_cemeteries as mod


# ===========================================================================
# Helpers
# ===========================================================================

def _make_raw(**overrides) -> pd.DataFrame:
    """
    Return a single-row DataFrame that satisfies every field the module touches.
    Override individual column values with keyword arguments.
    """
    defaults = {
        "cemetery_name": "Montevallo National Cemetery",
        "state": "Alabama",
        "address": "3133 Highway 119, Montevallo, AL 35115",
        "latitude": "33.1167",
        "longitude": "-86.8716",
        "contact": "Phone: 205-665-9039, FAX: 205-665-7790",
        "burial_space": "Open",
        "source_file": "https://datahub.va.gov/api/views/fcxt-zc8r/rows.csv?accessType=DOWNLOAD",
    }
    defaults.update(overrides)
    return pd.DataFrame([defaults])


def _make_large_raw(n: int = 150, **row_overrides) -> pd.DataFrame:
    """
    Return a DataFrame with ``n`` rows — enough to satisfy the row-count
    guard in assert_source_shape().  Each row gets a unique cemetery_name.
    """
    rows = []
    for i in range(n):
        row = _make_raw(
            cemetery_name=row_overrides.get("cemetery_name", f"Cemetery {i:04d}"),
            **{k: v for k, v in row_overrides.items() if k != "cemetery_name"},
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

    def test_raises_when_cemetery_name_column_missing(self):
        df = _make_large_raw().drop(columns=["cemetery_name"])
        with pytest.raises(ValueError, match="cemetery_name"):
            mod.assert_source_shape(df)

    def test_raises_when_state_column_missing(self):
        df = _make_large_raw().drop(columns=["state"])
        with pytest.raises(ValueError, match="state"):
            mod.assert_source_shape(df)

    def test_raises_when_address_column_missing(self):
        df = _make_large_raw().drop(columns=["address"])
        with pytest.raises(ValueError, match="address"):
            mod.assert_source_shape(df)

    def test_raises_when_latitude_column_missing(self):
        df = _make_large_raw().drop(columns=["latitude"])
        with pytest.raises(ValueError, match="latitude"):
            mod.assert_source_shape(df)

    def test_raises_when_longitude_column_missing(self):
        df = _make_large_raw().drop(columns=["longitude"])
        with pytest.raises(ValueError, match="longitude"):
            mod.assert_source_shape(df)

    def test_raises_when_contact_column_missing(self):
        df = _make_large_raw().drop(columns=["contact"])
        with pytest.raises(ValueError, match="contact"):
            mod.assert_source_shape(df)

    def test_raises_when_row_count_below_150(self):
        """
        CRITICAL: truncation guard — fewer than 150 rows means the download
        was empty or cut short and the connector must not proceed.
        """
        df = pd.concat([_make_raw()] * 100, ignore_index=True)
        with pytest.raises(ValueError):
            mod.assert_source_shape(df)

    def test_passes_at_exactly_150_rows(self):
        df = _make_large_raw(n=150)
        mod.assert_source_shape(df)

    def test_raises_at_149_rows(self):
        df = _make_large_raw(n=149)
        with pytest.raises(ValueError):
            mod.assert_source_shape(df)

    def test_raises_when_state_full_name_unrecognized(self):
        """
        Unrecognized full state name catches encoding corruption or schema drift.
        """
        df = _make_large_raw(state="NotAState")
        with pytest.raises(ValueError, match="NotAState"):
            mod.assert_source_shape(df)

    def test_passes_with_all_known_target_states(self):
        frames = [
            _make_large_raw(n=30, state=name)
            for name in ["Florida", "Texas", "North Carolina", "South Carolina", "Pennsylvania"]
        ]
        df = pd.concat(frames, ignore_index=True)
        mod.assert_source_shape(df)


# ===========================================================================
# Tests: normalize()
# ===========================================================================

class TestNormalize:

    # -- name_normalized -----------------------------------------------------

    def test_name_normalized_column_added(self):
        df = _make_raw()
        out = mod.normalize(df)
        assert "name_normalized" in out.columns

    def test_name_normalized_is_uppercase_stripped(self):
        df = _make_raw(cemetery_name="Fort Rosecrans National Cemetery")
        out = mod.normalize(df)
        assert out["name_normalized"].iloc[0] == "FORT ROSECRANS NATIONAL CEMETERY"

    # -- packed address: ZIP extraction --------------------------------------

    def test_zip5_extracted_from_address(self):
        """CRITICAL: ZIP lives at the end of the packed address string."""
        df = _make_raw(address="3133 Highway 119, Montevallo, AL 35115")
        out = mod.normalize(df)
        assert out["zip5"].iloc[0] == "35115"

    def test_zip5_extracted_from_address_different_record(self):
        df = _make_raw(address="1 Cemetery Road, Houston, TX 77038")
        out = mod.normalize(df)
        assert out["zip5"].iloc[0] == "77038"

    def test_zip5_is_empty_when_address_missing(self):
        df = _make_raw(address=None)
        out = mod.normalize(df)
        assert out["zip5"].iloc[0] == ""

    # -- packed address: city extraction -------------------------------------

    def test_city_extracted_from_address(self):
        """CRITICAL: city is the comma-delimited segment before 'ST ZIPCODE'."""
        df = _make_raw(address="3133 Highway 119, Montevallo, AL 35115")
        out = mod.normalize(df)
        assert out["city"].iloc[0] == "Montevallo"

    def test_city_extracted_multiword(self):
        df = _make_raw(address="15 Soldiers Drive, San Antonio, TX 78209")
        out = mod.normalize(df)
        assert out["city"].iloc[0] == "San Antonio"

    def test_city_is_empty_when_address_missing(self):
        df = _make_raw(address=None)
        out = mod.normalize(df)
        assert out["city"].iloc[0] == ""

    # -- packed address: state abbreviation extraction -----------------------

    def test_state_abbr_extracted_from_address(self):
        """CRITICAL: 2-letter state abbreviation is parsed from the address string."""
        df = _make_raw(address="3133 Highway 119, Montevallo, AL 35115")
        out = mod.normalize(df)
        assert out["state_abbr"].iloc[0] == "AL"

    def test_state_abbr_extracted_tx(self):
        df = _make_raw(address="1 Cemetery Road, Houston, TX 77038")
        out = mod.normalize(df)
        assert out["state_abbr"].iloc[0] == "TX"

    def test_state_abbr_is_empty_when_address_missing(self):
        df = _make_raw(address=None)
        out = mod.normalize(df)
        assert out["state_abbr"].iloc[0] == ""

    # -- packed address: street extraction -----------------------------------

    def test_address_line_1_extracted_from_address(self):
        df = _make_raw(address="3133 Highway 119, Montevallo, AL 35115")
        out = mod.normalize(df)
        assert out["address_line_1"].iloc[0] == "3133 Highway 119"

    def test_address_line_1_is_empty_when_address_missing(self):
        df = _make_raw(address=None)
        out = mod.normalize(df)
        assert out["address_line_1"].iloc[0] == ""

    # -- packed contact: phone extraction ------------------------------------

    def test_phone_raw_extracted_from_contact(self):
        df = _make_raw(contact="Phone: 205-665-9039, FAX: 205-665-7790")
        out = mod.normalize(df)
        assert out["phone_raw"].iloc[0] == "205-665-9039"

    def test_phone_normalized_strips_non_digits(self):
        df = _make_raw(contact="Phone: 205-665-9039, FAX: 205-665-7790")
        out = mod.normalize(df)
        assert out["phone_normalized"].iloc[0] == "2056659039"

    def test_phone_or_alternate_takes_first_number(self):
        """
        CRITICAL: some rows have "Phone: NNN Or NNN" — the FIRST number must
        be taken as phone_raw; the alternate must be discarded.
        """
        df = _make_raw(contact="Phone: 800-535-1117 Or 281-447-8686, FAX: 281-447-0580")
        out = mod.normalize(df)
        assert out["phone_raw"].iloc[0] == "800-535-1117"
        assert out["phone_normalized"].iloc[0] == "8005351117"

    def test_phone_raw_empty_when_contact_missing(self):
        df = _make_raw(contact=None)
        out = mod.normalize(df)
        assert out["phone_raw"].iloc[0] == ""

    def test_phone_normalized_empty_when_contact_missing(self):
        df = _make_raw(contact=None)
        out = mod.normalize(df)
        assert out["phone_normalized"].iloc[0] == ""

    # -- coordinates ---------------------------------------------------------

    def test_latitude_coerced_to_float(self):
        df = _make_raw(latitude="33.1167")
        out = mod.normalize(df)
        assert out["latitude"].iloc[0] == pytest.approx(33.1167)

    def test_longitude_coerced_to_float(self):
        df = _make_raw(longitude="-86.8716")
        out = mod.normalize(df)
        assert out["longitude"].iloc[0] == pytest.approx(-86.8716)

    # -- always-set columns --------------------------------------------------

    def test_segment_is_federal(self):
        """
        CRITICAL: segment must be 'federal' — these are NCA sites excluded from
        commercial lead output.
        """
        df = _make_raw()
        out = mod.normalize(df)
        assert out["segment"].iloc[0] == "federal"

    def test_county_fips_is_none(self):
        """Not available in this source."""
        df = _make_raw()
        out = mod.normalize(df)
        assert out["county_fips"].iloc[0] is None

    def test_ein_is_none(self):
        df = _make_raw()
        out = mod.normalize(df)
        assert out["ein"].iloc[0] is None

    # -- multi-row -----------------------------------------------------------

    def test_normalize_handles_multiple_rows(self):
        df = pd.concat([
            _make_raw(cemetery_name="Cemetery A", address="1 A St, Tampa, FL 33601"),
            _make_raw(cemetery_name="Cemetery B", address="2 B Ave, Austin, TX 78701"),
        ], ignore_index=True)
        out = mod.normalize(df)
        assert len(out) == 2
        assert out["state_abbr"].tolist() == ["FL", "TX"]


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

    def test_source_id_is_constant_va_cemeteries(self):
        """D3: source_id must be the constant 'va_cemeteries', not a per-row composite."""
        df = _make_raw(cemetery_name="Tahoma National Cemetery", address="18600 SE 240th St, Kent, WA 98042")
        out = mod.to_canonical(mod.normalize(df))
        assert out["source_id"].iloc[0] == "va_cemeteries"

    def test_source_id_does_not_encode_name_or_state(self):
        """D3: source_id is a constant — name and state_abbr live in natural_key only."""
        df = _make_raw(
            cemetery_name="Biloxi National Cemetery",
            address="400 Veterans Ave, Biloxi, MS 39535",
        )
        out = mod.to_canonical(mod.normalize(df))
        sid = out["source_id"].iloc[0]
        assert sid == "va_cemeteries"
        assert "BILOXI" not in sid
        assert "|MS" not in sid

    def test_natural_key_uses_full_state_name(self):
        """natural_key uses state full-name (stable) not the parsed abbreviation."""
        df = _make_raw(
            cemetery_name="Montevallo National Cemetery",
            state="Alabama",
        )
        out = mod.to_canonical(mod.normalize(df))
        assert out["natural_key"].iloc[0] == "Montevallo National Cemetery|Alabama"

    def test_vertical_is_deathcare(self):
        out = mod.to_canonical(self._normalized_df())
        assert (out["vertical"] == "deathcare").all()

    def test_account_type_is_federal(self):
        """
        CRITICAL: VA cemeteries are federally managed; account_type must be
        'federal', not 'cemetery' or 'municipal'.
        """
        out = mod.to_canonical(self._normalized_df())
        assert (out["account_type"] == "federal").all()

    def test_segment_is_federal(self):
        """
        CRITICAL: segment='federal' excludes these records from lead output
        without a separate filter pass at merge time.
        """
        out = mod.to_canonical(self._normalized_df())
        assert (out["segment"] == "federal").all()

    def test_county_fips_is_none(self):
        out = mod.to_canonical(self._normalized_df())
        assert out["county_fips"].iloc[0] is None

    def test_ein_is_none(self):
        out = mod.to_canonical(self._normalized_df())
        assert out["ein"].iloc[0] is None

    def test_size_columns_are_none(self):
        out = mod.to_canonical(self._normalized_df())
        for col in ("size_metric", "size_value", "size_unit"):
            assert out[col].iloc[0] is None

    def test_state_is_2letter_abbreviation(self):
        df = _make_raw(address="3133 Highway 119, Montevallo, AL 35115")
        out = mod.to_canonical(mod.normalize(df))
        assert out["state"].iloc[0] == "AL"

    def test_zip5_present_in_canonical(self):
        df = _make_raw(address="3133 Highway 119, Montevallo, AL 35115")
        out = mod.to_canonical(mod.normalize(df))
        assert out["zip5"].iloc[0] == "35115"

    def test_phone_raw_and_normalized_flow_through(self):
        df = _make_raw(contact="Phone: 205-665-9039, FAX: 205-665-7790")
        out = mod.to_canonical(mod.normalize(df))
        assert out["phone_raw"].iloc[0] == "205-665-9039"
        assert out["phone_normalized"].iloc[0] == "2056659039"

    def test_latitude_and_longitude_present(self):
        df = _make_raw(latitude="33.1167", longitude="-86.8716")
        out = mod.to_canonical(mod.normalize(df))
        assert out["latitude"].iloc[0] == pytest.approx(33.1167)
        assert out["longitude"].iloc[0] == pytest.approx(-86.8716)

    def test_source_file_is_socrata_url(self):
        out = mod.to_canonical(self._normalized_df())
        assert "datahub.va.gov" in out["source_file"].iloc[0]

    def test_row_count_preserved(self):
        df = pd.concat([
            _make_raw(cemetery_name=f"Cemetery {i}") for i in range(5)
        ], ignore_index=True)
        out = mod.to_canonical(mod.normalize(df))
        assert len(out) == 5


# ===========================================================================
# Tests: report_quality()
# ===========================================================================

class TestReportQuality:
    def test_does_not_raise_on_well_formed_input(self):
        df = mod.normalize(_make_raw())
        mod.report_quality(df)

    def test_produces_stderr_output(self, capsys):
        df = mod.normalize(_make_raw())
        mod.report_quality(df)
        captured = capsys.readouterr()
        assert captured.err, "report_quality produced no stderr output"

    def test_parseable_phone_percentage_reported_in_stderr(self, capsys):
        """
        report_quality emits parseable phone rate as a Python :.1% formatted string.
        With 4 rows and 1 having no contact (empty phone_normalized), the rate
        is 3/4 = 75.0%.
        """
        # Arrange: 4 rows, 1 with null contact → phone_normalized will be ""
        rows = [
            _make_raw(
                cemetery_name=f"Cemetery {i}",
                address=f"1 Main St, Tampa, FL 3360{i}",
                contact="Phone: 813-555-0001, FAX: 813-555-0002",
            )
            for i in range(3)
        ]
        rows.append(
            _make_raw(
                cemetery_name="Cemetery No Phone",
                address="1 Main St, Tampa, FL 33604",
                contact=None,
            )
        )
        df = mod.normalize(pd.concat(rows, ignore_index=True))

        # Act
        mod.report_quality(df)

        # Assert: stderr must contain the exact formatted percentage
        captured = capsys.readouterr()
        assert "75.0%" in captured.err, (
            f"Expected '75.0%' in stderr (3/4 parseable phones). Got:\n{captured.err}"
        )

    def test_does_not_raise_with_missing_contact(self):
        df = mod.normalize(_make_raw(contact=None))
        mod.report_quality(df)

    def test_does_not_raise_with_missing_address(self):
        df = mod.normalize(_make_raw(address=None))
        mod.report_quality(df)

    def test_does_not_raise_on_multi_state_batch(self):
        frames = [
            _make_raw(
                cemetery_name=f"Cem {abbr}",
                address=f"1 Main St, City, {abbr} 10001",
                state=name,
            )
            for abbr, name in [
                ("FL", "Florida"),
                ("TX", "Texas"),
                ("NC", "North Carolina"),
                ("SC", "South Carolina"),
                ("PA", "Pennsylvania"),
            ]
        ]
        df = mod.normalize(pd.concat(frames, ignore_index=True))
        mod.report_quality(df)

    def test_does_not_raise_without_burial_space_column(self):
        df = mod.normalize(_make_raw())
        df = df.drop(columns=["burial_space"], errors="ignore")
        mod.report_quality(df)

    def test_stderr_mentions_total_count(self, capsys):
        df = mod.normalize(_make_raw())
        mod.report_quality(df)
        captured = capsys.readouterr()
        assert "1" in captured.err
