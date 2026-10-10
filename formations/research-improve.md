---
name: research-improve
roles:
  - id: proposer
    archetype: scout
    tier: mid
    fan: true
    produces: one proposal per slot in the contract's shape
  - id: implementer
    archetype: eng
    worktree: isolated
    produces: the attempt's change in its own worktree
  - id: notes
    archetype: scout
    tier: mid      # dec-ad4dd702ef73: no research job runs on the low tier (Haiku)
    produces: one line on what the attempt tried and what happened
phases: [build, verify]
gate_map:
  build->verify: workspace_guard
  verify->release: verdict
budget_class: heavy
output: one recorded, audited and verified attempt task per slot and round, and a champion on the research branch only
---

# research improve

The shape of one improve round of the research engine (`alpaca/research/`). Each slot of a round
gets a proposer, which writes one proposal in the shape the frozen contract asks for; an
implementer, which makes the attempt's change in its own isolated worktree inside a jail; and a
notes writer, which records one line on what the attempt tried and what happened. The proposers
fan out, one per slot, each with its own hypothesis category.

The formation is a declaration. A research run's op has no phases, so no phase door runs this
gate map. The gates name the properties the engine keeps: `workspace_guard` for a change contained
in its worktree, and `verdict` for an independent verdict before an attempt counts. The verifiers
and the auditor are not roles here: they use the blind `verifier` role of `builder-verifier`
(design/research-engine/verifier.md sections 5.5 and 9.1). A champion only ever moves on the run's
research branch, never on `main`.

Design: design/research-engine/architecture.md sections 2.3 and 12.1.
