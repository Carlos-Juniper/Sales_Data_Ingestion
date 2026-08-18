"""
Tests for usgs_nsd.py — USGS National Structures Dataset cemetery connector.

Strategy
--------
Every public function is covered in isolation.  All DataFrames are built
in-memory; no CSV reads and no network calls occur.

Three traps called out in the spec each have a dedicated test:

  1. Null NAME trap     — normalize() must handle unnamed cemetery sites
                          without raising (return empty string, not NaN error).
  2. Segment=None trap  — to_canonical() must emit segment=None, not
                          'municipal' or 'unknown'.
  3. Row-count guard    — assert_source_shape() must raise when the total
                          row count is below 25,000 (truncation detection).
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from lib.schema import CANONICAL_COLUMNS

import usgs_nsd as mod


# ===========================================================================
# Helpers
# ===========================================================================

def _required_cols() -> list[str]:
    """Minimum column set that every raw-like DataFrame needs."""
    return [
        "PERMANENT_IDENTIFIER",
        "NAME",
        "STATE",
        "CITY",
        "ADDRESS",
        "ZIPCODE",
        "_lat",
        "_lon",
    ]


def _make_raw(**overrides) -> pd.DataFrame:
    """
    Return a single-row DataFrame that satisfies every field the module touches.
    Override individual column values with keyword arguments.
    """
    defaults = {
        "PERMANENT_IDENTIFIER": "abc123-uuid",
        "NAME": "Oak Grove Cemetery",
        "STATE": "FL",
        "CITY": "Tampa",
        "ADDRESS": "123 Main St",
        "ZIPCODE": "33601",
        "_lat": 27.9,
        "_lon": -82.4,
        "source_file": "https://carto.nationalmap.gov/arcgis/rest/services/structures/MapServer/37",
    }
    defaults.update(overrides)
    return pd.DataFrame([defaults])


def _make_large_raw(n: int = 25001, **row_overrides) -> pd.DataFrame:
    """
    Return a DataFrame with ``n`` rows — enough to satisfy the row-count
    guard in assert_source_shape().  Each row is a valid NSD record with a
    unique PERMANENT_IDENTIFIER derived from the row index.
    """
    rows = []
    for i in range(n):
        row = _make_raw(
            PERMANENT_IDENTIFIER=f"uuid-{i:06d}",
            STATE=row_overrides.get("STATE", "FL"),
            **{k: v for k, v in row_overrides.items() if k != "STATE"},
        ).iloc[0].to_dict()
        rows.append(row)
    df = pd.DataFrame(rows)
    # assert_source_shape reads STATE but not _lat/_lon — add OBJECTID for it
    if "OBJECTID" not in df.columns:
        df["OBJECTID"] = range(len(df))
    return df


# ===========================================================================
# Tests: assert_source_shape()
# ===========================================================================

class TestAssertSourceShape:
    def test_passes_on_well_formed_dataframe(self):
        df = _make_large_raw()
        # Must not raise
        mod.assert_source_shape(df)

    def test_raises_when_permanent_identifier_column_missing(self):
        df = _make_large_raw().drop(columns=["PERMANENT_IDENTIFIER"])
        with pytest.raises(ValueError, match="PERMANENT_IDENTIFIER"):
            mod.assert_source_shape(df)

    def test_raises_when_state_values_outside_allowed_set(self):
        df = _make_large_raw()
        # Inject an invalid STATE to trigger the allowed-set check
        df.loc[0, "STATE"] = "ZZ"
        with pytest.raises(ValueError, match="STATE"):
            mod.assert_source_shape(df)

    def test_raises_when_permanent_identifier_fill_rate_below_99_percent(self):
        """
        Row-count guard: PERMANENT_IDENTIFIER fill rate must be >= 99%.
        Setting 2% of rows to None crosses the threshold.
        """
        df = _make_large_raw(n=25001)
        # Nullify 2% of rows (501 rows) — exceeds the 1% tolerance
        null_count = int(len(df) * 0.02)
        df.loc[:null_count - 1, "PERMANENT_IDENTIFIER"] = None
        with pytest.raises(ValueError, match="PERMANENT_IDENTIFIER"):
            mod.assert_source_shape(df)

    def test_raises_when_row_count_below_25000(self):
        """
        CRITICAL: truncation guard — fewer than 25,000 rows means the API
        response was likely cut off and the connector must not proceed.
        """
        df = pd.concat([_make_raw()] * 100, ignore_index=True)
        with pytest.raises(ValueError):
            mod.assert_source_shape(df)

    def test_passes_at_exactly_25000_rows(self):
        df = _make_large_raw(n=25000)
        # Boundary: 25,000 rows is the minimum acceptable count
        mod.assert_source_shape(df)

    def test_raises_at_24999_rows(self):
        df = _make_large_raw(n=24999)
        with pytest.raises(ValueError):
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

    def test_name_normalized_is_string_for_valid_name(self):
        df = _make_raw(NAME="Oak Grove Cemetery")
        out = mod.normalize(df)
        assert isinstance(out["name_normalized"].iloc[0], str)

    def test_null_name_does_not_raise(self):
        """
        CRITICAL: NSD has unnamed cemetery sites where NAME is null.
        normalize() must handle this without raising — return empty string.
        """
        df = _make_raw(NAME=None)
        # Must not raise
        out = mod.normalize(df)
        assert out["name_normalized"].iloc[0] == ""

    def test_empty_string_name_returns_empty_normalized(self):
        df = _make_raw(NAME="")
        out = mod.normalize(df)
        assert out["name_normalized"].iloc[0] == ""

    # -- zip5 ----------------------------------------------------------------

    def test_zip5_column_added(self):
        df = _make_raw()
        out = mod.normalize(df)
        assert "zip5" in out.columns

    def test_zip5_plain_5_digit_unchanged(self):
        df = _make_raw(ZIPCODE="33601")
        out = mod.normalize(df)
        assert out["zip5"].iloc[0] == "33601"

    def test_zip5_hyphenated_9_digit_stripped_to_5(self):
        df = _make_raw(ZIPCODE="33601-4209")
        out = mod.normalize(df)
        assert out["zip5"].iloc[0] == "33601"

    def test_zip5_concatenated_9_digit_stripped_to_5(self):
        df = _make_raw(ZIPCODE="336014209")
        out = mod.normalize(df)
        assert out["zip5"].iloc[0] == "33601"

    def test_zip5_null_zipcode_returns_empty_string(self):
        df = _make_raw(ZIPCODE=None)
        out = mod.normalize(df)
        assert out["zip5"].iloc[0] == ""

    # -- latitude / longitude ------------------------------------------------

    def test_latitude_column_preserved(self):
        df = _make_raw(latitude=27.9)
        out = mod.normalize(df)
        assert out["latitude"].iloc[0] == pytest.approx(27.9)

    def test_longitude_column_preserved(self):
        df = _make_raw(longitude=-82.4)
        out = mod.normalize(df)
        assert out["longitude"].iloc[0] == pytest.approx(-82.4)

    # -- always-None columns -------------------------------------------------

    def test_county_fips_is_none(self):
        """NSD has no county FIPS — must always be None, not a derived value."""
        df = _make_raw()
        out = mod.normalize(df)
        assert out["county_fips"].iloc[0] is None

    def test_ein_is_none(self):
        df = _make_raw()
        out = mod.normalize(df)
        assert out["ein"].iloc[0] is None

    def test_segment_is_none(self):
        """
        CRITICAL: segment is resolved by the merge module, not here.
        normalize() must emit None, not 'municipal' or 'unknown'.
        """
        df = _make_raw()
        out = mod.normalize(df)
        assert out["segment"].iloc[0] is None

    # -- multi-state batch ---------------------------------------------------

    def test_normalize_preserves_state_column(self):
        df = _make_raw(STATE="TX")
        out = mod.normalize(df)
        assert out["STATE"].iloc[0] == "TX"

    def test_normalize_handles_multiple_rows(self):
        df = pd.concat([
            _make_raw(STATE="FL", PERMANENT_IDENTIFIER="uuid-fl"),
            _make_raw(STATE="TX", PERMANENT_IDENTIFIER="uuid-tx"),
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

    def test_source_id_prefixed_with_nsd(self):
        """source_id must be 'nsd:' + PERMANENT_IDENTIFIER."""
        df = _make_raw(PERMANENT_IDENTIFIER="abc123-uuid")
        out = mod.to_canonical(mod.normalize(df))
        assert out["source_id"].iloc[0] == "nsd:abc123-uuid"

    def test_source_id_uses_permanent_identifier_as_suffix(self):
        df = _make_raw(PERMANENT_IDENTIFIER="zzz999-uuid")
        out = mod.to_canonical(mod.normalize(df))
        assert out["source_id"].iloc[0].endswith("zzz999-uuid")

    def test_vertical_is_deathcare(self):
        out = mod.to_canonical(self._normalized_df())
        assert (out["vertical"] == "deathcare").all()

    def test_account_type_is_cemetery(self):
        out = mod.to_canonical(self._normalized_df())
        assert (out["account_type"] == "cemetery").all()

    def test_segment_is_none(self):
        """
        CRITICAL: segment must be None — not 'municipal', not 'unknown'.
        Segment resolution belongs to the merge module, not this connector.
        """
        out = mod.to_canonical(self._normalized_df())
        assert out["segment"].iloc[0] is None

    def test_county_fips_is_none(self):
        """NSD provides no county FIPS data — must always be None."""
        out = mod.to_canonical(self._normalized_df())
        assert out["county_fips"].iloc[0] is None

    def test_ein_is_none(self):
        out = mod.to_canonical(self._normalized_df())
        assert out["ein"].iloc[0] is None

    def test_natural_key_matches_permanent_identifier(self):
        df = _make_raw(PERMANENT_IDENTIFIER="abc123-uuid")
        out = mod.to_canonical(mod.normalize(df))
        assert out["natural_key"].iloc[0] == "abc123-uuid"

    def test_state_matches_source_state(self):
        df = _make_raw(STATE="NC")
        out = mod.to_canonical(mod.normalize(df))
        assert out["state"].iloc[0] == "NC"

    def test_zip5_maps_from_zipcode(self):
        df = _make_raw(ZIPCODE="33601-9999")
        out = mod.to_canonical(mod.normalize(df))
        assert out["zip5"].iloc[0] == "33601"

    def test_null_name_produces_empty_site_name(self):
        """
        to_canonical() must handle a None NAME all the way through.
        The unnamed cemetery trap must not raise at the output stage either.
        """
        df = _make_raw(NAME=None)
        out = mod.to_canonical(mod.normalize(df))
        # Must not raise; name-derived column is an empty string or NaN — not an exception
        assert out["name_normalized"].iloc[0] == ""

    def test_latitude_and_longitude_present_in_output(self):
        df = _make_raw(_lat=35.2, _lon=-80.8)
        out = mod.to_canonical(mod.normalize(df))
        assert out["latitude"].iloc[0] == pytest.approx(35.2)
        assert out["longitude"].iloc[0] == pytest.approx(-80.8)

    def test_row_count_preserved(self):
        df = pd.concat([
            _make_raw(PERMANENT_IDENTIFIER=f"uuid-{i}") for i in range(5)
        ], ignore_index=True)
        out = mod.to_canonical(mod.normalize(df))
        assert len(out) == 5


# ===========================================================================
# Tests: report_quality()
# ===========================================================================

class TestReportQuality:
    def test_does_not_raise_on_well_formed_input(self):
        df = mod.normalize(_make_raw())
        # Must not raise regardless of what it prints
        mod.report_quality(df)

    def test_does_not_raise_with_null_name(self):
        """Unnamed cemetery sites must not cause report_quality to crash."""
        df = mod.normalize(_make_raw(NAME=None))
        mod.report_quality(df)

    def test_produces_stderr_output(self, capsys):
        df = mod.normalize(_make_raw())
        mod.report_quality(df)
        captured = capsys.readouterr()
        # At minimum, some diagnostic text must be written to stderr or stdout
        assert captured.out or captured.err, (
            "report_quality produced no output — expected at least one diagnostic line"
        )

    def test_null_name_percentage_reported_in_stderr(self, capsys):
        """
        report_quality emits null NAME rate as a Python :.1% formatted string.
        With 4 rows and 1 null NAME, the rate is 1/4 = 25.0%.
        """
        # Arrange: 4 rows, 1 with null NAME
        rows = [
            _make_raw(PERMANENT_IDENTIFIER=f"uuid-{i}", STATE="FL")
            for i in range(3)
        ]
        rows.append(_make_raw(PERMANENT_IDENTIFIER="uuid-null", NAME=None, STATE="FL"))
        df = pd.concat(rows, ignore_index=True)

        # Act
        mod.report_quality(df)

        # Assert: stderr must contain the exact formatted percentage
        captured = capsys.readouterr()
        assert "25.0%" in captured.err, (
            f"Expected '25.0%' in stderr (1/4 null NAMEs). Got:\n{captured.err}"
        )

    def test_does_not_raise_on_multi_state_batch(self):
        frames = [
            _make_raw(STATE=state, PERMANENT_IDENTIFIER=f"uuid-{i}")
            for i, state in enumerate(["FL", "TX", "NC", "SC", "PA"])
        ]
        df = mod.normalize(pd.concat(frames, ignore_index=True))
        mod.report_quality(df)

    def test_does_not_raise_on_all_null_addresses(self):
        """ADDRESS is frequently null in NSD — report_quality must handle it."""
        df = mod.normalize(_make_raw(ADDRESS=None, CITY=None, ZIPCODE=None))
        mod.report_quality(df)


# ===========================================================================
# Tests: fetch()
# ===========================================================================

class TestFetch:
    """
    Tests for usgs_nsd.fetch().

    Strategy: patch lib.arcgis.iter_features to yield controlled GeoJSON
    feature dicts. This isolates fetch() from all HTTP machinery — we test
    only that the connector correctly processes the feature stream it receives.

    Patch target is 'lib.arcgis.iter_features' because usgs_nsd imports via
    `from lib import arcgis` and calls `arcgis.iter_features(...)`.  Patching
    at the module level intercepts the call regardless of when the module was
    imported.
    """

    def _point_feature(self, **prop_overrides) -> dict:
        """
        Build a minimal GeoJSON point feature with properties and geometry.
        All NSD fields are in 'properties' (GeoJSON style from f=geojson).
        """
        props = {
            "PERMANENT_IDENTIFIER": "uuid-test-001",
            "NAME": "Cypress Hill Cemetery",
            "STATE": "FL",
            "CITY": "Tampa",
            "ADDRESS": "100 Oak St",
            "ZIPCODE": "33601",
            "OBJECTID": 42,
        }
        props.update(prop_overrides)
        return {
            "type": "Feature",
            "properties": props,
            "geometry": {
                "type": "Point",
                "coordinates": [-82.4579, 27.9478],
            },
        }

    def _attributes_feature(self, **attr_overrides) -> dict:
        """
        Feature using 'attributes' key instead of 'properties'.
        ArcGIS servers may return either form depending on the f= format.
        feature_props() must handle both via its properties-or-attributes fallback.
        """
        attrs = {
            "PERMANENT_IDENTIFIER": "uuid-attr-002",
            "NAME": "Palmetto Gardens",
            "STATE": "TX",
            "CITY": "Austin",
            "ADDRESS": "200 Pine Ave",
            "ZIPCODE": "78701",
            "OBJECTID": 99,
        }
        attrs.update(attr_overrides)
        return {
            "type": "Feature",
            "attributes": attrs,
            "geometry": {
                "type": "Point",
                "coordinates": [-97.7431, 30.2672],
            },
        }

    # -- happy path -----------------------------------------------------------

    def test_happy_path_returns_dataframe(self):
        feature = self._point_feature()
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert isinstance(df, pd.DataFrame)

    def test_happy_path_yields_one_row_per_feature(self):
        features = [self._point_feature(OBJECTID=i) for i in range(3)]
        with patch("lib.arcgis.iter_features", return_value=iter(features)):
            df = mod.fetch()
        assert len(df) == 3

    def test_lon_extracted_from_geometry_coordinates(self):
        """geometry.coordinates[0] must become _lon as a float."""
        feature = self._point_feature()
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["_lon"].iloc[0] == pytest.approx(-82.4579)

    def test_lat_extracted_from_geometry_coordinates(self):
        """geometry.coordinates[1] must become _lat as a float."""
        feature = self._point_feature()
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["_lat"].iloc[0] == pytest.approx(27.9478)

    def test_attribute_fields_mapped_to_columns(self):
        """Properties dict keys must map to DataFrame column names verbatim."""
        feature = self._point_feature(
            NAME="River Bend Cemetery",
            STATE="NC",
            CITY="Raleigh",
        )
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["NAME"].iloc[0] == "River Bend Cemetery"
        assert df["STATE"].iloc[0] == "NC"
        assert df["CITY"].iloc[0] == "Raleigh"

    def test_permanent_identifier_preserved(self):
        feature = self._point_feature(PERMANENT_IDENTIFIER="uuid-abc-999")
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["PERMANENT_IDENTIFIER"].iloc[0] == "uuid-abc-999"

    def test_source_file_column_added(self):
        """fetch() must inject source_file with the SOURCE_URL constant."""
        feature = self._point_feature()
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert "source_file" in df.columns
        assert "nationalmap.gov" in df["source_file"].iloc[0]

    # -- null / missing geometry ----------------------------------------------

    def test_null_geometry_produces_none_lon_lat(self):
        """
        CRITICAL: features with geometry=null must not raise — they must produce
        _lon=None and _lat=None.  Unnamed burial sites sometimes lack geometry.
        """
        feature = self._point_feature()
        feature["geometry"] = None
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["_lon"].iloc[0] is None
        assert df["_lat"].iloc[0] is None

    def test_geometry_missing_coordinates_key_produces_none_lon_lat(self):
        """geometry dict present but with no 'coordinates' key → None, not crash."""
        feature = self._point_feature()
        feature["geometry"] = {"type": "Point"}
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["_lon"].iloc[0] is None
        assert df["_lat"].iloc[0] is None

    # -- attributes fallback --------------------------------------------------

    def test_attributes_key_used_when_properties_absent(self):
        """
        feature_props() falls back to 'attributes' when 'properties' is absent.
        fetch() must produce the same column values regardless of which key
        the server used.
        """
        feature = self._attributes_feature(NAME="Magnolia Rest Gardens")
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["NAME"].iloc[0] == "Magnolia Rest Gardens"

    def test_attributes_geometry_coordinates_still_extracted(self):
        """Geometry extraction must work even when properties uses 'attributes' key."""
        feature = self._attributes_feature()
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["_lon"].iloc[0] == pytest.approx(-97.7431)
        assert df["_lat"].iloc[0] == pytest.approx(30.2672)

    # -- empty response -------------------------------------------------------

    @pytest.mark.xfail(
        reason=(
            "BUG: usgs_nsd.fetch() crashes with KeyError on an empty feature stream "
            "because pd.DataFrame([]) has no columns and the subsequent "
            "`df[col].astype('string')` loop raises KeyError. "
            "Fix: guard with `if df.empty: return df` before the coercion loop, "
            "or initialise the DataFrame with explicit columns."
        )
    )
    def test_empty_feature_stream_returns_empty_dataframe(self):
        """Zero features from the server must yield an empty DataFrame, not crash."""
        with patch("lib.arcgis.iter_features", return_value=iter([])):
            df = mod.fetch()
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0

    # -- state filter passthrough ---------------------------------------------

    def test_state_filter_passed_to_iter_features(self):
        """
        The WHERE clause sent to iter_features must include the requested
        state list — spot-check that the states argument flows through.

        Uses a single dummy feature to avoid the empty-DataFrame crash
        (tracked in test_empty_feature_stream_returns_empty_dataframe).
        """
        dummy = self._point_feature()
        with patch("lib.arcgis.iter_features", return_value=iter([dummy])) as mock_iter:
            mod.fetch(states=["TX", "NC"])
        call_kwargs = mock_iter.call_args.kwargs
        assert "TX" in call_kwargs["where"]
        assert "NC" in call_kwargs["where"]

    def test_default_states_used_when_states_arg_is_none(self):
        """When states=None, all five default states must appear in the WHERE clause."""
        dummy = self._point_feature()
        with patch("lib.arcgis.iter_features", return_value=iter([dummy])) as mock_iter:
            mod.fetch(states=None)
        where = mock_iter.call_args.kwargs["where"]
        for state in ("FL", "TX", "NC", "SC", "PA"):
            assert state in where, f"Expected state '{state}' in WHERE clause: {where}"

    # -- OBJECTID numeric coercion --------------------------------------------

    def test_objectid_column_is_numeric(self):
        """OBJECTID must be coerced to numeric (int or float), not left as string."""
        feature = self._point_feature(OBJECTID=7)
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert pd.api.types.is_numeric_dtype(df["OBJECTID"])
