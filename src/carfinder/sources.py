"""Listing sources: the ListingSource protocol, AutoDevSource, and a disabled
Marketcheck stub. Every outbound Auto.dev call is gated + recorded by
BudgetGuard — there is no other path to the network here.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

from .budget import AUTODEV, BudgetGuard
from .query import SELECT_FIELDS

log = logging.getLogger("carfinder.sources")

# Starter tier allows 5 req/s. Stay under it with a small min interval, and
# retry the statuses that mean "slow down" or "transient".
_RETRYABLE = {429, 500, 502, 503, 504}


@dataclass
class ListingRow:
    """Normalized listing, source-agnostic. `raw` keeps the full payload."""

    vin: str
    source: str
    price: float | None = None
    miles: int | None = None
    year: int | None = None
    make: str | None = None
    model: str | None = None
    trim: str | None = None
    color: str | None = None
    created_at: str | None = None
    url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Page:
    rows: list[ListingRow]
    total: int | None
    has_next: bool
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ListingSource(Protocol):
    name: str

    def search_page(
        self,
        params: dict[str, str],
        page: int = 1,
        sort: str = "createdAt.desc",
        run_id: str | None = None,
    ) -> Page: ...


# --- normalization ---------------------------------------------------------


def _first(raw: dict[str, Any], *paths: str) -> Any:
    """Look up the first present path, accepting either a flat dot-key
    (Auto.dev's `select` output) or a nested dict path."""
    for path in paths:
        if path in raw and raw[path] not in (None, ""):
            return raw[path]
        cur: Any = raw
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, ""):
            return cur
    return None


def _as_int(v: Any) -> int | None:
    try:
        return int(str(v).replace(",", "").split(".")[0]) if v is not None else None
    except (ValueError, TypeError):
        return None


def _as_float(v: Any) -> float | None:
    try:
        return float(str(v).replace(",", "").replace("$", "")) if v is not None else None
    except (ValueError, TypeError):
        return None


def _as_url(v: Any) -> str | None:
    """Absolute http(s) URLs only. Auto.dev sometimes sends junk fragments
    like '#-4976296983083547062' as retailListing.vdp — rendering those as a
    link just re-anchors the current page."""
    if isinstance(v, str) and v.lower().startswith(("http://", "https://")):
        return v
    return None


def normalize(item: dict[str, Any], source: str) -> ListingRow:
    """Map a raw Auto.dev listing (nested or flat) into a ListingRow.

    Candidate key lists are intentionally broad; the exact shape is pinned by
    the recorded fixtures and this stays tolerant of minor drift.
    """
    return ListingRow(
        vin=str(_first(item, "vehicle.vin", "vin") or "").upper(),
        source=source,
        price=_as_float(_first(item, "retailListing.price", "price")),
        miles=_as_int(
            _first(item, "vehicle.mileage", "vehicle.miles", "retailListing.miles", "mileage")
        ),
        year=_as_int(_first(item, "vehicle.year", "year")),
        make=_first(item, "vehicle.make", "make"),
        model=_first(item, "vehicle.model", "model"),
        trim=_first(item, "vehicle.trim", "trim"),
        color=_first(
            item, "vehicle.exteriorColor", "vehicle.exterior_color", "vehicle.color", "color"
        ),
        created_at=_first(item, "createdAt", "created_at"),
        url=_as_url(_first(item, "retailListing.vdp", "retailListing.vdpUrl", "url")),
        raw=item,
    )


# --- Auto.dev --------------------------------------------------------------


