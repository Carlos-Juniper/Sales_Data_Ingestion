"""
Tests for fgdl_cemeteries.py — FGDL / GeoPlan Florida Cemetery connector.

Strategy
--------
Every public function is covered in isolation. All DataFrames are built
in-memory; no CSV reads and no network calls occur.

Key traps each covered by a dedicated test:

  1. ZIPCODE integer trap — ZIPCODE arrives as int from the API. zip5 must
                            be a zero-padded 5-digit string, not a float
                            string ('32909' not '3290.9' or '32909.0').
  2. Segment inference    — TYPE='RELIGIOUS' → 'religious';
                            TYPE='MUNICIPAL' → 'municipal';
                            TYPE='CEMETERY / UNSPECIFIED' + OPERATING='PRIVATE' → None.
  3. ACRES flow-through   — non-null ACRES populates size_value/size_metric/size_unit;
                            null ACRES leaves those fields None.
  4. Ambiguous segment    — non-religious, non-municipal TYPE → None.
  5. source_id prefix     — 'fgdl:' + str(GCID), e.g. 'fgdl:483'.
  6. State hardcoded      — state='FL' always, regardless of COUNTY value.
  7. Coordinates from fields — latitude/longitude come from LAT_DD/LONG_DD,
                               not geometry.
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from lib.schema import CANONICAL_COLUMNS

import fgdl_cemeteries as mod


# ===========================================================================
# Helpers
# ===========================================================================

def _make_raw(**overrides) -> pd.DataFrame:
    """
    Return a single-row DataFrame that satisfies every field the module touches.
    Override individual column values with keyword arguments.
    """
    defaults = {
        "GCID":       483,
        "NAME":       "Fountainhead Memorial Park",
        "ADDRESS":    "7303 Babcock St SE",
        "CITY":       "Palm Bay",
        "ZIPCODE":    32909,
        "COUNTY":     "BREVARD",
        "TYPE":       "CEMETERY / UNSPECIFIED",
        "OWNER":      "FOUNTAINHEAD MEMORIAL PARK INC",
        "OPERATING":  "PRIVATE",
        "LAT_DD":     27.96,
        "LONG_DD":    -80.62,
        "ACRES":      95.0,
        "FLAG":       "V",
        "OBJECTID":   1,
        "source_file": (
            "https://services.arcgis.com/LBbVDC0hKPAnLRpO/arcgis/rest/services"
            "/gc_cemetery_dec24/FeatureServer/0"
        ),
    }
    defaults.update(overrides)
    return pd.DataFrame([defaults])


def _make_large_raw(n: int = 3001) -> pd.DataFrame:
    """
    Return a DataFrame with ``n`` rows, each with a unique GCID.
    Used to satisfy the row-count guard in assert_source_shape().
    """
    rows = []
    for i in range(n):
        row = _make_raw(GCID=i + 1).iloc[0].to_dict()
        rows.append(row)
    return pd.DataFrame(rows)


# ===========================================================================
# Tests: assert_source_shape()
# ===========================================================================

class TestAssertSourceShape:
    def test_passes_on_well_formed_dataframe(self):
        df = _make_large_raw()
        mod.assert_source_shape(df)

    def test_raises_when_gcid_column_missing(self):
        df = _make_large_raw().drop(columns=["GCID"])
        with pytest.raises(ValueError, match="GCID"):
            mod.assert_source_shape(df)

    def test_raises_when_name_column_missing(self):
        df = _make_large_raw().drop(columns=["NAME"])
        with pytest.raises(ValueError, match="NAME"):
            mod.assert_source_shape(df)

    def test_raises_when_lat_dd_column_missing(self):
        df = _make_large_raw().drop(columns=["LAT_DD"])
        with pytest.raises(ValueError, match="LAT_DD"):
            mod.assert_source_shape(df)

    def test_raises_when_long_dd_column_missing(self):
        df = _make_large_raw().drop(columns=["LONG_DD"])
        with pytest.raises(ValueError, match="LONG_DD"):
            mod.assert_source_shape(df)

    def test_raises_when_type_column_missing(self):
        df = _make_large_raw().drop(columns=["TYPE"])
        with pytest.raises(ValueError, match="TYPE"):
            mod.assert_source_shape(df)

    def test_raises_when_gcid_fill_rate_below_99_percent(self):
        """GCID is the natural key — a low fill rate means the layer changed."""
        df = _make_large_raw(n=3001)
        null_count = int(len(df) * 0.02)
        df.loc[:null_count - 1, "GCID"] = None
        with pytest.raises(ValueError, match="GCID"):
            mod.assert_source_shape(df)

    def test_raises_when_row_count_below_3000(self):
        """Truncation guard — fewer than 3,000 rows means the API was cut off."""
        df = pd.concat([_make_raw()] * 100, ignore_index=True)
        with pytest.raises(ValueError):
            mod.assert_source_shape(df)

    def test_passes_at_exactly_3000_rows(self):
        df = _make_large_raw(n=3000)
        mod.assert_source_shape(df)

    def test_raises_at_2999_rows(self):
        df = _make_large_raw(n=2999)
        with pytest.raises(ValueError):
            mod.assert_source_shape(df)


# ===========================================================================
# Tests: normalize()
# ===========================================================================

class TestNormalize:

    # -- name_normalized -------------------------------------------------------

    def test_name_normalized_column_added(self):
        df = _make_raw()
        out = mod.normalize(df)
        assert "name_normalized" in out.columns

    def test_name_normalized_is_string_for_valid_name(self):
        df = _make_raw(NAME="Fountainhead Memorial Park")
        out = mod.normalize(df)
        assert isinstance(out["name_normalized"].iloc[0], str)

    def test_null_name_does_not_raise(self):
        df = _make_raw(NAME=None)
        out = mod.normalize(df)
        assert out["name_normalized"].iloc[0] == ""

    # -- zip5 (CRITICAL: integer trap) -----------------------------------------

    def test_zip5_from_integer_zipcode_is_zero_padded_string(self):
        """
        CRITICAL: ZIPCODE arrives as int from the ArcGIS API.
        zip5 must be '32909', not 32909 or '3290.9' or '32909.0'.
        """
        df = _make_raw(ZIPCODE=32909)
        out = mod.normalize(df)
        assert out["zip5"].iloc[0] == "32909"
        assert isinstance(out["zip5"].iloc[0], str)

    def test_zip5_leading_zero_preserved(self):
        """ZIP codes starting with 0 must be zero-padded, not truncated."""
        df = _make_raw(ZIPCODE=7001)
        out = mod.normalize(df)
        assert out["zip5"].iloc[0] == "07001"

    def test_zip5_null_zipcode_returns_empty_string(self):
        df = _make_raw(ZIPCODE=None)
        out = mod.normalize(df)
        assert out["zip5"].iloc[0] == ""

    def test_zip5_is_exactly_5_chars(self):
        df = _make_raw(ZIPCODE=32909)
        out = mod.normalize(df)
        assert len(out["zip5"].iloc[0]) == 5

    # -- latitude / longitude --------------------------------------------------

    def test_latitude_comes_from_lat_dd(self):
        """Coordinates must come from LAT_DD/LONG_DD, not geometry."""
        df = _make_raw(LAT_DD=27.96, LONG_DD=-80.62)
        out = mod.normalize(df)
        assert out["latitude"].iloc[0] == pytest.approx(27.96)

    def test_longitude_comes_from_long_dd(self):
        df = _make_raw(LAT_DD=27.96, LONG_DD=-80.62)
        out = mod.normalize(df)
        assert out["longitude"].iloc[0] == pytest.approx(-80.62)

    # -- segment inference (CRITICAL) ------------------------------------------

    def test_segment_religious_when_type_contains_religious(self):
        """TYPE='RELIGIOUS' → segment='religious'."""
        df = _make_raw(TYPE="RELIGIOUS", OPERATING="PRIVATE")
        out = mod.normalize(df)
        assert out["segment"].iloc[0] == "religious"

    def test_segment_religious_case_insensitive(self):
        df = _make_raw(TYPE="Religious Institution", OPERATING="PRIVATE")
        out = mod.normalize(df)
        assert out["segment"].iloc[0] == "religious"

    def test_segment_municipal_when_type_contains_municipal(self):
        """TYPE='MUNICIPAL' → segment='municipal'."""
        df = _make_raw(TYPE="MUNICIPAL", OPERATING="PUBLIC")
        out = mod.normalize(df)
        assert out["segment"].iloc[0] == "municipal"

    def test_segment_municipal_when_operating_is_public(self):
        """OPERATING='PUBLIC' → segment='municipal' regardless of TYPE."""
        df = _make_raw(TYPE="COMMUNITY", OPERATING="PUBLIC")
        out = mod.normalize(df)
        assert out["segment"].iloc[0] == "municipal"

    def test_segment_none_for_private_unspecified_cemetery(self):
        """
        CRITICAL: TYPE='CEMETERY / UNSPECIFIED' + OPERATING='PRIVATE' → None.
        Merge module resolves ambiguous segment downstream.
        """
        df = _make_raw(TYPE="CEMETERY / UNSPECIFIED", OPERATING="PRIVATE")
        out = mod.normalize(df)
        assert out["segment"].iloc[0] is None

    def test_segment_none_for_family_private(self):
        """Non-religious, non-municipal TYPE → None."""
        df = _make_raw(TYPE="FAMILY", OPERATING="PRIVATE")
        out = mod.normalize(df)
        assert out["segment"].iloc[0] is None

    def test_segment_none_for_columbarium_private(self):
        df = _make_raw(TYPE="COLUMBARIUM", OPERATING="PRIVATE")
        out = mod.normalize(df)
        assert out["segment"].iloc[0] is None

    # -- ACRES / size fields ---------------------------------------------------

    def test_size_value_populated_when_acres_not_null(self):
        """ACRES flows through to size_value when present."""
        df = _make_raw(ACRES=95.0)
        out = mod.normalize(df)
        assert out["size_value"].iloc[0] == pytest.approx(95.0)

    def test_size_metric_is_acres_when_acres_not_null(self):
        df = _make_raw(ACRES=95.0)
        out = mod.normalize(df)
        assert out["size_metric"].iloc[0] == "acres"

    def test_size_unit_is_acres_when_acres_not_null(self):
        df = _make_raw(ACRES=95.0)
        out = mod.normalize(df)
        assert out["size_unit"].iloc[0] == "acres"

    def test_size_value_none_when_acres_null(self):
        df = _make_raw(ACRES=None)
        out = mod.normalize(df)
        assert pd.isna(out["size_value"].iloc[0])

    def test_size_metric_none_when_acres_null(self):
        df = _make_raw(ACRES=None)
        out = mod.normalize(df)
        assert out["size_metric"].iloc[0] is None

    def test_size_unit_none_when_acres_null(self):
        df = _make_raw(ACRES=None)
        out = mod.normalize(df)
        assert out["size_unit"].iloc[0] is None

    # -- always-None columns ---------------------------------------------------

    def test_county_fips_is_none(self):
        """COUNTY is a name string — FIPS must always be None."""
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
            _make_raw(GCID=1, TYPE="RELIGIOUS"),
            _make_raw(GCID=2, TYPE="MUNICIPAL"),
        ], ignore_index=True)
        out = mod.normalize(df)
        assert len(out) == 2


# ===========================================================================
# Tests: to_canonical()
# ===========================================================================

class TestToCanonical:

    def _normalized_df(self, **overrides) -> pd.DataFrame:
        return mod.normalize(_make_raw(**overrides))

    def test_all_expected_columns_present(self):
        out = mod.to_canonical(self._normalized_df())
        missing = set(CANONICAL_COLUMNS) - set(out.columns)
        assert not missing, f"Missing columns: {missing}"

    def test_source_id_prefixed_with_fgdl(self):
        """source_id must be 'fgdl:' + str(GCID)."""
        out = mod.to_canonical(self._normalized_df(GCID=483))
        assert out["source_id"].iloc[0] == "fgdl:483"

    def test_source_id_integer_gcid_rendered_as_string(self):
        """GCID=483 must produce 'fgdl:483', not 'fgdl:483.0'."""
        out = mod.to_canonical(self._normalized_df(GCID=483))
        assert "." not in out["source_id"].iloc[0]

    def test_natural_key_matches_gcid_string(self):
        out = mod.to_canonical(self._normalized_df(GCID=483))
        assert out["natural_key"].iloc[0] == "483"

    def test_vertical_is_deathcare(self):
        out = mod.to_canonical(self._normalized_df())
        assert (out["vertical"] == "deathcare").all()

    def test_account_type_is_cemetery(self):
        out = mod.to_canonical(self._normalized_df())
        assert (out["account_type"] == "cemetery").all()

    def test_state_is_always_fl(self):
        """State must be hardcoded 'FL' — all FGDL records are Florida."""
        out = mod.to_canonical(self._normalized_df())
        assert (out["state"] == "FL").all()

    def test_phone_raw_is_none(self):
        out = mod.to_canonical(self._normalized_df())
        assert out["phone_raw"].iloc[0] is None

    def test_phone_normalized_is_none(self):
        out = mod.to_canonical(self._normalized_df())
        assert out["phone_normalized"].iloc[0] is None

    def test_ein_is_none(self):
        out = mod.to_canonical(self._normalized_df())
        assert out["ein"].iloc[0] is None

    def test_county_fips_is_none(self):
        out = mod.to_canonical(self._normalized_df())
        assert out["county_fips"].iloc[0] is None

    def test_segment_religious_flows_through(self):
        out = mod.to_canonical(self._normalized_df(TYPE="RELIGIOUS", OPERATING="PRIVATE"))
        assert out["segment"].iloc[0] == "religious"

    def test_segment_municipal_flows_through(self):
        out = mod.to_canonical(self._normalized_df(TYPE="MUNICIPAL", OPERATING="PUBLIC"))
        assert out["segment"].iloc[0] == "municipal"

    def test_segment_none_for_private_unspecified(self):
        """
        CRITICAL: TYPE='CEMETERY / UNSPECIFIED' + OPERATING='PRIVATE' → None.
        """
        out = mod.to_canonical(
            self._normalized_df(TYPE="CEMETERY / UNSPECIFIED", OPERATING="PRIVATE")
        )
        assert out["segment"].iloc[0] is None

    def test_size_value_acres_flows_through(self):
        out = mod.to_canonical(self._normalized_df(ACRES=95.0))
        assert out["size_value"].iloc[0] == pytest.approx(95.0)

    def test_size_metric_acres_when_present(self):
        out = mod.to_canonical(self._normalized_df(ACRES=95.0))
        assert out["size_metric"].iloc[0] == "acres"

    def test_size_unit_acres_when_present(self):
        out = mod.to_canonical(self._normalized_df(ACRES=95.0))
        assert out["size_unit"].iloc[0] == "acres"

    def test_size_fields_none_when_acres_null(self):
        out = mod.to_canonical(self._normalized_df(ACRES=None))
        assert out["size_metric"].iloc[0] is None
        assert out["size_unit"].iloc[0] is None

    def test_latitude_comes_from_lat_dd(self):
        out = mod.to_canonical(self._normalized_df(LAT_DD=27.96))
        assert out["latitude"].iloc[0] == pytest.approx(27.96)

    def test_longitude_comes_from_long_dd(self):
        out = mod.to_canonical(self._normalized_df(LONG_DD=-80.62))
        assert out["longitude"].iloc[0] == pytest.approx(-80.62)

    def test_zip5_zero_padded_from_integer(self):
        """ZIPCODE=32909 (int) must produce zip5='32909' in canonical output."""
        out = mod.to_canonical(self._normalized_df(ZIPCODE=32909))
        assert out["zip5"].iloc[0] == "32909"

    def test_row_count_preserved(self):
        df = pd.concat([
            _make_raw(GCID=i) for i in range(1, 6)
        ], ignore_index=True)
        out = mod.to_canonical(mod.normalize(df))
        assert len(out) == 5

    def test_source_file_preserved(self):
        out = mod.to_canonical(self._normalized_df())
        assert "FeatureServer" in out["source_file"].iloc[0]


# ===========================================================================
# Tests: report_quality()
# ===========================================================================

class TestReportQuality:
    def test_does_not_raise_on_well_formed_input(self):
        df = mod.normalize(_make_raw())
        mod.report_quality(df)

    def test_does_not_raise_with_null_acres(self):
        """Null ACRES is the common case — report_quality must handle it."""
        df = mod.normalize(_make_raw(ACRES=None))
        mod.report_quality(df)

    def test_does_not_raise_with_null_address(self):
        df = mod.normalize(_make_raw(ADDRESS=None))
        mod.report_quality(df)

    def test_produces_stderr_output(self, capsys):
        # Arrange: 4 rows, 3 with ACRES present and 1 with ACRES null.
        # report_quality writes "non-null ACRES  {acres_pct:.1%}", so
        # 3/4 non-null → "75.0%".
        frames = [
            _make_raw(GCID=1, ACRES=10.0),
            _make_raw(GCID=2, ACRES=20.0),
            _make_raw(GCID=3, ACRES=30.0),
            _make_raw(GCID=4, ACRES=None),
        ]
        df = mod.normalize(pd.concat(frames, ignore_index=True))

        # Act
        mod.report_quality(df)

        # Assert: stderr carries the expected non-null ACRES percentage
        captured = capsys.readouterr()
        assert "75.0%" in captured.err, (
            f"Expected '75.0%' in stderr (3/4 non-null ACRES), got: {captured.err!r}"
        )

    def test_does_not_raise_on_multiple_types(self):
        frames = [
            _make_raw(GCID=1, TYPE="RELIGIOUS", OPERATING="PRIVATE"),
            _make_raw(GCID=2, TYPE="MUNICIPAL", OPERATING="PUBLIC"),
            _make_raw(GCID=3, TYPE="FAMILY", OPERATING="PRIVATE"),
            _make_raw(GCID=4, TYPE="CEMETERY / UNSPECIFIED", OPERATING="PRIVATE"),
        ]
        df = mod.normalize(pd.concat(frames, ignore_index=True))
        mod.report_quality(df)

    def test_does_not_raise_when_flag_column_mixed(self):
        frames = [
            _make_raw(GCID=1, FLAG="V"),
            _make_raw(GCID=2, FLAG="U"),
        ]
        df = mod.normalize(pd.concat(frames, ignore_index=True))
        mod.report_quality(df)


# ===========================================================================
# Tests: fetch()
# ===========================================================================

class TestFetch:
    """
    Tests for fgdl_cemeteries.fetch().

    Strategy: patch lib.arcgis.iter_features to yield controlled feature dicts.
    FGDL is distinct from other connectors in two ways:
      - Coordinates come from LAT_DD/LONG_DD attributes, not from geometry.
        fetch() sets return_geometry=False and never calls feature_lonlat().
      - Properties arrive under 'properties' (GeoJSON) or 'attributes' (ESRI JSON).

    Patch target is 'lib.arcgis.iter_features' — fgdl_cemeteries imports via
    `from lib import arcgis` and calls `arcgis.iter_features(...)`.
    """

    def _props_feature(self, **prop_overrides) -> dict:
        """
        Build a minimal FGDL feature dict with 'properties' and no geometry.
        LAT_DD/LONG_DD are the authoritative coordinate fields for this layer.
        """
        props = {
            "GCID": 483,
            "NAME": "Fountainhead Memorial Park",
            "ADDRESS": "7303 Babcock St SE",
            "CITY": "Palm Bay",
            "ZIPCODE": 32909,
            "COUNTY": "BREVARD",
            "TYPE": "CEMETERY / UNSPECIFIED",
            "OWNER": "FOUNTAINHEAD MEMORIAL PARK INC",
            "OPERATING": "PRIVATE",
            "LAT_DD": 27.9621,
            "LONG_DD": -80.6213,
            "ACRES": 95.0,
            "FLAG": "V",
            "OBJECTID": 1,
        }
        props.update(prop_overrides)
        return {
            "type": "Feature",
            "properties": props,
            "geometry": None,
        }

    def _attributes_feature(self, **attr_overrides) -> dict:
        """
        Feature using 'attributes' key — tests feature_props() fallback path.
        """
        attrs = {
            "GCID": 999,
            "NAME": "Sunset Rest Cemetery",
            "ADDRESS": "500 Sunset Blvd",
            "CITY": "Orlando",
            "ZIPCODE": 32801,
            "COUNTY": "ORANGE",
            "TYPE": "RELIGIOUS",
            "OWNER": "FIRST BAPTIST CHURCH",
            "OPERATING": "PRIVATE",
            "LAT_DD": 28.5383,
            "LONG_DD": -81.3792,
            "ACRES": None,
            "FLAG": "V",
            "OBJECTID": 2,
        }
        attrs.update(attr_overrides)
        return {
            "type": "Feature",
            "attributes": attrs,
            "geometry": None,
        }

    # -- happy path -----------------------------------------------------------

    def test_happy_path_returns_dataframe(self):
        feature = self._props_feature()
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert isinstance(df, pd.DataFrame)

    def test_happy_path_yields_one_row_per_feature(self):
        features = [self._props_feature(GCID=i, OBJECTID=i) for i in range(1, 5)]
        with patch("lib.arcgis.iter_features", return_value=iter(features)):
            df = mod.fetch()
        assert len(df) == 4

    def test_lat_dd_maps_to_lat_dd_column(self):
        """
        CRITICAL: coordinates come from LAT_DD/LONG_DD attribute fields, not geometry.
        fetch() must read them directly from props — never call feature_lonlat().
        """
        feature = self._props_feature(LAT_DD=27.9621)
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["LAT_DD"].iloc[0] == pytest.approx(27.9621)

    def test_long_dd_maps_to_long_dd_column(self):
        feature = self._props_feature(LONG_DD=-80.6213)
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["LONG_DD"].iloc[0] == pytest.approx(-80.6213)

    def test_gcid_column_populated(self):
        feature = self._props_feature(GCID=483)
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["GCID"].iloc[0] == 483

    def test_name_column_populated(self):
        feature = self._props_feature(NAME="Magnolia Gardens")
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["NAME"].iloc[0] == "Magnolia Gardens"

    def test_source_file_column_added(self):
        """fetch() must inject source_file with the FGDL SOURCE_URL constant."""
        feature = self._props_feature()
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert "source_file" in df.columns
        assert "gc_cemetery" in df["source_file"].iloc[0]

    # -- missing LAT_DD / LONG_DD ---------------------------------------------

    def test_missing_lat_dd_yields_none(self):
        """
        Feature missing LAT_DD in props → that column value must be None.
        fetch() uses props.get(col) which returns None for absent keys.
        """
        feature = self._props_feature()
        del feature["properties"]["LAT_DD"]
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["LAT_DD"].iloc[0] is None

    def test_missing_long_dd_yields_none(self):
        feature = self._props_feature()
        del feature["properties"]["LONG_DD"]
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["LONG_DD"].iloc[0] is None

    def test_null_lat_dd_yields_none(self):
        """Explicit None in LAT_DD prop must pass through, not become NaN string."""
        feature = self._props_feature(LAT_DD=None)
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["LAT_DD"].iloc[0] is None

    # -- attributes fallback --------------------------------------------------

    def test_attributes_key_used_when_properties_absent(self):
        """
        feature_props() falls back to 'attributes'. fetch() must extract
        the same coordinate and metadata fields regardless of which dict key
        the server used.
        """
        feature = self._attributes_feature(NAME="Oak Hill Memorial")
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["NAME"].iloc[0] == "Oak Hill Memorial"

    def test_attributes_lat_dd_extracted_correctly(self):
        feature = self._attributes_feature(LAT_DD=28.5383)
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert df["LAT_DD"].iloc[0] == pytest.approx(28.5383)

    # -- empty response -------------------------------------------------------

    def test_empty_feature_stream_returns_empty_dataframe(self):
        """Zero features from the server must yield an empty DataFrame, not crash."""
        with patch("lib.arcgis.iter_features", return_value=iter([])):
            df = mod.fetch()
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0

    # -- return_geometry=False verified ---------------------------------------

    def test_iter_features_called_with_return_geometry_false(self):
        """
        FGDL geometry is in WKID 3087 (Florida Albers) — not WGS84.
        fetch() must set return_geometry=False and rely on LAT_DD/LONG_DD.
        """
        with patch("lib.arcgis.iter_features", return_value=iter([])) as mock_iter:
            mod.fetch()
        call_kwargs = mock_iter.call_args.kwargs
        assert call_kwargs.get("return_geometry") is False

    # -- OBJECTID numeric coercion --------------------------------------------

    def test_objectid_column_is_numeric(self):
        """OBJECTID must be coerced to numeric after fetch — not left as object."""
        feature = self._props_feature(OBJECTID=7)
        with patch("lib.arcgis.iter_features", return_value=iter([feature])):
            df = mod.fetch()
        assert pd.api.types.is_numeric_dtype(df["OBJECTID"])
