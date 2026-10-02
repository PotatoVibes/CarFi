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
SUPRA_PREMIUM_VIN = "WZ1DB0G07PW059282"  # real 2023 3.0 Premium automatic
SUPRA_AUTO20_VIN = "WZ1DB4C01LW022039"  # real 2020 Launch Ed. (VIN-verified auto;
# no fixture on purpose — the VIN fast path must exclude it before any fetch)


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


FIXTURES = Path(__file__).parent / "fixtures" / "partsdeal"


def _fixture(vin: str) -> dict:
    return json.loads((FIXTURES / f"{vin}.json").read_text(encoding="utf-8"))


def _pipeline(rows):
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

    def pd_client(make: str) -> httpx.Client:
        # Serve the recorded fixture for whatever VIN is requested.
        def handler(request: httpx.Request) -> httpx.Response:
            vin = json.loads(request.read())["vin"]
            return httpx.Response(200, json=_fixture(vin))

        return httpx.Client(base_url=SITES[make][0], transport=httpx.MockTransport(handler))

    partsdeal = {
        make: PartsDealFetcher(conn, guard, make, client=pd_client(make), min_interval_s=0.0)
        for make in ("bmw", "toyota")
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


def test_2023_supra_catalog_never_verifies_transmission():
    """The recorded fixture for this factory-MANUAL car (A91-MT) carries the
    catalog's wrong 'ATM' attribute AND a '6-Speed Manual' gear-shift line.
    Neither may verify nor fail the manual requirement: the listing text
    (claimed) routes it to needs_review for a human, and the Premium gate
    stays claimed-only too."""
    rows = [
        _row(
            SUPRA_MT_VIN,
            year=2023,
            make="Toyota",
            model="GR Supra",
            trim="A91-MT 6-Speed Manual",
        )
    ]
    pipe, conn = _pipeline(rows)
    profile = load_profile(ROOT / "config" / "gr_supra.yaml")
    stats = pipe.run(profile, run_id="r1")
    assert stats.scored == 1
    hard = _hard(conn, SUPRA_MT_VIN)
    assert hard["model_supra"]["status"] == "pass"
    assert hard["engine_30"]["status"] == "pass"  # VIN pos-6, verified
    # Claimed listing text, never the catalog: needs_review, claimed.
    assert hard["manual_transmission"]["status"] == "needs_review"
    assert hard["manual_transmission"]["provenance"] == "claimed"
    assert stats.buildsheets_fetched == 1  # the partsdeal call happened...
    assert stats.stickers_fetched == 0  # ...and no GM endpoint was touched


def test_2023_premium_auto_is_not_verified_manual():
    """Regression pin for the fixture-caught bug: the automatic Premium's
    catalog payload contains '6-Speed Manual Floor Shift'. Had that reached
    verified_text, this car would verified-PASS manual. It must not."""
    rows = [_row(SUPRA_PREMIUM_VIN, year=2023, make="Toyota", model="GR Supra", trim="3.0 Premium")]
    pipe, conn = _pipeline(rows)
    profile = load_profile(ROOT / "config" / "gr_supra.yaml")
    pipe.run(profile, run_id="r1")
    hard = _hard(conn, SUPRA_PREMIUM_VIN)
    assert hard["manual_transmission"]["status"] == "needs_review"
    assert hard["manual_transmission"]["provenance"] != "verified"
    # Premium claimed via listing trim text -> review, not a silent pass.
    assert hard["trim_premium"]["status"] == "needs_review"
    assert hard["trim_premium"]["provenance"] == "claimed"


def test_2020_supra_vin_fast_path_excludes_before_any_fetch():
    # 2020-22 Supra VINs are corpus-verified automatic: the VIN fast path must
    # exclude on the manual requirement with ZERO evidence fetches.
    rows = [_row(SUPRA_AUTO20_VIN, year=2020, make="Toyota", model="GR Supra", trim="3.0 Premium")]
    pipe, conn = _pipeline(rows)
    profile = load_profile(ROOT / "config" / "gr_supra.yaml")
    stats = pipe.run(profile, run_id="r1")
    assert stats.excluded == 1
    assert stats.buildsheets_fetched == 0  # no partsdeal call was spent
    hard = _hard(conn, SUPRA_AUTO20_VIN)
    assert hard["manual_transmission"]["status"] == "fail"
    assert hard["manual_transmission"]["provenance"] == "verified"
    assert hard["year_2023_plus"]["status"] == "fail"


def _insert_listing(conn, source: str, vin: str, raw: dict) -> None:
    conn.execute(
        """INSERT INTO listings (source, vin, raw_json, first_seen, last_seen)
           VALUES (?, ?, ?, '2026-07-24T00:00:00Z', '2026-07-24T00:00:00Z')""",
        (source, vin, json.dumps(raw)),
    )
    conn.commit()


def test_supra_edition_rides_marketcheck_listing_text_not_autodev_trim():
    """Auto.dev's decoded Supra trim is a default ("3.0" on every 2023, "MkV
    Final Edition" on every 2026) — it must never name an edition. The
    Marketcheck row's build.trim for the same VIN does: a claimed +5."""
    rows = [
        _row(
            SUPRA_MT_VIN,
            year=2023,
            make="Toyota",
            model="GR Supra",
            trim="3.0",
            raw={"vehicle.trim": "MkV Final Edition"},
        )
    ]
    pipe, conn = _pipeline(rows)
    profile = load_profile(ROOT / "config" / "gr_supra.yaml")
    pipe.run(profile, run_id="r1")
    m = conn.execute("SELECT * FROM match_results WHERE vin=?", (SUPRA_MT_VIN,)).fetchone()
    assert m["edition"] is None and m["edition_bonus"] == 0

    _insert_listing(
        conn,
        "marketcheck",
        SUPRA_MT_VIN,
        {
            "heading": "2023 Toyota GR Supra 3.0 6MT Manual Transmission",
            "build": {"trim": "A91-MT Edition", "version": "3.0 A91-MT Edition"},
            "dealer": {"name": "Burnout Garage"},
        },
    )
    assert _hard(conn, SUPRA_MT_VIN)["manual_transmission"]["provenance"] == "unknown"
    pipe.rescore(profile)
    m = conn.execute("SELECT * FROM match_results WHERE vin=?", (SUPRA_MT_VIN,)).fetchone()
    assert m["edition"] == "A91-MT Edition"
    assert m["edition_provenance"] == "claimed"
    assert m["edition_bonus"] == 5.0
    # Listing text feeds the edition only: the heading's "6MT Manual
    # Transmission" would claim the manual gate if it leaked into claimed_text.
    assert _hard(conn, SUPRA_MT_VIN)["manual_transmission"]["provenance"] == "unknown"


def test_autodev_final_edition_trim_never_reaches_a_2026_supra():
    # Real 2026 3.0 Premium M/T that Auto.dev mislabels "MkV Final Edition".
    fe_vin = "WZ1DB0G05TW071957"
    pipe, conn = _pipeline([])
    _insert_listing(conn, "autodev", fe_vin, {"vehicle.trim": "MkV Final Edition"})
    assert "Final Edition" not in pipe._listing_text(fe_vin)
