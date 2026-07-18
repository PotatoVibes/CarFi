"""Manual listing intake: a user-pasted URL (eBay, dealer site, anywhere) for
a car that arrived by VIN, or whose feed link is wrong.

Best-effort by design: the page is fetched once with a browser-ish UA, reduced
to text, and price/mileage are pulled with conservative heuristics. Anything
the page doesn't yield falls back to what the user typed (or what we already
knew). The page text is also kept as claimed evidence so the scorer's text
matchers (and the AI extractor, for sticker-less cars) can use it.
"""

from __future__ import annotations

import html
import logging
import re

import httpx

log = logging.getLogger("carfinder.manual_listing")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
MAX_TEXT = 20_000

_TAG_DROP = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")

# Price: "$56,900", "US $56,900.00" — thousands separator required, which
# filters shipping costs and option prices; plausibility bounds do the rest.
_PRICE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+)(?:\.\d{2})?\b")
PRICE_MIN, PRICE_MAX = 5_000, 500_000

# Mileage: a labeled "Mileage: 12,345" beats a free-standing "12,345 miles".
_MILES_LABELED = re.compile(r"\bmileage\b[:\s]*([\d,]{1,9})", re.I)
_MILES_FREE = re.compile(r"\b(\d{1,3}(?:,\d{3})*|\d{1,6})\s*miles?\b", re.I)
MILES_MIN, MILES_MAX = 1, 200_000


def strip_html(page: str) -> str:
    """Reduce an HTML page to visible-ish text (order preserved)."""
    text = _TAG_DROP.sub(" ", page)
    text = _TAGS.sub(" ", text)
    text = html.unescape(text)
    return _WS.sub(" ", text).strip()


def fetch_page_text(
    url: str, timeout: float = 15.0, client: httpx.Client | None = None
) -> str | None:
    """Fetch a listing page and return its text, or None on any failure.
    One user-initiated GET — never retried, never scheduled."""
    try:
        if client is None:
            with httpx.Client(follow_redirects=True, timeout=timeout) as own:
                resp = own.get(url, headers={"User-Agent": _UA, "Accept-Language": "en-US"})
        else:
            resp = client.get(url, headers={"User-Agent": _UA, "Accept-Language": "en-US"})
    except Exception as exc:  # DNS, TLS, timeouts, blocked sandboxes — all soft
        log.warning("listing page fetch failed for %s: %s", url, exc)
        return None
    if resp.status_code != 200 or not resp.text:
        log.warning("listing page fetch for %s returned HTTP %d", url, resp.status_code)
        return None
    return strip_html(resp.text)[:MAX_TEXT]


def extract_price(text: str | None) -> float | None:
    """First plausible asking price in the page text (main price leads)."""
    for m in _PRICE.finditer(text or ""):
        value = float(m.group(1).replace(",", ""))
        if PRICE_MIN <= value <= PRICE_MAX:
            return value
    return None


def extract_miles(text: str | None) -> int | None:
    """Odometer reading: labeled 'Mileage:' first, then 'N miles'."""
    for pattern in (_MILES_LABELED, _MILES_FREE):
        for m in pattern.finditer(text or ""):
            try:
                value = int(m.group(1).replace(",", ""))
            except ValueError:
                continue
            if MILES_MIN <= value <= MILES_MAX:
                return value
    return None
