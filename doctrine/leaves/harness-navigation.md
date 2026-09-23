# Harness navigation

A loading discipline for a many-file tree: load the invariant core every time, retrieve the long tail on demand.

## The rule

The tree is too large to load whole each session. The core set loads unconditionally so no invariant rule is ever skipped; everything past the core is retrieved when the task reaches for it. Navigation optimizes the long tail, never gates the core.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
`MAP.md` (the navigator) and `doctrine/CORE-CARD.md`.
