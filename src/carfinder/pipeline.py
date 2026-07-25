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
from .buildsheet import FOUND as SHEET_FOUND
from .buildsheet import BuildSheet, BuildSheetFetcher
from .buildsheet import vehicle_kwargs as sheet_vehicle_kwargs
from .config import Settings
from .models import Profile, Provenance, Vehicle
from .notify import Notifier
from .partsdeal import PartsDealFetcher
from .query import build_listing_params, build_marketcheck_params
from .reconcile import Facts, reconcile
from .scorer import score_vehicle
from .sources import AutoDevSource, ListingSource, MarketcheckSource, scrub_api_key
from .sticker_parser import PARSER_VERSION, ParsedSticker, parse_sticker, vehicle_kwargs
from .stickers import FOUND, StickerFetcher
from .sync import MAX_PAGES_DEFAULT, SyncEngine, SyncStats

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
    buildsheets_fetched: int = 0  # new network build-sheet fetches this run
    ai_calls: int = 0  # new (non-cached) AI extraction calls this run
    ai_cost_usd: float = 0.0
    ai_budget_exhausted: bool = False
    notified: int = 0
    # Marketcheck secondary-source walk (full passes only). None when the
    # source isn't configured or the run was incremental.
    mc_sync: SyncStats | None = None
    mc_failed: bool = False  # a Marketcheck segment died — delist was skipped


