"""Reconciler + cross-reference tests (offline)."""

from __future__ import annotations

from carfinder.models import Provenance
from carfinder.oem import BuildFacts
from carfinder.reconcile import (
    Facts,
    cross_reference,
    facts_from_build,
    facts_from_listing,
    facts_from_sticker,
    reconcile,
)
from carfinder.sticker_parser import ParsedSticker


def _sticker(**kw):
    return ParsedSticker(**kw)


# --- precedence ------------------------------------------------------------


def test_sticker_wins_over_listing_and_logs_conflict():
    facts = [
        facts_from_listing(year=2022, make="Chevrolet"),
        Facts(source="sticker", provenance=Provenance.verified, year=2023, make="Chevrolet"),
    ]
    r = reconcile(facts)
    assert r.resolved["year"].value == 2023  # sticker wins regardless of input order
    assert r.resolved["year"].source == "sticker"
    assert r.resolved["year"].provenance == Provenance.verified
    conflict = next(c for c in r.conflicts if c.field == "year")
    assert conflict.winner == "sticker"
    assert ("listing", 2022) in conflict.values and ("sticker", 2023) in conflict.values


def test_no_conflict_when_sources_agree():
    facts = [
        facts_from_listing(year=2022, make="Cadillac"),
        Facts(source="sticker", provenance=Provenance.verified, year=2022, make="Cadillac"),
    ]
    r = reconcile(facts)
    assert r.conflicts == []
    assert r.resolved["make"].value == "Cadillac"


def test_build_sits_between_sticker_and_listing():
    facts = [
        facts_from_listing(trim="Base"),
        facts_from_build(
            BuildFacts(trim="Blackwing", available=True), provenance=Provenance.verified
        ),
    ]
    r = reconcile(facts)
    assert r.resolved["trim"].value == "Blackwing"
    assert r.resolved["trim"].source == "build"


def test_options_union_and_diffs():
    facts = [
        Facts(source="sticker", provenance=Provenance.verified, options={"A", "B", "C"}),
        Facts(source="build", provenance=Provenance.verified, options={"B", "C", "D"}),
    ]
    r = reconcile(facts)
    assert r.options == {"A", "B", "C", "D"}
    assert r.option_diffs["sticker"] == {"A"}
    assert r.option_diffs["build"] == {"D"}


def test_string_comparison_is_case_insensitive():
    facts = [
        facts_from_listing(make="cadillac"),
        Facts(source="sticker", provenance=Provenance.verified, make="CADILLAC"),
    ]
    assert reconcile(facts).conflicts == []


# --- source adapters -------------------------------------------------------


def test_facts_from_sticker_resolves_paint_from_options():
    parsed = _sticker(
        year=2022,
        maker="CADILLAC",
        msrp=70345.0,
        exterior="BLACK RAVEN",
        options={"GBA", "FE4", "KU9"},
    )
    f = facts_from_sticker(parsed, "ct4v_blackwing")
    assert f.source == "sticker" and f.provenance == Provenance.verified
    assert f.paint_rpo == "GBA"  # GBA is in the CT4-V paint table


def test_facts_from_build_defaults_to_claimed_hint():
    f = facts_from_build(BuildFacts(year=2022, available=True))
    assert f.provenance == Provenance.claimed  # demoted until verified on a paid plan
    unavailable = facts_from_build(BuildFacts(available=False))
    assert unavailable.provenance == Provenance.unknown


# --- cross-reference -------------------------------------------------------


def test_cross_reference_agreement():
    parsed = _sticker(
        year=2022, maker="Cadillac", msrp=70345.0, exterior="Black Raven", options={"GBA", "FE4"}
    )
    build = BuildFacts(
        year=2022,
        make="Cadillac",
        msrp=70345.0,
        paint_rpo="GBA",
        paint_desc="Black Raven",
        options={"GBA", "FE4"},
        available=True,
    )
    xr = cross_reference(parsed, build, "ct4v_blackwing")
    assert xr.build_available
    assert all(f.agree for f in xr.fields)
    assert xr.agrees is True
    assert xr.shared_options == {"GBA", "FE4"}


def test_cross_reference_flags_year_disagreement():
    parsed = _sticker(year=2022, maker="Cadillac", options={"GBA"})
    build = BuildFacts(year=2021, make="Cadillac", options={"GBA"}, available=True)
    xr = cross_reference(parsed, build, "ct4v_blackwing")
    year_cmp = next(f for f in xr.fields if f.field == "year")
    assert year_cmp.agree is False
    assert xr.agrees is False


def test_cross_reference_unavailable_build():
    parsed = _sticker(year=2022, maker="Cadillac", options={"GBA"})
    xr = cross_reference(parsed, BuildFacts(available=False), "ct4v_blackwing")
    assert xr.build_available is False
    assert xr.agrees is False  # can't agree with nothing
