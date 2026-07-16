"""Webhook notification tests — mocked transport, offline."""

from __future__ import annotations

import httpx

from carfinder.notify import Notifier


def _notifier(url, handler):
    return Notifier(url, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_disabled_when_no_url():
    n = Notifier(None)
    assert n.enabled is False
    assert n.notify_match("V", "t", "b") is False


def test_ntfy_posts_body_with_title_header():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["title"] = request.headers.get("title")
        seen["body"] = request.content.decode()
        return httpx.Response(200)

    n = _notifier("https://ntfy.sh/my-topic", handler)
    assert n.notify_match("V1", "Match!", "body text") is True
    assert seen["url"] == "https://ntfy.sh/my-topic"
    assert seen["title"] == "Match!"
    assert seen["body"] == "body text"


def test_discord_posts_json_content():
    seen = {}

    def handler(request):
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(204)

    n = _notifier("https://discord.com/api/webhooks/123/abc", handler)
    assert n.notify_match("V1", "Match!", "body") is True
    assert "Match!" in seen["body"]["content"]


def test_failure_is_swallowed():
    n = _notifier("https://ntfy.sh/x", lambda r: httpx.Response(500))
    assert n.notify_match("V1", "t", "b") is False  # logged, not raised
