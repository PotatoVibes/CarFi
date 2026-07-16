"""FastAPI UI: ranked matches, car detail, budget, and conflicts.

Every read page renders from SQLite alone — no API key required to browse.
Only 'Sync now' and the scheduler need keys. The app is created via
create_app(settings) so tests can point it at a temp DB.

Long-running work (sync / full pass / rescore) runs in a background thread,
one job at a time, and reports live progress through app.state.job — polled
by the header status pill via GET /api/job.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import colors, store
from .budget import BudgetGuard
from .config import Settings, load_settings
from .db import bootstrap
from .pipeline import PipelineStats
from .runner import load_profiles

log = logging.getLogger("carfinder.web")

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _summarize_sync(results: dict[str, PipelineStats]) -> str:
    new = sum(s.sync.new_vins for s in results.values())
    changed = sum(s.sync.price_changes for s in results.values())
    scored = sum(s.scored for s in results.values())
    ranked = sum(s.ranked for s in results.values())
    fetched = sum(s.stickers_fetched for s in results.values())
    sold = sum(len(s.sync.presumed_sold) for s in results.values())
    parts = [f"{new} new", f"{changed} price changes", f"{scored} scored ({ranked} ranked)"]
    if fetched:
        parts.append(f"{fetched} sticker fetches")
    if sold:
        parts.append(f"{sold} presumed sold")
    if any(s.sync.truncated for s in results.values()):
        parts.append("⚠ page ceiling hit — results incomplete")
    return " · ".join(parts)


def _summarize_rescore(results: dict[str, PipelineStats]) -> str:
    scored = sum(s.scored for s in results.values())
    ranked = sum(s.ranked for s in results.values())
    review = sum(s.needs_review for s in results.values())
    return f"re-scored {scored} cars ({ranked} ranked, {review} need review)"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="Car Finder")
    app.state.settings = settings
    # Profiles are static config; load once.
    try:
        app.state.profiles = load_profiles(settings.config_dir)
    except Exception:
        app.state.profiles = []
    app.state.profiles_by_key = {p.model_key: p for p in app.state.profiles}
    # One background job at a time; its live status feeds the UI status pill.
    app.state.job_lock = threading.Lock()
    app.state.job = {
        "state": "idle",  # idle | running | done | error
        "kind": None,  # sync | full pass | rescore
        "detail": "",
        "started_at": None,
        "finished_at": None,
        "summary": None,
        "error": None,
    }

    def conn():
        return bootstrap(settings.db_path)

    def _start_job(kind: str, work) -> bool:
        """Run `work(progress_cb)` in a background thread; False if one is
        already running. `work` returns a one-line summary string."""
        if not app.state.job_lock.acquire(blocking=False):
            return False
        app.state.job.update(
            state="running",
            kind=kind,
            detail="starting…",
            started_at=_now_iso(),
            finished_at=None,
            summary=None,
            error=None,
        )

        def _run():
            try:
                summary = work(lambda msg: app.state.job.update(detail=msg))
                app.state.job.update(state="done", summary=summary, finished_at=_now_iso())
            except Exception as exc:  # surface the failure in the UI, don't die silently
                log.exception("%s job failed", kind)
                app.state.job.update(
                    state="error",
                    error=f"{type(exc).__name__}: {exc}",
                    finished_at=_now_iso(),
                )
            finally:
                app.state.job_lock.release()

        threading.Thread(target=_run, daemon=True, name=f"carfinder-{kind}").start()
        return True

    def _flash_redirect(message: str) -> RedirectResponse:
        return RedirectResponse(f"/?flash={quote(message)}", status_code=303)

    _TEMPLATES.env.filters["chip"] = lambda v: colors.chip_hex(
        v.get("model_key", ""), v.get("paint_rpo"), v.get("color")
    )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, flash: str | None = None):
        c = conn()
        try:
            models = [
                {
                    "model_key": p.model_key,
                    "name": p.name,
                    "counts": store.match_counts(c, p.model_key),
                }
                for p in app.state.profiles
            ]
            return _TEMPLATES.TemplateResponse(
                request,
                "index.html",
                {
                    "models": models,
                    "latest_sync": store.latest_sync(c),
                    "flash": flash,
                    "has_autodev_key": bool(settings.autodev_api_key),
                },
            )
        finally:
            c.close()

    @app.get("/m/{model_key}", response_class=HTMLResponse)
    def matches(request: Request, model_key: str, show_excluded: bool = False):
        profile = app.state.profiles_by_key.get(model_key)
        cookie_key = f"seen_{model_key}"
        since = request.cookies.get(cookie_key)
        c = conn()
        try:
            rows = store.ranked_matches(c, model_key, include_excluded=show_excluded, since=since)
            counts = store.match_counts(c, model_key)
        finally:
            c.close()
        resp = _TEMPLATES.TemplateResponse(
            request,
            "matches.html",
            {
                "model_key": model_key,
                "name": profile.name if profile else model_key,
                "rows": rows,
                "counts": counts,
                "show_excluded": show_excluded,
                "chip_hex": colors.chip_hex,
            },
        )
        resp.set_cookie(cookie_key, _now_iso(), max_age=60 * 60 * 24 * 365)
        return resp

    @app.get("/v/{vin}", response_class=HTMLResponse)
    def detail(request: Request, vin: str):
        c = conn()
        try:
            det = store.vehicle_detail(c, vin)
        finally:
            c.close()
        if det is None:
            return HTMLResponse("Unknown VIN", status_code=404)
        sticker_pdf = (settings.stickers_dir / f"{vin.upper()}.pdf").exists()
        return _TEMPLATES.TemplateResponse(
            request,
            "detail.html",
            {"d": det, "chip_hex": colors.chip_hex, "sticker_pdf": sticker_pdf},
        )

    @app.get("/budget", response_class=HTMLResponse)
    def budget(request: Request):
        c = conn()
        try:
            guard = BudgetGuard.from_settings(c, settings)
            usage = guard.all_usage()
        finally:
            c.close()
        return _TEMPLATES.TemplateResponse(request, "budget.html", {"usage": usage})

    @app.get("/conflicts", response_class=HTMLResponse)
    def conflicts(request: Request, show_excluded: bool = False):
        c = conn()
        try:
            items = store.all_conflicts(c, include_excluded=show_excluded)
        finally:
            c.close()
        return _TEMPLATES.TemplateResponse(
            request, "conflicts.html", {"conflicts": items, "show_excluded": show_excluded}
        )

    @app.get("/sticker/{vin}")
    def sticker(vin: str):
        # The cached PDF on disk is the source of truth; the stickers table is
        # fetch bookkeeping and may lag (e.g. a committed fixture never fetched).
        path = settings.stickers_dir / f"{vin.upper()}.pdf"
        if path.exists():
            return FileResponse(str(path), media_type="application/pdf")
        c = conn()
        try:
            row = c.execute(
                "SELECT path FROM stickers WHERE vin = ? AND status = 'found'", (vin,)
            ).fetchone()
        finally:
            c.close()
        if row and row["path"] and Path(row["path"]).exists():
            return FileResponse(row["path"], media_type="application/pdf")
        return Response("No sticker on file", status_code=404)

    @app.get("/api/job")
    def job_status():
        return {"job": app.state.job}

    def _sync_work(mode: str):
        def work(progress):
            from .runner import run_sync

            c = bootstrap(settings.db_path)
            try:
                results = run_sync(
                    c,
                    settings,
                    datetime.now(UTC).year,
                    mode=mode,
                    config_dir=settings.config_dir,
                    progress=progress,
                )
            finally:
                c.close()
            return _summarize_sync(results)

        return work

    def _start_sync(mode: str, kind: str) -> RedirectResponse:
        if not settings.autodev_api_key:
            return _flash_redirect("Sync needs AUTODEV_API_KEY in .env — no key found.")
        if not _start_job(kind, _sync_work(mode)):
            return _flash_redirect("A job is already running — wait for it to finish.")
        return RedirectResponse("/", status_code=303)

    @app.post("/sync")
    def sync_now():
        return _start_sync("incremental", "sync")

    @app.post("/sync/full")
    def sync_full():
        return _start_sync("full", "full pass")

    @app.post("/rescore")
    def rescore_now():
        def work(progress):
            from .runner import run_rescore

            c = bootstrap(settings.db_path)
            try:
                results = run_rescore(
                    c,
                    settings,
                    datetime.now(UTC).year,
                    config_dir=settings.config_dir,
                    progress=progress,
                )
            finally:
                c.close()
            return _summarize_rescore(results)

        if not _start_job("rescore", work):
            return _flash_redirect("A job is already running — wait for it to finish.")
        return RedirectResponse("/", status_code=303)

    return app
