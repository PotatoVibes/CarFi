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
uv run carfinder budget      # month-to-date API usage per provider
```

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
