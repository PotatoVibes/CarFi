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
from pathlib import Path

import httpx

from . import colors, db
from .ai import Attribute, HaikuExtractor
from .budget import BudgetExceeded, BudgetGuard
from .config import Settings
from .models import Profile, Provenance, Vehicle
from .notify import Notifier
from .query import build_listing_params
from .reconcile import Facts, reconcile
from .scorer import score_vehicle
from .sources import AutoDevSource, ListingSource
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
    conflicts: int = 0
    sticker_capped: bool = False
    ai_calls: int = 0  # new (non-cached) AI extraction calls this run
    ai_cost_usd: float = 0.0
    ai_budget_exhausted: bool = False
    notified: int = 0


class Pipeline:
    def __init__(
        self,
        conn: sqlite3.Connection,
        source: ListingSource,
        fetcher: StickerFetcher,
        current_year: int,
        now: Callable[[], datetime] | None = None,
        max_stickers_per_run: int = 50,
        extractor: HaikuExtractor | None = None,
        notifier: Notifier | None = None,
        notify_threshold: float = 85.0,
    ):
        self._conn = conn
        self._fetcher = fetcher
        self._engine = SyncEngine(conn, source, now=now)
        self._current_year = current_year
        self._now = now or (lambda: datetime.now(UTC))
        self._max_stickers = max_stickers_per_run
        self._extractor = extractor
        self._notifier = notifier
        self._notify_threshold = notify_threshold

    def run(
        self,
        profile: Profile,
        mode: str = "incremental",
        run_id: str | None = None,
    ) -> PipelineStats:
        params = build_listing_params(profile, self._current_year)
        sync_stats = self._engine.sync(params, profile.model_key, mode=mode, run_id=run_id)
        stats = PipelineStats(sync=sync_stats)

        attributes = _profile_attributes(profile) if self._extractor is not None else []

        for vin in dict.fromkeys(sync_stats.new_or_changed):  # de-dupe, keep order
            parsed, sticker_path = self._maybe_sticker(vin, sync_stats.run_id, stats)
            row = self._conn.execute(
                "SELECT year, miles, price, make, model, trim, color FROM vehicles WHERE vin=?",
                (vin,),
            ).fetchone()
            claimed_features = self._ai_features(
                vin, parsed, sticker_path, row, attributes, sync_stats.run_id, stats
            )
            vehicle = self._build_vehicle(vin, profile.model_key, parsed, row, claimed_features)
            result = score_vehicle(profile, vehicle)
            prior = self._conn.execute(
                "SELECT verdict, total FROM match_results WHERE vin=?", (vin,)
            ).fetchone()
            self._persist(vin, profile.model_key, result)
            self._log_conflicts(vin, profile.model_key, parsed, row)
            if self._maybe_notify(vin, profile, result, prior):
                stats.notified += 1
            stats.scored += 1
            stats.ranked += result.verdict == "ranked"
            stats.needs_review += result.verdict == "needs_review"
            stats.excluded += result.verdict == "excluded"
            stats.conflicts += self._conn.execute(
                "SELECT COUNT(*) FROM conflicts WHERE vin=?", (vin,)
            ).fetchone()[0]

        if stats.sticker_capped:
            log.warning(
                "sticker fetch cap (%d) reached this run — remaining VINs scored on "
                "listing data only and will fetch next run",
                self._max_stickers,
            )
        return stats

    # -- steps ---------------------------------------------------------------

    def _maybe_sticker(self, vin: str, run_id: str, stats: PipelineStats):
        """Fetch + parse the sticker, honoring the per-run network-fetch cap.

        Returns (parsed_or_None, pdf_path_or_None)."""
        would_network = not self._fetcher.is_cached(vin)
        if would_network and stats.stickers_fetched >= self._max_stickers:
            stats.sticker_capped = True
            return None, None
        result = self._fetcher.fetch(vin, run_id=run_id)
        if not result.from_cache:
            stats.stickers_fetched += 1
        if result.status == FOUND and result.path is not None:
            try:
                return parse_sticker(result.path), result.path
            except Exception as exc:  # a bad PDF must not crash the whole run
                log.warning("sticker parse failed for %s: %s", vin, exc)
        return None, None

    def _ai_features(
        self, vin, parsed, sticker_path, row, attributes, run_id, stats: PipelineStats
    ) -> dict[str, str]:
        """AI fallback (M5): claimed features from dealer text when no sticker,
        or a vision re-parse when the sticker parsed at low confidence. Every
        result is claimed provenance — it can never satisfy a hard requirement."""
        if self._extractor is None or not attributes:
            return {}
        try:
            if parsed is None:
                text = _claimed_text(row)
                if not text:
                    return {}
                ext = self._extractor.extract(text, attributes, run_id=run_id)
            elif not parsed.complete and sticker_path is not None:
                ext = self._extractor.extract_from_pdf(
                    Path(sticker_path).read_bytes(), attributes, run_id=run_id
                )
            else:
                return {}  # complete verified sticker — no AI needed
        except BudgetExceeded:
            stats.ai_budget_exhausted = True
            return {}
        except (httpx.HTTPError, OSError) as exc:
            log.warning("AI extraction failed for %s: %s", vin, exc)
            return {}
        if not ext.cached:
            stats.ai_calls += 1
        stats.ai_cost_usd += ext.cost_usd
        return ext.features

    def _maybe_notify(self, vin, profile, result, prior) -> bool:
        """Notify once when a car newly reaches ranked at/above the threshold.

        'Newly' = it wasn't already ranked-above-threshold on the prior run, so
        a stable qualifying car doesn't ping every sync."""
        if self._notifier is None or not self._notifier.enabled:
            return False
        qualifies = result.verdict == "ranked" and result.total >= self._notify_threshold
        if not qualifies:
            return False
        was = prior is not None and prior["verdict"] == "ranked" and (
            prior["total"] >= self._notify_threshold
        )
        if was:
            return False
        title = f"Car Finder: {profile.name} scored {result.total:.0f}"
        body = f"VIN {vin} — verdict {result.verdict}, total {result.total:.0f}/110."
        return self._notifier.notify_match(vin, title, body)

    def _build_vehicle(
        self, vin: str, model_key: str, parsed, row, claimed_features: dict[str, str]
    ) -> Vehicle:
        """Merge verified sticker data with claimed listing/AI data into a Vehicle."""
        miles = row["miles"] if row else None
        price = row["price"] if row else None
        listing_year = row["year"] if row else None
        claimed_text = _claimed_text(row)

        if parsed is not None:
            kw = vehicle_kwargs(parsed, model_key)
            kw["miles"] = miles
            kw["price"] = price
            if kw.get("year") is None:
                kw["year"] = listing_year
            # Keep the listing text as a claimed fallback alongside verified data.
            kw["claimed_text"] = claimed_text
            kw["claimed_features"] = claimed_features
            return Vehicle(**kw)

        # No sticker: listing data only (claimed provenance).
        return Vehicle(
            vin=vin,
            year=listing_year,
            miles=miles,
            price=price,
            claimed_text=claimed_text,
            color_text=(row["color"] if row else None),
            claimed_features=claimed_features,
        )

    def _log_conflicts(self, vin: str, model_key: str, parsed, row) -> None:
        """Detect + persist sticker-vs-listing disagreements.

        Only genuinely apples-to-apples fields are compared: model year and
        resolved color family. (Brand strings like sticker "CHEVY" vs listing
        "Chevrolet", and factory-vs-generic color names, would be spurious.)
        OEM build data would slot in here as a middle tier when available.
        """
        facts: list[Facts] = []
        if parsed is not None:
            paint = next((c for c in parsed.options if c in colors.paint_codes(model_key)), None)
            fam = colors.family_from_rpo(model_key, paint) or colors.family_from_text(
                parsed.exterior
            )
            facts.append(
                Facts(
                    source="sticker",
                    provenance=Provenance.verified,
                    year=parsed.year,
                    exterior=fam.value if fam else None,
                )
            )
        if row is not None:
            fam = colors.family_from_text(row["color"])
            facts.append(
                Facts(
                    source="listing",
                    provenance=Provenance.claimed,
                    year=row["year"],
                    exterior=fam.value if fam else None,
                )
            )

        result = reconcile(facts)
        rows = [
            (c.field, c.winner, json.dumps(c.values, separators=(",", ":")))
            for c in result.conflicts
        ]
        db.replace_conflicts(self._conn, vin, rows, self._now())

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


