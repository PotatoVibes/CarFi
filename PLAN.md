# Car Finder — Implementation Plan

Tool to find Bill's Miata replacement (lease ends April 2027). Candidates: C8 Corvette Stingray, Cadillac CT4-V Blackwing. Deterministic-first architecture; AI only where unstructured text forces it.

**Stack:** Python 3.12 (`uv`), FastAPI, APScheduler, SQLite, httpx, pdfplumber, PyYAML/pydantic, pytest + respx. Single Docker container, volume-mounted state.

**Data sources:**
- **Auto.dev Listings API** (primary) — free Starter tier: 1,000 calls/mo, 20 rows/page cap, national search (no radius restriction)
- **Auto.dev OEM Build Data API** — evaluate in M4; cross-referenced against window sticker, never trusted alone
- **GM window sticker** — `https://cws.gm.com/vs-cws/vehshop/v2/vehicle/windowsticker?vin={VIN}` — ground truth for trim/RPO/paint; free; cached forever per VIN
- **Marketcheck** — adapter stubbed, disabled by default (100-mile radius on free tier makes it a Houston-local supplement only)
- **Anthropic API (Haiku)** — fallback extraction of *claimed* features from dealer free-text only

---

## Milestone 0 — Scaffold + API budget guard (FIRST, before any live call)

The budget guard ships before the first API client so it's impossible to burn quota accidentally.

Tasks:
1. Repo scaffold: `uv init`, ruff, pytest, pre-commit, `.env.example`, `src/carfinder/` layout
2. SQLite bootstrap + migrations-lite (schema versioning table)
3. `api_calls` table: `(provider, endpoint, ts, run_id)` — every outbound call recorded
4. `BudgetGuard` class wrapping all HTTP clients:
   - `MONTHLY_CALL_CAP_AUTODEV=800` (buffer under the 1,000 free; calendar-month window)
   - Warn-level log at 50/75/90% consumed; **hard exception at cap** — no call leaves the process
   - Separate cap for Anthropic spend (`MONTHLY_AI_BUDGET_USD`, default $2)
5. CLI: `carfinder budget` prints month-to-date usage per provider

Tests: unit tests for cap enforcement, month rollover, warning thresholds. No network anywhere.

Commits:
- `chore: scaffold project (uv, ruff, pytest, pre-commit)`
- `feat: sqlite bootstrap + api_calls ledger`
- `feat: BudgetGuard with hard monthly caps and threshold warnings`
- `feat: budget CLI command`
- Tag: `m0`

---

## Milestone 1 — Requirement schema + deterministic scorer

Pure functions, zero I/O. This is the heart of the tool and the most heavily tested code.

Tasks:
1. Pydantic models for requirement profiles:
   - `hard` constraints (verified-only pass/fail)
   - `soft` constraints (ideal/hard bounds, penalty curve, weight)
   - `highlights` (bonus badges — see YAML below)
   - Attribute matchers support **RPO code match AND/OR sticker-text regex** (GM sticker text is more stable than my memory of every code — see M2 verification step)
2. YAML loader + validation; `config/c8_stingray.yaml`, `config/ct4v_blackwing.yaml` seeded with Bill's stated requirements
3. Scorer:
   - Hard gate first (fail → excluded, with reason)
   - Attribute with `unknown` provenance on a hard requirement → **"needs review" bucket**, not silent rejection (configurable)
   - Soft score: weighted linear penalty between ideal and hard bound; 0–100
   - Highlight bonus: sum of per-item bonuses, **capped at +10 total** so trinkets can't beat fundamentals
   - Output: score + per-requirement breakdown + provenance tag (`verified` / `claimed` / `unknown`) per attribute
4. Color handling: paint RPO → color-family lookup table per model (deterministic; listing color text as fallback with `claimed` provenance)

Tests: golden-case table tests (known car dicts → expected scores), property tests (monotonicity: more miles never raises score; verified beats claimed on ties), YAML round-trip validation, malformed-config rejection.

Commits:
- `feat: requirement profile schema (hard/soft/highlight) + YAML loader`
- `feat: deterministic scorer with penalty curves and provenance`
- `feat: paint RPO -> color family tables (C8, CT4-V BW)`
- `feat: seed requirement profiles for C8 and Blackwing`
- `test: scorer golden cases + property tests`
- Tag: `m1`

