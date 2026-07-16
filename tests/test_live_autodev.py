"""Live Auto.dev smoke tests — opt-in only (`pytest -m live`), counted against
budget. Skipped entirely in the default offline profile. Requires
AUTODEV_API_KEY in the environment/.env.
"""

from __future__ import annotations

import pytest

from carfinder.budget import BudgetGuard
from carfinder.config import load_settings
from carfinder.db import bootstrap
from carfinder.profiles import load_profile
from carfinder.query import build_listing_params
from carfinder.sources import AutoDevSource

pytestmark = pytest.mark.live

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


@pytest.fixture
def source():
    settings = load_settings()
    if not settings.autodev_api_key:
        pytest.skip("no AUTODEV_API_KEY")
    conn = bootstrap(":memory:")
    guard = BudgetGuard.from_settings(conn, settings)
    return AutoDevSource(settings.autodev_api_key, guard, page_size=20)


def test_select_projection_returns_real_miles(source):
    """The corrected SELECT_FIELDS must return a real odometer for used cars
    (retailListing.miles), not the null vehicle.mileage spec field."""
    c8 = load_profile(ROOT / "config" / "c8_stingray.yaml")
    params = build_listing_params(c8, current_year=2026)
    page = source.search_page(params, page=1, include_total=True)
    assert page.total and page.total > 0
    assert page.rows, "expected at least one listing"
    # At least one used listing on the page should report mileage.
    assert any(r.miles and r.miles > 0 for r in page.rows), (
        "no row reported miles — SELECT_FIELDS mileage path may be wrong"
    )
