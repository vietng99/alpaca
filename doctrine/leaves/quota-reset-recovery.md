# Quota-reset recovery

A run waits out a quota reset from the real reset header, not a guessed delay, then resumes cost-aware.

## The rule

Reasoning from an estimated wait strands a run too early or too late. Recovery reads the reset time the provider actually reports, holds until it, and admits the next step only when the budget allows, so the resume is honest rather than hopeful.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
`alpaca/posture/` admission control.
