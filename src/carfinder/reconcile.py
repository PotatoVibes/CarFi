"""Cross-source reconciliation with explicit precedence + a conflict log.

Precedence (highest first):
    window sticker  >  OEM build data  >  listing structured  >  listing free-text

For each attribute the highest-precedence source that has a value wins; when
two sources disagree the disagreement is recorded as a Conflict and surfaced
(never silently resolved). Build data currently arrives demoted to a *hint*
(claimed provenance) — see docs/build_data_evaluation.md — because we have no
agreement evidence on the Free plan; flip that once verified on a paid plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import colors
from .models import Provenance
from .oem import BuildFacts
from .sticker_parser import ParsedSticker

# Comparable scalar attributes (options handled separately as sets).
SCALAR_FIELDS = ("year", "make", "model", "trim", "msrp", "paint_rpo", "exterior")

# Lower rank = higher precedence.
SOURCE_RANK = {"sticker": 0, "build": 1, "listing": 2, "listing_text": 3}


@dataclass
class Facts:
    """Normalized, comparable facts from a single source."""

    source: str
    provenance: Provenance
    year: int | None = None
    make: str | None = None
    model: str | None = None
    trim: str | None = None
    msrp: float | None = None
    paint_rpo: str | None = None
    exterior: str | None = None
    options: set[str] = field(default_factory=set)


@dataclass
class Resolved:
    field: str
    value: Any
    source: str
    provenance: Provenance


@dataclass
class Conflict:
    field: str
    winner: str
    values: list[tuple[str, Any]]  # (source, value) pairs that disagree


@dataclass
class ReconcileResult:
    resolved: dict[str, Resolved]
    conflicts: list[Conflict]
    options: set[str]  # union of options across sources
    option_diffs: dict[str, set[str]]  # source -> codes unique to it vs the others


def _norm(v: Any) -> Any:
    return v.strip().upper() if isinstance(v, str) else v


def _rank(source: str) -> int:
    return SOURCE_RANK.get(source, 99)


def reconcile(facts_list: list[Facts]) -> ReconcileResult:
    """Merge facts by precedence; record every disagreement."""
    ordered = sorted(facts_list, key=lambda f: _rank(f.source))
    resolved: dict[str, Resolved] = {}
    conflicts: list[Conflict] = []

    for fld in SCALAR_FIELDS:
        present = [(f.source, getattr(f, fld), f.provenance) for f in ordered]
        present = [(s, v, p) for s, v, p in present if v is not None]
        if not present:
            continue
        winner_source, winner_value, winner_prov = present[0]
        resolved[fld] = Resolved(fld, winner_value, winner_source, winner_prov)

        distinct = {_norm(v) for _, v, _ in present}
        if len(distinct) > 1:
            conflicts.append(
                Conflict(field=fld, winner=winner_source, values=[(s, v) for s, v, _ in present])
            )

    # Options: union across sources; note codes unique to each source that
    # carries options (sticker/build). Symmetric differences among *verified*
    # option sources are the interesting signal.
    option_sources = {f.source: f.options for f in ordered if f.options}
    union: set[str] = set()
    for opts in option_sources.values():
        union |= opts
    option_diffs: dict[str, set[str]] = {}
    for src, opts in option_sources.items():
        others: set[str] = set()
        for other_src, other_opts in option_sources.items():
            if other_src != src:
                others |= other_opts
        unique = opts - others
        if unique:
            option_diffs[src] = unique

    return ReconcileResult(resolved, conflicts, union, option_diffs)


# --- source adapters -------------------------------------------------------


def facts_from_sticker(parsed: ParsedSticker, model_key: str) -> Facts:
    paint = next((c for c in parsed.options if c in colors.paint_codes(model_key)), None)
    return Facts(
        source="sticker",
        provenance=Provenance.verified,
        year=parsed.year,
        make=parsed.maker,
        msrp=parsed.msrp,
        paint_rpo=paint,
        exterior=parsed.exterior,
        options=set(parsed.options),
    )


def facts_from_build(build: BuildFacts, provenance: Provenance = Provenance.claimed) -> Facts:
    """Build data starts as a hint (claimed) until agreement is verified on a
    paid plan; pass provenance=verified to promote it once confirmed."""
    return Facts(
        source="build",
        provenance=provenance if build.available else Provenance.unknown,
        year=build.year,
        make=build.make,
        model=build.model,
        trim=build.trim,
        msrp=build.msrp,
        paint_rpo=build.paint_rpo,
        exterior=build.paint_desc,
        options=set(build.options),
    )


def facts_from_listing(
    year: int | None = None,
    make: str | None = None,
    model: str | None = None,
    trim: str | None = None,
    color: str | None = None,
) -> Facts:
    return Facts(
        source="listing",
        provenance=Provenance.claimed,
        year=year,
        make=make,
        model=model,
        trim=trim,
        exterior=color,
    )


# --- cross-reference report (build vs sticker) -----------------------------


@dataclass
class FieldComparison:
    field: str
    sticker: Any
    build: Any
    agree: bool


@dataclass
class CrossReference:
    vin: str | None
    build_available: bool
    fields: list[FieldComparison]
    sticker_only_options: set[str]
    build_only_options: set[str]
    shared_options: set[str]

    @property
    def agrees(self) -> bool:
        """All comparable scalar fields agree and no verified option disagreements."""
        return (
            self.build_available
            and all(f.agree for f in self.fields)
            and not self.sticker_only_options
            and not self.build_only_options
        )


def cross_reference(
    sticker: ParsedSticker, build: BuildFacts, model_key: str, vin: str | None = None
) -> CrossReference:
    """Field-by-field comparison of a window sticker vs OEM build data."""
    s = facts_from_sticker(sticker, model_key)
    b = facts_from_build(build)
    fields: list[FieldComparison] = []
    for fld in SCALAR_FIELDS:
        sv, bv = getattr(s, fld), getattr(b, fld)
        # Only compare where both sources have a value.
        agree = sv is None or bv is None or _norm(sv) == _norm(bv)
        fields.append(FieldComparison(fld, sv, bv, agree))
    return CrossReference(
        vin=vin or sticker.vin,
        build_available=build.available,
        fields=fields,
        sticker_only_options=s.options - b.options if build.available else set(),
        build_only_options=b.options - s.options if build.available else set(),
        shared_options=s.options & b.options,
    )
