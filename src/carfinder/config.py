"""Runtime settings, loaded from environment (and optionally a .env file).

Keys and caps live in the environment only — never hard-coded, never committed.
A tiny built-in .env reader keeps us dependency-free for this.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Repo root = two levels up from this file (src/carfinder/config.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a .env file without overriding existing vars."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, val)


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of runtime configuration."""

    db_path: Path
    autodev_api_key: str | None
    anthropic_api_key: str | None
    monthly_call_cap_autodev: int
    monthly_ai_budget_usd: float


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings, reading .env at the repo root first (if present)."""
    _load_dotenv(env_file if env_file is not None else _REPO_ROOT / ".env")
    return Settings(
        db_path=Path(os.environ.get("CARFINDER_DB_PATH", "data/carfinder.sqlite")),
        autodev_api_key=os.environ.get("AUTODEV_API_KEY") or None,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        monthly_call_cap_autodev=int(os.environ.get("MONTHLY_CALL_CAP_AUTODEV", "800")),
        monthly_ai_budget_usd=float(os.environ.get("MONTHLY_AI_BUDGET_USD", "2")),
    )
