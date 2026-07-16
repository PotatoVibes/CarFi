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
    """Render a usage/cap number: dollars for cost, plain count otherwise."""
    return f"${usage_value:,.2f}" if kind == COST else f"{usage_value:,.0f}"


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


if __name__ == "__main__":
    app()
