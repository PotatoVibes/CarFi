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
from carfinder.scorer import EDITION_BONUS, HIGHLIGHT_CAP, MAX_TOTAL, match, score_vehicle

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


def test_unknown_numeric_scores_neutral_50_at_full_weight():
    p = _numeric_profile()
    # Unknown miles scores a neutral 50 (full weight) so the known dims don't
    # get renormalized to outsized influence; unknown color still drops out.
    r = score_vehicle(p, Vehicle(price=67500))  # price at 50% of its band
    by = {s.key: s for s in r.soft}
    assert by["miles"].dim_score == pytest.approx(50.0)
    assert by["miles"].provenance == Provenance.unknown
    assert "neutral" in by["miles"].detail
    assert r.soft_score == pytest.approx(50.0)  # (50*.25 + 50*.20) / .45
    assert by["color"].provenance == Provenance.unknown
    assert by["color"].dim_score is None


def test_no_listing_car_color_not_overweighted():
    # The motivating case: sticker-verified car, no listing at all. A loved
    # color must not carry the entire soft score (100) — it's one dim of three.
    p = _numeric_profile()
    r = score_vehicle(p, Vehicle(paint_rpo="GM0"))  # blue = loved, nothing else
    # (50*.25 + 50*.20 + 100*.10) / .55
    assert r.soft_score == pytest.approx(59.0909, abs=1e-3)


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


def test_color_detail_includes_paint_name_when_informative():
    p = _numeric_profile()
    # Paint name differs from the family -> shown in parens, title-cased.
    r = score_vehicle(
        p, Vehicle(miles=50000, price=65000, paint_rpo="GM0", color_text="ELKHART LAKE BLUE")
    )
    d = next(s for s in r.soft if s.key == "color").detail
    assert d == "Blue (Elkhart Lake Blue) -> 100"
    # Paint name IS the family word -> no redundant "(White)".
    r2 = score_vehicle(p, Vehicle(miles=50000, price=65000, color_text="White"))
    d2 = next(s for s in r2.soft if s.key == "color").detail
    assert d2 == "White -> 0"


def test_rift_metallic_classifies_as_white():
    # Owner's call (2026-07-17): Rift Metallic reads white in person, and the
    # profile penalizes white — so a Rift car must score 0, labeled with both.
    p = _numeric_profile()
    r = score_vehicle(
        p, Vehicle(miles=50000, price=65000, paint_rpo="GRW", color_text="Rift Metallic")
    )
    c = next(s for s in r.soft if s.key == "color")
    assert c.dim_score == pytest.approx(0.0)
    assert c.detail == "White (Rift Metallic) -> 0"
    # Structured fields ride along so list pages render the same color.
    assert c.color_family == "white"
    assert c.paint_name == "Rift Metallic"


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


# --- AI claimed_features (M5) ----------------------------------------------


def test_claimed_feature_satisfies_soft_but_not_hard():
    p = _one_require_profile()  # hard require z51 (key "z51")
    # AI says the dealer claims Z51 -> promising, but claimed can't pass a hard req.
    v = Vehicle(claimed_features={"z51": "claimed"})
    r = score_vehicle(p, v)
    assert r.verdict == "needs_review"
    assert r.hard[0].status == "needs_review"
    assert r.hard[0].provenance == Provenance.claimed


def test_claimed_feature_highlight_adds_bonus():
    p = Profile(
        name="t",
        model_key="c8_stingray",
        highlights=[
            Highlight(label="Front lift", bonus=8, matcher=Matcher(sticker_text="FRONT LIFT"))
        ],
    )
    # Keyed by the highlight label; AI claim lights up the badge + bonus.
    v = Vehicle(claimed_features={"Front lift": "claimed"})
    r = score_vehicle(p, v)
    assert r.highlight_bonus == pytest.approx(8.0)
    assert r.highlights[0].matched is True
    assert r.highlights[0].provenance == Provenance.claimed


