"""Marketcheck radius-probe tests — pure verdict logic + mocked-transport run.

Zero real network: run_probe drives a MarketcheckSource wired to an httpx
MockTransport, exactly like the source tests. The point is to prove the probe
interprets national/wide/local counts correctly and never leaks the API key.
"""

from __future__ import annotations

import httpx

from carfinder import db
from carfinder.budget import MARKETCHECK, BudgetGuard, ProviderBudget
from carfinder.marketcheck_probe import ProbeResult, interpret, run_probe, scrub
from carfinder.sources import MarketcheckSource

# --- scrub -----------------------------------------------------------------


def test_scrub_redacts_api_key():
    msg = "GET https://api.marketcheck.com/v2/search/car/active?api_key=SECRET123&make=Ford failed"
    out = scrub(msg)
    assert "SECRET123" not in out
    assert "api_key=REDACTED" in out


# --- interpret: national search --------------------------------------------


def _national(num_found, n_states):
    states = {f"S{i}": 1 for i in range(n_states)}
    return ProbeResult(
        "national (no location)", num_found=num_found, rows_returned=50, states=states
    )


def test_interpret_national_works_when_many_states():
    results = [_national(4200, 12), ProbeResult("radius=2000 @ z"), ProbeResult("radius=100 @ z")]
    verdict = " ".join(interpret(results))
    assert "NATIONAL search" in verdict


def test_interpret_national_failed_means_location_required():
    results = [
        ProbeResult("national (no location)", error="HTTP 422"),
        ProbeResult("radius=2000 @ z", num_found=10),
        ProbeResult("radius=100 @ z", num_found=10),
    ]
    verdict = " ".join(interpret(results))
    assert "REQUIRED" in verdict


def test_interpret_national_clustered_is_flagged():
    results = [_national(300, 1), ProbeResult("radius=2000 @ z"), ProbeResult("radius=100 @ z")]
    verdict = " ".join(interpret(results))
    assert "region-clustered" in verdict


# --- interpret: radius behavior --------------------------------------------


def test_interpret_radius_capped_when_counts_match():
    results = [
        _national(4200, 12),
        ProbeResult("radius=2000 @ z", num_found=812),
        ProbeResult("radius=100 @ z", num_found=800),  # within 10%
    ]
    verdict = " ".join(interpret(results))
    assert "CAPPED" in verdict


def test_interpret_radius_honored_when_wide_much_larger():
    results = [
        _national(4200, 12),
        ProbeResult("radius=2000 @ z", num_found=3900),
        ProbeResult("radius=100 @ z", num_found=800),
    ]
    verdict = " ".join(interpret(results))
    assert "HONORED" in verdict


def test_interpret_radius_uncomparable_when_local_zero():
    results = [
        _national(4200, 12),
        ProbeResult("radius=2000 @ z", num_found=5),
        ProbeResult("radius=100 @ z", num_found=0),
    ]
    verdict = " ".join(interpret(results))
    assert "can't compare" in verdict


# --- run_probe (mocked transport) ------------------------------------------


def _source(handler, cap=100):
    conn = db.bootstrap(":memory:")
    guard = BudgetGuard(conn, {MARKETCHECK: ProviderBudget(MARKETCHECK, "count", cap)})
    client = httpx.Client(
        base_url=MarketcheckSource.BASE_URL, transport=httpx.MockTransport(handler)
    )
    src = MarketcheckSource("key", guard, client=client, min_interval_s=0.0)
    return src, guard


def test_run_probe_makes_three_gated_calls_and_collects_states():
    # National (no zip) returns nationwide states; radius queries return fewer.
    def handler(request):
        has_radius = "radius=" in str(request.url)
        if has_radius:
            listings = [{"vin": "L", "dealer": {"state": "TX"}}]
            return httpx.Response(200, json={"num_found": 800, "listings": listings})
        states = [("A", "TX"), ("B", "CA"), ("C", "FL"), ("D", "NY")]
        listings = [{"vin": v, "dealer": {"state": s}} for v, s in states]
        return httpx.Response(200, json={"num_found": 4200, "listings": listings})

    src, guard = _source(handler)
    results = run_probe(src, "Ford", "F-150", 2018, 2024, "77498", run_id="t")
    labels = [r.label.split(" @")[0].split(" (")[0] for r in results]
    assert labels == ["national", "radius=2000", "radius=100"]
    national = results[0]
    assert national.num_found == 4200
    assert set(national.states) == {"TX", "CA", "FL", "NY"}
    assert guard.usage(MARKETCHECK).used == 3  # every probe call gated + recorded


def test_run_probe_captures_error_without_raising():
    def handler(request):
        # National ok; any radius query 500s.
        if "radius=" in str(request.url):
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"num_found": 10, "listings": []})

    # max_retries defaults to 4, so a persistent 500 exhausts and raises inside
    # search_page — run_probe must catch it and record an error result.
    src, _ = _source(handler)
    src._max_retries = 1
    results = run_probe(src, "Ford", "F-150", 2018, 2024, "77498")
    assert results[0].error is None and results[0].num_found == 10
    assert results[1].error is not None  # radius=2000 failed, captured not raised
    assert results[2].error is not None
