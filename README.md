# Alpaca

**The engineering harness**

A standalone engineering harness for Claude Code and Codex. Every session records itself into one project-local record, resumes from `RESUME.md`, and rebuilds its views when it ends. It includes ops and tasks with sealed proof reports, phase gates, a shared session ledger, the workspace hub with the cockpit and session analytics, durable observability with backup and restore, and a clean shipment builder.

Alpaca is domain-neutral. Domain work plugs in as a profile: a project names its profile with a `profile:` key in `project.yaml`, and the profile supplies that domain's stages and acceptance cards. Without a profile, Alpaca runs as the generic harness.

## Quickstart

From a git clone (Python 3.10 or newer with venv support):

```sh
git clone <repository-url> my-project
cd my-project
bash setup/bootstrap.sh
bin/alpaca onboard --name my-project --who alex:owner --what "One line on what this project builds"
bin/alpaca doctor
```

`--who` takes `name:role` pairs separated by commas. Add `--task "..."` once per open task and `--preset plain-writing` to turn on the plain-writing rules. Onboarding writes these answers into the shipped `project.yaml` and keeps every other key of it. Onboarding keeps the template's `commands` (they build and test the harness itself) unless it finds a Makefile, a package.json or a pytest.ini of the project's own, so edit `commands` in project.yaml to name your project's build, test, lint and run. After onboarding, `bin/alpaca doctor` prints no ERROR line; the WARN "no transcript dir yet" clears after the first Claude Code session.

## Start from a release archive

1. Extract the release archive and enter its directory. The directory may be renamed.
2. Run `bash setup/bootstrap.sh`. This creates a local Python environment inside this copy and installs nothing else. Python 3.10 or newer with venv support is required.
3. Open Claude Code or Codex in that directory. For a shell check, run `bin/alpaca --help`, then `bin/alpaca doctor`.
4. The first chat runs onboarding: answer the questions, run `bin/alpaca onboard ...` once (flags as in the Quickstart), then `bin/alpaca doctor`.

Claude Code has native lifecycle hooks. Codex follows AGENTS.md and uses explicit lifecycle commands. Both write one local Alpaca record. No global agent configuration is modified.

## Included

- `alpaca/`, `bin/alpaca`, `ALPACA-MANIFEST`: the harness mechanisms.
- `.alpaca/`: fresh per-installation runtime created on use; never shipped.
- `MANUAL.md` and `docs/manual.html`: command reference and phone-readable manual.
- `doctrine/`, `MAP.md`, `contracts/`, `formations/`: the rules, the boot router, and the work shapes.
- `plugin/alpaca/` and `skills/`: optional bundled skills.
- `docs/runbook-format.md`, `templates/runbook-example/` and `skills/alpaca-runbook-forge/`: the runbook format (a domain's stages, checks, knobs, retry rules and owner gates), a worked example, and the skill that writes a runbook from a spec. `alpaca runbook check` checks one.

See `docs/DESIGN.md` for architecture, and `docs/operators.md`, `docs/operations-hub.md`, `docs/host-hub.md`, `docs/observability-operations.md`, and `docs/shipping.md` for operation details.

## Development and shipment

Run `bin/alpaca-python -m pytest` for the regression suite. Use `bin/alpaca-python setup/ship.py --help` for clean packaging. Never ship a live directory with cp -r. The exporter excludes sessions, evidence, tool installs, caches, local settings and worktrees; third-party notices travel with the package.
