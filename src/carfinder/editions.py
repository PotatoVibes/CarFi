"""Factory special editions — deterministic identification. Pure, zero I/O.

A special edition is a named, numbered/limited FACTORY package: the 2022 IMSA
GTLM Championship C8.R Edition, the 2023 70th Anniversary, the 2023
Watkins Glen / Sebring / Road Atlanta IMSA Blackwings, the 2023 A91-MT
Supra... Not standard commemorative content every car of a year carries
(2024's "20TH ANNIVERSARY OF V-SERIES" sticker line is on 38/38 Blackwings),
not appearance packages, not dealer "editions". One is worth a flat
scorer.EDITION_BONUS, added OUTSIDE the highlight cap.

Evidence precedence mirrors scorer.match():
  1. Edition RPO code on the window sticker / build sheet (verified).
  2. Edition line in the window-sticker text (verified).
  3. VIN serial block — GM numbers some editions in their own serial range;
     the VIN is the physical car, so this is verified too (checked after the
     factory documents only so the evidence shown names the option code).
  4. A complete sticker with none of the above is a verified "no" — a
     listing's claim can't override the factory document.
  5. Listing headline text (claimed): trim, heading, version, listing URL
     slug, exterior paint name — see listing_text() for the field policy.

Every rule is gated by a VIN scope pinning model line + model year (pos 10):
GM reuses option codes across years (CFY marks the 5 IMSA cars in 2022 but
357 non-edition Stingrays in 2023), CT4 ZL* codes are generic per-model
"designer theme" slots, and phrases like "IMSA" or "Launch Edition" name
editions of other models.

Catalog sourced 2026-10-02 from press releases / order guides / Toyota
pricing sheets, then audited against the live corpus (1,083 stickers, 649
build sheets, 15,658 listings: zero false positives per rule). Per-edition
evidence, counts, and rejected lookalikes: docs/special_editions.md.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote_plus

from .models import Provenance, Vehicle

# Between words in listing text: spaces, punctuation, URL-slug dashes. (%-escapes
# and underscores are normalized to spaces by listing_text() first — "\b" would
# otherwise see "%20A91" or "_a91" as one word.) One character class, so a long
# run of punctuation can't backtrack exponentially.
_SEP = r"[\W_]*"


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


@dataclass(frozen=True)
class Edition:
    key: str
    label: str
    note: str  # one-line production fact, shown with the evidence
    scope: re.Pattern[str]  # VIN: model line + model year this edition exists for
    rpo: frozenset[str] = frozenset()
    sticker_text: re.Pattern[str] | None = None
    vin_serial: re.Pattern[str] | None = None
    listing_text: re.Pattern[str] | None = None


@dataclass(frozen=True)
class EditionMatch:
    edition: Edition
    provenance: Provenance
    evidence: str


# --- Catalog ---------------------------------------------------------------

# C8 Stingray 3LT coupe/convertible with the LT2 (vin.py: pos 5 C = 3LT,
# pos 6 2/3 = body, pos 8 4 = LT2). All three editions were 3LT-only, so a
# 1LT/2LT car whose listing mentions "70th Anniversary" (every 2023 carries
# 70th badging) can't trip the claimed path.
_C8_3LT = r"^1G1YC[23]D4[0-9X]"

# CT4-V Blackwing (vin.py: pos 8 P = LF4 3.6TT; pos 5 6/7 = 6MT/10AT, L =
# 2023's unified series). Cadillac numbers its editions in pos 12-17: '5',
# then 1 (auto) / 6 (manual), then the edition digit, then the plaque number
# (GM-published plaque format "4-5[1|6][1|2|3][001-066]" for the 2024s).
# Corpus: 29/29 block VINs with factory data are editions (4 more have none)
# and 0 of 385 regular Blackwings use pos 12 = '5' (they use '4'; pilot cars
# '2'). The block is complete AND definitive, so these rules carry no
# listing-text path — a claim from outside the block could only be wrong.
_BW = r"^1G6D[67L]5RP[0-9X]"

_CATALOG: dict[str, tuple[Edition, ...]] = {
    "c8_stingray": (
        Edition(
            key="c8_imsa_gtlm_c8r",
            label="IMSA GTLM Championship C8.R Edition",
            note="2022, 1,000 built, Hypersonic Gray or Accelerate Yellow",
            scope=_rx(_C8_3LT + "N"),
            rpo=frozenset({"ZCR"}),
            sticker_text=_rx(r"IMSA\s+GTLM|C8\.?R\s+(?:SPECIAL\s+)?EDITION"),
            listing_text=_rx(
                rf"\bi\.?m\.?s\.?a\.?{_SEP}(?:gtlm|championship|edition)\b|\bgtlm\b"
                rf"|\bc8{_SEP}r{_SEP}(?:special{_SEP})?edition\b"
                rf"|\bchampionship{_SEP}(?:c8{_SEP}r{_SEP})?edition\b"
            ),
        ),
        Edition(
            key="c8_70th_anniversary",
            label="70th Anniversary Edition",
            note="2023, 4,579 Stingrays, White Pearl or Carbon Flash",
            scope=_rx(_C8_3LT + "P"),
            rpo=frozenset({"Y70"}),
            # Not bare "70TH ANNIVERSARY": the $460 "70th Anniversary indoor
            # car cover" accessory sits on ordinary 2023 stickers.
            sticker_text=_rx(r"70TH\s+ANNIVERSARY\s+SPECIAL"),
            # Own serial sequence 500001+ (regular 2023s run 1xxxxx): 31/31
            # with factory data are Y70. ~9 built on pilot VINs fall outside
            # it — Y70 still catches those.
            vin_serial=_rx(_C8_3LT + r"P550\d{4}$"),
            listing_text=_rx(
                rf"\b70th{_SEP}anniv(?:ersary)?{_SEP}(?:special{_SEP})?"
                r"(?:edition|ed|pkg|package)\b"
            ),
        ),
        Edition(
            key="c8_stars_and_steel",
            label="Stars & Steel Limited Edition",
            note="2026, 250 built across the C8 line, America-250 graphics",
            scope=_rx(_C8_3LT + "T"),
            rpo=frozenset({"USA"}),
            sticker_text=_rx(r"STARS\s+(?:AND|&)\s+STEEL"),
            listing_text=_rx(rf"\bstars{_SEP}(?:(?:and|n|amp){_SEP})?steel\b"),
        ),
    ),
    "ct4v_blackwing": (
        Edition(
            key="ct4v_watkins_glen_imsa",
            label="Watkins Glen IMSA Edition",
            note="2023, 99 built, Electric Blue",
            scope=_rx(_BW + "P"),
            rpo=frozenset({"ZLE"}),
            sticker_text=_rx(r"WATKINS\s+GLEN\s+IMSA"),
            vin_serial=_rx(_BW + r"P05[16]10\d\d$"),
        ),
        Edition(
            key="ct4v_sebring_imsa",
            label="Sebring IMSA Edition",
            note="2023, 99 built, Maverick Noir Frost",
            scope=_rx(_BW + "P"),
            rpo=frozenset({"ZLD"}),
            sticker_text=_rx(r"SEBRING\s+IMSA"),
            vin_serial=_rx(_BW + r"P05[16]60\d\d$"),
        ),
        Edition(
            key="ct4v_road_atlanta_imsa",
            label="Road Atlanta IMSA Edition",
            note="2023, 99 built, Rift Metallic",
            scope=_rx(_BW + "P"),
            rpo=frozenset({"ZLG"}),
            sticker_text=_rx(r"ROAD\s+ATLANTA\s+IMSA"),
            vin_serial=_rx(_BW + r"P05[16]80\d\d$"),
        ),
        Edition(
            key="ct4v_arrival",
            label="Arrival Edition",
            note="2024 Mondrian series, 66 built, Mercury Silver",
            scope=_rx(_BW + "R"),
            rpo=frozenset({"ZLK"}),
            sticker_text=_rx(r"BLACKWING\s+ARRIVAL"),
            vin_serial=_rx(_BW + r"R05[16]10(?:[0-5]\d|6[0-6])$"),
        ),
        Edition(
            key="ct4v_impact",
            label="Impact Edition",
            note="2024 Mondrian series, 66 built, Black Raven",
            scope=_rx(_BW + "R"),
            rpo=frozenset({"ZLJ"}),
            sticker_text=_rx(r"BLACKWING\s+IMPACT"),
            vin_serial=_rx(_BW + r"R05[16]20(?:[0-5]\d|6[0-6])$"),
        ),
        Edition(
            key="ct4v_elevation",
            label="Elevation Edition",
            note="2024 Mondrian series, 66 built, Velocity Red",
            scope=_rx(_BW + "R"),
            rpo=frozenset({"ZLR"}),
            # The sticker wraps "EDITION" onto the next line — don't require it.
            sticker_text=_rx(r"BLACKWING\s+ELEVATION"),
            vin_serial=_rx(_BW + r"R05[16]30(?:[0-5]\d|6[0-6])$"),
        ),
        Edition(
            key="ct4v_petit_pataud",
            label="Petit Pataud Edition",
            note="2025 Le Mans tribute, 50 built, Magnus Metal Frost",
            scope=_rx(_BW + "S"),
            rpo=frozenset({"ZLZ"}),
            sticker_text=_rx(r"PETITE?\s+PATAUD"),
            vin_serial=_rx(_BW + r"S05[16]00(?:[0-4]\d|50)$"),
        ),
    ),
    # BMW and Toyota have no free window-sticker source and the VIN carries
    # no edition signal (serials interleave with regular production), so these
    # are claimed-only: listing text, never verified.
    "bmw_m2": (
        Edition(
            key="m2_turbo_design_edition",
            label="Turbo Design Edition",
            note="2026-27, ~300 built, Alpine White with 2002 Turbo stripes",
            # pos 4-8 23DM0 (RWD; 13DM0 is 2023-24, 33DM0 the CS) or 43DM0
            # (2027 xDrive), pos 10 T/V = 2026/2027.
            scope=_rx(r"^3MF[24]3DM0[0-9X][TV]"),
            # Requires "M2 turbo design/edition", "M2 TDE" or "turbo design
            # edition" — bare "turbo" is in every S58 description.
            listing_text=_rx(
                rf"\bm2{_SEP}(?:turbo{_SEP}(?:design|edition)|tde)\b"
                rf"|\bturbo{_SEP}design{_SEP}edition\b"
            ),
        ),
    ),
    # Supra scope: WZ1DB, pos 6 = engine (0 = 3.0 2021+, 4 = 3.0 2020), pos 7
    # = era (C 2020-22, G 2023+), pos 10 = model year. Edition-only paint
    # names count (Toyota product info: Burnout / CU Later Gray were A91-MT
    # only in 2023, Mikan Blast 45th only, Burnout / Undercover FE only).
    "gr_supra": (
        Edition(
            key="gr_supra_launch_edition",
            label="Launch Edition",
            note="2020, 1,500 built",
            scope=_rx(r"^WZ1DB4C[0-9A-Z][0-9X]L"),
            listing_text=_rx(rf"\blaunch{_SEP}edition\b"),
        ),
        Edition(
            key="gr_supra_a91_edition",
            label="A91 Edition",
            note="2021, 1,000 built, Refraction blue",
            scope=_rx(r"^WZ1DB0C[0-9A-Z][0-9X]M"),
            listing_text=_rx(rf"\ba91{_SEP}edition\b|\brefraction\b"),
        ),
        Edition(
            key="gr_supra_a91_cf_edition",
            label="A91-CF Edition",
            note="2022, 600 built, carbon-fiber aero",
            scope=_rx(r"^WZ1DB0C[0-9A-Z][0-9X]N"),
            listing_text=_rx(rf"\ba91{_SEP}cf\b"),
        ),
        Edition(
            key="gr_supra_a91_mt_edition",
            label="A91-MT Edition",
            note="2023, 500 built, 6-speed manual only",
            scope=_rx(r"^WZ1DB0G[0-9A-Z][0-9X]P"),
            listing_text=_rx(rf"\ba91{_SEP}m\W?t\b|\bcu{_SEP}later\b|\bburnout\b"),
        ),
        Edition(
            key="gr_supra_45th_anniversary",
            label="45th Anniversary Edition",
            note="2024, 900 built, Absolute Zero or Mikan Blast",
            scope=_rx(r"^WZ1DB0G[0-9A-Z][0-9X]R"),
            listing_text=_rx(rf"\b45(?:th)?{_SEP}anniv|\bmikan\b"),
        ),
        Edition(
            key="gr_supra_mkv_final_edition",
            label="MkV Final Edition",
            note="2026, last-year GT4-style send-off",
            scope=_rx(r"^WZ1DB0G[0-9A-Z][0-9X]T"),
            # "Final Edition", never bare "final": 2026 IS the last MkV year,
            # so "MkV final year!" is ordinary dealer copy on a 3.0 Premium.
            listing_text=_rx(
                rf"\bfinal{_SEP}edition\b|\bgt4{_SEP}style\b|\bburnout\b|\bundercover\b"
            ),
        ),
    ),
}


def catalog(model_key: str) -> tuple[Edition, ...]:
    return _CATALOG.get(model_key, ())


def detect(model_key: str, vehicle: Vehicle) -> EditionMatch | None:
    """The special edition this car is, if any, with the best provenance."""
    vin = (vehicle.vin or "").strip().upper()
    candidates = [e for e in catalog(model_key) if e.scope.match(vin)]
    if not candidates:
        return None

    # Verified evidence first, every candidate, before any claim is weighed.
    for e in candidates:
        codes = sorted(e.rpo & vehicle.verified_rpo)
        if codes:
            return EditionMatch(e, Provenance.verified, f"factory option code {codes[0]}")
    for e in candidates:
        if e.sticker_text is not None and e.sticker_text.search(vehicle.verified_text):
            return EditionMatch(e, Provenance.verified, "named on the window sticker")
    for e in candidates:
        if e.vin_serial is not None and e.vin_serial.match(vin):
            return EditionMatch(e, Provenance.verified, "VIN in the edition's factory serial block")

    # A complete factory sticker without the edition is a definitive "no".
    if vehicle.sticker_complete:
        return None

    for e in candidates:
        hit = e.listing_text.search(vehicle.listing_text) if e.listing_text else None
        if hit:
            return EditionMatch(e, Provenance.claimed, f'listing says "{hit.group(0).strip()}"')
    return None


# --- Listing text ----------------------------------------------------------

# Listing fields that can name a car's edition, per source. Deliberately NOT
# dealer name/address/city ("Alan Jay Cadillac of Sebring", "Burnout Garage",
# "2101 45th Street"), photo URLs (hex hashes), or long descriptions / pasted
# page text (similar-vehicle widgets name other cars).
_HEADLINE_FIELDS: dict[str, tuple[str, ...]] = {
    "marketcheck": ("heading", "build.trim", "build.version", "vdp_url", "exterior_color"),
    "autodev": ("vehicle.trim", "vehicle.exteriorColor", "retailListing.vdp"),
    "manual": ("url",),
}


def listing_text(rows: Iterable[tuple[str, dict[str, Any]]], make: str | None) -> str:
    """Edition-relevant claimed text from a VIN's listings rows.

    ``rows`` are (source, raw listing payload) pairs — every source's row, not
    the merged vehicles row, which is last-writer-wins and loses edition names
    (a Marketcheck "A91-MT Edition" trim overwritten by Auto.dev's "3.0").
    Auto.dev's decoded trim is skipped for non-GM makes: it is a decoder
    default there — every 2026 Supra reads "MkV Final Edition", proven wrong
    on a real 3.0 Premium.
    """
    parts: list[str] = []
    for source, raw in rows:
        for path in _HEADLINE_FIELDS.get(source, ()):
            if source == "autodev" and path == "vehicle.trim" and make != "gm":
                continue
            value = _lookup(raw, path)
            if isinstance(value, str) and value.strip():
                # URL slugs: "GR%20Supra-3.0%20A91-MT" / "a91_mt_edition" -> words.
                parts.append(unquote_plus(value.strip()).replace("_", " "))
    return " | ".join(parts)


def _lookup(raw: dict[str, Any], path: str) -> Any:
    """A flat dot-key (Auto.dev's `select` output) or a nested dict path."""
    if path in raw:
        return raw[path]
    cur: Any = raw
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur
