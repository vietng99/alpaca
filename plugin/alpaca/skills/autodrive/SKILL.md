---
name: autodrive
description: Set the current Alpaca session autonomy level when the owner requests it.
---

# Alpaca session autonomy

Use the active session ID recorded by the operator adapter. Record a requested level with `bin/alpaca --session SESSION_ID session level L2 --goal "authorized scope"`, replacing L2 with the requested L1 through L6. Read current posture from `bin/alpaca status`; never inherit another account's or project's level. For OFF, run `bash plugin/alpaca/hooks/autodrive-set.sh --session SESSION_ID OFF`; this checkpoints an OFF note and ends the active session without changing its recorded level. Stop autonomous work. A running backend job remains observable and is not killed implicitly.

Explicit owner scope, host permissions, and the project's acceptance criteria remain binding at every level. A level change never authorizes relaxed acceptance, changes to protected inputs, or an external publication. Both operators use the same project-local record.

At L6 the Stop hook refuses a top-level stop until the run records a done marker, with one exception: a stop caused by the account usage limit is always released and recorded as a `limit-stop` event. The limit ends the turn to protect the run, so the session parks, continues after the reset, and hands off from its last checkpoint. Keep the checkpoint current during an L6 run.
