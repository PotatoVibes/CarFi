"""carfinder command-line interface."""

from __future__ import annotations

import typer

from .budget import COST, BudgetGuard
from .config import load_settings
from .db import bootstrap

app = typer.Typer(add_completion=False, help="Car Finder — VIN-verified listing search.")


@app.callback()
def main() -> None:
    """Car Finder — VIN-verified listing search."""


def _fmt(usage_value: float, kind: str) -> str:
    """Render a usage/cap number: dollars for cost, plain count otherwise.

    Cost uses 4 decimals — AI calls are sub-cent, and a $2 budget would
    otherwise show every real spend as $0.00.
    """
    return f"${usage_value:,.4f}" if kind == COST else f"{usage_value:,.0f}"


@app.command()
def fetch_stickers(vins: list[str]) -> None:
    """Fetch window sticker PDFs for one or more VINs (cache-first)."""
    settings = load_settings()
    conn = bootstrap(settings.db_path)
    try:
        from .stickers import StickerFetcher

        guard = BudgetGuard.from_settings(conn, settings)
        fetcher = StickerFetcher(conn, guard, stickers_dir=settings.stickers_dir)
        for vin in vins:
            result = fetcher.fetch(vin)
            source = "cache" if result.from_cache else "live"
            location = f" -> {result.path}" if result.path else ""
            typer.echo(f"  {result.vin}  {result.status:<10} ({source}){location}")
    finally:
        conn.close()


@app.command()
def sync(
    full: bool = False,
    model: str = typer.Option(None, help="Only this model_key (e.g. ct4v_blackwing)"),
) -> None:
    """Run a sync over every configured profile — or just one with --model."""
    from datetime import UTC, datetime

    from .runner import run_sync

    settings = load_settings()
    conn = bootstrap(settings.db_path)
    try:
        results = run_sync(
            conn,
            settings,
            datetime.now(UTC).year,
            mode="full" if full else "incremental",
            config_dir=settings.config_dir,
            only_model=model,
        )
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    for model_key, s in results.items():
        mc = ""
        if s.mc_sync is not None:
            mc = f", marketcheck {s.mc_sync.new_vins} new"
            if s.mc_failed:
                mc += " (PARTIAL — a segment failed; delist skipped)"
        typer.echo(
            f"  {model_key}: {s.scored} scored ({s.ranked} ranked, {s.needs_review} review, "
            f"{s.excluded} excluded), {s.sync.new_vins} new{mc}, "
            f"{s.stickers_fetched} sticker fetches"
        )


@app.command()
def rescore(
    only_model: str = typer.Option(
        None,
        "--only-model",
        "-m",
        help="Re-score just this model_key (e.g. ct4v_blackwing) instead of every profile",
    ),
) -> None:
    """Re-score every car in the database against the current profiles.

    Run this after editing a profile YAML — it re-ranks the existing cars using
    cached sticker/AI data, so it makes ~zero API calls and no Auto.dev key is
    needed. After editing ONE profile, --only-model skips the other models'
    cars entirely (a full-database rescore is thousands of cars)."""
    from datetime import UTC, datetime

    from .runner import run_rescore

    settings = load_settings()
    conn = bootstrap(settings.db_path)
    try:
        results = run_rescore(
            conn,
            settings,
            datetime.now(UTC).year,
            config_dir=settings.config_dir,
            only_model=only_model,
        )
    except ValueError as exc:  # unknown --only-model: show the known keys
        raise typer.BadParameter(str(exc)) from exc
    finally:
        conn.close()
    for model_key, s in results.items():
        typer.echo(
            f"  {model_key}: re-scored {s.scored} ({s.ranked} ranked, "
            f"{s.needs_review} review, {s.excluded} excluded)"
        )


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, no_scheduler: bool = False) -> None:
    """Start the web UI (and, unless --no-scheduler, the sync scheduler)."""
    import uvicorn

    from .scheduler import build_scheduler
    from .web import create_app

    settings = load_settings()
    application = create_app(settings)
    if not no_scheduler and settings.autodev_api_key:
        build_scheduler(settings).start()
        typer.echo("scheduler started (Mon/Wed/Fri incremental, Sun full pass)")
    elif not no_scheduler:
        typer.echo("scheduler disabled (no AUTODEV_API_KEY)")
    uvicorn.run(application, host=host, port=port)


