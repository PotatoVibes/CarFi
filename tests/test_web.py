"""FastAPI UI tests via TestClient — offline, seeded DB."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from carfinder.web import create_app
from conftest import BW_VIN, C8_VIN, REVIEW_VIN


def _client(settings):
    return TestClient(create_app(settings))


def test_health(seeded_settings):
    assert _client(seeded_settings).get("/health").json() == {"status": "ok"}


def test_index_lists_models_with_counts(seeded_settings):
    r = _client(seeded_settings).get("/")
    assert r.status_code == 200
    assert "C8 Corvette Stingray" in r.text
    assert "Cadillac CT4-V Blackwing" in r.text


def test_matches_hides_excluded_by_default(seeded_settings):
    c = _client(seeded_settings)
    r = c.get("/m/c8_stingray")
    assert r.status_code == 200
    assert REVIEW_VIN in r.text  # needs_review shown
    assert C8_VIN not in r.text  # excluded hidden
    r2 = c.get("/m/c8_stingray?show_excluded=true")
    assert C8_VIN in r2.text  # now shown


def test_matches_ranked_appears(seeded_settings):
    r = _client(seeded_settings).get("/m/ct4v_blackwing")
    assert BW_VIN in r.text
    assert "ranked" in r.text


def test_new_since_last_visit_cookie(seeded_settings):
    c = _client(seeded_settings)
    # First visit: no cookie -> REVIEW_VIN (first_seen 2026-07-20) not flagged new
    # (no baseline). Second visit sets a cookie in the past-relative sense.
    r1 = c.get("/m/c8_stingray")
    assert "seen_c8_stingray" in r1.cookies
    # A visit with an old cookie flags the newer car as "new".
    c.cookies.set("seen_c8_stingray", "2026-07-16T00:00:00Z")
    r2 = c.get("/m/c8_stingray")
    assert ">new<" in r2.text or "badge new" in r2.text


def test_detail_shows_breakdown_and_sticker_link(seeded_settings):
    r = _client(seeded_settings).get(f"/v/{BW_VIN}")
    assert r.status_code == 200
    assert "Z51 Performance Package" in r.text
    assert "verified" in r.text
    assert f"/sticker/{BW_VIN}" in r.text  # committed PDF exists on disk


def test_detail_unknown_vin_404(seeded_settings):
    assert _client(seeded_settings).get("/v/NOPE").status_code == 404


def test_sticker_pdf_served_from_disk(seeded_settings):
    r = _client(seeded_settings).get(f"/sticker/{BW_VIN}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:4] == b"%PDF"


def test_sticker_missing_404(seeded_settings):
    assert _client(seeded_settings).get("/sticker/NOSUCHVIN").status_code == 404


def test_budget_page(seeded_settings):
    r = _client(seeded_settings).get("/budget")
    assert r.status_code == 200
    assert "autodev" in r.text and "anthropic" in r.text


def test_conflicts_page_hides_excluded_by_default(seeded_settings):
    c = _client(seeded_settings)
    r = c.get("/conflicts")
    assert r.status_code == 200
    assert C8_VIN not in r.text  # excluded car's conflict is noise
    r2 = c.get("/conflicts?show_excluded=true")
    assert C8_VIN in r2.text  # available on request


def test_matches_table_is_sortable_and_filterable(seeded_settings):
    r = _client(seeded_settings).get("/m/c8_stingray")
    assert 'data-key="price"' in r.text  # sortable headers
    assert 'id="q"' in r.text  # filter box
    assert f'data-vin="{REVIEW_VIN}"' in r.text  # row data attrs for sort/filter


def test_matches_rows_show_vin_with_google_link(seeded_settings):
    r = _client(seeded_settings).get("/m/c8_stingray")
    assert f"https://www.google.com/search?q={REVIEW_VIN}" in r.text
    assert 'data-key="vin"' in r.text  # sortable VIN column


def test_detail_links_to_dealer_listing(seeded_settings):
    r = _client(seeded_settings).get(f"/v/{BW_VIN}")
    assert "https://dealer.example/bw-listing" in r.text


def test_detail_vin_links_to_google_search(seeded_settings):
    r = _client(seeded_settings).get(f"/v/{BW_VIN}")
    assert f"https://www.google.com/search?q={BW_VIN}" in r.text


def test_detail_shows_seller_location_and_distance(seeded_settings):
    r = _client(seeded_settings).get(f"/v/{BW_VIN}").text
    assert "Example Motors of Austin" in r
    assert "Austin, TX" in r and "78701" in r
    assert "mi from 77498" in r  # ~130 mi, rendered with the home zip


def test_detail_without_location_says_unknown(seeded_settings):
    r = _client(seeded_settings).get(f"/v/{REVIEW_VIN}").text
    assert "location unknown" in r
    assert "unknown (private sale or unlisted dealer)" in r


def test_matches_color_uses_scorer_resolved_family(seeded_settings):
    r = _client(seeded_settings).get("/m/ct4v_blackwing").text
    # Generic family label, matching the detail page's "Black (Black Raven)".
    assert 'data-color="black black raven"' in r
    assert 'title="Black Raven"' in r  # specific paint name on hover
    # A row never (re)scored with the new columns falls back to listing text.
    r2 = _client(seeded_settings).get("/m/c8_stingray").text
    assert "Rapid Blue" in r2


def test_unicorn_icon_marks_qualifying_cars_only(seeded_settings):
    from carfinder import db

    # Seeded BW car: ranked, bonus 8 >= 5, 30k miles under the cap — but BLACK,
    # a penalized color, so no unicorn.
    assert "🦄" not in _client(seeded_settings).get("/m/ct4v_blackwing").text
    # Repaint it blue (a preferred color): now it clears the whole unicorn bar.
    conn = db.bootstrap(str(seeded_settings.db_path))
    conn.execute("UPDATE match_results SET color_family='blue' WHERE vin=?", (BW_VIN,))
    conn.commit()
    conn.close()
    assert "🦄" in _client(seeded_settings).get("/m/ct4v_blackwing").text


def test_favicon_is_declared(seeded_settings):
    r = _client(seeded_settings).get("/").text
    assert '<link rel="icon" href="data:image/svg+xml' in r


def test_detail_and_matches_chip_agree(seeded_settings):
    from carfinder.colors import FAMILY_HEX

    matches = _client(seeded_settings).get("/m/ct4v_blackwing").text
    detail = _client(seeded_settings).get(f"/v/{BW_VIN}").text
    # Both pages color the chip from the resolved family (black), even though
    # a listing feed could claim something else.
    assert FAMILY_HEX["black"] in matches
    assert FAMILY_HEX["black"] in detail


def test_matches_rows_show_location_and_distance(seeded_settings):
    r = _client(seeded_settings).get("/m/ct4v_blackwing").text
    assert 'data-key="dist"' in r  # sortable Location column
    assert "Austin, TX" in r
    assert 'data-dist="99999"' not in r  # the seeded BW has a real distance
    # Cars without a zip sink to the bottom of a distance sort.
    r2 = _client(seeded_settings).get("/m/c8_stingray").text
    assert 'data-dist="99999"' in r2


def test_sync_now_without_key_flashes(seeded_settings):
    r = _client(seeded_settings).post("/sync", follow_redirects=False)
    assert r.status_code == 303
    assert "flash=" in r.headers["location"]
    # The flash renders on the index page.
    r2 = _client(seeded_settings).get(r.headers["location"])
    assert "AUTODEV_API_KEY" in r2.text


def test_full_sync_without_key_flashes(seeded_settings):
    r = _client(seeded_settings).post("/sync/full", follow_redirects=False)
    assert r.status_code == 303
    assert "flash=" in r.headers["location"]


def test_model_scoped_sync_unknown_model_404s(seeded_settings):
    c = _client(seeded_settings)
    assert c.post("/sync?model=nope", follow_redirects=False).status_code == 404
    assert c.post("/sync/full?model=nope", follow_redirects=False).status_code == 404


def test_model_scoped_sync_without_key_flashes(seeded_settings):
    r = _client(seeded_settings).post("/sync?model=ct4v_blackwing", follow_redirects=False)
    assert r.status_code == 303
    assert "flash=" in r.headers["location"]


def test_matches_page_has_model_scoped_sync_buttons(seeded_settings):
    r = _client(seeded_settings).get("/m/ct4v_blackwing").text
    assert "/sync?model=ct4v_blackwing" in r
    assert "/sync/full?model=ct4v_blackwing" in r
    assert "Sync this model" in r


def test_refresh_one_without_key_flashes(seeded_settings):
    r = _client(seeded_settings).post(f"/refresh/{BW_VIN}", follow_redirects=False)
    assert r.status_code == 303
    assert "flash=" in r.headers["location"]


def test_refresh_unknown_vin_404(seeded_settings):
    assert _client(seeded_settings).post("/refresh/NOPE", follow_redirects=False).status_code == 404


def test_detail_has_refresh_button(seeded_settings):
    r = _client(seeded_settings).get(f"/v/{BW_VIN}").text
    assert f"/refresh/{BW_VIN}" in r
    assert "Refresh this car" in r


def test_dismiss_hides_car_and_restore_brings_it_back(seeded_settings):
    c = _client(seeded_settings)
    assert BW_VIN in c.get("/m/ct4v_blackwing").text
    # Dismiss: redirects back to the given page; car vanishes from the list.
    r = c.post(f"/dismiss/{BW_VIN}?next=/m/ct4v_blackwing", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/m/ct4v_blackwing"
    page = c.get("/m/ct4v_blackwing").text
    assert BW_VIN not in page
    assert "show 1 dismissed" in page
    # Counts on the index no longer include it as ranked.
    assert "0 ranked" in c.get("/").text
    # Visible again via the toggle, flagged.
    shown = c.get("/m/ct4v_blackwing?show_dismissed=true").text
    assert BW_VIN in shown and "dismissed" in shown
    # Detail page shows the banner + restore.
    det = c.get(f"/v/{BW_VIN}").text
    assert "Restore" in det and "Dismissed" in det
    # Toggle back: car returns to the list.
    c.post(f"/dismiss/{BW_VIN}", follow_redirects=False)
    assert BW_VIN in c.get("/m/ct4v_blackwing").text


def test_favorite_toggles_and_floats_to_top(seeded_settings):
    c = _client(seeded_settings)
    # REVIEW_VIN (needs_review, 100pts) sits below nothing among c8 rows by
    # default; favorite it and it must carry the fav data attr + red heart.
    r = c.post(f"/favorite/{REVIEW_VIN}?next=/m/c8_stingray", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/m/c8_stingray"
    page = c.get("/m/c8_stingray").text
    assert 'data-fav="1"' in page
    assert "♥" in page
    assert 'id="fav-only"' in page  # the ♥-only filter checkbox
    # Toggle back off.
    c.post(f"/favorite/{REVIEW_VIN}", follow_redirects=False)
    assert 'data-fav="1"' not in c.get("/m/c8_stingray").text


def test_favorite_and_dismiss_are_mutually_exclusive(seeded_settings):
    c = _client(seeded_settings)
    import sqlite3

    def flags():
        conn = sqlite3.connect(str(seeded_settings.db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT dismissed_at, favorited_at FROM vehicles WHERE vin=?", (BW_VIN,)
        ).fetchone()
        conn.close()
        return bool(row["dismissed_at"]), bool(row["favorited_at"])

    c.post(f"/favorite/{BW_VIN}", follow_redirects=False)
    assert flags() == (False, True)
    c.post(f"/dismiss/{BW_VIN}", follow_redirects=False)  # dismissing clears the heart
    assert flags() == (True, False)
    c.post(f"/favorite/{BW_VIN}", follow_redirects=False)  # re-hearting un-dismisses
    assert flags() == (False, True)


def test_favorited_sorts_before_same_verdict(seeded_settings):
    import sqlite3

    conn = sqlite3.connect(str(seeded_settings.db_path))
    conn.execute("UPDATE vehicles SET favorited_at='2026-07-16T00:00:00Z' WHERE vin=?", (C8_VIN,))
    conn.commit()
    conn.close()
    from carfinder import store
    from carfinder.db import bootstrap

    c = bootstrap(seeded_settings.db_path)
    rows = store.ranked_matches(c, "c8_stingray", include_excluded=True)
    excluded = [m for m in rows if m.verdict == "excluded"]
    assert excluded and excluded[0].vin == C8_VIN and excluded[0].favorited


def test_dismiss_unknown_vin_404_and_next_sanitized(seeded_settings):
    c = _client(seeded_settings)
    assert c.post("/dismiss/NOPE", follow_redirects=False).status_code == 404
    # An absolute/protocol-relative next must not become an open redirect.
    r = c.post(f"/dismiss/{BW_VIN}?next=//evil.example", follow_redirects=False)
    assert r.headers["location"] == "/"


def test_sync_summary_flags_truncation():
    from carfinder.pipeline import PipelineStats
    from carfinder.sync import SyncStats
    from carfinder.web import _summarize_sync

    full = PipelineStats(sync=SyncStats(run_id="r", mode="incremental", new_vins=3))
    assert "incomplete" not in _summarize_sync({"a": full})
    cut = PipelineStats(sync=SyncStats(run_id="r", mode="incremental", truncated=True))
    assert "incomplete" in _summarize_sync({"a": cut})


def test_job_status_idle(seeded_settings):
    job = _client(seeded_settings).get("/api/job").json()["job"]
    assert job["state"] == "idle"
    assert job["kind"] is None


def _wait_for_job(client, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get("/api/job").json()["job"]
        if job["state"] in ("done", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job did not finish: {job}")


def _stub_offline_fetchers(monkeypatch):
    """Answer sticker AND build-sheet lookups from 'cache' with not_found, so
    scoring jobs run with zero network. (The build-sheet stub is load-bearing:
    without it an uncached VIN would POST to gmpartsgiant for real.)"""
    from carfinder import buildsheet
    from carfinder.stickers import NOT_FOUND, FetchResult, StickerFetcher

    monkeypatch.setattr(StickerFetcher, "is_cached", lambda self, vin: True)
    monkeypatch.setattr(
        StickerFetcher,
        "fetch",
        lambda self, vin, run_id=None: FetchResult(
            vin=vin, status=NOT_FOUND, path=None, from_cache=True
        ),
    )
    monkeypatch.setattr(
        buildsheet.BuildSheetFetcher,
        "fetch",
        lambda self, vin, run_id=None: buildsheet.FetchResult(
            vin=vin, status=buildsheet.NOT_FOUND, sheet=None, from_cache=True
        ),
    )


def test_rescore_endpoint_runs_job_offline(seeded_settings, monkeypatch):
    _stub_offline_fetchers(monkeypatch)
    c = _client(seeded_settings)
    r = c.post("/rescore", follow_redirects=False)
    assert r.status_code == 303
    job = _wait_for_job(c)
    assert job["state"] == "done", job
    assert "re-scored" in job["summary"]
    assert job["started_at"] and job["finished_at"]
    # A second job can start once the first finished (lock released).
    r2 = c.post("/rescore", follow_redirects=False)
    assert r2.status_code == 303 and "flash=" not in r2.headers["location"]
    assert _wait_for_job(c)["state"] == "done"


def test_rescore_scoped_to_one_model(seeded_settings, monkeypatch):
    _stub_offline_fetchers(monkeypatch)
    c = _client(seeded_settings)
    # Scoped: only the 1 seeded BW car is re-scored (the 2 C8s are skipped),
    # and the redirect returns to that model's page.
    r = c.post("/rescore?model=ct4v_blackwing", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/m/ct4v_blackwing"
    job = _wait_for_job(c)
    assert job["state"] == "done", job
    assert job["kind"] == "rescore ct4v_blackwing"
    assert "re-scored 1 cars" in job["summary"]
    # Unknown model: flash, no job started.
    r2 = c.post("/rescore?model=nope", follow_redirects=False)
    assert r2.status_code == 303 and "flash=" in r2.headers["location"]


def test_matches_page_has_model_scoped_rescore_button(seeded_settings):
    r = _client(seeded_settings).get("/m/ct4v_blackwing").text
    assert 'action="/rescore?model=ct4v_blackwing"' in r


def test_job_cancel_stops_a_running_job(seeded_settings, monkeypatch):
    # A deliberately slow fake rescore: ~10s if allowed to finish, but every
    # progress tick is a cancellation point, so /job/cancel stops it fast.
    from carfinder import runner

    def slow_rescore(conn, settings, year, config_dir=None, progress=None, only_model=None):
        for i in range(200):
            progress(f"step {i}")
            time.sleep(0.05)
        return {}

    monkeypatch.setattr(runner, "run_rescore", slow_rescore)
    c = _client(seeded_settings)
    assert c.post("/rescore", follow_redirects=False).status_code == 303
    # Wait until it's actually running, then pull the plug.
    deadline = time.monotonic() + 5
    while c.get("/api/job").json()["job"]["state"] != "running":
        assert time.monotonic() < deadline, "job never started"
        time.sleep(0.02)
    assert c.post("/job/cancel").json() == {"cancelling": True}
    job = _wait_for_job(c)
    assert job["state"] == "done"
    assert "stopped by user" in job["summary"]
    # The lock is released: a new job can start immediately.
    assert c.post("/job/cancel").json() == {"cancelling": False}  # nothing running
    r = c.post("/rescore?model=ct4v_blackwing", follow_redirects=False)
    assert r.status_code == 303 and "flash=" not in r.headers["location"]
    # Don't leak the second slow job past the test: stop it and wait it out.
    deadline = time.monotonic() + 5
    while c.get("/api/job").json()["job"]["state"] != "running":
        assert time.monotonic() < deadline, "second job never started"
        time.sleep(0.02)
    c.post("/job/cancel")
    _wait_for_job(c)


# --- manually tracked VINs ---------------------------------------------------

NEW_BW_VIN = "1G6D65RP2R0460256"  # real 2024 Blackwing manual, not seeded


def test_add_vin_rejects_invalid(seeded_settings):
    c = _client(seeded_settings)
    for bad in ("SHORT", "1G6D65RP9R046025X", "1G6D65RP2R046025O"):
        r = c.post("/vins", data={"vin": bad}, follow_redirects=False)
        assert r.status_code == 303
        assert "flash=" in r.headers["location"]
    # Valid check digit but not a model this tool tracks (a Honda).
    r = c.post("/vins", data={"vin": "1HGCM82633A004352"}, follow_redirects=False)
    assert "flash=" in r.headers["location"]
    # Nothing was inserted for any of them.
    import sqlite3

    conn = sqlite3.connect(str(seeded_settings.db_path))
    assert conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 3  # the seeds
    conn.close()


def test_add_vin_tracks_and_scores(seeded_settings, monkeypatch):
    _stub_offline_fetchers(monkeypatch)
    c = _client(seeded_settings)
    r = c.post("/vins", data={"vin": NEW_BW_VIN.lower()}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/v/{NEW_BW_VIN}"  # normalized to upper
    job = _wait_for_job(c)
    assert job["state"] == "done", job
    assert "verified & scored" in job["summary"]
    # Vehicle row exists with model + VIN-decoded year; the car was scored.
    import sqlite3

    conn = sqlite3.connect(str(seeded_settings.db_path))
    conn.row_factory = sqlite3.Row
    v = conn.execute("SELECT model_key, year FROM vehicles WHERE vin=?", (NEW_BW_VIN,)).fetchone()
    assert v["model_key"] == "ct4v_blackwing" and v["year"] == 2024
    m = conn.execute("SELECT verdict FROM match_results WHERE vin=?", (NEW_BW_VIN,)).fetchone()
    assert m is not None
    conn.close()
    # The detail page renders (no listing link needed) and shows the car.
    det = c.get(f"/v/{NEW_BW_VIN}")
    assert det.status_code == 200
    # Re-adding the same VIN just redirects to the existing detail page.
    r2 = c.post("/vins", data={"vin": NEW_BW_VIN}, follow_redirects=False)
    assert r2.status_code == 303 and r2.headers["location"] == f"/v/{NEW_BW_VIN}"


def test_index_has_track_vin_form(seeded_settings):
    r = _client(seeded_settings).get("/").text
    assert 'action="/vins"' in r
    assert "Track VIN" in r


# --- manual listing info ------------------------------------------------------


def test_set_listing_price_miles_and_rescores(seeded_settings, monkeypatch):
    _stub_offline_fetchers(monkeypatch)
    c = _client(seeded_settings)
    r = c.post(
        f"/v/{BW_VIN}/listing",
        data={"url": "", "price": "54,000", "miles": "25000"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == f"/v/{BW_VIN}"
    job = _wait_for_job(c)
    assert job["state"] == "done", job
    assert "listing info saved" in job["summary"]
    import sqlite3

    conn = sqlite3.connect(str(seeded_settings.db_path))
    conn.row_factory = sqlite3.Row
    v = conn.execute("SELECT price, miles FROM vehicles WHERE vin=?", (BW_VIN,)).fetchone()
    assert (v["price"], v["miles"]) == (54000.0, 25000)
    manual = conn.execute(
        "SELECT price, miles, url FROM listings WHERE source='manual' AND vin=?", (BW_VIN,)
    ).fetchone()
    assert (manual["price"], manual["miles"], manual["url"]) == (54000.0, 25000, None)
    # Existing autodev URL survives (manual row has none to override it).
    conn.close()
    assert "https://dealer.example/bw-listing" in c.get(f"/v/{BW_VIN}").text


def test_set_listing_url_overrides_feed_url_even_when_fetch_fails(seeded_settings, monkeypatch):
    _stub_offline_fetchers(monkeypatch)
    c = _client(seeded_settings)
    # The offline guard blocks the page fetch — the URL must still be saved
    # and become the one the detail page links to.
    r = c.post(
        f"/v/{BW_VIN}/listing",
        data={"url": "https://www.ebay.com/itm/188641881520", "price": "", "miles": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    job = _wait_for_job(c)
    assert job["state"] == "done", job
    assert "page fetch failed" in job["summary"]
    det = c.get(f"/v/{BW_VIN}").text
    assert "https://www.ebay.com/itm/188641881520" in det
    # Prior price/miles untouched by the failed fetch.
    import sqlite3

    conn = sqlite3.connect(str(seeded_settings.db_path))
    v = conn.execute("SELECT price, miles FROM vehicles WHERE vin=?", (BW_VIN,)).fetchone()
    conn.close()
    assert tuple(v) == (58000.0, 30000)


def test_set_listing_rejects_bad_url_and_unknown_vin(seeded_settings):
    c = _client(seeded_settings)
    r = c.post(f"/v/{BW_VIN}/listing", data={"url": "not-a-url"}, follow_redirects=False)
    assert "flash=" in r.headers["location"]
    assert c.post("/v/NOPE/listing", data={"price": "1"}, follow_redirects=False).status_code == 404
    # Nothing at all provided -> no job, straight back to the page.
    r2 = c.post(f"/v/{BW_VIN}/listing", data={}, follow_redirects=False)
    assert r2.headers["location"] == f"/v/{BW_VIN}"


def test_detail_page_has_listing_form(seeded_settings):
    r = _client(seeded_settings).get(f"/v/{BW_VIN}").text
    assert f'action="/v/{BW_VIN}/listing"' in r
    assert "Save listing info" in r
