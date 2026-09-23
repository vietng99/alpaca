# Context clearing

An agent evicts spent context in-window the same way file-as-truth evicts it across a boundary: keep the pointer, drop the body.

## The rule

Loading everything and holding it crowds out the budget the work needs. The rule that a return message keeps a pointer and drops the body applies one level lower, to a single agent's own window: once a file is written and pointed at, its body is dropped from context and re-read on demand.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
`doctrine/CORE-CARD.md` (the pointer card) and `alpaca/pad.py`.
