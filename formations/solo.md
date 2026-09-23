---
name: solo
roles:
  - id: solo
    archetype: eng
    produces: the change, its narrative, and its own proof pointer
phases: [requirement, design, build, verify, release]
gate_map:
  requirement->design: synthesis
  design->build: structural_conformance
  build->verify: regression_suite
  verify->release: closure
budget_class: light
output: one discharged op carrying a proof pointer per acceptance row
---

# solo

The everyday shape for a small, low-stakes op: one agent walks the whole ladder from
requirement to release. There is no second mind, so there is no blind pair here; independence
comes instead from the instruments at each boundary, which the one agent cannot talk out of a
verdict. The gates are the real instruments: `synthesis` derives the register, a structural
check closes design, the regression suite closes build to verify, and `closure` closes the op.

Solo is the right formation only while the stakes stay light. The moment the work needs an
independent verdict rather than a self-report, the op moves to builder-verifier; the moment it
fans out over many rows, to fan-out. This file names roles and the boundary gates; it never
re-authors the role briefs under `agents/`.
