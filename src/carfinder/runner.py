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


def _selected(config_dir: str | Path, only_model: str | None) -> list[Profile]:
    """All profiles, or just the one matching `only_model` (by model_key)."""
    profiles = load_profiles(config_dir)
    if only_model is None:
        return profiles
    selected = [p for p in profiles if p.model_key == only_model]
    if not selected:
        known = ", ".join(p.model_key for p in profiles)
        raise ValueError(f"unknown model {only_model!r} (have: {known})")
    return selected


def run_sync(
    conn: sqlite3.Connection,
    settings: Settings,
    current_year: int,
    mode: str = "incremental",
    config_dir: str | Path = "config",
    progress: Callable[[str], None] | None = None,
    only_model: str | None = None,
    max_pages: int | None = None,
) -> dict[str, PipelineStats]:
    """Build a live pipeline and run every profile (or just `only_model`).
    Returns {model_key: stats}."""
    pipeline = build_pipeline(conn, settings, current_year)
    results: dict[str, PipelineStats] = {}
    for profile in _selected(config_dir, only_model):
        results[profile.model_key] = pipeline.run(
            profile, mode=mode, progress=_prefixed(progress, profile.name), max_pages=max_pages
        )
    return results


def run_rescore(
    conn: sqlite3.Connection,
    settings: Settings,
    current_year: int,
    config_dir: str | Path = "config",
    progress: Callable[[str], None] | None = None,
    only_model: str | None = None,
) -> dict[str, PipelineStats]:
    """Re-score every known vehicle against the current profiles — no sync, no
    Auto.dev key needed, ~zero network (cached sticker/AI). Use after editing a
    profile to re-rank cars already in the database."""
    pipeline = build_rescore_pipeline(conn, settings, current_year)
    results: dict[str, PipelineStats] = {}
    for profile in _selected(config_dir, only_model):
        results[profile.model_key] = pipeline.rescore(
            profile, progress=_prefixed(progress, profile.name)
        )
    return results
