"""Zip-centroid distance for "how far away is this car".

Uses the offline `zipcodes` dataset (US zip -> centroid lat/lon) — no network,
no API key. Centroid-to-centroid distance is city-level accurate (a few miles),
which is exactly the fidelity a "worth the drive / needs shipping" glance needs.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache

import zipcodes

_ZIP_RE = re.compile(r"^\d{5}")
_EARTH_RADIUS_MI = 3958.8


@lru_cache(maxsize=4096)
def zip_latlon(zip_code: str | None) -> tuple[float, float] | None:
    """Centroid (lat, lon) for a US zip, or None if unknown/malformed."""
    m = _ZIP_RE.match(str(zip_code or "").strip())
    if not m:
        return None
    try:
        matches = zipcodes.matching(m.group(0))
    except (TypeError, ValueError):
        return None
    if not matches or not matches[0].get("lat") or not matches[0].get("long"):
        return None
    return float(matches[0]["lat"]), float(matches[0]["long"])


def zip_distance_miles(zip_a: str | None, zip_b: str | None) -> int | None:
    """Great-circle miles between two zip centroids (None if either unknown)."""
    a, b = zip_latlon(zip_a), zip_latlon(zip_b)
    if a is None or b is None:
        return None
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return round(2 * _EARTH_RADIUS_MI * math.asin(math.sqrt(h)))
