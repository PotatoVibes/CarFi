"""Build Auto.dev prefilter params from a requirement profile.

Prefilter bounds are intentionally WIDER than the scorer's hard bounds: the
server should never exclude a car the deterministic scorer might still want to
see (e.g. a slightly-over-budget car that a price drop could rescue). The
scorer, not the API, is the source of truth.
"""

from __future__ import annotations

from .models import Profile

# Fields we pull back on every listing. Auto.dev returns these as flat,
# dot-keyed properties when `select` is present. Kept centrally so the
# normalizer and the projection stay in sync.
SELECT_FIELDS = (
    "vehicle.vin",
    "vehicle.year",
    "vehicle.make",
    "vehicle.model",
    "vehicle.trim",
    "vehicle.mileage",
    "vehicle.exteriorColor",
    "retailListing.price",
    "retailListing.vdpUrl",
    "createdAt",
)


def _year_min(profile: Profile) -> int | None:
    for c in profile.hard:
        if c.kind == "year_min":
            return c.min_year
    return None


def _price_hard_bound(profile: Profile) -> float | None:
    for c in profile.soft:
        if c.kind == "numeric" and c.attr == "price":
            return c.hard
    return None


def build_listing_params(profile: Profile, current_year: int) -> dict[str, str]:
    """Return Auto.dev query params (excluding pagination/sort) for a profile.

    Raises ValueError if the profile has no `query` block.
    """
    q = profile.query
    if q is None:
        raise ValueError(f"profile {profile.model_key!r} has no query block")

    year_lo = q.year_min if q.year_min is not None else _year_min(profile)
    year_hi = q.year_max if q.year_max is not None else current_year + 1

    price_hi = q.price_max
    if price_hi is None:
        hard = _price_hard_bound(profile)
        # Widen the scorer's hard price ceiling so fuzzy-band cars survive
        # the server-side filter.
        price_hi = round(hard * (1 + q.price_margin)) if hard is not None else None

    params: dict[str, str] = {
        "vehicle.make": q.make,
        "vehicle.model": q.model,
    }
    if year_lo is not None and year_hi is not None:
        params["vehicle.year"] = f"{year_lo}-{year_hi}"
    elif year_lo is not None:
        params["vehicle.year"] = str(year_lo)
    if price_hi is not None:
        # Lower bound 1 drops $0 "call for price" placeholder rows.
        params["retailListing.price"] = f"1-{int(price_hi)}"
    return params
