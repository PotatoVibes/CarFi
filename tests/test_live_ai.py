"""Live Haiku smoke test — opt-in only (`pytest -m live`), counted against the
Anthropic budget. Skipped in the default offline profile and when
ANTHROPIC_API_KEY is unset. One call costs a fraction of a cent.
"""

from __future__ import annotations

import pytest

from carfinder.ai import Attribute, HaikuExtractor
from carfinder.budget import ANTHROPIC, BudgetGuard
from carfinder.config import load_settings
from carfinder.db import bootstrap

pytestmark = pytest.mark.live


def test_haiku_structured_output_contract():
    settings = load_settings()
    if not settings.anthropic_api_key:
        pytest.skip("no ANTHROPIC_API_KEY")
    conn = bootstrap(":memory:")
    guard = BudgetGuard.from_settings(conn, settings)
    ex = HaikuExtractor(conn, guard, settings.anthropic_api_key)
    attrs = [
        Attribute(key="front_lift", label="Front lift w/ adjustable height"),
        Attribute(key="sunroof", label="Power sunroof"),
    ]
    result = ex.extract(
        "2023 Corvette Stingray convertible. Equipped with front lift and Z51. No sunroof.",
        attrs,
    )
    # Real structured output must return exactly our keys with valid statuses.
    assert set(result.features) == {"front_lift", "sunroof"}
    assert all(v in ("claimed", "denied", "not_mentioned") for v in result.features.values())
    assert result.features["front_lift"] == "claimed"
    assert result.cost_usd > 0
    assert guard.usage(ANTHROPIC).used > 0
