# Halt on drift

When the subject under check changes underfoot, the run halts rather than reporting a stale verdict.

## The rule

A cached verdict is valid only while the bytes it was computed over are unchanged. A sentinel re-derives the subject digest and halts the moment it drifts in either direction, so a green from before an edit is never presented as a green after it.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
`alpaca/gates/monotonicity.py`.
