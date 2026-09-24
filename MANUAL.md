# Alpaca operations

The engineering harness. It is ready to use after bootstrap and onboarding. Domain work plugs in as a profile named by the `profile:` key in `project.yaml`; the value is a dotted module path importable from the project root, and the profile supplies that domain's stages, acceptance cards, extra verbs and web pages. A project without the key runs the generic harness; `alpaca doctor` reports a profile that fails to import. Harness verification and a profile's domain acceptance are separate results.

## Alpaca-specific commands

- `alpaca session` starts, checkpoints, stops, and ends a Claude Code or Codex session using one shared project-local record. See `docs/operators.md`.

Codex lifecycle calls are explicit through AGENTS.md. Claude Code uses native hooks. Tool usage and token counts unavailable from the operator are shown as unavailable, never inferred as measured zero.

# Alpaca user manual

`alpaca` is the one writer of the record (`.alpaca/alpaca.db`). Every verb below dispatches through that one
CLI. Exit codes follow the verdict contract: 0 PASS, 1 FAIL, 2 BLOCKED, 3 PAUSED-FOR-DECISION,
64..67 harness errors. `alpaca doctor` reports 0 ok, 1 warn, 2 error.

This chapter is the truth source for the operating surface (`alpaca.surface`). The front page is the
small set of everyday verbs; the rest are reachable but grouped behind it. The surface lint
(`alpaca.surface.lint`) checks this chapter against the live dispatch table, so a verb cannot ship
undocumented and this chapter cannot name a verb the CLI does not dispatch.

## Verbs

### Front page (daily use)

The everyday operating verbs. Small on purpose.

- `alpaca status` - render the landing pad, or `--json` for the machine shape (the op index rides on it).
- `alpaca op` - open, list, move and close an op (the unit of work in the record).
- `alpaca task` - add, claim, list and move a task; a task is done only with a sealed proof report.
- `alpaca board` - show the board and move a row between lanes; an expired lease returns its row.
- `alpaca msg` - post and read messages on the board between agents working one op.
- `alpaca decide` - record a decision row, the human-owned choice a level cannot auto-advance.
- `alpaca ask` - post a question that pauses work until it is answered.
- `alpaca answer` - answer an open question and let the paused work resume.
- `alpaca phase` - enter a phase by level, or adjudicate a phase boundary door (`enter`, `gate`).
- `alpaca doctor` - check the installation and the record's own consistency; read-only.

### Setup and adoption

- `alpaca init` - create the runtime record under `.alpaca/` in this project root.
- `alpaca onboard` - sense the repository, propose an identity, and seed the record on first contact.
- `alpaca upgrade` - refresh the mechanism paths and leave the memory paths untouched, crash-safe.
- `alpaca spec` - install one spec tool from the vendored copies, offline (`init --kit spec-kit|openspec`, `status`, `verify`); see Spec tools below.

### Maintenance

- `alpaca verify` - recompute the append-only event chain and report PASS or FAIL.
- `alpaca retention` - apply the declared store retention and compaction policy.
- `alpaca apply` - apply a recorded in-place change with its mandatory snapshot note.
- `alpaca token` - manage a claim token (the live lease a row holds).

### The record over time

- `alpaca day` - regenerate the per-project daily digest, a view over the record, never a source.
- `alpaca proof` - the report a task hands in: `new` scaffolds it, `seal` hashes it onto the record, `check` re-hashes it.
- `alpaca review` - list and move the review-card rows a formation produces.
- `alpaca barrier` - the outbound push barrier: `install` writes the pre-push hook and pins a copy of the barrier and its settings in the git directory; the hook scans every object a push would send (commits, tags, trees, files and the members of compressed files, path names, identities, messages and refs); `scan <rev>` runs it by hand. The term list, the allow rules and what a hook cannot stop are in `docs/shipping.md`.

### Specs and runbooks

