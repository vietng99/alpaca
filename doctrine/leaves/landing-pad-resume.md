# Landing pad and resume

Every session lands on one rendered pad that says where the project is and what is owed; resume reads the pad, it does not race.

## The rule

The first thing a session reads is the landing pad the record renders: the open ops, the claimed rows, the owed decisions, the resume state. A resuming session acts from the pad rather than guessing, and it never takes over a step the pad shows as live.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
`alpaca/pad.py` and `alpaca/token.py`.
