# Level-gated advance

What an agent may decide next is gated by the recorded level in force, and the gate reads the level from the record, not the environment.

## The rule

Every boundary reads the autodrive level from a decision row on the record before it advances. A higher rung widens what may be decided without a human; a lower one narrows it. Because the gate reads the record, the level cannot be spoofed by an environment variable.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
`alpaca/posture/level.py` and `alpaca/decisions.py`.
