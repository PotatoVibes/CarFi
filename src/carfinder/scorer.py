"""Deterministic scorer — the heart of the tool. Pure functions, zero I/O.

Pipeline per vehicle:
  1. Hard gate (verified-only pass/fail; unknown -> needs review; claimed can
     never satisfy a hard requirement).
  2. Soft dimensions -> weighted 0–100 score (linear penalty curves).
  3. Highlights -> bonus, capped at +15 total.
Output carries a per-requirement breakdown with a provenance tag on each.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import colors
from .models import (
    HardConstraint,
    Highlight,
    Matcher,
    Profile,
    Provenance,
    RequirementResult,
    ScoreResult,
    SoftConstraint,
    Vehicle,
)

HIGHLIGHT_CAP = 15.0


@dataclass(frozen=True)
class MatchOutcome:
    """Tri-state match with the best provenance that produced it."""

    matched: bool | None  # True / False / None(unknown)
    provenance: Provenance


def match(matcher: Matcher, vehicle: Vehicle, key: str | None = None) -> MatchOutcome:
    """Resolve a matcher against a vehicle, preferring verified evidence.

    Verified data (a VIN-decoded fact or a complete window sticker) yields a
    definitive True/False. Only when verified evidence is absent do we fall
    back to claimed evidence (dealer free-text, then the AI extractor's
    per-attribute claim for ``key``).
    """
    # 0. VIN-decoded fact — definitive both ways: the VIN physically encodes
    #    trim/body/model, so a fact outside vin_any is a verified "no".
    if matcher.vin_attr:
        fact = vehicle.vin_facts.get(matcher.vin_attr)
        if fact is not None:
            return MatchOutcome(fact in matcher.vin_any, Provenance.verified)

    # 1. Verified RPO code hit. (Empty sets/lists make any() False, so no guard.)
    if any(code in vehicle.verified_rpo for code in matcher.rpo):
        return MatchOutcome(True, Provenance.verified)

    # 2. Verified sticker-text hit.
    pattern = matcher.compiled()
    if pattern and vehicle.verified_text and pattern.search(vehicle.verified_text):
        return MatchOutcome(True, Provenance.verified)

    # 3. A complete sticker with no hit is a definitive verified "no".
    if vehicle.sticker_complete:
        return MatchOutcome(False, Provenance.verified)

    # 4. Claimed text hit (dealer free-text) — unconfirmed.
    if pattern and vehicle.claimed_text and pattern.search(vehicle.claimed_text):
        return MatchOutcome(True, Provenance.claimed)

    # 5. AI-extracted per-attribute claim (also claimed provenance).
    if key is not None:
        status = vehicle.claimed_features.get(key)
        if status == "claimed":
            return MatchOutcome(True, Provenance.claimed)
        if status == "denied":
            return MatchOutcome(False, Provenance.claimed)

    # 6. Nothing to go on.
    return MatchOutcome(None, Provenance.unknown)


# --- Hard gate -------------------------------------------------------------


def _evaluate_hard(
    c: HardConstraint, vehicle: Vehicle, review_on_unknown: bool
) -> RequirementResult:
    if c.kind == "year_min":
        return _evaluate_year_min(c, vehicle)

    outcome = match(c.matcher, vehicle, key=c.key)  # type: ignore[arg-type]
    if c.kind == "require":
        return _resolve_require(c, outcome, review_on_unknown)
    return _resolve_forbid(c, outcome, review_on_unknown)


def _hard_result(
    c: HardConstraint, status: str, prov: Provenance, detail: str, matched: bool | None
):
    return RequirementResult(
        key=c.key,
        label=c.label,
        group="hard",
        status=status,  # type: ignore[arg-type]
        provenance=prov,
        detail=detail,
        matched=matched,
    )


def _evaluate_year_min(c: HardConstraint, vehicle: Vehicle) -> RequirementResult:
    if vehicle.year is None:
        return _hard_result(c, "needs_review", Provenance.unknown, "model year unknown", None)
    if vehicle.year >= c.min_year:  # type: ignore[operator]
        return _hard_result(c, "pass", Provenance.verified, f"year {vehicle.year}", True)
    return _hard_result(
        c, "fail", Provenance.verified, f"year {vehicle.year} < {c.min_year}", False
    )


def _resolve_require(
    c: HardConstraint, o: MatchOutcome, review_on_unknown: bool
) -> RequirementResult:
    # Only a verified-True satisfies a hard requirement.
    if o.matched is True and o.provenance == Provenance.verified:
        return _hard_result(c, "pass", Provenance.verified, "verified present", True)
    if o.matched is False and o.provenance == Provenance.verified:
        return _hard_result(c, "fail", Provenance.verified, "verified absent", False)
    if o.matched is True and o.provenance == Provenance.claimed:
        # Promising but unconfirmed — never a silent pass.
        return _hard_result(c, "needs_review", Provenance.claimed, "claimed only", True)
    # unknown
    status = "needs_review" if review_on_unknown else "fail"
    return _hard_result(c, status, Provenance.unknown, "no evidence", None)


def _resolve_forbid(
    c: HardConstraint, o: MatchOutcome, review_on_unknown: bool
) -> RequirementResult:
    if o.matched is True and o.provenance == Provenance.verified:
        return _hard_result(c, "fail", Provenance.verified, "forbidden option present", True)
    if o.matched is False and o.provenance == Provenance.verified:
        return _hard_result(c, "pass", Provenance.verified, "verified absent", False)
    if o.matched is True and o.provenance == Provenance.claimed:
        return _hard_result(c, "needs_review", Provenance.claimed, "claimed present", True)
    status = "needs_review" if review_on_unknown else "fail"
    return _hard_result(c, status, Provenance.unknown, "no evidence", None)


# --- Soft dimensions -------------------------------------------------------


@dataclass(frozen=True)
class _SoftEval:
    result: RequirementResult
    breach: bool  # exceeded a hard bound flagged exclude_beyond_hard


def _numeric_score(c: SoftConstraint, value: float) -> float:
    """Linear penalty between ideal (100) and hard (0), clamped to [0, 100]."""
    ideal, hard = c.ideal, c.hard  # type: ignore[assignment]
    if c.direction == "lower_better":
        if value <= ideal:
            return 100.0
        if value >= hard:
            return 0.0
        return 100.0 * (hard - value) / (hard - ideal)
    # higher_better
    if value >= ideal:
        return 100.0
    if value <= hard:
        return 0.0
    return 100.0 * (value - hard) / (ideal - hard)


def _evaluate_soft(c: SoftConstraint, vehicle: Vehicle, model_key: str) -> _SoftEval:
    if c.kind == "numeric":
        return _evaluate_numeric(c, vehicle)
    return _evaluate_color(c, vehicle, model_key)


def _soft_result(c, status, prov, detail, dim_score, **extra) -> RequirementResult:
    return RequirementResult(
        key=c.key,
        label=c.label,
        group="soft",
        status=status,
        provenance=prov,
        detail=detail,
        dim_score=dim_score,
        weight=c.weight,
        **extra,
    )


def _evaluate_numeric(c: SoftConstraint, vehicle: Vehicle) -> _SoftEval:
    value = getattr(vehicle, c.attr)  # type: ignore[arg-type]
    if value is None:
        # Unknown price/miles score a neutral 50 AT FULL WEIGHT instead of
        # dropping out of the weighted mean. Exclusion renormalizes the
        # remaining dims — a no-listing car's color would carry ~100% of the
        # soft score and swing the total to the extremes.
        return _SoftEval(
            _soft_result(
                c,
                "scored",
                Provenance.unknown,
                f"{c.attr} unknown -> 50 (neutral until known)",
                50.0,
            ),
            False,
        )
    score = _numeric_score(c, float(value))
    breach = False
    if c.exclude_beyond_hard:
        beyond = (c.direction == "lower_better" and value > c.hard) or (
            c.direction == "higher_better" and value < c.hard
        )
        breach = bool(beyond)
    detail = f"{c.attr}={value:g} -> {score:.0f}"
    return _SoftEval(_soft_result(c, "scored", Provenance.claimed, detail, score), breach)


def _evaluate_color(c: SoftConstraint, vehicle: Vehicle, model_key: str) -> _SoftEval:
    family = colors.family_from_rpo(model_key, vehicle.paint_rpo)
    prov = Provenance.verified
    if family is None:
        family = colors.family_from_text(vehicle.color_text)
        prov = Provenance.claimed if family is not None else Provenance.unknown

    if family is None:
        return _SoftEval(
            _soft_result(c, "scored", Provenance.unknown, "color unknown", None), False
        )

    name = family.value
    if name in c.preferred:
        score = 100.0
    elif name in c.penalized:
        score = 0.0
    else:
        score = c.neutral_score
    # Show the actual paint name next to the family when it adds information:
    # "White (Rift Metallic) -> 0", but never "White (White) -> 100". The
    # family is title-cased to match the match-list Color column exactly. The
    # resolved family + paint name also ride along as structured fields so
    # every page renders the same color this scorer used.
    paint = (vehicle.color_text or "").strip()
    label = f"{name.title()} ({paint.title()})" if paint and paint.lower() != name else name.title()
    return _SoftEval(
        _soft_result(
            c,
            "scored",
            prov,
            f"{label} -> {score:.0f}",
            score,
            color_family=name,
            paint_name=paint or None,
        ),
        False,
    )


# --- Highlights ------------------------------------------------------------


def _evaluate_highlight(h: Highlight, vehicle: Vehicle) -> RequirementResult:
    outcome = match(h.matcher, vehicle, key=h.label)
    applied = h.bonus if outcome.matched is True else 0.0
    return RequirementResult(
        key=h.label,
        label=h.label,
        group="highlight",
        status="badge",
        provenance=outcome.provenance if outcome.matched else Provenance.unknown,
        detail=f"+{applied:g}" if applied else "not present",
        matched=outcome.matched is True,
        bonus=applied,
    )


# --- Top level -------------------------------------------------------------


def _weighted_soft_score(evals: list[_SoftEval]) -> float:
    """Weighted mean over dimensions that produced a score. Unknowns excluded
    and the remaining weights renormalized. No known dims -> 0."""
    known = [e for e in evals if e.result.dim_score is not None]
    total_weight = sum(e.result.weight for e in known)  # type: ignore[misc]
    if total_weight <= 0:
        return 0.0
    return sum(e.result.dim_score * e.result.weight for e in known) / total_weight  # type: ignore[misc]


def score_vehicle(profile: Profile, vehicle: Vehicle) -> ScoreResult:
    hard = [_evaluate_hard(c, vehicle, profile.review_on_unknown_hard) for c in profile.hard]

    soft_evals = [_evaluate_soft(c, vehicle, profile.model_key) for c in profile.soft]

    highlights = [_evaluate_highlight(h, vehicle) for h in profile.highlights]

    soft_score = _weighted_soft_score(soft_evals)
    highlight_bonus = min(sum(h.bonus or 0.0 for h in highlights), HIGHLIGHT_CAP)

    hard_fail = any(r.status == "fail" for r in hard)
    soft_breach = any(e.breach for e in soft_evals)
    needs_review = any(r.status == "needs_review" for r in hard)

    if hard_fail or soft_breach:
        verdict = "excluded"
    elif needs_review:
        verdict = "needs_review"
    else:
        verdict = "ranked"

    return ScoreResult(
        vin=vehicle.vin,
        verdict=verdict,
        soft_score=round(soft_score, 4),
        highlight_bonus=highlight_bonus,
        total=round(soft_score + highlight_bonus, 4),
        hard=hard,
        soft=[e.result for e in soft_evals],
        highlights=highlights,
    )
