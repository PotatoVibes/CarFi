"""partsdeal siblings (bmwpartsdeal / toyotapartsdeal) — offline.

Payload shapes mirror the live probes of 2026-07-24 (real VINs
3MF13DM08P8D49739 and WZ1DB4C08LW027514); the committed fixture capture (M8)
replaces these inline samples with recorded JSON once taken.
"""

from __future__ import annotations

import httpx

from carfinder import db
from carfinder.budget import (
    BMW_PARTSDEAL,
    TOYOTA_PARTSDEAL,
    BudgetGuard,
    ProviderBudget,
)
from carfinder.buildsheet import FOUND, NOT_FOUND
from carfinder.partsdeal import SITES, PartsDealFetcher, parse_response, sheet_facts

M2_VIN = "3MF13DM09P8D14983"
SUPRA_VIN = "WZ1DB4C08LW027514"


def _bmw_payload() -> dict:
    return {
        "data": {
            "vinInfos": [
                {
                    "vehicleInfo": "2023 BMW M2",
                    "optionalInfo": "",
                    "majorAttribute": [
                        {"name": "Trim", "desc": "1959-Base"},
                        {"name": "Engine", "desc": "765-6 Cyl 3.0 L GAS"},
                        {"name": "BodyNumDoors", "desc": "2"},
                        {"name": "BodyType", "desc": "Coupe"},
                    ],
                    "vehicleInformation": [],
                    "specification": [],
                }
            ]
        }
    }


def _toyota_payload(trans: str = "ATM") -> dict:
    return {
        "data": {
            "vinInfos": [
                {
                    "vehicleInfo": "2020 Toyota GR Supra",
                    "optionalInfo": "",
                    "majorAttribute": [
                        {"name": "Destination", "desc": "USA"},
                        {"name": "Engine Model", "desc": "B58-3.0L"},
                        {"name": "Body Type", "desc": "CP"},
                        {"name": "Trim", "desc": "BASE"},
                        {"name": "Trans", "desc": trans},
                        {"name": "Model Code", "desc": "DB43L-ZRLA"},
                    ],
                    "vehicleInformation": [
                        {"name": "Productiondate", "desc": "2019-09"},
                        {"name": "CatalogCode", "desc": "291440"},
                        {"name": "FrameNo.", "desc": "DB43"},
                    ],
                    "specification": [],
                }
            ]
        }
    }


# --- parser ------------------------------------------------------------------


def test_parse_bmw_thin_decode():
    sheet = parse_response(_bmw_payload(), M2_VIN)
    assert sheet is not None
    assert sheet.year == 2023
    assert sheet.options == set()  # BMW returns no factory option codes
    assert sheet.transmission is None  # and no transmission — never guess
    assert sheet.engine == "765-6 Cyl 3.0 L GAS"
    # Everything lands in the evidence text for sticker_text regexes.
    assert "TRIM 1959-BASE" in sheet.text
    assert "2023 BMW M2" in sheet.text


def test_parse_toyota_decode_carries_trans_and_katashiki():
    sheet = parse_response(_toyota_payload(), SUPRA_VIN)
    assert sheet is not None
    assert sheet.year == 2020
    assert sheet.transmission == "ATM"
    assert sheet.production_date == "2019-09"
    assert sheet.model_string == "DB43L-ZRLA"  # katashiki, corpus-study fodder
    assert sheet.options == set()
    assert "MODEL CODE DB43L-ZRLA" in sheet.text
    assert "TRANS ATM" in sheet.text


def test_parse_unknown_vin_returns_none():
    assert parse_response({"data": {"vinInfos": []}}, "X") is None
    assert parse_response({"data": {}}, "X") is None


# --- fact derivation ---------------------------------------------------------


def test_toyota_trans_facts_verified_both_ways():
    auto = parse_response(_toyota_payload("ATM"), SUPRA_VIN)
    manual = parse_response(_toyota_payload("MTM"), SUPRA_VIN)
    assert sheet_facts("toyota", auto) == {"trans": "AUTO"}
    assert sheet_facts("toyota", manual) == {"trans": "MANUAL"}


def test_bmw_never_derives_trans_fact():
    sheet = parse_response(_bmw_payload(), M2_VIN)
    assert sheet_facts("bmw", sheet) == {}


def test_unrecognized_trans_string_derives_nothing():
    sheet = parse_response(_toyota_payload("CVT-ISH?"), SUPRA_VIN)
    assert sheet_facts("toyota", sheet) == {}


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
        return httpx.Response(200, json=_bmw_payload())

    fetcher, _ = _fetcher(handler)
    first = fetcher.fetch(M2_VIN)
    assert first.status == FOUND and not first.from_cache
    assert first.sheet is not None and first.sheet.year == 2023
    second = fetcher.fetch(M2_VIN)
    assert second.status == FOUND and second.from_cache
    assert calls["n"] == 1  # cache-forever per VIN
    assert fetcher.is_cached(M2_VIN)


def test_fetch_records_definitive_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"vinInfos": []}})

    fetcher, _ = _fetcher(handler, make="toyota")
    result = fetcher.fetch(SUPRA_VIN)
    assert result.status == NOT_FOUND and not result.from_cache
    # Cached as definitive: the second call makes no network request.
    again = fetcher.fetch(SUPRA_VIN)
    assert again.status == NOT_FOUND and again.from_cache


def test_fetch_blocked_is_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    fetcher, _ = _fetcher(handler)
    result = fetcher.fetch(M2_VIN)
    assert result.status == "blocked"
    assert not fetcher.is_cached(M2_VIN)  # retry allowed on a later run


def test_calls_are_ledgered_per_make_provider():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_toyota_payload())

    fetcher, conn = _fetcher(handler, make="toyota")
    fetcher.fetch(SUPRA_VIN)
    row = conn.execute(
        "SELECT provider, endpoint FROM api_calls WHERE provider = ?", (TOYOTA_PARTSDEAL,)
    ).fetchone()
    assert row is not None and row["endpoint"] == "decode-vin"
