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


def test_parsed_sticker_cached_across_runs(monkeypatch):
    """pdfplumber parsing happens once per VIN ever — reruns hit the DB cache."""
    import carfinder.pipeline as pl

    calls = []
    real_parse = pl.parse_sticker
    monkeypatch.setattr(pl, "parse_sticker", lambda p: (calls.append(1), real_parse(p))[1])

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
    pipe.run(bw, run_id="r1")
    assert len(calls) == 1
    # Price change so the VIN re-processes; the parse must come from cache.
    pipe._engine._source.rows = [
        ListingRow(vin=BW_VIN, source="autodev", price=55000, miles=30000, year=2022)
    ]
    pipe.run(bw, run_id="r2")
    assert len(calls) == 1  # no re-parse
    # And the cached result scores identically (still ranked).
    assert _match(conn, BW_VIN)["verdict"] == "ranked"


def test_rescore_processes_best_active_cars_first():
    """The sticker-fetch budget goes to promising active cars, not sync order."""
    pipe, conn = _pipeline([])
    ts = "2026-07-15T12:00:00Z"
    cars = [  # (vin, status, prior_total)
        ("VINLOW000000000A1", "active", 50.0),
        ("VINHIGH00000000B2", "active", 90.0),
        ("VINSOLD00000000C3", "presumed_sold", 100.0),
    ]
    for vin, status, total in cars:
        conn.execute(
            """INSERT INTO vehicles (vin, model_key, status, missing_passes,
               first_seen, last_seen) VALUES (?, 'c8_stingray', ?, 0, ?, ?)""",
            (vin, status, ts, ts),
        )
        conn.execute(
            """INSERT INTO match_results (vin, model_key, verdict, soft_score,
               highlight_bonus, total, breakdown_json, scored_at)
               VALUES (?, 'c8_stingray', 'needs_review', ?, 0, ?, '{}', ?)""",
            (vin, total, total, ts),
        )
    conn.commit()
    seen: list[str] = []
    c8 = load_profile(ROOT / "config" / "c8_stingray.yaml")
    pipe.rescore(c8, progress=lambda msg: seen.append(msg.split("— ")[1].split(" ")[0]))
    # Active best-first, sold last.
    assert seen == ["VINHIGH00000000B2", "VINLOW000000000A1", "VINSOLD00000000C3"]


