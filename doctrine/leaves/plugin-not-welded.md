# Plugin, not welded

Target-specific behavior lives behind a seam as a plugin; the generic core never hardcodes a domain value.

## The rule

The harness is a generic core pointed at any project. Anything specific to one target sits behind the intake seam as a plugin and never in the core, so the same core drops into the next project unchanged. A domain literal welded into the core is a defect.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
`project.yaml` (the intake seam) and `alpaca/gates/literal_guard.py`.
