# Workspace isolation

Each unit of work runs in its own isolated tree; a builder never writes into another's live workspace.

## The rule

Parallel work runs in separate worktrees so one builder's changes never race or corrupt another's. Isolation is the precondition that lets many sessions run at once without a shared-state collision that no gate could later untangle.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
`alpaca/gates/workspace_guard.py` and `alpaca/gates/git_containment.py`.
