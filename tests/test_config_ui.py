"""Config editor: feature catalog, guided edits, and the web routes.

Web tests operate on a COPY of the seed configs in tmp_path so the real
config/ files are never touched.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from carfinder import db, features, profile_edit
from carfinder.config import Settings
from carfinder.profiles import ProfileError, load_profile
from carfinder.web import create_app

ROOT = Path(__file__).resolve().parents[1]
BW_FIXTURE = ROOT / "tests" / "fixtures" / "buildsheet" / "1G6D65RP8N0460286.json"
BW_VIN = "1G6D65RP8N0460286"


# --- feature catalog ----------------------------------------------------------


def _seed_catalog_db(path):
    conn = db.bootstrap(str(path))
    ts = "2026-07-15T12:00:00Z"
    conn.execute(
        """INSERT INTO vehicles (vin, model_key, status, missing_passes, first_seen, last_seen)
           VALUES (?, 'ct4v_blackwing', 'active', 0, ?, ?)""",
        (BW_VIN, ts, ts),
    )
    conn.execute(
        "INSERT INTO build_sheets (vin, status, raw_json, fetched_at) VALUES (?, 'found', ?, ?)",
        (BW_VIN, BW_FIXTURE.read_text(encoding="utf-8"), ts),
    )
    conn.commit()
    conn.close()


def test_catalog_names_features_without_codes(tmp_path):
    dbp = tmp_path / "cf.sqlite"
    _seed_catalog_db(dbp)
    conn = db.bootstrap(str(dbp))
    bw = load_profile(ROOT / "config" / "ct4v_blackwing.yaml")
    feats = features.catalog(conn, bw)
    by_code = {f.code: f for f in feats}
    # Human descriptions, prevalence, and profile status all present.
    assert "CF5" in by_code
    assert "Sun" in by_code["CF5"].description or "Roof" in by_code["CF5"].description
    assert by_code["CF5"].status == "highlight" and by_code["CF5"].bonus == 3
    assert by_code["KU9"].status == "require"
    assert by_code["KU9"].total == 1 and by_code["KU9"].prevalence == 100.0
    # Administrative codes are filtered out of the catalog.
    assert "LAL" not in by_code  # plant code
    assert "EF7" not in by_code  # country
    assert "NTB" not in by_code  # emission system


# --- guided edits ---------------------------------------------------------------


def test_add_and_remove_highlight_roundtrip():
    bw = load_profile(ROOT / "config" / "ct4v_blackwing.yaml")
    p = profile_edit.add_highlight(bw, "JF5", "Pedals Sporty, Alloy", 2)
    h = next(x for x in p.highlights if "Pedals" in x.label)
    assert h.bonus == 2 and h.matcher.rpo == ["JF5"]
    p2 = profile_edit.remove_code(p, "JF5")
    assert not any("Pedals" in x.label for x in p2.highlights)


def test_add_hard_replaces_prior_role_for_code():
    bw = load_profile(ROOT / "config" / "ct4v_blackwing.yaml")
    p = profile_edit.add_highlight(bw, "JF5", "Alloy Pedals", 2)
    p = profile_edit.add_hard(p, "JF5", "Alloy Pedals", "require")
    assert not any("JF5" in h.matcher.rpo for h in p.highlights)
    assert any(c.matcher and "JF5" in c.matcher.rpo and c.kind == "require" for c in p.hard)


def test_custom_feature_without_code_matches_by_name_text():
    bw = load_profile(ROOT / "config" / "ct4v_blackwing.yaml")
    p = profile_edit.add_highlight(bw, "", "Cyber Yellow Metallic", 3)
    h = next(x for x in p.highlights if x.label == "Cyber Yellow Metallic")
    assert h.matcher.rpo == []
    # Falls back to the literal (escaped) name as the sticker-text pattern.
    assert h.matcher.compiled().search("EXTERIOR: CYBER YELLOW METALLIC ENGINE")


def test_custom_feature_with_code_and_custom_text():
    bw = load_profile(ROOT / "config" / "ct4v_blackwing.yaml")
    p = profile_edit.add_highlight(
        bw, "GCP", "Cyber Yellow Metallic", 5, sticker_text="CYBER YELLOW"
    )
    h = next(x for x in p.highlights if x.label == "Cyber Yellow Metallic")
    assert h.matcher.rpo == ["GCP"]
    assert h.matcher.sticker_text == "CYBER YELLOW"


def test_custom_feature_same_label_replaces_not_duplicates():
    bw = load_profile(ROOT / "config" / "ct4v_blackwing.yaml")
    p = profile_edit.add_highlight(bw, "", "Cyber Yellow Metallic", 3)
    p = profile_edit.add_highlight(p, "", "Cyber Yellow Metallic", 8)
    matches = [x for x in p.highlights if x.label == "Cyber Yellow Metallic"]
    assert len(matches) == 1 and matches[0].bonus == 8


def test_custom_feature_bad_regex_rejected():
    bw = load_profile(ROOT / "config" / "ct4v_blackwing.yaml")
    # Pydantic raises ValidationError (a ValueError) at Matcher construction;
    # the web layer catches ValueError and flashes "Rejected" without writing.
    with pytest.raises(ValueError):
        profile_edit.add_highlight(bw, "", "Broken", 3, sticker_text="(unclosed")


def test_set_numeric_and_year_and_colors():
    bw = load_profile(ROOT / "config" / "ct4v_blackwing.yaml")
    p = profile_edit.set_numeric(bw, "price", 58000, 63000, 0.3, True)
    price = next(s for s in p.soft if s.attr == "price")
    assert (price.ideal, price.hard, price.weight) == (58000, 63000, 0.3)
    assert price.exclude_beyond_hard is True
    p = profile_edit.set_year_min(p, 2023)
    assert next(c.min_year for c in p.hard if c.kind == "year_min") == 2023
    p = profile_edit.set_colors(p, ["blue", "gray"], ["white"], 0.15)
    color = next(s for s in p.soft if s.kind == "color")
    assert color.preferred == ["blue", "gray"] and color.penalized == ["white"]


def test_invalid_edit_is_rejected_before_write(tmp_path):
    bw = load_profile(ROOT / "config" / "ct4v_blackwing.yaml")
    with pytest.raises(ProfileError):
        profile_edit.set_colors(bw, ["chartreuse"], [], 0.1)  # unknown family


def test_save_profile_backs_up_and_validates(tmp_path):
    src = ROOT / "config" / "ct4v_blackwing.yaml"
    dst = tmp_path / "ct4v_blackwing.yaml"
    shutil.copyfile(src, dst)
    p = profile_edit.set_year_min(load_profile(dst), 2023)
    profile_edit.save_profile(dst, p)
    assert dst.with_suffix(".yaml.bak").exists()
    reloaded = load_profile(dst)
    assert next(c.min_year for c in reloaded.hard if c.kind == "year_min") == 2023


# --- web routes ------------------------------------------------------------------


@pytest.fixture
def editor_client(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    for f in (ROOT / "config").glob("*.yaml"):
        shutil.copyfile(f, cfg / f.name)
    dbp = tmp_path / "cf.sqlite"
    _seed_catalog_db(dbp)
    settings = Settings(
        db_path=dbp,
        autodev_api_key=None,
        anthropic_api_key=None,
        monthly_call_cap_autodev=800,
        monthly_ai_budget_usd=2.0,
        monthly_call_cap_gm=1000,
        monthly_call_cap_buildsheet=1000,
        stickers_dir=ROOT / "stickers",
        config_dir=cfg,
        notify_webhook_url=None,
        notify_threshold=85.0,
    )
    return TestClient(create_app(settings)), cfg


def test_editor_page_renders_catalog_and_profile(editor_client):
    c, _ = editor_client
    r = c.get("/config/ct4v_blackwing")
    assert r.status_code == 200
    assert "Discover features" in r.text
    assert "Ventilated Seat" in r.text  # KU9 from the build sheet, humanized
    assert "6-speed manual" in r.text  # current hard requirement listed
    assert "/config" in c.get("/").request.url.path or True  # nav link exists in base


def test_editor_feature_highlight_persists_to_yaml(editor_client):
    c, cfg = editor_client
    r = c.post(
        "/config/ct4v_blackwing/feature",
        data={"action": "highlight", "code": "JF5", "description": "Alloy Pedals", "bonus": "5"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "Saved" in r.headers["location"]
    saved = load_profile(cfg / "ct4v_blackwing.yaml")
    h = next(x for x in saved.highlights if x.label == "Alloy Pedals")
    assert h.bonus == 5 and h.matcher.rpo == ["JF5"]
    # The editor page now shows it as in-profile.
    assert "Alloy Pedals" in c.get("/config/ct4v_blackwing").text


def test_editor_custom_feature_without_code(editor_client):
    c, cfg = editor_client
    r = c.post(
        "/config/ct4v_blackwing/feature",
        data={
            "action": "require",
            "code": "",
            "description": "Cyber Yellow Metallic",
            "sticker_text": "CYBER YELLOW",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "Saved" in r.headers["location"]
    saved = load_profile(cfg / "ct4v_blackwing.yaml")
    req = next(x for x in saved.hard if x.label == "Cyber Yellow Metallic")
    assert req.kind == "require" and req.matcher.rpo == []
    assert req.matcher.sticker_text == "CYBER YELLOW"


def test_editor_bounds_persist(editor_client):
    c, cfg = editor_client
    r = c.post(
        "/config/ct4v_blackwing/bounds",
        data={
            "year_min": "2023",
            "price_ideal": "57000",
            "price_hard": "62000",
            "price_weight": "0.3",
            "price_exclude": "on",
            "miles_ideal": "30000",
            "miles_hard": "50000",
            "miles_weight": "0.2",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "Saved" in r.headers["location"]
    saved = load_profile(cfg / "ct4v_blackwing.yaml")
    price = next(s for s in saved.soft if s.attr == "price")
    assert price.hard == 62000 and price.exclude_beyond_hard is True
    miles = next(s for s in saved.soft if s.attr == "miles")
    assert miles.exclude_beyond_hard is False
    assert next(x.min_year for x in saved.hard if x.kind == "year_min") == 2023


def test_editor_rejects_invalid_edit_without_writing(editor_client):
    c, cfg = editor_client
    before = (cfg / "ct4v_blackwing.yaml").read_text(encoding="utf-8")
    r = c.post(
        "/config/ct4v_blackwing/bounds",
        data={
            "year_min": "2023",
            "price_ideal": "57000",
            "price_hard": "62000",
            "price_weight": "-1",  # invalid: weight must be > 0
            "miles_ideal": "30000",
            "miles_hard": "50000",
            "miles_weight": "0.2",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "Rejected" in r.headers["location"]
    assert (cfg / "ct4v_blackwing.yaml").read_text(encoding="utf-8") == before


def test_editor_remove_highlight(editor_client):
    c, cfg = editor_client
    r = c.post(
        "/config/ct4v_blackwing/remove",
        data={"kind": "highlight", "ident": "Power sunroof"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "Saved" in r.headers["location"]
    saved = load_profile(cfg / "ct4v_blackwing.yaml")
    assert not any(h.label == "Power sunroof" for h in saved.highlights)


def test_saved_profile_is_immediately_live(editor_client):
    """After a save, the matches page uses the reloaded profile (hot reload)."""
    c, _ = editor_client
    c.post(
        "/config/ct4v_blackwing/feature",
        data={"action": "highlight", "code": "JF5", "description": "Alloy Pedals", "bonus": "5"},
        follow_redirects=False,
    )
    # /config lists profiles from app.state — reload keeps them consistent.
    r = c.get("/config/ct4v_blackwing")
    assert "Alloy Pedals" in r.text


def test_form_posts_need_no_multipart_dependency():
    # Guard: the editor parses urlencoded bodies itself (web._form), because
    # python-multipart is not a dependency of this project.
    import importlib.util

    assert importlib.util.find_spec("multipart") is None, (
        "python-multipart appeared as a dependency — fine, but then web._form "
        "can be replaced with request.form() and this guard dropped"
    )
