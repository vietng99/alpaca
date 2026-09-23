# Spec-driven checklist

Work is driven by a checklist derived from the spec; a discharged item moves to the discharged store, never a signed sink.

## The rule

The done-bar is the spec's own checklist, not an author's sense of finished. An item is discharged with its proof pointer and moves to the discharged store as a recorded event, replacing the old signed-sink ceremony with a derived, auditable record.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
`alpaca/checklist/` and `alpaca/ops.py`.
