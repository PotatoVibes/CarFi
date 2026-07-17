"""Feature catalog: every factory option observed for a model, in plain
English, with prevalence — the discovery layer behind the config editor.

Descriptions come from cached build sheets (gmpartsgiant broadcast lines,
"CODE-DESCRIPTION"); prevalence is counted across build sheets AND parsed
window stickers, since both carry the option codes. Rare options are the
interesting differentiators; near-universal ones are standard equipment.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from .models import Profile

# Administrative broadcast codes that are never a shoppable "feature".
_NOISE = re.compile(
    r"PLANT CODE|COUNTRY |MARKETING AREA|ENGINEERING YEAR|VIN MODEL YEAR"
    r"|EMISSION|EVAPORATIVE|VEHICLE STATEMENT|SALES PROCESSING|FREQUENCIES"
    r"|LANGUAGE LABEL|OWNERS MANUAL|OWNER INFORMATION|VEHICLE TYPE|CONTROL SALES"
    r"|SALES ITEM|PACKAGE OPTION-|VEHICLE FUEL|REFRIGERANT|MODEL CONVERSION"
    r"|IDENTIFICATION-GM|MERCHANDISING OPTION\)$"
)

_CODE_DESC = re.compile(r"^([A-Z0-9]{2,3})-(.+)$")


@dataclass
class Feature:
    code: str
    description: str  # GM's broadcast description, title-cased
    count: int  # cars observed with this option
    total: int  # cars with any verified evidence
    status: str | None = None  # require | forbid | highlight | None
    bonus: float | None = None  # when status == highlight

    @property
    def prevalence(self) -> float:
        return 100.0 * self.count / self.total if self.total else 0.0


def _profile_code_status(profile: Profile) -> dict[str, tuple[str, float | None]]:
    """Map RPO code -> (status, bonus) for codes already used by the profile."""
    out: dict[str, tuple[str, float | None]] = {}
    for c in profile.hard:
        if c.matcher is not None:
            for code in c.matcher.rpo:
                out[code] = (c.kind, None)
    for h in profile.highlights:
        for code in h.matcher.rpo:
            out[code] = ("highlight", h.bonus)
    return out


def catalog(conn: sqlite3.Connection, profile: Profile) -> list[Feature]:
    """All options observed for a model, sorted rarest-first (differentiators
    on top, standard equipment at the bottom)."""
    model_key = profile.model_key

    # Descriptions + per-VIN option sets from build sheets.
    descriptions: dict[str, str] = {}
    per_vin: dict[str, set[str]] = {}
    for r in conn.execute(
        """SELECT b.vin, b.raw_json FROM build_sheets b
           JOIN vehicles v ON v.vin = b.vin
           WHERE v.model_key = ? AND b.status = 'found' AND b.raw_json IS NOT NULL""",
        (model_key,),
    ).fetchall():
        codes: set[str] = set()
        raw = json.loads(r["raw_json"])
        infos = (raw.get("data") or {}).get("vinInfos") or []
        for s in (infos[0].get("specification") or []) if infos else []:
            for part in (s.get("desc") or "").split(";"):
                m = _CODE_DESC.match(part.strip())
                if not m or _NOISE.search(m.group(2).upper()):
                    continue
                codes.add(m.group(1))
                descriptions.setdefault(m.group(1), m.group(2).strip())
        if codes:
            per_vin[r["vin"]] = codes

    # Sticker option sets add prevalence for the same codes.
    for r in conn.execute(
        """SELECT p.vin, p.parsed_json FROM parsed_stickers p
           JOIN vehicles v ON v.vin = p.vin
           WHERE v.model_key = ?""",
        (model_key,),
    ).fetchall():
        opts = set(json.loads(r["parsed_json"]).get("options", []))
        if opts:
            per_vin[r["vin"]] = per_vin.get(r["vin"], set()) | opts

    total = len(per_vin)
    counts: dict[str, int] = {}
    for codes in per_vin.values():
        for code in codes:
            if code in descriptions:  # only nameable features are shoppable
                counts[code] = counts.get(code, 0) + 1

    status = _profile_code_status(profile)
    feats = [
        Feature(
            code=code,
            description=descriptions[code].title(),
            count=n,
            total=total,
            status=status.get(code, (None, None))[0],
            bonus=status.get(code, (None, None))[1],
        )
        for code, n in counts.items()
    ]
    feats.sort(key=lambda f: (f.count, f.code))
    return feats
