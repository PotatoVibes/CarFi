"""Manual listing intake: HTML stripping + price/mileage heuristics. Offline."""

from __future__ import annotations

import httpx

from carfinder.manual_listing import (
    extract_miles,
    extract_price,
    fetch_page_text,
    strip_html,
)

EBAY_ISH = """
<html><head><title>2023 Cadillac CT4-V Blackwing | eBay</title>
<script>var junk = "$1.00";</script><style>.x{color:red}</style></head>
<body><h1>2023 Cadillac CT4-V Blackwing 6-Speed Manual</h1>
<div class="price">US $56,900.00</div>
<span>$5.99 shipping</span>
<table><tr><td>Mileage:</td><td>12,345</td></tr></table>
<p>Sueded microfiber package, carbon fiber seats. 12 photos.</p>
</body></html>
"""


def test_strip_html_drops_scripts_and_tags():
    text = strip_html(EBAY_ISH)
    assert "var junk" not in text and "<div" not in text
    assert "US $56,900.00" in text
    assert "Sueded microfiber package" in text


def test_extract_price_skips_shipping_and_junk():
    # $5.99 has no thousands separator; $1.00 lives in a dropped <script>.
    assert extract_price(strip_html(EBAY_ISH)) == 56900.0
    assert extract_price("no dollars here") is None
    assert extract_price("$3.50 fee, $900 deposit") is None  # below plausibility
    assert extract_price(None) is None


def test_extract_miles_prefers_labeled_mileage():
    assert extract_miles(strip_html(EBAY_ISH)) == 12345
    assert extract_miles("driven 8,200 miles, garage kept") == 8200
    assert extract_miles("500,000 miles!") is None  # beyond plausibility
    assert extract_miles("no odometer mentioned") is None
    assert extract_miles(None) is None


def test_fetch_page_text_soft_fails():
    # Any transport/HTTP failure returns None — never raises into the job.
    def die(request):
        raise httpx.ConnectError("nope")

    client = httpx.Client(transport=httpx.MockTransport(die))
    assert fetch_page_text("https://example.com/x", client=client) is None
    client404 = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, text="gone"))
    )
    assert fetch_page_text("https://example.com/x", client=client404) is None


def test_fetch_page_text_returns_stripped_text():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=EBAY_ISH))
    )
    text = fetch_page_text("https://example.com/x", client=client)
    assert "US $56,900.00" in text and "<div" not in text