- `alpaca runbook` - `alpaca runbook check <file> [--spec <path>]` refuses a malformed runbook and, with a spec-kit or OpenSpec spec, fails when a success criterion or scenario has no check. Read-only; see `docs/runbook-format.md`. `alpaca runbook kit [--out <dir>] [--zip]` builds the partner runbook kit, `alpaca-runbook-kit-v1`, from this product's own files: a folder (and a zip that is the same bytes on every build) a partner hands to their own agent, with the checker as one Python file that needs only PyYAML and gives the same verdicts and codes, a JSON Schema, the format reference, `AGENTS.md` and a Claude Code skill, templates and the worked example.
- `alpaca intake` - `alpaca intake <spec> <runbook> [--op <op>] [--dry-run]` checks the runbook against a spec-kit or OpenSpec spec, then gives the op one checklist row per success criterion or scenario, one task contract per runbook stage and per owner gate, and the profile stages. A rerun after a spec change supersedes only the rows whose criterion changed, withdraws removed ones, adds new ones and keeps the rest with their verdicts. See `docs/intake.md`.
- `alpaca start` - `alpaca start <notes> [--kit spec-kit|openspec] [--prepare]` is the one entry point from raw notes: it picks spec-kit for a new thing or OpenSpec for a change, prepares the project, and prints the steps to a spec, a runbook and intake. Raw notes go to the inbox and the interview first: `raw pieces -> input/notes/ -> interview (slots, rounds, readback, sign-off) -> spec -> runbook 2 -> intake`, so without a signed interview the steps start with `alpaca note add` and `/alpaca-interview`, and `--json` says `interview: needed|signed|stale` (see `docs/interview.md`). The skill `/alpaca-from-notes` (`.claude/skills/alpaca-from-notes/SKILL.md`) walks them.
- `alpaca note` - `alpaca note add "<text>" | --file <path> | -` keeps one raw piece in `input/notes/<UTC stamp>-<sha12>.md`: a small front matter, then the body byte for byte. A note is never rewritten; the same bytes again give `already have <file>`. `alpaca note list [--json]` lists the notes in time order with their first line. See `docs/interview.md`.
- `alpaca interview` - the slot map a runbook needs (goal, scope-out, done-bar, thresholds, edge-cases, failures, never, owner-gates, rollback, evidence, knobs, commands; a project adds or replaces slots in `input/interview/slots.yaml`). `alpaca interview set <slot> --answered|--default|--waived|--open --value <text> --source <ref>` appends one line to `input/interview/log.jsonl`; `status` lists each slot, the open slots and the notes no answer cites, and exits 0 only when no slot is open; `readback` gives five buckets (clear from the start, added during the interview, filled by default, waived, drifted); `signoff --by <name>` writes the signed readback with the sha256 of the log, and a later change to the log makes it stale. The skill `/alpaca-interview` (`.claude/skills/alpaca-interview/SKILL.md`) asks the questions. See `docs/interview.md`.

### Knowledge

- `alpaca sort` - drain the session and sort notes into their op timelines or the unsorted bucket.
- `alpaca wiki` - the wiki engine verbs; `alpaca wiki extract` emits a portable vault and nothing writes back.

### Output and views

- `alpaca export` - export the record as a static bundle.
- `alpaca hub` - `alpaca hub publish` writes this workspace's tile to a host hub's drop directory from `data.json` or `board.json` (settings under `hub:` in project.yaml; it also runs at session end when `hub.enabled` is true); `--print` writes nothing. `alpaca hub timer` prints a systemd user service and timer that publish every 60 s. `setup/hub-publish.py` is the same publisher as one standalone file (Python standard library only) that any member of the machine can copy to publish a workspace without installing Alpaca. See `docs/host-hub.md`.
- `alpaca deploy` - deploy the read-only page for the owner to read while away.
- `alpaca serve` - serve the read-only page locally.
- `alpaca analytics` - build the analytics projection (`analytics/index.html`).
- `alpaca recall` - recall a session's record by its id.

### Style

- `alpaca style` - the plain-writing ban list: `ban`, `unban`, `use` a preset, or `humanize` (lint) text.

## Copy and rename

Use the clean archive produced by `setup/ship.py`. Extract it into a new directory, rename that directory if desired, and run `bash setup/bootstrap.sh`. The local Python environment is recreated on the destination; any tools a domain profile needs are installed separately. No source-machine paths or runtime state are required.

Nothing in the tree names the harness folder or bakes in an absolute path, so you can rename the folder or move it and the harness keeps working. The root is discovered by walking up to the directory that holds `ALPACA-MANIFEST`, the marker that says "this directory is the harness root".

Two file classes travel differently. The mechanism class (the code, the doctrine, the contracts) is replaced wholesale on an upgrade. The memory class (`RESUME.md` and the record under `.alpaca/`) is yours and is never overwritten; `alpaca upgrade` refreshes the mechanism paths and leaves the memory paths untouched. `project.yaml` ships as a template with no project identity filled in. Each installation receives a fresh project-local identity in `.alpaca/instance.json`. Runtime record, resume pad, analytics and tool installations never travel in the clean archive. An in-place upgrade preserves the target's configuration and record. When a release stops listing a mechanism path an older release shipped (the top-level skills folder of earlier releases, for example), the upgrade removes the files there that still hold the shipped bytes recorded in `MANIFEST.json`; a file you edited or added there stays, and `alpaca upgrade --plan` names both.

