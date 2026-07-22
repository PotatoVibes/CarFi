"""Query-builder golden tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from carfinder.models import ListingQuery, Profile
from carfinder.profiles import load_profile
from carfinder.query import build_listing_params, build_marketcheck_params

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
    # Both Auto.dev model buckets (most Blackwings are filed under "CT4").
    assert params["vehicle.model"] == "CT4,CT4-V"
    # NO trim prefilter: Auto.dev's trim data proved lossy (live Marketcheck
    # comparison, 2026-07-21) — the VIN engine char verifies trim client-side.
    assert "vehicle.trim" not in params
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


# --- marketcheck -------------------------------------------------------------


def test_marketcheck_params_c8_derived():
    c8 = load_profile(CONFIG / "c8_stingray.yaml")
    params = build_marketcheck_params(c8, current_year=2026)
    assert params["make"] == "Chevrolet"
    assert params["model"] == "Corvette"  # no override configured -> q.model
    # Ranges ride the year_range param — the plain `year` param can't parse
    # "lo-hi" (it silently reads it as the low year; verified live).
    assert params["year_range"] == f"{_year_min(c8)}-2027"
    assert "year" not in params
    margin = c8.query.price_margin
    assert params["price_range"] == f"1-{round(_price_hard(c8) * (1 + margin))}"
    assert "trim" not in params  # never sent; scorer gates trim


def test_marketcheck_model_override_wins():
    bw = load_profile(CONFIG / "ct4v_blackwing.yaml")
    params = build_marketcheck_params(bw, current_year=2026)
    # The Auto.dev comma-list ("CT4,CT4-V") would be junk for Marketcheck;
    # the profile carries their taxonomy name instead ("CT4" — they file all
    # CT4 variants incl. Blackwing under the base model; live facet 2026-07-21).
    assert params["model"] == "CT4"
    assert "," not in params["model"]


def test_marketcheck_missing_query_block_raises():
    p = Profile(name="t", model_key="x")
    with pytest.raises(ValueError, match="no query block"):
        build_marketcheck_params(p, current_year=2026)
