"""Requirement-profile schema, scoring input, and scoring output models.

Everything here is pure data + validation. No I/O. The scorer (scorer.py)
consumes a `Profile` and a `Vehicle` and returns a `ScoreResult`.

Provenance discipline (the core rule of this tool):
  * verified  — from the window sticker / OEM build data (ground truth)
  * claimed   — from dealer listing text/fields (unconfirmed)
  * unknown   — no evidence either way
A claimed match can raise a soft score but can NEVER satisfy a hard requirement.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Provenance(StrEnum):
    verified = "verified"
    claimed = "claimed"
    unknown = "unknown"


# --- Matcher ---------------------------------------------------------------


class Matcher(BaseModel):
    """Matches a vehicle option via a VIN-decoded fact, RPO code(s), and/or a
    sticker-text regex.

    VIN facts are checked first (definitive physical identity — verified both
    ways: a fact outside vin_any is a verified "no"). Then RPO / text, where a
    match on either counts (OR semantics). RPO codes are the precise signal;
    the text regex is the drift-proof safety net.
    """

    model_config = ConfigDict(extra="forbid")

    # VIN fact check: vehicle.vin_facts[vin_attr] must be one of vin_any.
    vin_attr: str | None = None  # "model" | "trim" | "body" (see vin.py)
    vin_any: list[str] = Field(default_factory=list)
    rpo: list[str] = Field(default_factory=list)
    sticker_text: str | None = None

    @field_validator("rpo", mode="before")
    @classmethod
    def _coerce_rpo(cls, v: object) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return list(v)  # type: ignore[arg-type]

    @field_validator("rpo")
    @classmethod
    def _normalize_rpo(cls, v: list[str]) -> list[str]:
        return [c.strip().upper() for c in v if c.strip()]

    @field_validator("sticker_text")
    @classmethod
    def _validate_regex(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            re.compile(v, re.IGNORECASE)
        except re.error as exc:  # malformed config → reject at load time
            raise ValueError(f"invalid sticker_text regex {v!r}: {exc}") from exc
        return v

    @field_validator("vin_any")
    @classmethod
    def _normalize_vin_any(cls, v: list[str]) -> list[str]:
        return [c.strip().upper() for c in v if c.strip()]

    @model_validator(mode="after")
    def _require_something(self) -> Matcher:
        if not self.rpo and not self.sticker_text and not self.vin_attr:
            raise ValueError("matcher must specify vin_attr, rpo, and/or sticker_text")
        if (self.vin_attr is None) != (not self.vin_any):
            raise ValueError("vin_attr and vin_any must be provided together")
        return self

    def compiled(self) -> re.Pattern[str] | None:
        return re.compile(self.sticker_text, re.IGNORECASE) if self.sticker_text else None


# --- Constraints -----------------------------------------------------------


class HardConstraint(BaseModel):
    """A verified-only pass/fail gate.

    kind:
      * require   — the option must be present (verified).
      * forbid    — the option must be absent (e.g., exclude Z06/E-Ray).
      * year_min  — model year must be >= min_year.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    kind: Literal["require", "forbid", "year_min"] = "require"
    matcher: Matcher | None = None
    min_year: int | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> HardConstraint:
        if self.kind in ("require", "forbid") and self.matcher is None:
            raise ValueError(f"hard constraint {self.key!r} ({self.kind}) needs a matcher")
        if self.kind == "year_min" and self.min_year is None:
            raise ValueError(f"hard constraint {self.key!r} (year_min) needs min_year")
        return self


