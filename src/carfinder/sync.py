"""Incremental + full-pass listing sync.

Incremental (frequent): walk createdAt.desc pages and stop as soon as a full
page contains only already-known VINs at unchanged prices — everything below
is older and already ingested. Catches new listings cheaply.

Full pass (weekly): walk every page to catch price drops on existing listings
and to detect delistings — a VIN absent across two consecutive full passes is
marked presumed_sold.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from .sources import ListingRow, ListingSource


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class SyncStats:
    run_id: str
    mode: str
    pages_fetched: int = 0
    new_vins: int = 0
    updated_vins: int = 0
    price_changes: int = 0
    stopped_early: bool = False
    seen_vins: list[str] = field(default_factory=list)
    # VINs that are new or had a price change this run — the pipeline only needs
    # to (re)fetch stickers and (re)score these, not every seen VIN.
    new_or_changed: list[str] = field(default_factory=list)
    presumed_sold: list[str] = field(default_factory=list)


class SyncEngine:
    def __init__(
        self,
        conn: sqlite3.Connection,
        source: ListingSource,
        now: Callable[[], datetime] | None = None,
    ):
        self._conn = conn
        self._source = source
        self._now = now or (lambda: datetime.now(UTC))

    # -- persistence ---------------------------------------------------------

    def _upsert_row(self, row: ListingRow, model_key: str, ts: str) -> str:
        """Insert or update a listing. Returns 'new' | 'price_change' | 'unchanged'."""
        src = self._source.name
        prior = self._conn.execute(
            "SELECT price FROM listings WHERE source = ? AND vin = ?", (src, row.vin)
        ).fetchone()

        if prior is None:
            self._conn.execute(
                """INSERT INTO listings
                   (source, vin, price, miles, year, make, model, trim, color,
                    created_at, url, raw_json, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    src,
                    row.vin,
                    row.price,
                    row.miles,
                    row.year,
                    row.make,
                    row.model,
                    row.trim,
                    row.color,
                    row.created_at,
                    row.url,
                    _dumps(row.raw),
                    ts,
                    ts,
                ),
            )
            if row.price is not None:
                self._append_price(row.vin, row.price, ts)
            status = "new"
        else:
            price_changed = row.price is not None and prior["price"] != row.price
            self._conn.execute(
                """UPDATE listings SET price=?, miles=?, year=?, make=?, model=?,
                   trim=?, color=?, created_at=?, url=?, raw_json=?, last_seen=?
                   WHERE source=? AND vin=?""",
                (
                    row.price,
                    row.miles,
                    row.year,
                    row.make,
                    row.model,
                    row.trim,
                    row.color,
                    row.created_at,
                    row.url,
                    _dumps(row.raw),
                    ts,
                    src,
                    row.vin,
                ),
            )
            if price_changed:
                self._append_price(row.vin, row.price, ts)
                status = "price_change"
            else:
                status = "unchanged"

        self._upsert_vehicle(row, model_key, ts)
        return status

    def _append_price(self, vin: str, price: float, ts: str) -> None:
        self._conn.execute(
            "INSERT INTO price_history (vin, source, price, observed_at) VALUES (?,?,?,?)",
            (vin, self._source.name, price, ts),
        )

    def _upsert_vehicle(self, row: ListingRow, model_key: str, ts: str) -> None:
        # Best (lowest) current price across all sources for this VIN.
        best = self._conn.execute(
            "SELECT MIN(price) FROM listings WHERE vin = ? AND price IS NOT NULL", (row.vin,)
        ).fetchone()[0]
        exists = self._conn.execute("SELECT 1 FROM vehicles WHERE vin = ?", (row.vin,)).fetchone()
        if exists:
            # Seeing the VIN again resets delist bookkeeping.
            self._conn.execute(
                """UPDATE vehicles SET model_key=?, year=?, make=?, model=?, trim=?,
                   color=?, miles=?, price=?, status='active', missing_passes=0, last_seen=?
                   WHERE vin=?""",
                (
                    model_key,
                    row.year,
                    row.make,
                    row.model,
                    row.trim,
                    row.color,
                    row.miles,
                    best,
                    ts,
                    row.vin,
                ),
            )
        else:
            self._conn.execute(
                """INSERT INTO vehicles
                   (vin, model_key, year, make, model, trim, color, miles, price,
                    status, missing_passes, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?, 'active', 0, ?, ?)""",
                (
                    row.vin,
                    model_key,
                    row.year,
                    row.make,
                    row.model,
                    row.trim,
                    row.color,
                    row.miles,
                    best,
                    ts,
                    ts,
                ),
            )

    # -- delist detection (full pass only) -----------------------------------

    def _detect_delist(self, model_key: str, seen: set[str]) -> list[str]:
        active = self._conn.execute(
            "SELECT vin, missing_passes FROM vehicles WHERE model_key=? AND status='active'",
            (model_key,),
        ).fetchall()
        sold: list[str] = []
        for r in active:
            if r["vin"] in seen:
                continue  # already reset in _upsert_vehicle
            passes = r["missing_passes"] + 1
            status = "presumed_sold" if passes >= 2 else "active"
            self._conn.execute(
                "UPDATE vehicles SET missing_passes=?, status=? WHERE vin=?",
                (passes, status, r["vin"]),
            )
            if status == "presumed_sold":
                sold.append(r["vin"])
        return sold

    # -- driver --------------------------------------------------------------

    def sync(
        self,
        params: dict[str, str],
        model_key: str,
        mode: str = "incremental",
        run_id: str | None = None,
        max_pages: int = 50,
        sort: str = "createdAt.desc",
    ) -> SyncStats:
        run_id = run_id or uuid4().hex
        started = _iso(self._now())
        self._conn.execute(
            """INSERT INTO sync_runs (run_id, source, model_key, mode, started_at)
               VALUES (?,?,?,?,?)""",
            (run_id, self._source.name, model_key, mode, started),
        )
        stats = SyncStats(run_id=run_id, mode=mode)

        page = 1
        while page <= max_pages:
            result = self._source.search_page(params, page=page, sort=sort, run_id=run_id)
            stats.pages_fetched += 1
            if not result.rows:
                break

            page_all_known_unchanged = True
            for row in result.rows:
                if not row.vin:
                    continue
                status = self._upsert_row(row, model_key, _iso(self._now()))
                stats.seen_vins.append(row.vin)
                if status == "new":
                    stats.new_vins += 1
                    stats.new_or_changed.append(row.vin)
                    page_all_known_unchanged = False
                elif status == "price_change":
                    stats.price_changes += 1
                    stats.updated_vins += 1
                    stats.new_or_changed.append(row.vin)
                    page_all_known_unchanged = False

            if mode == "incremental" and page_all_known_unchanged:
                stats.stopped_early = True
                break
            if not result.has_next:
                break
            page += 1

        if mode == "full":
            stats.presumed_sold = self._detect_delist(model_key, set(stats.seen_vins))

        self._conn.execute(
            """UPDATE sync_runs SET finished_at=?, pages_fetched=?, new_vins=?,
               updated_vins=?, calls=? WHERE run_id=?""",
            (
                _iso(self._now()),
                stats.pages_fetched,
                stats.new_vins,
                stats.updated_vins,
                stats.pages_fetched,
                run_id,
            ),
        )
        self._conn.commit()
        return stats


def _dumps(raw: dict) -> str:
    import json

    return json.dumps(raw, separators=(",", ":"))
