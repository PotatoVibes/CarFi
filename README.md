# car-finder

Personal tool to find a C8 Corvette Stingray or Cadillac CT4-V Blackwing meeting
strict, VIN-verified requirements. Deterministic-first; AI only where unstructured
text forces it. Full design in [PLAN.md](PLAN.md).

## Setup

```bash
uv sync                      # create .venv and install deps (Python 3.12)
cp .env.example .env         # then fill in keys
uv run pre-commit install    # optional: enable lint hooks
```

## Usage

```bash
uv run carfinder budget                      # month-to-date API usage per provider
uv run carfinder fetch-stickers VIN [VIN…]   # cache-first GM window sticker fetch
uv run carfinder sync                        # one-shot sync over every config profile
uv run carfinder sync --full                 # weekly full pass (price drops + delist)
uv run carfinder serve                       # web UI + scheduler at http://127.0.0.1:8000
```

`carfinder sync` runs headless — point Windows Task Scheduler or cron at it if you
don't want the server running. `carfinder serve` also starts the APScheduler jobs
(Mon/Wed/Fri incremental, Sunday full pass) unless you pass `--no-scheduler`.

The web UI (browse without any API key): ranked matches per model, car detail with
per-attribute provenance and the window-sticker PDF, a budget page, and a conflicts
page. Optional ntfy/Discord alerts fire when a car crosses `NOTIFY_THRESHOLD`.

## Docker

```bash
cp .env.example .env         # fill in keys
docker compose up --build    # serves on http://localhost:8000
```

Volumes: `./data` (SQLite DB), `./stickers` (cached PDFs), `./config` (profiles,
read-only). Container healthcheck hits `/health`.

## Testing

```bash
uv run pytest                # default profile: fully OFFLINE, zero network calls
uv run pytest -m live        # opt-in live tests (counted against budget) — ask first
```

## Hard rules

- No live API call may bypass `BudgetGuard`. Auto.dev free tier = 1,000 calls/mo;
  hard cap 800.
- Never commit `.env`. API keys live in `.env` only.
- Work one milestone at a time, in PLAN.md order.
