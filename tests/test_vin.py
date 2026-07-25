"""VIN decoder — goldens from the corpus-verified mappings, plus scorer wiring."""

from __future__ import annotations

from carfinder.models import Matcher, Provenance, Vehicle
from carfinder.scorer import match
from carfinder.vin import decode, model_year, wmi_make

# --- decoder goldens (each class represented in the committed sticker corpus) ---


def test_c8_trim_body_model_decoding():
    # 1LT coupe (the screenshot car): verified-fails trim + convertible.
    assert decode("1G1YA2D4XP5108883") == {"trim": "1LT", "body": "COUPE", "model": "STINGRAY"}
    # 2LT convertible.
    assert decode("1G1YB3D46M5122651") == {
        "trim": "2LT",
        "body": "CONVERTIBLE",
        "model": "STINGRAY",
    }
    # 3LT convertible (the known Z51 fixture car).
    assert decode("1G1YC3D47P5121316") == {
        "trim": "3LT",
        "body": "CONVERTIBLE",
        "model": "STINGRAY",
    }


def test_c8_unknown_series_decodes_to_nothing_definitive():
    # Hypothetical Z06-style VIN (LZ trim series 'D', LT6 engine '6'): outside
    # verified ground truth -> no trim/model facts, never a guessed fail.
    facts = decode("1G1YD2D60P5100001")
    assert "trim" not in facts and "model" not in facts
    assert facts.get("body") == "COUPE"  # body digit is still standard


def test_ct4_blackwing_vs_non_blackwing():
    # D65 = manual Blackwing (6/6 corpus split; GM certifies 6MT and 10AT
    # as separate series configs).
    assert decode("1G6D65RP8N0460286") == {"model": "BLACKWING", "trans": "MANUAL"}
    assert decode("1G6D65RP2R0460256") == {"model": "BLACKWING", "trans": "MANUAL"}
    # D75 = automatic Blackwing.
    assert decode("1G6D75RP6N0410466") == {"model": "BLACKWING", "trans": "AUTO"}
    # 2023's unified DL5 series covered both transmissions -> model only.
    assert decode("1G6DL5RPXP0410176") == {"model": "BLACKWING"}
    assert decode("1G6DH5RL5P0123651") == {"model": "NON_BLACKWING"}  # 2.7T CT4-V
    # 2.0T (engine K) — base/Luxury CT4s, never a Blackwing. Real VINs from
    # the live Marketcheck CT4 sample (2026-07-21).
    assert decode("1G6DJ5RK8N0130912") == {"model": "NON_BLACKWING"}
    assert decode("1G6DB5RK0N0110165") == {"model": "NON_BLACKWING"}


def test_g87_m2_decoding():
    # Real VINs, 2026-07-24 research (classic.com / Cars & Bids / duPont).
    assert decode("3MF13DM09P8D14983") == {"model": "M2", "gen": "G87"}  # 2023
    assert decode("3MF23DM09S8F25064") == {"model": "M2", "gen": "G87"}  # 2025 6MT
    assert decode("3MF23DM07S8F59522") == {"model": "M2", "gen": "G87"}  # 2025
    # Transmission is NOT VIN-encoded on the G87 — never a trans fact.
    assert "trans" not in decode("3MF23DM09S8F25064")
    # A 3MF VIN outside the registered patterns decodes to nothing.
    assert decode("3MF53CM09P8D14983") == {}


def test_f87_m2_decoding():
    # Real VINs (classic.com): gen fact lets a G87-only profile verified-fail.
    assert decode("WBS1H9C56GV785915") == {"model": "M2", "gen": "F87"}  # 2016
    assert decode("WBS1H9C32HV888099") == {"model": "M2", "gen": "F87"}  # 2017 6MT
    assert decode("WBS2U7C08L7E80678") == {"model": "M2", "gen": "F87"}  # 2020 Comp
    # Other WBS M cars decode to nothing (outside verified ground truth).
    assert decode("WBS8M9C56J5J78168") == {}


def test_supra_decoding():
    # 2020-2022 (era char C): all automatic — verified trans fact.
    assert decode("WZ1DB4C01LW022039") == {"model": "SUPRA", "engine": "3.0", "trans": "AUTO"}
    assert decode("WZ1DB0C02MW035702") == {"model": "SUPRA", "engine": "3.0", "trans": "AUTO"}
    assert decode("WZ1DB2C01MW042488") == {"model": "SUPRA", "engine": "2.0", "trans": "AUTO"}
    # 2023+ (era char G): manual exists — deliberately NO trans fact. A 2023
    # A91-MT manual and a 2023 3.0 Premium automatic share this pattern.
    assert decode("WZ1DB0G06PW056034") == {"model": "SUPRA", "engine": "3.0"}
    assert decode("WZ1DB0G07PW059282") == {"model": "SUPRA", "engine": "3.0"}
    assert decode("WZ1DB2G00PW048011") == {"model": "SUPRA", "engine": "2.0"}
    # Unknown era char -> engine-only facts never invented.
    assert decode("WZ1DB0X06PW056034") == {}


