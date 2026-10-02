"""Special-edition detection (editions.py): pure, offline.

VINs are real cars from the 2026-10-02 corpus audit (live DB snapshot +
public listings) unless marked synthetic. Each edition gets a verified
positive, its lookalike traps, and the scope gate that keeps codes and
phrases from leaking across model lines / model years.
"""

from __future__ import annotations

import pytest

from carfinder import editions
from carfinder.models import Provenance, Vehicle


def _detect(model_key: str, vin: str, **kw):
    return editions.detect(model_key, Vehicle(vin=vin, **kw))


def _key(model_key: str, vin: str, **kw) -> str | None:
    found = _detect(model_key, vin, **kw)
    return found.edition.key if found else None


# --- C8 Corvette Stingray ---------------------------------------------------


def test_c8_imsa_gtlm_by_zcr_code():
    # 2022 3LT convertible; ZCR on its gmpartsgiant build sheet.
    found = _detect("c8_stingray", "1G1YC3D47N5101788", verified_rpo={"ZCR", "Z51"})
    assert found.edition.key == "c8_imsa_gtlm_c8r"
    assert found.provenance == Provenance.verified
    assert "ZCR" in found.evidence


def test_c8_imsa_lookalike_options_without_zcr_are_not_an_edition():
    # Accelerate Yellow + Strike Yellow interior + yellow belts/calipers, all
    # individually orderable — paint and color content never trigger.
    assert (
        _key("c8_stingray", "1G1YC3D45N5124163", verified_rpo={"GD0", "4FD", "3M9", "J6E", "5ZZ"})
        is None
    )


def test_c8_imsa_sticker_line_and_listing_slug():
    assert (
        _key(
            "c8_stingray",
            "1G1YC2D49N5109840",
            verified_text="IMSA GTLM CHAMPIONSHIP C8.R EDITION 6,595.00",
        )
        == "c8_imsa_gtlm_c8r"
    )
    found = _detect(
        "c8_stingray",
        "1G1YC2D44N5102150",
        listing_text="https://www.example.com/used-2022-chevrolet-corvette-imsa-gtlm-championship-edition",
    )
    assert found.edition.key == "c8_imsa_gtlm_c8r"
    assert found.provenance == Provenance.claimed


def test_c8_imsa_text_never_fires_outside_2022():
    # A C8.R-rendered car cover on a 2023 is not the edition.
    assert (
        _key("c8_stingray", "1G1YC3D45P5101341", listing_text="C8.R Edition car cover included")
        is None
    )


def test_c8_70th_by_y70_code_and_by_vin_serial_block():
    found = _detect("c8_stingray", "1G1YC3D44P5500628", verified_rpo={"Y70", "G1W"})
    assert found.edition.key == "c8_70th_anniversary"
    assert found.provenance == Provenance.verified
    # No sticker, no build sheet: the 50xxxx serial block alone verifies it.
    found = _detect("c8_stingray", "1G1YC3D47P5501577")
    assert found.edition.key == "c8_70th_anniversary"
    assert found.provenance == Provenance.verified
    assert "serial" in found.evidence


def test_c8_70th_lookalikes():
    # $460 "70th Anniversary indoor car cover" accessory on a regular 2023.
    assert (
        _key(
            "c8_stingray",
            "1G1YC3D43P5119241",
            verified_text="70TH ANNIVERSARY INDOOR CAR COVER 460.00",
            sticker_complete=True,
        )
        is None
    )
    # Every 2023 carries 70th badging — a 2LT can't be the (3LT-only) edition,
    # whatever its listing says.
    assert _key("c8_stingray", "1G1YB3D40P5133200", listing_text="70th Anniversary Edition") is None
    # Feature-list prose that isn't an edition claim.
    assert (
        _key(
            "c8_stingray",
            "1G1YC3D41P5108000",  # synthetic 3LT, regular serial
            listing_text="70th Anniversary steering wheel and sill plates",
        )
        is None
    )
    # Z06 70th (pos 5 F) is the same package on a model this profile excludes.
    assert _key("c8_stingray", "1G1YF3D33P5700303", verified_rpo={"Y70"}) is None


