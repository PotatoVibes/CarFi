"""Optional webhook notifications — ntfy or Discord.

Fired by the pipeline when a car newly reaches `ranked` at/above the score
threshold. Best-effort: a webhook failure logs a warning and never breaks a
sync run. Not budget-gated (the target is the user's own ntfy/Discord, not a
metered provider).
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger("carfinder.notify")


def _is_discord(url: str) -> bool:
    return "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url


def _payload(url: str, title: str, body: str) -> dict:
    if _is_discord(url):
        return {"content": f"**{title}**\n{body}"}
    # ntfy accepts a plain-text body with a Title header, but a JSON body also
    # works when posting to the base host; keep it simple with a text-ish JSON.
    return {"title": title, "message": body}


class Notifier:
    def __init__(self, webhook_url: str | None, client: httpx.Client | None = None):
        self._url = webhook_url
        self._client = client or httpx.Client(timeout=10.0)

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    def notify_match(self, vin: str, title: str, body: str) -> bool:
        """Send one notification. Returns True on success, False on any failure
        (logged, never raised) — a dead webhook must not fail a sync."""
        if not self._url:
            return False
        try:
            if _is_discord(self._url):
                resp = self._client.post(self._url, json=_payload(self._url, title, body))
            else:
                # ntfy: POST the message body with a Title header.
                resp = self._client.post(
                    self._url, content=body.encode("utf-8"), headers={"Title": title}
                )
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.warning("notification for %s failed: %s", vin, exc)
            return False
