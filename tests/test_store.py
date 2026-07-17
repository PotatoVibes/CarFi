"""Read/query layer tests — offline, seeded DB."""

from __future__ import annotations

from carfinder import db, store
from conftest import BW_VIN, C8_VIN, REVIEW_VIN


def _conn(settings):
    return db.bootstrap(str(settings.db_path))


def test_ranked_matches_orders_and_hides_excluded(seeded_settings):
    conn = _conn(seeded_settings)
    rows = store.ranked_matches(conn, "c8_stingray")
    vins = [r.vin for r in rows]
    assert C8_VIN not in vins  # excluded hidden
    assert REVIEW_VIN in vins
    rows_all = store.ranked_matches(conn, "c8_stingray", include_excluded=True)
    # ranked/needs_review sort before excluded regardless of total.
    assert rows_all[-1].verdict == "excluded"


def test_ranked_matches_new_flag(seeded_settings):
    conn = _conn(seeded_settings)
    rows = store.ranked_matches(conn, "c8_stingray", since="2026-07-16T00:00:00Z")
    review = next(r for r in rows if r.vin == REVIEW_VIN)
    assert review.is_new is True  # first_seen 2026-07-20 > since


def test_match_counts(seeded_settings):
    conn = _conn(seeded_settings)
    counts = store.match_counts(conn, "c8_stingray")
    assert counts == {"ranked": 0, "needs_review": 1, "excluded": 1, "dismissed": 0}
    # Dismissing a car moves it out of its verdict bucket into 'dismissed'.
    conn.execute(
        "UPDATE vehicles SET dismissed_at='2026-07-16T00:00:00Z' WHERE vin=?", (REVIEW_VIN,)
    )
    conn.commit()
    counts = store.match_counts(conn, "c8_stingray")
    assert counts == {"ranked": 0, "needs_review": 0, "excluded": 1, "dismissed": 1}


def test_vehicle_detail_assembles_everything(seeded_settings):
    conn = _conn(seeded_settings)
    det = store.vehicle_detail(conn, BW_VIN)
    assert det is not None
    assert det.match["verdict"] == "ranked"
    assert det.breakdown["hard"][0]["key"] == "z51"
    assert len(det.price_history) == 2
    assert det.price_history[0]["price"] == 60000


def test_vehicle_detail_conflicts(seeded_settings):
    conn = _conn(seeded_settings)
    det = store.vehicle_detail(conn, C8_VIN)
    assert det.conflicts and det.conflicts[0]["field"] == "year"
    assert det.conflicts[0]["winner"] == "sticker"


def test_detail_ignores_junk_fragment_urls_already_stored(seeded_settings):
    # Rows ingested before the ingest-side fix can hold fragment "urls" —
    # the read side must skip them rather than render a self-anchor link.
    conn = _conn(seeded_settings)
    conn.execute(
        """INSERT INTO listings (source, vin, price, url, raw_json, first_seen, last_seen)
           VALUES ('autodev', ?, 61000, '#-4976296983083547062', '{}',
                   '2026-07-01T00:00:00Z', '2026-07-20T00:00:00Z')""",
        (REVIEW_VIN,),
    )
    conn.commit()
    det = store.vehicle_detail(conn, REVIEW_VIN)
    assert det.listing_url is None  # junk skipped, no link rendered
    # The BW car's genuine URL still comes through.
    assert store.vehicle_detail(conn, BW_VIN).listing_url.startswith("https://")


def test_all_conflicts_hides_excluded_cars_by_default(seeded_settings):
    # The seeded conflict is on the excluded C8 — a car that already
    # hard-failed. Its conflict is noise, hidden unless explicitly requested.
    conn = _conn(seeded_settings)
    assert store.all_conflicts(conn) == []
    items = store.all_conflicts(conn, include_excluded=True)
    assert len(items) == 1 and items[0]["vin"] == C8_VIN
    assert items[0]["verdict"] == "excluded"


def test_vehicle_detail_unknown(seeded_settings):
    conn = _conn(seeded_settings)
    assert store.vehicle_detail(conn, "NOPE") is None
