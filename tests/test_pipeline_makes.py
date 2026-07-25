"""Make-aware evidence routing: non-GM VINs never touch GM endpoints, and the
partsdeal decode drives verified transmission facts. Offline (mock transports
that FAIL the test if the wrong make's endpoint is hit).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from carfinder import db
from carfinder.budget import (
    BMW_PARTSDEAL,
    BUILDSHEET,
    GM_STICKER,
    TOYOTA_PARTSDEAL,
    BudgetGuard,
    ProviderBudget,
)
from carfinder.buildsheet import BuildSheetFetcher
from carfinder.partsdeal import SITES, PartsDealFetcher
from carfinder.pipeline import Pipeline
from carfinder.profiles import load_profile
from carfinder.sources import ListingRow, Page
from carfinder.stickers import StickerFetcher

ROOT = Path(__file__).resolve().parents[1]

M2_VIN = "3MF23DM09S8F25064"  # real 2025 G87 6MT (Cars & Bids)
SUPRA_MT_VIN = "WZ1DB0G06PW056034"  # real 2023 A91-MT manual
SUPRA_AUTO20_VIN = "WZ1DB4C01LW022039"  # real 2020 Launch Ed. (VIN-verified auto)


class Clock:
    def __init__(self):
        self.t = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)

    def __call__(self):
        self.t = self.t.replace(second=(self.t.second + 1) % 60)
        return self.t


class FakeSource:
    name = "autodev"

    def __init__(self, rows):
        self.rows = rows

    def search_page(self, params, page=1, sort="createdAt.desc", run_id=None):
        rows = self.rows if page == 1 else []
        return Page(rows=rows, total=len(self.rows), has_next=False)


def _forbidden_transport(label: str) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"{label} endpoint was called for a non-{label} VIN: {request.url}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def _toyota_payload(trans: str) -> dict:
    return {
        "data": {
            "vinInfos": [
                {
                    "vehicleInfo": "2023 Toyota GR Supra",
                    "majorAttribute": [
                        {"name": "Engine Model", "desc": "B58-3.0L"},
                        {"name": "Trim", "desc": "BASE"},
                        {"name": "Trans", "desc": trans},
                        {"name": "Model Code", "desc": "DB03L-XXXX"},
                    ],
                    "vehicleInformation": [],
                    "specification": [],
                }
            ]
        }
    }


def _bmw_payload() -> dict:
    return {
        "data": {
            "vinInfos": [
                {
                    "vehicleInfo": "2025 BMW M2",
                    "majorAttribute": [
                        {"name": "Trim", "desc": "1959-Base"},
                        {"name": "Engine", "desc": "765-6 Cyl 3.0 L GAS"},
                    ],
                    "vehicleInformation": [],
                    "specification": [],
                }
            ]
        }
    }


def _pipeline(rows, toyota_trans="MTM"):
    conn = db.bootstrap(":memory:")
    guard = BudgetGuard(
        conn,
        {
            GM_STICKER: ProviderBudget(GM_STICKER, "count", 100),
            BUILDSHEET: ProviderBudget(BUILDSHEET, "count", 100),
            BMW_PARTSDEAL: ProviderBudget(BMW_PARTSDEAL, "count", 100),
            TOYOTA_PARTSDEAL: ProviderBudget(TOYOTA_PARTSDEAL, "count", 100),
        },
    )
    # GM endpoints hard-fail the test when touched by a foreign VIN. The
    # stickers_dir is a tmp-less sentinel path: no cached PDFs for these VINs.
    sticker_fetcher = StickerFetcher(
        conn,
        guard,
        stickers_dir=ROOT / "stickers",
        client=_forbidden_transport("GM sticker"),
        min_interval_s=0.0,
        sleep=lambda s: None,
    )
    gm_sheets = BuildSheetFetcher(
        conn, guard, client=_forbidden_transport("gmpartsgiant"), min_interval_s=0.0
    )

    def pd_client(make: str, payload: dict) -> httpx.Client:
        return httpx.Client(
            base_url=SITES[make][0],
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)),
        )

    partsdeal = {
        "bmw": PartsDealFetcher(
            conn, guard, "bmw", client=pd_client("bmw", _bmw_payload()), min_interval_s=0.0
        ),
        "toyota": PartsDealFetcher(
            conn,
            guard,
            "toyota",
            client=pd_client("toyota", _toyota_payload(toyota_trans)),
            min_interval_s=0.0,
        ),
    }
    pipe = Pipeline(
        conn,
        FakeSource(rows),
        sticker_fetcher,
        current_year=2026,
        now=Clock(),
        buildsheet_fetcher=gm_sheets,
        partsdeal_fetchers=partsdeal,
    )
    return pipe, conn


def _row(vin, **kw):
    defaults = dict(source="autodev", price=55000, miles=15000, year=2024)
    return ListingRow(vin=vin, **{**defaults, **kw})


def _hard(conn, vin) -> dict[str, dict]:
    bd = json.loads(
        conn.execute("SELECT breakdown_json FROM match_results WHERE vin=?", (vin,)).fetchone()[
            "breakdown_json"
        ]
    )
    return {h["key"]: h for h in bd["hard"]}


def test_m2_routes_to_bmw_partsdeal_never_gm():
    rows = [_row(M2_VIN, year=2025, make="BMW", model="M2", trim="Base", price=63000)]
    pipe, conn = _pipeline(rows)
    profile = load_profile(ROOT / "config" / "bmw_m2.yaml")
    stats = pipe.run(profile, run_id="r1")
    # GM transports raise on contact — reaching here proves the routing.
    assert stats.scored == 1
    hard = _hard(conn, M2_VIN)
    assert hard["model_m2"]["status"] == "pass"  # VIN fact, verified
    assert hard["gen_g87"]["status"] == "pass"
    # Manual is not machine-verifiable for a G87: needs_review, never a pass.
    assert hard["manual_transmission"]["status"] == "needs_review"
    assert stats.needs_review == 1


def test_supra_manual_verified_by_partsdeal_mtm():
    rows = [_row(SUPRA_MT_VIN, year=2023, make="Toyota", model="GR Supra", trim="3.0 Premium")]
    pipe, conn = _pipeline(rows, toyota_trans="MTM")
    profile = load_profile(ROOT / "config" / "gr_supra.yaml")
    stats = pipe.run(profile, run_id="r1")
    assert stats.scored == 1
    hard = _hard(conn, SUPRA_MT_VIN)
    assert hard["model_supra"]["status"] == "pass"
    assert hard["engine_30"]["status"] == "pass"  # VIN pos-6, verified
    # The factory MTM field is definitive: manual verified-passes.
    assert hard["manual_transmission"]["status"] == "pass"
    assert hard["manual_transmission"]["provenance"] == "verified"
    # Premium rides claimed evidence (listing trim text) into review.
    assert hard["trim_premium"]["status"] == "needs_review"


def test_2020_supra_vin_fast_path_excludes_before_any_fetch():
    # 2020-22 Supra VINs are corpus-verified automatic: the VIN fast path must
    # exclude on the manual requirement with ZERO evidence fetches. The
    # partsdeal transport returning MTM (wrong on purpose) proves it was
    # never consulted: had it been, the car would have PASSED manual.
    rows = [_row(SUPRA_AUTO20_VIN, year=2020, make="Toyota", model="GR Supra", trim="3.0 Premium")]
    pipe, conn = _pipeline(rows, toyota_trans="MTM")
    profile = load_profile(ROOT / "config" / "gr_supra.yaml")
    stats = pipe.run(profile, run_id="r1")
    assert stats.excluded == 1
    assert stats.buildsheets_fetched == 0  # no partsdeal call was spent
    hard = _hard(conn, SUPRA_AUTO20_VIN)
    assert hard["manual_transmission"]["status"] == "fail"
    assert hard["manual_transmission"]["provenance"] == "verified"
    assert hard["year_2023_plus"]["status"] == "fail"
