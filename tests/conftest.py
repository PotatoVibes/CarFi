"""Shared fixtures for M6 UI/scheduler tests — a directly-seeded DB + Settings.

Seeds match_results/vehicles/conflicts/price_history with SQL (no pipeline, no
network) so the read layer and web routes have deterministic data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from carfinder import db
from carfinder.config import Settings

ROOT = Path(__file__).resolve().parents[1]

BW_VIN = "1G6D65RP8N0460286"  # committed sticker fixture exists on disk
C8_VIN = "1G1YC3D47P5121316"
REVIEW_VIN = "1G1YCLAIMONLY00001"


def _breakdown(hard_status, hard_prov):
    return {
        "hard": [
            {
                "key": "z51",
                "label": "Z51 Performance Package",
                "group": "hard",
                "status": hard_status,
                "provenance": hard_prov,
                "detail": "verified present",
                "matched": hard_status == "pass",
            }
        ],
        "soft": [
            {
                "key": "price",
                "label": "Price",
                "group": "soft",
                "status": "scored",
                "provenance": "claimed",
                "detail": "price=61000 -> 80",
                "dim_score": 80.0,
                "weight": 0.25,
            }
        ],
        "highlights": [
            {
                "key": "Front lift w/ memory",
                "label": "Front lift w/ memory",
                "group": "highlight",
                "status": "badge",
                "provenance": "verified",
                "matched": True,
                "bonus": 8.0,
            }
        ],
    }


def _add_vehicle(conn, vin, model_key, verdict, total, **v):
    ts = "2026-07-15T12:00:00Z"
    conn.execute(
        """INSERT INTO vehicles (vin, model_key, year, make, model, trim, color, miles,
           price, status, missing_passes, first_seen, last_seen)
           VALUES (?,?,?,?,?,?,?,?,?, 'active', 0, ?, ?)""",
        (
            vin,
            model_key,
            v.get("year"),
            v.get("make"),
            v.get("model"),
            v.get("trim"),
            v.get("color"),
            v.get("miles"),
            v.get("price"),
            v.get("first_seen", ts),
            ts,
        ),
    )
    conn.execute(
        """INSERT INTO match_results (vin, model_key, verdict, soft_score, highlight_bonus,
           total, breakdown_json, scored_at) VALUES (?,?,?,?,?,?,?,?)""",
        (
            vin,
            model_key,
            verdict,
            total - 8,
            8.0,
            total,
            json.dumps(
                _breakdown(
                    "pass" if verdict != "excluded" else "fail",
                    "verified" if verdict != "needs_review" else "claimed",
                )
            ),
            ts,
        ),
    )
    conn.commit()


@pytest.fixture
def seeded_settings(tmp_path) -> Settings:
    db_path = tmp_path / "cf.sqlite"
    conn = db.bootstrap(str(db_path))
    _add_vehicle(
        conn,
        BW_VIN,
        "ct4v_blackwing",
        "ranked",
        92.0,
        year=2022,
        make="Cadillac",
        model="CT4-V",
        trim="Blackwing",
        color="Black Raven",
        miles=30000,
        price=58000,
    )
    _add_vehicle(
        conn,
        C8_VIN,
        "c8_stingray",
        "excluded",
        90.0,
        year=2023,
        make="Chevrolet",
        model="Corvette",
        trim="Stingray",
        color="Black",
        miles=12000,
        price=64000,
    )
    _add_vehicle(
        conn,
        REVIEW_VIN,
        "c8_stingray",
        "needs_review",
        100.0,
        year=2024,
        make="Chevrolet",
        model="Corvette",
        trim="Stingray Convertible",
        color="Rapid Blue",
        miles=22000,
        price=61000,
        first_seen="2026-07-20T00:00:00Z",
    )
    # Scorer-resolved color on the ranked car: the listing text ("Black Raven")
    # stays on vehicles.color; the resolved family+paint live on match_results.
    conn.execute(
        "UPDATE match_results SET color_family='black', paint_name='BLACK RAVEN' WHERE vin=?",
        (BW_VIN,),
    )
    # Seller + location on the ranked car (Austin is ~130 mi from 77498).
    conn.execute(
        "UPDATE vehicles SET dealer='Example Motors of Austin', city='Austin', "
        "state='TX', zip='78701' WHERE vin=?",
        (BW_VIN,),
    )
    # A conflict + price history on the ranked car.
    from datetime import UTC, datetime

    db.replace_conflicts(
        conn,
        C8_VIN,
        [("year", "sticker", json.dumps([["sticker", 2023], ["listing", 2024]]))],
        datetime(2026, 7, 15, tzinfo=UTC),
    )
    conn.execute(
        "INSERT INTO price_history (vin, source, price, observed_at) VALUES (?,?,?,?)",
        (BW_VIN, "autodev", 60000, "2026-07-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO price_history (vin, source, price, observed_at) VALUES (?,?,?,?)",
        (BW_VIN, "autodev", 58000, "2026-07-14T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO sync_runs (run_id, source, model_key, mode, started_at, new_vins) "
        "VALUES ('r1','autodev','c8_stingray','incremental','2026-07-15T12:00:00Z', 3)"
    )
    # A listing row so the detail page can link to the dealer VDP.
    conn.execute(
        """INSERT INTO listings (source, vin, price, url, raw_json, first_seen, last_seen)
           VALUES ('autodev', ?, 58000, 'https://dealer.example/bw-listing', '{}',
                   '2026-07-01T00:00:00Z', '2026-07-15T12:00:00Z')""",
        (BW_VIN,),
    )
    conn.commit()
    conn.close()
    return Settings(
        db_path=db_path,
        autodev_api_key=None,
        anthropic_api_key=None,
        monthly_call_cap_autodev=800,
        monthly_ai_budget_usd=2.0,
        monthly_call_cap_gm=500,
        monthly_call_cap_buildsheet=500,
        stickers_dir=ROOT / "stickers",
        config_dir=ROOT / "config",
        notify_webhook_url=None,
        notify_threshold=85.0,
    )