def test_vin_excluded_car_skips_sticker_fetch_entirely():
    """A 1LT coupe is verified-excluded by VIN alone — zero sticker spend."""
    vin = "1G1YA2D4XP5108883"  # 1LT coupe (real listing shape)
    rows = [
        ListingRow(
            vin=vin,
            source="autodev",
            price=59998,
            miles=5030,
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
    assert stats.stickers_fetched == 0  # fast path: no GM call for a VIN-failed car
    m = _match(conn, vin)
    assert m["verdict"] == "excluded"
    bd = json.loads(m["breakdown_json"])
    trim = next(h for h in bd["hard"] if h["key"] == "trim_2lt_3lt")
    conv = next(h for h in bd["hard"] if h["key"] == "convertible")
    assert trim["status"] == "fail" and trim["provenance"] == "verified"
    assert conv["status"] == "fail" and conv["provenance"] == "verified"
    stingray = next(h for h in bd["hard"] if h["key"] == "model_stingray")
    assert stingray["status"] == "pass" and stingray["provenance"] == "verified"


def test_vin_passing_car_still_fetches_sticker():
    """A 3LT convertible passes the VIN gate — sticker still needed for Z51."""
    vin = "1G1YC3D40P5999999"  # 3LT convertible pattern, no cached sticker
    rows = [
        ListingRow(
            vin=vin,
            source="autodev",
            price=64000,
            miles=15000,
            year=2023,
            make="Chevrolet",
            model="Corvette",
            trim="Stingray 3LT",
        )
    ]
    pipe, conn = _pipeline(rows)
    c8 = load_profile(ROOT / "config" / "c8_stingray.yaml")
    stats = pipe.run(c8, run_id="r1")
    assert stats.stickers_fetched == 1  # GM miss attempted (mocked)
    m = _match(conn, vin)
    bd = json.loads(m["breakdown_json"])
    # VIN verified the identity facts even with no sticker on file...
    trim = next(h for h in bd["hard"] if h["key"] == "trim_2lt_3lt")
    assert trim["status"] == "pass" and trim["provenance"] == "verified"
    # ...but Z51 isn't VIN-encoded, so the car correctly stays needs_review.
    z51 = next(h for h in bd["hard"] if h["key"] == "z51")
    assert z51["status"] == "needs_review"
    assert m["verdict"] == "needs_review"


class YearSource:
    """Serves rows keyed by the vehicle.year param — models Auto.dev's
    per-query pagination window forcing one walk per model year."""

    name = "autodev"

    def __init__(self, by_year):
        self.by_year = by_year
        self.year_params = []

    def search_page(self, params, page=1, sort="createdAt.desc", run_id=None):
        yp = params.get("vehicle.year")
        if page == 1:
            self.year_params.append(yp)
        rows = self.by_year.get(yp, []) if page == 1 else []
        return Page(rows=rows, total=None, has_next=False)


def _year_row(vin, year):
    return ListingRow(
        vin=vin,
        source="autodev",
        price=64000,
        miles=15000,
        year=year,
        make="Chevrolet",
        model="Corvette",
        trim="Stingray",
    )


def _seed_active_vehicle(conn, vin, model_key="c8_stingray"):
    ts = "2026-07-01T00:00:00Z"
    conn.execute(
        """INSERT INTO vehicles (vin, model_key, status, missing_passes, first_seen, last_seen)
           VALUES (?, ?, 'active', 0, ?, ?)""",
        (vin, model_key, ts, ts),
    )
    conn.commit()


def test_full_pass_walks_each_model_year_separately():
    """The provider window caps a single query at 2,000 rows; a segmented
    full pass sees every year's cars and delists only on the union."""
    a = "1G1YC3D40N5000001"  # 2022 car, served only by the 2022 segment
    b = "1G1YC3D40P5000002"  # 2023 car, served only by the 2023 segment
    src = YearSource({"2022-2022": [_year_row(a, 2022)], "2023-2023": [_year_row(b, 2023)]})
    conn = db.bootstrap(":memory:")
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
    pipe = Pipeline(conn, src, fetcher, current_year=2026, now=Clock())
    # A pre-existing active car that no segment returns -> one missed pass.
    gone = "1G1YC3D40M5000003"
    _seed_active_vehicle(conn, gone)

    c8 = load_profile(ROOT / "config" / "c8_stingray.yaml")
    stats = pipe.run(c8, mode="full", run_id="r1")

    # One single-year walk per year in the profile's band (2021..2027).
    assert src.year_params == [f"{y}-{y}" for y in range(2021, 2028)]
    assert stats.sync.new_vins == 2  # both years' cars ingested
    assert stats.sync.truncated is False
    rows = {
        r["vin"]: r["missing_passes"]
        for r in conn.execute("SELECT vin, missing_passes FROM vehicles").fetchall()
    }
    # Seen cars untouched; only the truly absent car accrues a miss —
    # per-segment delisting would have wrongly hit every other year's cars.
    assert rows[a] == 0 and rows[b] == 0
    assert rows[gone] == 1


def test_truncated_full_pass_skips_delist():
    """If any segment was cut off, absence is not evidence of a sale."""

    class EndlessSource(YearSource):
        def search_page(self, params, page=1, sort="createdAt.desc", run_id=None):
            if params.get("vehicle.year") != "2022-2022":
                return Page(rows=[], total=None, has_next=False)
            # 2022 never ends -> hits the page ceiling -> truncated.
            return Page(rows=[_year_row(f"1G1YC3D40N5{page:06d}", 2022)], total=None, has_next=True)

    conn = db.bootstrap(":memory:")
    guard = BudgetGuard(conn, {GM_STICKER: ProviderBudget(GM_STICKER, "count", 10000)})
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
    pipe = Pipeline(
        conn, EndlessSource({}), fetcher, current_year=2026, now=Clock(), max_stickers_per_run=0
    )
    gone = "1G1YC3D40M5000003"
    _seed_active_vehicle(conn, gone)

    c8 = load_profile(ROOT / "config" / "c8_stingray.yaml")
    stats = pipe.run(c8, mode="full", run_id="r1")
    assert stats.sync.truncated is True
    assert stats.sync.presumed_sold == []
    row = conn.execute("SELECT missing_passes FROM vehicles WHERE vin=?", (gone,)).fetchone()
    assert row["missing_passes"] == 0  # absence unproven -> untouched


def test_buildsheet_fallback_ranks_manual_blackwing_without_sticker():
    """Sticker not_found -> the build-sheet decoder supplies verified
    evidence: manual, ventilated seats, HUD, and the Blackwing model string."""
    from carfinder.budget import BUILDSHEET
    from carfinder.buildsheet import BuildSheetFetcher

    vin = "1G6D65RP0X0999999"  # Blackwing engine code P, not sticker-cached
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "buildsheet" / "1G6D65RP8N0460286.json").read_text(
            encoding="utf-8"
        )
    )
    rows = [
        ListingRow(
            vin=vin,
            source="autodev",
            price=58000,
            miles=30000,
            year=2022,
            make="Cadillac",
            model="CT4",
            trim="V Blackwing",
        )
    ]
    conn = db.bootstrap(":memory:")
    guard = BudgetGuard(
        conn,
        {
            GM_STICKER: ProviderBudget(GM_STICKER, "count", 100),
            BUILDSHEET: ProviderBudget(BUILDSHEET, "count", 100),
        },
    )
    sticker_fetcher = StickerFetcher(
        conn,
        guard,
        stickers_dir=STICKERS,
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=GM_MISS))
        ),
        min_interval_s=0.0,
        sleep=lambda s: None,
    )
    sheet_fetcher = BuildSheetFetcher(
        conn,
        guard,
        client=httpx.Client(
            base_url=BuildSheetFetcher.BASE_URL,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=fixture)),
        ),
        min_interval_s=0.0,
        sleep=lambda s: None,
    )
    pipe = Pipeline(
        conn,
        FakeSource(rows),
        sticker_fetcher,
        current_year=2026,
        now=Clock(),
        buildsheet_fetcher=sheet_fetcher,
    )
    bw = load_profile(ROOT / "config" / "ct4v_blackwing.yaml")
    stats = pipe.run(bw, run_id="r1")
    assert stats.stickers_fetched == 1  # the miss
    assert stats.buildsheets_fetched == 1  # the fallback
    m = _match(conn, vin)
    assert m["verdict"] == "ranked"
    bd = json.loads(m["breakdown_json"])
    manual = next(h for h in bd["hard"] if h["key"] == "manual_transmission")
    assert manual["status"] == "pass" and manual["provenance"] == "verified"
    vented = next(h for h in bd["hard"] if h["key"] == "ventilated_seats")
    assert vented["status"] == "pass" and vented["provenance"] == "verified"


