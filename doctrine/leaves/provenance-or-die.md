# Provenance or die

Every assertion carries a resolvable evidence pointer; an unevidenced claim is discarded, never laundered.

## The rule

No floating claims. Each assertion names a pointer a reader can resolve to ground truth: a file path, a command output, or a record row. A dangling pointer is repaired to the truth it names or marked unresolved; it is never quietly kept as if it held.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
`alpaca/gates/quote_check.py` and `alpaca/gates/record_check.py`.