def test_c8_70th_claimed_from_listing_heading():
    found = _detect(
        "c8_stingray",
        "1G1YC2D41P5108000",  # synthetic 3LT coupe, regular serial, no factory data
        listing_text="2023 Chevrolet Corvette Stingray 3LT 70th Anniversary Edition",
    )
    assert found.edition.key == "c8_70th_anniversary"
    assert found.provenance == Provenance.claimed


def test_c8_complete_sticker_overrides_a_listing_claim():
    # The factory document wins: no Y70 on a complete sticker = not an edition.
    assert (
        _key(
            "c8_stingray",
            "1G1YC2D41P5108000",
            verified_rpo={"FE3", "Z51"},
            sticker_complete=True,
            listing_text="70th Anniversary Edition",
        )
        is None
    )


def test_c8_stars_and_steel_is_2026_stingray_only():
    # Synthetic 2026 3LT carrying USA (the only corpus USA car is an E-Ray).
    assert _key("c8_stingray", "1G1YC3D46T5107093", verified_rpo={"USA"}) == "c8_stars_and_steel"
    assert _key("c8_stingray", "1G1YM3D49T5501121", verified_rpo={"USA"}) is None  # E-Ray
    assert _key("c8_stingray", "1G1YC3D46S5107093", verified_rpo={"USA"}) is None  # 2025
    assert (
        _key("c8_stingray", "1G1YC3D46T5107093", listing_text="corvette-stingray-stars-steel-ltd")
        == "c8_stars_and_steel"
    )


def test_c8_regular_car_is_not_an_edition():
    assert _key("c8_stingray", "1G1YB2D46R5110761", verified_rpo={"FE3", "Z51"}) is None


# --- CT4-V Blackwing --------------------------------------------------------


@pytest.mark.parametrize(
    ("vin", "code", "key"),
    [
        ("1G6DL5RP3P0511012", "ZLE", "ct4v_watkins_glen_imsa"),
        ("1G6DL5RP0P0566047", "ZLD", "ct4v_sebring_imsa"),
        ("1G6DL5RP2P0518033", "ZLG", "ct4v_road_atlanta_imsa"),
        ("1G6D65RP4R0561010", "ZLK", "ct4v_arrival"),
        ("1G6D65RP4R0562027", "ZLJ", "ct4v_impact"),
        ("1G6D65RP4R0563033", "ZLR", "ct4v_elevation"),
        ("1G6D75RP7S0510040", "ZLZ", "ct4v_petit_pataud"),
    ],
)
def test_blackwing_editions_by_code_and_by_vin_serial(vin, code, key):
    assert _key("ct4v_blackwing", vin, verified_rpo={code}) == key
    # The serial block (pos 12 '5' + trans + edition digit + plaque number)
    # identifies the same car with no factory data at all.
    found = _detect("ct4v_blackwing", vin)
    assert found.edition.key == key
    assert found.provenance == Provenance.verified


def test_blackwing_sticker_lines():
    cases = {
        "ONSTAR (R) SERVICES & WATKINS GLEN IMSA EDITION 19,200.00": "ct4v_watkins_glen_imsa",
        "HOTSPOT CAPABLE; SEBRING IMSA EDITION 22,500.00": "ct4v_sebring_imsa",
        "THE STANDARD VEHICLE PRICE SHOWN ROAD ATLANTA IMSA EDITION": "ct4v_road_atlanta_imsa",
        "STEERING WHEEL, HEATED CT4-V BLACKWING ARRIVAL EDITION 22,000.00": "ct4v_arrival",
        "STEERING WHEEL, HEATED CT4-V BLACKWING IMPACT EDITION 22,000.00": "ct4v_impact",
        # "EDITION" wraps to the next sticker line on the Elevation.
        "STEERING WHEEL, HEATED CT4-V BLACKWING ELEVATION 22,000.00": "ct4v_elevation",
        "[ZLZ] Petit Pataud Special Edition $25,000": "ct4v_petit_pataud",
    }
    year_for = {"ct4v_arrival": "R", "ct4v_impact": "R", "ct4v_elevation": "R"}
    for text, key in cases.items():
        year = year_for.get(key, "S" if key == "ct4v_petit_pataud" else "P")
        vin = f"1G6D65RP0{year}0460100"  # synthetic regular serial: text alone decides
        assert _key("ct4v_blackwing", vin, verified_text=text) == key, text


