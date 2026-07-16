# car-finder
Personal tool to find a C8 Corvette or CT4-V Blackwing meeting strict,
VIN-verified requirements. Full design in PLAN.md — read it before any work.

## Hard rules
- NO live API call may bypass BudgetGuard. Auto.dev free tier = 1,000 calls/mo;
  hard cap 800. Do not write or run ad-hoc scripts that hit api.auto.dev directly.
- `pytest` default profile is fully offline. Live tests only behind `-m live`,
  and only with explicit user approval per session.
- Never commit .env. API keys live in .env only.
- Python 3.12 via uv. Conventional commits. Tag each milestone (m0, m1...).
- Work one milestone at a time, in PLAN.md order. Ask before starting the next.
- Window sticker PDFs and recorded API fixtures ARE committed (they're contract tests).

# Guidelines
- Any call to an LLM should be structured to maximize cache hit rates. The system prompt should not be artificially inflated to reach the caching minimum for a given model, but the overall query should be assembled in such a way that if it becomes long enough to get cached, it will do so in the best possible way.
- If a docker container for this project is already running, don't start a new one on top of it. Either use the existing container or rebuild it if necessary. Don't stack them on top of each other.