## First chat and onboarding

On the first session the SessionStart hook prints `RESUME.md` into the chat. On a generic unconfigured project
there is nothing to resume yet, so the first thing to do is onboard.

- `alpaca onboard` senses the repository, proposes an identity, and seeds the record. It writes
  `project.yaml` (the project identity, the default autodrive level, the plain-writing preset
  choice) and opens the record. On a fresh copy it writes these answers into the shipped template
  `project.yaml` and keeps every other key and its value. It is a sense-then-propose step: it reads the repository and
  proposes, and you confirm, rather than guessing silently.
- `alpaca doctor` then checks that the installation and the record are consistent. Run it once right
  after onboarding.

Onboarding asks who is working on the project, what is being worked on, what the open tasks are,
and whether to turn on the plain-writing preset. Answer those, run `alpaca onboard`, then `alpaca doctor`.

## Daily use

The everyday loop is four surfaces over the one record.

- The landing pad. `alpaca status` renders the landing pad: the open ops, the open tasks, the last
  event, whether the project is onboarded. `alpaca status --json` prints the same shape for a machine;
  the op index rides on it. The rendered pad also lands in `RESUME.md`, which the SessionStart hook
  prints when a session opens or resumes. `RESUME.md` is a projection of the record, never a
  source you hand-edit.
- The board. `alpaca board` shows the board and moves a row between lanes. When a claim lease expires,
  the row returns to the board on its own, so no work is stranded under a lease that went stale.
- The verbs. `alpaca op` opens, lists and closes an op (the unit of work). `alpaca task` adds, claims and
  moves a task; a task is done only with a sealed proof report, so the closing move is
  `alpaca proof new <id>`, write the report, `alpaca proof seal <id>`, then
  `alpaca task move <id> done --proof local:<report>`. `alpaca msg` posts and reads messages between agents working one op. `alpaca decide`
  records a decision the level cannot auto-advance. `alpaca ask` and `alpaca answer` bank and clear a
  question that pauses work until it is answered. `alpaca phase` enters a phase or adjudicates a phase
  boundary door.
- Resume. When you come back to a project, read the pad first. Do not race ahead of it: `alpaca status`
  tells you the open work and the last event, and the record is the truth behind both. Pick up the
  first open task, claim it, and move it when its proof exists.

## The autodrive levels and what each one asks of you

Autonomy is one ladder of named rungs from attended to pre-authorised. The level in force is a
recorded decision every gate reads. You may drop to a safer rung at any time; you never raise your
own. A raise is one human decision. The project-local session command sets the level; the reason the ladder
exists lives in `doctrine/leaves/autodrive-levels.md`, and the band is enforced by
`alpaca/posture/level.py`. A fresh install runs at L2.

| Level | Name | What it asks of you |
|---|---|---|
| L1 | Assist | The agent investigates and hands over ready-to-run changes; it writes nothing itself. You apply every change. |
| L2 | Partial | The agent makes single bounded edits, one at a time, and confirms each. You approve each edit. |
| L3 | Conditional | The agent runs a task end to end but hands control back at every fork or irreversible step. You decide at the forks. |
| L4 | High | The agent is autonomous inside one declared scope and self-verifies; it stops at the scope edge or a hard blocker. You set the scope. |
| L5 | Full | The agent plans and runs the whole task, routes around a wall, and surfaces what it could not finish once at the end. You read the end report. |
| L6 | Never-defer | The agent pursues the goal without deferring mid-run; it stops only when the goal marker is written or you stop it. You set the goal and can stop it. |

Ship, release close, and irreversible external actions stay human decisions at every level.

## Formations and when to call one

A formation is a shape for the work: how many agents, who checks whom. The manifests live under
`formations/`.

- `formations/solo.md` - one agent, the default for a bounded task.
- `formations/builder-verifier.md` - one builds, a second verifies, when a change needs an
  independent check.
- `formations/fan-out.md` - several agents on independent rows of one op, when the work parallelises.
- `formations/bug-loop.md` - a reproduce-fix-verify loop, when chasing a defect.
- `formations/nuclear.md` - the depth-first adversarial audit and fix, for a narrow high-stakes call.
- `formations/napalm.md` - the breadth-first sweep, for a wide surface at lower stakes.

