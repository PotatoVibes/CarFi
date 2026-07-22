"""Paint RPO -> color-family resolution (deterministic).

Per-model RPO lookup is the precise, verified path; a keyword classifier over
the listing color text is the claimed fallback. RPO codes here are seeded from
memory and marked ``# VERIFY`` — M2 corrects them against real window stickers.
The text classifier is the safety net either way, so a wrong code degrades to
a claimed match rather than a silent miss.
"""

from __future__ import annotations

from enum import StrEnum


class ColorFamily(StrEnum):
    blue = "blue"
    red = "red"
    white = "white"
    black = "black"
    gray = "gray"
    silver = "silver"
    yellow = "yellow"
    orange = "orange"
    green = "green"
    brown = "brown"
    other = "other"


# --- Per-model paint RPO -> family ----------------------------------------
# Codes are best-effort until M2 verification. Text classifier backs them up.

_C8_PAINT: dict[str, ColorFamily] = {
    # blue
    "GM0": ColorFamily.blue,  # Elkhart Lake Blue   # VERIFY
    "GKM": ColorFamily.blue,  # Rapid Blue          # VERIFY
    "G1M": ColorFamily.blue,  # Riptide Blue        # VERIFY
    # red
    "GKZ": ColorFamily.red,  # Torch Red            # VERIFY
    "G7C": ColorFamily.red,  # Red Mist Tintcoat    # VERIFY
    "GNE": ColorFamily.red,  # Long Beach Red       # VERIFY
    # white
    "GAZ": ColorFamily.white,  # Arctic White       # VERIFY
    # black
    "GBA": ColorFamily.black,  # Black — VERIFIED (1G1YC3D47P5121316, "BLACK")
    "GAR": ColorFamily.black,  # Carbon Flash Met.  # VERIFY
    # gray / silver
    "GXD": ColorFamily.gray,  # Ceramic Matrix Gray # VERIFY
    "GA7": ColorFamily.gray,  # Hypersonic Gray     # VERIFY
    "G9K": ColorFamily.silver,  # Silver Flare      # VERIFY
    # yellow / orange
    "GCK": ColorFamily.yellow,  # Accelerate Yellow # VERIFY
    "GXH": ColorFamily.orange,  # Amplify Orange    # VERIFY
    # GB8 removed 2026-07-22: seeded as "Sebring Orange", but zero corpus
    # occurrences and G26 is the code Sebring Orange stickers actually carry.
    # 2026-07-22 corpus audit (sticker exterior line x option G-code, all
    # VERIFIED): the earlier "Caffeine"/"Zeus Bronze" brown guesses were wrong.
    "G26": ColorFamily.orange,  # Sebring Orange (3 stickers)
    "G1W": ColorFamily.white,  # White Pearl Metallic (8 stickers)
    # other
    "GXL": ColorFamily.other,  # Hysteria Purple — VERIFIED (corpus sticker)
}

_CT4V_PAINT: dict[str, ColorFamily] = {
    # GSK was seeded as "Electric Blue" — WRONG: the one GSK sticker in the
    # corpus (1G6D65RPXN0460404, 2026-07-22) reads INFRARED TINTCOAT.
    # Electric Blue's real code is unknown; the text fallback covers it.
    "GSK": ColorFamily.red,  # Infrared Tintcoat — VERIFIED
    "GKZ": ColorFamily.red,  # Velocity Red         # VERIFY
    "G7C": ColorFamily.red,  # Infrared Tintcoat    # VERIFY
    "GAZ": ColorFamily.white,  # Summit White       # VERIFY
    "G1W": ColorFamily.white,  # Crystal White Tri. # VERIFY
    "GBA": ColorFamily.black,  # Black Raven — VERIFIED (1G6D65RP8N0460286)
    "G9K": ColorFamily.gray,  # Shadow Metallic     # VERIFY
    "GNL": ColorFamily.silver,  # Radiant Silver    # VERIFY
    # 2026-07-16 corpus audit (sticker exterior line x paint G-code, all
    # VERIFIED): the earlier GLK "Rift" and G1K "Wave" guesses were wrong.
    "GRW": ColorFamily.white,  # Rift Metallic (owner's call: white, not silver)
    "GAI": ColorFamily.blue,  # Deep Space Metallic (dark navy)
    "GAE": ColorFamily.gray,  # Drift Metallic
    "GNW": ColorFamily.black,  # Maverick Noir Frost (matte black)
    "GXU": ColorFamily.gray,  # Midnight Steel Metallic
    "GBW": ColorFamily.gray,  # Typhoon Metallic
    "GKK": ColorFamily.blue,  # Wave Metallic (light blue)
    "GCP": ColorFamily.yellow,  # Cyber Yellow Metallic (user-supplied)
}

