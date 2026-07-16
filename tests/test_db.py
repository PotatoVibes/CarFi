"""Ledger and migration tests — fully offline."""

from __future__ import annotations

from datetime import UTC, datetime

from carfinder import db


def _mem():
    return db.bootstrap(":memory:")


def test_migrate_sets_latest_version_and_is_idempotent():
    conn = _mem()
    assert db.migrate(conn) == db.LATEST_VERSION
    # Running again applies nothing new and keeps the same version.
    assert db.migrate(conn) == db.LATEST_VERSION
    rows = conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    assert [r[0] for r in rows] == [v for v, _ in db.MIGRATIONS]


def test_api_calls_table_exists():
    conn = _mem()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(api_calls)")}
    assert {"provider", "endpoint", "ts", "run_id", "cost_usd"} <= cols


def test_record_and_count_within_month():
    conn = _mem()
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    db.record_call(conn, "autodev", "search", now, run_id="r1")
    db.record_call(conn, "autodev", "listing", now, run_id="r1")
    start, end = datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)
    assert db.count_calls(conn, "autodev", start, end) == 2


def test_count_and_cost_scoped_to_range():
    conn = _mem()
    db.record_call(conn, "anthropic", "extract", datetime(2026, 6, 30, tzinfo=UTC), cost_usd=0.5)
    db.record_call(conn, "anthropic", "extract", datetime(2026, 7, 2, tzinfo=UTC), cost_usd=0.75)
    jul_start, jul_end = datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)
    assert db.count_calls(conn, "anthropic", jul_start, jul_end) == 1
    assert db.sum_cost(conn, "anthropic", jul_start, jul_end) == 0.75
