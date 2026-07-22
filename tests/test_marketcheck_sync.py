"""Marketcheck-as-secondary-source pipeline tests. Fully offline.

Covers the integration contract: full passes walk Marketcheck before Auto.dev
(primary wins last-writer-wins on shared vehicles fields), VINs from either
feed score identically, delisting runs once on the union of both feeds' seen
sets, a Marketcheck failure never sinks the run (but does veto delisting), and
incremental runs skip Marketcheck entirely.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx

from carfinder import db
from carfinder.budget import GM_STICKER, BudgetGuard, ProviderBudget
from carfinder.pipeline import Pipeline
from carfinder.sources import ListingRow, Page
from carfinder.stickers import StickerFetcher

ROOT = Path(__file__).resolve().parents[1]
STICKERS = ROOT / "stickers"
GM_MISS = b'{"errorCode":1001,"errorMessage":"No Window Sticker found for the requested VIN."}'


class Clock:
    def __init__(self):
        self.t = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)

    def __call__(self):
        self.t = self.t.replace(second=(self.t.second + 1) % 60)
        return self.t


class FakeSource:
    """Single-page primary feed; counts search calls."""

    name = "autodev"

    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def search_page(self, params, page=1, sort="createdAt.desc", run_id=None):
        self.calls += 1
        rows = self.rows if page == 1 else []
        return Page(rows=rows, total=len(self.rows), has_next=False)


class FakeMarketcheck(FakeSource):
    name = "marketcheck"


class FailingMarketcheck:
    name = "marketcheck"

    def __init__(self):
        self.calls = 0

    def search_page(self, params, page=1, sort="createdAt.desc", run_id=None):
        self.calls += 1
        req = httpx.Request("GET", "https://api.example/search?api_key=SECRET")
        raise httpx.HTTPStatusError(
            "boom api_key=SECRET", request=req, response=httpx.Response(500, request=req)
        )


def _profile(model_key="c8_stingray"):
    """Minimal profile: no year bounds -> single unsegmented walk per source,
    no constraints -> every car ranks. Keeps these tests about SYNC mechanics,
    not scoring."""
    from carfinder.models import Profile

    return Profile(
        name="Test",
        model_key=model_key,
        query={"make": "Chevrolet", "model": "Corvette"},
    )


def _pipeline(primary_rows, mc_source):
    conn = db.bootstrap(":memory:")
    guard = BudgetGuard(conn, {GM_STICKER: ProviderBudget(GM_STICKER, "count", 100)})
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=GM_MISS))
    )
    fetcher = StickerFetcher(
        conn, guard, stickers_dir=STICKERS, client=client, min_interval_s=0.0, sleep=lambda s: None
    )
    primary = FakeSource(primary_rows)
    pipe = Pipeline(
        conn,
        primary,
        fetcher,
        current_year=2026,
        now=Clock(),
        secondary_source=mc_source,
    )
    return pipe, conn, primary


def _row(vin, source, **kw):
    return ListingRow(vin=vin, source=source, **kw)


A_VIN = "1G1YZZZZZZ5000001"  # in both feeds
B_VIN = "1G1YZZZZZZ5000002"  # marketcheck-only
C_VIN = "1G1YZZZZZZ5000003"  # autodev-only


def _seed_active(conn, vin, missing_passes=0):
    conn.execute(
        """INSERT INTO vehicles (vin, model_key, status, missing_passes, first_seen, last_seen)
           VALUES (?, 'c8_stingray', 'active', ?, '2026-07-01T00:00:00Z',
                   '2026-07-01T00:00:00Z')""",
        (vin, missing_passes),
    )
    conn.commit()


# --- ingestion + merge -------------------------------------------------------


def test_full_pass_ingests_marketcheck_only_vins():
    mc = FakeMarketcheck([_row(B_VIN, "marketcheck", price=61000, miles=9000, year=2023)])
    pipe, conn, _ = _pipeline([_row(C_VIN, "autodev", price=62000, miles=8000, year=2023)], mc)
    stats = pipe.run(_profile(), mode="full", run_id="r1")

    # Both feeds' cars landed, in listings (per-source) and vehicles (merged).
    sources = {
        r["source"]: r["vin"] for r in conn.execute("SELECT source, vin FROM listings").fetchall()
    }
    assert sources == {"marketcheck": B_VIN, "autodev": C_VIN}
    vins = {r["vin"] for r in conn.execute("SELECT vin FROM vehicles").fetchall()}
    assert vins == {B_VIN, C_VIN}
    # The marketcheck-only VIN went through the same scoring chain.
    assert stats.scored == 2
    assert conn.execute("SELECT 1 FROM match_results WHERE vin=?", (B_VIN,)).fetchone()
    assert stats.mc_sync is not None and stats.mc_sync.new_vins == 1
    assert stats.mc_failed is False


def test_shared_vin_merges_to_one_vehicle_with_primary_fields_and_best_price():
    # Same car on both feeds: marketcheck is cheaper but claims different miles.
    mc = FakeMarketcheck(
        [_row(A_VIN, "marketcheck", price=59000, miles=12500, year=2023, color="Red")]
    )
    pipe, conn, _ = _pipeline(
        [_row(A_VIN, "autodev", price=60000, miles=12000, year=2023, color="Rapid Blue")], mc
    )
    pipe.run(_profile(), mode="full", run_id="r1")

    v = conn.execute("SELECT * FROM vehicles WHERE vin=?", (A_VIN,)).fetchone()
    assert v is not None  # one merged row, not two
    # Marketcheck walks FIRST, so the primary's write lands last on shared
    # descriptive fields...
    assert v["miles"] == 12000
    assert v["color"] == "Rapid Blue"
    # ...while price is the best (lowest) across sources, not last-writer.
    assert v["price"] == 59000
    # Both feeds' price observations recorded.
    assert (
        conn.execute("SELECT COUNT(*) FROM price_history WHERE vin=?", (A_VIN,)).fetchone()[0] == 2
    )


# --- delist union ------------------------------------------------------------


def test_delist_runs_on_union_of_both_feeds():
    mc = FakeMarketcheck([_row(B_VIN, "marketcheck", price=61000, year=2023)])
    pipe, conn, _ = _pipeline([_row(C_VIN, "autodev", price=62000, year=2023)], mc)
    # B is marketcheck-only and one miss from delisting: the union must save it.
    _seed_active(conn, B_VIN, missing_passes=1)
    # X is in neither feed and one miss away: this pass must delist it.
    x = "1G1YZZZZZZ5000009"
    _seed_active(conn, x, missing_passes=1)

    stats = pipe.run(_profile(), mode="full", run_id="r1")

    rows = {
        r["vin"]: (r["status"], r["missing_passes"])
        for r in conn.execute("SELECT vin, status, missing_passes FROM vehicles").fetchall()
    }
    assert rows[B_VIN] == ("active", 0)  # seen by marketcheck -> reset, alive
    assert rows[C_VIN] == ("active", 0)  # seen by autodev
    assert rows[x] == ("presumed_sold", 2)  # absent from BOTH -> gone
    assert x in stats.sync.presumed_sold


def test_marketcheck_failure_survives_run_but_vetoes_delist():
    mc = FailingMarketcheck()
    pipe, conn, _ = _pipeline([_row(C_VIN, "autodev", price=62000, year=2023)], mc)
    # One miss from delisting; with marketcheck dead, absence proves nothing.
    x = "1G1YZZZZZZ5000009"
    _seed_active(conn, x, missing_passes=1)

    stats = pipe.run(_profile(), mode="full", run_id="r1")

    assert mc.calls == 1  # it tried
    assert stats.mc_failed is True
    assert stats.scored == 1  # primary work still landed
    row = conn.execute("SELECT status, missing_passes FROM vehicles WHERE vin=?", (x,)).fetchone()
    assert row["status"] == "active" and row["missing_passes"] == 1  # untouched
    assert stats.sync.presumed_sold == []


# --- mode gating ---------------------------------------------------------------


def test_incremental_walks_marketcheck_once():
    """dos_active-asc is a stable newest-first order, so Marketcheck joins
    incrementals: one cheap early-stopping walk, no segmentation, no sweep."""
    mc = FakeMarketcheck([_row(B_VIN, "marketcheck", price=61000, year=2023)])
    pipe, conn, _ = _pipeline([_row(C_VIN, "autodev", price=62000, year=2023)], mc)
    stats = pipe.run(_profile(), mode="incremental", run_id="r1")

    assert mc.calls == 1  # a single unsegmented walk
    assert stats.mc_sync is not None and stats.mc_sync.new_vins == 1
    assert stats.scored == 2  # the marketcheck VIN scored in the same run
    assert (
        conn.execute("SELECT COUNT(*) FROM listings WHERE source='marketcheck'").fetchone()[0] == 1
    )


def test_incremental_marketcheck_failure_survives():
    mc = FailingMarketcheck()
    pipe, conn, _ = _pipeline([_row(C_VIN, "autodev", price=62000, year=2023)], mc)
    stats = pipe.run(_profile(), mode="incremental", run_id="r1")

    assert stats.mc_failed is True
    assert stats.mc_sync is None
    assert stats.scored == 1  # primary work unaffected


def test_no_secondary_source_full_pass_unchanged():
    pipe, conn, _ = _pipeline([_row(C_VIN, "autodev", price=62000, year=2023)], None)
    stats = pipe.run(_profile(), mode="full", run_id="r1")
    assert stats.mc_sync is None and stats.mc_failed is False
    assert stats.scored == 1


# --- year segmentation format ---------------------------------------------------


class ParamRecordingMarketcheck(FakeSource):
    name = "marketcheck"

    def __init__(self, rows):
        super().__init__(rows)
        self.seen_params: list[dict] = []

    def search_page(self, params, page=1, sort="createdAt.desc", run_id=None):
        self.seen_params.append(dict(params))
        return super().search_page(params, page=page, sort=sort, run_id=run_id)


def test_marketcheck_full_segments_send_single_year_not_ranges():
    """Marketcheck's `year` param reads "2022-2030" as just 2022 (verified
    live) — every full-pass segment must carry a bare single `year` and drop
    the internal year_range."""
    from carfinder.models import Profile

    profile = Profile(
        name="Test",
        model_key="c8_stingray",
        query={"make": "Chevrolet", "model": "Corvette", "year_min": 2022, "year_max": 2024},
    )
    mc = ParamRecordingMarketcheck([_row(B_VIN, "marketcheck", price=61000, year=2023)])
    pipe, conn, _ = _pipeline([_row(C_VIN, "autodev", price=62000, year=2023)], mc)
    pipe.run(profile, mode="full", run_id="r1")

    # One walk per year, each a bare year value — never "lo-hi".
    assert [p.get("year") for p in mc.seen_params] == ["2022", "2023", "2024"]
    assert all("year_range" not in p for p in mc.seen_params)


def test_marketcheck_full_runs_noprice_sweep_when_price_bounded():
    """A price bound silently excludes "call for price" listings, so a full
    pass adds one has_price=false sweep (sans price_range) to catch them."""
    from carfinder.models import Profile

    profile = Profile(
        name="Test",
        model_key="c8_stingray",
        query={"make": "Chevrolet", "model": "Corvette", "price_max": 77000},
    )
    mc = ParamRecordingMarketcheck([_row(B_VIN, "marketcheck", price=61000, year=2023)])
    pipe, conn, _ = _pipeline([_row(C_VIN, "autodev", price=62000, year=2023)], mc)
    pipe.run(profile, mode="full", run_id="r1")

    assert len(mc.seen_params) == 2  # priced walk + unpriced sweep
    priced, sweep = mc.seen_params
    assert priced["price_range"] == "1-77000" and "has_price" not in priced
    assert sweep["has_price"] == "false" and "price_range" not in sweep


def test_marketcheck_incremental_sends_year_range_unsegmented():
    """Incrementals stop early, so one ranged query (year_range, the real
    range param) replaces per-year segmentation."""
    from carfinder.models import Profile

    profile = Profile(
        name="Test",
        model_key="c8_stingray",
        query={"make": "Chevrolet", "model": "Corvette", "year_min": 2022, "year_max": 2024},
    )
    mc = ParamRecordingMarketcheck([_row(B_VIN, "marketcheck", price=61000, year=2023)])
    pipe, conn, _ = _pipeline([_row(C_VIN, "autodev", price=62000, year=2023)], mc)
    pipe.run(profile, mode="incremental", run_id="r1")

    assert len(mc.seen_params) == 1
    assert mc.seen_params[0]["year_range"] == "2022-2024"
    assert "year" not in mc.seen_params[0]


# --- cross-source conflicts ----------------------------------------------------


def test_listing_vs_listing_conflict_logged():
    # The two feeds disagree on year AND color family for the same car.
    mc = FakeMarketcheck([_row(A_VIN, "marketcheck", price=59000, year=2022, color="Red")])
    pipe, conn, _ = _pipeline(
        [_row(A_VIN, "autodev", price=60000, year=2023, color="Rapid Blue")], mc
    )
    pipe.run(_profile(), mode="full", run_id="r1")

    conflicts = {
        r["field"]: r
        for r in conn.execute("SELECT * FROM conflicts WHERE vin=?", (A_VIN,)).fetchall()
    }
    assert "year" in conflicts and "exterior" in conflicts
    # No sticker for this VIN, so the primary feed wins the tie...
    assert conflicts["year"]["winner"] == "listing:autodev"
    # ...and both feeds' values are recorded for the UI.
    import json

    values = dict(json.loads(conflicts["year"]["values_json"]))
    assert values == {"listing:autodev": 2023, "listing:marketcheck": 2022}
