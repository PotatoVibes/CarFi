"""VIN decoding -> verified facts, zero API calls.

GM's VDS encodes model line, trim series, and body style. Every mapping here
was derived empirically from the committed sticker corpus (53 PDFs, zero
contradictions — see tests/test_vin.py):

C8 Corvette (1G1Y…):
  pos 5 (trim series): A=1LT, B=2LT, C=3LT       (19+12+6 stickers)
  pos 6 (body):        2=coupe, 3=convertible     (28+9 stickers)
  pos 8 (engine):      4=LT2 6.2L (Stingray)      (37/37 stickers)
  Model is decoded ONLY when trim is A/B/C and engine is 4 — Z06/E-Ray use
  LZ trim series and other engine codes, for which we have no verified
  ground truth, so they deliberately decode to nothing and fall through to
  the window-sticker evidence chain.

CT4 (1G6D…):
  pos 8 (engine): P = LF4 3.6TT — Blackwing-only  (10/10 stickers)
                  L = L3B 2.7T — never a Blackwing (6/6 stickers)
  pos 5 (series, Blackwing only): 6 = 6MT manual, 7 = 10-speed automatic —
  GM certifies the two transmissions as separate configs. 6/6 consistent
  across the corpus (D65 manual 2022/2024/2025; D75 auto 2022/2024), with
  the 2023-only unified "DL5" series confirmed MIXED via per-VIN build
  sheets — DL5 deliberately decodes to nothing and the sticker/build sheet
  decides.

Pos 10 model-year codes follow the standard alphabet; the subset below is
title-verified in the corpus (N/P/R/S/T) plus M from a user-supplied 2021 car.

Facts are definitive physical identity — verified provenance. Absence of a
fact means "this VIN pattern is outside verified ground truth", never a guess.
"""

from __future__ import annotations

import re

_C8_TRIM = {"A": "1LT", "B": "2LT", "C": "3LT"}
_C8_BODY = {"2": "COUPE", "3": "CONVERTIBLE"}
_YEAR = {"L": 2020, "M": 2021, "N": 2022, "P": 2023, "R": 2024, "S": 2025, "T": 2026, "V": 2027}

# --- ISO 3779 validation (17 chars, no I/O/Q, position-9 check digit) -------

_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
_CHECK_VALUES = {
    **{str(d): d for d in range(10)},
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
}  # fmt: skip
_CHECK_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)


def validate(vin: str | None) -> str | None:
    """Full VIN validation. Returns an error message, or None when valid.

    North-American VINs (all GM cars this tool tracks) carry an ISO 3779
    check digit at position 9, so a typo'd VIN is caught before any
    fetching/scoring spends time or budget on it.
    """
    v = (vin or "").strip().upper()
    if len(v) != 17:
        return f"a VIN is 17 characters (got {len(v)})"
    if not _VIN_RE.match(v):
        return "VINs never contain I, O, or Q — check for typos"
    total = sum(_CHECK_VALUES[ch] * w for ch, w in zip(v, _CHECK_WEIGHTS, strict=True))
    expected = "X" if total % 11 == 10 else str(total % 11)
    if v[8] != expected:
        return "check digit mismatch — one of the characters is mistyped"
    return None


def decode(vin: str | None) -> dict[str, str]:
    """Verified facts from a VIN: subset of {model, trim, body}. Empty when
    the VIN is malformed or outside the verified mappings."""
    if not vin:
        return {}
    v = vin.strip().upper()
    if len(v) != 17:
        return {}

    facts: dict[str, str] = {}
    if v.startswith("1G1Y"):  # C8 Corvette
        trim = _C8_TRIM.get(v[4])
        body = _C8_BODY.get(v[5])
        if trim:
            facts["trim"] = trim
        if body:
            facts["body"] = body
        # LT trim series + LT2 engine = Stingray. (Z06/E-Ray: LZ series,
        # different engine char — decode to nothing, sticker decides.)
        if trim and v[7] == "4":
            facts["model"] = "STINGRAY"
    elif v.startswith("1G6D"):  # CT4 family
        if v[7] == "P":
            facts["model"] = "BLACKWING"
            # Blackwing series char: 6 = manual, 7 = automatic. 2023's
            # unified 'L' series covered both -> no fact, evidence decides.
            if v[4] == "6":
                facts["trans"] = "MANUAL"
            elif v[4] == "7":
                facts["trans"] = "AUTO"
        elif v[7] == "L":
            facts["model"] = "NON_BLACKWING"
    return facts


def model_year(vin: str | None) -> int | None:
    """Model year from VIN pos 10, or None outside the verified code range."""
    if not vin:
        return None
    v = vin.strip().upper()
    if len(v) != 17:
        return None
    return _YEAR.get(v[9])
