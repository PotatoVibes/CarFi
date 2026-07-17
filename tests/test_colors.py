"""Color-family resolution tests."""

from __future__ import annotations

from carfinder import colors
from carfinder.colors import ColorFamily


def test_rpo_lookup_per_model():
    assert colors.family_from_rpo("c8_stingray", "GM0") == ColorFamily.blue
    assert colors.family_from_rpo("c8_stingray", "gkz") == ColorFamily.red  # case-insensitive
    assert colors.family_from_rpo("ct4v_blackwing", "GBA") == ColorFamily.black


def test_corpus_audited_paint_codes():
    # 2026-07-16 audit: sticker exterior line x paint G-code, zero conflicts.
    assert colors.family_from_rpo("ct4v_blackwing", "GRW") == ColorFamily.silver  # Rift
    assert colors.family_from_rpo("ct4v_blackwing", "GAI") == ColorFamily.blue  # Deep Space
    assert colors.family_from_rpo("ct4v_blackwing", "GNW") == ColorFamily.black  # Maverick Noir
    assert colors.family_from_rpo("ct4v_blackwing", "GKK") == ColorFamily.blue  # Wave
    assert colors.family_from_rpo("c8_stingray", "GXL") == ColorFamily.other  # Hysteria Purple


def test_gm_marketing_names_resolve_via_text():
    assert colors.family_from_text("RIFT METALLIC") == ColorFamily.silver
    assert colors.family_from_text("Deep Space Metallic") == ColorFamily.blue
    assert colors.family_from_text("Maverick Noir Frost") == ColorFamily.black
    assert colors.family_from_text("Typhoon Metallic") == ColorFamily.gray
    assert colors.family_from_text("Hysteria Purple") == ColorFamily.other


def test_rpo_unknown_or_missing():
    assert colors.family_from_rpo("c8_stingray", "ZZZ") is None
    assert colors.family_from_rpo("c8_stingray", None) is None
    assert colors.family_from_rpo("no_such_model", "GM0") is None


def test_text_classifier_keywords():
    assert colors.family_from_text("Rapid Blue Metallic") == ColorFamily.blue
    assert colors.family_from_text("Torch Red") == ColorFamily.red
    assert colors.family_from_text("Carbon Flash Metallic") == ColorFamily.black  # specific first
    assert colors.family_from_text("Hypersonic Gray") == ColorFamily.gray
    assert colors.family_from_text(None) is None
    assert colors.family_from_text("Chartreuse") is None
