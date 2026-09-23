# Heartbeat and the OS kicker

Re-entry is guarded across a restart by an account-independent timer, not by a lock a crash could strand.

## The rule

A cross-session correctness claim needs a beat that survives the account that made it. An operating-system timer re-derives liveness independent of any one session, reinforcing the record's claim rather than replacing it, so a zombie hold is detected and recycled.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
the lease and heartbeat fields in `alpaca/claims.py`.
