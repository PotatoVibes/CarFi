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


def test_detail_links_to_dealer_listing(seeded_settings):
    r = _client(seeded_settings).get(f"/v/{BW_VIN}")
    assert "https://dealer.example/bw-listing" in r.text


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


def test_rescore_endpoint_runs_job_offline(seeded_settings, monkeypatch):
    # Keep the job fully offline: the fake REVIEW_VIN isn't sticker-cached, so
    # stub the fetcher to answer from cache with not_found.
    from carfinder.stickers import NOT_FOUND, FetchResult, StickerFetcher

    monkeypatch.setattr(StickerFetcher, "is_cached", lambda self, vin: True)
    monkeypatch.setattr(
        StickerFetcher,
        "fetch",
        lambda self, vin, run_id=None: FetchResult(
            vin=vin, status=NOT_FOUND, path=None, from_cache=True
        ),
    )
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
