# Preflight floors

This leaf states the rule that the preflight half of `alpaca/resilience/budget.py` enforces. The code
is the mechanism; this leaf is the reason, so the two never drift. It is net-new to M3.12:
preflight floors have no predecessor module, only doctrine.

## The rule

Before a run starts or resumes, it must clear its floors. A floor is a precondition that must hold
BEFORE work begins, checked against the record, not a check applied after the fact. The point of a
preflight floor is to hold a run at the gate it cannot pass rather than launch it into a failure it
cannot recover from unattended.

## The floors

- **Budget floor.** The budget posture must be GO (see `doctrine/budget-survival.md`). A run whose
  remaining allowance is at or below its reserve is held at the floor, not started: starting it
  would be starting work it cannot finish, the mid-op death budget survival exists to prevent.
- **Concurrency floor.** The concurrency tier must not be breached (see
  `doctrine/concurrency-tiers.md`). A run already at or over its per-class tier does not take
  another claim.

A run clears preflight only when every floor holds. A run that fails any floor is held, with the
failing floors named, so the hold is legible: the report says which floor stopped the run, never
just that it stopped.

## Why floors, not caps

A floor is checked before work, and it holds a run back; a cap is noticed after work, and it stops
a run part-way. For an unattended run the difference is the whole point. A cap noticed mid-op
leaves the record half-written with no human to recover it. A floor checked at preflight keeps the
run from ever entering that state: it either clears the floor and runs cleanly, or it holds at the
floor and waits, and a held run at a floor is a clean, resumable state.

## Composition

Preflight floors compose the other two leaves. Budget survival and concurrency tiers each define
one floor; preflight is the gate that checks them together and refuses a run below either. The
three are read together, from the same `project.yaml`, so a project that tunes its reserve or its
tier tunes what its runs must clear to start, with no code change.

## What this rule does not do

Preflight decides whether a run may START. It does not watch a run once it is running (that is the
pulse watchdog, `doctrine`-side of `alpaca/resilience/pulse_watch.py`), and it does not decide how a
held run is later woken (that is the quota-window pause and auto-resume on the skill side). It is
the gate at the front door, nothing more.