def test_verified_sticker_overrides_ai_claim():
    # Complete sticker says the option is absent; an AI "claimed" must not win.
    p = _one_require_profile()
    v = Vehicle(
        verified_text="no perf pkg here",
        sticker_complete=True,
        claimed_features={"z51": "claimed"},
    )
    r = score_vehicle(p, v)
    assert r.verdict == "excluded"  # verified-absent beats the AI claim


def test_ai_denied_does_not_add_bonus():
    p = Profile(
        name="t",
        model_key="c8_stingray",
        highlights=[
            Highlight(label="Front lift", bonus=8, matcher=Matcher(sticker_text="FRONT LIFT"))
        ],
    )
    v = Vehicle(claimed_features={"Front lift": "denied"})
    r = score_vehicle(p, v)
    assert r.highlight_bonus == 0.0
    assert r.highlights[0].matched is False


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
            Highlight(label="c", bonus=5, matcher=Matcher(sticker_text="CCC")),
        ],
    )
    v = Vehicle(verified_text="AAA BBB CCC", sticker_complete=True)
    r = score_vehicle(p, v)
    assert r.highlight_bonus == HIGHLIGHT_CAP  # 8 + 6 + 5 = 19, capped at 15
    assert sorted(b.label for b in r.highlights if b.matched) == ["a", "b", "c"]


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
    # Profile-derived, not golden: the user edits bounds/colors via the UI, so
    # build the "perfect car" from whatever the profile currently asks for.
    c8 = load_profile(CONFIG / "c8_stingray.yaml")
    price_c = next(s for s in c8.soft if s.kind == "numeric" and s.attr == "price")
    miles_c = next(s for s in c8.soft if s.kind == "numeric" and s.attr == "miles")
    color_c = next((s for s in c8.soft if s.kind == "color"), None)
    paint = "GM0" if (color_c is None or "blue" in color_c.preferred) else None
    sticker = (
        "CHEVROLET CORVETTE STINGRAY CONVERTIBLE 3LT\n"
        "Z51 PERFORMANCE PACKAGE\nMAGNETIC RIDE CONTROL\n"
        "FRONT LIFT ADJUSTABLE\nPERFORMANCE EXHAUST\n"
    )
    v = Vehicle(
        vin="1G1Y",
        year=2023,
        miles=int(miles_c.ideal),
        price=price_c.ideal,
        verified_text=sticker,
        sticker_complete=True,
        verified_rpo={"Z51", "FE4", "E60", "NPP"},
        paint_rpo=paint,
    )
    r = score_vehicle(c8, v)
    assert r.verdict == "ranked"
    # Ideal-value car: every KNOWN dimension scores 100 (color drops to
    # unknown if the user ever stops preferring blue — still fine).
    known = [s for s in r.soft if s.dim_score is not None]
    assert all(s.dim_score == pytest.approx(100.0) for s in known)
    assert r.soft_score == pytest.approx(100.0)
    # Front lift (8) + performance exhaust (4): under the +15 cap, so the
    # full sum comes through.
    assert r.highlight_bonus == pytest.approx(12.0)


