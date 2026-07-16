"""VIN decoder — goldens from the corpus-verified mappings, plus scorer wiring."""

from __future__ import annotations

from carfinder.models import Matcher, Provenance, Vehicle
from carfinder.scorer import match
from carfinder.vin import decode, model_year

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


def test_malformed_vins_decode_empty():
    assert decode(None) == {}
    assert decode("") == {}
    assert decode("1G1Y") == {}  # short
    assert decode("WBA123456789ABCDE") == {}  # not a GM pattern we know


def test_model_year_codes():
    assert model_year("1G1YB3D46M5122651") == 2021
    assert model_year("1G1YA2D4XP5108883") == 2023
    assert model_year("1G1YA2D42T5111674") == 2026
    assert model_year("1G1YA2D42Z5111674") is None  # outside verified range


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
