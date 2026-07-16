"""Query-builder golden tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from carfinder.models import ListingQuery, Profile
from carfinder.profiles import load_profile
from carfinder.query import build_listing_params

CONFIG = Path(__file__).resolve().parents[1] / "config"


def _year_min(profile):
    return next(c.min_year for c in profile.hard if c.kind == "year_min")


def _price_hard(profile):
    return next(c.hard for c in profile.soft if c.kind == "numeric" and c.attr == "price")


def test_c8_prefilter_params_derived_from_profile():
    # Derives from the seed so tuning preferences (year floor, price) doesn't
    # break the test — it verifies the builder LOGIC, not specific bounds.
    c8 = load_profile(CONFIG / "c8_stingray.yaml")
    params = build_listing_params(c8, current_year=2026)
    assert params["vehicle.make"] == "Chevrolet"
    assert params["vehicle.model"] == "Corvette"
    assert params["vehicle.year"] == f"{_year_min(c8)}-2027"  # year_min .. current+1
    assert params["retailListing.price"] == f"1-{round(_price_hard(c8) * 1.08)}"  # widened


def test_blackwing_prefilter_params_golden():
    bw = load_profile(CONFIG / "ct4v_blackwing.yaml")
    params = build_listing_params(bw, current_year=2026)
    assert params["vehicle.make"] == "Cadillac"
    # Both Auto.dev model buckets (most Blackwings are filed under "CT4"),
    # narrowed by their normalized trim value.
    assert params["vehicle.model"] == "CT4,CT4-V"
    assert params["vehicle.trim"] == "V Blackwing"
    assert params["vehicle.year"] == f"{_year_min(bw)}-2027"
    margin = bw.query.price_margin
    assert params["retailListing.price"] == f"1-{round(_price_hard(bw) * (1 + margin))}"


def test_trim_omitted_when_not_configured():
    c8 = load_profile(CONFIG / "c8_stingray.yaml")
    params = build_listing_params(c8, current_year=2026)
    assert "vehicle.trim" not in params


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
