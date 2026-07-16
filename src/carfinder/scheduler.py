"""APScheduler jobs: Mon/Wed/Fri incremental sync + a weekly full pass.

Started by `carfinder serve` (not by create_app), so the FastAPI TestClient in
the test suite never spins up a live scheduler.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Settings
from .db import bootstrap
from .runner import run_sync

log = logging.getLogger("carfinder.scheduler")


def _sync_job(settings: Settings, mode: str) -> None:
    conn = bootstrap(settings.db_path)
    try:
        results = run_sync(
            conn, settings, datetime.now(UTC).year, mode=mode, config_dir=settings.config_dir
        )
        total_new = sum(s.sync.new_vins for s in results.values())
        log.info("scheduled %s sync done: %d new across %d models", mode, total_new, len(results))
    except Exception:  # a scheduler job must never crash the process
        log.exception("scheduled %s sync failed", mode)
    finally:
        conn.close()


def build_scheduler(settings: Settings) -> BackgroundScheduler:
    """Return a (not-yet-started) scheduler with the two sync jobs."""
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(
        _sync_job,
        CronTrigger(day_of_week="mon,wed,fri", hour=13, minute=0),
        args=[settings, "incremental"],
        id="incremental_sync",
        replace_existing=True,
    )
    sched.add_job(
        _sync_job,
        CronTrigger(day_of_week="sun", hour=12, minute=0),
        args=[settings, "full"],
        id="weekly_full_pass",
        replace_existing=True,
    )
    return sched
