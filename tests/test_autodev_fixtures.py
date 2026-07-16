"""Contract tests against recorded Auto.dev fixtures (offline).

These lock the real payload shape captured in the M3 fixture session, so
provider drift shows up as a failing test rather than a silent sync bug.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from carfinder import db
from carfinder.budget import AUTODEV, BudgetGuard, ProviderBudget
from carfinder.sources import AutoDevSource, normalize

FIX = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "autodev"


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _source_returning(payload: dict, page_size: int = 20) -> AutoDevSource:
    conn = db.bootstrap(":memory:")
    guard = BudgetGuard(conn, {AUTODEV: ProviderBudget(AUTODEV, "count", 100)})
    client = httpx.Client(
        base_url=AutoDevSource.BASE_URL,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)),
    )
    return AutoDevSource("key", guard, client=client, page_size=page_size)


def test_c8_search_page_contract():
    src = _source_returning(_load("c8_search_p1.json"))
    page = src.search_page({"vehicle.make": "Chevrolet"}, page=1)
    assert page.total == 1697
    assert page.has_next is True  # links.next present
    assert len(page.rows) == 20
    row = page.rows[0]
    assert row.vin and row.make == "Chevrolet" and row.model == "Corvette"
    assert row.year and 2022 <= row.year <= 2027


def test_flat_projection_fields_map():
    # The search projection returns flat dot-keyed records.
    rec = _load("c8_search_p1.json")["data"][0]
    assert "vehicle.vin" in rec  # confirms flat form
    row = normalize(rec, "autodev")
    assert row.vin == rec["vehicle.vin"].upper()
    assert row.price == rec["retailListing.price"]


def test_single_vin_listing_nested_shape():
    # The un-projected single-VIN payload is nested; miles live under
    # retailListing.miles and the URL under retailListing.vdp.
    src = _source_returning(_load("listing_1G1YB2D48S5106930.json"))
    row = src.get_listing("1G1YB2D48S5106930")
    assert row.vin == "1G1YB2D48S5106930"
    assert row.miles == 12980  # retailListing.miles, not the null vehicle.mileage
    assert row.price == 73590
    assert row.color == "Black"
    assert row.url and "carvana" in row.url.lower()


def test_blackwing_model_string_is_ct4v():
    data = _load("bw_search_p1.json")
    rows = [normalize(item, "autodev") for item in data["data"]]
    assert rows, "blackwing fixture should have rows"
    assert all(r.make == "Cadillac" for r in rows)
    assert all(r.model == "CT4-V" for r in rows)
    # Trim carries the Blackwing designation (claimed; scorer confirms via VIN).
    assert any("blackwing" in (r.trim or "").lower() for r in rows)
