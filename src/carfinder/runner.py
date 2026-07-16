"""Sync orchestration shared by the CLI and the web scheduler.

Loads every profile in the config directory and runs the pipeline over each.
Kept separate from the web app so `carfinder sync` works headless (Windows
Task Scheduler / cron) with no server running.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import Settings
from .pipeline import PipelineStats, build_pipeline
from .profiles import ProfileError, load_profile
from .models import Profile


def load_profiles(config_dir: str | Path) -> list[Profile]:
    """Load and validate every *.yaml profile in a directory (sorted)."""
    profiles: list[Profile] = []
    for path in sorted(Path(config_dir).glob("*.yaml")):
        try:
            profiles.append(load_profile(path))
        except ProfileError:
            raise
    return profiles


def run_sync(
    conn: sqlite3.Connection,
    settings: Settings,
    current_year: int,
    mode: str = "incremental",
    config_dir: str | Path = "config",
) -> dict[str, PipelineStats]:
    """Build a live pipeline and run every profile. Returns {model_key: stats}."""
    pipeline = build_pipeline(conn, settings, current_year)
    results: dict[str, PipelineStats] = {}
    for profile in load_profiles(config_dir):
        results[profile.model_key] = pipeline.run(profile, mode=mode)
    return results
