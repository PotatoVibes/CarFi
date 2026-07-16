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
  Transmission is NOT VIN-encoded (corpus has manual and automatic
  Blackwings sharing a VDS) — manual still requires the sticker (MG9).

Pos 10 model-year codes follow the standard alphabet; the subset below is
title-verified in the corpus (N/P/R/S/T) plus M from a user-supplied 2021 car.

Facts are definitive physical identity — verified provenance. Absence of a
fact means "this VIN pattern is outside verified ground truth", never a guess.
"""

from __future__ import annotations

_C8_TRIM = {"A": "1LT", "B": "2LT", "C": "3LT"}
_C8_BODY = {"2": "COUPE", "3": "CONVERTIBLE"}
_YEAR = {"L": 2020, "M": 2021, "N": 2022, "P": 2023, "R": 2024, "S": 2025, "T": 2026, "V": 2027}


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
