"""AutoDevSource + normalizer tests — mocked transport, zero real network."""

from __future__ import annotations

import httpx
import pytest

from carfinder import db
from carfinder.budget import AUTODEV, MARKETCHECK, BudgetExceeded, BudgetGuard, ProviderBudget
from carfinder.sources import (
    AutoDevSource,
    ListingSource,
    MarketcheckSource,
    normalize,
    normalize_marketcheck,
)


def _source(handler, cap: float = 100, page_size: int = 20, max_retries: int = 4):
    conn = db.bootstrap(":memory:")
    guard = BudgetGuard(conn, {AUTODEV: ProviderBudget(AUTODEV, "count", cap)})
    client = httpx.Client(base_url=AutoDevSource.BASE_URL, transport=httpx.MockTransport(handler))
    sleeps: list[float] = []
    src = AutoDevSource(
        "key",
        guard,
        client=client,
        page_size=page_size,
        min_interval_s=0.0,
        max_retries=max_retries,
        sleep=sleeps.append,
    )
    src._sleeps = sleeps  # expose for assertions
    return src, guard


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


def test_normalize_captures_seller_and_location():
    # Flat (search projection) shape.
    flat = normalize(
        {
            "vehicle.vin": "V",
            "retailListing.dealer": "Van Hyundai",
            "retailListing.city": "Carrollton",
            "retailListing.state": "TX",
            "retailListing.zip": 75006,  # numbers coerce to strings
        },
        "autodev",
    )
    assert flat.dealer == "Van Hyundai"
    assert flat.city == "Carrollton" and flat.state == "TX" and flat.zip == "75006"
    # Nested (single-listing endpoint) shape.
    nested = normalize(
        {
            "vehicle": {"vin": "V"},
            "retailListing": {"dealer": "Carvana", "city": "Miami", "state": "FL", "zip": "33132"},
        },
        "autodev",
    )
    assert nested.dealer == "Carvana" and nested.zip == "33132"
    # Absent -> None, not "".
    empty = normalize({"vehicle.vin": "V", "retailListing.dealer": ""}, "autodev")
    assert empty.dealer is None and empty.city is None and empty.zip is None


def test_normalize_rejects_junk_fragment_urls():
    # Auto.dev sends fragments like '#-4976296983083547062' as vdp for some
    # rows — rendering those as links just re-anchors the current page.
    junk = normalize({"vehicle.vin": "V", "retailListing.vdp": "#-4976296983083547062"}, "autodev")
    assert junk.url is None
    rel = normalize({"vehicle.vin": "V", "retailListing.vdp": "/cars/123"}, "autodev")
    assert rel.url is None
    ok = normalize({"vehicle.vin": "V", "retailListing.vdp": "https://dealer.example/x"}, "autodev")
    assert ok.url == "https://dealer.example/x"


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


# --- rate limiting + retry -------------------------------------------------


def test_429_is_retried_respecting_retry_after():
    responses = iter(
        [
            httpx.Response(429, headers={"retry-after": "2"}, json={}),
            httpx.Response(200, json={"data": [], "total": 0}),
        ]
    )
    src, guard = _source(lambda r: next(responses))
    page = src.search_page({})  # succeeds on the retry, no exception
    assert page.total == 0
    assert src._sleeps == [2.0]  # honored Retry-After
    assert guard.usage(AUTODEV).used == 2  # both attempts counted against quota


def test_429_exhausts_retries_then_raises():
    src, _ = _source(lambda r: httpx.Response(429, json={}), max_retries=3)
    with pytest.raises(httpx.HTTPStatusError):
        src.search_page({})
    assert len(src._sleeps) == 3  # backed off on every attempt


def test_5xx_retried_with_exponential_backoff():
    responses = iter([httpx.Response(503, json={}), httpx.Response(200, json={"data": []})])
    src, _ = _source(lambda r: next(responses))
    src.search_page({})
    assert src._sleeps == [1.0]  # no Retry-After -> backoff base


def test_non_retryable_4xx_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404, json={})

    src, _ = _source(handler)
    with pytest.raises(httpx.HTTPStatusError):
        src.search_page({})
    assert len(calls) == 1  # 404 fails fast, no retry
    assert src._sleeps == []


def test_throttle_spaces_requests(monkeypatch):
    # With a real min interval and a fake clock, the second call sleeps.
    conn = db.bootstrap(":memory:")
    guard = BudgetGuard(conn, {AUTODEV: ProviderBudget(AUTODEV, "count", 100)})
    client = httpx.Client(
        base_url=AutoDevSource.BASE_URL,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []})),
    )
    slept: list[float] = []
    clock = {"t": 1000.0}
    monkeypatch.setattr("carfinder.sources.time.monotonic", lambda: clock["t"])
    src = AutoDevSource("k", guard, client=client, min_interval_s=0.25, sleep=slept.append)
    src.search_page({})  # first call: no wait
    src.search_page({})  # immediate second call (clock unchanged): must wait
    assert slept == [0.25]


# --- protocol --------------------------------------------------------------


def test_autodev_satisfies_protocol():
    src, _ = _source(lambda r: httpx.Response(200, json={"data": []}))
    assert isinstance(src, ListingSource)


