"""Incremental + full-pass sync tests — scripted fake source, offline."""

from __future__ import annotations

from datetime import UTC, datetime

from carfinder import db
from carfinder.sources import ListingRow, Page
from carfinder.sync import SyncEngine

PARAMS = {"vehicle.make": "Chevrolet"}


def _row(vin, price, miles=20000, year=2023):
    return ListingRow(
        vin=vin,
        source="autodev",
        price=price,
        miles=miles,
        year=year,
        make="Chevrolet",
        model="Corvette",
        trim="Stingray",
    )


class FakeSource:
    """Serves scripted pages; each sync() call re-reads self.pages from page 1."""

    name = "autodev"

    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def search_page(self, params, page=1, sort="createdAt.desc", run_id=None):
        self.calls += 1
        idx = page - 1
        rows = self.pages[idx] if 0 <= idx < len(self.pages) else []
        return Page(rows=rows, total=None, has_next=idx + 1 < len(self.pages))


class Clock:
    def __init__(self):
        self.t = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)

    def __call__(self):
        self.t = self.t.replace(second=(self.t.second + 1) % 60)
        return self.t


def _engine(pages):
    conn = db.bootstrap(":memory:")
    src = FakeSource(pages)
    return SyncEngine(conn, src, now=Clock()), conn, src


# --- incremental ingest ----------------------------------------------------


def test_incremental_ingests_new_vins():
    eng, conn, _ = _engine([[_row("A", 60000), _row("B", 61000)]])
    stats = eng.sync(PARAMS, "c8_stingray", run_id="r1")
    assert stats.new_vins == 2
    assert set(stats.seen_vins) == {"A", "B"}
    listings = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    vehicles = conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]
    prices = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    assert listings == 2 and vehicles == 2 and prices == 2


def test_reingest_same_vin_no_duplicate_and_no_extra_price_row():
    pages = [[_row("A", 60000)]]
    eng, conn, _ = _engine(pages)
    eng.sync(PARAMS, "c8_stingray", run_id="r1")
    eng.sync(PARAMS, "c8_stingray", run_id="r2")  # unchanged
    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 1


# --- incremental early stop ------------------------------------------------


def test_incremental_stops_on_full_page_of_known_unchanged():
    # Page 1 new; page 2 all-known-unchanged -> stop before page 3 exists.
    p1 = [_row("A", 60000), _row("B", 61000)]
    p2 = [_row("A", 60000), _row("B", 61000)]  # same, known, unchanged
    p3 = [_row("C", 62000)]  # should never be reached
    eng, conn, src = _engine([p1, p2, p3])
    # First run ingests everything so p1/p2 VINs are known.
    eng.sync(PARAMS, "c8_stingray", run_id="warm")
    src.calls = 0
    stats = eng.sync(PARAMS, "c8_stingray", run_id="r2")
    assert stats.stopped_early is True
    # Fetched page 1 (all known/unchanged) and stopped immediately.
    assert stats.pages_fetched == 1
    assert src.calls == 1


def test_price_change_prevents_early_stop_and_appends_history():
    eng, conn, src = _engine([[_row("A", 60000)]])
    eng.sync(PARAMS, "c8_stingray", run_id="r1")
    src.pages = [[_row("A", 57000)]]  # price drop
    stats = eng.sync(PARAMS, "c8_stingray", run_id="r2")
    assert stats.price_changes == 1 and stats.updated_vins == 1
    assert stats.stopped_early is False
    hist = conn.execute("SELECT price FROM price_history WHERE vin='A' ORDER BY id").fetchall()
    assert [h[0] for h in hist] == [60000, 57000]
    veh_price = conn.execute("SELECT price FROM vehicles WHERE vin='A'").fetchone()[0]
    assert veh_price == 57000  # best/lowest


# --- full pass + delist ----------------------------------------------------


def test_full_pass_walks_all_pages():
    p1 = [_row("A", 1), _row("B", 2)]
    p2 = [_row("C", 3)]
    eng, conn, _ = _engine([p1, p2])
    stats = eng.sync(PARAMS, "c8_stingray", mode="full", run_id="r1")
    assert stats.pages_fetched == 2
    assert set(stats.seen_vins) == {"A", "B", "C"}


def test_delist_after_two_missing_full_passes():
    eng, conn, src = _engine([[_row("A", 60000), _row("B", 61000)]])
    eng.sync(PARAMS, "c8_stingray", mode="full", run_id="r1")  # A,B active

    # B disappears. Pass 1 missing: not yet sold.
    src.pages = [[_row("A", 60000)]]
    s2 = eng.sync(PARAMS, "c8_stingray", mode="full", run_id="r2")
    assert s2.presumed_sold == []
    assert conn.execute("SELECT missing_passes FROM vehicles WHERE vin='B'").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM vehicles WHERE vin='B'").fetchone()[0] == "active"

    # Pass 2 still missing: presumed sold.
    s3 = eng.sync(PARAMS, "c8_stingray", mode="full", run_id="r3")
    assert s3.presumed_sold == ["B"]
    assert (
        conn.execute("SELECT status FROM vehicles WHERE vin='B'").fetchone()[0] == "presumed_sold"
    )


def test_reappearing_vin_resets_missing_passes():
    eng, conn, src = _engine([[_row("A", 1), _row("B", 2)]])
    eng.sync(PARAMS, "c8_stingray", mode="full", run_id="r1")
    src.pages = [[_row("A", 1)]]  # B missing once
    eng.sync(PARAMS, "c8_stingray", mode="full", run_id="r2")
    assert conn.execute("SELECT missing_passes FROM vehicles WHERE vin='B'").fetchone()[0] == 1
    src.pages = [[_row("A", 1), _row("B", 2)]]  # B returns
    eng.sync(PARAMS, "c8_stingray", mode="full", run_id="r3")
    assert conn.execute("SELECT missing_passes FROM vehicles WHERE vin='B'").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM vehicles WHERE vin='B'").fetchone()[0] == "active"


def test_progress_reported_per_page():
    p1 = [_row("A", 60000)]
    p2 = [_row("B", 61000)]
    eng, _, _ = _engine([p1, p2])
    seen: list[str] = []
    eng.sync(PARAMS, "c8_stingray", mode="full", run_id="r1", progress=seen.append)
    assert len(seen) == 2  # one message before each page fetch
    assert "page 1" in seen[0] and "page 2" in seen[1]


def test_sync_run_recorded():
    eng, conn, _ = _engine([[_row("A", 60000)]])
    eng.sync(PARAMS, "c8_stingray", run_id="r1")
    run = conn.execute("SELECT * FROM sync_runs WHERE run_id='r1'").fetchone()
    assert run["mode"] == "incremental" and run["new_vins"] == 1
    assert run["finished_at"] is not None and run["calls"] == 1
