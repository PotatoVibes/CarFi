"""OEM build data client + normalizer tests (offline)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from carfinder import db
from carfinder.budget import AUTODEV, BudgetExceeded, BudgetGuard, ProviderBudget
from carfinder.oem import OemBuildSource, _extract_option_codes, normalize_build

FIX = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "autodev"


def _source(handler, cap=100):
    conn = db.bootstrap(":memory:")
    guard = BudgetGuard(conn, {AUTODEV: ProviderBudget(AUTODEV, "count", cap)})
    client = httpx.Client(base_url=OemBuildSource.BASE_URL, transport=httpx.MockTransport(handler))
    return OemBuildSource("key", guard, client=client), guard


# --- normalizer ------------------------------------------------------------


def test_normalize_available_build():
    raw = {
        "data": {
            "vin": "1G6D65RP8N0460286",
            "year": 2022,
            "make": "Cadillac",
            "model": "CT4-V",
            "trim": "Blackwing",
            "msrp": 70345,
            "exteriorColorCode": "GBA",
            "exteriorColor": "Black Raven",
            "options": ["FE4", "KU9", "UV6", "CF5", "GBA", "MG9", "UQT"],
        }
    }
    f = normalize_build(raw)
    assert f.available
    assert f.vin == "1G6D65RP8N0460286" and f.year == 2022
    assert f.make == "Cadillac" and f.msrp == 70345
    assert f.paint_rpo == "GBA" and f.paint_desc == "Black Raven"
    assert {"FE4", "KU9", "MG9"} <= f.options


def test_normalize_real_402_fixture_is_unavailable():
    raw = json.loads((FIX / "build_402_unavailable.json").read_text())
    f = normalize_build(raw)
    assert f.available is False
    assert f.options == set()


def test_extract_options_variants():
    assert _extract_option_codes({"options": ["fe4", "ku9"]}) == {"FE4", "KU9"}
    assert _extract_option_codes({"options": [{"code": "FE4"}, {"rpo": "KU9"}]}) == {"FE4", "KU9"}
    assert _extract_option_codes({"rpo": {"FE4": "desc", "KU9": "desc"}}) == {"FE4", "KU9"}
    assert _extract_option_codes({}) == set()


# --- client ----------------------------------------------------------------


def test_get_build_402_is_unavailable_not_error():
    body = {"status": 402, "code": "FEATURE_NOT_AVAILABLE"}
    src, guard = _source(lambda r: httpx.Response(402, json=body))
    facts = src.get_build("1G6D65RP8N0460286")
    assert facts.available is False
    assert guard.usage(AUTODEV).used == 1  # still counted through the guard


def test_get_build_404_is_unavailable():
    src, _ = _source(lambda r: httpx.Response(404))
    assert src.get_build("XYZ").available is False


def test_get_build_200_available():
    def handler(request):
        assert request.url.path.endswith("/build/ABC")
        return httpx.Response(200, json={"data": {"vin": "ABC", "year": 2023, "options": ["Z51"]}})

    src, _ = _source(handler)
    f = src.get_build("ABC")
    assert f.available and f.year == 2023 and "Z51" in f.options


def test_build_call_counts_against_autodev_budget():
    src, _ = _source(lambda r: httpx.Response(200, json={"data": {"vin": "A"}}), cap=0)
    with pytest.raises(BudgetExceeded):
        src.get_build("A")


def test_server_error_raises():
    src, _ = _source(lambda r: httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        src.get_build("A")
