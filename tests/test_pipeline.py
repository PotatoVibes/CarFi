"""End-to-end pipeline tests: sync -> sticker -> parse -> score -> persist.

Uses the committed real sticker PDFs (cache hits, no network) plus a mocked GM
transport for the no-sticker VIN. Offline.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from carfinder import db
from carfinder.budget import GM_STICKER, BudgetGuard, ProviderBudget
from carfinder.pipeline import Pipeline
from carfinder.profiles import load_profile
from carfinder.sources import ListingRow, Page
from carfinder.stickers import StickerFetcher

ROOT = Path(__file__).resolve().parents[1]
STICKERS = ROOT / "stickers"
GM_MISS = b'{"errorCode":1001,"errorMessage":"No Window Sticker found for the requested VIN."}'

BW_VIN = "1G6D65RP8N0460286"  # cached fixture: manual Blackwing
C8_VIN = "1G1YC3D47P5121316"  # cached fixture: 3LT Z51 convertible, no MagneRide


class Clock:
    def __init__(self):
        self.t = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)

    def __call__(self):
        self.t = self.t.replace(second=(self.t.second + 1) % 60)
        return self.t


class FakeSource:
    name = "autodev"

    def __init__(self, rows):
        self.rows = rows

    def search_page(self, params, page=1, sort="createdAt.desc", run_id=None):
        rows = self.rows if page == 1 else []
        return Page(rows=rows, total=len(self.rows), has_next=False)


def _pipeline(rows, max_stickers=50):
    conn = db.bootstrap(":memory:")
    guard = BudgetGuard(conn, {GM_STICKER: ProviderBudget(GM_STICKER, "count", 100)})
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=GM_MISS))
    )
    fetcher = StickerFetcher(
        conn, guard, stickers_dir=STICKERS, client=client, min_interval_s=0.0, sleep=lambda s: None
    )
    pipe = Pipeline(
        conn,
        FakeSource(rows),
        fetcher,
        current_year=2026,
        now=Clock(),
        max_stickers_per_run=max_stickers,
    )
    return pipe, conn


def _match(conn, vin):
    return conn.execute("SELECT * FROM match_results WHERE vin=?", (vin,)).fetchone()


def test_blackwing_verified_ranked():
    rows = [
        ListingRow(
            vin=BW_VIN,
            source="autodev",
            price=58000,
            miles=30000,
            year=2022,
            make="Cadillac",
            model="CT4-V",
            trim="V-Series Blackwing",
        )
    ]
    pipe, conn = _pipeline(rows)
    bw = load_profile(ROOT / "config" / "ct4v_blackwing.yaml")
    stats = pipe.run(bw, run_id="r1")
    assert stats.scored == 1
    assert stats.ranked == 1
    m = _match(conn, BW_VIN)
    assert m["verdict"] == "ranked"
    assert m["total"] > 0
    # Breakdown persisted and parseable; every hard req verified-passed.
    bd = json.loads(m["breakdown_json"])
    assert all(h["provenance"] == "verified" and h["status"] == "pass" for h in bd["hard"])


def test_c8_verified_excluded_on_magneride():
    rows = [
        ListingRow(
            vin=C8_VIN,
            source="autodev",
            price=64000,
            miles=12000,
            year=2023,
            make="Chevrolet",
            model="Corvette",
            trim="Stingray",
        )
    ]
    pipe, conn = _pipeline(rows)
    c8 = load_profile(ROOT / "config" / "c8_stingray.yaml")
    stats = pipe.run(c8, run_id="r1")
    assert stats.excluded == 1
    m = _match(conn, C8_VIN)
    assert m["verdict"] == "excluded"
    bd = json.loads(m["breakdown_json"])
    mag = next(h for h in bd["hard"] if h["key"] == "magneride")
    assert mag["status"] == "fail"


def test_no_sticker_vin_needs_review():
    vin = "1G1YZZZZZZ5999999"
    rows = [
        ListingRow(
            vin=vin,
            source="autodev",
            price=64000,
            miles=15000,
            year=2024,
            make="Chevrolet",
            model="Corvette",
            trim="Stingray",
        )
    ]
    pipe, conn = _pipeline(rows)
    c8 = load_profile(ROOT / "config" / "c8_stingray.yaml")
    stats = pipe.run(c8, run_id="r1")
    assert stats.needs_review == 1
    assert stats.stickers_fetched == 1  # one network attempt (the miss)
    assert _match(conn, vin)["verdict"] == "needs_review"
    assert db.get_sticker_status(conn, vin)["status"] == "not_found"


def test_second_run_uses_cache_no_new_fetches():
    rows = [
        ListingRow(
            vin=BW_VIN,
            source="autodev",
            price=58000,
            miles=30000,
            year=2022,
            make="Cadillac",
            model="CT4-V",
            trim="Blackwing",
        )
    ]
    pipe, conn = _pipeline(rows)
    bw = load_profile(ROOT / "config" / "ct4v_blackwing.yaml")
    s1 = pipe.run(bw, run_id="r1")
    # BW sticker is a committed cache hit -> zero network fetches even first run.
    assert s1.stickers_fetched == 0


def test_conflict_logged_when_listing_disagrees_with_sticker():
    # Sticker (verified) says 2022 Black; listing (claimed) says 2099 and Blue.
    rows = [
        ListingRow(
            vin=BW_VIN,
            source="autodev",
            price=58000,
            miles=30000,
            year=2099,
            make="Cadillac",
            model="CT4-V",
            trim="Blackwing",
            color="Rapid Blue",
        )
    ]
    pipe, conn = _pipeline(rows)
    bw = load_profile(ROOT / "config" / "ct4v_blackwing.yaml")
    stats = pipe.run(bw, run_id="r1")
    assert stats.conflicts >= 2
    fields = {
        r["field"]
        for r in conn.execute("SELECT field FROM conflicts WHERE vin=?", (BW_VIN,)).fetchall()
    }
    assert {"year", "exterior"} <= fields
    # Winner is always the sticker (verified).
    winners = {
        r["winner"]
        for r in conn.execute("SELECT winner FROM conflicts WHERE vin=?", (BW_VIN,)).fetchall()
    }
    assert winners == {"sticker"}


def test_conflicts_rewritten_idempotently_on_rerun():
    rows = [
        ListingRow(
            vin=BW_VIN,
            source="autodev",
            price=58000,
            miles=30000,
            year=2099,
            make="Cadillac",
            model="CT4-V",
            trim="Blackwing",
        )
    ]
    pipe, conn = _pipeline(rows)
    bw = load_profile(ROOT / "config" / "ct4v_blackwing.yaml")
    pipe.run(bw, run_id="r1")
    first = conn.execute("SELECT COUNT(*) FROM conflicts WHERE vin=?", (BW_VIN,)).fetchone()[0]
    # Re-run with a price change (so the VIN re-processes); conflicts must not
    # accumulate duplicates.
    pipe._engine._source.rows = [
        ListingRow(
            vin=BW_VIN,
            source="autodev",
            price=55000,
            miles=30000,
            year=2099,
            make="Cadillac",
            model="CT4-V",
            trim="Blackwing",
        )
    ]
    pipe.run(bw, run_id="r2")
    second = conn.execute("SELECT COUNT(*) FROM conflicts WHERE vin=?", (BW_VIN,)).fetchone()[0]
    assert first == second == 1  # year conflict only, not duplicated


def test_ai_fallback_raises_claimed_features_when_no_sticker():
    """No sticker for this VIN -> Haiku reads the dealer text and its claimed
    features flow into scoring (needs_review, never a verified pass)."""
    import httpx as _httpx

    from carfinder.ai import HaikuExtractor
    from carfinder.budget import ANTHROPIC, ProviderBudget

    vin = "1G1YQQQQQQ5222222"  # no GM sticker
    rows = [
        ListingRow(
            vin=vin,
            source="autodev",
            price=64000,
            miles=15000,
            year=2024,
            make="Chevrolet",
            model="Corvette",
            trim="Stingray Convertible 3LT Z51",
        )
    ]
    pipe, conn = _pipeline(rows)

    # Attach a mocked Haiku extractor: it claims every requested attribute.
    def ai_handler(request):
        import json

        body = json.loads(request.content)
        schema = body["output_config"]["format"]["schema"]
        feats = {k: "claimed" for k in schema["properties"]}
        return _httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": json.dumps(feats)}],
                "usage": {"input_tokens": 500, "output_tokens": 40},
            },
        )

    ai_guard = BudgetGuard(conn, {ANTHROPIC: ProviderBudget(ANTHROPIC, "cost", 2.0)})
    pipe._extractor = HaikuExtractor(
        conn,
        ai_guard,
        "key",
        client=_httpx.Client(transport=_httpx.MockTransport(ai_handler)),
    )

    c8 = load_profile(ROOT / "config" / "c8_stingray.yaml")
    stats = pipe.run(c8, run_id="r1")
    assert stats.ai_calls == 1
    assert stats.ai_cost_usd > 0
    m = _match(conn, vin)
    # Every hard req is claimed-only -> needs_review (AI never satisfies hard).
    assert m["verdict"] == "needs_review"
    bd = json.loads(m["breakdown_json"])
    z51 = next(h for h in bd["hard"] if h["key"] == "z51")
    assert z51["status"] == "needs_review" and z51["provenance"] == "claimed"
    # Highlights the AI claimed should badge (claimed provenance).
    assert m["highlight_bonus"] > 0


def test_rescore_reapplies_profile_without_a_source():
    """Editing a profile then rescoring re-ranks existing cars with no sync,
    no source, and no network."""
    rows = [
        ListingRow(
            vin=BW_VIN,
            source="autodev",
            price=58000,
            miles=30000,
            year=2022,
            make="Cadillac",
            model="CT4-V",
            trim="V-Series Blackwing",
        )
    ]
    pipe, conn = _pipeline(rows)
    bw = load_profile(ROOT / "config" / "ct4v_blackwing.yaml")
    pipe.run(bw, run_id="r1")
    before = _match(conn, BW_VIN)["total"]

    # Rescore-only pipeline: no source at all.
    from carfinder.pipeline import Pipeline as _P

    guard = BudgetGuard(conn, {GM_STICKER: ProviderBudget(GM_STICKER, "count", 100)})
    fetcher = StickerFetcher(
        conn,
        guard,
        stickers_dir=STICKERS,
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=GM_MISS))
        ),
        min_interval_s=0.0,
        sleep=lambda s: None,
    )
    rescorer = _P(conn, None, fetcher, 2026, now=Clock())

    # Tighten the price curve so this car scores lower, then rescore.
    price = next(c for c in bw.soft if c.key == "price")
    object.__setattr__(price, "ideal", 40000)
    object.__setattr__(price, "hard", 45000)
    stats = rescorer.rescore(bw)
    after = _match(conn, BW_VIN)["total"]
    assert stats.scored == 1
    assert after < before  # profile edit re-applied to the existing car


def test_rescore_requires_no_source_but_run_does():
    conn = db.bootstrap(":memory:")
    guard = BudgetGuard(conn, {GM_STICKER: ProviderBudget(GM_STICKER, "count", 100)})
    fetcher = StickerFetcher(
        conn,
        guard,
        stickers_dir=STICKERS,
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        min_interval_s=0.0,
        sleep=lambda s: None,
    )
    from carfinder.pipeline import Pipeline as _P

    p = _P(conn, None, fetcher, 2026)
    bw = load_profile(ROOT / "config" / "ct4v_blackwing.yaml")
    # rescore works with no vehicles (no-op); run() must refuse without a source.
    assert p.rescore(bw).scored == 0
    import pytest as _pytest

    with _pytest.raises(ValueError, match="listing source"):
        p.run(bw)


def test_sticker_cap_scores_on_listing_only():
    vin = "1G1YAAAAAA5111111"  # not cached -> would hit network
    rows = [
        ListingRow(
            vin=vin,
            source="autodev",
            price=64000,
            miles=15000,
            year=2024,
            make="Chevrolet",
            model="Corvette",
            trim="Stingray",
        )
    ]
    pipe, conn = _pipeline(rows, max_stickers=0)
    c8 = load_profile(ROOT / "config" / "c8_stingray.yaml")
    stats = pipe.run(c8, run_id="r1")
    assert stats.sticker_capped is True
    assert stats.stickers_fetched == 0
    # Still scored (on listing data) and persisted.
    assert _match(conn, vin) is not None
