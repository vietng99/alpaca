# Concurrency tiers

Heavy operations are classified into tiers so what may overlap overlaps and what must serialize does.

## The rule

Many heavy operations share one box. Without a tier taxonomy that says which may run together and which must wait, parallel launches thrash or starve. The tiers are the classification; the numeric caps are project-tuned parameters, not core.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
`project.yaml` tier caps and the sort cadence in `alpaca/sort.py`.