class Pipeline:
    def __init__(
        self,
        conn: sqlite3.Connection,
        source: ListingSource | None,
        fetcher: StickerFetcher,
        current_year: int,
        now: Callable[[], datetime] | None = None,
        max_stickers_per_run: int = 100,
        extractor: HaikuExtractor | None = None,
        notifier: Notifier | None = None,
        notify_threshold: float = 85.0,
        buildsheet_fetcher: BuildSheetFetcher | None = None,
        max_buildsheets_per_run: int = 100,
        secondary_source: ListingSource | None = None,
        partsdeal_fetchers: dict[str, PartsDealFetcher] | None = None,
    ):
        self._conn = conn
        self._fetcher = fetcher
        self._buildsheet = buildsheet_fetcher
        self._max_buildsheets = max_buildsheets_per_run
        # make -> PartsDealFetcher for non-GM catalog decodes (CLAIMED-only
        # evidence — see partsdeal.py for the disqualification fixtures).
        self._partsdeal = partsdeal_fetchers or {}
        # rescore() needs no source; run() does (guarded).
        self._source = source
        self._engine = SyncEngine(conn, source, now=now) if source is not None else None
        # Secondary source (Marketcheck): full passes only. Walked BEFORE the
        # primary so the primary's values win the vehicles-row last-writer-
        # wins on VINs both feeds carry.
        self._mc_engine = (
            SyncEngine(conn, secondary_source, now=now) if secondary_source is not None else None
        )
        self._current_year = current_year
        self._now = now or (lambda: datetime.now(UTC))
        self._max_stickers = max_stickers_per_run
        self._extractor = extractor
        self._notifier = notifier
        self._notify_threshold = notify_threshold
        # When True, per-VIN writes skip their own commit; the scoring loop
        # commits every _COMMIT_EVERY cars instead. Cuts a full rescore's
        # fsync count from ~2/car to ~1/25 cars — the dominant cost at rest.
        self._defer_commits = False

    _COMMIT_EVERY = 25

    def _score_batch(
        self,
        vins: list[str],
        profile: Profile,
        attributes,
        run_id: str,
        stats: PipelineStats,
        progress: Callable[[str], None] | None,
        verb: str = "scoring",
    ) -> None:
        """Score a list of VINs with batched commits (see _defer_commits)."""
        started = time.monotonic()
        self._defer_commits = True
        try:
            for i, vin in enumerate(vins, 1):
                if progress is not None:
                    progress(f"{verb} {i}/{len(vins)} — {vin}{_eta(started, i - 1, len(vins))}")
                self._process_vin(vin, profile, attributes, run_id, stats)
                if i % self._COMMIT_EVERY == 0:
                    self._conn.commit()
        finally:
            self._defer_commits = False
            self._conn.commit()

    def run(
        self,
        profile: Profile,
        mode: str = "incremental",
        run_id: str | None = None,
        progress: Callable[[str], None] | None = None,
        max_pages: int | None = None,
    ) -> PipelineStats:
        if self._engine is None:
            raise ValueError("run() needs a listing source; none was configured")
        # An explicit cap is a budget-tight TEST pull: results get flagged
        # truncated by the engine, which also suppresses delist evidence.
        pages = max_pages if max_pages is not None else MAX_PAGES_DEFAULT
        params = build_listing_params(profile, self._current_year)
        mc_stats: SyncStats | None = None
        mc_failed = False
        if mode == "full":
            # Marketcheck (secondary) walks first so the primary's values win
            # the vehicles-row last-writer-wins; a car both feeds carry ends up
            # displaying Auto.dev's numbers.
            mc_stats, mc_failed = self._sync_marketcheck_full(profile, run_id, progress)
            # Full passes walk deep, and Auto.dev's pagination window ends at
            # 2,000 results per query (observed live: page 101 refused). Walk
            # each model year separately — every year fits the window. Delist
            # runs inside, on the union of everything EITHER feed saw.
            sync_stats = self._sync_full_segmented(
                params,
                profile,
                run_id,
                progress,
                extra_seen=set(mc_stats.seen_vins) if mc_stats is not None else set(),
                extra_truncated=mc_failed or bool(mc_stats is not None and mc_stats.truncated),
            )
        else:
            # Incremental stops at the first known page, far inside any
            # window — a single newest-first walk is correct and cheapest.
            # Marketcheck gets the same treatment: dos_active-asc is a stable
            # newest-first order, so the known-page stop is sound there too.
            mc_stats, mc_failed = self._sync_marketcheck_incremental(
                profile, run_id, progress, max_pages=pages
            )
            sync_stats = self._engine.sync(
                params,
                profile.model_key,
                mode=mode,
                run_id=run_id,
                progress=progress,
                max_pages=pages,
            )
        stats = PipelineStats(sync=sync_stats, mc_sync=mc_stats, mc_failed=mc_failed)
        attributes = _profile_attributes(profile) if self._extractor is not None else []

        todo = sync_stats.new_or_changed + (mc_stats.new_or_changed if mc_stats else [])
        todo = list(dict.fromkeys(todo))  # de-dupe across sources, keep order
        self._score_batch(todo, profile, attributes, sync_stats.run_id, stats, progress)

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
        self._score_batch(vins, profile, attributes, run_id, stats, progress, verb="re-scoring")
        return stats

    def refresh_vin(
        self,
        vin: str,
        profile: Profile,
        progress: Callable[[str], None] | None = None,
    ) -> str:
        """Refresh ONE car: refetch its listing (a single Auto.dev call),
        re-verify from caches (sticker/build sheet/AI), and rescore it.

        A 404 from the per-VIN endpoint means Auto.dev no longer has an active
        listing — the car is marked presumed_sold (reversible: any later sync
        that sees the VIN flips it back to active). Returns a summary line.
        """
        if self._engine is None or self._source is None:
            raise ValueError("refresh_vin() needs a listing source; none was configured")
        vin = vin.upper()
        run_id = f"refresh-{uuid4().hex[:8]}"
        if progress is not None:
            progress(f"fetching listing for {vin}")
        try:
            listing = self._source.get_listing(vin, run_id=run_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            listing = None

        if listing is not None and listing.vin == vin:
            change = self._engine.upsert(listing, profile.model_key)
            fetched = {"new": "listing refetched", "price_change": "price changed"}.get(
                change, "listing unchanged"
            )
        else:
            self._conn.execute("UPDATE vehicles SET status='presumed_sold' WHERE vin=?", (vin,))
            self._conn.commit()
            fetched = "no active listing — marked presumed sold"

        verdict = self._score_one(vin, profile, run_id, progress)
        return f"{vin}: {fetched} · {verdict}"

    def score_vin(
        self,
        vin: str,
        profile: Profile,
        progress: Callable[[str], None] | None = None,
    ) -> str:
        """Verify + score ONE car from its VIN alone (manually tracked — no
        listing). VIN facts, window sticker, and build sheet drive the verdict
        exactly as for synced cars; needs no Auto.dev key. Returns a summary."""
        vin = vin.upper()
        run_id = f"addvin-{uuid4().hex[:8]}"
        verdict = self._score_one(vin, profile, run_id, progress)
        return f"{vin}: verified & scored · {verdict}"

    def _score_one(self, vin, profile, run_id, progress) -> str:
        """Run the verification chain + scorer for one VIN; returns a short
        'verdict at total' summary of the persisted result."""
        if progress is not None:
            progress(f"verifying & scoring {vin}")
        stats = PipelineStats(sync=SyncStats(run_id=run_id, mode="single"))
        attributes = _profile_attributes(profile) if self._extractor is not None else []
        self._process_vin(vin, profile, attributes, run_id, stats)
        result = self._conn.execute(
            "SELECT verdict, total FROM match_results WHERE vin=?", (vin,)
        ).fetchone()
        return (
            f"{result['verdict'].replace('_', ' ')} at {result['total']:.0f}"
            if result
            else "scored"
        )

    def _sync_full_segmented(
        self,
        params,
        profile,
        run_id,
        progress,
        extra_seen: set[str] | None = None,
        extra_truncated: bool = False,
    ) -> SyncStats:
        """One full walk per model year, merged; delist runs once on the union.

        Per-segment delisting would wrongly count every other year's cars as
        missing, and a truncated walk can't distinguish 'sold' from 'beyond
        the provider's window' — so delist is skipped entirely on truncation.
        `extra_seen`/`extra_truncated` fold in a secondary source's walk: a car
        only Marketcheck still lists is not missing, and an incomplete
        Marketcheck walk poisons absence-evidence exactly like a truncated
        primary walk does.
        """
        extra_seen = extra_seen or set()
        years = _segment_years(params.get("vehicle.year"))
        if not years:  # no year range to slice on — single walk as before
            merged = self._engine.sync(
                params,
                profile.model_key,
                mode="full",
                run_id=run_id,
                progress=progress,
                delist=False,
            )
        else:
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

        if merged.truncated or extra_truncated:
            log.warning(
                "%s full pass truncated — skipping delist detection (absence is "
                "not evidence of a sale when the walk was incomplete)",
                profile.model_key,
            )
        else:
            merged.presumed_sold = self._engine.detect_delist(
                profile.model_key, set(merged.seen_vins) | extra_seen
            )
        return merged

    def _sync_marketcheck_full(self, profile, run_id, progress) -> tuple[SyncStats | None, bool]:
        """Walk Marketcheck for a full pass: one segment per model year, plus
        a no-price sweep.

        Year segmentation keeps every query under the free tier's 500-row
        window (single `year` values — the ranged `year_range` param stays in
        the incremental path where walks are shallow). The sweep re-runs the
        query with has_price=false and no price bound: `price_range` silently
        excludes "call for price" listings (documented), and an unpriced car
        can still be a unicorn — the scorer treats unknown price neutrally.

        Segments are isolated: a dead one (HTTP failure past retries) is
        logged and skipped so the secondary source can never sink the run; any
        failure flags `failed` so the caller skips delisting. Returns
        (stats-or-None, failed).
        """
        if self._mc_engine is None:
            return None, False
        try:
            params = build_marketcheck_params(profile, self._current_year)
        except ValueError:
            return None, False

        base = f"{run_id or uuid4().hex}-mc"
        merged = SyncStats(run_id=base, mode="full")
        failed = False

        def walk(seg_params: dict, seg_run_id: str, label: str) -> None:
            nonlocal failed
            try:
                seg = self._mc_engine.sync(
                    seg_params,
                    profile.model_key,
                    mode="full",
                    run_id=seg_run_id,
                    progress=(lambda msg, pre=label: progress(f"{pre}: {msg}"))
                    if progress
                    else None,
                    delist=False,
                )
            except BudgetExceeded as exc:
                log.warning("marketcheck budget exhausted — walk abandoned: %s", exc)
                failed = True
                raise
            except httpx.HTTPError as exc:
                # Scrub: Marketcheck carries the api key as a query param, so
                # httpx error strings can embed it in the URL.
                log.warning("%s segment failed: %s", label, scrub_api_key(str(exc)))
                failed = True
                return
            merged.pages_fetched += seg.pages_fetched
            merged.new_vins += seg.new_vins
            merged.updated_vins += seg.updated_vins
            merged.price_changes += seg.price_changes
            merged.new_or_changed.extend(seg.new_or_changed)
            merged.seen_vins.extend(seg.seen_vins)
            merged.truncated |= seg.truncated

        years = _segment_years(params.get("year_range"))
        try:
            for year in years or [None]:
                if year is None:
                    seg_params, seg_id, label = params, base, "marketcheck"
                else:
                    # Single-year `year` param per segment; drop the range key.
                    seg_params = {k: v for k, v in params.items() if k != "year_range"}
                    seg_params["year"] = str(year)
                    seg_id, label = f"{base}-y{year}", f"marketcheck {year}"
                walk(seg_params, seg_id, label)

            # No-price sweep: same filter minus the price bound. Only worth a
            # call when a price bound exists (otherwise the walk above already
            # covered unpriced cars).
            if "price_range" in params:
                sweep = {k: v for k, v in params.items() if k != "price_range"}
                sweep["has_price"] = "false"
                walk(sweep, f"{base}-noprice", "marketcheck no-price")
        except BudgetExceeded:
            pass  # already logged; keep whatever was ingested before the cap

        if not failed and not merged.seen_vins:
            log.warning(
                "marketcheck returned ZERO listings for %s — check the profile's "
                "query.marketcheck_model against their model taxonomy",
                profile.model_key,
            )
        return merged, failed

    def _sync_marketcheck_incremental(
        self, profile, run_id, progress, max_pages: int = MAX_PAGES_DEFAULT
    ) -> tuple[SyncStats | None, bool]:
        """Cheap Marketcheck incremental: ONE newest-first walk per model
        (dos_active asc — stable order) with the known-page early stop, so a
        quiet market costs a single call. Ranged `year_range` is fine here —
        the walk stops far inside the 500-row window. Coverage duties (year
        segmentation, no-price sweep, delist evidence) stay with the weekly
        full pass. Failures are logged and flagged, never raised.
        """
        if self._mc_engine is None:
            return None, False
        try:
            params = build_marketcheck_params(profile, self._current_year)
        except ValueError:
            return None, False
        try:
            stats = self._mc_engine.sync(
                params,
                profile.model_key,
                mode="incremental",
                run_id=f"{run_id or uuid4().hex}-mc",
                progress=(lambda msg: progress(f"marketcheck: {msg}")) if progress else None,
                delist=False,
                max_pages=max_pages,
            )
        except (BudgetExceeded, httpx.HTTPError) as exc:
            log.warning("marketcheck incremental failed: %s", scrub_api_key(str(exc)))
            return None, True
        return stats, False

    def _process_vin(self, vin, profile, attributes, run_id, stats: PipelineStats) -> None:
        row = self._conn.execute(
            "SELECT year, miles, price, make, model, trim, color FROM vehicles WHERE vin=?",
            (vin,),
        ).fetchone()
        manual_text = self._manual_page_text(vin)

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

        # Evidence routing is make-aware: GM VINs get the window sticker ->
        # gmpartsgiant chain (per-VIN factory data, VERIFIED). BMW/Toyota
        # VINs get their partsdeal decoder — a parts-CATALOG lookup that
        # collapses trims, proven wrong on real cars (see partsdeal.py), so
        # it joins the CLAIMED text only. Calling a GM endpoint with a
        # foreign VIN would waste GM budget AND poison the cache with a
        # false definitive not_found.
        make = vin_decode.wmi_make(vin)
        parsed, sticker_path = (
            self._maybe_sticker(vin, run_id, stats) if make == "gm" else (None, None)
        )
        sheet = None
        catalog_text = ""
        if parsed is None:
            if make == "gm":
                # Build-sheet decoder covers pre-2022 cars and sticker
                # not_founds. Full options list -> AI is skipped when found.
                sheet = self._maybe_buildsheet(vin, run_id, stats)
            elif make in self._partsdeal:
                catalog = self._maybe_partsdeal(vin, make, run_id, stats)
                catalog_text = catalog.text if catalog is not None else ""
        claimed_features = (
            {}
            if sheet is not None
            else self._ai_features(
                vin, parsed, sticker_path, row, attributes, run_id, stats, manual_text=manual_text
            )
        )
        vehicle = self._build_vehicle(
            vin,
            profile.model_key,
            parsed,
            row,
            claimed_features,
            sheet=sheet,
            manual_text=manual_text,
            catalog_text=catalog_text,
        )
        result = score_vehicle(profile, vehicle)
        self._finish_vin(vin, profile, result, parsed, row, stats)

    def _manual_page_text(self, vin: str) -> str:
        """Text of a hand-pasted listing page (manual source), if one exists —
        claimed evidence, same trust level as dealer free-text."""
        row = self._conn.execute(
            "SELECT raw_json FROM listings WHERE source='manual' AND vin=?", (vin,)
        ).fetchone()
        if row is None:
            return ""
        try:
            return str(json.loads(row["raw_json"]).get("page_text") or "")
        except ValueError:
            return ""

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

    def _maybe_buildsheet(self, vin: str, run_id: str, stats: PipelineStats) -> BuildSheet | None:
        """Build-sheet fallback, honoring a per-run network-fetch cap."""
        if self._buildsheet is None:
            return None
        would_network = not self._buildsheet.is_cached(vin)
        if would_network and stats.buildsheets_fetched >= self._max_buildsheets:
            return None
        try:
            result = self._buildsheet.fetch(vin, run_id=run_id)
        except BudgetExceeded:
            return None
        if not result.from_cache:
            stats.buildsheets_fetched += 1
        return result.sheet if result.status == SHEET_FOUND else None

    def _maybe_partsdeal(
        self, vin: str, make: str, run_id: str, stats: PipelineStats
    ) -> BuildSheet | None:
        """Non-GM identity decode (partsdeal siblings), sharing the per-run
        build-sheet network cap — same fetch class, same cache table."""
        fetcher = self._partsdeal[make]
        would_network = not fetcher.is_cached(vin)
        if would_network and stats.buildsheets_fetched >= self._max_buildsheets:
            return None
        try:
            result = fetcher.fetch(vin, run_id=run_id)
        except BudgetExceeded:
            return None
        if not result.from_cache:
            stats.buildsheets_fetched += 1
        return result.sheet if result.status == SHEET_FOUND else None

    def _ai_features(
        self,
        vin,
        parsed,
        sticker_path,
        row,
        attributes,
        run_id,
        stats: PipelineStats,
        manual_text: str = "",
    ) -> dict[str, str]:
        """AI fallback (M5): claimed features from dealer text (and any pasted
        listing page) when no sticker, or a vision re-parse when the sticker
        parsed at low confidence. Every result is claimed provenance — it can
        never satisfy a hard requirement."""
        if self._extractor is None or not attributes:
            return {}
        try:
            if parsed is None:
                text = " ".join(t for t in (_claimed_text(row), manual_text) if t)
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
        dismissed = self._conn.execute(
            "SELECT dismissed_at FROM vehicles WHERE vin=?", (vin,)
        ).fetchone()
        if dismissed and dismissed["dismissed_at"]:
            return False  # user said "not interested" — don't ping about it
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
        self,
        vin: str,
        model_key: str,
        parsed,
        row,
        claimed_features: dict[str, str],
        sheet: BuildSheet | None = None,
        manual_text: str = "",
        catalog_text: str = "",
    ) -> Vehicle:
        """Merge verified sticker/build-sheet + VIN data with claimed data.

        ``catalog_text`` (partsdeal decode) is CLAIMED evidence — the catalog
        collapses trims (proven on real 2023 Supras), so it can never verify.
        """
        miles = row["miles"] if row else None
        price = row["price"] if row else None
        listing_year = row["year"] if row else None
        claimed_text = " ".join(t for t in (_claimed_text(row), manual_text, catalog_text) if t)
        facts = vin_decode.decode(vin)

        if parsed is None and sheet is not None:
            kw = sheet_vehicle_kwargs(sheet)
            kw["miles"] = miles
            kw["price"] = price
            if kw.get("year") is None:
                kw["year"] = listing_year or vin_decode.model_year(vin)
            kw["claimed_text"] = claimed_text
            kw["claimed_features"] = claimed_features
            # The FE suspension code is as definitive as a VIN char — it joins
            # the facts so Z51/MagneRide resolve on build-sheet-only cars.
            kw["vin_facts"] = facts | c8_suspension_facts(model_key, kw["verified_rpo"])
            self._fallback_listing_color(kw, model_key, row)
            return Vehicle(**kw)

        if parsed is not None:
            kw = vehicle_kwargs(parsed, model_key)
            kw["miles"] = miles
            kw["price"] = price
            if kw.get("year") is None:
                kw["year"] = listing_year or vin_decode.model_year(vin)
            # Keep the listing text as a claimed fallback alongside verified data.
            kw["claimed_text"] = claimed_text
            kw["claimed_features"] = claimed_features
            kw["vin_facts"] = facts | c8_suspension_facts(model_key, kw["verified_rpo"])
            self._fallback_listing_color(kw, model_key, row)
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

    @staticmethod
    def _fallback_listing_color(kw: dict, model_key: str, row) -> None:
        """If neither the paint code nor the OEM document's marketing name maps
        to a color family, fall back to the listing's color text (both text
        paths score as claimed provenance anyway). Applies to sticker AND
        build-sheet cars — sheets often carry no paint line at all."""
        listing_color = row["color"] if row else None
        if (
            colors.family_from_rpo(model_key, kw.get("paint_rpo")) is None
            and colors.family_from_text(kw.get("color_text")) is None
            and colors.family_from_text(listing_color) is not None
        ):
            kw["color_text"] = listing_color

    def _log_conflicts(self, vin: str, model_key: str, parsed, row) -> None:
        """Detect + persist cross-source disagreements for one car.

        Only genuinely apples-to-apples fields are compared: model year and
        resolved color family. (Brand strings like sticker "CHEVY" vs listing
        "Chevrolet", and factory-vs-generic color names, would be spurious.)
        Listing facts are per-feed (listing:autodev, listing:marketcheck, ...)
        so two feeds disagreeing about the same car surfaces on the conflicts
        page; the vehicles row is only used as a fallback when no listings
        exist (e.g. a hand-tracked VIN).
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
        listing_facts = 0
        for lr in self._conn.execute(
            "SELECT source, year, color FROM listings WHERE vin = ?", (vin,)
        ).fetchall():
            fam = colors.family_from_text(lr["color"])
            if lr["year"] is None and fam is None:
                continue  # nothing comparable from this feed
            facts.append(
                Facts(
                    source=f"listing:{lr['source']}",
                    provenance=Provenance.claimed,
                    year=lr["year"],
                    exterior=fam.value if fam else None,
                )
            )
            listing_facts += 1
        if listing_facts == 0 and row is not None:
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
        db.replace_conflicts(self._conn, vin, rows, self._now(), commit=not self._defer_commits)

    def _persist(self, vin: str, model_key: str, result) -> None:
        breakdown = {
            "hard": [r.model_dump(mode="json") for r in result.hard],
            "soft": [r.model_dump(mode="json") for r in result.soft],
            "highlights": [r.model_dump(mode="json") for r in result.highlights],
        }
        # The scorer's resolved color, denormalized so lists render the same
        # color the detail page explains (listing text can disagree).
        color = next((s for s in result.soft if s.color_family), None)
        self._conn.execute(
            """INSERT INTO match_results
               (vin, model_key, verdict, soft_score, highlight_bonus, total,
                breakdown_json, scored_at, color_family, paint_name)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(vin) DO UPDATE SET
                 model_key=excluded.model_key, verdict=excluded.verdict,
                 soft_score=excluded.soft_score, highlight_bonus=excluded.highlight_bonus,
                 total=excluded.total, breakdown_json=excluded.breakdown_json,
                 scored_at=excluded.scored_at, color_family=excluded.color_family,
                 paint_name=excluded.paint_name""",
            (
                vin,
                model_key,
                result.verdict,
                result.soft_score,
                result.highlight_bonus,
                result.total,
                json.dumps(breakdown, separators=(",", ":")),
                self._now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                color.color_family if color else None,
                color.paint_name if color else None,
            ),
        )
        if not self._defer_commits:
            self._conn.commit()


def _build(conn, settings: Settings, current_year: int, with_source: bool) -> Pipeline:
    guard = BudgetGuard.from_settings(conn, settings)
    source = AutoDevSource(settings.autodev_api_key, guard) if with_source else None
    # Marketcheck rides along whenever its key is present (full passes only).
    secondary = (
        MarketcheckSource(settings.marketcheck_api_key, guard)
        if with_source and settings.marketcheck_api_key
        else None
    )
    fetcher = StickerFetcher(conn, guard, stickers_dir=settings.stickers_dir)
    buildsheet_fetcher = BuildSheetFetcher(conn, guard)
    partsdeal_fetchers = {make: PartsDealFetcher(conn, guard, make) for make in ("bmw", "toyota")}
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
        buildsheet_fetcher=buildsheet_fetcher,
        secondary_source=secondary,
        partsdeal_fetchers=partsdeal_fetchers,
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


_C8_FE_CODES = frozenset({"FE1", "FE2", "FE3", "FE4"})


def c8_suspension_facts(model_key: str, options: set[str]) -> dict[str, str]:
    """Definitive facts from the C8's mutually-exclusive FE suspension code.

    Corpus-verified against 312 parsed window stickers (2026-07-18, zero
    Z51 exceptions):
      FE1 = base            -> no Z51, no MagneRide      (103/103)
      FE2 = base + MR       -> no Z51, MagneRide         (34/36; both outliers
                               had garbled sticker text, code trusted)
      FE3 = Z51             -> Z51, no MagneRide         (86/86)
      FE4 = Z51 special R&H -> Z51; MagneRide UNDECIDED  (44 with / 43 without)
    FE4 therefore emits no magneride fact — the sticker evidence chain
    (F55 code / MAGNETIC text) decides, exactly like an unverified VIN range.
    """
    if model_key != "c8_stingray":
        return {}
    fe = options & _C8_FE_CODES
    if len(fe) != 1:
        return {}
    code = next(iter(fe))
    facts = {"z51": "YES" if code in ("FE3", "FE4") else "NO"}
    if code != "FE4":
        facts["magneride"] = "YES" if code == "FE2" else "NO"
    return facts


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
