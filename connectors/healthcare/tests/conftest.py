"""
Shared pytest fixtures for the parcel connector test suite.

Fixtures here are consumed by test_normalize.py, test_arcgis.py, and
test_parcel_enrich.py.  Everything in this file is pure in-memory data;
no network or filesystem side-effects occur during setup.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


@pytest.fixture
def minimal_yaml() -> dict:
    """
    A small in-memory YAML equivalent covering exactly one statewide entry
    (FL) and one county entry (TX/48201).

    Use this fixture with _build_layer_maps() to exercise the config parser
    without touching the real config/parcel_layers.yaml on disk.
    """
    return {
        "statewide": {
            "FL": {
                "url": "https://fake.arcgis.com/FL/FeatureServer/0",
                "area_field": "LND_SQFOOT",
                "area_unit": "sqft",
                "parcel_id_field": "PARCELNO",
                "owner_field": "OWN_NAME",
            },
        },
        "county": {
            "TX": {
                "48201": {
                    "url": "https://fake.arcgis.com/TX/48201/FeatureServer/0",
                    "area_field": "LND_SQFOOT",
                    "area_unit": "sqft",
                    "parcel_id_field": "ACCT",
                    "owner_field": "OWNER_NM",
                },
            },
        },
    }


@pytest.fixture
def fl_feature() -> dict:
    """
    A realistic GeoJSON feature dict for a single FL parcel.

    LND_SQFOOT = 87_120 sq ft == exactly 2.0 acres (87_120 / 43_560).
    Includes a minimal polygon geometry so boundary_geojson can be populated.
    """
    return {
        "type": "Feature",
        "properties": {
            "PARCELNO": "08-1234-567-0001",
            "LND_SQFOOT": 87120,
            "OWN_NAME": "GENERAL HOSPITAL LLC",
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [-81.379236, 28.538333],
                    [-81.378236, 28.538333],
                    [-81.378236, 28.537333],
                    [-81.379236, 28.537333],
                    [-81.379236, 28.538333],
                ]
            ],
        },
    }
