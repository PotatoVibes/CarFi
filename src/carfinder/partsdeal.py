"""BMW / Toyota per-VIN catalog decode (bmwpartsdeal.com, toyotapartsdeal.com).

Same operator and API as the GM build-sheet source (gmpartsgiant) — verified
live 2026-07-24: ``POST /api/vehicle/mul/decode-vin-attributes`` with
``{"vin": ...}``, identical response envelope. Unlike GM, these brands return
NO per-VIN broadcast data (``specification: []`` on every fixture): the
payload is a parts-CATALOG lookup — which catalog bucket the VIN falls in —
not factory order data for the specific car.

Provenance ruling — CLAIMED, never verified (fixture evidence, 2026-07-24):
the 2023 A91-MT Edition WZ1DB0G06PW056034 (factory 6-speed MANUAL) and the
2023 3.0 Premium WZ1DB0G07PW059282 (automatic) return byte-identical catalog
attributes: both say ``Auto or Manual Trans: ATM-Automatic`` AND ``Gear Shift
Type: 6F-6-Speed Manual Floor Shift`` (each field wrong for one of the two
cars), both say ``Trim Level: BASE`` (wrong for the Premium), same
``Model Code: DB07L-ZULA``. Toyota's 2023+ catalog collapses every 3.0 into
one entry; per-car truth does not survive. Treating any of it as verified
would have hard-failed real manual cars and verified-passed automatics.

So: the decode contributes to ``claimed_text`` only — it can raise a soft
score or route a hard requirement to needs_review, but can NEVER satisfy or
fail one. The self-contradictory transmission attributes are dropped from the
text entirely (worthless even as claims). What remains useful per VIN:
production date and frame number (genuinely per-car), engine model as a
claimed cross-check, and the catalog trim/model codes for corpus study.

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


# Catalog attributes proven self-contradictory on real 2023+ Supras (see
# module docstring): dropped from the claimed text AND never parsed into
# fields a matcher could reach. The optionalInfo line repeats them, so it is
# dropped wholesale too.
_UNTRUSTWORTHY_ATTRS = re.compile(r"trans|gear shift", re.IGNORECASE)


def parse_response(raw: dict, vin: str) -> BuildSheet | None:
    """Parse a decode-vin-attributes payload into a thin, CLAIMED-only sheet.

    Returns None when the catalog doesn't know the VIN (``vinInfos: []``).
    Attribute key names are fixture-pinned per brand (tests/fixtures/
    partsdeal) but looked up by substring to tolerate drift; every kept
    attribute lands in ``text`` as a ``NAME VALUE`` line for downstream
    claimed-text matching.
    """
    infos = (raw.get("data") or {}).get("vinInfos") or []
    if not infos:
        return None
    info = infos[0]

    major = _attr_pairs(info, "majorAttribute")
    vinfo = _attr_pairs(info, "vehicleInformation")
    kept = [(n, d) for n, d in major + vinfo if not _UNTRUSTWORTHY_ATTRS.search(n)]

    def find(*needles: str) -> str | None:
        for name, desc in kept:
            low = name.lower()
            if any(n in low for n in needles):
                return desc
        return None

    vehicle_line = str(info.get("vehicleInfo") or "")
    ym = _LEAD_YEAR.match(vehicle_line)
    text = "\n".join([vehicle_line, *(f"{n} {d}" for n, d in kept)]).upper()

    return BuildSheet(
        vin=vin,
        year=int(ym.group(1)) if ym else None,
        # Toyota's catalog model code ("DB07L-ZULA") is the closest thing to
        # a model string; BMW payloads fall back to the vehicle line.
        model_string=find("model code") or (vehicle_line or None),
        transmission=None,  # catalog trans data is disproven — never surfaced
        engine=find("engine"),
        production_date=find("production"),
        paint_rpo=None,  # neither brand returns paint through this API
        paint_name=None,
        options=set(),  # no factory option codes for these brands
        text=text,
    )


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
