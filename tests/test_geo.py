"""Zip-centroid geo helpers — fully offline (bundled zipcodes dataset)."""

from __future__ import annotations

from carfinder.geo import zip_distance_miles, zip_latlon


def test_zip_latlon_known():
    lat, lon = zip_latlon("77498")  # Sugar Land, TX
    assert 29.0 < lat < 30.0
    assert -96.5 < lon < -95.0


def test_zip_latlon_handles_zip_plus_four():
    assert zip_latlon("77498-1234") == zip_latlon("77498")


def test_zip_latlon_unknown_or_malformed():
    assert zip_latlon("00000") is None
    assert zip_latlon("ABCDE") is None
    assert zip_latlon("") is None
    assert zip_latlon(None) is None


def test_distance_sugar_land_to_downtown_houston():
    d = zip_distance_miles("77498", "77002")
    assert d is not None
    assert 10 <= d <= 30  # ~18 mi as the crow flies


def test_distance_cross_country_sanity():
    d = zip_distance_miles("77498", "90210")  # Houston -> Beverly Hills
    assert d is not None
    assert 1200 <= d <= 1700


def test_distance_none_when_either_unknown():
    assert zip_distance_miles("77498", None) is None
    assert zip_distance_miles(None, "77498") is None
    assert zip_distance_miles("77498", "XYZ") is None
