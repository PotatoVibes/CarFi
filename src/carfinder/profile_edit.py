"""Guided profile edits for the config UI.

Every operation takes a Profile, returns a new validated Profile, and
save_profile() round-trips through the loader before touching disk — a bad
edit can never corrupt a config. The previous file is kept as .bak.

Note: saves emit generated YAML; hand-written comments in the file are not
preserved (git history and the .bak keep them).
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import HardConstraint, Highlight, Matcher, Profile, SoftConstraint
from .profiles import ProfileError, dump_profile, load_profile_str

_HEADER = (
    "# Managed by the Car Finder config UI (edits regenerate this file;\n"
    "# the previous version is kept alongside as .bak).\n"
)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:40] or "feature"


def _validated(profile: Profile) -> Profile:
    """Round-trip through the YAML loader so every edit obeys profile rules."""
    return load_profile_str(dump_profile(profile))


# --- feature operations (RPO-code based) -------------------------------------


def add_highlight(profile: Profile, code: str, description: str, bonus: float) -> Profile:
    p = remove_code(profile, code)  # replace any prior role for this code
    p.highlights.append(Highlight(label=description, bonus=bonus, matcher=Matcher(rpo=[code])))
    return _validated(p)


def add_hard(profile: Profile, code: str, description: str, kind: str) -> Profile:
    if kind not in ("require", "forbid"):
        raise ProfileError(f"unknown hard kind {kind!r}")
    p = remove_code(profile, code)
    p.hard.append(
        HardConstraint(
            key=_slug(description),
            label=description,
            kind=kind,  # type: ignore[arg-type]
            matcher=Matcher(rpo=[code]),
        )
    )
    return _validated(p)


def remove_code(profile: Profile, code: str) -> Profile:
    """Drop any hard constraint / highlight whose matcher targets this code."""
    code = code.strip().upper()
    p = profile.model_copy(deep=True)
    p.hard = [c for c in p.hard if c.matcher is None or code not in c.matcher.rpo]
    p.highlights = [h for h in p.highlights if code not in h.matcher.rpo]
    return _validated(p)


def remove_hard(profile: Profile, key: str) -> Profile:
    p = profile.model_copy(deep=True)
    p.hard = [c for c in p.hard if c.key != key]
    return _validated(p)


def remove_highlight(profile: Profile, label: str) -> Profile:
    p = profile.model_copy(deep=True)
    p.highlights = [h for h in p.highlights if h.label != label]
    return _validated(p)


# --- bounds / preferences -----------------------------------------------------


def set_year_min(profile: Profile, year: int) -> Profile:
    p = profile.model_copy(deep=True)
    for c in p.hard:
        if c.kind == "year_min":
            c.min_year = year
            return _validated(p)
    p.hard.append(
        HardConstraint(
            key=f"year_{year}_plus", label=f"{year} or newer", kind="year_min", min_year=year
        )
    )
    return _validated(p)


def set_numeric(
    profile: Profile,
    attr: str,  # "price" | "miles"
    ideal: float,
    hard: float,
    weight: float,
    exclude_beyond_hard: bool,
) -> Profile:
    p = profile.model_copy(deep=True)
    for c in p.soft:
        if c.kind == "numeric" and c.attr == attr:
            c.ideal, c.hard, c.weight = ideal, hard, weight
            c.exclude_beyond_hard = exclude_beyond_hard
            return _validated(p)
    p.soft.append(
        SoftConstraint(
            key=attr,
            label=attr.title(),
            kind="numeric",
            attr=attr,  # type: ignore[arg-type]
            ideal=ideal,
            hard=hard,
            direction="lower_better",
            weight=weight,
            exclude_beyond_hard=exclude_beyond_hard,
        )
    )
    return _validated(p)


def set_colors(
    profile: Profile, preferred: list[str], penalized: list[str], weight: float
) -> Profile:
    p = profile.model_copy(deep=True)
    for c in p.soft:
        if c.kind == "color":
            c.preferred = preferred
            c.penalized = penalized
            c.weight = weight
            return _validated(p)
    p.soft.append(
        SoftConstraint(
            key="color",
            label="Color",
            kind="color",
            weight=weight,
            preferred=preferred,
            penalized=penalized,
        )
    )
    return _validated(p)


# --- persistence ---------------------------------------------------------------


def save_profile(path: str | Path, profile: Profile) -> None:
    """Validate, back up the current file, then write the new YAML."""
    path = Path(path)
    text = _HEADER + dump_profile(_validated(profile))
    load_profile_str(text)  # belt and braces: the exact bytes we write must load
    if path.exists():
        path.with_suffix(path.suffix + ".bak").write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    path.write_text(text, encoding="utf-8")