---

## Milestone 2 — Window sticker fetcher + RPO parser

Tasks:
1. Fetcher: httpx with retries/exponential backoff, realistic headers, per-run rate limit (be polite — this endpoint intermittently throws Akamai denials); cache PDF to `./stickers/{vin}.pdf`; **never refetch a cached VIN**; record fetch outcome (found / not-found / blocked) in DB so not-found isn't retried every run
2. Parser (pdfplumber):
   - Extract trim line, options section (lines carry RPO codes + descriptions + prices), paint, MSRP
   - Normalize into `{attribute: (value, provenance=verified)}`
   - Parser confidence score; below threshold → flag for M5 AI-parse fallback, don't guess
3. **RPO verification pass (human-in-loop, one time):** run parser on 3–5 real VINs from Bill's current shopping; diff extracted codes/descriptions against the highlight YAML; **correct any code I got wrong** (entries marked `# VERIFY` below). Commit corrected YAML with sticker fixtures as evidence.

Tests: committed sticker PDF fixtures (3–5 real VINs Bill supplies — mix of C8 configs, ideally one Blackwing); parser unit tests per fixture; cache-behavior tests; blocked-response retry tests (mocked).

Commits:
- `feat: GM window sticker fetcher with cache + polite retry`
- `feat: sticker PDF parser -> verified attributes`
- `test: parser fixtures from real VINs`
- `fix: correct highlight RPO codes against real stickers` ← expected
- Tag: `m2`

---

## Milestone 3 — Auto.dev client + incremental sync

**Prereq: Bill creates the Auto.dev account/key.** First live action is a supervised *fixture capture session*, budgeted at ~15 calls:
- 2 pages of C8 search, 1 page Blackwing search
- 2 single-VIN listing lookups
- 2 OEM Build Data lookups (for M4) on VINs we hold window stickers for
- 1 `includes=total` count query per model

Everything after that is developed offline against the captured fixtures. `pytest` default profile makes **zero network calls**; live smoke tests behind `-m live` (≤3 calls).