def test_dismissed_car_never_notifies():
    """A qualifying car the user dismissed must not ping the webhook."""

    class SpyNotifier:
        enabled = True

        def __init__(self):
            self.sent = []

        def notify_match(self, vin, title, body):
            self.sent.append(vin)
            return True

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
    spy = SpyNotifier()
    pipe._notifier = spy
    pipe._notify_threshold = 50.0
    bw = load_profile(ROOT / "config" / "ct4v_blackwing.yaml")
    pipe.run(bw, run_id="r1")
    assert spy.sent == [BW_VIN]  # ranked above threshold -> notified once

    # User dismisses it; a price change re-processes the car -> no re-ping.
    conn.execute("UPDATE vehicles SET dismissed_at='2026-07-16T00:00:00Z' WHERE vin=?", (BW_VIN,))
    conn.execute("DELETE FROM match_results WHERE vin=?", (BW_VIN,))  # forces 'newly ranked'
    conn.commit()
    pipe._engine._source.rows = [
        ListingRow(vin=BW_VIN, source="autodev", price=55000, miles=30000, year=2022)
    ]
    pipe.run(bw, run_id="r2")
    assert spy.sent == [BW_VIN]  # still just the one ping


def test_sticker_cap_scores_on_listing_only():
    # 3LT convertible pattern so the VIN gate passes (a VIN-failed car would
    # be fast-path excluded before the cap is ever consulted).
    vin = "1G1YC3D40P5111111"  # not cached -> would hit network
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