def build_pipeline(
    conn, settings: Settings, current_year: int, guard: BudgetGuard | None = None
) -> Pipeline:
    """Wire a live Pipeline from settings (Auto.dev + GM stickers + optional
    Haiku + optional webhook). Raises ValueError if the Auto.dev key is absent."""
    if not settings.autodev_api_key:
        raise ValueError("AUTODEV_API_KEY is required to sync")
    guard = guard or BudgetGuard.from_settings(conn, settings)
    source = AutoDevSource(settings.autodev_api_key, guard)
    fetcher = StickerFetcher(conn, guard, stickers_dir=settings.stickers_dir)
    extractor = (
        HaikuExtractor(conn, guard, settings.anthropic_api_key)
        if settings.anthropic_api_key
        else None
    )
    notifier = Notifier(settings.notify_webhook_url) if settings.notify_webhook_url else None
    return Pipeline(
        conn,
        source,
        fetcher,
        current_year,
        extractor=extractor,
        notifier=notifier,
        notify_threshold=settings.notify_threshold,
    )


def _claimed_text(row) -> str:
    """Build a claimed dealer-text blob from a vehicles row."""
    if row is None:
        return ""
    return " ".join(str(row[k]) for k in ("make", "model", "trim", "color") if row[k])


def _profile_attributes(profile: Profile) -> list[Attribute]:
    """The attribute list the AI extractor classifies — hard require/forbid
    constraints (by key) and highlights (by label, matching the scorer's key)."""
    attrs = [
        Attribute(key=c.key, label=c.label) for c in profile.hard if c.kind in ("require", "forbid")
    ]
    attrs += [Attribute(key=h.label, label=h.label) for h in profile.highlights]
    return attrs