class SoftConstraint(BaseModel):
    """A weighted 0–100 dimension with a linear penalty curve.

    kind:
      * numeric — score by `attr` (miles/price) against ideal/hard bounds.
      * color   — score by resolved color family (preferred/penalized/neutral).
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    kind: Literal["numeric", "color"]
    weight: float = Field(gt=0)

    # numeric
    attr: Literal["miles", "price"] | None = None
    ideal: float | None = None
    hard: float | None = None
    direction: Literal["lower_better", "higher_better"] = "lower_better"
    exclude_beyond_hard: bool = False

    # color
    preferred: list[str] = Field(default_factory=list)
    penalized: list[str] = Field(default_factory=list)
    neutral_score: float = 50.0

    @model_validator(mode="after")
    def _check_shape(self) -> SoftConstraint:
        if self.kind == "numeric":
            if self.attr is None or self.ideal is None or self.hard is None:
                raise ValueError(f"numeric soft constraint {self.key!r} needs attr, ideal, hard")
            if self.ideal == self.hard:
                raise ValueError(f"soft constraint {self.key!r}: ideal and hard must differ")
        if self.kind == "color":
            from .colors import ColorFamily

            valid = {f.value for f in ColorFamily}
            unknown = (set(self.preferred) | set(self.penalized)) - valid
            if unknown:
                raise ValueError(
                    f"color soft constraint {self.key!r} has unknown families {sorted(unknown)}"
                )
        return self


class Highlight(BaseModel):
    """A bonus badge. Bonus 0–10; the scorer caps the total contribution."""

    model_config = ConfigDict(extra="forbid")

    label: str
    bonus: float = Field(ge=0, le=10)
    matcher: Matcher


class ListingQuery(BaseModel):
    """Server-side prefilter hints for a listing source.

    Bounds are deliberately WIDER than the scorer's hard bounds so fuzzy-band
    cars aren't excluded server-side — the deterministic scorer, not the API,
    makes the final call. year_max/price_max default to derived values when
    omitted (see query.build_listing_params).
    """

    model_config = ConfigDict(extra="forbid")

    make: str
    model: str  # Auto.dev accepts comma-separated values, e.g. "CT4,CT4-V"
    trim: str | None = None  # Auto.dev's normalized trim, e.g. "V Blackwing"
    year_min: int | None = None  # else derived from the year_min hard constraint
    year_max: int | None = None  # else current year + 1
    price_max: float | None = None  # else derived from the price soft hard bound
    price_margin: float = Field(default=0.08, ge=0)  # widen a derived price ceiling
    # Marketcheck's model taxonomy differs from Auto.dev's (no comma-lists, no
    # separate trim filter; Blackwings are likely their own model). When set,
    # this overrides `model` for Marketcheck queries only. Tune in YAML if a
    # sync warns about zero Marketcheck results.
    marketcheck_model: str | None = None


class Profile(BaseModel):
    """A full requirement profile for one model."""

    model_config = ConfigDict(extra="forbid")

    name: str
    model_key: str
    query: ListingQuery | None = None
    hard: list[HardConstraint] = Field(default_factory=list)
    soft: list[SoftConstraint] = Field(default_factory=list)
    highlights: list[Highlight] = Field(default_factory=list)
    # An unknown-provenance hard requirement routes to "needs review" rather
    # than a silent rejection. Flip to False to treat unknown as failure.
    review_on_unknown_hard: bool = True
    # False parks this model: every all-model run (the global Sync / Full pass /
    # Rescore buttons, the scheduled jobs, `carfinder sync` with no --model)
    # skips it, so it stops spending API calls and stops re-ranking. Naming the
    # model explicitly always runs it — the model page's own buttons and a
    # per-car Refresh ignore this flag. Toggled from the model page.
    auto_sync: bool = True


# --- Scoring input ---------------------------------------------------------


class Vehicle(BaseModel):
    """Normalized, provenance-tagged view of one car, ready for scoring.

    Populated by later milestones (sticker parser, listing sync). Tests build
    it directly.
    """

    model_config = ConfigDict(extra="forbid")

    vin: str | None = None
    year: int | None = None
    miles: int | None = None
    price: float | None = None

    # Verified evidence (window sticker / OEM build data).
    verified_rpo: set[str] = Field(default_factory=set)
    verified_text: str = ""
    # VIN-decoded facts (vin.decode): definitive identity, verified provenance.
    vin_facts: dict[str, str] = Field(default_factory=dict)
    # True once an authoritative, complete sticker was parsed: then the absence
    # of an option in the verified evidence is a definitive "no".
    sticker_complete: bool = False

    # Claimed evidence (dealer listing free-text / structured fields).
    claimed_text: str = ""
    # Per-requirement claims from the AI extractor (M5), keyed by constraint key
    # / highlight label: "claimed" | "denied" | "not_mentioned". Always claimed
    # provenance — can raise a soft score, never satisfy a hard requirement.
    claimed_features: dict[str, str] = Field(default_factory=dict)

    # Paint / color.
    paint_rpo: str | None = None
    color_text: str | None = None

    @field_validator("verified_rpo", mode="before")
    @classmethod
    def _upper_rpo(cls, v: object) -> object:
        if isinstance(v, (set, list, tuple)):
            return {str(c).strip().upper() for c in v}
        return v

    @field_validator("paint_rpo")
    @classmethod
    def _upper_paint(cls, v: str | None) -> str | None:
        return v.strip().upper() if v else v


# --- Scoring output --------------------------------------------------------

Verdict = Literal["excluded", "needs_review", "ranked"]
Group = Literal["hard", "soft", "highlight"]
Status = Literal["pass", "fail", "needs_review", "scored", "badge"]


class RequirementResult(BaseModel):
    key: str
    label: str
    group: Group
    status: Status
    provenance: Provenance
    detail: str = ""
    matched: bool | None = None
    dim_score: float | None = None  # 0–100, soft only
    weight: float | None = None
    bonus: float | None = None  # highlight contribution actually applied
    # Set on the color soft dimension only: the resolved family + the actual
    # paint name, so the UI shows one consistent color everywhere.
    color_family: str | None = None
    paint_name: str | None = None


class ScoreResult(BaseModel):
    vin: str | None
    verdict: Verdict
    soft_score: float  # 0–100
    highlight_bonus: float  # 0–HIGHLIGHT_CAP
    total: float  # soft_score + highlight_bonus
    hard: list[RequirementResult] = Field(default_factory=list)
    soft: list[RequirementResult] = Field(default_factory=list)
    highlights: list[RequirementResult] = Field(default_factory=list)

    @property
    def failures(self) -> list[RequirementResult]:
        return [r for r in self.hard if r.status == "fail"]

    @property
    def reviews(self) -> list[RequirementResult]:
        return [r for r in self.hard if r.status == "needs_review"]
