"""BudgetGuard tests: cap enforcement, month rollover, threshold warnings.

Fully offline — no HTTP client is ever constructed.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from carfinder import db
from carfinder.budget import (
    ANTHROPIC,
    AUTODEV,
    COST,
    COUNT,
    BudgetExceeded,
    BudgetGuard,
    ProviderBudget,
)

JULY = datetime(2026, 7, 15, 12, tzinfo=UTC)


def _guard(providers: dict[str, ProviderBudget]) -> BudgetGuard:
    return BudgetGuard(db.bootstrap(":memory:"), providers)


def _count_guard(cap: float) -> BudgetGuard:
    return _guard({AUTODEV: ProviderBudget(AUTODEV, COUNT, cap)})


def _cost_guard(cap: float) -> BudgetGuard:
    return _guard({ANTHROPIC: ProviderBudget(ANTHROPIC, COST, cap)})


# --- count cap -------------------------------------------------------------


def test_count_cap_allows_exactly_cap_calls_then_blocks():
    guard = _count_guard(3)
    for i in range(3):
        with guard.reserve(AUTODEV, f"call{i}", now=JULY):
            pass
    assert guard.usage(AUTODEV, now=JULY).used == 3
    with pytest.raises(BudgetExceeded):
        guard.check(AUTODEV, now=JULY)


def test_blocked_call_is_not_recorded():
    guard = _count_guard(1)
    with guard.reserve(AUTODEV, "first", now=JULY):
        pass
    with pytest.raises(BudgetExceeded), guard.reserve(AUTODEV, "second", now=JULY):
        pass  # pragma: no cover - body never runs
    assert guard.usage(AUTODEV, now=JULY).used == 1


def test_check_passes_up_to_the_cap_boundary():
    guard = _count_guard(3)
    for i in range(2):
        with guard.reserve(AUTODEV, f"c{i}", now=JULY):
            pass
    # used=2, cap=3: the 3rd call is still allowed.
    guard.check(AUTODEV, now=JULY)


# --- cost cap --------------------------------------------------------------


def test_cost_cap_blocks_when_projected_spend_exceeds():
    guard = _cost_guard(2.0)
    db.record_call(guard._conn, ANTHROPIC, "x", JULY, cost_usd=1.5)
    guard.check(ANTHROPIC, cost_usd=0.5, now=JULY)  # 1.5 + 0.5 == 2.0, allowed
    with pytest.raises(BudgetExceeded):
        guard.check(ANTHROPIC, cost_usd=0.6, now=JULY)  # 2.1 > 2.0


# --- month rollover --------------------------------------------------------


def test_usage_resets_across_calendar_month():
    guard = _count_guard(800)
    for i in range(5):
        with guard.reserve(AUTODEV, f"jul{i}", now=datetime(2026, 7, 20, tzinfo=UTC)):
            pass
    assert guard.usage(AUTODEV, now=datetime(2026, 7, 31, tzinfo=UTC)).used == 5
    # New calendar month → fresh budget.
    assert guard.usage(AUTODEV, now=datetime(2026, 8, 1, tzinfo=UTC)).used == 0


def test_december_to_january_rollover():
    guard = _count_guard(800)
    with guard.reserve(AUTODEV, "dec", now=datetime(2026, 12, 31, 23, tzinfo=UTC)):
        pass
    assert guard.usage(AUTODEV, now=datetime(2027, 1, 1, tzinfo=UTC)).used == 0


# --- threshold warnings ----------------------------------------------------


def _fill(guard: BudgetGuard, n: int) -> None:
    for i in range(n):
        with guard.reserve(AUTODEV, f"f{i}", now=JULY):
            pass


def test_warns_when_crossing_each_threshold(caplog):
    guard = _count_guard(100)
    with caplog.at_level(logging.WARNING, logger="carfinder.budget"):
        _fill(guard, 49)
        guard.check(AUTODEV, now=JULY)  # projected 50 -> 50%
        _fill(guard, 25)  # used now 74
        guard.check(AUTODEV, now=JULY)  # projected 75 -> 75%
        _fill(guard, 15)  # used now 89
        guard.check(AUTODEV, now=JULY)  # projected 90 -> 90%
    pcts = [r.getMessage() for r in caplog.records]
    assert any("50%" in m for m in pcts)
    assert any("75%" in m for m in pcts)
    assert any("90%" in m for m in pcts)


def test_no_warning_below_first_threshold(caplog):
    guard = _count_guard(100)
    with caplog.at_level(logging.WARNING, logger="carfinder.budget"):
        _fill(guard, 10)
        guard.check(AUTODEV, now=JULY)  # projected 11 -> 11%
    assert caplog.records == []


# --- introspection ---------------------------------------------------------


def test_usage_fraction_and_remaining():
    guard = _count_guard(100)
    _fill(guard, 25)
    u = guard.usage(AUTODEV, now=JULY)
    assert u.used == 25
    assert u.remaining == 75
    assert u.fraction == pytest.approx(0.25)


def test_unknown_provider_raises():
    guard = _count_guard(10)
    with pytest.raises(KeyError):
        guard.check("nope", now=JULY)
