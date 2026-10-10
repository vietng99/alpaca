# Alpaca operations

The engineering studio around your coding agents. It is ready to use after bootstrap and onboarding. Domain work plugs in as a profile named by the `profile:` key in `project.yaml`; the value is a dotted module path importable from the project root, and the profile supplies that domain's stages, acceptance cards, extra verbs and web pages. A project without the key runs the generic harness; `alpaca doctor` reports a profile that fails to import. Harness verification and a profile's domain acceptance are separate results.

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
- `alpaca new` - start a project as a full copy of this installation: asks for the name and directory, copies the shipped files, fills the README name block, then runs bootstrap, onboard and doctor and starts the hub.
- `alpaca upgrade` - refresh the mechanism paths and leave the memory paths untouched, crash-safe.
- `alpaca spec` - install one spec tool from the vendored copies, offline (`init --kit spec-kit|openspec`, `status`, `verify`); see Spec tools below.

### Maintenance

- `alpaca release` - initialize GitHub test workflows, approve one exact development commit, build an allowlisted release, run mandatory and project checks, and prepare or publish a clean sync pull request. Configure `release:` in `project.yaml`; checks and publication refuse a commit without a current approval, publication defaults to a dry run and the owner merges. See [Release a clean public copy](docs/release.md) for configuration, receipts, credentials and retries.
- `alpaca verify` - recompute the append-only event chain and report PASS or FAIL.
- `alpaca retention` - apply the declared store retention and compaction policy; `retention gc` plans (and with `--apply` runs) the project.yaml `disk:` cleanup of ended sessions' scratch, stale /tmp leftovers, old run trees and merged worktrees of closed ops (`docs/temp-and-scratch.md`).
- `alpaca apply` - apply a recorded in-place change with its mandatory snapshot note.
- `alpaca token` - manage a claim token (the live lease a row holds).
- `alpaca watchdog` - the advisory guard for destructive git work (design/watchdog/design.md). `watch [--interval S] [--once] [--json]` prints one `WATCHDOG <kind> <key> :: <summary>` line per new event (a request message to `watchdog`, main dirty with several sessions' files, a new or orphaned worktree, a record chain failure) and nothing when idle; the `/watchdog` skill runs it under the Monitor tool. `preflight [--for <who>] -- <git command>` writes a report under `.alpaca/watchdog/runs/<id>/` with what the command touches, the live sessions holding work there and a dry run, and exits 0 safe, 1 risky or 2 stop. `undo <id>` keeps refs under `refs/watchdog/<id>/` and a record snapshot before the owner signs; `rollback <id> [--dry-run]` recreates the branches and worktrees it saved. `postcheck <id> [--to <who>]` verifies the record, compares refs with the preflight and posts the result. It never blocks or runs another session's command.

### The record over time

- `alpaca day` - regenerate the per-project daily digest, a view over the record, never a source.
- `alpaca proof` - the report a task hands in: `new` scaffolds it, `seal` hashes it onto the record, `check` re-hashes it, `verify` runs the two-agent verification of the latest seal (`seal --verify` puts a task in its scope; an engine task is always in it, and `done` then waits for a `verified` end).
- `alpaca review` - list and move the review-card rows a formation produces. An `out-of-brief` card (L5) moves only with `review move <card> --accept|--reject --by owner --quote Q`, the owner's own words. See `docs/intent-fidelity.md`.
- `alpaca owner` - `owner ask <subject>` records that the owner is asked about a record id; the owner's reply in the chat is the proof an owner-only write checks. See `docs/intent-fidelity.md`.
- `alpaca brief` - `brief present|sign|show <op>`: copy an op's brief, record the owner's signature of it, list what was signed. See `docs/intent-fidelity.md`.
- `alpaca order` - `order add|close|list|check-note|not-order`: record an owner's standing order with the owner's quoted words, close it only with the owner's words, and fail a handoff or landing note that drops an open order. An open order pauses the phase door at every level, L6 included. See `docs/intent-fidelity.md`.
- `alpaca selfcheck` - `selfcheck brief|submit|derived|status`: write the packet a fresh checking agent reads at a phase door, record its checked result, list the derived choices for the owner to read. At L5 each finding raises an `out-of-brief` owner card; the door fails on a held finding, as at L6, and pauses while a card is open otherwise. See `docs/intent-fidelity.md`.
- `alpaca choice` - `choice <task> --text T (--covered-by REF | --outside | --withdraw <card>)`: record a task's choice between doors. At L5 `--outside` raises an owner card and the task waits (claim and move refused) until the owner answers; at L6 it is a derived choice and nothing stops. `--withdraw` takes a rejected choice back. See `docs/intent-fidelity.md`.
- `alpaca batch` - `batch open|propose|show|answer|check|derive <op>`: the start-of-op question batch (six big-choice classes and the plan's questions); an L5 or L6 claim or move is refused until the owner has answered every item and the brief is signed. See `docs/intent-fidelity.md`.
- `alpaca barrier` - the outbound push barrier: `install` writes the pre-push hook and pins a copy of the barrier and its settings in the git directory; the hook scans every object a push would send (commits, tags, trees, files and the members of compressed files, path names, identities, messages and refs); `scan <rev>` runs it by hand. The term list, the allow rules and what a hook cannot stop are in `docs/shipping.md`.

### Specs and runbooks

- `alpaca runbook` - `alpaca runbook check <file> [--spec <path>]` refuses a malformed runbook and, with a spec-kit or OpenSpec spec, fails when a success criterion, edge case (format 2) or scenario has no check. Read-only; see `docs/runbook-format.md` (format 2 adds numbered edge cases, known failures with `detect` and `then`, recovery stages and `source`; a `runbook: 1` file is still read). `alpaca runbook kit [--out <dir>] [--zip]` builds the partner runbook kit, `alpaca-runbook-kit-v2`, from this product's own files: a folder (and a zip that is the same bytes on every build) a partner hands to their own agent, with the checker as one Python file that needs only PyYAML and gives the same verdicts and codes, a JSON Schema, the format reference, `AGENTS.md` and a Claude Code skill, templates and the worked example.
- `alpaca intake` - `alpaca intake <spec> <runbook> [--op <op>] [--dry-run]` checks the runbook against a spec-kit or OpenSpec spec, then gives the op one checklist row per success criterion, edge case (runbook 2) or scenario, each with its bar, the stage that shows it first and its sources, one task contract per runbook stage and per owner gate (a recovery stage gets no task: the fail case line that sends to it names it), and the profile stages. A rerun after a spec or runbook change supersedes only the rows whose criterion or bar changed, withdraws removed ones, adds new ones and keeps the rest with their verdicts. See `docs/intake.md`.
- `alpaca start` - `alpaca start <notes> [--kit spec-kit|openspec] [--prepare]` is the one entry point from raw notes: it picks spec-kit for a new thing or OpenSpec for a change, prepares the project, and prints the steps to a spec, a runbook and intake. Raw notes go to the inbox and the interview first: `raw pieces -> input/notes/ (inbox) -> interview (slot map, rounds, readback, sign-off) -> spec (spec-kit or OpenSpec) -> runbook 2 (forge, sources) -> intake -> rows with bars`, so without a signed interview the steps start with `alpaca note add` and `/alpaca-interview`, and `--json` says `interview: needed|signed|stale` (see `docs/interview.md`). The skill `/alpaca-from-notes` (`.claude/skills/alpaca-from-notes/SKILL.md`) walks them.
- `alpaca note` - `alpaca note add "<text>" | --file <path> | -` keeps one raw piece in `input/notes/<UTC stamp>-<sha12>.md`: a small front matter, then the body byte for byte. A note is never rewritten; the same bytes again give `already have <file>`. `alpaca note list [--json]` lists the notes in time order with their first line. See `docs/interview.md`.
- `alpaca interview` - the slot map a runbook needs (goal, scope-out, done-bar, thresholds, edge-cases, failures, never, owner-gates, rollback, evidence, knobs, commands; a project adds or replaces slots in its file input/interview/slots.yaml). `alpaca interview set <slot> --answered|--default|--waived|--open --value <text> --source <ref> [--reason <text>]` appends one line to the log, input/interview/log.jsonl (a waive needs a reason); `status` lists each slot, the open slots and the notes no answer cites, and exits 0 only when no slot is open; `readback` gives five buckets (clear from the start, added during the interview, filled by default, waived, drifted); `signoff --by <name>` refuses while a slot is open, then writes the signed readback with the sha256 of the log, the slot list and the notes it saw; a later change to the log or the slot map, an open slot, or a note kept after it makes it stale. `alpaca interview use <set>` turns a slot set on by adding it to the `include:` list of input/interview/slots.yaml: `alpaca interview use research` adds the fourteen research slots a format 3 research contract needs (templates/interview/research-slots.yaml), and templates/research/improve.yaml and repair.yaml are the fill-in contract templates. The skill `/alpaca-interview` (`.claude/skills/alpaca-interview/SKILL.md`) asks the questions. See `docs/interview.md`.

### Research

- `alpaca research` - run the research engine under a format 3 runbook. `start --runbook <file> [--task <id>] [--op <op>] [--control noop|random] [--via cli|autoresearch] [--model <alias>] [--foreground]` freezes the contract and starts the loop runner; no flag sets a metric, a target, K, N or a budget, which come only from the frozen contract, and `--model` names one model for every agent job of the run. `status [<run>]` folds one run, or every run, from the record and says whether its runner is alive. `stop <run> [--reason <text>]` stops a run. `extend <run> [--decision <id>] [--no-resume] [--model <alias>] [--repin-engine] [--context-path <path>]...` lifts an extension hold and resumes the run, and exits 3 when no owner grant answers the hold. `resume <run> [--decision <id>] [--model <alias>] [--repin-engine] [--context-path <path>]...` starts a halted, paused or killed run again (a session run needs autodrive L3 or above; `--repin-engine` records a new engine pin at the owner's terminal, with a `--context-path` for each loaded file the owner keeps). The session that starts a run drives it: `next-job <run> [--wait <s>] [--limit <n>] [--list] [--with-prompt]` (or `next-job --verification <id>` for a proof verification) hands out the agent jobs the runner has issued, each with a spawn prompt for one subagent of this session, and `submit <job> --nonce <nonce> --agent-id <id> [--usage-json <file>] [--transcript <file>] [--final <file>] [--tool <name>] [--tool-use-id <id>] [--diagnose] [--wait <s>]` files that subagent's claim for the runner's judge; the engine starts no agent process. `report <run> [--out <dir>]` writes report.md and report.json under .alpaca/reports/research/ from the run's events, and opens the ship hold for a finished run with something to ship. `grant` is the owner's verb, typed at an interactive terminal outside any agent session: `grant <run> extend --rounds <M> [--no-apply]`, `grant <run> budget [--usd <X>] [--tokens <T>] [--wall <S>] [--no-apply]`, `grant <run> stop`, `grant --runbook <file> [--task <id>] loosen --set <field>=<value>`, `grant --runbook <file> [--task <id>] budget` and `grant --runbook <file> reopen`, where `--no-apply` records the grant and leaves the run held, and any form takes a note, as in `grant <run> stop --note <text>`; it records an owner decision and a `research-grant` event. `contracts` lists the tracked format 3 runbooks with their contract hash, burn-in state, runs and open holds, and `template --mode improve|repair [--out <file>]` prints or copies the fill-in contract template. Every sub-verb takes `--json` and then prints one JSON object. A verb that meets an open owner hold exits 3, and each verb records its event on the record (research-run-start, research-status, research-stop, research-extend, research-resume, research-report, research-grant, research-refused, and for jobs research-job, research-job-handed, research-job-claim, research-job-result, research-job-refused, research-job-expired, research-job-late, research-driver, research-driver-audit, research-repin). The operator and engineer guide, with every flag and every format 3 field, is `docs/research-engine.md`.

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

The short route is `install.sh` once, then `alpaca new` per project. `install.sh` checks a release against its `MANIFEST.json`, keeps it under `~/.local/share/alpaca/releases/`, and writes one launcher at `~/.local/bin/alpaca`. Inside a project the launcher runs that project's `bin/alpaca`; elsewhere it runs the installed release. `alpaca new` copies the mechanism class into a new directory, puts the release templates in place, writes the project name into the block between `<!-- alpaca:project-name -->` and `<!-- /alpaca:project-name -->` at the top of README.md, and runs `git init`, `bash setup/bootstrap.sh`, `alpaca onboard` and `alpaca doctor`. README.md is then the project's own: `alpaca upgrade` keeps it, and `setup/gen_manifest.py --verify` lists it as instance-owned.

By hand: use the clean archive produced by `setup/ship.py`. Extract it into a new directory, rename that directory if desired, and run `bash setup/bootstrap.sh`. The local Python environment is recreated on the destination; any tools a domain profile needs are installed separately. No source-machine paths or runtime state are required.

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
| SessionStart | `alpaca.hooks.session_start` | Prints `RESUME.md` and the router into the chat when a session opens or resumes. With a `disk:` block in project.yaml it also starts the background disk sweep (`alpaca/disk.py`) when one is due. |
| UserPromptSubmit | `alpaca.hooks.user_prompt` | Re-arms the level contract and the plain-writing reminder on a cadence. |
| PreToolUse | `alpaca.hooks.pre_tool` | Writes the tool name and the whole tool input to the capture pool. Prints nothing, so it never blocks a call. |
| PreToolUse (Bash) | `alpaca.hooks.git_guard` | Denies git commands that rewrite working files in the shared main tree (`git stash` other than list and show, `git reset --hard`, `git checkout -- <path>`, `git restore <path>`, `git clean -f`), because other sessions and the live record work there. The same commands run in a linked worktree. |
| PreToolUse (Bash) | `alpaca.hooks.temp_guard` | Warns, never blocks, when a pytest run passes `--basetemp` and the same command does not remove it, or points it at the disk: pytest never deletes an explicit basetemp, so it stays in RAM-backed `/tmp`. See `docs/temp-and-scratch.md`. |
| PostToolUse | `alpaca.hooks.post_tool` | Records a heartbeat of tool activity against the right project, and the whole call and its response to the pool. |
| Stop | `alpaca.hooks.stop` | Runs the plain-writing lint over the turn before it lands, and keeps the local transcript copy current. |
| PreToolUse (research) | `alpaca.hooks.research_guard` | Denies a subagent's call that reaches the control plane (the record, the hooks, the settings, the engine's code, another agent's transcript, a process that outlives the call, a git command that writes) while a research run is live. It does nothing for the driving session or with no live run. |
| SubagentStart | `alpaca.hooks.subagent_start` | Records that a subagent started, with its agent id and type, so a research job's agent has a second witness. |
| SubagentStop | `alpaca.hooks.subagent_stop` | Records that a subagent finished and copies its transcript into the project. |
| SessionEnd | `alpaca.hooks.session_end` | Drains the session into the record, snapshots the transcript and rebuilds the projections. With a `disk:` block it starts the same background disk sweep when one is due; the ending session's own scratch stays for `ended_grace_hours`. |
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
requirement per success criterion, edge case and functional requirement, each scenario named by
its id (`#### Scenario: SC-002`, `#### Scenario: EC-001`). The runbook's `covers` entries keep matching, and `alpaca intake` keeps
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


### mission

`alpaca op new` pins each new op (pass `--no-pin` to skip) and records a short name from `--title`, or from the intent before its first colon when no title is given; `alpaca op title <op> "<name>"` renames it. The map's label reads "Operation map Op21 - <short name>". `alpaca op pin <op>` puts the op's operation map on the Roadmap tab; `alpaca op unpin <op>` removes it. Both are recorded events and the newest one wins. The map lays the op's phases out in their recorded order, lists each phase's tasks by workstream with the roadmap's states and prerequisite arrows, and shows a phase strip (current phase, done counts, tasks in progress), the last phase-door refusal, tasks held for the owner and blocked tasks. Finished tasks stay on the map as done; it refreshes like the cockpit.

`alpaca mission` keeps a project capability map. Initialize with `mission init --preset generic` (or `alpaca`), inspect with `mission show`, record source changes with `mission scan`, and associate work with `mission link NODE TASK --kind work|fix`. Run a declared check using `mission check --check tests NODE -- COMMAND`. Use `mission show --definition` and `mission define --file FILE --revision REVISION` to update the definition. Check results name their exact inputs and evidence; task completion does not certify a capability.
