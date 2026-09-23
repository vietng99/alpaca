# Alpaca design

Alpaca is the engineering harness: a project-local record, the rules that govern work on it, and the operator adapters that let Claude Code and Codex share it. It is domain-neutral. Domain work plugs in as a profile.

## Product boundary

All active package, CLI, hook, manifest, database, and plugin names use Alpaca or alpaca. Source project history, original runtime state, personal configuration, Git worktrees, and predecessor Git history are excluded from a shipment.

The bundle includes the operating instructions, the core engineering mechanisms, the doctrine, and the tests. Python environments and any domain tools are installed on the destination. Python 3.10+ with venv support is required for bootstrap.

## Shared core and operator adapters

The core keeps its event record, operations and tasks, sealed proof reports, phase gates, projections, knowledge store, worker coordination, and recovery mechanisms under alpaca/. The immutable obligation hash is shared across synthesis, persistence, and verification; derived status and maturity tags are not frozen inputs.

Claude Code uses native lifecycle hooks in .claude/settings.json. Codex uses AGENTS.md and explicit session commands. Both call the same lifecycle handlers and write .alpaca/alpaca.db. Session IDs remain distinct, while each clean installation creates its own .alpaca/instance.json. No account-wide autonomy or transcript discovery is inherited. Usage that an operator does not expose is reported as unavailable.

## Tasks, evidence and proof

A task closes only with a sealed proof report: `alpaca proof new` scaffolds it, the author writes what was done, how, where, the result, deviations, how to reproduce and the evidence list, and `alpaca proof seal` hashes it onto the record. A task contract (input, expected output, done bar and fail cases) can restate what a task must deliver; it never replaces the proof. Every completion claim needs current evidence, and input changes invalidate prior evidence. Failed and interrupted attempts stay visible.

## Domain profiles

A project names its domain profile with a `profile:` key in `project.yaml`, whose value is a dotted module path importable from the project root. The profile supplies the domain's stages, acceptance cards, extra verbs, doctor checks and web pages through the hooks in `alpaca/profile.py`; generic modules reach a domain only through that seam. A profile that fails to import falls back to the empty profile and `alpaca doctor` names why. With no profile, Alpaca runs as the generic harness: ops, tasks, proofs, the record and its views. A profile's domain receipts are the authority for its own acceptance; generic checklist text alone cannot certify a domain result.

## Views and observability

The record is the only source. `RESUME.md`, `analytics/index.html`, `board.json`, `data.json` and `CHECKLIST.md` are projections. `alpaca serve` serves the workspace hub, the cockpit and the session analytics behind an optional web login. The collector ingests transcripts and tool pools with byte cursors, `alpaca artifact` preserves approved result bytes, and `alpaca backup` takes verified snapshots that restore into an empty destination. See `docs/operations-hub.md` and `docs/observability-operations.md`.

## Shipping and future maintenance

ALPACA-MANIFEST declares an explicit shipment allowlist and local-memory exclusions. setup/ship.py builds a reproducible archive containing hashes and executable modes. It refuses missing members, symlinks, populated project identities, and inherited runtime/history paths. Bootstrap recreates the local environment rather than copying one from another machine. `alpaca upgrade` refreshes mechanism paths in an installed copy and leaves its memory untouched.

Development plans stay in the development repository; the release carries this design, the operating guides, and license notices.