class AutoDevSource:
    """Auto.dev Listings API client. All calls flow through BudgetGuard."""

    name = "autodev"
    BASE_URL = "https://api.auto.dev"

    def __init__(
        self,
        api_key: str,
        guard: BudgetGuard,
        client: httpx.Client | None = None,
        page_size: int = 20,  # Starter free-tier cap
        min_interval_s: float = 0.25,  # <= 4 req/s, under the 5/s tier limit
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._guard = guard
        self._page_size = page_size
        self._min_interval = min_interval_s
        self._max_retries = max_retries
        self._sleep = sleep
        self._last_request_at: float | None = None
        self._client = client or httpx.Client(
            base_url=self.BASE_URL,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=30.0,
        )

    def _rate_limit(self) -> None:
        """Space requests out to stay under the tier's per-second limit."""
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self._min_interval:
                self._sleep(self._min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _get(self, path: str, params: dict, endpoint: str, run_id: str | None) -> dict:
        backoff = 1.0
        last_exc: httpx.HTTPStatusError | None = None
        for attempt in range(1, self._max_retries + 1):
            self._rate_limit()
            # Every attempt is a real outbound call, so it's gated + recorded.
            with self._guard.reserve(AUTODEV, endpoint, run_id=run_id):
                resp = self._client.get(path, params=params)
            if resp.status_code in _RETRYABLE:
                retry_after = resp.headers.get("retry-after")
                wait = float(retry_after) if (retry_after or "").isdigit() else backoff
                log.warning(
                    "auto.dev %s HTTP %d (attempt %d) — waiting %.1fs",
                    endpoint,
                    resp.status_code,
                    attempt,
                    wait,
                )
                try:
                    last_exc = resp.raise_for_status()  # type: ignore[assignment]
                except httpx.HTTPStatusError as exc:
                    last_exc = exc
                self._sleep(wait)
                backoff *= 2
                continue
            resp.raise_for_status()  # non-retryable 4xx -> raise immediately
            return resp.json()
        # Exhausted retries on a retryable status.
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"auto.dev {endpoint}: retries exhausted")

    def search_page(
        self,
        params: dict[str, str],
        page: int = 1,
        sort: str = "createdAt.desc",
        run_id: str | None = None,
        select: bool = True,
        include_total: bool = False,
    ) -> Page:
        q: dict[str, str] = {**params, "page": str(page), "limit": str(self._page_size)}
        if sort:
            q["sort"] = sort
        if select:
            q["select"] = ",".join(SELECT_FIELDS)
        if include_total:
            q["includes"] = "total"
        data = self._get("/listings", q, "listings.search", run_id)
        return self._to_page(data)

    def get_listing(self, vin: str, run_id: str | None = None) -> ListingRow | None:
        data = self._get(f"/listings/{vin}", {}, "listings.vin", run_id)
        item = data.get("data", data) if isinstance(data, dict) else data
        if isinstance(item, list):
            item = item[0] if item else None
        return normalize(item, self.name) if item else None

    def count(self, params: dict[str, str], run_id: str | None = None) -> int:
        q: dict[str, str] = {**params, "includes": "total", "limit": "1"}
        data = self._get("/listings", q, "listings.count", run_id)
        return int(data.get("total") or 0)

    def _to_page(self, data: dict) -> Page:
        items = data.get("data", []) if isinstance(data, dict) else []
        rows = [normalize(item, self.name) for item in items]
        links = (data.get("links") or {}) if isinstance(data, dict) else {}
        total = data.get("total") if isinstance(data, dict) else None
        has_next = bool(links.get("next")) if "next" in links else len(rows) == self._page_size
        return Page(rows=rows, total=total, has_next=has_next, raw=data)


# --- Marketcheck (stub, disabled) ------------------------------------------


class MarketcheckSource:
    """Protocol-conforming stub. Disabled by default: the free tier's 100-mile
    radius makes it a Houston-local supplement at best (see PLAN.md M3)."""

    name = "marketcheck"
    enabled = False

    def __init__(self, *args: Any, **kwargs: Any):
        pass

    def search_page(
        self,
        params: dict[str, str],
        page: int = 1,
        sort: str = "createdAt.desc",
        run_id: str | None = None,
    ) -> Page:
        raise NotImplementedError("MarketcheckSource is disabled (free-tier 100-mile radius)")
