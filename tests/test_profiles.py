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
    # Sanity: the documented hard requirements are present.
    assert {c.key for c in c8.hard} >= {"z51", "magneride", "convertible", "year_2022_plus"}
    assert {c.key for c in bw.hard} >= {
        "manual_transmission",
        "head_up_display",
        "ventilated_seats",
    }


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
