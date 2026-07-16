# Car Finder — How to use

A practical guide: one-time setup, day-to-day use, what's safe, and how to
tune the search. For the design, see [PLAN.md](../PLAN.md); for a command
reference, `uv run carfinder --help`.

---

## 1. One-time setup

You only do this once.

### Option A — local (uv)

```bash
uv sync                      # create the .venv and install everything (Python 3.12)
cp .env.example .env         # then open .env and fill in your keys (below)
```

### Option B — Docker

```bash
cp .env.example .env         # fill in keys
docker compose up --build    # builds the image and serves on http://localhost:8000
```

### Fill in `.env`

| Key | Needed for | Notes |
|-----|-----------|-------|
| `AUTODEV_API_KEY` | Finding listings (the only paid quota) | Free Starter tier: 1,000 calls/mo. Required for `sync`. |
| `ANTHROPIC_API_KEY` | AI fallback on cars with no window sticker | Optional. Sub-cent per car; hard-capped at `$2/mo`. |
| `NOTIFY_WEBHOOK_URL` | Push alerts on a great match | Optional. An [ntfy](https://ntfy.sh) topic URL or a Discord webhook URL. |
| `NOTIFY_THRESHOLD` | Alert score cutoff (0–110) | Default `85`. |

**`.env` is git-ignored — never commit it.** Everything else has sane defaults.

That's the whole setup. There's no database to create — it's built automatically
on first run.

---

## 2. Seeing the latest cars

Two moving parts: a **sync** pulls fresh listings and scores them; the **web UI**
shows the results.

### The web UI

```bash
uv run carfinder serve       # → http://127.0.0.1:8000   (Docker: already running)
```

- **Home** — each model with ranked / needs-review / excluded counts.
- **Matches** (click a model) — ranked best-first, with a **"new"** badge on cars
  you haven't seen since your last visit (tracked by a browser cookie, per model).
- **Car detail** — every hard requirement with a ✓/✗ and a *verified / claimed /
  unknown* tag, the soft-score bars, highlight badges, price history, any
  cross-source conflicts, a **view listing ↗** link to the dealer's page, and a
  link to the actual **window-sticker PDF**.
- **Budget** — how much of each month's quota you've used.
- **Conflicts** — where a listing disagreed with the window sticker.

Browsing needs **no API keys** — it only reads the local database.

### Running a sync

- **From the UI:** click **Sync now** (top-right, or on the home page's
  **Actions** row, which also has **Full pass** and **Rescore** buttons). The job
  runs in the background and a **live status pill** in the header shows what it's
  doing in real time (which page it's fetching, which car it's scoring). When it
  finishes, the page refreshes itself so you're looking at the latest results;
  if it fails (e.g. a budget cap), the pill turns red with the reason.
- **From the terminal (headless — no server needed):**
  ```bash
  uv run carfinder sync          # incremental: cheap, grabs new listings
  uv run carfinder sync --full   # weekly full pass: price drops + delist detection
  ```
- **Automatically:** `carfinder serve` also starts a scheduler — **Mon/Wed/Fri**
  incremental syncs and a **Sunday** full pass. (Disable with `carfinder serve
  --no-scheduler`.) For a headless box, point Windows Task Scheduler or cron at
  `carfinder sync` instead.

**What a sync actually does:** find new/changed listings → for genuinely new cars,
fetch the free GM window sticker → score against your requirements → save. A car
that fails a *verified* hard requirement is **excluded**; one that only has
unconfirmed (claimed) evidence lands in **needs review**; the rest are **ranked**.

---

## 3. Money & data safety

The design goal is that you *cannot* accidentally overspend. Still, know these:

### Spend

- **Only Auto.dev costs real quota** (1,000/mo free; hard-capped at 800 by
  `BudgetGuard`). GM window stickers are free. Anthropic is capped at `$2/mo`.
- **`sync --full` is the expensive one** — it walks every page. A C8 full pass is
  roughly **85–90 Auto.dev calls** (there are ~1,700 in-band listings). An
  **incremental** sync stops as soon as it hits cars it already knows, so it's
  usually just a handful of calls. **Don't loop `--full`** — once a week is the
  intent, which is exactly what the scheduler does.
- Every call is recorded and hard-capped. If you hit a cap mid-sync, the sync
  **stops cleanly** rather than overspending — check the **Budget** page.
- Watch spend anytime: `uv run carfinder budget`.

### Data loss

- **`data/carfinder.sqlite` is your whole history** — scores, price history,
  which cars are new, sticker outcomes. It's git-ignored. **To back up, just copy
  that file.** Deleting it isn't catastrophic (a sync rebuilds current data) but
  you lose price history and "new since" tracking.
- Under Docker the DB lives in the `./data` volume — don't `docker compose down
  -v` unless you mean to wipe it (`-v` deletes volumes).
- **Cached window stickers** (`stickers/*.pdf`) are safe to delete — they just
  re-fetch (one free GM call each). The two committed fixture PDFs are contract
  tests; leave those.
- Nothing the tool does is destructive to the outside world — it only reads
  listings and posts to *your own* webhook (if you set one).

---

## 4. Adjusting your preferences

Your requirements live in plain YAML — **[config/c8_stingray.yaml](../config/c8_stingray.yaml)**
and **[config/ct4v_blackwing.yaml](../config/ct4v_blackwing.yaml)**. Edit, then
**rescore** (see below).

Common tweaks:

```yaml
soft:
  - key: price
    kind: numeric
    attr: price
    ideal: 65000      # at/below this = full marks
    hard: 70000       # at this = zero marks for the price dimension
    weight: 0.25      # how much price matters vs miles vs color
  - key: color
    kind: color
    preferred: [blue]                 # 100 points
    penalized: [black, white, red]    # 0 points  (everything else = 50)
```

- **Hard requirements** (`hard:`) are pass/fail gates — a car missing one is
  excluded. These only pass on *verified* (window-sticker) evidence.
- **Soft requirements** (`soft:`) are weighted 0–100 curves — miles, price, color.
- **Highlights** (`highlights:`) are bonus badges (e.g. front lift); the total
  bonus is capped at +10 so trinkets can't outweigh fundamentals.
- **Server-side prefilter** (`query:`) sets make/model/year/price bounds sent to
  Auto.dev — kept deliberately *wider* than your hard bounds so nothing good is
  filtered out before scoring.

### Apply your changes with `rescore`

Click **Rescore** on the home page, or run:

```bash
uv run carfinder rescore
```

This re-ranks **every car already in your database** against the edited profiles.
It uses cached sticker/AI data, so it makes **~zero API calls** and needs no
Auto.dev key — tune freely and re-run as often as you like. (A sync only re-scores
*new/changed* cars, so `rescore` is what applies a preference change to the cars
you already have.)

> A malformed YAML is rejected with a clear error and nothing is changed, so a
> typo can't corrupt your data — fix it and re-run.

---

## 5. Adding a new car to the search

To track a third model, add `config/<your_model>.yaml`. The quickest path is to
copy an existing profile and change:

1. `name` and `model_key` (a unique lowercase id, e.g. `mustang_gt`).
2. The `query:` block — `make` and `model` **exactly as Auto.dev spells them**
   (that's how we learned Blackwing is `"CT4-V"`). If a sync returns nothing,
   the make/model string is the first thing to check.
3. `hard` / `soft` / `highlights` for what you want.

Then `uv run carfinder sync` to pull it and `uv run carfinder rescore` if you keep
editing. It appears on the home page automatically.

Two caveats for a brand-new model:

- **Paint colors**: color *scoring* works from the listing's color text out of the
  box, but exact paint-code → color mapping needs a per-model table in
  `src/carfinder/colors.py`. Without it, color still works (via text) — it's just
  less precise. Ask me and I'll add the table.
- **Window stickers**: the GM sticker endpoint only covers **GM vehicles, model
  year 2022+**. A non-GM model won't have verified stickers, so its cars will lean
  on listing/AI (claimed) data and mostly land in *needs review* rather than
  *ranked*. That's the tool being honest, not broken.

---

## Quick reference

| I want to… | Do this |
|---|---|
| See current matches | `carfinder serve` → http://127.0.0.1:8000 |
| Pull the latest listings | **Sync now** button (or `carfinder sync`) |
| Full refresh (price drops, sold cars) | **Full pass** button (or `carfinder sync --full`, weekly) |
| Open the actual dealer listing | car detail page → **view listing ↗** |
| Change what I'm looking for | edit `config/*.yaml`, then the **Rescore** button (or `carfinder rescore`) |
| Add a model | new `config/<model>.yaml`, then `carfinder sync` |
| Check my spend | `carfinder budget` |
| Back up everything | copy `data/carfinder.sqlite` |
| Fetch one car's sticker manually | `carfinder fetch-stickers <VIN>` |
