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
from . import vin as vin_decode
from .budget import BudgetGuard
from .colors import ColorFamily
from .config import Settings, load_settings
from .db import bootstrap
from .pipeline import PipelineStats
from .profiles import ProfileError, load_profile
from .runner import load_profiles

log = logging.getLogger("carfinder.web")

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class JobCancelled(Exception):
    """Raised inside a background job's progress callback to stop it early."""


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _form(request: Request) -> dict[str, list[str]]:
    """Parse a urlencoded form body without the python-multipart dependency
    (starlette's request.form() requires it even for urlencoded posts)."""
    return parse_qs((await request.body()).decode("utf-8"))


def _field(form: dict[str, list[str]], key: str, default: str = "") -> str:
    values = form.get(key)
    return values[0] if values else default


def _unicorn_vins(conn, model_key: str, profile) -> set[str]:
    """VINs on this model's board that clear the unicorn bar (same definition
    as the `unicorns` CLI: ranked + preferred color + within the miles hard
    bound + highlight bonus >= 5). Purely decorative — worth a 🦄."""
    if profile is None:
        return set()
    color_c = next((s for s in profile.soft if s.kind == "color"), None)
    miles_c = next((s for s in profile.soft if s.kind == "numeric" and s.attr == "miles"), None)
    preferred = color_c.preferred if color_c else []
    if not preferred:
        return set()
    cap = int(miles_c.hard) if miles_c else 60000
    return {u.vin for u in store.unicorn_arrivals(conn, model_key, preferred, miles_cap=cap)}


