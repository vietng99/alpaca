---
name: fan-out
roles:
  - id: orchestrator
    archetype: orchestrator
    role: the sole serial writer; assigns rows and folds, never judges
  - id: worker
    archetype: eng
    fan: true
    produces: one discharged row each, in parallel over the row set
  - id: pruner
    archetype: red
    blind: true
    receives: [claim, pointer]
    role: an independent check that prunes the row set a worker cannot argue with
phases: [build, verify, release]
gate_map:
  build->verify: regression_suite
  verify->release: integration_check
budget_class: heavy
output: a row set discharged in parallel and pruned by an independent check
---

# fan-out

N workers over a row set, each taking one row, pruned by an independent check. The shape is
generic: it is not tied to any one kind of row. The orchestrator is the sole serial writer that
assigns and folds and never judges; the workers run in parallel, one discharged row each; the
`pruner` is a blind, independent check that decides which rows survive on the bare claim and its
pointer, so a worker cannot talk its own row past the check.

The build boundary is closed by the regression suite over the whole set; the fold to release is
closed by `integration_check`, which is the independent check that the parallel rows compose
rather than each passing alone. The parallel-worker count is a declared budget the dispatch
protocol reads up front, not a number this file invents at runtime.
