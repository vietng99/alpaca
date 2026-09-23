# Concurrency tiers

This leaf states the rule that the concurrency half of `alpaca/resilience/budget.py` enforces. The
code is the mechanism; this leaf is the reason, so the two never drift. It is net-new to M3.12:
concurrency tiers have no predecessor module, only doctrine.

## The rule

How many workers a run may hold live at once is bounded, and the bound is a per-class number read
from `project.yaml`. A run may not exceed its tier. Unbounded fan-out is the concurrency analogue
of the unbounded loop: it spends a run's whole allowance in parallel, contends on the same rows,
and multiplies the blast radius of any single misbehaving worker. The tier is the ceiling that
keeps parallelism proportionate to the run's resource class.

## Where the number comes from

The tier is DATA in `project.yaml`, never a constant in code:

- `concurrency.tiers[<resource_class>]` is the maximum number of rows a run of that class may
  hold under a live claim at once.

A small run holds one, a medium run a few, a large run more. The exact numbers are the project's
to set; the mechanism only reads them. Reading the tier from `project.yaml` is what lets a project
size its own parallelism without a code change, the same discipline budget survival keeps for the
reserve.

## How live concurrency is measured

Live concurrency is not a counter a worker increments; it is a fold over the record. A row is in
the run's live concurrency when it currently holds a live claim (a claim whose lease has not
passed). The count of such rows is the run's live concurrency at that instant. Measuring it from
the board derivation over the record, rather than from a shared counter, means a crashed worker's
claim ages out of the count on its own (its lease passes), and a resumed run reads the true live
count, never a stale one.

## The check

- `live_worker_count <= tier` -> within the tier. A new claim may be taken.
- `live_worker_count > tier` -> the tier is breached. The run must not take another claim.

The tier is a ceiling on live claims, so a run at its tier waits for a claim to be released or to
expire before it fans out further.

## Relationship to the other floors

The concurrency tier is one of the preflight floors (see `doctrine/preflight-floors.md`): a run
over its tier does not clear preflight. It sits beside budget survival: budget survival bounds how
much a run may spend in total, concurrency tiers bound how much it may run at once. A run must
respect both.