Reach for a formation when the shape of the work needs more than one pair of hands or an
independent check; stay solo when it does not.

## The analytics file

`alpaca analytics` builds one HTML file per project, `analytics/index.html`, from the record. Open it
to see what the project's sessions did, overall and per session. It is a projection: it is rebuilt
from the record and is never a source you edit. Externally sourced values are neutralised before
they enter the page, so a hostile value in a name or a message cannot inject live markup, and the
file is pure ASCII so it reads on a phone. `alpaca export`, `alpaca deploy` and `alpaca serve` package and
serve the same read-only view.

## The cockpit

`alpaca serve` serves the workspace hub at the root address it prints at start; its Alpaca tile
opens the cockpit at `/hub/`. When a web login is configured (the files-auth file under `.alpaca/`, or `ALPACA_FILES_AUTH`), every page
first shows the sign-in page, and a correct access code sets a 12-hour session cookie. The cockpit reads the
seven keys of `data.json`, served live beside it, and nothing else from the record: every
obligation row grouped by acceptance item with its state, the observed value, the instrument and
the proof pointer; the latest verdict of every gate; the recent gate runs; and the latest record
events. It refreshes while it is open. The analytics page stays a view of sessions; the cockpit is
where you read the status of the work. The classic status board page is retired, and its old
addresses (/board/ and /board.html) redirect to the cockpit.

## Troubleshooting with alpaca doctor

`alpaca doctor` is the first stop when something looks wrong. It is read-only: it never changes the
record. It checks the installation and the record's own consistency - a pad naming an op that does
not exist, a cursor that resolves to nothing, an op marked live whose cursor has not advanced, an
expired authority still being cited, an unresolved collision, a store over its retention ceiling -
and reports each with a pointer. Its exit codes are 0 ok, 1 warn, 2 error. `alpaca verify` recomputes
the append-only event chain and reports PASS or FAIL when you suspect the record itself.

## The hooks

The harness registers a set of hooks in `.claude/settings.json`. Each one is a small module under
`alpaca/hooks/` that the harness runs at a point in the session. Every hook is fail-open: any error is
swallowed and the session continues.

| Event | Module | What it does |
|---|---|---|
| SessionStart | `alpaca.hooks.session_start` | Prints `RESUME.md` and the router into the chat when a session opens or resumes. |
| UserPromptSubmit | `alpaca.hooks.user_prompt` | Re-arms the level contract and the plain-writing reminder on a cadence. |
| PreToolUse | `alpaca.hooks.pre_tool` | Writes the tool name and the whole tool input to the capture pool. Prints nothing, so it never blocks a call. |
| PostToolUse | `alpaca.hooks.post_tool` | Records a heartbeat of tool activity against the right project, and the whole call and its response to the pool. |
| Stop | `alpaca.hooks.stop` | Runs the plain-writing lint over the turn before it lands, and keeps the local transcript copy current. |
| SubagentStop | `alpaca.hooks.subagent_stop` | Records that a subagent finished and copies its transcript into the project. |
| SessionEnd | `alpaca.hooks.session_end` | Drains the session into the record, snapshots the transcript and rebuilds the projections. |
| PreCompact | `alpaca.hooks.pre_compact` | Snapshots the working context before the transcript is compacted. |

## The rules that survive

These hold in every session, at every level.

- The record is the only truth. `.alpaca/alpaca.db` is the source; `RESUME.md`, `analytics/index.html`,
  `board.json` and `data.json` are projections, rendered and never hand-edited.
- The record is append-only, with a hash chain `alpaca verify` recomputes. `alpaca` is the sole writer.
- A task is done only with a sealed proof report: `alpaca proof new`, write it, `alpaca proof seal`,
  then `--proof local:<report>`. No sealed report, not done.
- Every claim carries a pointer to a file, a command output, or a record row.
- Ship and irreversible external actions are human decisions at every autodrive level.
- Never leave the project root, and never hand-edit a projection.
- The authority surface (`contracts/`, `project.yaml`, `doctrine/`, the boot block in `CLAUDE.md`)
  is human-owned; below L5 an agent write there is a review card.

## Spec tools

Alpaca carries two upstream spec tools at pinned versions, with their MIT notices, and uses them as
they ship. A project uses one of them, so they never overlap:

