# Dumb capture, agentic sort, and nothing dropped

This leaf states the rule that `alpaca/wiki/ingest/drain.py`, `alpaca/sort.py` and `alpaca/historian.py`
enforce. The code is the mechanism; this leaf is the reason, so the two never drift.

## Two layers, one boundary

Capture and judgment are two separate acts, and the boundary between them is kept in code, not
only here.

- **Capture is dumb.** The drain lands every session event as a raw note. It assigns the note a
  role by a fixed mechanical lookup over the event kind (`intent`, `result`, or the neutral
  `note`), and it never decides what a specific note means, never pairs an intent with a result,
  and never guesses. An event kind it does not recognise becomes a `note`, not a forced guess at
  `intent` or `result`. Capture may never judge.
- **Sort is the one pass that judges.** `alpaca sort` groups the raw layer by op and pairs each intent
  note with its result note, writing that judgment as an assertion that points back to both notes.
  An intent with no result is surfaced as an unpaired assertion, never dropped.

The boundary holds on both sides in code. The capture path carries no classifier: role is a table
lookup. The sort verb refuses to be run as a background classifier: `alpaca sort --auto` is refused
(BLOCKED), because sweeping a whole backlog into assertions on its own would mechanise the very
judgment sort exists to make. Sort surfaces for a reading and writes assertions; it is not an
automatic sorter over history.

## Nothing is dropped: the never-drop unsorted bucket

Every raw note whose timestamp appears in no timeline is surfaced by name with a reason, never
removed. `alpaca/historian.py` is the completeness layer that proves this: it builds a per-op timeline
and cross-checks every note against the timelines. A note that lands in no timeline is unplaceable
and goes to the unsorted bucket with a reason:

- a note with no op is unattached ("no op");
- a note naming an op with no record is a dangling reference ("dangling op <id>").

Placed notes plus unsorted notes are every note, so the record is partitioned and nothing falls
through the gap. The unsorted bucket is surfaced on the pad and by `alpaca day` until it is cleared.

## The per-op record model

The op page carries the op's record model, each field with a pointer to the note it came from:

- the **intent** and the **done bar** (from the op-open note);
- the **authority** the op ran under (from the authority-bind note): its id, level and scope;
- the **judgment basis** its close recorded (from the intent-judge note): who judged the bar met
  and on what;
- the **choices log**: the op's notes in timeline order, each with its pointer.

The model is a pure derivation over the record. A closed op always says what authorised it and on
what basis it was judged done, because both are notes on the append-only chain, not a mutable
field.

## Cadence

Sort runs at session end and on demand, never as a background sweep. `alpaca day` regenerates a
per-project daily digest that is a view over the record and never a source: it writes nothing, so
a re-render under a fixed clock is byte-identical. A day whose notes are placed in no timeline is
reported on the pad until it is cleared.