@app.command()
def budget() -> None:
    """Print month-to-date API usage per provider."""
    settings = load_settings()
    conn = bootstrap(settings.db_path)
    try:
        guard = BudgetGuard.from_settings(conn, settings)
        typer.echo("Month-to-date API usage (calendar month, UTC):")
        typer.echo(f"  DB: {settings.db_path}")
        typer.echo("")
        for u in guard.all_usage():
            pct = u.fraction * 100 if u.cap > 0 else float("inf")
            typer.echo(
                f"  {u.provider:<10} {_fmt(u.used, u.kind):>10} / {_fmt(u.cap, u.kind):<10}"
                f" ({pct:5.1f}%)  remaining {_fmt(u.remaining, u.kind)}"
            )
    finally:
        conn.close()


@app.command()
def marketcheck_probe(
    make: str = typer.Option("Ford", help="Probe query make (high-volume by default)"),
    model: str = typer.Option("F-150", help="Probe query model"),
    year_min: int = typer.Option(2018, help="Probe query min year"),
    year_max: int = typer.Option(2024, help="Probe query max year"),
    zip_code: str = typer.Option(None, "--zip", help="Anchor zip (defaults to home zip)"),
    yes: bool = typer.Option(False, "--yes", help="Skip the live-call confirmation prompt"),
) -> None:
    """Settle Marketcheck's undocumented free-tier radius behavior with ~3 LIVE
    gated calls. Prints the evidence (hit counts + state spread) and a verdict.

    Spends real quota against the 500/mo free tier, so it prompts before calling
    and every call is recorded by BudgetGuard (provider 'marketcheck'). Requires
    MARKETCHECK_API_KEY.
    """
    from uuid import uuid4

    from .marketcheck_probe import interpret, run_probe
    from .sources import MarketcheckSource

    settings = load_settings()
    if not settings.marketcheck_api_key:
        typer.echo("error: MARKETCHECK_API_KEY is not set (add it to .env)", err=True)
        raise typer.Exit(code=1)
    zip_code = zip_code or settings.home_zip

    typer.echo(
        f"Probe: {make} {model} {year_min}-{year_max}, anchor zip {zip_code}\n"
        "This makes ~3 LIVE Marketcheck calls against your 500/mo free tier."
    )
    if not yes:
        typer.confirm("Proceed?", abort=True)

    conn = bootstrap(settings.db_path)
    try:
        guard = BudgetGuard.from_settings(conn, settings)
        source = MarketcheckSource(settings.marketcheck_api_key, guard)
        run_id = f"mc-probe-{uuid4().hex[:8]}"
        results = run_probe(source, make, model, year_min, year_max, zip_code, run_id=run_id)

        typer.echo("")
        typer.echo(f"  {'query':<26} {'num_found':>10} {'rows':>5}   states (first page)")
        for r in results:
            if r.error:
                typer.echo(f"  {r.label:<26} {'ERROR':>10}   {r.error}")
                continue
            found = f"{r.num_found:,}" if r.num_found is not None else "?"
            st = ", ".join(f"{s}:{n}" for s, n in sorted(r.states.items())) or "-"
            typer.echo(f"  {r.label:<26} {found:>10} {r.rows_returned:>5}   {st}")

        typer.echo("")
        typer.echo("Verdict:")
        for line in interpret(results):
            typer.echo(f"  - {line}")

        typer.echo("")
        u = guard.usage("marketcheck")
        typer.echo(
            f"marketcheck budget: {u.used:,.0f} / {u.cap:,.0f} used this month "
            f"(remaining {u.remaining:,.0f})"
        )
    finally:
        conn.close()