# --- marketcheck -----------------------------------------------------------


def _mc_source(handler, cap: float = 100, page_size: int = 50):
    conn = db.bootstrap(":memory:")
    guard = BudgetGuard(conn, {MARKETCHECK: ProviderBudget(MARKETCHECK, "count", cap)})
    client = httpx.Client(
        base_url=MarketcheckSource.BASE_URL, transport=httpx.MockTransport(handler)
    )
    src = MarketcheckSource("key", guard, client=client, page_size=page_size, min_interval_s=0.0)
    return src, guard


def test_marketcheck_satisfies_protocol():
    src, _ = _mc_source(lambda r: httpx.Response(200, json={"listings": [], "num_found": 0}))
    assert isinstance(src, ListingSource)
    assert src.enabled is True


def test_normalize_marketcheck_nested_shape():
    item = {
        "vin": "1g1yc3d47p5121316",
        "price": 64995,
        "miles": 12345,
        "exterior_color": "Rapid Blue",
        "vdp_url": "https://dealer.example/vdp/1",
        "first_seen_at_date": "2026-07-01",
        "build": {"year": 2023, "make": "Chevrolet", "model": "Corvette", "trim": "Stingray"},
        "dealer": {"name": "Classic Chevy", "city": "Sugar Land", "state": "TX", "zip": "77478"},
    }
    row = normalize_marketcheck(item)
    assert row.vin == "1G1YC3D47P5121316"  # upper-cased
    assert row.source == "marketcheck"
    assert row.year == 2023 and row.make == "Chevrolet" and row.trim == "Stingray"
    assert row.price == pytest.approx(64995.0) and row.miles == 12345
    assert row.color == "Rapid Blue"
    assert row.url == "https://dealer.example/vdp/1"
    assert row.created_at == "2026-07-01"
    assert row.dealer == "Classic Chevy" and row.state == "TX" and row.zip == "77478"


def test_marketcheck_search_page_params_and_parse():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "num_found": 137,
                "listings": [
                    {"vin": "A", "build": {"year": 2023}, "dealer": {"state": "TX"}},
                    {"vin": "B", "build": {"year": 2022}, "dealer": {"state": "CA"}},
                ],
            },
        )

    src, guard = _mc_source(handler)
    page = src.search_page({"make": "Chevrolet", "model": "Corvette"}, page=1)
    url = seen["url"]
    assert "/search/car/active" in url
    assert "make=Chevrolet" in url and "model=Corvette" in url
    assert "rows=50" in url and "start=0" in url
    assert "api_key=key" in url  # key injected on every request
    # Protocol sort translated to the stable newest-first order.
    assert "sort_by=dos_active" in url and "sort_order=asc" in url
    assert [r.vin for r in page.rows] == ["A", "B"]
    assert page.total == 137
    assert page.has_next is True
    assert guard.usage(MARKETCHECK).used == 1  # gated + recorded


def test_marketcheck_pagination_start_offset_and_has_next():
    def handler(request):
        # A full page at the end of the window: start(100) + 50 rows == num_found.
        return httpx.Response(
            200,
            json={"num_found": 150, "listings": [{"vin": str(i)} for i in range(50)]},
        )

    src, _ = _mc_source(handler, page_size=50)
    page = src.search_page({"make": "X"}, page=3)  # start = 100
    assert page.has_next is False  # 100 + 50 == 150, nothing beyond


def test_marketcheck_explicit_sort_wins_over_default():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"num_found": 0, "listings": []})

    src, _ = _mc_source(handler)
    src.search_page({"make": "X", "sort_by": "price", "sort_order": "asc"})
    assert "sort_by=price" in seen["url"] and "sort_order=asc" in seen["url"]
    assert "dos_active" not in seen["url"]


def test_marketcheck_budget_cap_blocks_search():
    src, _ = _mc_source(lambda r: httpx.Response(200, json={"listings": []}), cap=0)
    with pytest.raises(BudgetExceeded):
        src.search_page({"make": "X"})


def test_marketcheck_facets_one_gated_call():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "num_found": 812,
                "facets": {
                    "model": [
                        {"item": "CT4-V Blackwing", "count": 74},
                        {"item": "CT4-V", "count": 210},
                        {"item": "CT4", "count": 528},
                    ]
                },
            },
        )

    src, guard = _mc_source(handler)
    total, result = src.facets({"make": "Cadillac"}, ["model"])
    assert "rows=0" in seen["url"] and "facets=model" in seen["url"]
    assert total == 812
    assert result["model"][0] == ("CT4-V Blackwing", 74)
    assert len(result["model"]) == 3
    assert guard.usage(MARKETCHECK).used == 1  # exactly one gated call


def test_marketcheck_facets_tolerates_value_key_and_missing_fields():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "num_found": 10,
                "facets": {"year": [{"value": "2023", "count": 4}, {"bogus": True}]},
            },
        )

    src, _ = _mc_source(handler)
    total, result = src.facets({"make": "X"}, ["year", "model"])
    assert result["year"] == [("2023", 4)]  # 'value' accepted, junk skipped
    assert result["model"] == []  # requested but not echoed -> empty, no crash
