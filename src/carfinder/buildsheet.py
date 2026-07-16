"""GM build-sheet fallback source (gmpartsgiant.com VIN decoder).

The parts catalog's VIN decoder returns the factory build sheet — the full
broadcast option list — for ANY model year, covering the pre-2022 gap in
GM's window-sticker endpoint. Validated 2026-07-16 against four corpus cars
(see tests): trim packages, Z51, body/roof, transmission (manual vs auto),
factory paint, HUD/vented seats/PDR all match sticker ground truth exactly.

Known limitation: Chevrolet sheets OMIT MagneRide even on equipped cars
(confirmed by diffing a sticker-verified MagneRide C8 against a non-MagneRide
one — no distinguishing field). Cadillac sheets do carry it (F55 "MAGNETO
RHEOLOGICAL"). Absence of an option on a build sheet is therefore NOT treated
as definitive — parsed sheets never set sticker_complete.

This is an unofficial endpoint on a retailer's site: usage stays polite
(3 s between requests, cache-forever per VIN, per-run cap in the pipeline)
and every call is gated + recorded by BudgetGuard.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from . import db
from .budget import BUILDSHEET, BudgetGuard

log = logging.getLogger("carfinder.buildsheet")

FOUND = "found"
NOT_FOUND = "not_found"
BLOCKED = "blocked"  # transient (throttled/edge mood) — retry a later run

_CODE_DESC = re.compile(r"^([A-Z0-9]{2,3})-(.+)$")
_PAINT = re.compile(r"^(G[A-Z0-9]{2})-PRIMARY COLOR EXTERIOR,\s*(.+?)(?:\s*-?\d?\s*\(.*\))?$")
_LEAD_YEAR = re.compile(r"^(20\d{2})\b")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept": "application/json",
    "site": "GPG",
}


@dataclass
class BuildSheet:
    """Parsed factory build sheet: verified evidence, absence NOT definitive."""

    vin: str
    year: int | None = None
    model_string: str | None = None  # e.g. "CT4 V-Series Notchback-Blackwing"
    transmission: str | None = None  # e.g. "6-Speed Manual Transmission, Tremec"
    engine: str | None = None
    production_date: str | None = None
    paint_rpo: str | None = None  # e.g. "GBA"
    paint_name: str | None = None  # e.g. "BLACK"
    options: set[str] = field(default_factory=set)  # broadcast codes
    text: str = ""  # all descriptors, for sticker_text-style regexes


def parse_response(raw: dict, vin: str) -> BuildSheet | None:
    """Parse a decode-vin-attributes payload. None when the VIN is unknown."""
    infos = (raw.get("data") or {}).get("vinInfos") or []
    if not infos:
        return None
    info = infos[0]
    major = {a.get("name"): a.get("desc") for a in info.get("majorAttribute") or []}
    vinfo = {a.get("name"): a.get("desc") for a in info.get("vehicleInformation") or []}

    codes: set[str] = set()
    lines: list[str] = []
    paint_rpo = paint_name = None
    for s in info.get("specification") or []:
        for part in (s.get("desc") or "").split(";"):
            part = part.strip()
            m = _CODE_DESC.match(part)
            if not m:
                continue
            codes.add(m.group(1))
            lines.append(f"{m.group(1)} {m.group(2)}")
            pm = _PAINT.match(part)
            if pm:
                paint_rpo, paint_name = pm.group(1), pm.group(2).strip()

    vehicle_line = info.get("vehicleInfo") or ""
    ym = _LEAD_YEAR.match(vehicle_line)
    text = "\n".join(
        [vehicle_line, *(v for v in major.values() if v), *lines],
    ).upper()
    return BuildSheet(
        vin=vin,
        year=int(ym.group(1)) if ym else None,
        model_string=major.get("Model String"),
        transmission=major.get("Transmission"),
        engine=major.get("Engine"),
        production_date=vinfo.get("Productiondate"),
        paint_rpo=paint_rpo,
        paint_name=paint_name,
        options=codes,
        text=text,
    )


def vehicle_kwargs(sheet: BuildSheet) -> dict:
    """Vehicle fields from a build sheet. sticker_complete stays False:
    GM sheets omit some real options (MagneRide on Chevrolet), so absence
    here is never evidence of absence."""
    return {
        "vin": sheet.vin,
        "year": sheet.year,
        "verified_rpo": sheet.options,
        "verified_text": sheet.text,
        "sticker_complete": False,
        "paint_rpo": sheet.paint_rpo,
        "color_text": sheet.paint_name,
    }


@dataclass(frozen=True)
class FetchResult:
    vin: str
    status: str  # FOUND | NOT_FOUND | BLOCKED
    sheet: BuildSheet | None
    from_cache: bool


class BuildSheetFetcher:
    """Fetch + cache build sheets, one VIN at a time, budget-gated."""

    BASE_URL = "https://www.gmpartsgiant.com"
    PATH = "/api/vehicle/mul/decode-vin-attributes"

    def __init__(
        self,
        conn: sqlite3.Connection,
        guard: BudgetGuard,
        client: httpx.Client | None = None,
        min_interval_s: float = 3.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._conn = conn
        self._guard = guard
        self._client = client or httpx.Client(
            base_url=self.BASE_URL,
            headers={**_HEADERS, "guid": str(uuid4())},
            timeout=30.0,
        )
        self._min_interval = min_interval_s
        self._sleep = sleep
        self._last_request_at: float | None = None

    def _cached(self, vin: str) -> FetchResult | None:
        row = db.get_build_sheet(self._conn, vin)
        if row is None:
            return None
        if row["status"] == FOUND:
            sheet = parse_response(json.loads(row["raw_json"]), vin)
            return FetchResult(vin, FOUND, sheet, from_cache=True)
        if row["status"] == NOT_FOUND:
            return FetchResult(vin, NOT_FOUND, None, from_cache=True)
        return None  # BLOCKED is transient — retry on a later run

    def is_cached(self, vin: str) -> bool:
        return self._cached(vin.strip().upper()) is not None

    def _rate_limit(self) -> None:
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self._min_interval:
                self._sleep(self._min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def fetch(self, vin: str, run_id: str | None = None) -> FetchResult:
        vin = vin.strip().upper()
        cached = self._cached(vin)
        if cached is not None:
            return cached

        self._rate_limit()
        now = datetime.now(UTC)
        try:
            with self._guard.reserve(BUILDSHEET, "decode-vin", run_id=run_id):
                resp = self._client.post(self.PATH, json={"vin": vin})
        except httpx.HTTPError as exc:
            log.warning("build sheet fetch failed for %s: %s", vin, exc)
            db.record_build_sheet(self._conn, vin, BLOCKED, None, now)
            return FetchResult(vin, BLOCKED, None, from_cache=False)

        if resp.status_code == 200:
            try:
                raw = resp.json()
            except ValueError:
                raw = None
            sheet = parse_response(raw, vin) if isinstance(raw, dict) else None
            if sheet is not None:
                db.record_build_sheet(
                    self._conn, vin, FOUND, json.dumps(raw, separators=(",", ":")), now
                )
                return FetchResult(vin, FOUND, sheet, from_cache=False)
            # Well-formed 200 with no vinInfos = the catalog doesn't know
            # this VIN — a definitive miss, cached so we never re-ask.
            if isinstance(raw, dict) and "data" in raw:
                db.record_build_sheet(self._conn, vin, NOT_FOUND, None, now)
                return FetchResult(vin, NOT_FOUND, None, from_cache=False)
            db.record_build_sheet(self._conn, vin, BLOCKED, None, now)
            return FetchResult(vin, BLOCKED, None, from_cache=False)
        if resp.status_code == 400:
            db.record_build_sheet(self._conn, vin, NOT_FOUND, None, now)
            return FetchResult(vin, NOT_FOUND, None, from_cache=False)
        # 403/429/5xx: edge throttling or mood — transient, never a false miss.
        log.warning("build sheet HTTP %d for %s — treating as blocked", resp.status_code, vin)
        db.record_build_sheet(self._conn, vin, BLOCKED, None, now)
        return FetchResult(vin, BLOCKED, None, from_cache=False)
