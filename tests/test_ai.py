"""Haiku extractor tests — mocked Anthropic API, zero real network."""

from __future__ import annotations

import json

import httpx
import pytest

from carfinder import db
from carfinder.ai import (
    ANTHROPIC_URL,
    Attribute,
    HaikuExtractor,
    _parse_features,
    _schema,
)
from carfinder.budget import ANTHROPIC, BudgetExceeded, BudgetGuard, ProviderBudget

ATTRS = [
    Attribute(key="front_lift", label="Front lift"),
    Attribute(key="z51", label="Z51 package"),
]


def _msg(features: dict, input_tokens=1000, output_tokens=50) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(features)}],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def _extractor(handler, cap_usd: float = 2.0):
    conn = db.bootstrap(":memory:")
    guard = BudgetGuard(conn, {ANTHROPIC: ProviderBudget(ANTHROPIC, "cost", cap_usd)})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HaikuExtractor(conn, guard, "key", client=client), guard, conn


# --- schema + parse --------------------------------------------------------


def test_schema_is_strict_enum():
    s = _schema(ATTRS)
    assert s["additionalProperties"] is False
    assert set(s["required"]) == {"front_lift", "z51"}
    assert s["properties"]["z51"]["enum"] == ["claimed", "denied", "not_mentioned"]


def test_parse_features_fills_missing_and_rejects_invalid():
    data = {
        "content": [{"type": "text", "text": json.dumps({"front_lift": "claimed", "z51": "bogus"})}]
    }
    out = _parse_features(data, ATTRS)
    assert out == {"front_lift": "claimed", "z51": "not_mentioned"}


def test_parse_features_non_json_is_all_not_mentioned():
    data = {"content": [{"type": "text", "text": "not json at all"}]}
    assert _parse_features(data, ATTRS) == {"front_lift": "not_mentioned", "z51": "not_mentioned"}


# --- request shape + extraction --------------------------------------------


def test_extract_sends_temp0_and_schema_and_parses():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json=_msg({"front_lift": "claimed", "z51": "not_mentioned"}))

    ex, guard, conn = _extractor(handler)
    result = ex.extract("Has front lift adjustable height", ATTRS)
    body = seen["body"]
    assert body["model"] == "claude-haiku-4-5"
    assert body["temperature"] == 0
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert seen["headers"]["x-api-key"] == "key"
    assert result.features["front_lift"] == "claimed"
    assert result.cached is False
    # cost = 1000 in * $1/M + 50 out * $5/M
    assert result.cost_usd == pytest.approx(1000 / 1e6 + 50 * 5 / 1e6)
    assert guard.usage(ANTHROPIC).used == pytest.approx(result.cost_usd)


def test_extract_posts_to_messages_endpoint():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_msg({"front_lift": "denied", "z51": "claimed"}))

    ex, _, _ = _extractor(handler)
    ex.extract("no front lift; has Z51", ATTRS)
    assert seen["url"] == ANTHROPIC_URL


# --- caching ---------------------------------------------------------------


def test_second_identical_extraction_is_cached_no_second_call():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=_msg({"front_lift": "claimed", "z51": "claimed"}))

    ex, guard, conn = _extractor(handler)
    first = ex.extract("blob", ATTRS)
    second = ex.extract("blob", ATTRS)
    assert len(calls) == 1
    assert first.cached is False and second.cached is True
    assert second.cost_usd == 0.0
    assert second.features == first.features
    # Budget charged once.
    assert guard.usage(ANTHROPIC).used == pytest.approx(first.cost_usd)


def test_different_text_is_a_cache_miss():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=_msg({"front_lift": "claimed", "z51": "claimed"}))

    ex, _, _ = _extractor(handler)
    ex.extract("blob one", ATTRS)
    ex.extract("blob two", ATTRS)
    assert len(calls) == 2


# --- spend guard -----------------------------------------------------------


def test_budget_cap_blocks_before_call():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=_msg({"front_lift": "claimed", "z51": "claimed"}))

    ex, _, _ = _extractor(handler, cap_usd=0.0)
    with pytest.raises(BudgetExceeded):
        ex.extract("blob", ATTRS)
    assert calls == []  # never left the process


def test_http_error_raises():
    ex, _, _ = _extractor(lambda r: httpx.Response(500, json={"error": "server"}))
    with pytest.raises(httpx.HTTPStatusError):
        ex.extract("blob", ATTRS)


# --- vision fallback -------------------------------------------------------


def test_extract_from_pdf_sends_document_block():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_msg({"front_lift": "claimed", "z51": "not_mentioned"}))

    ex, _, _ = _extractor(handler)
    result = ex.extract_from_pdf(b"%PDF-1.7 fake", ATTRS)
    content = seen["body"]["messages"][0]["content"]
    doc = next(b for b in content if b["type"] == "document")
    assert doc["source"]["media_type"] == "application/pdf"
    assert doc["source"]["type"] == "base64"
    assert result.features["front_lift"] == "claimed"
