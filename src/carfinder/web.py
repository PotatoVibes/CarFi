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
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import colors, features, profile_edit, store
from .budget import BudgetGuard
from .colors import ColorFamily
from .config import Settings, load_settings
from .db import bootstrap
from .pipeline import PipelineStats
from .profiles import ProfileError, load_profile
from .runner import load_profiles

log = logging.getLogger("carfinder.web")

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _form(request: Request) -> dict[str, list[str]]:
    """Parse a urlencoded form body without the python-multipart dependency
    (starlette's request.form() requires it even for urlencoded posts)."""
    return parse_qs((await request.body()).decode("utf-8"))


def _field(form: dict[str, list[str]], key: str, default: str = "") -> str:
    values = form.get(key)
    return values[0] if values else default


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
    def matches(
        request: Request,
        model_key: str,
        show_excluded: bool = False,
        show_dismissed: bool = False,
    ):
        profile = app.state.profiles_by_key.get(model_key)
        cookie_key = f"seen_{model_key}"
        since = request.cookies.get(cookie_key)
        c = conn()
        try:
            rows = store.ranked_matches(
                c,
                model_key,
                include_excluded=show_excluded,
                since=since,
                include_dismissed=show_dismissed,
            )
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
                "show_dismissed": show_dismissed,
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

    def _reload_profiles() -> None:
        try:
            app.state.profiles = load_profiles(settings.config_dir)
        except Exception:
            log.exception("profile reload failed")
        app.state.profiles_by_key = {p.model_key: p for p in app.state.profiles}

    def _profile_path(model_key: str) -> Path | None:
        for path in sorted(Path(settings.config_dir).glob("*.yaml")):
            try:
                if load_profile(path).model_key == model_key:
                    return path
            except ProfileError:
                continue
        return None

    @app.get("/config", response_class=HTMLResponse)
    def config_index():
        first = app.state.profiles[0].model_key if app.state.profiles else None
        if first:
            return RedirectResponse(f"/config/{first}", status_code=303)
        return HTMLResponse("No profiles found", status_code=404)

    @app.get("/config/{model_key}", response_class=HTMLResponse)
    def config_editor(request: Request, model_key: str, flash: str | None = None):
        path = _profile_path(model_key)
        if path is None:
            return HTMLResponse("Unknown model", status_code=404)
        profile = load_profile(path)
        c = conn()
        try:
            feats = features.catalog(c, profile)
        finally:
            c.close()
        price = next((s for s in profile.soft if s.kind == "numeric" and s.attr == "price"), None)
        miles = next((s for s in profile.soft if s.kind == "numeric" and s.attr == "miles"), None)
        color = next((s for s in profile.soft if s.kind == "color"), None)
        year = next((c2 for c2 in profile.hard if c2.kind == "year_min"), None)
        return _TEMPLATES.TemplateResponse(
            request,
            "config.html",
            {
                "profile": profile,
                "models": app.state.profiles,
                "feats": feats,
                "price": price,
                "miles": miles,
                "color": color,
                "year": year,
                "families": [f.value for f in ColorFamily],
                "flash": flash,
            },
        )

    def _apply_and_save(model_key: str, mutate) -> RedirectResponse:
        """Load the profile fresh from disk, apply an edit, save, hot-reload."""
        path = _profile_path(model_key)
        if path is None:
            return HTMLResponse("Unknown model", status_code=404)  # type: ignore[return-value]
        try:
            updated = mutate(load_profile(path))
            profile_edit.save_profile(path, updated)
        except (ProfileError, ValueError) as exc:
            return RedirectResponse(
                f"/config/{model_key}?flash={quote(f'Rejected: {exc}'[:180])}", status_code=303
            )
        _reload_profiles()
        saved_msg = quote("Saved — hit Rescore (home page) to re-rank existing cars.")
        return RedirectResponse(f"/config/{model_key}?flash={saved_msg}", status_code=303)

    @app.post("/config/{model_key}/feature")
    async def config_feature(request: Request, model_key: str):
        """Add a feature from the catalog OR a hand-entered custom one.
        A build code is optional for custom features — the matcher falls back
        to a text pattern (default: the literal feature name)."""
        form = await _form(request)
        action = _field(form, "action")
        code = _field(form, "code").strip().upper()
        desc = _field(form, "description", code).strip()
        text = _field(form, "sticker_text").strip() or None
        if not code and not desc:
            return RedirectResponse(f"/config/{model_key}", status_code=303)

        def mutate(p):
            if action == "highlight":
                return profile_edit.add_highlight(
                    p, code, desc, float(_field(form, "bonus", "3")), sticker_text=text
                )
            if action in ("require", "forbid"):
                return profile_edit.add_hard(p, code, desc, action, sticker_text=text)
            if action == "remove":
                return profile_edit.remove_code(p, code)
            raise ProfileError(f"unknown action {action!r}")

        return _apply_and_save(model_key, mutate)

    @app.post("/config/{model_key}/bounds")
    async def config_bounds(request: Request, model_key: str):
        form = await _form(request)

        def mutate(p):
            p = profile_edit.set_year_min(p, int(_field(form, "year_min")))
            p = profile_edit.set_numeric(
                p,
                "price",
                float(_field(form, "price_ideal")),
                float(_field(form, "price_hard")),
                float(_field(form, "price_weight")),
                "price_exclude" in form,
            )
            return profile_edit.set_numeric(
                p,
                "miles",
                float(_field(form, "miles_ideal")),
                float(_field(form, "miles_hard")),
                float(_field(form, "miles_weight")),
                "miles_exclude" in form,
            )

        return _apply_and_save(model_key, mutate)

    @app.post("/config/{model_key}/colors")
    async def config_colors(request: Request, model_key: str):
        form = await _form(request)
        preferred = form.get("preferred", [])
        penalized = [v for v in form.get("penalized", []) if v not in preferred]
        weight = float(_field(form, "color_weight", "0.1"))
        return _apply_and_save(
            model_key, lambda p: profile_edit.set_colors(p, preferred, penalized, weight)
        )

    @app.post("/config/{model_key}/remove")
    async def config_remove(request: Request, model_key: str):
        form = await _form(request)
        kind = _field(form, "kind")
        ident = _field(form, "ident")

        def mutate(p):
            if kind == "hard":
                return profile_edit.remove_hard(p, ident)
            if kind == "highlight":
                return profile_edit.remove_highlight(p, ident)
            raise ProfileError(f"unknown remove kind {kind!r}")

        return _apply_and_save(model_key, mutate)

    @app.post("/dismiss/{vin}")
    def dismiss(vin: str, next: str = "/"):
        """Toggle manual dismissal ('seen it, not interested') for a car."""
        # Only same-site relative redirects (no open-redirect via next=).
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        c = conn()
        try:
            row = c.execute("SELECT dismissed_at FROM vehicles WHERE vin = ?", (vin,)).fetchone()
            if row is None:
                return HTMLResponse("Unknown VIN", status_code=404)
            if row["dismissed_at"]:
                c.execute("UPDATE vehicles SET dismissed_at = NULL WHERE vin = ?", (vin,))
            else:
                c.execute("UPDATE vehicles SET dismissed_at = ? WHERE vin = ?", (_now_iso(), vin))
            c.commit()
        finally:
            c.close()
        return RedirectResponse(target, status_code=303)

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
