"""Scheduler wiring + runner profile loading + pipeline notify — offline."""

from __future__ import annotations

import httpx
import pytest

from carfinder import db
from carfinder.budget import GM_STICKER, BudgetGuard, ProviderBudget
from carfinder.notify import Notifier
from carfinder.pipeline import Pipeline, build_pipeline
from carfinder.runner import load_profiles
from carfinder.scheduler import build_scheduler
from carfinder.sources import ListingRow, Page
from carfinder.stickers import StickerFetcher
from conftest import ROOT

# --- runner / factory ------------------------------------------------------


def test_load_profiles_reads_config_dir():
    profiles = load_profiles(ROOT / "config")
    keys = {p.model_key for p in profiles}
    assert {"c8_stingray", "ct4v_blackwing"} <= keys


def test_build_pipeline_requires_autodev_key(seeded_settings):
    conn = db.bootstrap(":memory:")
    with pytest.raises(ValueError, match="AUTODEV_API_KEY"):
        build_pipeline(conn, seeded_settings, 2026)


# --- scheduler -------------------------------------------------------------


def test_scheduler_has_two_jobs(seeded_settings):
    sched = build_scheduler(seeded_settings)  # not started
    ids = {j.id for j in sched.get_jobs()}
    assert ids == {"incremental_sync", "weekly_full_pass"}


def test_scheduler_cron_days(seeded_settings):
    sched = build_scheduler(seeded_settings)
    inc = sched.get_job("incremental_sync")
    full = sched.get_job("weekly_full_pass")
    assert "mon" in str(inc.trigger) and "fri" in str(inc.trigger)
    assert "sun" in str(full.trigger)


# --- pipeline notify (webhook wiring) --------------------------------------


class _Fake:
    name = "autodev"

    def __init__(self, rows):
        self.rows = rows

    def search_page(self, params, page=1, sort="createdAt.desc", run_id=None):
        return Page(rows=self.rows if page == 1 else [], total=len(self.rows), has_next=False)


def _pipeline_with_notifier(conn, rows, handler, threshold=85.0):
    guard = BudgetGuard(conn, {GM_STICKER: ProviderBudget(GM_STICKER, "count", 100)})
    miss = b'{"errorCode":1001,"errorMessage":"none"}'
    fetcher = StickerFetcher(
        conn,
        guard,
        stickers_dir=ROOT / "stickers",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=miss))
        ),
        min_interval_s=0.0,
        sleep=lambda s: None,
    )
    notifier = Notifier(
        "https://ntfy.sh/t", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    return Pipeline(conn, _Fake(rows), fetcher, 2026, notifier=notifier, notify_threshold=threshold)


def test_notifies_on_new_high_scoring_ranked_car():
    from carfinder.profiles import load_profile

    sent = []
    conn = db.bootstrap(":memory:")
    # BW cached sticker VIN, fully-optioned -> ranked, high total.
    rows = [
        ListingRow(
            vin="1G6D65RP8N0460286",
            source="autodev",
            price=58000,
            miles=20000,
            year=2022,
            make="Cadillac",
            model="CT4-V",
            trim="Blackwing",
        )
    ]
    pipe = _pipeline_with_notifier(conn, rows, lambda r: (sent.append(1), httpx.Response(200))[1])
    bw = load_profile(ROOT / "config" / "ct4v_blackwing.yaml")
    stats = pipe.run(bw, run_id="r1")
    assert stats.ranked == 1
    assert stats.notified == 1 and len(sent) == 1

    # Re-run (price change) — already-qualifying car must NOT re-notify.
    pipe._engine._source.rows = [
        ListingRow(
            vin="1G6D65RP8N0460286",
            source="autodev",
            price=57000,
            miles=20000,
            year=2022,
            make="Cadillac",
            model="CT4-V",
            trim="Blackwing",
        )
    ]
    stats2 = pipe.run(bw, run_id="r2")
    assert stats2.notified == 0 and len(sent) == 1
