"""BMW / Toyota per-VIN factory decode (bmwpartsdeal.com, toyotapartsdeal.com).

Same operator and API as the GM build-sheet source (gmpartsgiant) — verified
live 2026-07-24: ``POST /api/vehicle/mul/decode-vin-attributes`` with
``{"vin": ...}``, identical response envelope. Unlike GM, these brands return
NO factory option codes; the payload is a thin identity decode:

  BMW    — trim catalog id (e.g. "1959-Base"), engine, body. No transmission
           field, matching the VIN research: a G87's gearbox is simply not
           machine-verifiable from free sources today.
  Toyota — transmission (``ATM``/``MTM`` + speed), production date, engine
           model (B58/B48), and the full factory model code (katashiki,
           e.g. "DB43L-ZRLA"), which likely encodes trim — logged verbatim
           into the evidence text so a corpus-verified regex can graduate it
           into a matcher later.

Everything lands as identity-level VERIFIED evidence. Absence of a feature in
these payloads is NEVER evidence of absence (``sticker_complete`` stays False
downstream), exactly like GM build sheets.

Unofficial retailer endpoints: usage stays polite (3 s between requests,
cache-forever per VIN via the shared ``build_sheets`` table — a VIN belongs to
exactly one make, so the per-VIN cache is naturally partitioned) and every
call is gated + recorded by BudgetGuard under a per-make provider.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from . import db
from .budget import BMW_PARTSDEAL, TOYOTA_PARTSDEAL, BudgetGuard
from .buildsheet import BLOCKED, FOUND, NOT_FOUND, BuildSheet, FetchResult

log = logging.getLogger("carfinder.partsdeal")

_LEAD_YEAR = re.compile(r"^(20\d{2})\b")

# make -> (base_url, `site` header, BudgetGuard provider). The site code was
# confirmed working live but may be host-derived server-side; harmless either
# way (gmpartsgiant's GPG follows the same initials-of-host convention).
SITES: dict[str, tuple[str, str, str]] = {
    "bmw": ("https://www.bmwpartsdeal.com", "BPD", BMW_PARTSDEAL),
    "toyota": ("https://www.toyotapartsdeal.com", "TPD", TOYOTA_PARTSDEAL),
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def _attr_pairs(info: dict, key: str) -> list[tuple[str, str]]:
    """(name, desc) pairs from a vinInfos attribute list, blanks dropped."""
    return [
        (str(a.get("name") or "").strip(), str(a.get("desc") or "").strip())
        for a in info.get(key) or []
        if (a.get("desc") or "").strip()
    ]


def parse_response(raw: dict, vin: str) -> BuildSheet | None:
    """Parse a decode-vin-attributes payload into a (thin) BuildSheet.

    Returns None when the catalog doesn't know the VIN (``vinInfos: []``).
    Key names differ per brand and aren't pinned by fixtures yet, so lookups
    are substring-based and every attribute lands in ``text`` as an
    ``NAME VALUE`` line — sticker_text regexes see the whole payload either
    way, parsed fields are a convenience on top.
    """
    infos = (raw.get("data") or {}).get("vinInfos") or []
    if not infos:
        return None
    info = infos[0]

    major = _attr_pairs(info, "majorAttribute")
    vinfo = _attr_pairs(info, "vehicleInformation")

    def find(*needles: str) -> str | None:
        for name, desc in major + vinfo:
            low = name.lower()
            if any(n in low for n in needles):
                return desc
        return None

    vehicle_line = str(info.get("vehicleInfo") or "")
    ym = _LEAD_YEAR.match(vehicle_line)
    lines = [f"{n} {d}" for n, d in major + vinfo]
    text = "\n".join([vehicle_line, str(info.get("optionalInfo") or ""), *lines]).upper()

    return BuildSheet(
        vin=vin,
        year=int(ym.group(1)) if ym else None,
        # Toyota's katashiki ("Model Code DB43L-ZRLA") is the closest thing
        # to a model string; BMW payloads fall back to the vehicle line.
        model_string=find("model code") or (vehicle_line or None),
        transmission=find("trans"),
        engine=find("engine"),
        production_date=find("production"),
        paint_rpo=None,  # neither brand returns paint through this API
        paint_name=None,
        options=set(),  # no factory option codes for these brands
        text=text,
    )


def sheet_facts(make: str, sheet: BuildSheet) -> dict[str, str]:
    """Definitive identity facts derivable from a partsdeal decode.

    Toyota's ``Trans`` attribute is factory order data: ``MTM`` (manual) /
    ``ATM`` (automatic) — verified both ways, like a VIN char. BMW payloads
    carry no transmission, so no fact is ever derived (never guess).
    """
    if make != "toyota" or not sheet.transmission:
        return {}
    t = sheet.transmission.upper()
    if "MTM" in t or "MANUAL" in t:
        return {"trans": "MANUAL"}
    if "ATM" in t or "AUTOMATIC" in t:
        return {"trans": "AUTO"}
    return {}


class PartsDealFetcher:
    """Fetch + cache partsdeal VIN decodes, one VIN at a time, budget-gated.

    Mirrors BuildSheetFetcher: cache-forever per VIN in the shared
    ``build_sheets`` table, NOT_FOUND is definitive, BLOCKED is transient.
    """

    PATH = "/api/vehicle/mul/decode-vin-attributes"

    def __init__(
        self,
        conn: sqlite3.Connection,
        guard: BudgetGuard,
        make: str,
        client: httpx.Client | None = None,
        min_interval_s: float = 3.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if make not in SITES:
            raise ValueError(f"no partsdeal site for make {make!r}")
        base_url, site, provider = SITES[make]
        self.make = make
        self._provider = provider
        self._conn = conn
        self._guard = guard
        self._client = client or httpx.Client(
            base_url=base_url,
            headers={**_HEADERS, "site": site, "guid": str(uuid4())},
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
            with self._guard.reserve(self._provider, "decode-vin", run_id=run_id):
                resp = self._client.post(self.PATH, json={"vin": vin})
        except httpx.HTTPError as exc:
            log.warning("%s partsdeal fetch failed for %s: %s", self.make, vin, exc)
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
            # Well-formed 200 with no vinInfos = catalog doesn't know the VIN.
            if isinstance(raw, dict) and "data" in raw:
                db.record_build_sheet(self._conn, vin, NOT_FOUND, None, now)
                return FetchResult(vin, NOT_FOUND, None, from_cache=False)
            db.record_build_sheet(self._conn, vin, BLOCKED, None, now)
            return FetchResult(vin, BLOCKED, None, from_cache=False)
        if resp.status_code == 400:
            db.record_build_sheet(self._conn, vin, NOT_FOUND, None, now)
            return FetchResult(vin, NOT_FOUND, None, from_cache=False)
        log.warning(
            "%s partsdeal HTTP %d for %s — treating as blocked", self.make, resp.status_code, vin
        )
        db.record_build_sheet(self._conn, vin, BLOCKED, None, now)
        return FetchResult(vin, BLOCKED, None, from_cache=False)
