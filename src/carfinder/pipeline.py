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
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

from . import colors, db
from . import vin as vin_decode
from .ai import Attribute, HaikuExtractor
from .budget import BudgetExceeded, BudgetGuard
from .config import Settings
from .models import Profile, Provenance, Vehicle
from .notify import Notifier
from .query import build_listing_params
from .reconcile import Facts, reconcile
from .scorer import score_vehicle
from .sources import AutoDevSource, ListingSource
from .sticker_parser import PARSER_VERSION, ParsedSticker, parse_sticker, vehicle_kwargs
from .stickers import FOUND, StickerFetcher
from .sync import SyncEngine, SyncStats

log = logging.getLogger("carfinder.pipeline")


def _eta(start: float, done: int, total: int) -> str:
    """Progress suffix like ' · ~2m left', once enough samples exist."""
    if done < 3 or total <= done:
        return ""
    remain_s = (time.monotonic() - start) / done * (total - done)
    if remain_s < 5:
        return ""
    if remain_s < 90:
        return f" · ~{remain_s:.0f}s left"
    return f" · ~{remain_s / 60:.0f}m left"


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
        source: ListingSource | None,
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
        # rescore() needs no source; run() does (guarded).
        self._engine = SyncEngine(conn, source, now=now) if source is not None else None
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
        progress: Callable[[str], None] | None = None,
    ) -> PipelineStats:
        if self._engine is None:
            raise ValueError("run() needs a listing source; none was configured")
        params = build_listing_params(profile, self._current_year)
        if mode == "full":
            # Full passes walk deep, and Auto.dev's pagination window ends at
            # 2,000 results per query (observed live: page 101 refused). Walk
            # each model year separately — every year fits the window.
            sync_stats = self._sync_full_segmented(params, profile, run_id, progress)
        else:
            # Incremental stops at the first known page, far inside any
            # window — a single newest-first walk is correct and cheapest.
            sync_stats = self._engine.sync(
                params, profile.model_key, mode=mode, run_id=run_id, progress=progress
            )
        stats = PipelineStats(sync=sync_stats)
        attributes = _profile_attributes(profile) if self._extractor is not None else []

        todo = list(dict.fromkeys(sync_stats.new_or_changed))  # de-dupe, keep order
        started = time.monotonic()
        for i, vin in enumerate(todo, 1):
            if progress is not None:
                progress(f"scoring {i}/{len(todo)} — {vin}{_eta(started, i - 1, len(todo))}")
            self._process_vin(vin, profile, attributes, sync_stats.run_id, stats)

        if stats.sticker_capped:
            log.warning(
                "sticker fetch cap (%d) reached this run — remaining VINs scored on "
                "listing data only and will fetch next run",
                self._max_stickers,
            )
        return stats

    def rescore(
        self,
        profile: Profile,
        run_id: str = "rescore",
        progress: Callable[[str], None] | None = None,
    ) -> PipelineStats:
        """Re-score every known vehicle for a profile without syncing.

        For cars processed before, sticker and AI results are cache hits, so
        this makes ~zero network calls — the way to apply a profile edit to the
        whole existing set. (A genuinely new AI attribute list can miss cache
        and spend, but that stays gated by BudgetGuard.)"""
        stats = PipelineStats(sync=SyncStats(run_id=run_id, mode="rescore"))
        attributes = _profile_attributes(profile) if self._extractor is not None else []
        # Best-first: active cars before sold, higher prior score first. The
        # per-run sticker-fetch cap then spends its budget on the most
        # promising cars instead of whatever happened to sync first.
        vins = [
            r["vin"]
            for r in self._conn.execute(
                """SELECT v.vin FROM vehicles v
                   LEFT JOIN match_results m ON m.vin = v.vin
                   WHERE v.model_key = ?
                   ORDER BY (v.status = 'active') DESC, m.total DESC NULLS LAST""",
                (profile.model_key,),
            ).fetchall()
        ]
        started = time.monotonic()
        for i, vin in enumerate(vins, 1):
            if progress is not None:
                progress(f"re-scoring {i}/{len(vins)} — {vin}{_eta(started, i - 1, len(vins))}")
            self._process_vin(vin, profile, attributes, run_id, stats)
        return stats

    def _sync_full_segmented(self, params, profile, run_id, progress) -> SyncStats:
        """One full walk per model year, merged; delist runs once on the union.

        Per-segment delisting would wrongly count every other year's cars as
        missing, and a truncated walk can't distinguish 'sold' from 'beyond
        the provider's window' — so delist is skipped entirely on truncation.
        """
        years = _segment_years(params.get("vehicle.year"))
        if not years:  # no year range to slice on — single walk as before
            return self._engine.sync(
                params, profile.model_key, mode="full", run_id=run_id, progress=progress
            )

        base = run_id or uuid4().hex
        merged = SyncStats(run_id=base, mode="full")
        for year in years:
            seg = self._engine.sync(
                {**params, "vehicle.year": f"{year}-{year}"},
                profile.model_key,
                mode="full",
                run_id=f"{base}-y{year}",
                progress=(lambda msg, y=year: progress(f"{y}: {msg}")) if progress else None,
                delist=False,
            )
            merged.pages_fetched += seg.pages_fetched
            merged.new_vins += seg.new_vins
            merged.updated_vins += seg.updated_vins
            merged.price_changes += seg.price_changes
            merged.new_or_changed.extend(seg.new_or_changed)
            merged.seen_vins.extend(seg.seen_vins)
            merged.truncated |= seg.truncated

        if merged.truncated:
            log.warning(
                "%s full pass truncated — skipping delist detection (absence is "
                "not evidence of a sale when the walk was incomplete)",
                profile.model_key,
            )
        else:
            merged.presumed_sold = self._engine.detect_delist(
                profile.model_key, set(merged.seen_vins)
            )
        return merged

    def _process_vin(self, vin, profile, attributes, run_id, stats: PipelineStats) -> None:
        row = self._conn.execute(
            "SELECT year, miles, price, make, model, trim, color FROM vehicles WHERE vin=?",
            (vin,),
        ).fetchone()

        # Fast path: if the VIN alone verified-fails a hard requirement (wrong
        # trim/body/model), the car is excluded no matter what the sticker
        # says — skip the sticker fetch and AI spend entirely. Only taken when
        # the sticker isn't already cached (a cached one is free and gives the
        # richer breakdown).
        if not self._fetcher.is_cached(vin):
            pre = self._build_vehicle(vin, profile.model_key, None, row, {})
            pre_result = score_vehicle(profile, pre)
            if any(
                r.status == "fail" and r.provenance == Provenance.verified for r in pre_result.hard
            ):
                self._finish_vin(vin, profile, pre_result, None, row, stats)
                return

        parsed, sticker_path = self._maybe_sticker(vin, run_id, stats)
        claimed_features = self._ai_features(
            vin, parsed, sticker_path, row, attributes, run_id, stats
        )
        vehicle = self._build_vehicle(vin, profile.model_key, parsed, row, claimed_features)
        result = score_vehicle(profile, vehicle)
        self._finish_vin(vin, profile, result, parsed, row, stats)

    def _finish_vin(self, vin, profile, result, parsed, row, stats: PipelineStats) -> None:
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

    # -- steps ---------------------------------------------------------------

    def _maybe_sticker(self, vin: str, run_id: str, stats: PipelineStats):
        """Fetch + parse the sticker, honoring the per-run network-fetch cap.

        Parses are cached in the DB (PDFs are immutable per VIN), so a car is
        pdfplumber-parsed once ever — rescores hit the cache. Returns
        (parsed_or_None, pdf_path_or_None)."""
        would_network = not self._fetcher.is_cached(vin)
        if would_network and stats.stickers_fetched >= self._max_stickers:
            stats.sticker_capped = True
            return None, None
        result = self._fetcher.fetch(vin, run_id=run_id)
        if not result.from_cache:
            stats.stickers_fetched += 1
        if result.status == FOUND and result.path is not None:
            cached = db.get_parsed_sticker(self._conn, vin, PARSER_VERSION)
            if cached is not None:
                return ParsedSticker.from_json(cached), result.path
            try:
                parsed = parse_sticker(result.path)
            except Exception as exc:  # a bad PDF must not crash the whole run
                log.warning("sticker parse failed for %s: %s", vin, exc)
            else:
                db.put_parsed_sticker(
                    self._conn, vin, PARSER_VERSION, parsed.to_json(), self._now()
                )
                return parsed, result.path
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
        was = (
            prior is not None
            and prior["verdict"] == "ranked"
            and (prior["total"] >= self._notify_threshold)
        )
        if was:
            return False
        title = f"Car Finder: {profile.name} scored {result.total:.0f}"
        body = f"VIN {vin} — verdict {result.verdict}, total {result.total:.0f}/110."
        return self._notifier.notify_match(vin, title, body)

    def _build_vehicle(
        self, vin: str, model_key: str, parsed, row, claimed_features: dict[str, str]
    ) -> Vehicle:
        """Merge verified sticker + VIN data with claimed listing/AI data."""
        miles = row["miles"] if row else None
        price = row["price"] if row else None
        listing_year = row["year"] if row else None
        claimed_text = _claimed_text(row)
        facts = vin_decode.decode(vin)

        if parsed is not None:
            kw = vehicle_kwargs(parsed, model_key)
            kw["miles"] = miles
            kw["price"] = price
            if kw.get("year") is None:
                kw["year"] = listing_year or vin_decode.model_year(vin)
            # Keep the listing text as a claimed fallback alongside verified data.
            kw["claimed_text"] = claimed_text
            kw["claimed_features"] = claimed_features
            kw["vin_facts"] = facts
            return Vehicle(**kw)

        # No sticker: VIN facts (verified) + listing data (claimed).
        return Vehicle(
            vin=vin,
            year=listing_year or vin_decode.model_year(vin),
            miles=miles,
            price=price,
            vin_facts=facts,
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


def _build(conn, settings: Settings, current_year: int, with_source: bool) -> Pipeline:
    guard = BudgetGuard.from_settings(conn, settings)
    source = AutoDevSource(settings.autodev_api_key, guard) if with_source else None
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


def build_pipeline(conn, settings: Settings, current_year: int) -> Pipeline:
    """Wire a live sync Pipeline from settings. Requires the Auto.dev key."""
    if not settings.autodev_api_key:
        raise ValueError("AUTODEV_API_KEY is required to sync")
    return _build(conn, settings, current_year, with_source=True)


def build_rescore_pipeline(conn, settings: Settings, current_year: int) -> Pipeline:
    """Wire a Pipeline for rescore only (no source, no Auto.dev key needed)."""
    return _build(conn, settings, current_year, with_source=False)


def _segment_years(year_param: str | None) -> list[int]:
    """'2021-2027' -> [2021..2027]; a single year or missing param -> []
    (no segmentation possible/needed)."""
    if not year_param:
        return []
    m = re.fullmatch(r"(\d{4})-(\d{4})", year_param.strip())
    if not m:
        return []
    lo, hi = int(m.group(1)), int(m.group(2))
    if hi < lo:
        return []
    return list(range(lo, hi + 1))


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
