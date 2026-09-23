# Human decision gate

A boundary that only a human may cross pauses for a recorded decision, exit 3, never an auto-advance.

## The rule

Some boundaries are human decisions at every autodrive level: ship, an irreversible external action, an authority-surface change above the line. The gate pauses and surfaces the decision as a PAUSED-FOR-DECISION row rather than deciding on the agent's own authority.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
`alpaca/decisions.py` and the PAUSED verdict in `alpaca/gates/verdict.py`.
