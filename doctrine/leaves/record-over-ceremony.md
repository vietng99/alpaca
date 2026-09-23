# Record over ceremony

The append-only record with proof pointers replaces the old signed-checklist gate; a mark is an event, never a signature.

## The rule

Where the predecessor demanded a signature to advance, this harness demands a recorded event carrying a proof pointer. The record is the authority, not the ceremony: a discharge is one append-only row anyone can resolve, and there is no signing step to forge or skip.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
`alpaca/db.py` and `alpaca/checklist/`.
