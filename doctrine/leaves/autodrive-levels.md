# Autodrive levels

One unified ladder names the autonomy postures; a level is granted by a human and never self-raised.

## The rule

Autonomy is a dial with named rungs from attended to pre-authorised. The level in force is a recorded decision every gate reads; an agent may drop to a safer rung any time but never raises its own, and a raise is one human decision, one name, one time.

At L6 the Stop hook refuses a top-level stop until an exit holds, so an unattended run ends only on a recorded reason. The run records one of two exits itself: `bin/alpaca session done --reason "..."` marks the goal reached and holds until the next level decision for the session; `bin/alpaca session gate <kind> --ref <id> --reason "..."` records an owner-only gate (`ship`, `spend`, `rounds`, `bar`, `reopen`, `signoff`, `contract`, `owner-action`), holds for that turn only and posts a blocker message to the owner. A halted thread, a usage-limit stop and a dropped level (`bin/alpaca session level OFF` or the owner's `/autodrive off`) also end the refusal.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
`alpaca/posture/level.py`; the L6 exits by `alpaca/posture/loops.py` (`stop_should_refuse`,
`mark_done`, `mark_gate`) and the `session done` and `session gate` verbs in `alpaca/operator.py`.
