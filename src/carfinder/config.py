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
    marketcheck_api_key: str | None
    monthly_call_cap_autodev: int
    monthly_ai_budget_usd: float
    monthly_call_cap_gm: int
    monthly_call_cap_buildsheet: int
    monthly_call_cap_marketcheck: int
    stickers_dir: Path
    config_dir: Path
    notify_webhook_url: str | None
    notify_threshold: float
    # Distances on the UI are measured from here (zip centroid).
    home_zip: str = "77498"
    # partsdeal (BMW/Toyota VIN decoders): free/unofficial — politeness
    # backstop per make, mirroring the gmpartsgiant cap. Defaulted so
    # explicit Settings(...) construction in tests stays stable.
    monthly_call_cap_partsdeal: int = 500


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings, reading .env at the repo root first (if present)."""
    _load_dotenv(env_file if env_file is not None else _REPO_ROOT / ".env")
    # `or default` (not just .get default) so an env var set to "" — e.g. a
    # blank .env placeholder passed through docker's env_file — falls back
    # instead of producing Path("") / int("").
    return Settings(
        db_path=Path(os.environ.get("CARFINDER_DB_PATH") or "data/carfinder.sqlite"),
        autodev_api_key=os.environ.get("AUTODEV_API_KEY") or None,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        marketcheck_api_key=os.environ.get("MARKETCHECK_API_KEY") or None,
        monthly_call_cap_autodev=int(os.environ.get("MONTHLY_CALL_CAP_AUTODEV") or "800"),
        monthly_ai_budget_usd=float(os.environ.get("MONTHLY_AI_BUDGET_USD") or "2"),
        monthly_call_cap_gm=int(os.environ.get("MONTHLY_CALL_CAP_GM") or "1000"),
        monthly_call_cap_buildsheet=int(os.environ.get("MONTHLY_CALL_CAP_BUILDSHEET") or "1000"),
        # Free tier = 500 calls/mo; cap at 400 to leave a buffer (mirrors the
        # Auto.dev 1,000 -> 800 pattern). Enforced by BudgetGuard.
        monthly_call_cap_marketcheck=int(os.environ.get("MONTHLY_CALL_CAP_MARKETCHECK") or "400"),
        # partsdeal (BMW/Toyota VIN decoders): free/unofficial — politeness
        # backstop per make, mirroring the gmpartsgiant cap.
        monthly_call_cap_partsdeal=int(os.environ.get("MONTHLY_CALL_CAP_PARTSDEAL") or "500"),
        stickers_dir=Path(os.environ.get("CARFINDER_STICKERS_DIR") or "stickers"),
        config_dir=Path(os.environ.get("CARFINDER_CONFIG_DIR") or "config"),
        notify_webhook_url=os.environ.get("NOTIFY_WEBHOOK_URL") or None,
        notify_threshold=float(os.environ.get("NOTIFY_THRESHOLD") or "85"),
        home_zip=os.environ.get("CARFINDER_HOME_ZIP") or "77498",
    )
