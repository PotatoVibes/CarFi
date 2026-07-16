"""End-to-end pipeline: sync -> new VINs -> sticker fetch -> parse -> score.

Only VINs that are new or price-changed this run are sticker-fetched and
(re)scored — the incremental sync keeps that set small. A per-run cap on *new*
network sticker fetches keeps a first-run backfill from blowing the GM budget
in one shot; cached VINs are always processed (free) and the run makes progress
across invocations. Scores land in match_results for the UI.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from .models import Profile, Vehicle
from .query import build_listing_params
from .scorer import score_vehicle
from .sources import ListingSource
from .sticker_parser import parse_sticker, vehicle_kwargs
from .stickers import FOUND, StickerFetcher
from .sync import SyncEngine, SyncStats

log = logging.getLogger("carfinder.pipeline")


@dataclass
class PipelineStats:
    sync: SyncStats
    scored: int = 0
    stickers_fetched: int = 0  # new network fetches this run
    ranked: int = 0
    needs_review: int = 0
    excluded: int = 0
    sticker_capped: bool = False


class Pipeline:
    def __init__(
        self,
        conn: sqlite3.Connection,
        source: ListingSource,
        fetcher: StickerFetcher,
        current_year: int,
        now: Callable[[], datetime] | None = None,
        max_stickers_per_run: int = 50,
    ):
        self._conn = conn
        self._fetcher = fetcher
        self._engine = SyncEngine(conn, source, now=now)
        self._current_year = current_year
        self._now = now or (lambda: datetime.now(UTC))
        self._max_stickers = max_stickers_per_run

    def run(
        self,
        profile: Profile,
        mode: str = "incremental",
        run_id: str | None = None,
    ) -> PipelineStats:
        params = build_listing_params(profile, self._current_year)
        sync_stats = self._engine.sync(params, profile.model_key, mode=mode, run_id=run_id)
        stats = PipelineStats(sync=sync_stats)

        for vin in dict.fromkeys(sync_stats.new_or_changed):  # de-dupe, keep order
            parsed = self._maybe_sticker(vin, sync_stats.run_id, stats)
            vehicle = self._build_vehicle(vin, profile.model_key, parsed)
            result = score_vehicle(profile, vehicle)
            self._persist(vin, profile.model_key, result)
            stats.scored += 1
            stats.ranked += result.verdict == "ranked"
            stats.needs_review += result.verdict == "needs_review"
            stats.excluded += result.verdict == "excluded"

        if stats.sticker_capped:
            log.warning(
                "sticker fetch cap (%d) reached this run — remaining VINs scored on "
                "listing data only and will fetch next run",
                self._max_stickers,
            )
        return stats

    # -- steps ---------------------------------------------------------------

    def _maybe_sticker(self, vin: str, run_id: str, stats: PipelineStats):
        """Fetch + parse the sticker, honoring the per-run network-fetch cap."""
        would_network = not self._fetcher.is_cached(vin)
        if would_network and stats.stickers_fetched >= self._max_stickers:
            stats.sticker_capped = True
            return None
        result = self._fetcher.fetch(vin, run_id=run_id)
        if not result.from_cache:
            stats.stickers_fetched += 1
        if result.status == FOUND and result.path is not None:
            try:
                return parse_sticker(result.path)
            except Exception as exc:  # a bad PDF must not crash the whole run
                log.warning("sticker parse failed for %s: %s", vin, exc)
        return None

    def _build_vehicle(self, vin: str, model_key: str, parsed) -> Vehicle:
        """Merge verified sticker data with claimed listing data into a Vehicle."""
        row = self._conn.execute(
            "SELECT year, miles, price, make, model, trim, color FROM vehicles WHERE vin=?",
            (vin,),
        ).fetchone()
        miles = row["miles"] if row else None
        price = row["price"] if row else None
        listing_year = row["year"] if row else None
        claimed_text = " ".join(
            str(row[k]) for k in ("make", "model", "trim", "color") if row and row[k]
        )

        if parsed is not None:
            kw = vehicle_kwargs(parsed, model_key)
            kw["miles"] = miles
            kw["price"] = price
            if kw.get("year") is None:
                kw["year"] = listing_year
            # Keep the listing text as a claimed fallback alongside verified data.
            kw["claimed_text"] = claimed_text
            return Vehicle(**kw)

        # No sticker: listing data only (claimed provenance).
        return Vehicle(
            vin=vin,
            year=listing_year,
            miles=miles,
            price=price,
            claimed_text=claimed_text,
            color_text=(row["color"] if row else None),
        )

    def _persist(self, vin: str, model_key: str, result) -> None:
        breakdown = {
            "hard": [r.model_dump(mode="json") for r in result.hard],
            "soft": [r.model_dump(mode="json") for r in result.soft],
            "highlights": [r.model_dump(mode="json") for r in result.highlights],
        }
        self._conn.execute(
            """INSERT INTO match_results
               (vin, model_key, verdict, soft_score, highlight_bonus, total,
                breakdown_json, scored_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(vin) DO UPDATE SET
                 model_key=excluded.model_key, verdict=excluded.verdict,
                 soft_score=excluded.soft_score, highlight_bonus=excluded.highlight_bonus,
                 total=excluded.total, breakdown_json=excluded.breakdown_json,
                 scored_at=excluded.scored_at""",
            (
                vin,
                model_key,
                result.verdict,
                result.soft_score,
                result.highlight_bonus,
                result.total,
                json.dumps(breakdown, separators=(",", ":")),
                self._now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        )
        self._conn.commit()
