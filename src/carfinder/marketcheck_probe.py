"""Minimal live probe for Marketcheck's undocumented free-tier radius behavior.

The docs confirm 500 calls/mo + 5 req/s and mention a "100-mile radius
restriction" on the free tier, but they never state whether a location-free
query returns a national result set, or whether radius is *capped* at 100mi vs
merely *defaulted*. Those specifics decide whether Marketcheck is usable for a
nationwide, color-constrained C8/Blackwing hunt — so we settle them empirically
with a fixed, tiny set of calls instead of guessing.

Every call goes through BudgetGuard under the ``marketcheck`` provider, like all
other outbound calls. Three queries answer the two open questions:

  1. national  — make/model/year only, no location
  2. wide      — same + zip, radius=2000  (absurdly large on purpose)
  3. local     — same + zip, radius=100

Evidence read off the responses:
  * ``num_found`` per query (Marketcheck's total-hits count)
  * distinct dealer states in the first page

Verdicts:
  * national returns many states + a large count  -> location-free national
    search works.
  * wide count ~= local count                     -> radius is CAPPED at ~100mi.
  * wide count >> local count                      -> radius is honored.

Defaults probe a high-volume vehicle (Ford F-150) so radius=100 returns plenty
and the mechanics are unambiguous — the behavior is model-independent. Point it
at the real target with --make/--model/--year-min/--year-max if you want.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .sources import MarketcheckSource
from .sources import scrub_api_key as scrub


@dataclass
class ProbeResult:
    label: str
    num_found: int | None = None
    rows_returned: int = 0
    states: dict[str, int] = field(default_factory=dict)
    error: str | None = None


def _base_params(make: str, model: str, year_lo: int, year_hi: int) -> dict[str, str]:
    return {
        "make": make,
        "model": model,
        "year": f"{year_lo}-{year_hi}",
        "sort_by": "last_seen",
        "sort_order": "desc",
    }


def run_probe(
    source: MarketcheckSource,
    make: str,
    model: str,
    year_lo: int,
    year_hi: int,
    zip_code: str,
    run_id: str | None = None,
) -> list[ProbeResult]:
    """Run the three probe queries, returning a ProbeResult each. A failed
    query is captured (never raised) so one dead call can't sink the run."""
    base = _base_params(make, model, year_lo, year_hi)
    queries = [
        ("national (no location)", base),
        (f"radius=2000 @ {zip_code}", {**base, "zip": zip_code, "radius": "2000"}),
        (f"radius=100 @ {zip_code}", {**base, "zip": zip_code, "radius": "100"}),
    ]
    results: list[ProbeResult] = []
    for label, params in queries:
        try:
            page = source.search_page(params, page=1, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 — capture everything as evidence
            results.append(ProbeResult(label, error=scrub(str(exc)) or type(exc).__name__))
            continue
        states = Counter(r.state for r in page.rows if r.state)
        results.append(
            ProbeResult(
                label=label,
                num_found=page.total,
                rows_returned=len(page.rows),
                states=dict(states),
            )
        )
    return results


def interpret(results: list[ProbeResult]) -> list[str]:
    """Turn the three ProbeResults into plain-language verdict lines."""
    national = next((r for r in results if r.label.startswith("national")), None)
    wide = next((r for r in results if r.label.startswith("radius=2000")), None)
    local = next((r for r in results if r.label.startswith("radius=100")), None)

    lines: list[str] = []

    # Q1 — does a location-free query work, and is it national?
    if national is None:
        lines.append("national query missing — probe did not run it.")
    elif national.error:
        lines.append(
            f"Location-free query FAILED ({national.error}) -> a location is "
            "likely REQUIRED on the free tier."
        )
    elif not national.num_found:
        lines.append(
            "Location-free query returned 0 hits -> either national search is "
            "blocked or the model is too rare; retry with a denser --make/--model."
        )
    elif len(national.states) >= 4:
        lines.append(
            f"Location-free query WORKS: {national.num_found:,} hits, "
            f"{len(national.states)} states on the first page -> NATIONAL search "
            "is available (no radius forced)."
        )
    else:
        lines.append(
            f"Location-free query returned {national.num_found:,} hits but only "
            f"{len(national.states)} state(s) on the first page -> possibly "
            "region-clustered; inspect the state histogram below before trusting it."
        )

    # Q2 — is radius capped at 100mi, or honored?
    if wide and local and wide.num_found is not None and local.num_found is not None:
        if local.num_found == 0:
            lines.append(
                "radius=100 returned 0 hits -> can't compare radius behavior; "
                "try a denser zip or a higher-volume model."
            )
        elif abs(wide.num_found - local.num_found) <= max(2, 0.1 * local.num_found):
            lines.append(
                f"radius=2000 ({wide.num_found:,}) ~= radius=100 ({local.num_found:,}) "
                "-> RADIUS IS CAPPED at ~100mi on the free tier. A nationwide hunt "
                "would need many zip anchors (see the budget math in the eval)."
            )
        elif wide.num_found > local.num_found:
            lines.append(
                f"radius=2000 ({wide.num_found:,}) > radius=100 ({local.num_found:,}) "
                "-> radius is HONORED beyond 100mi (the free-tier restriction may "
                "not be enforced on this endpoint)."
            )
        else:
            lines.append(
                f"radius=2000 ({wide.num_found:,}) < radius=100 ({local.num_found:,}) "
                "-> unexpected; inspect the raw counts."
            )
    else:
        lines.append("radius comparison unavailable (a radius query failed — see errors above).")

    return lines
