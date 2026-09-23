# Historian

Capture is dumb and continuous; sorting into a per-op record is a separate, cadenced pass, and nothing is lost in between.

## The rule

The historian separates capturing events from making sense of them. Raw events land in an unsorted bucket as they happen; a cadenced sort folds them into the per-op record and the day roll-up. The split keeps capture cheap and honest and makes sorting auditable.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
`alpaca/historian.py` and `alpaca/sort.py`.
