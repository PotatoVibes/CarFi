"""Window sticker PDF -> verified attributes.

GM stickers embed a machine-readable JSON blob (maker, model_year, vin, and
the complete factory RPO option list) alongside the human-readable layout.
That blob is the primary source for verified RPO codes; the extracted page
text is kept for sticker-text regex matching, paint, trim, and MSRP. The
two-column layout scrambles text ordering, so line-oriented regexes stay
deliberately loose.

Confidence is the fraction of core signals recovered; below threshold the
sticker must NOT be treated as complete (absence of an option is then not
evidence of absence) — those cars get flagged instead of guessed at.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

log = logging.getLogger("carfinder.sticker_parser")

# A parse at or above this confidence marks the sticker "complete": the
# verified option list is authoritative and absence means absent.
COMPLETE_THRESHOLD = 0.8

# Bump whenever parsing logic changes — invalidates the parsed_stickers cache
# so every PDF gets re-parsed with the new rules.
PARSER_VERSION = 1

_JSON_START = re.compile(r'\{"maker"')
_RPO_TOKEN = re.compile(r'"([A-Z0-9]{2,3})"')
_MSRP = re.compile(r"TOTAL VEHICLE PRICE\*?\s*\$\s*([\d,]+(?:\.\d{2})?)")
_TOTAL_WITH_OPTIONS = re.compile(r"TOTAL VEHICLE & OPTIONS\s*\$\s*([\d,]+(?:\.\d{2})?)")
_YEAR_TITLE = re.compile(r"\b(20[2-9]\d)\b")
# Column merge glues the engine column onto the paint line. Cadillac prints
# "EXTERIOR: BLACK RAVEN ENGINE, 3.6L ..." (label first); Chevy prints
# "EXTERIOR: BLACK 6.2L V8 DI ENGINE" (displacement first) — cut at either.
_COLUMN_CUT = r"(?:\s+(?:ENGINE|TRANSMISSION|INTERIOR|\d+\.\d+L)\b|$)"
_EXTERIOR = re.compile(r"EXTERIOR:\s*(.+?)" + _COLUMN_CUT)
_INTERIOR = re.compile(r"INTERIOR:\s*(.+?)" + _COLUMN_CUT)


@dataclass
class ParsedSticker:
    vin: str | None = None
    year: int | None = None
    maker: str | None = None
    model_code: str | None = None  # GM merchandising model code, e.g. 6DF69
    title_line: str | None = None  # e.g. "2022 CT4 V-SERIES BLACKWING"
    exterior: str | None = None
    interior: str | None = None
    msrp: float | None = None  # TOTAL VEHICLE PRICE (incl. destination)
    options: set[str] = field(default_factory=set)  # factory RPO codes
    text: str = ""  # full extracted text (for sticker_text regexes)
    confidence: float = 0.0

    @property
    def complete(self) -> bool:
        return self.confidence >= COMPLETE_THRESHOLD

    def to_json(self) -> str:
        d = self.__dict__ | {"options": sorted(self.options)}
        return json.dumps(d, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> ParsedSticker:
        d = json.loads(raw)
        d["options"] = set(d.get("options", []))
        return cls(**d)


def _extract_json_blob(text: str) -> dict | None:
    """Locate and parse the embedded vehicle-data JSON."""
    m = _JSON_START.search(text)
    if m is None:
        return None
    # The blob ends at the first ']}' after the Options array opens.
    tail = text[m.start() :]
    end = tail.find("]}")
    if end == -1:
        return None
    blob = tail[: end + 2]
    # pdfplumber inserts line breaks between JSON tokens; those are legal
    # whitespace. Breaks *inside* a quoted token would break json.loads, so
    # fall back to token harvesting below if that ever happens.
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        log.warning("embedded JSON blob failed to parse; harvesting tokens")
        options = [t for t in _RPO_TOKEN.findall(blob) if t]
        vin_m = re.search(r'"vin"\s*:\s*"([A-HJ-NPR-Z0-9]{17})"', blob)
        year_m = re.search(r'"model_year"\s*:\s*"(\d{4})"', blob)
        return {
            "Options": options,
            "vin": vin_m.group(1) if vin_m else None,
            "model_year": year_m.group(1) if year_m else None,
        }


def _money(match: re.Match[str] | None) -> float | None:
    return float(match.group(1).replace(",", "")) if match else None


def parse_sticker(path: str | Path) -> ParsedSticker:
    """Parse one cached sticker PDF into verified attributes."""
    with pdfplumber.open(path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    parsed = ParsedSticker(text=text)

    blob = _extract_json_blob(text)
    if blob is not None:
        parsed.vin = blob.get("vin") or None
        parsed.maker = blob.get("maker") or None
        parsed.model_code = blob.get("mmc_code") or None
        year_raw = blob.get("model_year")
        parsed.year = int(year_raw) if year_raw and str(year_raw).isdigit() else None
        parsed.options = {c for c in blob.get("Options", []) if c}

    if parsed.year is None:
        m = _YEAR_TITLE.search(text)
        parsed.year = int(m.group(1)) if m else None

    ext = _EXTERIOR.search(text)
    parsed.exterior = ext.group(1).strip() if ext else None
    inte = _INTERIOR.search(text)
    parsed.interior = inte.group(1).strip() if inte else None
    # The title line is the one containing the model year + EXTERIOR label.
    for line in text.splitlines():
        if "EXTERIOR:" in line:
            parsed.title_line = line.split("EXTERIOR:")[0].strip() or line.strip()
            break

    parsed.msrp = _money(_MSRP.search(text)) or _money(_TOTAL_WITH_OPTIONS.search(text))

    # Confidence: fraction of core signals recovered.
    signals = [
        bool(parsed.options),
        parsed.vin is not None,
        parsed.year is not None,
        parsed.msrp is not None,
        parsed.exterior is not None,
    ]
    parsed.confidence = sum(signals) / len(signals)
    if not parsed.complete:
        log.warning(
            "sticker %s parsed at low confidence %.2f — flagged, not trusted as complete",
            path,
            parsed.confidence,
        )
    return parsed


def vehicle_kwargs(parsed: ParsedSticker, model_key: str) -> dict:
    """Map a parsed sticker onto Vehicle fields (see models.Vehicle).

    paint_rpo is recovered by intersecting the factory option list with the
    model's paint table — the paint code ships inside the options array.
    """
    from .colors import paint_codes

    paint_rpo = next((c for c in parsed.options if c in paint_codes(model_key)), None)
    return {
        "vin": parsed.vin,
        "year": parsed.year,
        "verified_rpo": parsed.options,
        "verified_text": parsed.text,
        "sticker_complete": parsed.complete,
        "paint_rpo": paint_rpo,
        "color_text": parsed.exterior,
    }
