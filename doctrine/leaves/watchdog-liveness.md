# Watchdog liveness

A long run proves it is alive by a heartbeat; a missed beat is a liveness fault the record shows.

## The rule

A silent process cannot be told from a dead one. A watchdog reads a heartbeat the run writes to the record and reports a missed beat as a liveness fault, so a stalled campaign surfaces instead of hanging unseen.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
the heartbeat rows in `alpaca/claims.py`.