def test_seed_c8_suspension_facts_decide_z51_and_magneride():
    """A build-sheet-only base+MagneRide car (FE2) must verified-FAIL Z51 —
    excluded, not needs_review — and verified-pass MagneRide."""
    c8 = load_profile(CONFIG / "c8_stingray.yaml")
    v = Vehicle(
        vin="1G1YB3D44M5125399",
        year=2021,
        vin_facts={
            "model": "STINGRAY",
            "trim": "2LT",
            "body": "CONVERTIBLE",
            "z51": "NO",
            "magneride": "YES",
        },
    )
    r = score_vehicle(c8, v)
    z51 = next(h for h in r.hard if h.key == "z51")
    mag = next(h for h in r.hard if h.key == "magneride")
    assert z51.status == "fail" and z51.provenance == Provenance.verified
    assert mag.status == "pass" and mag.provenance == Provenance.verified
    assert r.verdict == "excluded"

    # FE4 car: no magneride fact -> falls through to (absent) sticker
    # evidence -> needs_review on that one requirement, never a guess.
    v4 = Vehicle(
        vin="X",
        year=2023,
        vin_facts={"model": "STINGRAY", "trim": "3LT", "body": "CONVERTIBLE", "z51": "YES"},
    )
    r4 = score_vehicle(c8, v4)
    mag4 = next(h for h in r4.hard if h.key == "magneride")
    assert mag4.status == "needs_review" and mag4.provenance == Provenance.unknown
    z514 = next(h for h in r4.hard if h.key == "z51")
    assert z514.status == "pass" and z514.provenance == Provenance.verified


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


# --- special editions ------------------------------------------------------

_Y70_VIN = "1G1YC3D44P5500628"  # real 2023 Stingray 3LT 70th Anniversary


def _capped_profile() -> Profile:
    return Profile(
        name="t",
        model_key="c8_stingray",
        highlights=[
            Highlight(label="a", bonus=8, matcher=Matcher(sticker_text="AAA")),
            Highlight(label="b", bonus=8, matcher=Matcher(sticker_text="BBB")),
        ],
    )


def test_edition_bonus_lands_outside_the_highlight_cap():
    v = Vehicle(vin=_Y70_VIN, verified_rpo={"Y70"}, verified_text="AAA BBB", sticker_complete=True)
    r = score_vehicle(_capped_profile(), v)
    assert r.highlight_bonus == HIGHLIGHT_CAP  # 16 capped at 15 — unchanged
    assert r.edition_bonus == EDITION_BONUS
    assert r.total == pytest.approx(r.soft_score + HIGHLIGHT_CAP + EDITION_BONUS)
    assert r.edition.key == "c8_70th_anniversary"
    assert r.edition.group == "edition"
    assert r.edition.provenance == Provenance.verified
    assert r.edition.bonus == EDITION_BONUS
    assert "Y70" in r.edition.detail
    assert MAX_TOTAL == 100 + HIGHLIGHT_CAP + EDITION_BONUS


def test_no_edition_adds_nothing():
    v = Vehicle(vin="1G1YC3D41P5108000", verified_text="AAA", sticker_complete=True)
    r = score_vehicle(_capped_profile(), v)
    assert r.edition is None
    assert r.edition_bonus == 0.0
    assert r.total == pytest.approx(r.soft_score + 8)


def test_claimed_edition_still_bumps_score_but_never_a_hard_requirement():
    p = Profile(
        name="t",
        model_key="gr_supra",
        hard=[
            HardConstraint(
                key="manual",
                label="Manual",
                matcher=Matcher(sticker_text="6-SPEED MANUAL"),
            )
        ],
    )
    v = Vehicle(vin="WZ1DB0G08PW054589", listing_text="A91-MT Edition")
    r = score_vehicle(p, v)
    assert r.edition.provenance == Provenance.claimed
    assert r.edition_bonus == EDITION_BONUS
    # Edition text is its own input: it can't satisfy (or even claim) a hard
    # requirement — the manual gate still has no evidence.
    assert r.hard[0].provenance == Provenance.unknown
    assert r.verdict == "needs_review"


def test_edition_score_is_monotone_with_soft_score():
    # The bump is flat: it never reorders two editions (or two non-editions)
    # relative to each other.
    p = _numeric_profile()
    cheap = score_vehicle(p, Vehicle(vin=_Y70_VIN, verified_rpo={"Y70"}, price=60000, miles=1))
    dear = score_vehicle(p, Vehicle(vin=_Y70_VIN, verified_rpo={"Y70"}, price=69000, miles=1))
    assert cheap.total - dear.total == pytest.approx(cheap.soft_score - dear.soft_score)
