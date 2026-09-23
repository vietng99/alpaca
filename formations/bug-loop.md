---
name: bug-loop
roles:
  - id: finder
    archetype: scout
    fan: true
    lens: distinct
    role: finders with distinct lenses, each hunting the failure a different way
  - id: root-causer
    archetype: tracer
    produces: the root cause behind the symptom, not the symptom
  - id: fixer
    archetype: eng
    worktree: isolated
    produces: the fix, contained in its own worktree
  - id: verifier
    archetype: red
    blind: true
    receives: [claim, pointer]
    role: an independent verifier that never sees the fixer's narrative
phases: [requirement, build, verify, release]
gate_map:
  find->fix: fuzz_gate
  build->verify: workspace_guard
  verify->release: regression_suite
budget_class: heavy
output: a root-caused fix, contained in a worktree, cleared by an independent verifier and a full regression
---

# bug loop

The shape for a defect: finders with distinct lenses to a root cause, to a fix in a worktree, to
an independent verifier, to a full regression. It is generic; it names no particular bug. The
finders fan out with distinct lenses so the failure is approached more than one way; the
root-causer names the cause behind the symptom rather than patching the symptom; the fixer works
in an isolated worktree so a fix in flight touches nothing shared; the verifier is blind and
independent, receiving only the claim and its pointer.

The distinct-lens finding is closed by `fuzz_gate`; the contained fix is closed by
`workspace_guard`, which is what keeps the fix inside its worktree; the release boundary is closed
by the full `regression_suite`, never by the narrower check that first found the bug. A fix passes
only when a mind that did not write it, and the whole suite, both say so.
