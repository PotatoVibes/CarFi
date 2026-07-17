"""Read-only queries for the UI. Pure DB reads — no I/O beyond SQLite, so
every page renders without any API key present.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

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


def ranked_matches(
    conn: sqlite3.Connection,
    model_key: str,
    include_excluded: bool = False,
    since: str | None = None,
    include_dismissed: bool = False,
) -> list[MatchRow]:
    """Ranked matches for a model. Excluded and manually-dismissed cars are
    hidden unless requested. Rows with first_seen > `since` are flagged is_new
    (for 'new since visit')."""
    rows = conn.execute(
        """
        SELECT v.vin, m.model_key, v.year, v.make, v.model, v.trim, v.color,
               v.miles, v.price, m.verdict, m.total, m.soft_score, m.highlight_bonus,
               v.status, s.status AS sticker_status, v.first_seen, v.dismissed_at
        FROM match_results m
        JOIN vehicles v ON v.vin = m.vin
        LEFT JOIN stickers s ON s.vin = v.vin
        WHERE m.model_key = ?
        """,
        (model_key,),
    ).fetchall()

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
        )
        for r in rows
    ]
    if not include_excluded:
        matches = [m for m in matches if m.verdict != "excluded"]
    if not include_dismissed:
        matches = [m for m in matches if not m.dismissed]
    matches.sort(key=lambda m: (m.dismissed, _VERDICT_RANK.get(m.verdict, 9), -m.total))
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


def vehicle_detail(conn: sqlite3.Connection, vin: str) -> VehicleDetail | None:
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
