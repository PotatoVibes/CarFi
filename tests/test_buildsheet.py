"""Build-sheet fallback source (gmpartsgiant) — recorded fixture, offline."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from carfinder import db
from carfinder.budget import BUILDSHEET, BudgetExceeded, BudgetGuard, ProviderBudget
from carfinder.buildsheet import (
    BLOCKED,
    FOUND,
    NOT_FOUND,
    BuildSheetFetcher,
    parse_response,
    vehicle_kwargs,
)

FIXTURE = Path(__file__).parent / "fixtures" / "buildsheet" / "1G6D65RP8N0460286.json"
BW_VIN = "1G6D65RP8N0460286"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# --- parser ------------------------------------------------------------------


def test_parse_fixture_manual_blackwing():
    sheet = parse_response(_fixture(), BW_VIN)
    assert sheet is not None
    assert sheet.year == 2022
    assert sheet.production_date == "2022-01-28"
    assert sheet.model_string == "CT4 V-Series Notchback-Blackwing"
    assert sheet.transmission == "6-Speed Manual Transmission, Tremec"
    # The codes the BW profile matches on, all present in the broadcast list.
    assert {"KU9", "UV6", "UQT", "CF5", "F55"} <= sheet.options
    # Text blob carries the regex-matchable lines (model + transmission).
    assert "BLACKWING" in sheet.text
    assert "6-SPEED MANUAL TRANSMISSION" in sheet.text
    assert "MAGNETO RHEOLOGICAL" in sheet.text
    # Factory paint decoded.
    assert sheet.paint_rpo == "GBA"
    assert sheet.paint_name == "BLACK"


def test_parse_skips_structural_fields():
    sheet = parse_response(_fixture(), BW_VIN)
    # Date stamps / catalog keys must not leak into the RPO set.
    assert not any(c.startswith("2022") for c in sheet.options)
    assert "6D4" not in sheet.options  # catalog code has no descriptor


def test_parse_unknown_vin_returns_none():
    assert parse_response({"data": {"vinInfos": []}}, "X") is None
    assert parse_response({"data": {}}, "X") is None


def test_vehicle_kwargs_never_claims_completeness():
    kw = vehicle_kwargs(parse_response(_fixture(), BW_VIN))
    # GM sheets omit some real options (MagneRide on Chevrolet), so absence
    # of a code is never definitive evidence of absence.
    assert kw["sticker_complete"] is False
    assert "KU9" in kw["verified_rpo"]


# --- fetcher -----------------------------------------------------------------


def _fetcher(handler, cap: float = 100):
    conn = db.bootstrap(":memory:")
    guard = BudgetGuard(conn, {BUILDSHEET: ProviderBudget(BUILDSHEET, "count", cap)})
    client = httpx.Client(
        base_url=BuildSheetFetcher.BASE_URL, transport=httpx.MockTransport(handler)
    )
    return BuildSheetFetcher(conn, guard, client=client, min_interval_s=0.0, sleep=lambda s: None)


def test_found_and_cached():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=_fixture())

    f = _fetcher(handler)
    r1 = f.fetch(BW_VIN)
    assert r1.status == FOUND and not r1.from_cache
    assert r1.sheet.transmission.startswith("6-Speed Manual")
    assert calls == [{"vin": BW_VIN}]
    r2 = f.fetch(BW_VIN)  # second ask: DB cache, zero network
    assert r2.status == FOUND and r2.from_cache
    assert len(calls) == 1
    assert r2.sheet.options == r1.sheet.options


def test_empty_vininfos_is_definitive_not_found():
    f = _fetcher(lambda r: httpx.Response(200, json={"data": {"vinInfos": []}}))
    assert f.fetch("1G6XXXXXXXXXXXXXX").status == NOT_FOUND
    assert f.is_cached("1G6XXXXXXXXXXXXXX")  # never re-asked


def test_http_400_is_not_found_and_5xx_is_blocked():
    f = _fetcher(lambda r: httpx.Response(400, json={}))
    assert f.fetch("BADVIN00000000000").status == NOT_FOUND
    f2 = _fetcher(lambda r: httpx.Response(503))
    r = f2.fetch(BW_VIN)
    assert r.status == BLOCKED
    assert not f2.is_cached(BW_VIN)  # transient -> retry on a later run


def test_budget_cap_blocks_fetch():
    f = _fetcher(lambda r: httpx.Response(200, json=_fixture()), cap=0)
    with pytest.raises(BudgetExceeded):
        f.fetch(BW_VIN)
