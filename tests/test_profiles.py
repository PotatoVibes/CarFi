"""Profile loading: seed validity, round-trip, malformed rejection."""

from __future__ import annotations

from pathlib import Path

import pytest

from carfinder.models import Profile
from carfinder.profiles import ProfileError, dump_profile, load_profile, load_profile_str

CONFIG = Path(__file__).resolve().parents[1] / "config"


def test_seed_profiles_load():
    c8 = load_profile(CONFIG / "c8_stingray.yaml")
    bw = load_profile(CONFIG / "ct4v_blackwing.yaml")
    assert c8.model_key == "c8_stingray"
    assert bw.model_key == "ct4v_blackwing"
    # Sanity: the documented hard requirements are present. Year floor is
    # checked by kind (not key) so tuning the min year doesn't break this.
    assert {c.key for c in c8.hard} >= {"z51", "magneride", "convertible"}
    assert any(c.kind == "year_min" for c in c8.hard)
    assert {c.key for c in bw.hard} >= {
        "manual_transmission",
        "head_up_display",
        "ventilated_seats",
    }


def test_bw_belt_and_suede_matchers_fixed_against_real_sticker_text():
    """Regression for 1G6DL5RP5P0460421: the scrambled sticker prints
    'SEAT BELT COLOR, 400.00' (must match — Torch Red is the only offered
    belt color) and the STANDARD sueded shift knob line (must NOT match)."""
    bw = load_profile(CONFIG / "ct4v_blackwing.yaml")
    belts = next(h for h in bw.highlights if "seat belt" in h.label.lower())
    suede = next(h for h in bw.highlights if "microfiber" in h.label.lower())
    belt_re = belts.matcher.compiled()
    suede_re = suede.matcher.compiled()

    assert belt_re.search("STEERING WHEEL, HEATED SEAT BELT COLOR, 400.00")
    assert belt_re.search("TORCH RED SEAT BELT 495.00")
    assert belt_re.search("709-SEAT BELT COLOR TORCH RED (93)")  # build-sheet form
    assert not belt_re.search("719-SEAT BELT COLOR-BLACK (10)")  # standard black
    assert not belt_re.search("3N9-SEAT BELT COLOR TAN")  # standard tan

    assert not suede_re.search("SUEDED MICROFIBER WRAPPED AUTOMATIC")  # standard knob
    assert suede_re.search("SUEDED MICROFIBER PACKAGE 600.00")
    assert suede_re.search("SUEDED MICROFIBER STEERING WHEEL AND SHIFT KNOB")
    assert "709" in belts.matcher.rpo and "53S" in suede.matcher.rpo


def test_round_trip_is_stable():
    c8 = load_profile(CONFIG / "c8_stingray.yaml")
    reparsed = load_profile_str(dump_profile(c8))
    assert reparsed == c8


def test_missing_file():
    with pytest.raises(ProfileError, match="not found"):
        load_profile(CONFIG / "does_not_exist.yaml")


def test_reject_non_mapping():
    with pytest.raises(ProfileError, match="mapping"):
        load_profile_str("- just\n- a\n- list\n")


def test_reject_unknown_key():
    with pytest.raises(ProfileError):
        load_profile_str("name: x\nmodel_key: y\nbogus_key: 1\n")


def test_reject_bad_regex():
    bad = """
    name: x
    model_key: y
    hard:
      - key: k
        label: l
        kind: require
        matcher:
          sticker_text: "(unclosed"
    """
    with pytest.raises(ProfileError, match="regex"):
        load_profile_str(bad)


def test_reject_matcher_with_nothing():
    bad = """
    name: x
    model_key: y
    hard:
      - key: k
        label: l
        kind: require
        matcher: {}
    """
    with pytest.raises(ProfileError):
        load_profile_str(bad)


def test_reject_vin_any_without_vin_attr():
    bad = """
    name: x
    model_key: y
    hard:
      - key: k
        label: l
        kind: require
        matcher:
          vin_any: [2LT]
    """
    with pytest.raises(ProfileError):
        load_profile_str(bad)


def test_vin_matcher_alone_is_valid():
    p = load_profile_str(
        """
    name: x
    model_key: y
    hard:
      - key: k
        label: l
        kind: require
        matcher:
          vin_attr: trim
          vin_any: [2lt, 3lt]
    """
    )
    assert p.hard[0].matcher.vin_any == ["2LT", "3LT"]  # normalized upper


def test_reject_unknown_color_family():
    bad = """
    name: x
    model_key: y
    soft:
      - key: color
        label: Color
        kind: color
        weight: 0.1
        preferred: [chartreuse]
    """
    with pytest.raises(ProfileError, match="famil"):
        load_profile_str(bad)


def test_reject_numeric_missing_bounds():
    bad = """
    name: x
    model_key: y
    soft:
      - key: miles
        label: Mileage
        kind: numeric
        attr: miles
        weight: 0.2
    """
    with pytest.raises(ProfileError):
        load_profile_str(bad)


def test_reject_year_min_without_value():
    bad = """
    name: x
    model_key: y
    hard:
      - key: yr
        label: Year
        kind: year_min
    """
    with pytest.raises(ProfileError):
        load_profile_str(bad)


def test_empty_profile_is_valid():
    p = load_profile_str("name: x\nmodel_key: y\n")
    assert isinstance(p, Profile)
    assert p.hard == [] and p.soft == [] and p.highlights == []