def test_blackwing_lookalikes():
    # Standard 2024 commemorative content on every Blackwing.
    assert (
        _key(
            "ct4v_blackwing",
            "1G6D65RP0R0460174",
            verified_text="20TH ANNIVERSARY OF V-SERIES COMMEMORATIVE CONTENT",
            sticker_complete=True,
        )
        is None
    )
    # "CADILLAC EDITION FIRST AID" dealer accessory; edition paint on a
    # regular car (Electric Blue GMO is on 8 regular 2023s).
    assert (
        _key(
            "ct4v_blackwing",
            "1G6DL5RP1P0460531",
            verified_rpo={"GMO", "CFZ", "CFV"},
            verified_text="CADILLAC EDITION FIRST AID 55.00",
            sticker_complete=True,
        )
        is None
    )
    # ZL* codes are per-model-year "designer theme" slots: ZLE on a 2024 is
    # not the 2023 Watkins Glen.
    assert _key("ct4v_blackwing", "1G6D65RP0R0460100", verified_rpo={"ZLE"}) is None
    # Base CT4 (pos 8 K) from Alan Jay Cadillac in Sebring, FL.
    assert _key("ct4v_blackwing", "1G6DB5RK7T0117997", listing_text="for-sale-sebring-fl") is None


def test_blackwing_serial_block_respects_plaque_counts():
    # Mondrian editions were numbered 001-066; #066 is real, #067 can't be.
    assert _key("ct4v_blackwing", "1G6D75RP4R0513066") == "ct4v_elevation"
    assert _key("ct4v_blackwing", "1G6D75RP4R0513067") is None  # synthetic


def test_blackwing_has_no_claimed_path():
    # The serial block is complete: a regular-serial car's listing claim
    # could only be wrong ("CT4-V Blackwing, Arrival 10/14" is shipping text).
    assert (
        _key(
            "ct4v_blackwing",
            "1G6DL5RP6P0410174",
            listing_text="CT4-V Blackwing Watkins Glen IMSA Edition",
        )
        is None
    )


# --- BMW M2 -----------------------------------------------------------------


def test_m2_turbo_design_edition_is_claimed_only():
    # Real TDE (Cars & Bids); the VIN can't tell it apart from any 2026 M2.
    found = _detect(
        "bmw_m2",
        "3MF23DM00T8G27144",
        listing_text="2026 BMW M2 Turbo Design Edition | https://dealer.example/m2-tde",
    )
    assert found.edition.key == "m2_turbo_design_edition"
    assert found.provenance == Provenance.claimed
    assert _key("bmw_m2", "3MF23DM09T8G30320", listing_text="1 of 300 turbo design edition")


def test_m2_turbo_text_traps():
    vin = "3MF23DM00T8G27144"
    for text in ("M TwinPower Turbo", "its twin-turbo design delivers 473 hp", "M2 turbo i6"):
        assert _key("bmw_m2", vin, listing_text=text) is None, text
    # Right words, wrong car: a 2023 (13DM0) and an F87 (WBS).
    assert _key("bmw_m2", "3MF13DM09P8D14983", listing_text="M2 Turbo Design Edition") is None
    assert _key("bmw_m2", "WBS1J5C55JVD36611", listing_text="M2 Turbo Design Edition") is None


# --- GR Supra ---------------------------------------------------------------


def test_supra_a91_mt_from_marketcheck_trim_and_paint():
    found = _detect("gr_supra", "WZ1DB0G08PW054589", listing_text="A91-MT Edition")
    assert found.edition.key == "gr_supra_a91_mt_edition"
    assert found.provenance == Provenance.claimed
    burnout = _key("gr_supra", "WZ1DB0G00PW057423", listing_text="Burnout")
    assert burnout == "gr_supra_a91_mt_edition"  # A91-MT-only matte paint in 2023
    assert _key("gr_supra", "WZ1DB0G06PW056034", listing_text="2023 GR Supra 3.0 Premium") is None


