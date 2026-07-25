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
                  K = LSY 2.0T — never a Blackwing (GM spec: no Blackwing ever
                  shipped the 2.0T; the sticker corpus holds no base CT4s only
                  because Auto.dev prefilters trim. 30/30 consistent in a live
                  Marketcheck CT4 sample, 2026-07-21 — every 5RK car was a
                  $9-33k base/Luxury CT4.)
  pos 5 (series, Blackwing only): 6 = 6MT manual, 7 = 10-speed automatic —
  GM certifies the two transmissions as separate configs. 6/6 consistent
  across the corpus (D65 manual 2022/2024/2025; D75 auto 2022/2024), with
  the 2023-only unified "DL5" series confirmed MIXED via per-VIN build
  sheets — DL5 deliberately decodes to nothing and the sticker/build sheet
  decides.

BMW M2, G87 (3MF…, San Luis Potosí — WMI created for the G87 in 2022; the
F87 was WBS, and 3MW is non-M Mexico BMWs):
  pos 4-8: 13DM0 = 2023-2024, 23DM0 = 2025+ (LCI) — the ONLY registered M2
  patterns per NHTSA vPIC's manufacturer pattern table (2026-07-24; every
  pos-7/8 alternative rejected). Real-VIN verified: 3MF13DM09P8D14983 (2023),
  3MF23DM09S8F25064 (2025 6MT). Transmission is NOT VIN-encoded (6MT and
  auto share identical patterns), so no trans fact is ever emitted.

BMW M2, F87 (WBS…, BMW M GmbH, Germany, 2016-2021):
  pos 4-8: 1H9C3/1H9C5 (2016-17), 1J5C3/1J5C5 (2018), 2U7C3/2U7C5/2U7C0
  (2019-21). Pos-8 3-vs-5 is NOT transmission (real 2017 6MT …888099 and DCT
  …888794 both carry pos8=3). Decodes to gen=F87 so a G87-only profile
  verified-fails these instead of trusting listing year data.

Toyota GR Supra, A90/A91 (WZ1…, Magna Steyr Graz; WMI registered to BMW AG,
make TOYOTA per vPIC):
  pos 4-5: DB (all Supras)
  pos 6 (engine): 4 = B58 3.0 (2020 only), 0 = B58 3.0 (2021+),
                  2 = B48 2.0 — real-VIN verified across 2020-2024.
  pos 7 (era):    C = 2020-2022 — ALL automatic (6MT didn't exist until
                  2023; vPIC returns "Automatic 8" for every C-era pattern),
                  so C-era VINs emit trans=AUTO.
                  G = 2023+ — trans deliberately NOT decoded (manual and
                  auto share DB0G0; a 2023 A91-MT manual and a 2023 Premium
                  auto were confirmed pattern-identical).
  "3.0 Premium" is NOT VIN-encoded — trim evidence must come from build
  data / listing text, never from here.

Pos 10 model-year codes follow the standard alphabet; the subset below is
title-verified in the corpus (N/P/R/S/T) plus M from a user-supplied 2021 car;
G/H/J/K cover the F87 years and L/M the early Supras (standard alphabet,
cross-checked against vPIC decodes of real F87/A90 VINs, 2026-07-24).

Facts are definitive physical identity — verified provenance. Absence of a
fact means "this VIN pattern is outside verified ground truth", never a guess.
"""

from __future__ import annotations

import re

_C8_TRIM = {"A": "1LT", "B": "2LT", "C": "3LT"}
_C8_BODY = {"2": "COUPE", "3": "CONVERTIBLE"}
_YEAR = {
    "G": 2016, "H": 2017, "J": 2018, "K": 2019, "L": 2020, "M": 2021,
    "N": 2022, "P": 2023, "R": 2024, "S": 2025, "T": 2026, "V": 2027,
}  # fmt: skip

# G87 M2: the only VDS patterns BMW registered (see module docstring).
_M2_G87_VDS = frozenset({"13DM0", "23DM0"})
# F87 M2 patterns, 2016-2021.
_M2_F87_VDS = frozenset({"1H9C3", "1H9C5", "1J5C3", "1J5C5", "2U7C3", "2U7C5", "2U7C0"})
# Supra pos 6-8: engine char + era char + '0'.
_SUPRA_VDS = frozenset({"4C0", "0C0", "2C0", "0G0", "2G0"})


def wmi_make(vin: str | None) -> str | None:
    """Evidence-routing make for a VIN: 'gm' | 'bmw' | 'toyota' | None.

    Decides which make-specific verified-evidence fetchers may be called
    (GM window sticker / gmpartsgiant vs the partsdeal siblings). None means
    "no make-specific source known" — never guess. WZ1 is registered to BMW
    AG (Graz builds the car) but the vehicle is a Toyota and its parts
    catalog lives on toyotapartsdeal — routed by brand, not builder.
    """
    if not vin:
        return None
    v = vin.strip().upper()
    if v.startswith("1G"):
        return "gm"
    if v.startswith(("3MF", "WBS", "3MW", "WBA", "WBY", "4US", "5UX", "5YM")):
        return "bmw"
    if v.startswith("WZ1"):
        return "toyota"
    return None


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
        elif v[7] in ("L", "K"):  # 2.7T / 2.0T — neither ever a Blackwing
            facts["model"] = "NON_BLACKWING"
    elif v.startswith("3MF"):  # BMW G87 M2 — the WMI exists only for it
        if v[3:8] in _M2_G87_VDS:
            facts["model"] = "M2"
            facts["gen"] = "G87"
        # Transmission is not VIN-encoded on the G87 — no trans fact, ever.
    elif v.startswith("WBS"):  # BMW M GmbH (Germany)
        if v[3:8] in _M2_F87_VDS:
            facts["model"] = "M2"
            facts["gen"] = "F87"
    elif v.startswith("WZ1DB"):  # Toyota GR Supra (Graz)
        vds = v[5:8]
        if vds in _SUPRA_VDS:
            facts["model"] = "SUPRA"
            facts["engine"] = "2.0" if v[5] == "2" else "3.0"
            if v[6] == "C":  # 2020-2022: manual didn't exist yet
                facts["trans"] = "AUTO"
            # G-era (2023+): manual and auto share the pattern — no fact.
    return facts


def model_year(vin: str | None) -> int | None:
    """Model year from VIN pos 10, or None outside the verified code range."""
    if not vin:
        return None
    v = vin.strip().upper()
    if len(v) != 17:
        return None
    return _YEAR.get(v[9])