- spec-kit (github/spec-kit) for a new thing: from a raw idea to a first spec, with the clarify
  questions and answers. `alpaca spec init --kit spec-kit` writes its Claude Code skills
  (/speckit-specify, /speckit-clarify, /speckit-plan, /speckit-tasks, /speckit-implement and the
  rest) and its templates and scripts under .specify in the project. The spec-kit constitution is
  replaced by `vendor/spec-kit/constitution.md`, which points at `CLAUDE.md` and `doctrine/` and
  says Alpaca's rules win; it is not a second rulebook.
- OpenSpec (Fission-AI/OpenSpec) for changes to something that exists: living specs plus change
  deltas. `alpaca spec init --kit openspec` runs the vendored `openspec init --tools claude`, which
  writes the openspec folder and the /opsx:propose, /opsx:apply and /opsx:archive commands with
  their skills. `bin/openspec` runs the vendored CLI (`new change`, `validate`, `archive`, `list`,
  `show`) with the host node (20.19.0 or later); the first run unpacks the pinned packages under
  .alpaca/tools after checking each sha256. At session start the hook puts the project's bin
  folder on PATH for Claude Code, so the commands find `openspec`. Telemetry stays off.
  `openspec init` keeps a global config (profile, delivery, workflows); the install runs it with
  a scratch config dir that it removes afterwards, so your own OpenSpec config under
  XDG_CONFIG_HOME (or ~/.config/openspec) is not written and does not change what gets installed.
  Other `bin/openspec` commands read that user config as upstream does.

`alpaca spec init` refuses the second kit when the project already has the other one (its files,
or the `spec:` entry it records in `project.yaml`), and refuses to replace a file that differs from
what the kit would write (for OpenSpec it first runs `openspec init` in a scratch directory and
compares the skills and commands, so a refusal writes nothing); `--force` overrides both, and the
output lists the files it replaced. A rerun that changes nothing leaves `project.yaml` as it was. `alpaca spec status` shows the recorded kit, the kits
found on disk and the pins; `alpaca spec verify` checks every vendored archive against
`vendor/VENDOR.json`. Nothing here uses the network. `setup/vendor_spec_kits.py` rebuilds the
vendored copies from the upstream releases (maintainers only, needs the network).

A project starts in spec-kit and moves to OpenSpec at its first change: `alpaca start <notes>
--prepare` installs OpenSpec and writes each spec-kit spec as a living OpenSpec spec, one
requirement per success criterion and functional requirement, each scenario named by its id
(`#### Scenario: SC-002`). The runbook's `covers` entries keep matching, and `alpaca intake` keeps
every row and its verdicts across the move. The spec-kit files stay as history. See
`docs/intake.md`.

## This manual and its gate

This file is `MANUAL.md`. `setup/build_manual_html.py` renders it into `docs/manual.html`, a
phone-safe page you can open over a file link. The manual gate checks that every command, path,
verb, hook and file named here resolves in the shipped tree, that `docs/manual.html` is pure ASCII
and parses, and that the page carries the charset and viewport metas and both theme blocks. A
release candidate whose manual gate fails does not package. `setup/boot-check.py` composes the gate
with the rest of the instruments. The boot read-order and the router live in `MAP.md`, and the
work an op opens from lives in `intents/queue.md`.

## Durable observability and recovery

- `alpaca collect` registers expected sources, ingests bounded batches, runs independent
  projection consumers, and reports coverage, cursor backlog and retry state. `collect once`
  is a bounded repair pass; `collect watch --interval 15` is the supervised service.
  `collect enable` moves expensive native-hook snapshots and wiki/export work to that service.
- `alpaca artifact` preserves approved result bytes, catalogs older archive availability,
  pins evidence, and previews retention. `artifact retention-dry-run` never deletes bytes.
- `alpaca backup` creates and verifies coordinated SQLite snapshots plus replayable files,
  reports recorded verification, and restores only into an empty isolated destination.
  A structurally valid backup can still contain explicitly declared historical evidence gaps.

- `alpaca workspace` registers this instance in the host workspace registry (`add`, `list`,
  `remove`, and `move` after its folder was moved) and renders one combined cloudflared config
  for every registered hostname (`render-ingress`), which the owner applies. See
  `docs/workspaces.md` for the pathway from a local root to a cockpit on localhost and on a
  public hostname.

See `docs/observability-operations.md` for collection contracts, service setup, failure recovery,
retention budgets and the restore rehearsal. The optional project SQLite runtime is described
in `setup/sqlite-runtime.md`. Raw native reasoning is excluded from new analytical capture.
