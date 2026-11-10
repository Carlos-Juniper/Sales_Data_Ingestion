"""Tests for hoa.fl_common_area_parcels — synthetic parcels, no network."""

from __future__ import annotations

import pytest

from hoa import fl_common_area_parcels as m


def square(x, y, d=0.001):
    return {"type": "Polygon", "coordinates": [[[x, y], [x + d, y], [x + d, y + d], [x, y + d], [x, y]]]}


def feature(pid="P1", owner="TEST LAKES HOMEOWNERS ASSOCIAT", addr1="C/O TEST PROPERTY MANAGEMENT",
            geom=None, oid=1, sqft=10000):
    return {"type": "Feature", "geometry": geom if geom is not None else square(-82.4, 28.1),
            "properties": {"OBJECTID": oid, "CO_NO": 39.0, "PARCEL_ID": pid, "DOR_UC": "009",
                           "OWN_NAME": owner, "OWN_ADDR1": addr1, "OWN_ADDR2": "4917 TEST RD STE 1",
                           "OWN_CITY": "TAMPA", "OWN_STATE": "FL", "OWN_ZIPCD": 33624,
                           "PHY_ADDR1": " ", "PHY_CITY": "TAMPA", "PHY_ZIPCD": 33647,
                           "LND_SQFOOT": sqft}}


# ---------------------------------------------------------------- owner class

@pytest.mark.parametrize("name,cls", [
    ("ISLAND TEST PROPERTY OWNERS AS", "association"),   # truncated at 30
    ("MIRA TEST WEST HOMEOWNERS ASSO", "association"),
    ("TEST BAY CONDOMINIUM ASSOCIATION", "association"),
    ("TEST LAKE WATER TOWER HOA INC", "association"),      # association before utility
    ("TEST PALMS H O A INC", "association"),
    ("SOUTH TEST CDD", "cdd"),
    ("TEST COMMUNITY DEVELOPMENT DISTRICT", "cdd"),
    ("TEST HOMES LLC", "company"),
    ("TEST LAND TRUST", "company"),
    ("HILLSBOROUGH COUNTY", "government"),
    ("TAMPA ELECTRIC CO", "utility"),
    ("TEST BUILDERS INC", "company"),
    ("TEST CREEK", "other"),
    ("", "other"),
    (None, "other"),
])
def test_owner_class(name, cls):
    assert m.owner_class(name) == cls


def test_truncation_rule_needs_a_cut_name():
    # A short name ending in "AS" is not a truncated association.
    assert not m._truncated_association("TEST AS")
    assert m._truncated_association("TEST GROVE TOWNHOMES PROPERTY O")


# ---------------------------------------------------------------- geometry

def test_centroid_of_square():
    lon, lat = m.centroid(square(-82.4, 28.1, 0.002))
    assert lon == pytest.approx(-82.399) and lat == pytest.approx(28.101)


def test_centroid_weights_multipolygon_by_area():
    mp = m.merge_geometries([square(0, 0, 1), square(10, 0, 3)])
    assert mp["type"] == "MultiPolygon"
    lon, _ = m.centroid(mp)
    assert lon == pytest.approx((0.5 * 1 + 11.5 * 9) / 10)


def test_centroid_handles_missing_geometry():
    assert m.centroid(None) == (None, None)
    assert m.merge_geometries([None]) is None


# ---------------------------------------------------------------- dedupe / frame

def test_dedupe_collapses_repeated_parcel_and_merges_pieces():
    feats = [feature(oid=1, geom=square(0, 0, 1)), feature(oid=2, geom=square(5, 5, 2)),
             feature(oid=3, geom=square(0, 0, 1)),  # exact repeat
             feature(pid="P2", oid=4)]
    parcels = m.dedupe(feats)
    assert set(parcels) == {"39:P1", "39:P2"}
    assert parcels["39:P1"]["geometry"]["type"] == "MultiPolygon"
    assert len(parcels["39:P1"]["geometry"]["coordinates"]) == 2
    assert parcels["39:P1"]["props"]["OBJECTID"] == 2  # largest piece


def test_to_frame_canonical_and_attrs():
    parcels = m.dedupe([feature(), feature(pid="P2", owner="TEST HOMES LLC", addr1="1 BUILDER WAY")])
    df, attrs = m.to_frame(parcels)
    m.assert_source_shape(df)
    canon = m.to_canonical(df).set_index("natural_key")
    assert canon.loc["39:P1", "segment"] == "association"
    assert canon.loc["39:P2", "segment"] == "company"
    assert canon.loc["39:P1", "zip5"] == "33647"
    assert canon.loc["39:P1", "address_line_1"] is None  # blank PHY_ADDR1
    assert canon.loc["39:P1", "size_value"] == 10000
    assert canon.loc["39:P1", "latitude"] == pytest.approx(28.1005)
    a = attrs["39:P1"]
    assert "TEST PROPERTY MANAGEMENT" in a["care_of"]
    assert a["owner_name_truncated"] is True
    assert a["owner"]["zip"] == "33624"
    assert a["boundary"]["type"] == "Polygon"
    assert "care_of" not in attrs["39:P2"]


def test_shape_guard():
    import pandas as pd
    with pytest.raises(ValueError):
        m.assert_source_shape(pd.DataFrame(columns=["natural_key", "name_raw"]))


# ---------------------------------------------------------------- fetch

def test_fetch_uses_ids_then_chunks(monkeypatch):
    monkeypatch.setattr(m, "ID_CHUNK", 2)
    calls = []

    def post(params):
        calls.append(params)
        if params.get("returnIdsOnly"):
            return {"objectIds": [3, 1, 2]}
        ids = [int(i) for i in params["objectIds"].split(",")]
        return {"features": [feature(pid=f"P{i}", oid=i) for i in ids]}

    ids = m.county_ids(39, post)
    assert ids == [1, 2, 3]
    assert calls[0]["where"].startswith("CO_NO=39 AND")
    feats = list(m.fetch_features(ids, post))
    assert [f["properties"]["OBJECTID"] for f in feats] == [1, 2, 3]
    assert [c["objectIds"] for c in calls[1:]] == ["1,2", "3"]


def test_load_ids_accepts_saved_query(tmp_path):
    p = tmp_path / "ids.json"
    p.write_text('{"objectIdFieldName": "OBJECTID", "objectIds": [5, 2]}')
    assert m.load_ids(str(p)) == [2, 5]
    p.write_text("[3, 1]")
    assert m.load_ids(str(p)) == [1, 3]


def test_poster_retries_server_timeout(monkeypatch):
    class Resp:
        def __init__(self, body):
            self.ok, self._body = True, body

        def json(self):
            return self._body

    answers = [{"error": {"code": 400}}, {"objectIds": [1]}]

    class Session:
        def post(self, *a, **k):
            return Resp(answers.pop(0))

    monkeypatch.setattr(m, "make_session", Session)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    assert m._arcgis_poster(attempts=2)({"where": "x"}) == {"objectIds": [1]}

    answers[:] = [{"error": {"code": 400}}, {"error": {"code": 400}}]
    with pytest.raises(RuntimeError):
        m._arcgis_poster(attempts=2)({"where": "x"})
