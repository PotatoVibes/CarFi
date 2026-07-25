"""partsdeal siblings (bmwpartsdeal / toyotapartsdeal) — recorded fixtures,
offline.

The fixtures are the contract evidence for the CLAIMED-only provenance
ruling: the 2023 A91-MT Edition (factory MANUAL) and the 2023 3.0 Premium
(automatic) return byte-identical catalog attributes — including a trans
field that is wrong for one car and a gear-shift field wrong for the other.
Nothing from this source may ever verify or fail a hard requirement.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from carfinder import db
from carfinder.budget import (
    BMW_PARTSDEAL,
    TOYOTA_PARTSDEAL,
    BudgetGuard,
    ProviderBudget,
)
from carfinder.buildsheet import FOUND, NOT_FOUND
from carfinder.partsdeal import SITES, PartsDealFetcher, parse_response

FIXTURES = Path(__file__).parent / "fixtures" / "partsdeal"

M2_2023 = "3MF13DM09P8D14983"
M2_2025 = "3MF23DM09S8F25064"
SUPRA_MT_2023 = "WZ1DB0G06PW056034"  # A91-MT Edition, factory 6MT
SUPRA_PREMIUM_2023 = "WZ1DB0G07PW059282"  # 3.0 Premium, automatic
SUPRA_2020 = "WZ1DB4C08LW027514"


def _fixture(vin: str) -> dict:
    return json.loads((FIXTURES / f"{vin}.json").read_text(encoding="utf-8"))


# --- parser ------------------------------------------------------------------


def test_parse_bmw_thin_decode():
    sheet = parse_response(_fixture(M2_2023), M2_2023)
    assert sheet is not None
    assert sheet.year == 2023
    assert sheet.options == set()  # BMW returns no factory option codes
    assert sheet.transmission is None  # no transmission data exists — never guess
    assert "765" in sheet.engine
    # Everything kept lands in the (claimed) evidence text.
    assert "TRIM 1959-BASE" in sheet.text
    assert "2023 BMW M2" in sheet.text


def test_parse_toyota_keeps_pervin_data_and_katashiki():
    sheet = parse_response(_fixture(SUPRA_2020), SUPRA_2020)
    assert sheet is not None
    assert sheet.year == 2020
    assert sheet.production_date == "2019-09"  # genuinely per-VIN
    assert "DB43L-ZRLA" in sheet.model_string  # catalog code, corpus-study fodder
    assert sheet.options == set()
    assert "MODEL CODE DB43L-ZRLA" in sheet.text
    assert "B58" in sheet.text  # engine model as a claimed cross-check


def test_untrustworthy_transmission_attrs_are_dropped():
    """The catalog's trans fields are self-contradictory on real 2023 cars
    (manual A91-MT says ATM; automatic Premium says 6-speed-manual shift).
    Neither may reach the text a matcher could hit, nor the parsed field."""
    for vin in (SUPRA_MT_2023, SUPRA_PREMIUM_2023, SUPRA_2020):
        sheet = parse_response(_fixture(vin), vin)
        assert sheet.transmission is None
        assert "MANUAL" not in sheet.text  # the 6F gear-shift line is gone
        assert "ATM" not in sheet.text
        assert "AUTOMATIC" not in sheet.text
        assert "GEAR SHIFT" not in sheet.text


def test_2023_catalog_collapse_is_pinned_by_fixtures():
    """The A91-MT and the Premium share one catalog bucket — the reason this
    source is claimed-only. If Toyota ever splits them, this test fails and
    the provenance ruling deserves a fresh look."""
    mt = parse_response(_fixture(SUPRA_MT_2023), SUPRA_MT_2023)
    prem = parse_response(_fixture(SUPRA_PREMIUM_2023), SUPRA_PREMIUM_2023)
    assert mt.model_string == prem.model_string  # both DB07L-ZULA
    assert "BASE" in mt.text and "BASE" in prem.text  # trim collapsed too


def test_parse_unknown_vin_returns_none():
    assert parse_response({"data": {"vinInfos": []}}, "X") is None
    assert parse_response({"data": {}}, "X") is None


# --- fetcher -----------------------------------------------------------------


def _fetcher(handler, make: str = "bmw", cap: float = 100):
    conn = db.bootstrap(":memory:")
    provider = BMW_PARTSDEAL if make == "bmw" else TOYOTA_PARTSDEAL
    guard = BudgetGuard(conn, {provider: ProviderBudget(provider, "count", cap)})
    client = httpx.Client(base_url=SITES[make][0], transport=httpx.MockTransport(handler))
    fetcher = PartsDealFetcher(
        conn, guard, make, client=client, min_interval_s=0.0, sleep=lambda s: None
    )
    return fetcher, conn


def test_fetch_caches_found_and_never_refetches():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_fixture(M2_2023))

    fetcher, _ = _fetcher(handler)
    first = fetcher.fetch(M2_2023)
    assert first.status == FOUND and not first.from_cache
    assert first.sheet is not None and first.sheet.year == 2023
    second = fetcher.fetch(M2_2023)
    assert second.status == FOUND and second.from_cache
    assert calls["n"] == 1  # cache-forever per VIN
    assert fetcher.is_cached(M2_2023)


def test_fetch_records_definitive_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"vinInfos": []}})

    fetcher, _ = _fetcher(handler, make="toyota")
    result = fetcher.fetch(SUPRA_2020)
    assert result.status == NOT_FOUND and not result.from_cache
    # Cached as definitive: the second call makes no network request.
    again = fetcher.fetch(SUPRA_2020)
    assert again.status == NOT_FOUND and again.from_cache


def test_fetch_blocked_is_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    fetcher, _ = _fetcher(handler)
    result = fetcher.fetch(M2_2023)
    assert result.status == "blocked"
    assert not fetcher.is_cached(M2_2023)  # retry allowed on a later run


def test_calls_are_ledgered_per_make_provider():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_fixture(SUPRA_2020))

    fetcher, conn = _fetcher(handler, make="toyota")
    fetcher.fetch(SUPRA_2020)
    row = conn.execute(
        "SELECT provider, endpoint FROM api_calls WHERE provider = ?", (TOYOTA_PARTSDEAL,)
    ).fetchone()
    assert row is not None and row["endpoint"] == "decode-vin"
