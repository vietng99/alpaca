---
name: runbook-forge
description: >-
  Write an Alpaca runbook (runbook.yaml, format {{FORMAT}}) from a spec-kit spec.md (FR-nnn,
  SC-nnn, EC-nnn) or an OpenSpec spec, asking the person only for what the spec and the repository leave
  out, and run the kit's check_runbook.py until it passes. For work measured by a number or proved
  by a failing test it writes a research runbook (runbook: 3) and no target the spec does not
  state. Triggers on "/runbook-forge", "write the runbook for this spec", "turn this spec into a
  runbook".
---
