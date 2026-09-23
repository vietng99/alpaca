---
name: napalm
skill: plugin/alpaca/skills/napalm
roles:
  - id: orchestrator
    archetype: orchestrator
    role: the sole spawner; convenes the council, folds, never judges
  - id: seat
    archetype: scout
    fan: true
    lens: distinct
    role: a council seat, each a distinct lens over the wide surface
  - id: cross
    archetype: red
    blind: true
    receives: [claim, pointer]
    role: one round of different-lens cross-examination
phases: [sweep, cross-examine, fold]
gate_map:
  sweep->cross-examine: proportionality
  cross-examine->fold: quote_check
  fold->report: honest_tag_oracle
budget_class: standard
cutover: owner-decision
output: one honest report, hot spots handed up to nuclear, certified only as CONCURRED not PROVEN
---

# napalm

The breadth-first sweep, registered here as a manifest that points at its shipped skill
(`skill: plugin/alpaca/skills/napalm`). The doctrine in that skill is the source of truth; this file
registers the formation and binds its boundary gates to real instruments. Napalm is BFS and
cannot drill by construction; it feeds nuclear, it never becomes it.

The G0 control-pass that routes on breadth and stakes is closed by `proportionality`; the single
round of different-lens cross-examination is closed by `quote_check`, which holds each seat to what
the surface actually says; the fold to the report is closed by `honest_tag_oracle`, so nothing is
stamped beyond what a single sweep can honestly certify.

Napalm certifies its own findings as CONCURRED, not PROVEN, and hands hot spots up to nuclear as
frozen packets. As with nuclear, any fix rule stays a verified copy plus an owner decision at
cutover (`cutover: owner-decision`); the sweep never performs the cutover itself.