def test_wmi_make_routing():
    assert wmi_make("1G1YC3D47P5121316") == "gm"
    assert wmi_make("1G6D65RP8N0460286") == "gm"
    assert wmi_make("3MF13DM09P8D14983") == "bmw"
    assert wmi_make("WBS1H9C56GV785915") == "bmw"
    assert wmi_make("WZ1DB0G06PW056034") == "toyota"
    assert wmi_make("JTDKARFU0L3123456") is None  # Prius — no source known
    assert wmi_make(None) is None
    assert wmi_make("") is None


def test_malformed_vins_decode_empty():
    assert decode(None) == {}
    assert decode("") == {}
    assert decode("1G1Y") == {}  # short
    assert decode("WBA123456789ABCDE") == {}  # not a pattern we know


def test_model_year_codes():
    assert model_year("1G1YB3D46M5122651") == 2021
    assert model_year("1G1YA2D4XP5108883") == 2023
    assert model_year("1G1YA2D42T5111674") == 2026
    assert model_year("1G1YA2D42Z5111674") is None  # outside verified range
    # Import years (F87 / A90 / G87) follow the same standard alphabet.
    assert model_year("WBS1H9C56GV785915") == 2016
    assert model_year("WZ1DB4C01LW022039") == 2020
    assert model_year("3MF23DM09S8F25064") == 2025


# --- scorer integration ------------------------------------------------------


def _matcher(**kw):
    return Matcher(**kw)


def test_vin_fact_pass_is_verified():
    m = _matcher(vin_attr="trim", vin_any=["2LT", "3LT"], sticker_text="2LT|3LT")
    v = Vehicle(vin="X", vin_facts={"trim": "3LT"})
    o = match(m, v)
    assert o.matched is True and o.provenance == Provenance.verified


def test_vin_fact_fail_is_verified_no():
    m = _matcher(vin_attr="trim", vin_any=["2LT", "3LT"], sticker_text="2LT|3LT")
    v = Vehicle(vin="X", vin_facts={"trim": "1LT"}, claimed_text="3LT Z51")
    o = match(m, v)
    # The VIN is definitive: claimed text cannot resurrect a VIN-failed fact.
    assert o.matched is False and o.provenance == Provenance.verified


def test_missing_vin_fact_falls_through_to_sticker_chain():
    m = _matcher(vin_attr="model", vin_any=["STINGRAY"], sticker_text="STINGRAY")
    v = Vehicle(vin="X", verified_text="2023 CORVETTE STINGRAY CONV", vin_facts={})
    o = match(m, v)
    assert o.matched is True and o.provenance == Provenance.verified  # via text


def test_forbid_semantics_stingray_fact_clears_z06():
    m = _matcher(vin_attr="model", vin_any=["Z06", "ERAY"], sticker_text="Z06|E-RAY")
    v = Vehicle(vin="X", vin_facts={"model": "STINGRAY"})
    o = match(m, v)
    # Fact present and NOT in the forbidden set -> verified absent.
    assert o.matched is False and o.provenance == Provenance.verified


# --- ISO 3779 validation -----------------------------------------------------


def test_validate_accepts_real_corpus_vins():
    from carfinder.vin import validate

    for real in (
        "1G6D65RP8N0460286",  # BW sticker fixture
        "1G1YC3D47P5121316",  # C8 sticker fixture
        "1G6DL5RP5P0460421",
        "1G6D65RP2R0460256",
    ):
        assert validate(real) is None
        assert validate(real.lower()) is None  # case-insensitive


def test_validate_rejects_bad_vins():
    from carfinder.vin import validate

    assert "17 characters" in validate("1G6D65RP8N046028")  # 16 chars
    assert "17 characters" in validate("")
    assert "17 characters" in validate(None)
    assert "I, O, or Q" in validate("1G6D65RP8N04602I6")  # illegal letter
    # One mistyped character flips the check digit.
    assert "check digit" in validate("1G6D65RP8N0460287")
    assert "check digit" in validate("1G6D65RP7N0460286")
