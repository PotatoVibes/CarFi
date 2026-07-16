"""GM window sticker fetcher — cache-first, polite, budget-gated.

The endpoint is free and unauthenticated but intermittently throws Akamai
denials, so: realistic browser headers, exponential backoff, a per-run rate
limit, and a hard rule of never refetching a VIN we already hold. Fetch
outcomes (found / not_found / blocked) are recorded in the stickers table so a
not-found VIN isn't retried on every sync run.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from . import db
from .budget import GM_STICKER, BudgetGuard

log = logging.getLogger("carfinder.stickers")

STICKER_URL = "https://cws.gm.com/vs-cws/vehshop/v2/vehicle/windowsticker"

# Look like a normal browser; the endpoint serves dealers' web shoppers.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,application/octet-stream,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

FOUND = "found"
NOT_FOUND = "not_found"
BLOCKED = "blocked"


@dataclass(frozen=True)
class FetchResult:
    vin: str
    status: str  # FOUND | NOT_FOUND | BLOCKED
    path: Path | None
    from_cache: bool  # True when no network request was made


class StickerFetcher:
    """Fetch + cache window sticker PDFs, one VIN at a time."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        guard: BudgetGuard,
        stickers_dir: str | Path = "stickers",
        client: httpx.Client | None = None,
        min_interval_s: float = 3.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._conn = conn
        self._guard = guard
        self._dir = Path(stickers_dir)
        self._client = client or httpx.Client(headers=_HEADERS, timeout=30.0, follow_redirects=True)
        self._min_interval = min_interval_s
        self._max_retries = max_retries
        self._sleep = sleep
        self._last_request_at: float | None = None

    # -- cache layer ---------------------------------------------------------

    def _pdf_path(self, vin: str) -> Path:
        return self._dir / f"{vin.upper()}.pdf"

    def _cached(self, vin: str) -> FetchResult | None:
        """Return a no-network result when we already know the answer."""
        path = self._pdf_path(vin)
        if path.exists():
            return FetchResult(vin, FOUND, path, from_cache=True)
        prior = db.get_sticker_status(self._conn, vin)
        if prior is not None and prior["status"] == NOT_FOUND:
            # Definitive miss recorded — don't hammer GM every run.
            return FetchResult(vin, NOT_FOUND, None, from_cache=True)
        # BLOCKED is transient (Akamai mood) — allow a retry on a later run.
        return None

    def is_cached(self, vin: str) -> bool:
        """True if fetch(vin) would answer without a network call."""
        return self._cached(vin.strip().upper()) is not None

    # -- politeness ----------------------------------------------------------

    def _rate_limit(self) -> None:
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self._min_interval:
                self._sleep(self._min_interval - elapsed)
        self._last_request_at = time.monotonic()

    # -- fetch ---------------------------------------------------------------

    def fetch(self, vin: str, run_id: str | None = None) -> FetchResult:
        vin = vin.strip().upper()
        cached = self._cached(vin)
        if cached is not None:
            return cached

        now = datetime.now(UTC)
        status = self._fetch_with_retries(vin, run_id)
        path = self._pdf_path(vin) if status == FOUND else None
        db.record_sticker_outcome(self._conn, vin, status, now, str(path) if path else None)
        return FetchResult(vin, status, path, from_cache=False)

    def _fetch_with_retries(self, vin: str, run_id: str | None) -> str:
        backoff = 1.0
        for attempt in range(1, self._max_retries + 1):
            self._rate_limit()
            with self._guard.reserve(GM_STICKER, "windowsticker", run_id=run_id):
                try:
                    resp = self._client.get(STICKER_URL, params={"vin": vin})
                except httpx.HTTPError as exc:
                    log.warning("sticker %s attempt %d transport error: %s", vin, attempt, exc)
                    self._sleep(backoff)
                    backoff *= 2
                    continue

            if resp.status_code == 200 and resp.content.startswith(b"%PDF"):
                self._dir.mkdir(parents=True, exist_ok=True)
                self._pdf_path(vin).write_bytes(resp.content)
                log.info("sticker %s cached (%d bytes)", vin, len(resp.content))
                return FOUND
            if resp.status_code == 200 and b'"errorCode"' in resp.content[:200]:
                # GM's definitive JSON miss, e.g. errorCode 1001 "No Window
                # Sticker found for the requested VIN." (2021-and-older VINs
                # aren't served by this endpoint at all.)
                log.info("sticker %s not found: %s", vin, resp.content[:120])
                return NOT_FOUND
            if resp.status_code == 404:
                log.info("sticker %s not found (HTTP 404)", vin)
                return NOT_FOUND
            if resp.status_code == 200:
                # 200 with an unrecognized body — likely an interstitial /
                # denial page. Treat as blocked (retryable), never as a miss.
                log.warning("sticker %s unrecognized 200 body: %s", vin, resp.content[:120])
                self._sleep(backoff)
                backoff *= 2
                continue
            # 403 = Akamai denial; 5xx = transient. Both: back off and retry.
            log.warning("sticker %s attempt %d HTTP %d", vin, attempt, resp.status_code)
            self._sleep(backoff)
            backoff *= 2
        return BLOCKED
