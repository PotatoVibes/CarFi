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


def test_ui_read_indexes_exist():
    """Migration 12: board/detail queries must be index lookups, not scans of
    listings (whose raw_json payloads make a scan cost tens of MB of I/O)."""
    conn = _mem()
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_match_results_model_verdict", "idx_listings_vin"} <= names
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT url FROM listings WHERE vin = ?", ("X",)
    ).fetchall()
    assert any("idx_listings_vin" in r[-1] for r in plan)


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


def test_migration_10_backfills_dealer_from_raw_json():
    """Existing rows (schema v9) with retailListing.dealer in raw_json get the
    vehicles.dealer column populated by the migration itself."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # Bring the schema up to v9 only, then seed pre-migration data.
    conn.executescript(db._SCHEMA_VERSION_DDL)
    for version, sql in db.MIGRATIONS:
        if version > 9:
            break
        conn.executescript(sql)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    conn.execute(
        """INSERT INTO listings (source, vin, raw_json, first_seen, last_seen)
           VALUES ('autodev', 'VIN1', '{"retailListing.dealer":"Van Hyundai"}',
                   '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')"""
    )
    conn.execute(
        """INSERT INTO listings (source, vin, raw_json, first_seen, last_seen)
           VALUES ('autodev', 'VIN2', '{"retailListing": {"dealer": "Carvana"}}',
                   '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')"""
    )
    for vin in ("VIN1", "VIN2", "VIN3"):
        conn.execute(
            """INSERT INTO vehicles (vin, first_seen, last_seen)
               VALUES (?, '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')""",
            (vin,),
        )
    conn.commit()
    assert db.migrate(conn) == db.LATEST_VERSION
    got = {
        r["vin"]: r["dealer"] for r in conn.execute("SELECT vin, dealer FROM vehicles").fetchall()
    }
    assert got == {"VIN1": "Van Hyundai", "VIN2": "Carvana", "VIN3": None}
