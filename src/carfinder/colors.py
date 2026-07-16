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
    "GBA": ColorFamily.black,  # Black              # VERIFY
    "GAR": ColorFamily.black,  # Carbon Flash Met.  # VERIFY
    # gray / silver
    "GXD": ColorFamily.gray,  # Ceramic Matrix Gray # VERIFY
    "GA7": ColorFamily.gray,  # Hypersonic Gray     # VERIFY
    "G9K": ColorFamily.silver,  # Silver Flare      # VERIFY
    # yellow / orange
    "GCK": ColorFamily.yellow,  # Accelerate Yellow # VERIFY
    "GXH": ColorFamily.orange,  # Amplify Orange    # VERIFY
    "GB8": ColorFamily.orange,  # Sebring Orange    # VERIFY
    # brown
    "G26": ColorFamily.brown,  # Caffeine          # VERIFY
    "G1W": ColorFamily.brown,  # Zeus Bronze        # VERIFY
}

_CT4V_PAINT: dict[str, ColorFamily] = {
    "GSK": ColorFamily.blue,  # Electric Blue       # VERIFY
    "G1K": ColorFamily.blue,  # Wave Metallic (blue)# VERIFY
    "GKZ": ColorFamily.red,  # Velocity Red         # VERIFY
    "G7C": ColorFamily.red,  # Infrared Tintcoat    # VERIFY
    "GAZ": ColorFamily.white,  # Summit White       # VERIFY
    "G1W": ColorFamily.white,  # Crystal White Tri. # VERIFY
    "GBA": ColorFamily.black,  # Black Raven — VERIFIED (1G6D65RP8N0460286)
    "GLK": ColorFamily.gray,  # Rift Metallic (gray)# VERIFY
    "G9K": ColorFamily.gray,  # Shadow Metallic     # VERIFY
    "GNL": ColorFamily.silver,  # Radiant Silver    # VERIFY
}

_PAINT_TABLES: dict[str, dict[str, ColorFamily]] = {
    "c8_stingray": _C8_PAINT,
    "ct4v_blackwing": _CT4V_PAINT,
}

# Keyword -> family for the claimed text fallback. Order matters: earlier,
# more specific keywords win (e.g., "carbon flash" before "black").
_TEXT_KEYWORDS: list[tuple[str, ColorFamily]] = [
    ("CARBON FLASH", ColorFamily.black),
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
