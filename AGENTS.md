<!-- ALPACA:BOOT:BEGIN -->
# Alpaca: Codex operator

This is Alpaca, the engineering harness. Use this installation's `bin/alpaca`; the shared record is `.alpaca/alpaca.db`. The owner may use Codex and Claude Code on the same project. Both operators obey the same rules and phase gates.

## Start and resume

1. Run `bin/alpaca --session codex-SESSION_ID session start --operator codex` with the current Codex task ID. If the operator exposes no ID, omit `--session` on start and retain the generated ID from its output for this task.
2. Read the rendered pad and `bin/alpaca status`. Read `ALPACA-MANIFEST`, then the core set in `doctrine/CORE-CARD.md` and `MAP.md` section 2 before the router in section 3. Read the session level and modes after the core set.
3. If the pad says the project is not onboarded, ask who is working on it, what is being worked on, the open tasks, and whether to enable the plain-writing preset; then run `bin/alpaca onboard ...` once and `bin/alpaca doctor`. When `project.yaml` names a domain profile with a `profile:` key, that profile supplies the domain's stages and acceptance cards; read its instructions for the active step.

Codex uses explicit lifecycle commands. Claude-specific hook configuration does not run automatically in Codex. Before a final response or context handoff, run `bin/alpaca --session SESSION_ID session checkpoint --note "completed step; evidence; exact next action"` and `bin/alpaca --session SESSION_ID session stop`. Use `session end` only when closing the recorded session. Never invent automatic token/cost measurements.

## Operation rules

- Phases and profile stages run in their declared order. A missing result is BLOCKED, never PASS.
- Poll a running job before starting another. A disconnected chat does not mean a job stopped.
- Every completion claim has current evidence. Input changes invalidate prior evidence. Record both failures and successful runs.
- A task is done only with a sealed proof report: `bin/alpaca proof new ID`, write the report the way an engineer documents work (what was done, how, where, the result, deviations, how to reproduce, the evidence list), `bin/alpaca proof seal ID`, then `bin/alpaca task move ID done --proof local:REPORT`. A `remote:` ref belongs inside the report's Evidence list.
- Owner-authorized edits and installations proceed within scope. Ask before destructive or irreversible external actions. Changes to acceptance criteria or to a profile's protected inputs require owner review. Session autonomy is project-local and never inherited from another account.
- Use native Codex delegation tools when delegating. The formation library records assignments; it does not itself launch agents. Workers share the same operation record and must not race a running job.
- Keep generated state under `.alpaca/`. Use clean shipment tooling for another machine; do not copy live state, local settings, tool installations, or Git worktrees.
- Never emit Unicode U+2014. Standalone HTML is ASCII with UTF-8 charset first in head and a viewport tag.

Launch the operator from this project's trusted root so `.codex/config.toml` applies. Bootstrap with `bash setup/bootstrap.sh` if dependencies are missing.
<!-- ALPACA:BOOT:END -->