_PAINT_TABLES: dict[str, dict[str, ColorFamily]] = {
    "c8_stingray": _C8_PAINT,
    "ct4v_blackwing": _CT4V_PAINT,
}

# Keyword -> family for the claimed text fallback. Order matters: earlier,
# more specific keywords win (e.g., "carbon flash" before "black").
_TEXT_KEYWORDS: list[tuple[str, ColorFamily]] = [
    ("CARBON FLASH", ColorFamily.black),
    # GM marketing names that carry no family word (2026-07-16 corpus audit).
    ("DEEP SPACE", ColorFamily.blue),
    # DRIFT before RIFT: "RIFT METALLIC" is a substring of "DRIFT METALLIC".
    ("DRIFT METALLIC", ColorFamily.gray),
    ("RIFT METALLIC", ColorFamily.white),
    ("TYPHOON", ColorFamily.gray),
    ("MAVERICK NOIR", ColorFamily.black),
    ("NOIR", ColorFamily.black),
    ("MIDNIGHT STEEL", ColorFamily.gray),
    ("WAVE METALLIC", ColorFamily.blue),
    ("HYSTERIA", ColorFamily.other),
    ("PURPLE", ColorFamily.other),
    # Dealer-feed shorthands seen in Marketcheck data (2026-07-22 audit).
    ("BURGUNDY", ColorFamily.red),
    ("MIST", ColorFamily.red),  # bare "Mist" = Red Mist (only GM Mist paint here)
    ("PINK", ColorFamily.other),
    ("BLUE", ColorFamily.blue),
    ("RED", ColorFamily.red),
    ("WHITE", ColorFamily.white),
    ("BLACK", ColorFamily.black),
    ("SILVER", ColorFamily.silver),
    ("GRAY", ColorFamily.gray),
    ("GREY", ColorFamily.gray),
    ("YELLOW", ColorFamily.yellow),
    ("ORANGE", ColorFamily.orange),
    ("GREEN", ColorFamily.green),
    ("BRONZE", ColorFamily.brown),
    ("BROWN", ColorFamily.brown),
    ("CAFFEINE", ColorFamily.brown),
]


# Approximate swatch colors for UI chips, keyed by family.
FAMILY_HEX: dict[str, str] = {
    "blue": "#2f5fb0",
    "red": "#b02f2f",
    "white": "#e8e8e8",
    "black": "#1c1c1c",
    "gray": "#6b7280",
    "silver": "#c0c4c8",
    "yellow": "#e0c030",
    "orange": "#d67a2c",
    "green": "#3a7d44",
    "brown": "#6b4f32",
    "other": "#9aa0a6",
}


def chip_hex(model_key: str, paint_rpo: str | None, color_text: str | None) -> str:
    """Best-effort swatch hex for a vehicle: paint RPO first, then text."""
    fam = family_from_rpo(model_key, paint_rpo) or family_from_text(color_text)
    return FAMILY_HEX.get(fam.value if fam else "other", FAMILY_HEX["other"])


def resolved_chip_hex(family: str | None, model_key: str, color_text: str | None) -> str:
    """Swatch hex preferring the scorer-resolved family (match_results), falling
    back to classifying the listing text for rows not yet (re)scored."""
    if family and family in FAMILY_HEX:
        return FAMILY_HEX[family]
    return chip_hex(model_key, None, color_text)


def paint_codes(model_key: str) -> set[str]:
    """All known paint RPO codes for a model (for options-list intersection)."""
    return set(_PAINT_TABLES.get(model_key, {}))


def family_from_rpo(model_key: str, paint_rpo: str | None) -> ColorFamily | None:
    if not paint_rpo:
        return None
    return _PAINT_TABLES.get(model_key, {}).get(paint_rpo.strip().upper())


def family_from_text(color_text: str | None) -> ColorFamily | None:
    if not color_text:
        return None
    upper = color_text.upper()
    for keyword, family in _TEXT_KEYWORDS:
        if keyword in upper:
            return family
    return None
