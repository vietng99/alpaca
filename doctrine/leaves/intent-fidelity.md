# Intent fidelity

An agent may not invent intent and call it the owner's: the signed brief, the owner's answers and the owner's orders are the authority, and an agent's own choice is labelled as the agent's.

## The rule

Intent questions and the big-choice classes go to the owner in one batch at the start of an op, each with the agent's proposal marked as a proposal. The owner answers yes, no, or with their own answer. An unanswered item counts as no: while one is open, an L5 or L6 run of an op asked this way refuses to start, and the refusal names the items.

Each done-bar, input and expected line of a task contract has one origin: a quote of the signed brief or of an owner message, checked byte for byte; a recorded decision; a derived choice with a one-line reason; or an agent proposal. A task an agent adds is recorded with the agent as actor. No checker may cite an agent-proposed line as owner authority.

At L5, a choice that falls outside the signed brief and runbook raises an owner card, and a task the card names waits while the card is unanswered; other tasks go on. At L6 such a choice does not stop for the owner: at each phase door of an op asked this way, a separate agent is given the signed brief, the runbook, the owner decisions and the deliverable, never the builder's notes, and checks them against each other. A mismatch sends the work back for rework without asking the owner, and a choice the agent made on its own is recorded as derived with its reason, so the owner can read it later.

An owner order such as "when finish design phase, pause" becomes a record row. A phase door does not open while an open order applies, at every level, L6 included, and a handoff note that drops an open order fails its check.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
`alpaca/batch.py`, `alpaca/taskcontract.py` with `alpaca/gates/quote_check.py`,
`alpaca/ownerword.py`, `alpaca/oob.py`, `alpaca/review.py`, `alpaca/selfcheck.py`,
`alpaca/orders.py` and `alpaca/intentgate.py`.