def test_supra_45th_and_its_street_address_trap():
    assert (
        _key("gr_supra", "WZ1DB0G04RW062336", listing_text="45th Anniversary Edition")
        == "gr_supra_45th_anniversary"
    )
    assert _key("gr_supra", "WZ1DB0G04RW062336", listing_text="2101 45th Street") is None
    # Same words on a 2023 car: no edition that year.
    assert _key("gr_supra", "WZ1DB0G06PW056034", listing_text="45th Anniversary Edition") is None


def test_supra_final_edition_is_2026_only():
    assert (
        _key("gr_supra", "WZ1DB0G02TW078669", listing_text="2026 GR Supra MkV Final Edition")
        == "gr_supra_mkv_final_edition"
    )
    assert _key("gr_supra", "WZ1DB0G00PW057423", listing_text="MkV Final Edition") is None


def test_supra_2020_22_editions_gate_on_vin_era():
    assert _key("gr_supra", "WZ1DB4C01LW022039", listing_text="Launch Edition") == (
        "gr_supra_launch_edition"
    )
    assert _key("gr_supra", "WZ1DB0C01MW022039", listing_text="A91 Edition") == (
        "gr_supra_a91_edition"
    )
    assert _key("gr_supra", "WZ1DB0C01NW022039", listing_text="A91-CF Edition") == (
        "gr_supra_a91_cf_edition"
    )
    # "Launch Edition" on a 2023 means nothing.
    assert _key("gr_supra", "WZ1DB0G06PW056034", listing_text="Launch Edition") is None


# --- Misc -------------------------------------------------------------------


def test_unknown_model_or_missing_vin_detects_nothing():
    assert _key("nope", "1G1YC3D44P5500628", verified_rpo={"Y70"}) is None
    assert editions.detect("c8_stingray", Vehicle(verified_rpo={"Y70"})) is None


def test_every_catalog_entry_has_a_detection_path():
    for model_key in ("c8_stingray", "ct4v_blackwing", "bmw_m2", "gr_supra"):
        for e in editions.catalog(model_key):
            assert e.rpo or e.sticker_text or e.vin_serial or e.listing_text, e.key
            assert e.label and e.note


# --- listing_text field policy ----------------------------------------------


def test_listing_text_reads_headline_fields_per_source():
    rows = [
        (
            "marketcheck",
            {
                "heading": "2023 Cadillac CT4-V Blackwing Watkins Glen IMSA Edition",
                "vdp_url": "https://dealer.example/ct4-v-watkins-glen-imsa",
                "build": {"trim": "V-Series Blackwing", "version": "Blackwing Sedan"},
                "exterior_color": "Electric Blue",
                "dealer": {"name": "Burnout Garage", "city": "Sebring", "street": "2101 45th St"},
                "media": {"photo_links": ["https://img.example/a91mt0f00.jpg"]},
            },
        ),
        ("manual", {"url": "https://dealer.example/x", "page_text": "Similar: 70th Anniversary"}),
    ]
    text = editions.listing_text(rows, make="gm")
    assert "Watkins Glen IMSA Edition" in text
    assert "ct4-v-watkins-glen-imsa" in text
    assert "V-Series Blackwing" in text and "Blackwing Sedan" in text
    assert "Electric Blue" in text
    assert "https://dealer.example/x" in text
    # Never dealer identity/location, photo URLs, or pasted page text.
    for leak in ("Burnout", "Sebring", "45th", "a91mt", "Similar"):
        assert leak not in text, leak


def test_listing_text_skips_autodev_trim_for_non_gm():
    # Auto.dev stamps "MkV Final Edition" on every 2026 Supra (decoder default).
    rows = [
        (
            "autodev",
            {
                "vehicle.trim": "MkV Final Edition",
                "vehicle.exteriorColor": "Gray",
                "retailListing.vdp": "https://www.carfax.com/vehicle/WZ1DB0G05TW071957",
            },
        )
    ]
    text = editions.listing_text(rows, make="toyota")
    assert "Final Edition" not in text
    assert "Gray" in text
    assert _key("gr_supra", "WZ1DB0G05TW071957", listing_text=text) is None
    # On GM cars Auto.dev's trim is dealer free text — keep it.
    gm = editions.listing_text([("autodev", {"vehicle.trim": "3LT 70th Anniversary"})], make="gm")
    assert "70th Anniversary" in gm