Tasks:
1. `ListingSource` protocol; `AutoDevSource` implementation (all calls through BudgetGuard)
2. Query builder from requirement profile → prefilter params, e.g. `vehicle.make=Chevrolet&vehicle.model=Corvette&vehicle.year=2022-2026&retailListing.price=1-68000` (prefilter bounds slightly wider than hard bounds so fuzzy-band cars aren't excluded server-side)
3. `?select=` projection to trim payloads
4. Incremental sync: `sort=createdAt.desc`, walk pages of 20, stop when a full page of already-known VINs with unchanged prices is hit; weekly full pass for price drops + delist detection (missing VIN across 2 full passes → mark `presumed_sold`)
5. Schema: `listings` (per-source), `vehicles` (VIN PK, merged view), `price_history`, `stickers`, `sync_runs`
6. Pipeline: sync → new VINs → sticker fetch → parse → score → persist match results
7. Marketcheck: `MarketcheckSource` stub implementing the protocol, disabled by default

Tests: mocked pagination/incremental-stop logic, dedupe across sources, price-history append, query-builder golden tests, live smoke (marked).

Commits:
- `feat: ListingSource protocol + AutoDevSource with budget guard`
- `feat: query builder from requirement profiles`
- `test: recorded auto.dev fixtures + mocked client tests`
- `feat: incremental sync with known-VIN stop + weekly full pass`
- `feat: end-to-end pipeline sync->sticker->score`
- `chore: marketcheck adapter stub (disabled)`
- Tag: `m3`

---

## Milestone 4 — OEM Build Data evaluation + cross-reference reconciler

Tasks:
1. Normalize Build Data response into the same attribute shape as the sticker parser
2. **Cross-reference report** on the fixture VINs: build-data vs window-sticker, field by field. Decision gate:
   - Agrees + complete → build data becomes primary structured source, sticker spot-checks continue
   - Gaps/disagreements → sticker stays authoritative, build data demoted to hint
3. Reconciler with explicit precedence: `window sticker > OEM build data > listing structured fields > listing free-text (claimed)`; conflicts logged and surfaced in UI, never silently resolved
4. Check Build Data availability/cost on Starter tier (docs are vague; the fixture session answers this empirically)

Commits:
- `feat: OEM build data normalizer`
- `feat: cross-source reconciler with precedence + conflict log`
- `docs: build-data vs sticker evaluation results + decision`
- Tag: `m4`

---

## Milestone 5 — AI fallback (Haiku, optional path)

Only runs when sticker AND build data are both unavailable for a VIN.

Tasks:
1. Haiku extraction: dealer description blob → strict JSON (`{feature: claimed|denied|not_mentioned}`) against the profile's attribute list; temperature 0; response cached by `hash(listing_text)`
2. Results always tagged `claimed` — can raise a soft score, can never satisfy a hard requirement
3. Sticker-parse fallback: low-confidence PDFs escalated to a vision call (rare)
4. Spend recorded against `MONTHLY_AI_BUDGET_USD` in BudgetGuard

Tests: mocked API; JSON-schema validation of outputs; cache-hit tests; prompt fixtures.

Commits:
- `feat: haiku claimed-feature extraction with cache + spend guard`
- `feat: vision fallback for low-confidence stickers`
- Tag: `m5`

---

## Milestone 6 — FastAPI UI + scheduler + Docker

Tasks:
1. UI: ranked match list per model (score, price, miles, color chip, verification badges); car detail (requirement breakdown, provenance per attribute, highlight badges, price-history sparkline, sticker PDF link); "new since last visit"; budget page; conflicts page
2. APScheduler: Mon/Wed/Fri sync + weekly full pass; "Sync now" button; `carfinder sync` one-shot CLI (works without the server for Task Scheduler use)
3. Docker: `python:3.12-slim`, compose file, volumes `./data ./config ./stickers`, `.env` for keys, healthcheck
4. Optional ntfy/Discord webhook when a car scores above threshold

Commits:
- `feat: fastapi ui (matches, detail, budget, conflicts)`
- `feat: apscheduler jobs + sync CLI`
- `feat: dockerfile + compose + volumes`
- `feat: webhook notifications`
- Tag: `m6` / `v1.0`

---

## Milestone 7 — Polish (as needed)

Sold-car outcome tracking (what did matching cars actually sell for), CSV export, backup docs, profile hot-reload.

---

## Milestone 8 — Non-GM makes: BMW M2 (G87) + Toyota GR Supra 3.0 Premium

Added 2026-07-24. Extends the tool to its first non-GM models while keeping
the verified/claimed provenance discipline intact. Scope decisions (owner):
manual transmission HARD on both; M2 = G87 only (2023+, no F87, no CS —
the CS needs no forbid: automatic-only and priced beyond the hard bound);
Supra = 3.0 Premium, which with the manual requirement implies 2023+.

**Research findings (real-VIN + NHTSA vPIC verified, 2026-07-24):**
- G87 M2 WMI is `3MF` (created for the G87; F87 was WBS). VDS `13DM0`
  (2023–24) / `23DM0` (2025+) are the only registered M2 patterns.
  Transmission is NOT VIN-encoded.
- Supra WMI `WZ1` (Graz, registered to BMW AG). Pos 6 = engine (3.0 vs 2.0,
  verified); pos 7 era char: `C` = 2020–22 (all automatic — verified) vs
  `G` = 2023+ (manual/auto share the pattern). "Premium" is not VIN-encoded.
- Check digit validation holds for both imports (49 CFR 565).

**Evidence-source matrix (the M8 core):**

| Layer | GM | BMW / Toyota |
|---|---|---|
| VIN facts (vin.py) | verified | verified (patterns above) |
| Window sticker | verified, per-VIN | none available (free) |
| Parts-catalog decode | gmpartsgiant = true per-VIN build sheet, verified | partsdeal siblings = CATALOG lookup, **claimed-only** |
| Dealer text / AI | claimed | claimed |

The partsdeal demotion is fixture-proven (tests/fixtures/partsdeal): the
2023 A91-MT manual and 2023 Premium automatic return identical,
self-contradictory catalog attributes (ATM + "6-Speed Manual Floor Shift";
Trim "BASE" on a Premium). Nothing from that source may verify or fail a
hard requirement; its self-contradictory transmission attributes are
dropped even from claimed text.

**Consequence:** M2 manual and Supra manual/Premium (2023+) are not
machine-verifiable today — candidates land in needs_review, ranked by
score, for human confirmation (window-sticker photo, dealer call). The
VIN fast path still verified-excludes every 2020–22 Supra (auto) and
every F87 for free. Watch items: whether the M2 CS gets its own VDS
pattern, and whether Toyota's katashiki (logged per VIN) ever splits
2023+ trims.

**Budget:** market research puts both models at ~300–700 VINs national —
full passes ≈ 10–30 calls/model/source, incrementals ~1–3. July's Auto.dev
budget was nearly exhausted at build time (791/800), so the first pull is
a `sync --max-pages 1` test (20 rows = 1 call per model) plus
`marketcheck-test` dry-runs to pin the model-name taxonomy ("Supra" vs
"GR Supra"; M2 trim strings are useless — "Base").

Commits: `feat(vin)`, `feat(sources)`, `feat(pipeline)`, `feat(profiles)`,
`fix(partsdeal)` (the provenance demotion). Tag: `m8`.

---

## Feature — factory special editions (2026-10-02)

Badge + flat **+5** for named factory editions (2022 IMSA GTLM C8.R, 2023 70th
Anniversary, 2023 Watkins Glen / Sebring / Road Atlanta IMSA Blackwings, 2024
Mondrian trio, 2025 Petit Pataud, Supra A91-MT / 45th / Final Edition, M2 Turbo
Design Edition). Deterministic catalog in `editions.py`, gated by VIN scope
(model line + year), evidence order RPO code > sticker text > VIN serial block
> complete-sticker "no" > listing headline text (claimed). The +5 sits outside
the highlight cap (max total 120) and outside `highlight_bonus` (unicorn bar
unchanged). Research, corpus counts, sources, rejected lookalikes:
`docs/special_editions.md`.

---

## Call budget projection

| Activity | Calls/month |
|---|---|
| Fixture capture (one-time, M3) | ~15 |
| Incremental sync 3×/wk, 2 GM models | ~60–100 |
| Weekly full pass, 2 GM models | ~80–120 |
| Incremental + weekly full, M2 + Supra (M8; small markets) | ~60–150 |
| Sticker fetches | 0 (GM direct) |
| Live smoke tests | ~5 |
| **Total vs 1,000 free (cap 800)** | **~220–390** |

---

# Highlight (bonus) YAML — first pass

Scale 0–10 per item; scorer caps total highlight contribution at +10. Codes marked `# VERIFY` get corrected in M2 against real stickers — text patterns are the safety net either way. Items intentionally scored 0–1 are listed so they render as badges without influencing rank.

## C8 Corvette Stingray (`config/c8_stingray.yaml` excerpt)

```yaml
highlights:
  # --- Genuinely valuable ---
  - rpo: E60
    sticker_text: "FRONT LIFT"
    label: "Front lift w/ memory"
    bonus: 8        # ~$2K option; saves the nose on every driveway; strong resale
  - rpo: UQT        # VERIFY - PDR code on C8
    sticker_text: "PERFORMANCE DATA"
    label: "Performance Data Recorder"
    bonus: 3        # confirm whether your 2LT requirement already implies it
  - sticker_text: "COMPETITION SPORT.*SEAT|COMPETITION SEAT"
    label: "Competition Sport seats"    # VERIFY code (likely AE4-family)
    bonus: 5        # ~$1,500 seat upgrade; polarizing for touring comfort — tune to taste
  - rpo: NPP
    sticker_text: "PERFORMANCE EXHAUST"
    label: "Performance exhaust"
    bonus: 4        # included with Z51, so mostly redundant given your hard req; kept for non-Z51 audits
  - rpo: 5VM        # VERIFY
    sticker_text: "CARBON FIBER GROUND EFFECTS|VISIBLE CARBON FIBER"
    label: "Visible carbon fiber ground effects"
    bonus: 5        # ~$5K option, real money, real curb appeal
  - sticker_text: "CARBON FIBER.*INTERIOR|INTERIOR.*CARBON"
    label: "Carbon fiber interior trim"
    bonus: 3
  - sticker_text: "HIGH WING|CARBON FLASH SPOILER"
    label: "High wing / upgraded spoiler"
    bonus: 2

  # --- Nice-ish, mostly cosmetic ---
  - sticker_text: "RED.*CALIPER|YELLOW.*CALIPER|ORANGE.*CALIPER|EDGE RED CALIPER"
    label: "Painted calipers"
    bonus: 1
  - sticker_text: "SEAT BELT.*(RED|BLUE|YELLOW|ORANGE)"
    label: "Colored seat belts"
    bonus: 1
  - sticker_text: "STITCH"
    label: "Custom stitch package"
    bonus: 1

  # --- Throwaway (badge only, zero rank effect) ---
  - sticker_text: "CARGO NET"
    label: "Cargo net"
    bonus: 0
  - sticker_text: "WHEEL LOCK"
    label: "Wheel locks"
    bonus: 0
  - sticker_text: "SPLASH GUARD|FIRST AID|BATTERY PROTECTION|LUG NUT"
    label: "Dealer-pack accessories"
    bonus: 0
```

Notes: CC3 transparent roof and engine appearance packages are coupe-relevant and omitted (convertible hard req). MagneRide (FE4/FE2), Z51, 2LT, convertible are hard requirements, not highlights.

## Cadillac CT4-V Blackwing (`config/ct4v_blackwing.yaml` excerpt)

```yaml
highlights:
  # --- Genuinely valuable ---
  - rpo: CFZ
    sticker_text: "CARBON FIBER 1|CARBON FIBER PACKAGE 1"
    label: "Carbon Fiber Package 1"
    bonus: 8        # $4,350 new: splitter, rockers, rear spoiler; the signature BW option
  - rpo: CFV
    sticker_text: "CARBON FIBER 2|CARBON FIBER PACKAGE 2"
    label: "Carbon Fiber Package 2"
    bonus: 6        # requires Pkg 1; combined $7K car = heavily optioned example
  - sticker_text: "SUEDED MICROFIBER|SUEDE.*STEERING|ALCANTARA"
    label: "Sueded microfiber package"
    bonus: 3
  - sticker_text: "SUNROOF"
    label: "Power sunroof"
    bonus: 3
  - sticker_text: "PERFORMANCE DATA"
    label: "Performance Data Recorder"
    bonus: 2        # HUD (your hard req) required PDR on the order guide, so
                    # most HUD cars carry it — expect this to light up often
  - sticker_text: "MASSAG"
    label: "Massaging seats"
    bonus: 2        # rides along in the seat package that brings your ventilated-seat req

  # --- Cosmetic ---
  - sticker_text: "BRONZE.*CALIPER|RED.*CALIPER|BLUE.*CALIPER"
    label: "Painted calipers"
    bonus: 1
  - sticker_text: "TORCH RED SEAT BELT|RED SEAT BELT"
    label: "Red seat belts"
    bonus: 1
  - sticker_text: "BLACK.*WHEEL|POLISHED.*WHEEL|DIAMOND CUT"
    label: "Upgraded wheel finish"
    bonus: 1

  # --- Throwaway ---
  - sticker_text: "CARGO MAT|CARGO NET|ILLUMINATED.*SILL|WHEEL LOCK|LUG"
    label: "Dealer-pack accessories"
    bonus: 0
```

Notes: Super Cruise is automatic-only and dies on your manual hard requirement — omitted. Ventilated seats + HUD are hard requirements; the parser should match them via sticker text ("VENTILATED", "HEAD-UP DISPLAY") since they arrive inside packages whose codes vary by year.

---

# Requirement profiles (hard/soft seed — for reference)

**C8:** hard = model Corvette Stingray (exclude Z06/E-Ray trims), 2022+, convertible, 2LT or 3LT, Z51, MagneRide (FE4 or FE2). Soft = miles ideal ≤50K / hard ≤60K (w 0.20), price ideal ≤$65K / hard ≤$70K (w 0.25), color preferred blue / penalized black·white·red (w 0.10).

**Blackwing:** hard = CT4-V Blackwing, 2022+, 6-speed manual, ventilated seats, HUD. Soft = miles ideal ≤50K / hard ≤60K, price ideal ≤$60K / hard ≤$65K (his "for sure less than" → hard bound), color same as C8.

---

# Working agreement

- No live API call outside BudgetGuard, ever
- `pytest` default = fully offline; live tests opt-in and counted
- Conventional commits; tag per milestone; each milestone independently runnable
- Fixtures (JSON responses, sticker PDFs) committed to the repo — they're the contract tests against provider drift
