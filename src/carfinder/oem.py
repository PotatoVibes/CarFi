"""Auto.dev OEM Build Data API client + normalizer.

Build data is factory-order data keyed by VIN (GET /build/{vin}). It shares the
api.auto.dev account, so calls are gated + recorded under the same `autodev`
budget bucket. The normalizer maps the response into BuildFacts — the same
comparable attribute shape the sticker parser produces — so the cross-reference
(oem vs sticker) and the reconciler can diff sources field by field.

The exact response shape is pinned by the recorded fixtures; candidate key
lists stay broad to tolerate minor drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from .budget import AUTODEV, BudgetGuard


@dataclass
class BuildFacts:
    """Normalized factory facts from OEM build data (source='build')."""

    vin: str | None = None
    year: int | None = None
    make: str | None = None
    model: str | None = None
    trim: str | None = None
    msrp: float | None = None
    paint_rpo: str | None = None
    paint_desc: str | None = None
    options: set[str] = field(default_factory=set)  # factory RPO codes
    available: bool = True  # False when the endpoint returned no build record
    raw: dict[str, Any] = field(default_factory=dict)


def _first(raw: dict[str, Any], *paths: str) -> Any:
    for path in paths:
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


def _extract_option_codes(raw: dict[str, Any]) -> set[str]:
    """Pull RPO codes out of whatever option container the payload uses.

    Handles: a flat list of code strings, a list of {code/rpo/id: ...} dicts,
    or a dict keyed by code. Finalized against the recorded fixtures.
    """
    container = _first(raw, "options", "installedOptions", "equipment", "rpo", "optionCodes")
    codes: set[str] = set()
    if isinstance(container, dict):
        container = (
            list(container.keys()) if _looks_like_code_map(container) else list(container.values())
        )
    if isinstance(container, list):
        for item in container:
            if isinstance(item, str):
                codes.add(item.strip().upper())
            elif isinstance(item, dict):
                code = _first(item, "code", "rpo", "id", "optionCode")
                if code:
                    codes.add(str(code).strip().upper())
    return {c for c in codes if c}


def _looks_like_code_map(d: dict) -> bool:
    keys = list(d.keys())
    return bool(keys) and all(isinstance(k, str) and 2 <= len(k) <= 3 for k in keys[:5])


def normalize_build(raw: dict[str, Any]) -> BuildFacts:
    """Map a raw /build response into BuildFacts."""
    # Unwrap a {"data": {...}} envelope if present.
    body = raw.get("data", raw) if isinstance(raw, dict) else raw
    if not isinstance(body, dict):
        return BuildFacts(available=False, raw=raw)

    # No build record for this VIN (empty body or explicit error).
    if not body or "errorCode" in body or _first(body, "error"):
        return BuildFacts(available=False, raw=raw)

    return BuildFacts(
        vin=_first(body, "vin", "vehicle.vin"),
        year=_as_int(_first(body, "year", "modelYear", "vehicle.year")),
        make=_first(body, "make", "vehicle.make"),
        model=_first(body, "model", "vehicle.model"),
        trim=_first(body, "trim", "style", "vehicle.trim"),
        msrp=_as_float(_first(body, "msrp", "baseMsrp", "totalMsrp", "price.msrp")),
        paint_rpo=_first(body, "exteriorColorCode", "paintCode", "colorCode"),
        paint_desc=_first(body, "exteriorColor", "paint", "color"),
        options=_extract_option_codes(body),
        available=True,
        raw=raw,
    )


class OemBuildSource:
    """Auto.dev OEM Build Data client. Calls flow through BudgetGuard."""

    name = "autodev"  # same api.auto.dev quota bucket
    BASE_URL = "https://api.auto.dev"

    def __init__(self, api_key: str, guard: BudgetGuard, client: httpx.Client | None = None):
        self._guard = guard
        self._client = client or httpx.Client(
            base_url=self.BASE_URL,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=30.0,
        )

    def get_build_raw(self, vin: str, run_id: str | None = None) -> dict:
        with self._guard.reserve(AUTODEV, "build", run_id=run_id):
            resp = self._client.get(f"/build/{vin}")
        # 404 = no build record for this VIN; 402 = endpoint not on this plan.
        # Both mean "unavailable" here, not an exception — normalize_build reads
        # errorCode and returns available=False.
        if resp.status_code in (402, 404):
            try:
                body = resp.json()
            except ValueError:
                body = {}
            body.setdefault("errorCode", resp.status_code)
            return body
        resp.raise_for_status()
        return resp.json()

    def get_build(self, vin: str, run_id: str | None = None) -> BuildFacts:
        return normalize_build(self.get_build_raw(vin, run_id=run_id))
