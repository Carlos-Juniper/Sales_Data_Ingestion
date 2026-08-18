"""
Tests for txdot_cemeteries.py — TxDOT Texas Cemeteries ArcGIS connector.

Strategy
--------
Every public function is covered in isolation. All DataFrames are built
in-memory; no CSV reads and no network calls occur.

Five traps called out in the spec each have a dedicated test:

  1. Null CEMETERY_NM trap  — normalize() and to_canonical() must handle
                              unnamed cemeteries without raising.
  2. GID float-to-int trap  — GID=2.0 must become natural_key='2', never
                              '2.0'. Floating-point suffixes break dedup.
  3. source_id prefix trap  — source_id must be 'txdot:2', not 'txdot:2.0'
                              or '2'.
  4. county_fips=None trap  — CNTY_NBR is a county integer code (1–254),
                              NOT a FIPS code. Must never appear in county_fips.
  5. segment=None trap      — segment is resolved by the merge module, not here.
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from lib.schema import CANONICAL_COLUMNS

import txdot_cemeteries as mod


# ===========================================================================
# Helpers
# ===========================================================================

def _make_raw(**overrides) -> pd.DataFrame:
    """
    Return a single-row DataFrame that satisfies every field the module touches.
    Override individual column values with keyword arguments.
    """
    defaults = {
        "_lat": 30.0,
        "_lon": -97.0,
        "GID": 2.0,
        "CEMETERY_NM": "Oak Cemetery",
        "OBJECTID": 1,
        "CITY_NM": None,
        "CNTY_NBR": 227,
        "DIST_NM": "Austin",
        "source_file": (
            "https://services.arcgis.com/KTcxiTD9dsQw4r7Z/arcgis/rest/services"
            "/Texas_Cemeteries/FeatureServer/0"
        ),
    }
    defaults.update(overrides)
    return pd.DataFrame([defaults])


def _make_large_raw(n: int = 6001, **row_overrides) -> pd.DataFrame:
    """
    Return a DataFrame with ``n`` rows — enough to satisfy the row-count
    guard in assert_source_shape(). Each row has a unique GID.
    """
    rows = []
    for i in range(n):
        row = _make_raw(
            GID=float(i + 1),
            OBJECTID=i + 1,
            **row_overrides,
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

    def test_raises_when_gid_column_missing(self):
        df = _make_large_raw().drop(columns=["GID"])
        with pytest.raises(ValueError, match="GID"):
            mod.assert_source_shape(df)

    def test_raises_when_cemetery_nm_column_missing(self):
        df = _make_large_raw().drop(columns=["CEMETERY_NM"])
        with pytest.raises(ValueError, match="CEMETERY_NM"):
            mod.assert_source_shape(df)

    def test_raises_when_objectid_column_missing(self):
        df = _make_large_raw().drop(columns=["OBJECTID"])
        with pytest.raises(ValueError, match="OBJECTID"):
            mod.assert_source_shape(df)

    def test_raises_when_gid_fill_rate_below_95_percent(self):
        """
        GID fill rate must be >= 95%. Setting 6% of rows to None crosses
        the threshold and must trigger a ValueError.
        """
        df = _make_large_raw(n=6001)
        null_count = int(len(df) * 0.06)
        df.loc[:null_count - 1, "GID"] = None
        with pytest.raises(ValueError, match="GID"):
            mod.assert_source_shape(df)

    def test_passes_when_gid_fill_rate_at_95_percent(self):
        df = _make_large_raw(n=6001)
        # Null exactly 5% — boundary should pass
        null_count = int(len(df) * 0.05)
        df.loc[:null_count - 1, "GID"] = None
        mod.assert_source_shape(df)

    def test_raises_when_row_count_below_6000(self):
        """
        CRITICAL: truncation guard — fewer than 6,000 rows means the API
        response was cut off and the connector must not proceed.
        """
        df = pd.concat([_make_raw()] * 100, ignore_index=True)
        with pytest.raises(ValueError):
            mod.assert_source_shape(df)

    def test_passes_at_exactly_6000_rows(self):
        df = _make_large_raw(n=6000)
        mod.assert_source_shape(df)

    def test_raises_at_5999_rows(self):
        df = _make_large_raw(n=5999)
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
        df = _make_raw(CEMETERY_NM="Oak Cemetery")
        out = mod.normalize(df)
        assert isinstance(out["name_normalized"].iloc[0], str)

    def test_null_cemetery_nm_does_not_raise(self):
        """
        CRITICAL: CEMETERY_NM can be null for unnamed cemeteries.
        normalize() must handle this without raising — return empty string.
        """
        df = _make_raw(CEMETERY_NM=None)
        out = mod.normalize(df)
        assert out["name_normalized"].iloc[0] == ""

    def test_empty_string_cemetery_nm_returns_empty_normalized(self):
        df = _make_raw(CEMETERY_NM="")
        out = mod.normalize(df)
        assert out["name_normalized"].iloc[0] == ""

    # -- natural_key_str (GID float → integer string) -----------------------

    def test_gid_float_becomes_integer_string(self):
        """
        CRITICAL: GID=2.0 must become '2', not '2.0'. Floating-point suffixes
        in the natural key break downstream deduplication.
        """
        df = _make_raw(GID=2.0)
        out = mod.normalize(df)
        assert out["natural_key_str"].iloc[0] == "2"

    def test_gid_larger_value_strips_decimal(self):
        df = _make_raw(GID=1234.0)
        out = mod.normalize(df)
        assert out["natural_key_str"].iloc[0] == "1234"

    def test_gid_null_produces_empty_string(self):
        df = _make_raw(GID=None)
        out = mod.normalize(df)
        assert out["natural_key_str"].iloc[0] == ""

    # -- latitude / longitude ------------------------------------------------

    def test_latitude_column_populated_from_lat(self):
        df = _make_raw(_lat=30.0)
        out = mod.normalize(df)
        assert out["latitude"].iloc[0] == pytest.approx(30.0)

    def test_longitude_column_populated_from_lon(self):
        df = _make_raw(_lon=-97.0)
        out = mod.normalize(df)
        assert out["longitude"].iloc[0] == pytest.approx(-97.0)

    # -- state is always TX --------------------------------------------------

    def test_state_is_always_tx(self):
        """All TxDOT cemetery records are Texas — state must be 'TX' always."""
        df = _make_raw()
        out = mod.normalize(df)
        assert out["state"].iloc[0] == "TX"

    # -- city passes through (may be null) -----------------------------------

    def test_city_passes_through_when_present(self):
        df = _make_raw(CITY_NM="Austin")
        out = mod.normalize(df)
        assert out["city"].iloc[0] == "Austin"

    def test_city_is_null_when_city_nm_null(self):
        df = _make_raw(CITY_NM=None)
        out = mod.normalize(df)
        assert pd.isna(out["city"].iloc[0])

    # -- always-None columns -------------------------------------------------

    def test_county_fips_is_none(self):
        """
        CRITICAL: CNTY_NBR is a TxDOT county integer code (1–254), NOT a FIPS
        code. county_fips must always be None — never populate it from CNTY_NBR.
        """
        df = _make_raw(CNTY_NBR=227)
        out = mod.normalize(df)
        assert out["county_fips"].iloc[0] is None

    def test_ein_is_none(self):
        df = _make_raw()
        out = mod.normalize(df)
        assert out["ein"].iloc[0] is None

    def test_zip5_is_none(self):
        """No ZIP data exists in this layer — zip5 must always be None."""
        df = _make_raw()
        out = mod.normalize(df)
        assert out["zip5"].iloc[0] is None

    def test_segment_is_none(self):
        """
        CRITICAL: segment is resolved by the merge module, not here.
        normalize() must emit None, not 'municipal' or 'unknown'.
        """
        df = _make_raw()
        out = mod.normalize(df)
        assert out["segment"].iloc[0] is None

    # -- multi-row batch -----------------------------------------------------

    def test_normalize_handles_multiple_rows(self):
        df = pd.concat([
            _make_raw(GID=1.0, CEMETERY_NM="Alpha Cemetery"),
            _make_raw(GID=2.0, CEMETERY_NM="Beta Cemetery"),
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

    def test_source_id_prefixed_with_txdot(self):
        """
        CRITICAL: source_id must be 'txdot:' + integer string of GID.
        GID=2.0 → source_id='txdot:2', not 'txdot:2.0'.
        """
        df = _make_raw(GID=2.0)
        out = mod.to_canonical(mod.normalize(df))
        assert out["source_id"].iloc[0] == "txdot:2"

    def test_source_id_uses_integer_string_not_float(self):
        """source_id must not contain a decimal point from the GID double field."""
        df = _make_raw(GID=42.0)
        out = mod.to_canonical(mod.normalize(df))
        assert "." not in out["source_id"].iloc[0]

    def test_natural_key_is_integer_string_of_gid(self):
        """
        CRITICAL: natural_key must be the integer string of GID with no prefix.
        GID=2.0 → natural_key='2'.
        """
        df = _make_raw(GID=2.0)
        out = mod.to_canonical(mod.normalize(df))
        assert out["natural_key"].iloc[0] == "2"

    def test_vertical_is_deathcare(self):
        out = mod.to_canonical(self._normalized_df())
        assert (out["vertical"] == "deathcare").all()

    def test_account_type_is_cemetery(self):
        out = mod.to_canonical(self._normalized_df())
        assert (out["account_type"] == "cemetery").all()

    def test_state_is_always_tx(self):
        """State must be 'TX' — all records are Texas."""
        out = mod.to_canonical(self._normalized_df())
        assert (out["state"] == "TX").all()

    def test_segment_is_none(self):
        """
        CRITICAL: segment must be None — not 'municipal', not 'unknown'.
        Segment resolution belongs to the merge module, not this connector.
        """
        out = mod.to_canonical(self._normalized_df())
        assert out["segment"].iloc[0] is None

    def test_county_fips_is_none(self):
        """
        CRITICAL: CNTY_NBR is NOT a FIPS code — county_fips must always be None.
        """
        df = _make_raw(CNTY_NBR=227)
        out = mod.to_canonical(mod.normalize(df))
        assert out["county_fips"].iloc[0] is None

    def test_ein_is_none(self):
        out = mod.to_canonical(self._normalized_df())
        assert out["ein"].iloc[0] is None

    def test_zip5_is_none(self):
        """No ZIP data in this layer — zip5 must be None throughout."""
        out = mod.to_canonical(self._normalized_df())
        assert out["zip5"].iloc[0] is None

    def test_address_line_1_is_none(self):
        """No address fields exist in this layer."""
        out = mod.to_canonical(self._normalized_df())
        assert out["address_line_1"].iloc[0] is None

    def test_null_cemetery_nm_does_not_raise(self):
        """
        CRITICAL: to_canonical() must handle a None CEMETERY_NM all the way
        through. The unnamed cemetery trap must not raise at the output stage.
        """
        df = _make_raw(CEMETERY_NM=None)
        out = mod.to_canonical(mod.normalize(df))
        assert out["name_normalized"].iloc[0] == ""

    def test_latitude_and_longitude_present_in_output(self):
        df = _make_raw(_lat=30.0, _lon=-97.0)
        out = mod.to_canonical(mod.normalize(df))
        assert out["latitude"].iloc[0] == pytest.approx(30.0)
        assert out["longitude"].iloc[0] == pytest.approx(-97.0)

    def test_row_count_preserved(self):
        df = pd.concat([
            _make_raw(GID=float(i)) for i in range(1, 6)
        ], ignore_index=True)
        out = mod.to_canonical(mod.normalize(df))
        assert len(out) == 5

    def test_source_file_passes_through(self):
        out = mod.to_canonical(self._normalized_df())
        assert "Texas_Cemeteries" in out["source_file"].iloc[0]


# ===========================================================================
# Tests: report_quality()
# ===========================================================================

class TestReportQuality:
    def test_does_not_raise_on_well_formed_input(self):
        df = mod.normalize(_make_raw())
        mod.report_quality(df)

    def test_does_not_raise_with_null_cemetery_nm(self):
        """Unnamed cemeteries must not cause report_quality to crash."""
        df = mod.normalize(_make_raw(CEMETERY_NM=None))
        mod.report_quality(df)

    def test_does_not_raise_with_null_city_nm(self):
        """CITY_NM is frequently null — report_quality must handle it."""
        df = mod.normalize(_make_raw(CITY_NM=None))
        mod.report_quality(df)

    def test_produces_stderr_output(self, capsys):
        # Arrange: 4 rows, 1 with a null CEMETERY_NM (unnamed cemetery).
        # report_quality writes "null CEMETERY_NM  {null_name:.1%}", so
        # 1/4 null → "25.0%".
        frames = [
            _make_raw(GID=1.0, CEMETERY_NM="Alpha Cemetery"),
            _make_raw(GID=2.0, CEMETERY_NM="Beta Cemetery"),
            _make_raw(GID=3.0, CEMETERY_NM="Gamma Cemetery"),
            _make_raw(GID=4.0, CEMETERY_NM=None),
        ]
        df = mod.normalize(pd.concat(frames, ignore_index=True))

        # Act
        mod.report_quality(df)

        # Assert: stderr carries the expected null-name percentage
        captured = capsys.readouterr()
        assert "25.0%" in captured.err, (
            f"Expected '25.0%' in stderr (1/4 null CEMETERY_NM), got: {captured.err!r}"
        )

    def test_does_not_raise_on_multi_district_batch(self):
        frames = [
            _make_raw(GID=float(i), DIST_NM=dist)
            for i, dist in enumerate(
                ["Austin", "Abilene", "Amarillo", "Atlanta", "Beaumont"], start=1
            )
        ]
        df = mod.normalize(pd.concat(frames, ignore_index=True))
        mod.report_quality(df)


# ===========================================================================
# Tests: fetch()
# ===========================================================================

class TestFetch:
    """
    Tests for txdot_cemeteries.fetch().

    Strategy: patch lib.arcgis.iter_features to yield controlled GeoJSON
    feature dicts. fetch() calls both feature_props() and feature_lonlat(),
    so tests cover both the attribute extraction path and the geometry
    coordinate path.

    Patch target is 'lib.arcgis.iter_features' — txdot_cemeteries imports via
    `from lib import arcgis` and calls `arcgis.iter_features(...)`.
    """

    def _point_feature(self, **prop_overrides) -> dict:
        """
        Build a minimal TxDOT GeoJSON point feature.
        GID is a double field — pass it as float to match live API behaviour.
        """
        props = {
            "CEMETERY_NM": "Oak Hill Cemetery",
            "CITY_NM": "Austin",
            "CNTY_NBR": 227,
            "DIST_NM": "Austin",
            "GID": 2.0,
            "OBJECTID": 1,
        }
        props.update(prop_overrides)
        return {
            "type": "Feature",
            "properties": props,
            "geometry": {
                "type": "Point",
                "coordinates": [-97.7431, 30.2672],
            },
        }

    def _attributes_feature(self, **attr_overrides) -> dict:
        """Feature using 'attributes' key — tests feature_props() fallback."""
        attrs = {
            "CEMETERY_NM": "Bluebonnet Cemetery",
            "CITY_NM": None,
            "CNTY_NBR": 113,
            "DIST_NM": "Abilene",
            "GID": 5.0,
            "OBJECTID": 2,
        }
        attrs.update(attr_overrides)
        return {
            "type": "Feature",
            "attributes": attrs,
            "geometry": {
                "type": "Point",
                "coordinates": [-99.7290, 32.4487],
            },
        }

    # -- happy path -----------------------------------------------------------

    def test_happy_path_returns_dataframe(self):
        feature = self._point_feature()
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert isinstance(df, pd.DataFrame)

    def test_happy_path_yields_one_row_per_feature(self):
        features = [self._point_feature(GID=float(i), OBJECTID=i) for i in range(1, 4)]
        with patch("lib.arcgis.iter_features", return_value=iter(features)):
            df = mod.fetch()
        assert len(df) == 3

    def test_lon_extracted_from_geometry_coordinates(self):
        """geometry.coordinates[0] must become _lon as float."""
        feature = self._point_feature()
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["_lon"].iloc[0] == pytest.approx(-97.7431)

    def test_lat_extracted_from_geometry_coordinates(self):
        """geometry.coordinates[1] must become _lat as float."""
        feature = self._point_feature()
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["_lat"].iloc[0] == pytest.approx(30.2672)

    def test_cemetery_nm_column_populated(self):
        feature = self._point_feature(CEMETERY_NM="Pecan Grove Memorial")
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["CEMETERY_NM"].iloc[0] == "Pecan Grove Memorial"

    def test_gid_column_populated(self):
        """GID arrives as a double — fetch() must preserve the raw value."""
        feature = self._point_feature(GID=42.0)
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["GID"].iloc[0] == pytest.approx(42.0)

    def test_cnty_nbr_column_populated(self):
        """CNTY_NBR is a county integer code — must pass through as-is."""
        feature = self._point_feature(CNTY_NBR=113)
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["CNTY_NBR"].iloc[0] == 113

    def test_source_file_column_added(self):
        feature = self._point_feature()
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert "source_file" in df.columns
        assert "Texas_Cemeteries" in df["source_file"].iloc[0]

    # -- null / missing geometry ----------------------------------------------

    def test_null_geometry_produces_none_lon_lat(self):
        """
        CRITICAL: TxDOT is geometry-only for coordinates — no lat/lon attribute
        fields exist. A null geometry must produce _lon=None and _lat=None,
        not raise or silently produce 0.0.
        """
        feature = self._point_feature()
        feature["geometry"] = None
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["_lon"].iloc[0] is None
        assert df["_lat"].iloc[0] is None

    def test_geometry_missing_coordinates_produces_none_lon_lat(self):
        """geometry dict present but 'coordinates' key absent → None, not crash."""
        feature = self._point_feature()
        feature["geometry"] = {"type": "Point"}
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["_lon"].iloc[0] is None
        assert df["_lat"].iloc[0] is None

    # -- null CITY_NM ---------------------------------------------------------

    def test_null_city_nm_preserved_as_none(self):
        """
        CITY_NM is frequently null in TxDOT data.
        fetch() must not coerce None to an empty string or raise.
        """
        feature = self._point_feature(CITY_NM=None)
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["CITY_NM"].iloc[0] is None

    # -- attributes fallback --------------------------------------------------

    def test_attributes_key_used_when_properties_absent(self):
        """
        feature_props() falls back to 'attributes'. Attribute values must be
        extracted correctly regardless of which dict key the server used.
        """
        feature = self._attributes_feature(CEMETERY_NM="Brazos Valley Rest")
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["CEMETERY_NM"].iloc[0] == "Brazos Valley Rest"

    def test_attributes_geometry_coordinates_still_extracted(self):
        """Coordinate extraction must work even when the 'attributes' key is used."""
        feature = self._attributes_feature()
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["_lon"].iloc[0] == pytest.approx(-99.7290)
        assert df["_lat"].iloc[0] == pytest.approx(32.4487)

    # -- empty response -------------------------------------------------------

    def test_empty_feature_stream_returns_empty_dataframe(self):
        """Zero features must yield an empty DataFrame, not crash."""
        with patch("lib.arcgis.iter_features", return_value=iter([])):
            df = mod.fetch()
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0

    # -- OBJECTID numeric coercion --------------------------------------------

    def test_objectid_column_is_numeric(self):
        """OBJECTID must be coerced to numeric — not left as object dtype."""
        feature = self._point_feature(OBJECTID=99)
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert pd.api.types.is_numeric_dtype(df["OBJECTID"])
