"""Query-builder golden tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from carfinder.models import ListingQuery, Profile
from carfinder.profiles import load_profile
from carfinder.query import build_listing_params

CONFIG = Path(__file__).resolve().parents[1] / "config"


def test_c8_prefilter_params_golden():
    c8 = load_profile(CONFIG / "c8_stingray.yaml")
    params = build_listing_params(c8, current_year=2026)
    assert params == {
        "vehicle.make": "Chevrolet",
        "vehicle.model": "Corvette",
        "vehicle.year": "2022-2027",  # year_min 2022 .. current+1
        "retailListing.price": "1-75600",  # 70000 hard * 1.08, widened
    }


def test_blackwing_prefilter_params_golden():
    bw = load_profile(CONFIG / "ct4v_blackwing.yaml")
    params = build_listing_params(bw, current_year=2026)
    assert params["vehicle.make"] == "Cadillac"
    assert params["vehicle.model"] == "CT4-V"
    assert params["vehicle.year"] == "2022-2027"
    assert params["retailListing.price"] == "1-70200"  # 65000 * 1.08


def test_explicit_overrides_win():
    p = Profile(
        name="t",
        model_key="c8_stingray",
        query=ListingQuery(
            make="Chevrolet", model="Corvette", year_min=2022, year_max=2025, price_max=50000
        ),
    )
    params = build_listing_params(p, current_year=2026)
    assert params["vehicle.year"] == "2022-2025"
    assert params["retailListing.price"] == "1-50000"  # explicit max, no widening


def test_missing_query_block_raises():
    p = Profile(name="t", model_key="x")
    with pytest.raises(ValueError, match="no query block"):
        build_listing_params(p, current_year=2026)
