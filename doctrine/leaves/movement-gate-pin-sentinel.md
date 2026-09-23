# Movement, gate, pin, sentinel

The gated-phase spine: work moves, a gate adjudicates, a pin fixes the verdict, a sentinel guards it against drift.

## The rule

A verification-heavy campaign is ordered so a successor never repeats a predecessor's mistake. Each primitive answers one question: movement is what changed, the gate is whether it passes, the pin is the fixed verdict on the record, and the sentinel halts if the pinned subject drifts.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
`alpaca/gates/monotonicity.py` and the door checks under `alpaca/gates/`.
