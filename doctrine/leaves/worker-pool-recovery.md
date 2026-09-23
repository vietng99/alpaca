# Worker-pool recovery

A worker that dies mid-hold releases its claim by lease expiry; the pool refills without a stranded step.

## The rule

A crashed worker must not wedge the step it held. Its claim carries a lease that ages out on its own, so a replacement worker sees the reference free once the lease passes and picks it up without a human breaking a lock.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
the lease-expiry path in `alpaca/claims.py` and `alpaca/resolve.py`.
