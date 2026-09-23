# Write-ahead capture

The intent to act is written before the act, so a crash between the two is recoverable rather than invisible.

## The rule

A step writes its intent note and first heartbeat to the record before it mutates anything. If it dies in the gap, a successor reads the intent and knows a mutation may be half-done; if the intent were written after, the gap would be silent and unrecoverable.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
`alpaca/claims.py` (claim-before-mutate) and `alpaca/token.py`.
