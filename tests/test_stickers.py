"""Sticker fetcher tests — mocked transport, zero real network."""

from __future__ import annotations

import httpx
import pytest

from carfinder import db
from carfinder.budget import GM_STICKER, BudgetGuard, ProviderBudget
from carfinder.stickers import BLOCKED, FOUND, NOT_FOUND, StickerFetcher

PDF = b"%PDF-1.7 fake sticker body"
VIN = "1G1YB3D46M5122651"


def _fetcher(tmp_path, handler, cap: float = 100, max_retries: int = 3):
    conn = db.bootstrap(":memory:")
    guard = BudgetGuard(conn, {GM_STICKER: ProviderBudget(GM_STICKER, "count", cap)})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps: list[float] = []
    fetcher = StickerFetcher(
        conn,
        guard,
        stickers_dir=tmp_path / "stickers",
        client=client,
        min_interval_s=0.0,
        max_retries=max_retries,
        sleep=sleeps.append,
    )
    return fetcher, conn, guard, sleeps


def test_found_pdf_is_cached_and_recorded(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=PDF)

    fetcher, conn, guard, _ = _fetcher(tmp_path, handler)
    result = fetcher.fetch(VIN)
    assert result.status == FOUND and not result.from_cache
    assert result.path.read_bytes() == PDF
    assert VIN in calls[0]
    row = db.get_sticker_status(conn, VIN)
    assert row["status"] == FOUND and row["attempts"] == 1
    assert guard.usage(GM_STICKER).used == 1


def test_cached_vin_is_never_refetched(tmp_path):
    def handler(request):
        return httpx.Response(200, content=PDF)

    fetcher, _, guard, _ = _fetcher(tmp_path, handler)
    first = fetcher.fetch(VIN)
    second = fetcher.fetch(VIN)
    assert first.status == FOUND and not first.from_cache
    assert second.status == FOUND and second.from_cache
    assert guard.usage(GM_STICKER).used == 1  # one network call, ever


def test_not_found_recorded_and_not_retried(tmp_path):
    hits = []

    def handler(request):
        hits.append(1)
        return httpx.Response(200, content=b"<html>no sticker</html>")

    fetcher, conn, guard, _ = _fetcher(tmp_path, handler)
    first = fetcher.fetch(VIN)
    second = fetcher.fetch(VIN)  # must be answered from the DB, not GM
    assert first.status == NOT_FOUND and not first.from_cache
    assert second.status == NOT_FOUND and second.from_cache
    assert len(hits) == 1
    assert db.get_sticker_status(conn, VIN)["status"] == NOT_FOUND


def test_404_is_not_found(tmp_path):
    fetcher, _, _, _ = _fetcher(tmp_path, lambda r: httpx.Response(404))
    assert fetcher.fetch(VIN).status == NOT_FOUND


def test_akamai_denial_retries_with_backoff_then_blocked(tmp_path):
    hits = []

    def handler(request):
        hits.append(1)
        return httpx.Response(403)

    fetcher, conn, _, sleeps = _fetcher(tmp_path, handler, max_retries=3)
    result = fetcher.fetch(VIN)
    assert result.status == BLOCKED
    assert len(hits) == 3
    assert sleeps == [1.0, 2.0, 4.0]  # exponential backoff
    assert db.get_sticker_status(conn, VIN)["status"] == BLOCKED


def test_blocked_vin_is_retried_on_next_run(tmp_path):
    responses = iter(
        [
            httpx.Response(403),
            httpx.Response(403),
            httpx.Response(403),
            httpx.Response(200, content=PDF),
        ]
    )

    def handler(request):
        return next(responses)

    fetcher, conn, _, _ = _fetcher(tmp_path, handler, max_retries=3)
    assert fetcher.fetch(VIN).status == BLOCKED
    # Next run: blocked is transient, so we try again and succeed.
    assert fetcher.fetch(VIN).status == FOUND
    assert db.get_sticker_status(conn, VIN)["attempts"] == 2


def test_recovers_after_one_transient_error(tmp_path):
    responses = iter([httpx.Response(503), httpx.Response(200, content=PDF)])
    fetcher, _, _, sleeps = _fetcher(tmp_path, lambda r: next(responses))
    assert fetcher.fetch(VIN).status == FOUND
    assert sleeps == [1.0]


def test_budget_cap_blocks_fetch(tmp_path):
    from carfinder.budget import BudgetExceeded

    fetcher, _, _, _ = _fetcher(tmp_path, lambda r: httpx.Response(200, content=PDF), cap=0)
    with pytest.raises(BudgetExceeded):
        fetcher.fetch(VIN)


def test_vin_normalized_to_uppercase(tmp_path):
    fetcher, conn, _, _ = _fetcher(tmp_path, lambda r: httpx.Response(200, content=PDF))
    result = fetcher.fetch(VIN.lower())
    assert result.path.name == f"{VIN}.pdf"
    assert db.get_sticker_status(conn, VIN) is not None
