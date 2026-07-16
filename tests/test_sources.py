"""AutoDevSource + normalizer tests — mocked transport, zero real network."""

from __future__ import annotations

import httpx
import pytest

from carfinder import db
from carfinder.budget import AUTODEV, BudgetExceeded, BudgetGuard, ProviderBudget
from carfinder.sources import (
    AutoDevSource,
    ListingSource,
    MarketcheckSource,
    normalize,
)


def _source(handler, cap: float = 100, page_size: int = 20):
    conn = db.bootstrap(":memory:")
    guard = BudgetGuard(conn, {AUTODEV: ProviderBudget(AUTODEV, "count", cap)})
    client = httpx.Client(base_url=AutoDevSource.BASE_URL, transport=httpx.MockTransport(handler))
    return AutoDevSource("key", guard, client=client, page_size=page_size), guard


# --- normalizer ------------------------------------------------------------


def test_normalize_nested_shape():
    item = {
        "vehicle": {
            "vin": "1g1yc3d47p5121316",
            "year": 2023,
            "make": "Chevrolet",
            "model": "Corvette",
            "trim": "Stingray",
            "mileage": "12,345",
            "exteriorColor": "Black",
        },
        "retailListing": {"price": "64,995", "vdpUrl": "http://x/y"},
        "createdAt": "2026-07-01T00:00:00Z",
    }
    row = normalize(item, "autodev")
    assert row.vin == "1G1YC3D47P5121316"  # upper-cased
    assert row.year == 2023
    assert row.miles == 12345
    assert row.price == pytest.approx(64995.0)
    assert row.color == "Black"
    assert row.url == "http://x/y"
    assert row.created_at == "2026-07-01T00:00:00Z"


def test_normalize_flat_dotkey_shape():
    item = {
        "vehicle.vin": "ABC",
        "vehicle.year": 2022,
        "retailListing.price": 59000,
        "vehicle.mileage": 20000,
    }
    row = normalize(item, "autodev")
    assert row.vin == "ABC" and row.year == 2022 and row.price == 59000 and row.miles == 20000


def test_normalize_missing_fields_are_none():
    row = normalize({"vehicle": {"vin": "V"}}, "autodev")
    assert row.vin == "V"
    assert row.price is None and row.miles is None and row.color is None


# --- search_page -----------------------------------------------------------


def test_search_page_sets_params_and_parses(capfix=None):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"vehicle": {"vin": "A", "year": 2023}, "retailListing": {"price": 60000}},
                    {"vehicle": {"vin": "B", "year": 2022}, "retailListing": {"price": 61000}},
                ],
                "links": {"next": "http://api/next"},
                "total": 42,
            },
        )

    src, guard = _source(handler)
    page = src.search_page({"vehicle.make": "Chevrolet"}, page=1)
    url = seen["url"]
    assert "vehicle.make=Chevrolet" in url
    assert "limit=20" in url and "page=1" in url
    assert "sort=createdAt.desc" in url
    assert "select=" in url
    assert [r.vin for r in page.rows] == ["A", "B"]
    assert page.total == 42
    assert page.has_next is True
    assert guard.usage(AUTODEV).used == 1


def test_has_next_false_on_short_page_without_links():
    def handler(request):
        return httpx.Response(200, json={"data": [{"vehicle": {"vin": "A"}}]})

    src, _ = _source(handler, page_size=20)
    page = src.search_page({}, page=1)
    assert page.has_next is False  # 1 row < page_size, no links.next


def test_count_uses_includes_total():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [], "total": 137})

    src, _ = _source(handler)
    assert src.count({"vehicle.make": "Cadillac"}) == 137
    assert "includes=total" in seen["url"]


def test_get_listing_single_vin():
    def handler(request):
        assert request.url.path.endswith("/listings/1G1YC3D47P5121316")
        return httpx.Response(
            200, json={"data": {"vehicle": {"vin": "1G1YC3D47P5121316", "year": 2023}}}
        )

    src, _ = _source(handler)
    row = src.get_listing("1G1YC3D47P5121316")
    assert row.vin == "1G1YC3D47P5121316" and row.year == 2023


def test_budget_cap_blocks_search():
    src, _ = _source(lambda r: httpx.Response(200, json={"data": []}), cap=0)
    with pytest.raises(BudgetExceeded):
        src.search_page({})


def test_http_error_raises():
    src, _ = _source(lambda r: httpx.Response(401, json={"error": "unauthorized"}))
    with pytest.raises(httpx.HTTPStatusError):
        src.search_page({})


# --- protocol + marketcheck ------------------------------------------------


def test_autodev_satisfies_protocol():
    src, _ = _source(lambda r: httpx.Response(200, json={"data": []}))
    assert isinstance(src, ListingSource)


def test_marketcheck_is_disabled_stub():
    mc = MarketcheckSource()
    assert mc.enabled is False
    assert isinstance(mc, ListingSource)
    with pytest.raises(NotImplementedError):
        mc.search_page({})
