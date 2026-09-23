# Alpaca

**The engineering harness**

A standalone engineering harness for Claude Code and Codex. Every session records itself into one project-local record, resumes from `RESUME.md`, and rebuilds its views when it ends. It includes ops and tasks with sealed proof reports, phase gates, a shared session ledger, the workspace hub with the cockpit and session analytics, durable observability with backup and restore, and a clean shipment builder.

Alpaca is domain-neutral. Domain work plugs in as a profile: a project names its profile with a `profile:` key in `project.yaml`, and the profile supplies that domain's stages and acceptance cards. Without a profile, Alpaca runs as the generic harness.

## Start on another machine

1. Extract the release archive and enter its directory. The directory may be renamed.
2. Run `bash setup/bootstrap.sh`. This creates a local Python environment inside this copy and installs nothing else. Python 3.10 or newer with venv support is required.
3. Open Claude Code or Codex in that directory. For a shell check, run `bin/alpaca --help`, then `bin/alpaca doctor`.
4. The first chat runs onboarding: answer the questions, run `bin/alpaca onboard ...` once, then `bin/alpaca doctor`.

Claude Code has native lifecycle hooks. Codex follows AGENTS.md and uses explicit lifecycle commands. Both write one local Alpaca record. No global agent configuration is modified.

## Included

- `alpaca/`, `bin/alpaca`, `ALPACA-MANIFEST`: the harness mechanisms.
- `.alpaca/`: fresh per-installation runtime created on use; never shipped.
- `MANUAL.md` and `docs/manual.html`: command reference and phone-readable manual.
- `doctrine/`, `MAP.md`, `contracts/`, `formations/`: the rules, the boot router, and the work shapes.
- `plugin/alpaca/` and `skills/`: optional bundled skills.
- `docs/runbook-format.md`, `templates/runbook-example/` and `skills/alpaca-runbook-forge/`: the runbook format (a domain's stages, checks, knobs, retry rules and owner gates), a worked example, and the skill that writes a runbook from a spec. `alpaca runbook check` checks one.

See `docs/DESIGN.md` for architecture, and `docs/operators.md`, `docs/operations-hub.md`, `docs/observability-operations.md`, and `docs/shipping.md` for operation details.

## Development and shipment

Run `bin/alpaca-python -m pytest` for the regression suite. Use `bin/alpaca-python setup/ship.py --help` for clean packaging. Never ship a live directory with cp -r. The exporter excludes sessions, evidence, tool installs, caches, local settings and worktrees; third-party notices travel with the package.