def _summarize_sync(results: dict[str, PipelineStats]) -> str:
    new = sum(s.sync.new_vins for s in results.values())
    changed = sum(s.sync.price_changes for s in results.values())
    scored = sum(s.scored for s in results.values())
    ranked = sum(s.ranked for s in results.values())
    fetched = sum(s.stickers_fetched for s in results.values())
    sold = sum(len(s.sync.presumed_sold) for s in results.values())
    mc_new = sum(s.mc_sync.new_vins for s in results.values() if s.mc_sync is not None)
    parts = [f"{new} new", f"{changed} price changes", f"{scored} scored ({ranked} ranked)"]
    if mc_new:
        parts.append(f"{mc_new} via marketcheck")
    if fetched:
        parts.append(f"{fetched} sticker fetches")
    if sold:
        parts.append(f"{sold} presumed sold")
    if any(s.sync.truncated for s in results.values()):
        parts.append("⚠ page ceiling hit — results incomplete")
    if any(s.mc_failed for s in results.values()):
        parts.append("⚠ marketcheck partial — delist skipped")
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
    app.state.job_cancel = threading.Event()
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
        already running. `work` returns a one-line summary string.

        Cancellation is cooperative: /job/cancel sets an event that the
        progress callback checks — every job reports progress at least
        per-car/per-page, so a stop lands within about a second. Work already
        committed stays committed (per-car results are each complete)."""
        if not app.state.job_lock.acquire(blocking=False):
            return False
        app.state.job_cancel.clear()
        app.state.job.update(
            state="running",
            kind=kind,
            detail="starting…",
            started_at=_now_iso(),
            finished_at=None,
            summary=None,
            error=None,
        )

        def _progress(msg: str) -> None:
            if app.state.job_cancel.is_set():
                raise JobCancelled()
            app.state.job.update(detail=msg)

        def _run():
            try:
                summary = work(_progress)
                app.state.job.update(state="done", summary=summary, finished_at=_now_iso())
            except JobCancelled:
                app.state.job.update(
                    state="done",
                    summary="stopped by user — completed work kept",
                    finished_at=_now_iso(),
                )
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
                home_zip=settings.home_zip,
            )
            counts = store.match_counts(c, model_key)
            unicorn_vins = _unicorn_vins(c, model_key, profile)
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
                "resolved_chip_hex": colors.resolved_chip_hex,
                "unicorn_vins": unicorn_vins,
            },
        )
        resp.set_cookie(cookie_key, _now_iso(), max_age=60 * 60 * 24 * 365)
        return resp

    @app.get("/v/{vin}", response_class=HTMLResponse)
    def detail(request: Request, vin: str):
        c = conn()
        try:
            det = store.vehicle_detail(c, vin, home_zip=settings.home_zip)
        finally:
            c.close()
        if det is None:
            return HTMLResponse("Unknown VIN", status_code=404)
        sticker_pdf = (settings.stickers_dir / f"{vin.upper()}.pdf").exists()
        return _TEMPLATES.TemplateResponse(
            request,
            "detail.html",
            {
                "d": det,
                "chip_hex": colors.chip_hex,
                "resolved_chip_hex": colors.resolved_chip_hex,
                "sticker_pdf": sticker_pdf,
                "home_zip": settings.home_zip,
                "has_autodev_key": bool(settings.autodev_api_key),
            },
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

    @app.post("/config/{model_key}/bonus")
    async def config_bonus(request: Request, model_key: str):
        form = await _form(request)
        ident = _field(form, "ident")
        bonus = float(_field(form, "bonus", "0"))
        return _apply_and_save(
            model_key, lambda p: profile_edit.set_highlight_bonus(p, ident, bonus)
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

    def _toggle_flag(vin: str, next: str, column: str, opposite: str):
        """Toggle a user flag on a vehicle; setting it clears the opposite
        (liked and not-interested are mutually exclusive)."""
        # Only same-site relative redirects (no open-redirect via next=).
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        c = conn()
        try:
            row = c.execute(f"SELECT {column} FROM vehicles WHERE vin = ?", (vin,)).fetchone()
            if row is None:
                return HTMLResponse("Unknown VIN", status_code=404)
            if row[column]:
                c.execute(f"UPDATE vehicles SET {column} = NULL WHERE vin = ?", (vin,))
            else:
                c.execute(
                    f"UPDATE vehicles SET {column} = ?, {opposite} = NULL WHERE vin = ?",
                    (_now_iso(), vin),
                )
            c.commit()
        finally:
            c.close()
        return RedirectResponse(target, status_code=303)

    @app.post("/dismiss/{vin}")
    def dismiss(vin: str, next: str = "/"):
        """Toggle 'seen it, not interested' (clears any favorite)."""
        return _toggle_flag(vin, next, "dismissed_at", "favorited_at")

    @app.post("/favorite/{vin}")
    def favorite(vin: str, next: str = "/"):
        """Toggle 'looked at it and liked it' (clears any dismissal)."""
        return _toggle_flag(vin, next, "favorited_at", "dismissed_at")

    @app.get("/api/job")
    def job_status():
        return {"job": app.state.job}

    @app.post("/job/cancel")
    def job_cancel():
        """Ask the running job to stop (cooperative — it lands within ~a
        second, at the next progress tick). No-op when nothing is running."""
        if app.state.job["state"] == "running":
            app.state.job_cancel.set()
            app.state.job.update(detail="stopping…")
            return {"cancelling": True}
        return {"cancelling": False}

    def _sync_work(mode: str, only_model: str | None = None):
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
                    only_model=only_model,
                )
            finally:
                c.close()
            return _summarize_sync(results)

        return work

    def _start_sync(mode: str, kind: str, model: str | None = None) -> RedirectResponse:
        """Kick off a sync job — every profile, or just `model` when given."""
        if model is not None:
            profile = app.state.profiles_by_key.get(model)
            if profile is None:
                return HTMLResponse("Unknown model", status_code=404)  # type: ignore[return-value]
            kind = f"{kind} · {profile.name}"
        if not settings.autodev_api_key:
            return _flash_redirect("Sync needs AUTODEV_API_KEY in .env — no key found.")
        if not _start_job(kind, _sync_work(mode, only_model=model)):
            return _flash_redirect("A job is already running — wait for it to finish.")
        # Model-scoped syncs come from that model's page — return there.
        return RedirectResponse(f"/m/{model}" if model else "/", status_code=303)

    @app.post("/sync")
    def sync_now(model: str | None = None):
        return _start_sync("incremental", "sync", model)

    @app.post("/sync/full")
    def sync_full(model: str | None = None):
        return _start_sync("full", "full pass", model)

    # World-manufacturer prefix -> which profile a hand-entered VIN belongs to.
    _VIN_PREFIX_MODEL = {"1G1Y": "c8_stingray", "1G6D": "ct4v_blackwing"}

    @app.post("/vins")
    async def add_vin(request: Request):
        """Track a VIN by hand: validate it, then run the same VIN-decode ->
        sticker -> build-sheet -> score chain synced cars get. No listing —
        price/miles/location stay unknown until a sync happens to see it."""
        form = await _form(request)
        vin = _field(form, "vin").strip().upper()
        error = vin_decode.validate(vin)
        if error is not None:
            return _flash_redirect(f"Not a valid VIN: {error}.")
        model_key = _VIN_PREFIX_MODEL.get(vin[:4])
        profile = app.state.profiles_by_key.get(model_key) if model_key else None
        if profile is None:
            prefixes = ", ".join(f"{k}… ({v})" for k, v in _VIN_PREFIX_MODEL.items())
            return _flash_redirect(f"VIN {vin} isn't a tracked model — expected {prefixes}.")

        c = conn()
        try:
            exists = c.execute("SELECT 1 FROM vehicles WHERE vin = ?", (vin,)).fetchone()
            if exists is None:
                now = _now_iso()
                c.execute(
                    """INSERT INTO vehicles (vin, model_key, year, status, missing_passes,
                       first_seen, last_seen) VALUES (?,?,?, 'active', 0, ?, ?)""",
                    (vin, model_key, vin_decode.model_year(vin), now, now),
                )
                c.commit()
        finally:
            c.close()
        if exists is not None:
            return RedirectResponse(f"/v/{vin}", status_code=303)  # already tracked

        def work(progress):
            from .pipeline import build_rescore_pipeline

            c2 = bootstrap(settings.db_path)
            try:
                pipeline = build_rescore_pipeline(c2, settings, datetime.now(UTC).year)
                return pipeline.score_vin(vin, profile, progress=progress)
            finally:
                c2.close()

        if not _start_job("add VIN", work):
            return _flash_redirect(
                f"{vin} added, but a job is already running — hit Rescore later to score it."
            )
        return RedirectResponse(f"/v/{vin}", status_code=303)

    @app.post("/v/{vin}/listing")
    async def set_listing(request: Request, vin: str):
        """Attach/correct a listing by hand: URL and/or price and miles.
        Blank price/miles are pulled from the page when a URL is given (best
        effort); the page text becomes claimed evidence and the car rescores."""
        vin = vin.upper()
        form = await _form(request)
        url_raw = _field(form, "url").strip()
        url = url_raw if url_raw.lower().startswith(("http://", "https://")) else None
        if url_raw and url is None:
            return _flash_redirect("Listing URL must start with http:// or https://.")

        def _num(key: str) -> float | None:
            s = _field(form, key).replace(",", "").replace("$", "").strip()
            try:
                return float(s) if s else None
            except ValueError:
                return None

        price_in, miles_in = _num("price"), _num("miles")
        if url is None and price_in is None and miles_in is None:
            return RedirectResponse(f"/v/{vin}", status_code=303)  # nothing to save
        c = conn()
        try:
            v = c.execute("SELECT model_key FROM vehicles WHERE vin = ?", (vin,)).fetchone()
        finally:
            c.close()
        if v is None:
            return HTMLResponse("Unknown VIN", status_code=404)
        profile = app.state.profiles_by_key.get(v["model_key"])
        if profile is None:
            return _flash_redirect(f"No profile loaded for {v['model_key']!r}.")

        def work(progress):
            from .manual_listing import extract_miles, extract_price, fetch_page_text
            from .pipeline import build_rescore_pipeline
            from .sources import ListingRow, ManualSource
            from .sync import SyncEngine

            c2 = bootstrap(settings.db_path)
            try:
                page_text = None
                if url:
                    progress("fetching the listing page…")
                    page_text = fetch_page_text(url)
                price = price_in if price_in is not None else extract_price(page_text)
                miles = miles_in if miles_in is not None else extract_miles(page_text)
                vrow = c2.execute("SELECT * FROM vehicles WHERE vin = ?", (vin,)).fetchone()
                row = ListingRow(
                    vin=vin,
                    source="manual",
                    # New info wins; otherwise carry what we know so the
                    # vehicles upsert can't blank existing fields.
                    price=price if price is not None else vrow["price"],
                    miles=int(miles) if miles is not None else vrow["miles"],
                    year=vrow["year"],
                    make=vrow["make"],
                    model=vrow["model"],
                    trim=vrow["trim"],
                    color=vrow["color"],
                    url=url,
                    dealer=vrow["dealer"],
                    city=vrow["city"],
                    state=vrow["state"],
                    zip=vrow["zip"],
                    raw={
                        "manual": True,
                        "url": url,
                        "saved_at": _now_iso(),
                        "page_text": page_text or "",
                    },
                )
                change = SyncEngine(c2, ManualSource()).upsert(row, vrow["model_key"])
                parts = [f"listing info saved ({change.replace('_', ' ')})"]
                if url and page_text is None:
                    parts.append("page fetch failed — typed values only")
                elif url and (price_in is None or miles_in is None):
                    found = [
                        f"price ${price:,.0f}" if price_in is None and price is not None else None,
                        f"{miles:,.0f} mi" if miles_in is None and miles is not None else None,
                    ]
                    found = [f for f in found if f]
                    parts.append(
                        "from page: " + ", ".join(found) if found else "page had no price/miles"
                    )
                pipeline = build_rescore_pipeline(c2, settings, datetime.now(UTC).year)
                parts.append(pipeline.score_vin(vin, profile, progress=progress))
                return " · ".join(parts)
            finally:
                c2.close()

        if not _start_job("listing update", work):
            return _flash_redirect("A job is already running — wait for it to finish.")
        return RedirectResponse(f"/v/{vin}", status_code=303)

    @app.post("/refresh/{vin}")
    def refresh_one(vin: str):
        """Refresh a single car — one Auto.dev call + a rescore of that VIN."""
        vin = vin.upper()
        c = conn()
        try:
            v = c.execute("SELECT model_key FROM vehicles WHERE vin = ?", (vin,)).fetchone()
        finally:
            c.close()
        if v is None:
            return HTMLResponse("Unknown VIN", status_code=404)
        profile = app.state.profiles_by_key.get(v["model_key"])
        if profile is None:
            return _flash_redirect(f"No profile loaded for {v['model_key']!r}.")
        if not settings.autodev_api_key:
            return _flash_redirect("Refresh needs AUTODEV_API_KEY in .env — no key found.")

        def work(progress):
            from .pipeline import build_pipeline

            c2 = bootstrap(settings.db_path)
            try:
                pipeline = build_pipeline(c2, settings, datetime.now(UTC).year)
                return pipeline.refresh_vin(vin, profile, progress=progress)
            finally:
                c2.close()

        if not _start_job("refresh", work):
            return _flash_redirect("A job is already running — wait for it to finish.")
        return RedirectResponse(f"/v/{vin}", status_code=303)

    @app.post("/rescore")
    def rescore_now(model: str | None = None):
        # Scoped to one model when requested (the per-model button): a full
        # rescore walks every car in the DB — thousands — for no benefit when
        # only one profile changed.
        if model is not None and model not in app.state.profiles_by_key:
            return _flash_redirect(f"Unknown model {model!r}.")

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
                    only_model=model,
                )
            finally:
                c.close()
            return _summarize_rescore(results)

        kind = f"rescore {model}" if model else "rescore"
        if not _start_job(kind, work):
            return _flash_redirect("A job is already running — wait for it to finish.")
        return RedirectResponse(f"/m/{model}" if model else "/", status_code=303)

    return app
