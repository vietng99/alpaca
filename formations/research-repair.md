---
name: research-repair
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
  - id: reproducer-writer
    archetype: eng
    produces: the reproducing test before any fix
  - id: reproducer-reviewer
    archetype: red
    blind: true
    receives: [claim, pointer]
phases: [build, verify]
gate_map:
  build->verify: workspace_guard
  verify->release: regression_suite
budget_class: heavy
output: a proved reproducer, then a fix accepted by fail-to-pass and pass-to-pass and verified, per repair task
---

# research repair

The shape of one repair task of the research engine (`alpaca/research/`), adapted from
`bug-loop`. Before any fix, the reproducer writer writes a test that reproduces the bug, and a
blind reproducer reviewer, which receives only the claim and its pointer, checks it; the engine
shows the reproducer failing on the base itself. Then each fix attempt runs the whole attempt
plan with the improve roles: a proposer per slot, an implementer in an isolated worktree, and a
notes writer.

A fix counts only when the reproducer fails on the base and passes with the fix
(fail-to-pass), no test that passed on the base is lost (pass-to-pass), and both verifiers
support the fix. The formation is a declaration: a research run's op has no phases, so no phase
door runs this gate map. `workspace_guard` names the contained change and `regression_suite` the
pass-to-pass suite at the release boundary.

Design: design/research-engine/architecture.md sections 2.3, 7.4 and 12.1.
