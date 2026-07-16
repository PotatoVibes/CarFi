"""Load and validate requirement profiles from YAML."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import Profile


class ProfileError(ValueError):
    """Raised when a profile file is missing, unparseable, or invalid."""


def load_profile_str(text: str) -> Profile:
    """Parse + validate a profile from a YAML string."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProfileError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError("profile must be a YAML mapping at the top level")
    try:
        return Profile.model_validate(data)
    except ValidationError as exc:
        raise ProfileError(f"profile failed validation:\n{exc}") from exc


def load_profile(path: str | Path) -> Profile:
    """Load + validate a profile from a YAML file."""
    p = Path(path)
    if not p.exists():
        raise ProfileError(f"profile not found: {p}")
    return load_profile_str(p.read_text(encoding="utf-8"))


def dump_profile(profile: Profile) -> str:
    """Serialize a profile back to YAML (round-trip helper)."""
    return yaml.safe_dump(
        profile.model_dump(mode="json", exclude_defaults=False),
        sort_keys=False,
        allow_unicode=True,
    )
