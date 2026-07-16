# OEM Build Data — evaluation & decision (M4)

**Date:** 2026-07-15
**Question (PLAN.md M4, task 4):** Is Auto.dev's OEM Build Data API usable on our
free tier, and should it become the primary structured source or stay a hint?

## Result: not available on the Free plan

`GET https://api.auto.dev/build/{VIN}` returns **HTTP 402 Payment Required** on
our current (Free/Starter) plan. The response is explicit:

```json
{
  "status": 402,
  "code": "FEATURE_NOT_AVAILABLE",
  "message": "This feature requires the Growth plan or above. You are currently on the Free plan.",
  "currentPlan": "Free",
  "requiredPlan": "Growth",
  "plans": {
    "Growth": {"price": "$299/mo", "features": "build, specs, marketvalue, recalls, tco, payments"},
    "Scale":  {"price": "$599/mo", "features": "All Growth + openrecalls, plate lookup, taxes"}
  }
}
```

(Captured verbatim in `tests/fixtures/autodev/build_402_unavailable.json`.)

Build Data — along with `specs`, `marketvalue`, `recalls`, `tco`, and `payments`
— is gated behind **Growth ($299/mo)**. That is far outside this project's
free-tier premise, so build data is **unavailable to us in practice**.

Calls spent answering this: **2** (both 402s), recorded through BudgetGuard
under the `autodev` bucket.

## Decision

**The window sticker remains the sole verified structured source.** Build data
is deferred, not adopted. This is the "gaps/disagreements → sticker
authoritative" branch of the M4 decision gate, reached because there is *no*
build data to agree with — not because of a measured disagreement.

Because we have zero agreement evidence, the reconciler treats build data
**conservatively as a hint (`claimed` provenance)** if it is ever wired in
(`facts_from_build(..., provenance=claimed)` by default). A future cross-
reference on a paid plan can promote it to `verified` once agreement with the
sticker is confirmed on real VINs.

## What was built anyway (ready for a paid plan)

The machinery is complete and tested, so adopting build data later is a config
flip, not a rewrite:

- **`oem.py`** — `OemBuildSource` (GET /build/{vin}, gated by BudgetGuard) and
  `normalize_build()` → `BuildFacts`, the same comparable attribute shape the
  sticker parser produces. 402/404 both normalize to `available=False`.
- **`reconcile.py`** — precedence merge
  `sticker > OEM build > listing structured > listing free-text`, with a
  conflict log (never silently resolved) and a `cross_reference()` diff for the
  build-vs-sticker agreement check the decision gate calls for.
- **Pipeline** logs sticker-vs-listing conflicts today (model year, color
  family) into the `conflicts` table for the UI; build data would slot in as
  the middle tier.

## If we ever upgrade

1. Set the plan; `OemBuildSource` needs no code change.
2. Run `cross_reference()` on the VINs we hold stickers for
   (`1G6D65RP8N0460286`, `1G1YC3D47P5121316`) plus any new ones.
3. If scalars + options agree and coverage is complete, promote build data to
   `verified` and let it fill gaps where a sticker is `not_found`/`blocked`.
   Otherwise keep it a hint. Record the outcome here.
