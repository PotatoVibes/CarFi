"""Sticker parser tests against the committed real-VIN fixture.

Fixture: stickers/1G6D65RP8N0460286.pdf — 2022 CT4-V Blackwing, Black Raven,
6-speed manual, sunroof, PDR, red calipers, no carbon fiber packages.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from carfinder.models import Vehicle
from carfinder.profiles import load_profile
from carfinder.scorer import score_vehicle
from carfinder.sticker_parser import _extract_json_blob, parse_sticker, vehicle_kwargs

ROOT = Path(__file__).resolve().parents[1]
BW_FIXTURE = ROOT / "stickers" / "1G6D65RP8N0460286.pdf"


@pytest.fixture(scope="module")
def bw():
    return parse_sticker(BW_FIXTURE)


def test_identity_fields(bw):
    assert bw.vin == "1G6D65RP8N0460286"
    assert bw.year == 2022
    assert bw.maker == "CADILLAC"
    assert bw.model_code == "6DF69"
    assert bw.title_line == "2022 CT4 V-SERIES BLACKWING"


def test_paint_msrp_interior(bw):
    assert bw.exterior == "BLACK RAVEN"
    assert bw.msrp == pytest.approx(70345.0)
    assert bw.interior.startswith("JET BLACK")


def test_factory_options_extracted(bw):
    # Codes this car verifiably carries (from the embedded JSON blob).
    assert {"FE4", "UV6", "KU9", "MG9", "CF5", "UQT", "GBA"} <= bw.options
    # My original manual-transmission guess is NOT on the car.
    assert "MX0" not in bw.options
    assert len(bw.options) > 100  # full factory list, not a fragment


def test_confidence_complete(bw):
    assert bw.confidence == 1.0
    assert bw.complete


def test_vehicle_kwargs_maps_paint(bw):
    kw = vehicle_kwargs(bw, "ct4v_blackwing")
    assert kw["paint_rpo"] == "GBA"  # Black Raven, from options∩paint-table
    assert kw["sticker_complete"] is True
    assert kw["vin"] == bw.vin


def test_full_pipeline_scores_blackwing(bw):
    profile = load_profile(ROOT / "config" / "ct4v_blackwing.yaml")
    v = Vehicle(miles=30000, price=58000, **vehicle_kwargs(bw, "ct4v_blackwing"))
    r = score_vehicle(profile, v)
    assert r.verdict == "ranked"
    # Every hard requirement passes with verified provenance.
    assert all(h.status == "pass" and h.provenance == "verified" for h in r.hard)
    badges = {b.label for b in r.highlights if b.matched}
    # The profile is user-tunable (highlights come and go via the config UI),
    # so only assert on features the CURRENT profile still defines.
    labels = {h.label for h in profile.highlights}
    known_on_this_car = {
        "Power sunroof",
        "Performance Data Recorder",
        "Massaging seats",
        "Painted calipers",  # BRAKE CALIPERS, RED — regex fixed in M2 pass
    }
    assert (known_on_this_car & labels) <= badges
    # This car has no carbon fiber packages — must not badge them.
    assert "Carbon Fiber Package 1" not in badges
    # And NOT the sueded package: its build sheet shows a LEATHER steering
    # wheel (NK4); the old badge here was the standard-shift-knob text
    # overfiring ("SUEDED MICROFIBER WRAPPED..." prints on every BW sticker).
    assert "Sueded microfiber package" not in badges


# --- C8 fixture: 2023 Stingray 3LT Z51 convertible, Black, NO MagneRide -----

C8_FIXTURE = ROOT / "stickers" / "1G1YC3D47P5121316.pdf"


@pytest.fixture(scope="module")
def c8():
    return parse_sticker(C8_FIXTURE)


def test_c8_identity(c8):
    assert c8.vin == "1G1YC3D47P5121316"
    assert c8.year == 2023
    assert c8.model_code == "1YC67"  # 3LT convertible body code
    assert c8.title_line == "2023 CORVETTE STINGRAY CONV"
    assert c8.exterior == "BLACK"  # engine column correctly cut off
    assert c8.msrp == pytest.approx(95800.0)
    assert c8.confidence == 1.0


def test_c8_options(c8):
    assert {"Z51", "3LT", "FE3", "NPP", "UQT", "UV6", "GBA"} <= c8.options
    # The MagneRide code is genuinely absent on this car.
    assert "FE4" not in c8.options
    assert "E60" not in c8.options  # no front lift either


def test_c8_pipeline_verified_exclusion(c8):
    """The core value prop: a car that looks right everywhere except one
    hard requirement gets a VIN-verified exclusion, not a fuzzy pass."""
    profile = load_profile(ROOT / "config" / "c8_stingray.yaml")
    v = Vehicle(miles=12000, price=64000, **vehicle_kwargs(c8, "c8_stingray"))
    r = score_vehicle(profile, v)
    assert r.verdict == "excluded"
    by = {h.key: h for h in r.hard}
    # Sticker prints "STINGRAY CONV", never "CONVERTIBLE" — the fixed matcher
    # must verified-pass, not verified-fail.
    for key in ("model_stingray", "not_z06_eray", "convertible", "trim_2lt_3lt", "z51"):
        assert by[key].status == "pass" and by[key].provenance == "verified", key
    assert by["magneride"].status == "fail"  # FE3 car: no magnetic ride
    badges = {b.label for b in r.highlights if b.matched}
    assert "Performance Data Recorder" in badges  # UQT verified on a real C8
    assert "Colored seat belts" in badges  # "TORCH RED SEAT BELT 495.00"
    assert "Front lift w/ memory" not in badges


def test_c8_paint_maps_to_black(c8):
    kw = vehicle_kwargs(c8, "c8_stingray")
    assert kw["paint_rpo"] == "GBA"


# --- JSON blob extraction unit tests (no PDF needed) ------------------------


def test_blob_valid_json():
    text = (
        'noise\n{"maker":"CADILLAC","model_year":"2022","vin":"1G6D65RP8N0460286",'
        '"Options":["FE4","UV6",""]}\ntrailer'
    )
    blob = _extract_json_blob(text)
    assert blob["maker"] == "CADILLAC"
    assert blob["Options"][:2] == ["FE4", "UV6"]


def test_blob_with_linebreaks_between_tokens():
    text = (
        '{"maker":"CADILLAC","model_year":"2022","vin":"1G6D65RP8N0460286",\n'
        '"Options":["FE4",\n"UV6"]}'
    )
    assert _extract_json_blob(text)["Options"] == ["FE4", "UV6"]


def test_blob_malformed_falls_back_to_token_harvest():
    # A break INSIDE a token breaks json.loads; harvesting still recovers codes.
    text = (
        '{"maker":"CADILLAC","model_year":"2022","vin":"1G6D65RP8N0460286",'
        '"Options":["FE4","UV"\n"6","CF5"]}'
    )
    blob = _extract_json_blob(text)
    assert "FE4" in blob["Options"] and "CF5" in blob["Options"]
    assert blob["vin"] == "1G6D65RP8N0460286"
    assert blob["model_year"] == "2022"


def test_blob_absent_returns_none():
    assert _extract_json_blob("no json here at all") is None
