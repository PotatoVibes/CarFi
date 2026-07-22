"""BudgetGuard — the only sanctioned gate for outbound metered API calls.

Every live call must pass through here first. Two metering modes:

* count-metered (Auto.dev): capped on number of calls per calendar month.
* cost-metered  (Anthropic): capped on USD spend per calendar month.

At a threshold (50/75/90%) we log a warning; at the cap we raise
``BudgetExceeded`` *before* the call leaves the process. Month-to-date usage
is always read back from the api_calls ledger, so restarts never lose count.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from . import db
from .config import Settings

log = logging.getLogger("carfinder.budget")

# Provider identifiers (also the `provider` value stored in the ledger).
AUTODEV = "autodev"
ANTHROPIC = "anthropic"
GM_STICKER = "gm_sticker"
BUILDSHEET = "gm_buildsheet"
MARKETCHECK = "marketcheck"

# Metering kinds.
COUNT = "count"
COST = "cost"

# Fractions of cap at which we emit a warning as usage climbs past them.
WARN_THRESHOLDS: tuple[float, ...] = (0.50, 0.75, 0.90)


class BudgetExceeded(RuntimeError):
    """Raised when a call would push a provider past its monthly cap."""


@dataclass(frozen=True)
class ProviderBudget:
    name: str
    kind: str  # COUNT | COST
    cap: float


@dataclass(frozen=True)
class Usage:
    """Month-to-date usage snapshot for one provider."""

    provider: str
    kind: str
    used: float
    cap: float

    @property
    def fraction(self) -> float:
        return self.used / self.cap if self.cap > 0 else float("inf")

    @property
    def remaining(self) -> float:
        return self.cap - self.used


def _month_bounds(now: datetime) -> tuple[datetime, datetime]:
    """[start, end) of the calendar month containing ``now`` (UTC)."""
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


class BudgetGuard:
    """Gate + ledger recorder for metered providers."""

    def __init__(self, conn: sqlite3.Connection, providers: dict[str, ProviderBudget]):
        self._conn = conn
        self._providers = providers

    @classmethod
    def from_settings(cls, conn: sqlite3.Connection, settings: Settings) -> BudgetGuard:
        providers = {
            AUTODEV: ProviderBudget(AUTODEV, COUNT, float(settings.monthly_call_cap_autodev)),
            ANTHROPIC: ProviderBudget(ANTHROPIC, COST, float(settings.monthly_ai_budget_usd)),
            # GM sticker endpoint is free, but every outbound call is still
            # gated + recorded; the cap is a runaway-loop backstop, not a quota.
            GM_STICKER: ProviderBudget(GM_STICKER, COUNT, float(settings.monthly_call_cap_gm)),
            # Build-sheet decoder (gmpartsgiant): free + unofficial, so the
            # cap is a politeness backstop like the sticker endpoint's.
            BUILDSHEET: ProviderBudget(
                BUILDSHEET, COUNT, float(settings.monthly_call_cap_buildsheet)
            ),
            # Marketcheck secondary source (free tier = 500 calls/mo). A count
            # cap like Auto.dev's — no live call may bypass this gate.
            MARKETCHECK: ProviderBudget(
                MARKETCHECK, COUNT, float(settings.monthly_call_cap_marketcheck)
            ),
        }
        return cls(conn, providers)

    # --- introspection -----------------------------------------------------

    def _budget(self, provider: str) -> ProviderBudget:
        try:
            return self._providers[provider]
        except KeyError:
            raise KeyError(f"unknown provider {provider!r}") from None

    def _used(self, pb: ProviderBudget, now: datetime) -> float:
        start, end = _month_bounds(now)
        if pb.kind == COUNT:
            return float(db.count_calls(self._conn, pb.name, start, end))
        return db.sum_cost(self._conn, pb.name, start, end)

    def usage(self, provider: str, now: datetime | None = None) -> Usage:
        pb = self._budget(provider)
        now = now or datetime.now(UTC)
        return Usage(pb.name, pb.kind, self._used(pb, now), pb.cap)

    def all_usage(self, now: datetime | None = None) -> list[Usage]:
        now = now or datetime.now(UTC)
        return [self.usage(name, now) for name in self._providers]

    # --- gate --------------------------------------------------------------

    def check(self, provider: str, cost_usd: float = 0.0, now: datetime | None = None) -> None:
        """Raise ``BudgetExceeded`` if this call would exceed the monthly cap.

        Also logs a warning if the call crosses a 50/75/90% threshold.
        """
        pb = self._budget(provider)
        now = now or datetime.now(UTC)
        used = self._used(pb, now)
        increment = 1.0 if pb.kind == COUNT else cost_usd
        projected = used + increment

        if projected > pb.cap:
            raise BudgetExceeded(
                f"{pb.name}: call blocked — would reach {projected:g} of cap {pb.cap:g} "
                f"({pb.kind}) for the current calendar month"
            )
        self._warn_on_threshold_crossing(pb, used, projected)

    def _warn_on_threshold_crossing(
        self, pb: ProviderBudget, used: float, projected: float
    ) -> None:
        if pb.cap <= 0:
            return
        pre = used / pb.cap
        post = projected / pb.cap
        for t in WARN_THRESHOLDS:
            if pre < t <= post:
                log.warning(
                    "%s budget at %.0f%% of monthly cap (%g of %g %s)",
                    pb.name,
                    post * 100,
                    projected,
                    pb.cap,
                    pb.kind,
                )

    @contextmanager
    def reserve(
        self,
        provider: str,
        endpoint: str,
        run_id: str | None = None,
        cost_usd: float = 0.0,
        now: datetime | None = None,
    ) -> Iterator[None]:
        """Check the cap, record the call in the ledger, then run the body.

        The call is recorded *before* the body runs: passing the gate means the
        call is about to leave the process, so we never undercount quota.
        """
        now = now or datetime.now(UTC)
        self.check(provider, cost_usd=cost_usd, now=now)
        db.record_call(self._conn, provider, endpoint, now, run_id, cost_usd)
        yield
