"""Scorer golden cases and property tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from carfinder.models import (
    HardConstraint,
    Highlight,
    Matcher,
    Profile,
    Provenance,
    SoftConstraint,
    Vehicle,
)
from carfinder.profiles import load_profile
from carfinder.scorer import HIGHLIGHT_CAP, match, score_vehicle

CONFIG = Path(__file__).resolve().parents[1] / "config"


# --- small hand-built profile for precise golden numbers -------------------


def _numeric_profile() -> Profile:
    return Profile(
        name="t",
        model_key="c8_stingray",
        soft=[
            SoftConstraint(
                key="miles",
                label="Mileage",
                kind="numeric",
                attr="miles",
                ideal=50000,
                hard=60000,
                weight=0.20,
            ),
            SoftConstraint(
                key="price",
                label="Price",
                kind="numeric",
                attr="price",
                ideal=65000,
                hard=70000,
                weight=0.25,
            ),
            SoftConstraint(
                key="color",
                label="Color",
                kind="color",
                weight=0.10,
                preferred=["blue"],
                penalized=["black", "white", "red"],
            ),
        ],
    )


# --- match primitive -------------------------------------------------------


def test_match_verified_rpo_beats_everything():
    m = Matcher(rpo=["Z51"], sticker_text="Z51")
    v = Vehicle(verified_rpo={"Z51"})
    out = match(m, v)
    assert out.matched is True and out.provenance == Provenance.verified


def test_match_complete_sticker_absence_is_verified_false():
    m = Matcher(sticker_text="FRONT LIFT")
    v = Vehicle(verified_text="BASE TRIM ONLY", sticker_complete=True)
    out = match(m, v)
    assert out.matched is False and out.provenance == Provenance.verified


def test_match_claimed_text_when_no_verified_data():
    m = Matcher(sticker_text="FRONT LIFT")
    v = Vehicle(claimed_text="has front lift per dealer")
    out = match(m, v)
    assert out.matched is True and out.provenance == Provenance.claimed


def test_match_unknown_when_no_evidence():
    m = Matcher(sticker_text="FRONT LIFT")
    assert match(m, Vehicle()).matched is None


def test_verified_negative_overrides_claimed_positive():
    # Complete sticker without the option wins over a dealer's claim.
    m = Matcher(sticker_text="FRONT LIFT")
    v = Vehicle(verified_text="NO LIFT HERE", sticker_complete=True, claimed_text="FRONT LIFT!")
    assert match(m, v).matched is False


# --- soft penalty curve golden values --------------------------------------


def test_penalty_curve_midpoints():
    p = _numeric_profile()
    v = Vehicle(miles=55000, price=67500, paint_rpo="GM0")  # both at 50%, blue
    r = score_vehicle(p, v)
    by = {s.key: s for s in r.soft}
    assert by["miles"].dim_score == pytest.approx(50.0)
    assert by["price"].dim_score == pytest.approx(50.0)
    assert by["color"].dim_score == pytest.approx(100.0)
    # weighted: (50*.20 + 50*.25 + 100*.10) / .55
    assert r.soft_score == pytest.approx(59.0909, abs=1e-3)


def test_score_floor_and_ceiling():
    p = _numeric_profile()
    best = score_vehicle(p, Vehicle(miles=10000, price=1, paint_rpo="GM0"))
    assert best.soft_score == pytest.approx(100.0)
    worst = score_vehicle(p, Vehicle(miles=99999, price=99999, paint_rpo="GBA"))  # black
    assert worst.soft_score == pytest.approx(0.0)


def test_unknown_dim_excluded_and_renormalized():
    p = _numeric_profile()
    # Only price known -> score is exactly the price dimension's score.
    r = score_vehicle(p, Vehicle(price=67500))
    assert r.soft_score == pytest.approx(50.0)
    color = next(s for s in r.soft if s.key == "color")
    assert color.provenance == Provenance.unknown


# --- monotonicity property -------------------------------------------------


@pytest.mark.parametrize("miles", [0, 20000, 50000, 55000, 60000, 80000])
def test_more_miles_never_raises_score(miles):
    p = _numeric_profile()
    base = score_vehicle(p, Vehicle(miles=miles, price=65000, paint_rpo="GM0")).soft_score
    more = score_vehicle(p, Vehicle(miles=miles + 5000, price=65000, paint_rpo="GM0")).soft_score
    assert more <= base + 1e-9


# --- color scoring ---------------------------------------------------------


def test_color_verified_vs_claimed_provenance():
    p = _numeric_profile()
    verified = score_vehicle(p, Vehicle(miles=50000, price=65000, paint_rpo="GM0"))
    claimed = score_vehicle(p, Vehicle(miles=50000, price=65000, color_text="Rapid Blue"))
    vc = next(s for s in verified.soft if s.key == "color")
    cc = next(s for s in claimed.soft if s.key == "color")
    # Same family/score, but provenance differs — verified beats claimed on ties.
    assert vc.dim_score == cc.dim_score == pytest.approx(100.0)
    assert vc.provenance == Provenance.verified
    assert cc.provenance == Provenance.claimed


def test_penalized_color_scores_zero():
    p = _numeric_profile()
    r = score_vehicle(p, Vehicle(miles=50000, price=65000, paint_rpo="GBA"))  # black
    assert next(s for s in r.soft if s.key == "color").dim_score == pytest.approx(0.0)


# --- hard gate: verified beats claimed -------------------------------------


def _one_require_profile() -> Profile:
    return Profile(
        name="t",
        model_key="c8_stingray",
        hard=[
            HardConstraint(
                key="z51",
                label="Z51",
                kind="require",
                matcher=Matcher(rpo=["Z51"], sticker_text="Z51"),
            )
        ],
    )


def test_verified_pass_ranks_but_claimed_only_needs_review():
    p = _one_require_profile()
    verified = score_vehicle(p, Vehicle(verified_rpo={"Z51"}, sticker_complete=True))
    claimed = score_vehicle(p, Vehicle(claimed_text="includes Z51 package"))
    assert verified.verdict == "ranked"
    assert claimed.verdict == "needs_review"


def test_verified_absent_excludes():
    p = _one_require_profile()
    r = score_vehicle(p, Vehicle(verified_text="no perf pkg", sticker_complete=True))
    assert r.verdict == "excluded"
    assert [f.key for f in r.failures] == ["z51"]


def test_unknown_hard_routes_to_review_or_fail():
    p = _one_require_profile()
    assert score_vehicle(p, Vehicle()).verdict == "needs_review"
    p.review_on_unknown_hard = False
    assert score_vehicle(p, Vehicle()).verdict == "excluded"


def test_forbid_excludes_on_verified_presence():
    p = Profile(
        name="t",
        model_key="c8_stingray",
        hard=[
            HardConstraint(
                key="no_z06", label="Not Z06", kind="forbid", matcher=Matcher(sticker_text="Z06")
            )
        ],
    )
    present = score_vehicle(p, Vehicle(verified_text="CORVETTE Z06", sticker_complete=True))
    absent = score_vehicle(p, Vehicle(verified_text="STINGRAY", sticker_complete=True))
    assert present.verdict == "excluded"
    assert absent.verdict == "ranked"


def test_year_min_gate():
    p = Profile(
        name="t",
        model_key="c8_stingray",
        hard=[HardConstraint(key="yr", label="2022+", kind="year_min", min_year=2022)],
    )
    assert score_vehicle(p, Vehicle(year=2023)).verdict == "ranked"
    assert score_vehicle(p, Vehicle(year=2021)).verdict == "excluded"
    assert score_vehicle(p, Vehicle(year=None)).verdict == "needs_review"


# --- exclude_beyond_hard ---------------------------------------------------


def test_exclude_beyond_hard_flag():
    p = Profile(
        name="t",
        model_key="c8_stingray",
        soft=[
            SoftConstraint(
                key="price",
                label="Price",
                kind="numeric",
                attr="price",
                ideal=60000,
                hard=65000,
                weight=1.0,
                exclude_beyond_hard=True,
            )
        ],
    )
    assert score_vehicle(p, Vehicle(price=64000)).verdict == "ranked"
    assert score_vehicle(p, Vehicle(price=66000)).verdict == "excluded"


# --- highlights ------------------------------------------------------------


def test_highlight_bonus_capped():
    p = Profile(
        name="t",
        model_key="c8_stingray",
        highlights=[
            Highlight(label="a", bonus=8, matcher=Matcher(sticker_text="AAA")),
            Highlight(label="b", bonus=6, matcher=Matcher(sticker_text="BBB")),
        ],
    )
    v = Vehicle(verified_text="AAA BBB", sticker_complete=True)
    r = score_vehicle(p, v)
    assert r.highlight_bonus == HIGHLIGHT_CAP  # 8 + 6 = 14, capped at 10
    assert sorted(b.label for b in r.highlights if b.matched) == ["a", "b"]


def test_claimed_highlight_still_adds_bonus():
    p = Profile(
        name="t",
        model_key="c8_stingray",
        highlights=[Highlight(label="lift", bonus=8, matcher=Matcher(sticker_text="FRONT LIFT"))],
    )
    r = score_vehicle(p, Vehicle(claimed_text="front lift equipped"))
    assert r.highlight_bonus == pytest.approx(8.0)
    badge = r.highlights[0]
    assert badge.matched and badge.provenance == Provenance.claimed


def test_missing_highlight_contributes_nothing():
    p = Profile(
        name="t",
        model_key="c8_stingray",
        highlights=[Highlight(label="lift", bonus=8, matcher=Matcher(sticker_text="FRONT LIFT"))],
    )
    r = score_vehicle(p, Vehicle(verified_text="no options", sticker_complete=True))
    assert r.highlight_bonus == 0.0
    assert r.highlights[0].matched is False


# --- integration against the seeded profiles -------------------------------


def test_seed_c8_fully_loaded_verified_car_ranks_high():
    c8 = load_profile(CONFIG / "c8_stingray.yaml")
    sticker = (
        "CHEVROLET CORVETTE STINGRAY CONVERTIBLE 3LT\n"
        "Z51 PERFORMANCE PACKAGE\nMAGNETIC RIDE CONTROL\n"
        "FRONT LIFT ADJUSTABLE\nPERFORMANCE EXHAUST\n"
    )
    v = Vehicle(
        vin="1G1Y",
        year=2023,
        miles=18000,
        price=61000,
        verified_text=sticker,
        sticker_complete=True,
        verified_rpo={"Z51", "FE4", "E60", "NPP"},
        paint_rpo="GM0",
    )
    r = score_vehicle(c8, v)
    assert r.verdict == "ranked"
    assert r.soft_score == pytest.approx(100.0)
    assert r.highlight_bonus == pytest.approx(HIGHLIGHT_CAP)  # 8 + 4 capped at 10


def test_seed_c8_z06_excluded():
    c8 = load_profile(CONFIG / "c8_stingray.yaml")
    v = Vehicle(
        vin="z",
        year=2023,
        miles=5000,
        price=60000,
        verified_text="CORVETTE STINGRAY CONVERTIBLE Z06 3LT Z51 MAGNETIC RIDE CONTROL",
        sticker_complete=True,
    )
    r = score_vehicle(c8, v)
    assert r.verdict == "excluded"
    assert any(f.key == "not_z06_eray" for f in r.failures)
