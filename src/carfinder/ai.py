"""Haiku claimed-feature extraction (M5) — the optional AI fallback.

Runs only when no verified structured source (window sticker / OEM build data)
is available for a VIN. Reads a dealer free-text blob (or, as a rare fallback,
a low-confidence sticker PDF) and classifies each profile attribute as
claimed / denied / not_mentioned.

Discipline: every result is tagged ``claimed`` downstream — it can raise a soft
score or a highlight bonus but can NEVER satisfy a hard requirement. temperature
is 0 and responses are cached by hash, so a repeat input is a free exact replay.
Spend is recorded as ACTUAL cost (from token usage) against the Anthropic budget.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from . import db
from .budget import ANTHROPIC, BudgetGuard

log = logging.getLogger("carfinder.ai")

AI_MODEL = "claude-haiku-4-5"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# Haiku 4.5 pricing, USD per 1M tokens.
HAIKU_INPUT_PER_MTOK = 1.00
HAIKU_OUTPUT_PER_MTOK = 5.00

STATUSES = ("claimed", "denied", "not_mentioned")

SYSTEM_PROMPT = (
    "You classify whether a used-car dealer listing asserts specific vehicle "
    "features. For each requested attribute, decide strictly from the provided "
    "text:\n"
    "- 'claimed': the text explicitly says the vehicle HAS the feature.\n"
    "- 'denied': the text explicitly says the vehicle LACKS the feature.\n"
    "- 'not_mentioned': the text says nothing either way.\n"
    "Do not guess or infer from the model name. When unsure, use 'not_mentioned'."
)


@dataclass(frozen=True)
class Attribute:
    key: str
    label: str
    description: str = ""


@dataclass
class Extraction:
    features: dict[str, str]  # attribute key -> status
    cost_usd: float
    cached: bool
    model: str
    fields: list[str] = field(default_factory=list)


def _schema(attributes: list[Attribute]) -> dict:
    return {
        "type": "object",
        "properties": {a.key: {"type": "string", "enum": list(STATUSES)} for a in attributes},
        "required": [a.key for a in attributes],
        "additionalProperties": False,
    }


def _attribute_lines(attributes: list[Attribute]) -> str:
    return "\n".join(
        f"- {a.key}: {a.label}" + (f" ({a.description})" if a.description else "")
        for a in attributes
    )


def _cache_key(model: str, attributes: list[Attribute], material: tuple[str, str]) -> str:
    payload = json.dumps(
        [model, [(a.key, a.label, a.description) for a in attributes], list(material)],
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class HaikuExtractor:
    def __init__(
        self,
        conn,
        guard: BudgetGuard,
        api_key: str,
        client: httpx.Client | None = None,
        model: str = AI_MODEL,
        max_tokens: int = 512,
        now: Callable[[], datetime] | None = None,
    ):
        self._conn = conn
        self._guard = guard
        self._model = model
        self._max_tokens = max_tokens
        self._now = now or (lambda: datetime.now(UTC))
        self._client = client or httpx.Client(timeout=60.0)
        self._headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    # -- public API ----------------------------------------------------------

    def extract(
        self, text: str, attributes: list[Attribute], run_id: str | None = None
    ) -> Extraction:
        """Extract claimed features from a dealer free-text blob."""
        prompt = (
            "Classify these attributes for the vehicle described below.\n\n"
            f"Attributes:\n{_attribute_lines(attributes)}\n\n"
            f'Listing text:\n"""\n{text}\n"""'
        )
        content = [{"type": "text", "text": prompt}]
        return self._run(content, attributes, ("text", text), run_id)

    def extract_from_pdf(
        self, pdf_bytes: bytes, attributes: list[Attribute], run_id: str | None = None
    ) -> Extraction:
        """Vision/document fallback: extract attributes from a sticker PDF.

        Rare — used only when the deterministic parser's confidence is too low.
        Still tagged claimed downstream (AI output never counts as verified)."""
        b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
        prompt = (
            "This is a vehicle window sticker. Classify these attributes from it.\n\n"
            f"Attributes:\n{_attribute_lines(attributes)}"
        )
        content = [
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
            },
            {"type": "text", "text": prompt},
        ]
        digest = hashlib.sha256(pdf_bytes).hexdigest()
        return self._run(content, attributes, ("pdf", digest), run_id)

    # -- core ----------------------------------------------------------------

    def _run(
        self,
        content: list[dict],
        attributes: list[Attribute],
        material: tuple[str, str],
        run_id: str | None,
    ) -> Extraction:
        cache_key = _cache_key(self._model, attributes, material)
        hit = db.get_ai_cache(self._conn, cache_key)
        if hit is not None:
            features = json.loads(hit["response_json"])
            return Extraction(features, 0.0, cached=True, model=self._model, fields=list(features))

        body = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "temperature": 0,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": content}],
            "output_config": {"format": {"type": "json_schema", "schema": _schema(attributes)}},
        }

        now = self._now()
        # Enforce the monthly USD cap with a conservative upper-bound estimate
        # BEFORE the call leaves the process; record the ACTUAL cost after.
        self._guard.check(ANTHROPIC, cost_usd=self._estimate_cost(content), now=now)
        resp = self._client.post(ANTHROPIC_URL, headers=self._headers, json=body)
        resp.raise_for_status()
        data = resp.json()

        cost = self._actual_cost(data.get("usage", {}))
        features = _parse_features(data, attributes)
        db.record_call(self._conn, ANTHROPIC, "haiku.extract", now, run_id, cost_usd=cost)
        db.put_ai_cache(self._conn, cache_key, self._model, json.dumps(features), cost, now)
        return Extraction(features, cost, cached=False, model=self._model, fields=list(features))

    def _estimate_cost(self, content: list[dict]) -> float:
        # Upper bound: assume the response uses the full max_tokens; over-count
        # input from raw block sizes. Keeps the hard cap from being overshot.
        approx_in = sum(len(json.dumps(b)) for b in content) // 3 + 400
        return (
            approx_in * HAIKU_INPUT_PER_MTOK / 1_000_000
            + self._max_tokens * HAIKU_OUTPUT_PER_MTOK / 1_000_000
        )

    def _actual_cost(self, usage: dict) -> float:
        in_tok = int(usage.get("input_tokens", 0)) + int(usage.get("cache_read_input_tokens", 0))
        out_tok = int(usage.get("output_tokens", 0))
        return (
            in_tok * HAIKU_INPUT_PER_MTOK / 1_000_000 + out_tok * HAIKU_OUTPUT_PER_MTOK / 1_000_000
        )


def _parse_features(data: dict, attributes: list[Attribute]) -> dict[str, str]:
    """Parse + validate the model's JSON. Missing/invalid values -> not_mentioned."""
    text = next((b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"), "")
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        log.warning("AI extraction returned non-JSON; treating all attributes as not_mentioned")
        raw = {}
    out: dict[str, str] = {}
    for a in attributes:
        val = raw.get(a.key)
        out[a.key] = val if val in STATUSES else "not_mentioned"
    return out