@app.command()
def marketcheck_test(
    model: str = typer.Option("ct4v_blackwing", help="model_key of the profile to dry-run"),
    year: int = typer.Option(2022, help="Single model-year segment to fetch"),
    no_price: bool = typer.Option(
        False, "--no-price", help="Dry-run the has_price=false sweep variant instead"
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip the live-call confirmation prompt"),
) -> None:
    """Dry-run ONE Marketcheck sync segment (1 live call, page 1 only).

    Builds the exact query a full pass would send for one model year, fetches
    the first page through the real client + normalizer, and prints what came
    back — num_found, the segment's real page cost, and each row (with its
    VIN-decoded identity, e.g. BLACKWING vs NON_BLACKWING). Writes NOTHING to
    the database: pure preview.
    """
    import math
    from uuid import uuid4

    import httpx

    from . import vin as vin_decode
    from .profiles import load_profile
    from .query import build_marketcheck_params
    from .sources import MarketcheckSource, scrub_api_key

    settings = load_settings()
    if not settings.marketcheck_api_key:
        typer.echo("error: MARKETCHECK_API_KEY is not set (add it to .env)", err=True)
        raise typer.Exit(code=1)

    profile_path = settings.config_dir / f"{model}.yaml"
    if not profile_path.exists():
        typer.echo(f"error: no profile at {profile_path}", err=True)
        raise typer.Exit(code=1)
    profile = load_profile(profile_path)

    from datetime import UTC, datetime

    params = build_marketcheck_params(profile, datetime.now(UTC).year)
    params.pop("year_range", None)
    params["year"] = str(year)  # one real segment, single-year format
    if no_price:
        # The full pass's unpriced sweep: has_price=false, no price bound.
        params.pop("price_range", None)
        params["has_price"] = "false"
    q = " ".join(f"{k}={v}" for k, v in sorted(params.items()))
    typer.echo(f"Segment query (exactly what a full pass sends): {q}")
    typer.echo("This makes ONE live Marketcheck call. Nothing is written to the DB.")
    if not yes:
        typer.confirm("Proceed?", abort=True)

    conn = bootstrap(settings.db_path)
    try:
        guard = BudgetGuard.from_settings(conn, settings)
        source = MarketcheckSource(settings.marketcheck_api_key, guard)
        try:
            page = source.search_page(params, page=1, run_id=f"mc-test-{uuid4().hex[:8]}")
        except httpx.HTTPError as exc:
            typer.echo(f"error: {scrub_api_key(str(exc))}", err=True)
            raise typer.Exit(code=1) from None

        total = page.total or 0
        pages = math.ceil(total / 50) if total else 0
        typer.echo(f"\nnum_found: {total:,}  ->  this segment costs {pages} call(s) in a full pass")
        if total > 500:
            typer.echo("  ⚠ over the free tier's 500-row window — the walk would truncate")

        typer.echo("")
        for r in page.rows:
            identity = vin_decode.decode(r.vin).get("model") or "?"
            price = f"${r.price:,.0f}" if r.price is not None else "$?"
            miles = f"{r.miles:,}mi" if r.miles is not None else "?mi"
            typer.echo(
                f"  {r.vin}  {r.year or '?'}  {price:>9} {miles:>10}  "
                f"{(r.color or '?')[:18]:<18} {identity:<14} {r.state or '?'}"
            )
        known = {row["vin"] for row in conn.execute("SELECT vin FROM vehicles").fetchall()}
        overlap = sum(1 for r in page.rows if r.vin in known)
        typer.echo(
            f"\n{len(page.rows)} rows on page 1 · {overlap} already known from other sources "
            f"· {len(page.rows) - overlap} would be new VINs"
        )

        u = guard.usage("marketcheck")
        typer.echo(
            f"marketcheck budget: {u.used:,.0f} / {u.cap:,.0f} used this month "
            f"(remaining {u.remaining:,.0f})"
        )
    finally:
        conn.close()


@app.command()
def marketcheck_facets(
    make: str = typer.Option(..., help="Make to inspect (e.g. Cadillac)"),
    model: str = typer.Option(None, help="Optionally narrow to one model (their exact name)"),
    facet: str = typer.Option(
        "model", help="Field(s) to facet, comma-separated (e.g. model / year / model,year)"
    ),
    year_min: int = typer.Option(None, help="Optional year floor for the filter"),
    year_max: int = typer.Option(None, help="Optional year ceiling for the filter"),
    yes: bool = typer.Option(False, "--yes", help="Skip the live-call confirmation prompt"),
) -> None:
    """ONE live gated call: Marketcheck's value distribution for a field.

    The taxonomy ground-truth tool. `--make Cadillac --facet model` lists every
    Cadillac model name they use (with counts) — copy the right one into the
    profile's query.marketcheck_model. `--make Chevrolet --model Corvette
    --facet year` shows per-year counts (checks the 500-row window per segment).
    """
    from uuid import uuid4

    import httpx

    from .sources import MarketcheckSource, scrub_api_key

    settings = load_settings()
    if not settings.marketcheck_api_key:
        typer.echo("error: MARKETCHECK_API_KEY is not set (add it to .env)", err=True)
        raise typer.Exit(code=1)

    params: dict[str, str] = {"make": make}
    if model:
        params["model"] = model
    if year_min is not None or year_max is not None:
        # year_range takes "min-max"; the plain `year` param does NOT (it
        # reads "2022-2030" as just 2022 — verified live, 2026-07-21).
        lo = year_min if year_min is not None else 2019
        hi = year_max if year_max is not None else 2030
        params["year_range"] = f"{lo}-{hi}"
    fields = [f.strip() for f in facet.split(",") if f.strip()]

    filt = " ".join(f"{k}={v}" for k, v in params.items())
    typer.echo(f"Facet {'+'.join(fields)} over: {filt}\nThis makes ONE live Marketcheck call.")
    if not yes:
        typer.confirm("Proceed?", abort=True)

    conn = bootstrap(settings.db_path)
    try:
        guard = BudgetGuard.from_settings(conn, settings)
        source = MarketcheckSource(settings.marketcheck_api_key, guard)
        try:
            total, result = source.facets(params, fields, run_id=f"mc-facet-{uuid4().hex[:8]}")
        except httpx.HTTPError as exc:
            typer.echo(f"error: {scrub_api_key(str(exc))}", err=True)
            raise typer.Exit(code=1) from None

        if total is not None:
            typer.echo(f"\n{total:,} active listings match the filter")
        for field_name in fields:
            pairs = result.get(field_name, [])
            typer.echo(f"\n{field_name}:")
            if not pairs:
                typer.echo(
                    "  (no facet values returned — the API may not support faceting "
                    "this field, or the filter matched nothing)"
                )
                continue
            for value, count in pairs:
                typer.echo(f"  {count:>7,}  {value}")

        u = guard.usage("marketcheck")
        typer.echo(
            f"\nmarketcheck budget: {u.used:,.0f} / {u.cap:,.0f} used this month "
            f"(remaining {u.remaining:,.0f})"
        )
    finally:
        conn.close()


@app.command()
def unicorns(
    min_bonus: float = typer.Option(5.0, help="Minimum highlight bonus to qualify"),
    miles_cap: int = typer.Option(
        None, help="Override the mileage cap (default: each profile's miles hard bound)"
    ),
    since: str = typer.Option(
        None, help="Only count arrivals first seen on/after this ISO date (e.g. 2026-07-17)"
    ),
    cars: bool = typer.Option(True, help="List the individual matching cars"),
) -> None:
    """Track 'unicorn' arrivals over time — cars clearing every hard requirement,
    in a preferred color, within mileage, with a highlight bonus >= --min-bonus.

    Counts are grouped by first_seen (when the tool first saw the VIN), so once
    the incremental sync has run for a sustained window these become a real
    monthly arrival rate. The footer shows the observation window: a one-time
    bulk backfill stamps everything with a single day, and that history is NOT
    observed arrivals — use --since <day after backfill> to count only real ones.
    """
    from . import store
    from .runner import load_profiles

    settings = load_settings()
    conn = bootstrap(settings.db_path)
    try:
        profiles = load_profiles(settings.config_dir)
        window = store.observation_window(conn)

        for profile in profiles:
            color = next((s for s in profile.soft if s.kind == "color"), None)
            miles = next(
                (s for s in profile.soft if s.kind == "numeric" and s.attr == "miles"), None
            )
            preferred = color.preferred if color else []
            cap = miles_cap if miles_cap is not None else (int(miles.hard) if miles else 60000)

            typer.echo("")
            typer.echo(f"{profile.name}  ({profile.model_key})")
            pref_txt = ", ".join(preferred) if preferred else "(none configured)"
            typer.echo(
                f"  unicorn = ranked + color in [{pref_txt}] + miles <= {cap:,} "
                f"+ bonus >= {min_bonus:g}"
            )
            if not preferred:
                typer.echo("  no preferred colors in profile — nothing qualifies.")
                continue

            rows = store.unicorn_arrivals(conn, profile.model_key, preferred, min_bonus, cap, since)
            by_month = store.unicorn_rate_by_month(rows)
            if not rows:
                typer.echo("  no unicorns in the current data for this window.")
                continue

            typer.echo(f"  {'month':<9} {'cars':>4} {'events':>7}   states")
            for mo in by_month:
                st = ", ".join(mo.states) if mo.states else "-"
                typer.echo(f"  {mo.month:<9} {mo.cars:>4} {mo.events:>7}   {st}")
            events = sum(mo.events for mo in by_month)
            typer.echo(f"  total: {len(rows)} cars across {events} distinct arrival events")

            if cars:
                typer.echo("")
                for u in rows:
                    flags = (" [dismissed]" if u.dismissed else "") + (
                        " [favorited]" if u.favorited else ""
                    )
                    paint = f" {u.paint_name.title()}" if u.paint_name else ""
                    dealer = u.dealer or "?"
                    loc = f", {u.state}" if u.state else ""
                    price = f"${u.price:,.0f}" if u.price is not None else "?"
                    mi = f"{u.miles:,}mi" if u.miles is not None else "?mi"
                    typer.echo(
                        f"    {u.first_seen[:10]}  {(u.color_family or '?').title():<7}{paint}"
                        f"  {mi}  {price}  +{u.highlight_bonus:g}  {dealer}{loc}  {u.vin}{flags}"
                    )

        typer.echo("")
        if window:
            typer.echo(
                f"observation window: {window['first'][:10]} -> {window['last'][:10]}  "
                f"({window['total']:,} listings)"
            )
            if window["busiest_day_count"] > window["total"] * 0.2:
                from datetime import date, timedelta

                try:
                    day_after = (
                        date.fromisoformat(window["busiest_day"]) + timedelta(days=1)
                    ).isoformat()
                except ValueError:
                    day_after = window["busiest_day"]
                typer.echo(
                    f"  NOTE: {window['busiest_day_count']:,} listings share first_seen "
                    f"{window['busiest_day']} -- that's a bulk backfill, not observed arrivals. "
                    f"For a real rate, let the sync run and read arrivals AFTER "
                    f"{window['busiest_day']} (pass --since {day_after})."
                )
            typer.echo(
                "  'events' collapses one dealer listing a batch on one day into a single "
                "arrival opportunity -- trust it over raw 'cars' for rate estimates."
            )
    finally:
        conn.close()


if __name__ == "__main__":
    app()
