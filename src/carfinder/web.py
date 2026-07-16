"""FastAPI UI: ranked matches, car detail, budget, and conflicts.

Every read page renders from SQLite alone — no API key required to browse.
Only 'Sync now' and the scheduler need keys. The app is created via
create_app(settings) so tests can point it at a temp DB.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import colors, store
from .budget import BudgetGuard
from .config import Settings, load_settings
from .db import bootstrap
from .runner import load_profiles

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    app.state.sync_lock = threading.Lock()

    def conn():
        return bootstrap(settings.db_path)

    _TEMPLATES.env.filters["chip"] = lambda v: colors.chip_hex(
        v.get("model_key", ""), v.get("paint_rpo"), v.get("color")
    )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
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
                {"models": models, "latest_sync": store.latest_sync(c)},
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
    def conflicts(request: Request):
        c = conn()
        try:
            items = store.all_conflicts(c)
        finally:
            c.close()
        return _TEMPLATES.TemplateResponse(request, "conflicts.html", {"conflicts": items})

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

    @app.post("/sync")
    def sync_now():
        if not settings.autodev_api_key:
            return RedirectResponse("/?error=no_key", status_code=303)
        if not app.state.sync_lock.acquire(blocking=False):
            return RedirectResponse("/?sync=already_running", status_code=303)

        def _job():
            try:
                from .runner import run_sync

                c = bootstrap(settings.db_path)
                try:
                    run_sync(c, settings, datetime.now(UTC).year, mode="incremental")
                finally:
                    c.close()
            finally:
                app.state.sync_lock.release()

        threading.Thread(target=_job, daemon=True).start()
        return RedirectResponse("/?sync=started", status_code=303)

    return app
