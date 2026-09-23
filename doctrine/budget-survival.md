# Budget survival

This leaf states the rule that `alpaca/resilience/budget.py` enforces. The code is the mechanism;
this leaf is the reason, so the two never drift. It is net-new to M3.12: budget survival has no
predecessor module, only doctrine.

## The rule

An unattended run must not die mid-op because it ran out of allowance. Running out of allowance
in the middle of an op is the worst failure an unattended run can suffer: the work is half-done,
the record is half-written, and there is no human at the keyboard to notice and restart. The rule
that prevents it is simple: a run keeps a reserve, and when its remaining allowance falls to or
below that reserve, the run enters HOLD rather than starting work it cannot finish.

HOLD is a pause, not a death. A held run stops taking new work, surfaces that it is holding, and
waits. When the allowance is topped up (a new quota window, a raised budget) the run resumes from
the record where it left off. Nothing is lost, because nothing half-finished was ever started.

## Where the numbers come from

The two numbers are DATA in `project.yaml`, never constants in code:

- `budget.totals[<resource_class>]` is the total allowance for a run of that class. The run's
  class is its own `resource_class` key. A larger class gets a larger total.
- `budget.reserve` is the floor the run keeps in hand. It is the same for every class.

Reading them from `project.yaml` is what lets a project size its own survival margin without a
code change, and it is what the M3.12 done-when means by "read the resource class and the reserve
from `project.yaml`, never from code". A run that read its total or its reserve from a constant
would be a run whose survival margin could not be tuned per project, which is exactly the drift
this rule forbids.

## Where the spend comes from

Spend is recorded as append-only `budget-spend` events on the record. The remaining allowance is a
fold over those events: `total - sum(spend)`. Because the spend lives in the append-only record,
a crash and resume reads the same remaining allowance, never a reset that would let a run spend
past its reserve after a restart.

## The posture

- `remaining > reserve` -> GO. The run may take work.
- `remaining <= reserve` -> HOLD. The run pauses at the floor and waits.

The boundary is at-or-below, not strictly-below: a run that would land exactly on its reserve is
already at the floor and holds, so the reserve is genuinely kept, never spent down to zero.

## What this rule does not do

This leaf sets the posture. It does not decide how a topped-up allowance is signalled, nor how a
held run is woken. That is the quota-window pause and auto-resume, a skill-side mechanism bundled
in the skills plugin and mirrored into the record as events. Budget survival only answers "may
this run take work right now, or must it hold?".
