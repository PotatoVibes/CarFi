"""Read-only queries for the UI. Pure DB reads — no I/O beyond SQLite, so
every page renders without any API key present.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import geo

# Verdict ordering for ranked lists: ranked cars first, then needs-review,
# then excluded (usually hidden). Higher total breaks ties within a verdict.
_VERDICT_RANK = {"ranked": 0, "needs_review": 1, "excluded": 2}


@dataclass
class MatchRow:
    vin: str
    model_key: str
    year: int | None
    make: str | None
    model: str | None
    trim: str | None
    color: str | None
    miles: int | None
    price: float | None
    verdict: str
    total: float
    soft_score: float
    highlight_bonus: float
    status: str  # active | presumed_sold
    sticker_status: str | None
    first_seen: str
    is_new: bool = False
    dismissed: bool = False
    favorited: bool = False
    city: str | None = None
    state: str | None = None
    distance_mi: int | None = None  # from the configured home zip
    color_family: str | None = None  # scorer-resolved (match_results)
    paint_name: str | None = None

    @property
    def location(self) -> str | None:
        if self.city and self.state:
            return f"{self.city}, {self.state}"
        return self.state or None

    @property
    def color_display(self) -> str | None:
        """What the Color column shows: the generic family the scorer resolved
        ("White" — matching the detail page's label), else the listing's text.
        The specific paint name goes in the hover tooltip (paint_display)."""
        if self.color_family:
            return self.color_family.title()
        return self.color

    @property
    def paint_display(self) -> str | None:
        if self.paint_name and self.paint_name.lower() != (self.color_family or ""):
            return self.paint_name.title()
        return None


def ranked_matches(
    conn: sqlite3.Connection,
    model_key: str,
    include_excluded: bool = False,
    since: str | None = None,
    include_dismissed: bool = False,
    home_zip: str | None = None,
) -> list[MatchRow]:
    """Ranked matches for a model. Excluded and manually-dismissed cars are
    hidden unless requested. Rows with first_seen > `since` are flagged is_new
    (for 'new since visit'). With `home_zip`, distance_mi is computed from it
    to each car's listing zip (centroid-to-centroid, offline)."""
    # Filter in SQL, not Python: the default view shows a few dozen cars out of
    # thousands of rows, and fetching the excluded bulk just to drop it was the
    # slowest part of every board render.
    sql = """
        SELECT v.vin, m.model_key, v.year, v.make, v.model, v.trim, v.color,
               v.miles, v.price, m.verdict, m.total, m.soft_score, m.highlight_bonus,
               v.status, s.status AS sticker_status, v.first_seen, v.dismissed_at,
               v.favorited_at, v.city, v.state, v.zip, m.color_family, m.paint_name
        FROM match_results m
        JOIN vehicles v ON v.vin = m.vin
        LEFT JOIN stickers s ON s.vin = v.vin
        WHERE m.model_key = ?
        """
    if not include_excluded:
        sql += " AND m.verdict != 'excluded'"
    if not include_dismissed:
        sql += " AND v.dismissed_at IS NULL"
    rows = conn.execute(sql, (model_key,)).fetchall()

    matches = [
        MatchRow(
            vin=r["vin"],
            model_key=r["model_key"],
            year=r["year"],
            make=r["make"],
            model=r["model"],
            trim=r["trim"],
            color=r["color"],
            miles=r["miles"],
            price=r["price"],
            verdict=r["verdict"],
            total=r["total"],
            soft_score=r["soft_score"],
            highlight_bonus=r["highlight_bonus"],
            status=r["status"],
            sticker_status=r["sticker_status"],
            first_seen=r["first_seen"],
            is_new=bool(since and r["first_seen"] and r["first_seen"] > since),
            dismissed=bool(r["dismissed_at"]),
            favorited=bool(r["favorited_at"]),
            city=r["city"],
            state=r["state"],
            distance_mi=geo.zip_distance_miles(home_zip, r["zip"]) if home_zip else None,
            color_family=r["color_family"],
            paint_name=r["paint_name"],
        )
        for r in rows
    ]
    # Verdict, then score (desc), then price (asc, unpriced last); dismissed
    # sink to the bottom. Favorites keep their heart but no longer jump the
    # ranking — the owner reads the list as a pure leaderboard.
    matches.sort(
        key=lambda m: (
            m.dismissed,
            _VERDICT_RANK.get(m.verdict, 9),
            -m.total,
            m.price is None,
            m.price if m.price is not None else 0.0,
        )
    )
    return matches


@dataclass
class VehicleDetail:
    vin: str
    vehicle: dict[str, Any]
    match: dict[str, Any] | None
    breakdown: dict[str, Any]
    sticker: dict[str, Any] | None
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    price_history: list[dict[str, Any]] = field(default_factory=list)
    listing_url: str | None = None
    distance_mi: int | None = None  # from the configured home zip


def vehicle_detail(
    conn: sqlite3.Connection, vin: str, home_zip: str | None = None
) -> VehicleDetail | None:
    v = conn.execute("SELECT * FROM vehicles WHERE vin = ?", (vin,)).fetchone()
    if v is None:
        return None
    m = conn.execute("SELECT * FROM match_results WHERE vin = ?", (vin,)).fetchone()
    breakdown = json.loads(m["breakdown_json"]) if m else {"hard": [], "soft": [], "highlights": []}
    s = conn.execute("SELECT * FROM stickers WHERE vin = ?", (vin,)).fetchone()
    conflicts = [
        {"field": c["field"], "winner": c["winner"], "values": json.loads(c["values_json"])}
        for c in conn.execute("SELECT * FROM conflicts WHERE vin = ?", (vin,)).fetchall()
    ]
    prices = [
        {"price": p["price"], "observed_at": p["observed_at"]}
        for p in conn.execute(
            "SELECT price, observed_at FROM price_history WHERE vin = ? ORDER BY id", (vin,)
        ).fetchall()
    ]
    # LIKE 'http%' also filters junk fragment "urls" already stored before
    # the ingest-side fix (Auto.dev sends e.g. '#-497629...' for some rows).
    listing = conn.execute(
        "SELECT url FROM listings WHERE vin = ? AND url LIKE 'http%' "
        "ORDER BY last_seen DESC LIMIT 1",
        (vin,),
    ).fetchone()
    return VehicleDetail(
        vin=vin,
        vehicle=dict(v),
        match=dict(m) if m else None,
        breakdown=breakdown,
        sticker=dict(s) if s else None,
        conflicts=conflicts,
        price_history=prices,
        listing_url=listing["url"] if listing else None,
        distance_mi=geo.zip_distance_miles(home_zip, v["zip"]) if home_zip else None,
    )


def all_conflicts(conn: sqlite3.Connection, include_excluded: bool = False) -> list[dict[str, Any]]:
    """Cross-source conflicts, hiding excluded cars by default — a conflict on
    a car that already hard-failed is noise, not something to review."""
    rows = conn.execute(
        """
        SELECT c.vin, c.field, c.winner, c.values_json, c.detected_at,
               v.year, v.make, v.model, m.verdict
        FROM conflicts c
        LEFT JOIN vehicles v ON v.vin = c.vin
        LEFT JOIN match_results m ON m.vin = c.vin
        ORDER BY c.detected_at DESC, c.vin
        """
    ).fetchall()
    return [
        {
            "vin": r["vin"],
            "field": r["field"],
            "winner": r["winner"],
            "values": json.loads(r["values_json"]),
            "detected_at": r["detected_at"],
            "year": r["year"],
            "make": r["make"],
            "model": r["model"],
            "verdict": r["verdict"],
        }
        for r in rows
        if include_excluded or r["verdict"] != "excluded"
    ]


def match_counts(conn: sqlite3.Connection, model_key: str) -> dict[str, int]:
    """Count of each verdict for a model (for header badges). Manually
    dismissed cars are counted separately — they're handled, not pending."""
    rows = conn.execute(
        """SELECT m.verdict, COUNT(*) AS n FROM match_results m
           JOIN vehicles v ON v.vin = m.vin
           WHERE m.model_key = ? AND v.dismissed_at IS NULL
           GROUP BY m.verdict""",
        (model_key,),
    ).fetchall()
    counts = {"ranked": 0, "needs_review": 0, "excluded": 0}
    for r in rows:
        counts[r["verdict"]] = r["n"]
    counts["dismissed"] = conn.execute(
        """SELECT COUNT(*) FROM match_results m JOIN vehicles v ON v.vin = m.vin
           WHERE m.model_key = ? AND v.dismissed_at IS NOT NULL""",
        (model_key,),
    ).fetchone()[0]
    return counts


def latest_sync(conn: sqlite3.Connection) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(r) if r else None


# --- Unicorn arrival tracking ----------------------------------------------
# A "unicorn" clears every hard requirement AND lands in a preferred color, at
# or under the mileage cap, with a highlight bonus at/above a threshold. The
# point of tracking them over time is to measure how often one actually appears
# so a wait can be planned. first_seen (when *we* first ingested the VIN) is the
# arrival clock — it only becomes a real arrival rate once the tool has been
# syncing incrementally for a sustained window; a one-time bulk backfill stamps
# everything with a single date (the CLI surfaces that so the history isn't
# mistaken for observed arrivals).


@dataclass
class UnicornRow:
    vin: str
    model_key: str
    first_seen: str
    created_at: str | None  # source's listing-live date (survivorship-biased)
    dealer: str | None
    state: str | None
    price: float | None
    miles: int | None
    color_family: str | None
    paint_name: str | None
    total: float
    highlight_bonus: float
    status: str
    dismissed: bool
    favorited: bool


@dataclass
class UnicornMonth:
    month: str  # 'YYYY-MM' of first_seen
    cars: int  # raw count of qualifying VINs
    events: int  # distinct (dealer, day) clusters — guards against one dealer
    # listing a batch of near-identical cars reading as many independent arrivals
    states: list[str]


def unicorn_arrivals(
    conn: sqlite3.Connection,
    model_key: str,
    preferred_colors: list[str],
    min_bonus: float = 5.0,
    miles_cap: int = 60000,
    since: str | None = None,
    include_dismissed: bool = True,
) -> list[UnicornRow]:
    """Cars meeting the unicorn bar for a model, ordered by first_seen (arrival).

    `preferred_colors` and `miles_cap` come from the profile so this stays in
    sync with the config rather than hardcoding the definition. Price is not
    filtered here: the scorer already excludes over-budget cars (price soft
    constraint with exclude_beyond_hard), so any 'ranked' car is within budget.
    Dismissed cars are kept by default — a dismissal is a per-car decision that
    shouldn't distort the market-rate measurement.
    """
    if not preferred_colors:
        return []
    placeholders = ",".join("?" for _ in preferred_colors)
    params: list[Any] = [model_key, min_bonus, miles_cap, *[c.lower() for c in preferred_colors]]
    sql = f"""
        SELECT v.vin, m.model_key, v.first_seen,
               (SELECT MIN(l.created_at) FROM listings l
                WHERE l.vin = m.vin AND l.created_at IS NOT NULL) AS created_at,
               v.dealer, v.state,
               v.price, v.miles, m.color_family, m.paint_name, m.total,
               m.highlight_bonus, v.status, v.dismissed_at, v.favorited_at
        FROM match_results m
        JOIN vehicles v ON v.vin = m.vin
        WHERE m.model_key = ?
          AND m.verdict = 'ranked'
          AND m.highlight_bonus >= ?
          AND v.miles IS NOT NULL AND v.miles <= ?
          AND LOWER(m.color_family) IN ({placeholders})
    """  # noqa: S608 -- placeholders are '?' count only; all values are bound params
    if since:
        sql += " AND v.first_seen >= ?"
        params.append(since)
    sql += " ORDER BY v.first_seen, v.vin"
    rows = conn.execute(sql, params).fetchall()
    result = [
        UnicornRow(
            vin=r["vin"],
            model_key=r["model_key"],
            first_seen=r["first_seen"],
            created_at=r["created_at"],
            dealer=r["dealer"],
            state=r["state"],
            price=r["price"],
            miles=r["miles"],
            color_family=r["color_family"],
            paint_name=r["paint_name"],
            total=r["total"],
            highlight_bonus=r["highlight_bonus"],
            status=r["status"],
            dismissed=bool(r["dismissed_at"]),
            favorited=bool(r["favorited_at"]),
        )
        for r in rows
    ]
    if not include_dismissed:
        result = [u for u in result if not u.dismissed]
    return result


def unicorn_rate_by_month(rows: list[UnicornRow]) -> list[UnicornMonth]:
    """Aggregate unicorn arrivals by first_seen month. `events` collapses each
    (dealer, day) into one — a dealer listing a batch of near-identical cars is
    one arrival opportunity, not N, and shouldn't inflate the observed rate."""
    from collections import defaultdict

    buckets: dict[str, list[UnicornRow]] = defaultdict(list)
    for r in rows:
        buckets[r.first_seen[:7]].append(r)
    out: list[UnicornMonth] = []
    for month in sorted(buckets):
        group = buckets[month]
        # Null dealer → its own event (keyed by VIN) so distinct cars aren't merged.
        events = {(r.dealer or r.vin, r.first_seen[:10]) for r in group}
        states = sorted({r.state for r in group if r.state})
        out.append(UnicornMonth(month=month, cars=len(group), events=len(events), states=states))
    return out


def observation_window(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """first_seen span + the single busiest ingest day (a bulk backfill shows up
    as one day holding most rows). Lets callers flag how much of the arrival
    history is genuinely observed vs. stamped by a one-time load."""
    span = conn.execute(
        "SELECT MIN(first_seen) AS lo, MAX(first_seen) AS hi, COUNT(*) AS n FROM listings"
    ).fetchone()
    if span is None or span["n"] == 0:
        return None
    busiest = conn.execute(
        """SELECT substr(first_seen, 1, 10) AS day, COUNT(*) AS n
           FROM listings GROUP BY day ORDER BY n DESC LIMIT 1"""
    ).fetchone()
    return {
        "first": span["lo"],
        "last": span["hi"],
        "total": span["n"],
        "busiest_day": busiest["day"],
        "busiest_day_count": busiest["n"],
    }
