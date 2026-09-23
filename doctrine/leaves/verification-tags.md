# Verification tags, not signatures

A verdict is a derived tag over a real run, not a human signature; a guard that cannot fail proves nothing.

## The rule

Honest verification replaces the old signing language: the tag on a result is derived from the check that produced it, never asserted. A guard that passes no matter its input is dead verification wearing a green coat, so every gate must be able to fail on a real negative and is proved so by its own negative control.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
`alpaca/gates/honest_tag_oracle.py` and the verdict contract `alpaca/gates/verdict.py`.
