---
name: builder-verifier
default_for: [verify]
roles:
  - id: builder
    archetype: eng
    produces: the change and its narrative
  - id: verifier
    archetype: red
    blind: true
    receives: [claim, pointer]
phases: [build, verify, release]
gate_map:
  build->verify: regression_suite
  verify->release: verdict
budget_class: standard
output: a verified change carrying an independent blind verdict row
---

# builder + blind verifier

The default everyday shape for anything that needs an independent verdict rather than a
self-report. A builder produces the change and its narrative; a blind verifier receives only the
bare claim and its pointer (the orchestrator runs the delivery through `payload_for`, which
strips the narrative), so a "CONFIRMED" means someone tried to break the claim without inheriting
the builder's confidence.

This formation is the declared default for the verify phase (`default_for: [verify]`): when an op
reaches verify with no formation named, this is the one that runs. The build boundary is closed by
the regression suite; the verify boundary lands a `verdict` row under the verdict contract, which
is the blind verifier's independent judgment, not the builder's.
