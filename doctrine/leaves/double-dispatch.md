# Double dispatch guard

Two live sessions cannot silently run the same step; a claim on the record makes the collision visible.

## The rule

Crash-only resume plus a shared pad means two sessions can reach for one step. The guard is an optimistic claim in the record, taken before the first mutation, so a second session sees a live holder rather than free work and records the collision instead of clobbering it.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
`alpaca/claims.py`.
