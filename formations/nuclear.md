---
name: nuclear
skill: plugin/alpaca/skills/nuclear
roles:
  - id: orchestrator
    archetype: orchestrator
    role: the sole spawner; drives rounds, never judges
  - id: red
    archetype: red
    blind: true
    receives: [claim, pointer]
    role: tries to break the claim
  - id: blue
    archetype: eng
    role: defends and repairs
  - id: judge
    archetype: judge
    role: rules on the round; the verdict is the judge's, not a party's
phases: [audit, decide, fix]
gate_map:
  audit->decide: proportionality
  decide->fix: verdict
  fix->cutover: honest_tag_oracle
budget_class: heavy
cutover: owner-decision
output: a verified deliverable whose cutover is an owner-signed decision, never an automatic step
---

# nuclear

The depth-first adversarial formation, registered here as a manifest that points at its shipped
skill (`skill: plugin/alpaca/skills/nuclear`). The doctrine in that skill is the source of truth for
how a round runs; this file only registers the formation and binds its boundary gates to real
instruments so the registry knows it exists.

The G0 triage that keeps the operation proportionate is closed by `proportionality`; the round's
ruling lands a `verdict` row; the cutover boundary is closed by `honest_tag_oracle`, which will
not stamp a copy verified unless a behavioral probe backs it.

The fix rule stays what the doctrine makes it: a verified copy plus an owner decision at cutover.
`cutover: owner-decision` is load-bearing. The formation may prepare and verify the copy, but the
cutover from the verified copy to the live target is a recorded owner decision, never an automatic
step this formation takes on its own.
