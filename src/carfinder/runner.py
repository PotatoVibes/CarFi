"""Sync orchestration shared by the CLI and the web scheduler.

Loads every profile in the config directory and runs the pipeline over each.
Kept separate from the web app so `carfinder sync` works headless (Windows
Task Scheduler / cron) with no server running.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from .config import Settings
from .models import Profile
from .pipeline import PipelineStats, build_pipeline, build_rescore_pipeline
from .profiles import ProfileError, load_profile


def load_profiles(config_dir: str | Path) -> list[Profile]:
    """Load and validate every *.yaml profile in a directory (sorted)."""
    profiles: list[Profile] = []
    for path in sorted(Path(config_dir).glob("*.yaml")):
        try:
            profiles.append(load_profile(path))
        except ProfileError:
            raise
    return profiles


def _prefixed(progress: Callable[[str], None] | None, name: str):
    """Wrap a progress callback so messages carry the profile name."""
    if progress is None:
        return None
    return lambda msg: progress(f"{name}: {msg}")


def run_sync(
    conn: sqlite3.Connection,
    settings: Settings,
    current_year: int,
    mode: str = "incremental",
    config_dir: str | Path = "config",
    progress: Callable[[str], None] | None = None,
) -> dict[str, PipelineStats]:
    """Build a live pipeline and run every profile. Returns {model_key: stats}."""
    pipeline = build_pipeline(conn, settings, current_year)
    results: dict[str, PipelineStats] = {}
    for profile in load_profiles(config_dir):
        results[profile.model_key] = pipeline.run(
            profile, mode=mode, progress=_prefixed(progress, profile.name)
        )
    return results


def run_rescore(
    conn: sqlite3.Connection,
    settings: Settings,
    current_year: int,
    config_dir: str | Path = "config",
    progress: Callable[[str], None] | None = None,
) -> dict[str, PipelineStats]:
    """Re-score every known vehicle against the current profiles — no sync, no
    Auto.dev key needed, ~zero network (cached sticker/AI). Use after editing a
    profile to re-rank cars already in the database."""
    pipeline = build_rescore_pipeline(conn, settings, current_year)
    results: dict[str, PipelineStats] = {}
    for profile in load_profiles(config_dir):
        results[profile.model_key] = pipeline.rescore(
            profile, progress=_prefixed(progress, profile.name)
        )
    return results
