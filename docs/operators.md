# Operating Alpaca with Codex and Claude Code

Both operators use the same project-local record, `.alpaca/alpaca.db`, and the same
lifecycle handlers. Each session has its own ID and posture. A clean shipment has
no session record, imported transcript, account settings, or previous machine's
history. The first session creates this copy's instance identity.

## Codex

Read `AGENTS.md` when entering the project. Start an explicit session from the
project root:

```bash
bin/alpaca session start --operator codex
```

The JSON result contains `session`, `context`, and `level`. Read the returned boot
context and retain the generated ID for this task. Include that exact ID on every
subsequent command that records work. You can supply your own unique ID instead:

```bash
bin/alpaca --session codex-example session start --operator codex
bin/alpaca --session codex-example status
```

Use a different ID for a different task or worker. Do not share an ID with a
Claude session. An existing ID can be resumed with `session start`; its original
start time and recorded posture are retained. Starting a session does not launch
a dashboard. Add `--dashboard` or run `bin/alpaca serve` when desired.

If the pad says the first chat is due or the project is not onboarded, run the first chat
(`.claude/skills/alpaca-first-chat/SKILL.md`): look at the repository the owner names with
`bin/alpaca onboard look`, ask the printed questions, record the answers, show the one setup card from
`bin/alpaca onboard propose` unchanged, and run `bin/alpaca onboard apply` and `bin/alpaca doctor` only after
`bin/alpaca onboard confirm` accepts the owner's typed reply. In a Codex session `confirm` prints the card and asks
the person to type its id in a terminal of their own. When
`project.yaml` names a domain profile, read that profile's instructions for the
active step.

At a milestone, before returning a final reply, and before an expected context
handoff, record a continuation note:

```bash
bin/alpaca --session codex-example session checkpoint --note "Next: review the failing test log and rerun the suite"
```

This records the note, copies the registered transcript into `.alpaca/transcripts/`,
captures the session's recorded events and the note into the local wiki, and
refreshes the resume pad. The capture is the step the Claude Stop hook runs at the
end of every turn: it lands the events no earlier capture landed, each once, and
leaves the transcript itself for `session end`. While the project's `collect watch`
collector is running, the checkpoint records the note, copies the transcript and
refreshes the pad, and leaves the capture step to the collector. The command returns a nonzero exit
status if capture fails. A failure can leave a failure event in the record; inspect
it, fix the cause, and retry the checkpoint. It is safe to drain unchanged events
again.

The capture runs in a child process with a fixed time limit (the hooks: shortly before the hook's
own deadline; the checkpoint: 10 s plus 5 s). A capture still running then is stopped from outside
and the record gets a `capture-deferred` event; the next hook or checkpoint lands the rest. A capture
that fails leaves `drain-failed`. With `.alpaca/observability/enabled` present the hooks and the
checkpoint leave capture to the collector; if no collector is running, nothing is captured, and
`bin/alpaca doctor` and `bin/alpaca wiki status` say "observability enabled but no collector alive"
(the hooks also record one `capture-no-collector` event about it every 10 minutes at most; it is a
state, not a capture failure, and the hub does not list it among capture errors. Before t-070 the
same note was recorded as `drain-failed`, and those older events are still read as failures).

The hooks' other file steps run the same way: the pad, the data push and hub tile, the transcript
copy, the analytics build, a domain profile's refresh and the SessionEnd consolidation step each run
in a child process stopped at its share of the hook's time. A step stopped there or failing leaves the
record its failure always left (`analytics-build-failed`, `hub-publish-failed`, `drain-failed`), and
the hook's own outcome turns `timeout` (`StepOverrun:<step>`). The `session` verbs run the same steps
in their own process, with no time limit. Every Alpaca process puts SQLite's temp files (large sorts)
in `.alpaca/tmp/sqlite/` (mode 0700) on the project's own disk, unless `SQLITE_TMPDIR` is set; on a
host where `/var/tmp` is a slow disk this keeps a sort from stalling a hook.

Before the final reply, record the turn boundary and check stop rules. To include
the final reply in the style check, save its proposed text in a local file and
provide that file explicitly:

```bash
bin/alpaca --session codex-example session stop --reply-file path/to/proposed-reply.txt
```

Exit 2 with `decision: block` names a requirement to address. Without
`--reply-file`, the stop command checks the recorded work state and has no reply
text to lint. `stop` ends a turn; it leaves the session open. When deliberately
closing the recorded session, run:

```bash
bin/alpaca --session codex-example session end --reason complete
```

Codex and terminal operators can name their own transcript file: `bin/alpaca --session codex-example session start --transcript path/to/session.jsonl` and `bin/alpaca --session codex-example session end --transcript path/to/session.jsonl`. The path is stored in the record and copied into `.alpaca/transcripts/` on the next boundary. A Codex session whose ID is `codex-` followed by its Codex native session ID (a UUID), started without `--transcript`, has its transcript found for it: `session start` looks under `~/.codex/sessions/` for the one file named for that native ID whose session header names this project root, and registers that path. Nothing else in the account's transcript directory is read.

Codex does not run `.claude/settings.json`. These commands are explicit operator
steps, not automatic Codex hooks. Abrupt termination or unannounced compaction
cannot be assumed to run a final capture. Checkpoints preserve the work already
recorded; with observability off, a session that never reaches `session end` gets its
transcript into the wiki only through `bin/alpaca wiki recover`, which reads the registered
transcript file, or the copy the last checkpoint made when that file is gone. A Codex
transcript is registered on its own only in the
`codex-<native ID>` case above; name any other with `--transcript`. Token counts
and unreported tool activity are not imported automatically; the dashboard marks
usage as unavailable.

Optional explicit boundaries:

```bash
bin/alpaca --session codex-example session prompt
bin/alpaca --session codex-example session beat --tool exec_command --ref "reviewed the test report"
bin/alpaca --session codex-example session level L4 --goal "Run the authorized verification work"
```

`prompt` returns the current style reminder on its cadence. `beat` records only
the action you report; it does not measure every native tool call. Keep sensitive
command arguments out of `--ref`. A level change must reflect the owner's
authorization; posture alone does not grant separate unattended authority.

`session level` (and `session start --level`) records a drop, OFF or the level
already in force for anyone. A raise above the level in force records only on the
owner's own words, and is then recorded under the actor `owner`:

- In a Claude Code session, the session transcript must hold the owner's
  `/autodrive L<n>` call for exactly that level, typed after the latest level
  decision. The command reads the transcript; nothing is passed for it.
- With no agent session in the environment (the owner's own terminal), the
  command asks the person to type `L<n>` to confirm. A Codex session has no
  transcript Alpaca reads for owner words, so a Codex raise is run by the owner
  in a terminal of their own.

Otherwise the command exits 2 with `status: BLOCKED`, a `reason`
(`OWNER-WORD-LEVEL-RAISE`, `OWNER-WORD-NOT-TERMINAL`, `OWNER-WORD-SESSION` or
`OWNER-WORD-NO-TRANSCRIPT`) and nothing recorded. A refused `session start
--level` still starts the session, at the level already in force.

### Stopping an L6 run

At L6 the Stop hook refuses a stop until one of these exits holds:

```bash
bin/alpaca --session codex-example session done --reason "op-009 reached the t-117 ship hold"
bin/alpaca --session codex-example session gate ship --ref t-117 --reason "ship is the owner's call"
bin/alpaca --session codex-example session level OFF
```

- `done`: the goal is reached. It holds until a new level decision (a new goal) for the session.
- `gate <kind>`: the run reached an owner-only gate (`ship`, `spend`, `rounds`, `bar`,
  `reopen`, `signoff`, `contract`, `owner-action`). It holds for that turn only and posts a
  blocker message to the owner.
- `level OFF`, or the owner's `/autodrive off` (the shared skill's file in the account dir is read
  for this session; a drop needs no owner words): the record goes back to the default level.

A usage-limit stop and a halted thread also end the refusal on their own.

## Claude Code

Open the project in Claude Code and read `CLAUDE.md`. The project hooks in `.claude/settings.json` (nine events) invoke `bin/alpaca-python --isolated`, which selects the installed project environment and, when the project has its venv, starts the interpreter isolated (`-I`) through `alpaca/boot.py`, which loads the project's own `alpaca` package by its location, so no folder of the install root is on the import path (a linked worktree that shares the main venv runs its own package too). SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SubagentStart, SubagentStop, SessionEnd, and PreCompact call the same handlers used by the explicit commands. PreToolUse has a second entry, the research guard, which denies a subagent's control-plane call during a live research run and does nothing otherwise. PreToolUse and PostToolUse write each tool call and its response to `.alpaca/pool/tools/<session>.jsonl` (an input or a response over 256 KiB is cut there, with the sha256 and byte count of the whole value kept; a large input is stored once, on the PreToolUse line); PreToolUse prints nothing, so it can never block a call. Stop, PreCompact, SessionEnd and SubagentStop copy the registered transcript and its subagent files into `.alpaca/transcripts/`, so the session outlives the thirty-day clearing of the account's own storage.

Claude supplies its session ID and the exact transcript path in hook payloads.
Alpaca records that explicitly supplied path for that session. It never scans
the account's transcript directory or a historical project's directories.
Project hooks retain bounded, fail-open behavior so capture failures do not
stall Claude; an explicit `session checkpoint` is the way to require a visible
capture result before handoff.

Use the Claude-provided session ID with `bin/alpaca --session ID` for recorded
commands and explicit posture changes. Project posture files live under
`.alpaca/config`. The shared `/autodrive` skill writes its level to the account
dir instead (`$CLAUDE_CONFIG_DIR`, else `~/.claude`), as
`.autodrive-level.<session>`. Alpaca reads only that per-session file, never the
account-wide one, and only in two cases:

- A drop (`off`, or a level below the one in force) written after the latest
  level decision lands as is. Dropping to a safer level is always allowed.
- A raise lands only when the owner typed it. The session transcript that the
  first session-start recorded must hold an owner message that is the
  `/autodrive L<n>` call for that same level: a typed prompt or a queued
  command, not a sidechain, meta, compact-summary or `claude -p` entry. The
  message must be typed after the latest level decision, and the latest such
  call must name the file's level. The decision is recorded under the actor
  `owner` with the owner_word row (transcript, line, timestamp, quote), and
  the project-local file is rewritten to match. A raise that no owner message
  backs is ignored: an agent cannot raise its own level.

The dashboard is optional for Claude as well.

A `bin/alpaca` verb run without `--session` takes its session from `ALPACA_SESSION_ID`, then from `CLAUDE_CODE_SESSION_ID`, which Claude Code exports in every shell call. A task move, a proof seal or a message sent from a Claude session is therefore recorded under that session and not under the placeholder `cli`. Outside any session the placeholder still applies.

## Shared task planning

Both operators assign a named feature/workstream, work area, prerequisites and resources when they add a task. Reuse an existing group where it fits. Keep the map current when work splits or its prerequisites change. Follow the commands in [Maintaining a useful checklist](operations-hub.md#maintaining-a-useful-checklist); this also covers the project-local required-planning policy.

## Deferred tasks and after-action reports

Both operators use the same verbs, and the hub shows the result (see [Reading the work](operations-hub.md#reading-the-work)).

- Deferred. `task move <id> deferred` parks a task that should wait. It needs `--reason` and `--reactivate-when`; the trigger may name `after:t-NNN`, `op:op-NNN closed` or `date:YYYY-MM-DD`, and free text alone never fires. A claim held on the task is released with the move. A deferred task moves out only to open. Moving a done task to deferred counts as moving it back from done, so its op then owes an after-action report. The example and the trigger rules are in [Maintaining a useful checklist](operations-hub.md#maintaining-a-useful-checklist).
- The after-action report. `aar new <op>` writes `.alpaca/aar/<op>.md` with the op's timeline filled from the record. `aar check <op>` names every missing or empty part and every claim with no pointer. `aar review <op> --reviewer <id>` writes a packet a fresh subagent reviews (the checks are in `.claude/skills/alpaca-aar/SKILL.md`), and `aar accept <op> --review <file>` or `aar rework <op> --reason <why>` records the main session's decision. `aar seal <op>` needs an accepted review of the report as it is now; it checks the report, records its hash, and files each row of its Action items table as a task, in the standing After-action items op unless `--into <op>` names another open op. There is one standing After-action items op: a seal never opens a second one; when its plan is full the seal refuses and names `--into`, and its own report files its items into itself. While an action item filed by another op's report is not done (deferred and blocked count as not done), the report must keep its row under Carried over: `aar check`, `aar review` and `aar seal` refuse a report that leaves the section out or drops a row, and name the row to add. With nothing open the section is left out. The check of a sealed report that has not changed does not apply this rule.

```sh
bin/alpaca --session SESSION_ID aar new op-012
bin/alpaca --session SESSION_ID aar check op-012
bin/alpaca --session SESSION_ID aar review op-012 --reviewer rev-1
bin/alpaca --session SESSION_ID aar accept op-012 --review .alpaca/aar/op-012.review/review.json
bin/alpaca --session SESSION_ID aar seal op-012 --into op-013
```

- What makes an op owe a report. `aar owed <op>` lists the triggers recorded for that op after its last sealed report or owner waiver, with their event ids. A task of the op moved back from done (to open, doing, blocked or deferred) and an `aar owe <op>` always count for the op. A NO-GO counts for the op in its event's op column, else for the one op of the tasks the deciding session held in doing at that time. A release check FAIL counts for the one op of the tasks the recording session held then; the shared `cli` and `alpaca` sessions hold none. A trigger is tied only to an op that was open when it was recorded and is not a standing op: tasks of a closed op or of a standing op are not counted, and a session left with no op or with two ops leaves it unattributed. A NO-GO or FAIL no op can be tied to is unattributed, and it blocks the close of every op that was open when it was recorded (an op opened later is not blocked) until that op's report is sealed or the owner waives it for that op (one message `<op>: waive` per op); a seal answers every trigger counted for the op up to the seal, so one recorded after it needs a new seal. `aar owe <op> --event <id>` instead moves the trigger to one of them (or to an op closed since that was open when it was recorded, which then owes the report), which makes that op owe the report and clears the trigger for the others. Plain `aar owed` lists every op's triggers, then the unattributed ones with the open ops each blocks. One recorded while no op was open is listed and blocks nothing. The two standing ops (After-action items, Release watch) are open ops and are blocked too, but `aar owe` refuses to tie a trigger to one, since it never closes. A change to the profile's `review_flow_kinds` can add or remove NO-GOs. `aar owe <op> --reason "..."` records a trigger of its own for an op. An action item's due in a report is a date `YYYY-MM-DD`; `aar check` refuses any other form:

```sh
bin/alpaca aar owed
bin/alpaca --session SESSION_ID aar owe op-012 --reason "The deploy was rolled back twice"
bin/alpaca --session SESSION_ID aar owe op-012 --event 190629
bin/alpaca aar owed op-012
```

- The close gate. `op close` refuses while `aar owed <op>` lists a trigger, and also when it cannot read the review flow named in the profile, for example `GATE alpaca-op-close: BLOCKED (AAR owed for 1 trigger(s): reopen event:412 (t-031 moved from done to open); seal the report with alpaca aar seal op-012, or the owner waives it with alpaca aar waive op-012 --by owner --quote ...)`. It also blocks on a deferred task with no trigger on the record ("deferred with no trigger on the record: t-005"), and lists the other deferred tasks with their triggers in the op-close event. Seal the report to close the op. An edit to the report after the seal undoes it: the triggers it answered are owed again, unless an owner waiver covers them, and `aar check <op>` names the change until you run `aar seal <op>` again. A waiver is the owner's decision: the owner sends a message in this session that is exactly `op-012: waive`, and only then does `aar waive op-012 --by owner --quote "op-012: waive"` record it. An agent never writes that message for the owner and never waives on its own judgement; one owner message waives the triggers recorded before it, once, and a later trigger needs a new owner message.

## Shared boundaries

The CLI shim pins its own root. Python callers can select an explicit root;
otherwise resolution uses the nearest `ALPACA-MANIFEST` above the current
directory. `ALPACA_ROOT` is a fallback only when that directory has no marker.
Ambient `CLAUDE_PROJECT_DIR` and `CLAUDE_CONFIG_DIR` cannot redirect an ordinary
Alpaca command. Locally imported transcript files may be placed under
`.alpaca/transcripts`. The old `cwd_history` field never enables discovery.

The record remains the shared coordination surface for claims, tasks, proof
pointers, and decisions. Native agent creation belongs to each operator: use the
available Codex agent tools or Claude delegation tools. Recording a formation
assignment does not itself start an operating-system process.

Which model each delegated agent runs on is set by the shield policy,
`formations/shield.yaml`: every role has a tier, every tier maps to a model, and
check and fix rounds stop at a cap of 5 with one strong investigator per failed
round. Claude sessions apply it with the `/shield` skill; Codex models are not
mapped yet, so the resolver refuses a Codex runtime until they are.

Explicit command errors are visible: exit 0 is success, exit 2 is a blocked or
invalid session operation, exit 64 is command usage, and exit 67 is an internal
failure. Keep the JSON result and command exit status together when assessing a
lifecycle step.

## Updating to a newer release

`alpaca update` moves a project to the newest public release. It reads the newest `vN.N.N` tag of the release repository (`git ls-remote`, never a prompt), compares it with the project's own version, and when the tag is newer it installs that release and then runs `alpaca upgrade` on the project.

```
alpaca update --check     # report the project, installed and newest versions; change nothing
alpaca update             # install, show the plan, ask, apply
alpaca update --yes       # the same without questions (the plan is still printed first)
```

What it does, in order:

1. Finds the project (the folder you run it in, or `--target DIR`) and refuses a pending upgrade journal, a development checkout and an installed release before it touches the network.
2. Reads the newest release tag from `--repo URL` (default: `$ALPACA_UPDATE_REPO`, else the public repository). `http://` and `git://` addresses are refused; use https, ssh or a path. A relative path is read from the folder you ran the command in; `host:path` is read as ssh, as git does (write `./a:b` for a local folder with a colon).
3. Compares the tag with the project's version. Equal: it says so and exits 0. Older than the project: it refuses (no downgrade; older code refuses a newer record).
4. Installs the tag with that release's own `install.sh`: the release is cloned, every file is checked against the sha256 in its `MANIFEST.json`, it is kept under `<prefix>/releases/<id>`, and the `<prefix>/current` link moves after the launcher file is written, with the launcher put in place last. A release's `.venv` must pass `bash setup/bootstrap.sh --check`, not just exist; a half built one is rebuilt or removed. A failed install, a release that is not usable (wrong version or channel, `--version` fails, `.venv` not ready) and an interrupt (Ctrl-C) all put `current` and the launcher back as they were, and the message says so (`current and the launcher are back as they were`). A release that is already installed is not installed again, but its folder is checked against the `MANIFEST.json` of the fetched copy first: a copy that was changed or left half done is replaced by the checked one (`replacing it with the checked copy`), and one that is `current` and does not match refuses the install (`the current release folder ... does not match its MANIFEST.json`). Ctrl-C also removes the installer's stage folder and `alpaca.new`, and `alpaca update` ends with `FAIL ... interrupted` instead of a traceback.
5. Hands off to the newly installed release (an `exec`), so the code that replaces the project's files is not the code being replaced. The launcher does the same for you: `alpaca update` typed anywhere runs the installed release, never a project's copy.
6. Takes the project lock (`<project>/.alpaca/upgrade.lock`), which it keeps through the plan, the saving of local files and the apply. Prints the `alpaca upgrade` plan for the project, then asks `Apply this plan?` unless `--yes` is given. Each path that the new release changed shows as `will be refreshed`; a path that holds a local edit shows as `local edit will be replaced`; a `CLAUDE.md` or `AGENTS.md` that holds your own text inside the boot block (the marked region the release replaces) shows as `local edit in the boot block will be replaced by the release's block`. A `CLAUDE.md` that is a link (for example to `AGENTS.md`) stays a link: the plan says `kept as a link`, and its target is merged on its own when it is `CLAUDE.md` or `AGENTS.md`.
7. Saves every local file the upgrade would replace (an edited file, a file you added under a refreshed path, a `CLAUDE.md` or `AGENTS.md` with an edit inside its boot block) to `<project>/.alpaca/update/<UTC stamp>/<path>`, with or without `--yes`, then applies the upgrade. `.alpaca/` is a memory path, so the upgrade never touches the copies.
8. Checks that the project says the new version, that its `.venv` matches (`bash setup/bootstrap.sh --check`, then one bootstrap if it does not; a project without a `.venv` skips this), and runs `alpaca doctor`. A doctor ERROR fails the update; a doctor WARN passes and is named in the result (exit 1 counts as a warning only when doctor printed its own `GATE alpaca-doctor: WARN` line; any other exit 1 fails).

Exit codes: 0 PASS (already newest, `--check` reached a tag, installed only, or updated), 1 FAIL (install, usability, apply, version, venv or doctor failed), 2 BLOCKED (every refusal and every "no" at a prompt), 64 usage.

Refusals, and what to do:

| Refusal | Fix |
|---|---|
| `cannot read release tags from ...` | Offline, a wrong `--repo`, or git is missing. Nothing was changed; run it again when the repository is reachable. |
| `has a pending upgrade journal` | A crashed upgrade. Run `bin/alpaca upgrade --recover` in that project, then update. `alpaca update` reports the journal and never clears it. A journal file that cannot be read is refused the same way. |
| `is the launcher of ..., not ...` | `--prefix` and `--bin-dir` (or the launcher you ran, plus your `--prefix`) belong to two different installs. `install.sh` would rewrite the other install's launcher, so nothing runs. Pass `--bin-dir` with the launcher folder of the prefix you mean. A file there that `install.sh` did not write is refused the same way. `--check` reports it too. |
| `a repository address cannot start with '-'` | `--repo` (or `$ALPACA_UPDATE_REPO`) looks like an option to git. Use an https, ssh or path address. |
| `cannot write ...` | The prefix or its lock file is not writable. Nothing was changed. |
| `no downgrade` | The newest release is older than the project. Nothing to do. |
| `is a development tree (channel dev)` | A development checkout takes its code from git, not from a release. |
| `is an installed release, not a project` | Run it inside a project, or pass `--target`. |
| `no installed Alpaca found` | Pass `--prefix` and `--bin-dir`, or run `install.sh` first. Update upgrades an install; it never makes the first one. |
| `no terminal to ask; pass --yes` | There is no TTY to answer on. Pass `--yes`. `--check` needs no terminal. |
| `another alpaca update holds ...` | Another update is running against the same prefix. Wait for it. |
| `another alpaca update or upgrade of ... is running` / `an alpaca update or upgrade of ... is running now` | Another `alpaca update` or `alpaca upgrade` (or a second session's) holds the project lock `.alpaca/upgrade.lock`. Wait for it to end; do not run `upgrade --recover` on its journal. Nothing was changed. The lock is released when that process ends, even if it was killed. |
| `could not save your local files before the upgrade` | A file the upgrade would replace cannot be copied (unreadable, a named pipe, an unreadable folder). Nothing was applied. Fix the file or move it away, then run it again. |

Saying no at the plan prompt leaves the new release installed and current and the project untouched; run `alpaca update` again to finish. `--prefix DIR` and `--bin-dir DIR` name the install to update when no launcher written by `install.sh` is on your `PATH`; `install.sh` now records both folders in the launcher it writes. A project that is a git repository will show the upgrade's changes as uncommitted; the saved local files make that safe without a git refusal.

The sha256 check proves that the tree matches its own `MANIFEST.json`. It does not prove who published the tree; the repository address is the trust anchor.

Projects made by 1.3.0 or older: version 1.3.0 has no `update` verb, and its launcher does not route one. Run the install one-liner once (it installs the newest release and rewrites the launcher), then run `alpaca update` in each project. The manual fallback, with `$PREFIX` the install prefix (default `~/.local/share/alpaca`), is `"$PREFIX/current/bin/alpaca" upgrade --target "$PWD" --plan`, then the same command without `--plan`. Use that absolute path: inside a project, plain `alpaca upgrade` runs the project's own, older code with the project itself as the source.

## Research runs in the session

A research run is driven by the Claude Code or Codex session that starts it. The engine never starts an agent process: for each agent launch it issues a job, and the driving session spawns one subagent of its own for that job and files the result.

- The loop. `bin/alpaca research next-job <run> --wait 45 --json` hands out the jobs the runner has issued (`action: spawn`, each with a `spawn_prompt`, a `model` and a `nonce`) or says `wait`, `report`, `hold` or `resume`. For each job the session spawns a plain background subagent with the `spawn_prompt` as its only text, then runs `bin/alpaca research submit <job> --nonce <nonce> --agent-id <id> --json` when the subagent ends. `submit` writes a claim; the runner's judge reads the kept subagent transcript, runs the 19 checks (state, nonce, deadline, witnesses, tools, model, scope, group, intrusion, context and the rest) and is the only writer of the result and launch-end rows. The `/autoresearch` skill carries this loop in its step 5.
- The two witnesses. Every tool call of a subagent is in the hook pool (`.alpaca/pool/tools/<session>.jsonl`, now with `agent_id` and `agent_type` on each line) and in the kept transcript; the judge compares the two.
- Verifier independence. The two verifiers of a pass are two jobs of one group, handed together and checked in both directions before either ends.
- The guard. While a run is live (`.alpaca/research/live/<session>.json`) the PreToolUse guard denies a subagent call that reaches the record, the hooks, the settings, the engine's own code, another agent's transcript, a process that outlives the call or a git command that writes. The engine files, the hook scripts, the import roots and the text sources are pinned when the run starts and stat-checked at each step; a touch ends the run `interrupted` (`RUN-ENGINE-CHANGED`), and the owner records a new pin with `research resume <run> --repin-engine` at a terminal.
- Levels and runtimes. A session run needs autodrive level L3 or above (`RESEARCH-LEVEL-TOO-LOW` below). Claude Code is the supported runtime; a Codex session gets a job's transcript witness only, and a run refuses to start where the shield policy maps no model for the runtime. The headless launcher stays for tests and control runs, and `launcher.refuse_headless` refuses a headless agent in any real run.
- Spend. Cost and tokens are read from the kept transcript, priced by the rate cards (`RUN-RATECARD-MISSING` prints the `alpaca analytics price-check` command that records one).

## Run review and the main decision

When a domain profile wires `alpaca.runlog.reviewflow` (see `docs/observability-operations.md`,
"Review every run, then decide"), every run it captures is reviewed, PASS runs included, and
the main session decides. Claude Code and Codex follow the same loop, in
`.claude/skills/alpaca-log-review/SKILL.md`:

1. After a run finishes, and on every resume, list the runs waiting for review or a decision.
2. Assign the run to a reviewer session id distinct from your own. The assignment records a
   30 minute lease and returns a brief; it launches nothing.
3. Dispatch a fresh native subagent with that brief: the Agent or Task tool in Claude Code, the
   agent tool in Codex. It reads the log, submits a quote-checked review bound to the
   assignment and returns the review id. It does not run or fix the work and does not decide.
4. Read the review, starting with its At a glance block, and record GO or NO-GO with an
   evidence-based rationale. GO needs a recorded PASS and an intact assigned review that
   supports it; NO-GO holds the run for investigation.
5. The profile's runner advances only on a current GO. A PASS run that captures no log needs
   no review; a run with no log that did not pass never advances.

A review whose submit passed has quotes that match the log; it is not the run passing and not
permission to advance. Review state and decisions live in the record, so a disconnected chat
loses nothing; do not assign a second reviewer while a lease is live. No background model
process reviews on its own: the review waits for an active session.

## Alpaca's own skills

`.claude/skills/` holds Alpaca's own skills: `alpaca-first-chat` (the first chat before
onboarding), `alpaca-onboard`, `alpaca-op`, `alpaca-from-notes`, `alpaca-interview`,
`alpaca-runbook-forge`, `alpaca-log-review` and `autoresearch` (the research engine, from contract to
report; its folder has no `alpaca-` prefix). They are
Claude Code project skills and mechanism paths in `ALPACA-MANIFEST`, so a clone, a copy, a release
and `alpaca upgrade` all carry them and Claude Code offers each as a `/command` with nothing to
install. The rest of `.claude/skills/` (the spec-kit and OpenSpec skills `alpaca spec init` writes,
and any skill of your own) belongs to the project. Codex reads the same SKILL.md files directly.

## Optional bundled skills

The root lifecycle adapters do not require a plugin installation. To load the optional bundled skills in Claude Code, launch from the project root with `ALPACA_ROOT="$PWD" claude --plugin-dir ./plugin/alpaca`. Codex may read the relevant bundled SKILL.md files directly. No global plugin or agent configuration is installed by bootstrap.

## Native Rune2 memory

Rune2 is Alpaca's only memory engine. Its code is `alpaca/wiki/`, and it keeps its state in the
vault, `.alpaca/wiki/`: the SQLite store `rune.db`, its ledger and the note files. The record,
`.alpaca/alpaca.db`, stays the authority for sessions, tasks, decisions and proofs. Rune2 holds
source-linked knowledge, checked answers and the derived records an admission accepted, and none
of them changes a task's state or seals a proof.

A release needs no Hindsight package, service, database, credential or network endpoint. The
release check of op-008 (t-071) installed and ran a candidate with none of them present. Rune2
needs the Python standard library's SQLite module and the packages pinned in `requirements.txt`,
nothing more. The name-alike helpers in `alpaca/wiki/ingest/resolve.py` were adapted from the
Hindsight entity resolver; they are maintained here as Alpaca code, and THIRD-PARTY-NOTICES.md
keeps their MIT notice.

### What this release includes

| Capability | How you use it |
|---|---|
| Source time on captured blocks: an event's `ts` is its valid time, the capture time is its recorded time, and an unknown time stays null | automatic at capture |
| Capture that keeps up: a missing transcript no longer drops a session's event notes, every hook failure is recorded, missing events are reported and filled | the hooks, `session checkpoint`, the collector, `wiki status`, `wiki recover` |
| Outcomes of their own for stale, unavailable, partial and unsupported results, apart from grounded, conflict and abstained | `wiki query`, `wiki context` |
| Withdrawal that reaches every derived record and briefing, and an owner correction verb | `wiki withdraw`, `wiki correct`, `wiki withdraw-candidate` |
| Codex and Claude continuity: operator and session fields on notes, Codex capture through the same drain, a task briefing at session start for both | the hooks, `session start`, `session checkpoint`, `session end` |
| A graph filled from structured record events with no model (task status, op, proof, decision and handoff facts) | automatic at capture; `wiki record-facts --dry-run` previews |
| Candidate intake for the live session agent: proposed facts, observations and summaries checked against their sources and kept as candidates until admitted | `wiki propose`, `wiki candidates`, `wiki admit`, `wiki consolidate` |
| Proof reports, task contracts and decision pages as sources | automatic at capture |
| Budgeted, cited briefings of a task or an op | `wiki briefing`, and the context at session start |
| One secret screen at every write door | automatic; the forms are listed under "Project wiki evidence" |
| Export, import, rebuild and recover, with one record event per captured transcript version | `wiki export`, `wiki import`, `wiki recover` |

The graph's retrieval arm (personalized PageRank over the facts) ships but is off by default:
`retrieval_profile` is `narrow`. Turn it on with `meta: {retrieval_profile: graph}` under
`wiki_providers` in `project.yaml`. On the development question set it tied the narrow profile in
every category, so it did not earn the default.

### No model calls by default

Rune2 makes no model call and opens no network connection. The provider registry is closed to
deterministic names: the hash embedder, the identity or lexical reranker, the structural entailer,
and `llm_extractor: 'off'`. Any other name is refused when it is configured, as an unsupported
capability, and `wiki status` reports each provider. In the release check every core flow ran in an
offline sandbox on the Claude and on the Codex path (install, startup, capture, query, context,
briefing, propose and admit, correct, withdraw, export and import, restart, recovery) with 0
connections leaving the host and 0 model processes started.

One shipped hook does use the network stack: SessionStart probes the local dashboard on
127.0.0.1 and starts it when it is not running. That connection stays on the host. Set
`ALPACA_NO_AUTOSERVE=1` to turn it off.

Work that needs a language model (reading sources, extracting facts, writing summaries) is done by
the model of the live chat session that asks. Rune2 returns evidence to that agent and checks what
it proposes. Nothing an agent proposes becomes accepted knowledge without passing the checks below
and an admission by another session.

### Candidate intake for the live session agent

The agent reads first: `wiki query` returns each cited source with its `source_ref`, its `quote`
and, for a record event, its `event_hash`. A proposal anchors on those. Write it as a JSON file:

```json
{"kind": "fact",
 "fact": {"subject": "t-123", "predicate": "status", "obj": "blocked", "obj_datatype": "string"},
 "anchors": [{"source_ref": "event:4567", "quote": "t-123 is blocked", "hash": "EVENT_HASH"}]}
```

```
bin/alpaca --session ID wiki propose --file proposal.json
bin/alpaca --session ID wiki candidates --filter-session ID
bin/alpaca --session OTHER wiki admit CANDIDATE_ID
bin/alpaca --session ID wiki withdraw-candidate CANDIDATE_ID --reason "the anchor was a draft"
```

`propose` runs six read-only checks and records all six results in one `wiki-candidate` event. An
anchor must name a current source with active blocks (a record event also by its hash), its quote
must sit in one of those blocks, the source must be servable and inside the proposal's scope, and
it must be an independent kind: a record event, a proof, a contract or a decision. Transcript text,
`wiki ingest` documents and briefing text never count as support; put them in `context` instead. A
fact uses one of the predicates `status`, `choice`, `sourced_from`, `failed_with` or
`handed_off_to`, its subject, object and dates must each appear in a quote, its subject must name
one entity, and the structural entailer must find the claim supported. An observation or a summary
needs an anchor the entailer supports for every sentence (`sentence_anchors`). The limits are 8
anchors, 400 characters for an observation, 2,000 for a summary and 200 pending candidates per
proposer. A proposal that fails a
check is kept as `refused` with the reasons; one that passes is `checked`.

`admit` must come from a session other than the proposer, and a background job never admits. It
lands any uncaptured events first, runs the checks again on the current store, and records a
`wiki-admission` event. A fact becomes a graph fact; a procedural observation goes to the lesson
store with `--probe`; a declarative one goes to the knowledge store. A summary is never admitted: it
is shown in briefings as an agent summary candidate. `wiki consolidate` is one bounded, model-free
step that turns repeated failure signatures and superseded decisions into observation candidates;
it proposes and never admits. Every string of a proposal passes the secret screen before it is
stored.

### Coverage gaps and recorded limits

- Missing sources. A registered transcript whose file is gone cannot be captured, rebuilt or
  imported. `wiki status` counts them, and a question scoped to such a session is answered
  `unavailable`. On the development project 200 of 296 registered transcripts were unresolvable,
  among them the ten t-038 originals.
- Answers that span blocks. The answer door abstains when the words of a question are not all in one
  cited block, even when the fact is ranked near the top. Read the cited evidence of an abstention
  with `wiki context`.
- Tool output read as contradiction. The structural entailer can read tool output as contradicting
  a claim, so some answers come back `conflict` or abstain.
- Context packs one item per source. `wiki context` shows the best block of each source; another
  block of the same source that `wiki query` ranks lower may be left out.
- A corrected note is not marked in context. After a correction the briefing shows the corrected
  state, and the original note can still appear under the retrieved evidence with no mark.
- No purge. Withdrawal hides a source from every Rune2 door but deletes nothing (see "Withdrawal,
  retraction and purge"). Whether a purge verb should exist is an owner decision not yet taken.
- Old notes keep their old rendering. Notes an earlier release captured keep their text, and
  notes captured before the secret screen keep what the screen now masks, until the owner runs
  `wiki recover --rerender-notes` (below).
- Not measured. The question set used to judge this work could not measure context at a trigger
  point, other-operator attribution or a labelled relevant set, and Rune2 counts no model tokens
  because it makes no model call.

### Operating cost

Measured on the development project's question-set store (about 8,800 sources, about 180 MB),
median of three alternating rounds against the previous release on the same host, with the
1-minute load average beside each figure:

| Operation | This release | Previous release | Load |
|---|---|---|---|
| `wiki query`, p50 and p95 | 0.37 s, 0.45 s | 0.35 s, 0.66 s | 11.4 to 15.7 |
| `wiki context` at 2,500 characters, p50 and p95 | 0.39 s, 0.50 s | 0.33 s, 0.62 s | 11.4 to 15.7 |
| `wiki context` at 16,000 characters, p50 and p95 | 0.49 s, 0.50 s | 0.49 s, 0.51 s | 11.4 to 15.7 |
| SessionStart hook, whole, p50 and p95 (Rune2 part) | 1.13 s, 1.22 s (0.41 s, 0.50 s) | 1.02 s, 1.30 s (0.35 s, 0.60 s) | 15.6 to 18.4 |
| Ingest per event, p50 | 0.020 s | 0.035 s | 9.5 to 14.1 |
| Ingest per transcript, p50 and p95 | 0.29 s, 1.10 s | 0.24 s, 0.96 s | 9.5 to 14.1 |
| Full rebuild of the store | 212 s | 331 s | 9.5 to 14.1 |
| Store bytes per source | 20,705 | 20,829 | |

These were taken with the stores in memory (t-071, 2026-10-09). Hook and capture limits were timed
on disk the day before (t-070, 2026-10-08, load 6.7 to 11.8): SessionStart at most 1.2 s; SessionEnd
at most 5.8 s and PreCompact at most 5.5 s with capture in the hooks, at most 0.15 s each when the
collector captures; a collector pass that captures one ended session took at most 22.3 s, and 15.3
to 16.6 s after the collector speed-up (t-453). A capture that does not finish within a hook's
budget is recorded as `capture-deferred` and finished by the next hook or the collector.

A large live store costs more. The development project held about 116,000 event notes and a 5.5 GB
vault in October 2026: a full `wiki recover` of a copy took 4 h 33 min, and the collector's first
read of the echo index after a start took about 60 s.

### Recovery

- Catch-up. `bin/alpaca wiki recover` lands what capture missed and applies again any recorded act
  whose store write was lost. It resumes from its checkpoint. `wiki status` shows the missing events
  and each job's checkpoint.
- The store-behind-record gate. When the record holds a withdrawal, a candidate withdrawal or an
  applied correction that the store has not processed (a crash between the record append and the
  store commit, or a rebuild or import cut short), every serving door answers `unavailable` with
  reason `store-behind-record` and serves nothing until `bin/alpaca wiki recover` has applied it.
  `wiki admit`, `wiki correct`, `wiki withdraw` and `wiki withdraw-candidate` answer the same way.
  This keeps withdrawn or corrected text from being served while the store lags.
- The warm-up after a collector start. The collector reads the shared echo index once when it
  starts; meanwhile the wiki consumer reports `warming` and its captures wait, with no failure
  recorded. If that read fails, the captures read the index themselves within their own budget and
  record any failure (`drain-failed`); a read still running after 600 s gives way, and the waiting
  capture is recorded as `capture-deferred`. On a live-size copy the consumer was current 67 s
  after a restart.
- A rebuild. Export first (`bin/alpaca wiki export`) to keep the documents added with `wiki
  ingest`, move the whole vault, `.alpaca/wiki/`, aside and run `bin/alpaca wiki recover`; the store
  is rebuilt from the record (see "Rebuild, export, import and rollback"). If only `rune.db`
  is lost and the ledger is kept, `wiki recover` refuses with `LedgerDivergence: ledger ahead of
  committed DB`, although `wiki status` names `wiki recover` as the recovery: move the whole vault
  aside first.
- A locked store gives a bounded outcome: the hooks return in about 5 s with `drain-failed`
  recorded, SessionStart prints "WIKI CONTEXT UNAVAILABLE" and goes on, and `wiki query` and `wiki
  context` exit 1 with the error. With the temporary folder full, the hooks return within 3 s. Run
  `wiki recover` once the cause is gone.

An offline install: `install.sh` has no offline mode. Run it with `--no-bootstrap`, then
`bash setup/bootstrap.sh --offline --wheelhouse /path/to/wheels` in the release.

### Who maintains what

- The Rune2 code under `alpaca/wiki/`, its tests under `alpaca/tests/wiki/` and the adapted
  helpers in `resolve.py` are maintained in this repository with the rest of Alpaca. There is no
  upstream to follow and no Hindsight code to update.
- The project owner decides what may not be automated: running `wiki recover --rerender-notes`,
  any purge, `wiki correct` (declared with `--by owner`), editing installed service units, deleting
  development copies, and every release approval and publication.
- Sessions propose candidates, admit another session's checked candidates, withdraw sources with a
  reason, and keep capture current through the hooks or `session checkpoint`.
- The collector (`bin/alpaca collect enable`, then the units `bin/alpaca collect services`
  generates) captures in the background; the owner installs and restarts its unit.
- The fixed question set that judged this work, and its runner, stay in the development repository
  as a tool for the next round of improvements. They are development state and never ship; a
  release test refuses a shipped file that holds any of its questions or planted values. A change
  to the pinned set needs an owner decision and a new pin.

### Returning to the previous release

The previous release carries the older Rune2: a wiki store at schema 3, with no exclusions,
candidates, corrections or transcript-version events. Going back keeps every accepted row and the
whole record. Steps:

1. Run `bin/alpaca wiki status`. If it counts any withdrawn event, do not roll back: the older
   capture code knows no exclusions, and a capture it runs (`wiki recover`, or the SessionEnd and
   PreCompact hooks) can land a withdrawn note again and serve it. If the rollback cannot wait, turn
   off the Rune2 capture under the older code as "Rebuild, export, import and rollback" describes.
2. Let no session work and stop the collector (`systemctl --user stop alpaca-<id>-collector.service`).
3. Back up: `bin/alpaca backup create`, and `bin/alpaca wiki export accepted.jsonl` to keep the
   documents added with `wiki ingest`.
4. Refresh the mechanism paths from the previous release's tree (`install.sh` keeps each installed
   release under `~/.local/share/alpaca/releases/`): `bin/alpaca upgrade --source
   PATH_TO_PREVIOUS_RELEASE --plan`, review the plan, then run it without `--plan`. The upgrade
   replaces each mechanism path with the previous release's copy, keeps `project.yaml`, `intents/`
   and `README.md`, and leaves `.alpaca/` untouched.
5. Start the collector again, if you use it, and check `bin/alpaca wiki status` and
   `bin/alpaca doctor`.

What happens to the state. The record keeps every event, the new kinds included; the previous
release does not act on them, and this release adds only indexes to the record's schema. The wiki
store keeps its rows: the older code sets `schema_version` back to 3 and ignores the new tables, and
a withdrawn source stays retired, so the older read path serves none of it. Candidates, admissions,
corrections and exclusions stop working until you return. Coming back to this release finds the
accepted state unchanged and serves no withdrawn source again, whatever the older code landed in the
meantime, since the exclusion rows stay. Captures made under the older release write no
transcript-version events, so a later rebuild at a cutoff reads those sessions best-effort.

## Project wiki evidence

Rune2 is the local project wiki. Use `bin/alpaca wiki status` to inspect its corpus,
provider settings, transcript coverage and missing event count. These counts are separate
from the operational decision pages shown by the dashboard.

Before using historical material for a new task or after a resume, run:

```
bin/alpaca wiki context "SQLite offline"
bin/alpaca wiki query "Why does the project use SQLite?"
```

`context` returns bounded cited evidence. `query` returns JSON with the answer verdict,
source document/block pointers, currency and completeness checks. An abstention is a
successful query with no supported answer, not a claim that the source does not exist.
`--refresh` replays every recorded session through a fixed cutoff before querying. On a
large store that is a long job, so it is not a routine step; use it when `wiki status`
reports missing events that the collector is not landing. For explicit catch-up without a
query, use `bin/alpaca wiki recover`. Run `wiki status` afterward; new lifecycle or
recovery events may arrive after the reported cutoff.

A capture, a recover and a `--refresh` never rewrite an event note the wiki already holds:
when the note format changes, stored notes keep their old rendering (the report counts
them as `kept_renderings`) and new events land in the new format. Re-rendering the stored
notes is an owner job:

```
bin/alpaca wiki recover --rerender-notes
```

It rewrites each stored note whose rendering changed in place, lets its facts follow it
and waives the shrink guard for those notes as a format change. Notes captured before the
secret screen keep their old text until this job runs. On a large store it holds the wiki
write lock for one transaction per note for hours, so time it on a copy of the project
first and run it while no session is working. The steps of `wiki recover --rerender-notes` are
in the owner runbook below ("Re-rendering stored notes").

Both operators share the query door. Codex can request startup context explicitly:

```
bin/alpaca --session SESSION session start --operator codex --wiki-question "SQLite offline"
```

A started or resumed session with a claimed task gets that task's briefing as well. With no
question, the context is asked about the task itself (its id and title, any date in the title
left out); with a `--wiki-question` that names no task or op, the claimed task's briefing is
served beside the answer to that question. A question that names a task (`t-NNN`), or else an op
(`op-NNN`), gets that work's briefing instead. The context holds the briefing after its outcome
lines, so the Claude hook and the Codex `session start` serve the same block. A briefing states
the current state only: when the question pins a time (a date, a year with "in", "by", "during"
or "as of", or "as known at <date>"), the context says in one line that the briefing is not
served. The same briefing is available on demand:

```
bin/alpaca wiki briefing --task t-NNN
bin/alpaca wiki briefing --op op-NNN --max-chars 16000 --json
```

A briefing lists the contract, the current state, blockers, decisions, failures and agent
summary candidates, each item cited by its source, with stale and conflicting items labelled
and the gaps in its scope listed. It is evidence-only, built from the store each time it is
asked for and never stored. Capture leaves a briefing out of any message, proof report, decision
or transcript that carries one, `wiki ingest` refuses a document that holds one, and `wiki
propose` refuses an anchor whose quote or block holds briefing text, so its wording never
anchors, supports or corroborates anything. Anchor on the sources it cites.
The boot message names the query commands and reports an unavailable wiki without blocking
operational startup. Check capture freshness before relying on that context.

Index a local authored document with a stable identity:

```
bin/alpaca wiki ingest docs/design.md --doc-id wiki/design.md
```

This copies its content into the local vault through the normal write door. For private material
use `--doc-id wiki/private/design.md`; ordinary wiki paths remain eligible for public extraction
after the existing privacy scan. Existing private sources cannot be copied into a public wiki path
with this command. The stable document identity lets later ingests update the source while
retaining history; normal shrink and integrity gates still apply. Ingestion does not grant policy
authority or publish the content. Raw events and visible transcript exchanges remain evidence-only.
Hidden reasoning records are excluded. Visible tool inputs and results use the same secret
redaction as the transcript viewer; captured text still has no instruction authority. Every
document the wiki keeps, captured or ingested, passes one secret screen at its write door: AWS
keys, private key blocks, Slack tokens, JWTs, bearer and API keys, and plain phrasings such as "the
PIN is 4096" or "the wifi password is swordfishmarlin" are stored as `[redacted]`. A value of
letters alone is masked only after "is", "was", "are" or "were", when it is a word of six letters
or more that reads as no ordinary word, and never after a bare "PIN" or "secret". The screen also reads
these forms (task t-425, the 21 phrasings t-070 check round 1 found stored in clear, minor A-2): a
plural name ("the router passwords are X and Y", "the API keys were X, Y or Z"); a clause between
the name and its verb, after a comma ("the admin password, which the vendor set, is X") or a "for",
"of", "on", "at", "in", "by", "from" or "with" clause of up to sixteen words; "'s", "has been changed
to" (also set, reset, updated, rotated) and "will be" (also would, shall, should, must) as the verb;
a no-break space or a CRLF line break where a space or a line end goes; Markdown bold labels
(`**Password:** X`, `**Password**: X`, `- **wifi password** is X`); a tab or a dash as the separator
(`password<TAB>X`, "Router password - X"); a YAML block value (`password: >` with the value on the
next line); a PIN written in groups of digits ("the gate PIN is 7 3 1 9"); the column of a Markdown
table under a password, passcode, passphrase, PIN, pwd, pw, credential, secret or key header; the
names `pwd` and `pw` (`pwd: X`, never a path such as `pwd` prints) and an upper-case name ending in
`_PASS`, `_PASSWD`, `_PWD`, `_PW` or `_PIN` (`DB_PASS=X`); and the reverse order ("use X as
password", unless a noun such as "hash" follows). In these forms a value is masked when it holds a
digit; a value of letters alone is masked only after "is", "was", "are", "were", "has been changed
to" or "will be" with no clause between, when it is a word of six letters or more that reads as no
ordinary word, and never after a bare "PIN" or "secret". Plurals of "token", "PIN" and "secret" are not read as credential names, since
they count things in prose ("the input tokens were 12000"). Do not paste credentials into the
record. The classification-marking scanner reads
each document before the screen, so a marking the screen masks (the word after "SECRET:") still
keeps a captured document private and still refuses a marked document at a public `wiki ingest`
path.

Configuration precedence is defaults, then `project.yaml` `wiki_providers`, then
`.alpaca/wiki/rune.toml`. Unavailable provider names fail visibly. For example:

```yaml
wiki_providers:
  embedder: deterministic
  reranker: lexical
  entailer: structural
  llm_extractor: 'off'
  meta:
    retrieval_profile: full
```

The shipped providers run locally without external models. The deterministic hash embedder
is a retrieval fallback, not a trained semantic model. Full/hybrid profiles maintain vector
indexes, including backfill and invalidation on embedding changes. Narrow stays the default.
The answer checks handle ordinary English grammatical variants conservatively; they are
not a general language understanding guarantee.

Dream stays disarmed and unscheduled; lessons and knowledge admission remain explicit library
capabilities. Capture and retrieval do not automatically promote instructions or lessons.
Evaluate any future experiential-memory service separately from this wiki integration.

## Withdrawal, retraction and purge

Rune2 keeps three operations apart (FR-22).

Withdrawal is a retrieval exclusion:

```
bin/alpaca --session ID wiki withdraw event:123 --reason "pasted the wrong log"
```

It records a `wiki-withdrawal` event and an `exclusions` row. From then on no answer, context,
briefing, candidates view, export or public extract serves the source or anything derived from
it, also at an earlier knowledge time: its blocks are retired, the facts read from it are
retracted (a fact they had ended gets its window back), checked candidates that cite it become
stale, admitted ones are retracted, and the lessons and priors those wrote are withdrawn or
decayed. A later capture of the same source does not land it again, and `wiki recover` replays
the withdrawal. Nothing is deleted and no verb undoes an exclusion.

A historical retraction ends a fact's standing and keeps it as history in the store, with its
status, its reason and who made it. The owner supersedes a fact, dates a world change, retracts a
fact or reverts a correction with `wiki correct`, and a proposer takes back a candidate with `wiki
withdraw-candidate`. Current reads stop serving the fact. A superseded fact stays visible to a read
pinned to a knowledge time before the correction; a retracted one is served at no knowledge time,
and only the record and the store's history keep it. The source it was read from stays served.

A purge removes text from where it is kept. No purge verb exists: whether one should is an owner
decision (EX-7). Until it is made, the text of a withdrawn source stays in these places:

- the record, `.alpaca/alpaca.db`: the event that brought it, with its data, in an append-only
  hash chain;
- `.alpaca/transcripts/` and the operator's own transcript files, for a transcript;
- `.alpaca/proofs/`, for a proof report and its logs;
- the vault, `.alpaca/wiki/`: the note or document file and the store's retired blocks;
- every backup under `.alpaca/backups/`: `alpaca backup` copies the record, the vault, the proofs
  and the transcripts, and nothing deletes an old backup.

The same holds for text captured before the secret screen of the retain door (op-008 t-069). An
event note captured before it stays as it was, active and served, until the owner's job `wiki
recover --rerender-notes` renders it again through the screen: a capture, a plain `wiki recover`
and a `--refresh` keep a stored note as it is and count it in `kept_renderings`. A source copy or
a transcript captured before the screen is stored again, screened, when a later capture or `wiki
recover` lands it again. After that the earlier, unscreened blocks stay as retired rows, which a
read pinned to an earlier knowledge time still reaches. A document added
with `wiki ingest` before then keeps its unscreened vault file until it is ingested again. A store
rebuilt from the record (below) holds none of that text, because every note is rendered again
through the screen; removing it any other way is a purge. An owner correction or an admission
recorded before the screen names its fact by text; where the screen changes that text (a value it
now masks), the correction or admission no longer finds its fact after `wiki recover
--rerender-notes` or a rebuild: a rebuild or an import counts it `stale`, and the fact that
`--rerender-notes` writes is a new one.

## Rebuild, export, import and rollback

The store is derived state. `bin/alpaca wiki recover` on an empty store rebuilds it from the record,
and every event lands at the time the record gives it. Move the whole vault, `.alpaca/wiki/`, aside
first: it holds the store, its ledger and the note files, and a ledger left behind refuses a new
store. Export first to keep the documents added with `wiki ingest`. Live capture lags the event and the record keeps no capture time, so the
rebuilt store's knowledge time comes from the record: it may know at a time K what the live store
learned only after K. Every read pinned to a knowledge time on such a store carries the trace note
`knowledge-time-from-record`, which `wiki context` prints, and `wiki status` reports
`knowledge_time: record` for it (`capture` for a live store). On a live store `wiki recover` is a
catch-up: it lands what capture missed, at the time it lands it, and applies again any recorded
act whose store write was lost, a consolidation proposal included, so the job, which resumes from
its checkpoint, does not propose it twice. `wiki status` shows each job's checkpoint, the record
events it still has to read (`remaining`) and its failed items.

A rebuild keeps one version of each source. A replaced version stays visible to a read pinned to
an earlier knowledge time on the store that captured it, and is gone after a rebuild or an import.
A document added with `wiki ingest` is not in the record, so a rebuild does not bring it back; an
export does.

Each capture that lands a transcript version (the hooks, the Codex verbs, the collector, `wiki
recover`) appends one `transcript-version` event: ref `transcript:<session>`, writer session
`wiki-capture`, with the content sha256, the byte count and sha256 of the transcript bytes read, and
the record head the capture read. The event is appended inside the store's write lock of the
landing, so a session's events follow the order the store landed its versions. A capture of a
version the record already names appends nothing; a `wiki recover` into an empty store (a rebuild)
appends one event for each transcript session it lands.
A rebuild or an import at a cutoff before the record head reads each transcript at the version the
newest such event at or below the cutoff names, so a transcript that grew or was registered after
the cutoff leaves it as it was. Sessions captured before these events existed are read best-effort
(the newest `transcript-snapshot` at or below the cutoff, else the file as it is now), and the
report's `transcript_cutoff.best_effort` lists each one with how it was read. At the record head a
rebuild reads every transcript as its file is now.

Export and import move the accepted state to a fresh store (FR-23):

```
bin/alpaca wiki export accepted.jsonl
bin/alpaca wiki import accepted.jsonl
```

The export is `rune2-portable/1` JSON lines, readable by its owner only. It names the record
cutoff and holds the source envelopes, the facts with their anchors and support claims, the
admitted candidates, the corrections, the exclusions, and the lessons and priors, by source refs
and hashes, never by the store's own block, document or edge ids. It leaves out capture times,
replaced versions and every text the store may not serve: the quotes of a withdrawn or private
source, the subject and object of a fact read from one (in the fact's own row and in a correction
that names the fact), the replacement and reason of a correction whose own note is withdrawn or
private, the body of a candidate that cites one, a retracted candidate's text, and a withdrawn
lesson or prior travel as sha256 with `redacted: true`, and every text passes the secret screen. A
document stored before the screen goes without its text. `--private` keeps the text of private and
quarantined sources; withdrawn text never leaves.

The import needs an empty vault (move `.alpaca/wiki/` aside, as for a rebuild) and the same record,
holding the cutoff event with the hash the manifest names. It lands the exported documents,
rebuilds the store from the record through the cutoff, and then compares. A row the record backs is
imported. A row it does not back is refused and counted: a fact or lesson without an applied
`wiki-admission` at or below the cutoff, an edited one, a record fact no rule produces, a
correction or exclusion without its event. A row whose source cannot be landed (a document exported
without its text, a transcript whose file is gone) is counted as unrestorable. A `wiki ingest`
document is not in the record, so its row, text and envelope are taken from the export as they
stand, when the text hashes to the row's version; import only an export you made or trust.
Exporting the imported store gives the same bytes when nothing was refused, unrestorable or added
from the record. The imported store takes its
knowledge time from the record, as a rebuild does. A different provider configuration imports the
same accepted state.

Rollback to the code before schema 4 keeps every accepted row: that code ignores the new tables,
sets `schema_version` back to 3, and finds a withdrawn source retired, so its read path serves none
of it. Its capture knows no exclusions, and a capture it runs can land a withdrawn note again and
serve it: `wiki recover`, and also the SessionEnd and PreCompact hooks, which drain the whole
session that ends or compacts, so a session that holds a withdrawn event brings it back. So do not
roll back while an exclusion exists (`wiki status` counts withdrawn events); if a rollback cannot
wait, turn the Rune2 capture off under the old code (remove its SessionEnd and PreCompact hooks and
run no `wiki recover`) until you roll forward. Pouring the current code again finds the accepted
state unchanged, and its read path serves no withdrawn source again, whatever the old code landed,
since the exclusion rows stay.

Coverage limit: the ten missing t-038 transcript originals, and any other registered transcript
whose file is gone, cannot be captured, rebuilt or imported. `wiki status` counts them, every
evaluation report states them in its coverage denominator, and a question scoped to such a session
is answered `unavailable`.

## Re-rendering stored notes (owner runbook)

`bin/alpaca wiki recover --rerender-notes` is the owner's job, never an agent's and never a
step of a session. Nothing breaks without it: stored notes keep their old rendering and are
served as they are, and new events land in the new format.

When to run it: when the old notes themselves should change. Two cases today: notes captured
before the secret screen (t-069) still hold text the screen now masks, and only re-rendered or
newly landed notes carry the record facts that op-008 extracts from notes.

What it costs (measured on isolated copies of the t-318 replay root, never on the live store):

- It is a full `wiki recover` first: it replays every recorded session and checks the
  store. A default recover of the copy took 4 h 33 min and wrote 28.5 GB, and its first run
  also clears the legacy block validity rows in one transaction (fix-b2-b4.md section 4).
- Then one write per stored note whose rendering changed. Each is a store transaction plus one
  fsync of the note file in the vault. On 2026-10-07, 200 notes took 879 s on a fresh copy:
  median 0.32 s, mean 4.4 s, p95 34 s, max 106 s per note
  (`.alpaca/proofs/op-008/t-334.logs/rerender-200.jsonl`). A profile of 40 more notes put 74 %
  of the time in that fsync and 11 % in the ledger lock's fchmod, while the host's IO pressure
  (`/proc/pressure/io`, some avg60) was 30-50 % from other work; the store queries took about
  10 %. So the per-note cost mostly follows the disk.
- Every sampled stored note renders differently (679 of the 679 present in a sample of 800
  events), and the live store holds about 116,000 event notes (2026-10-07). At the median that
  is about 10 hours, at the mean of that day about 6 days, on top of the recover itself.
- The process reads the echo index of every active block once at the start (52 s and 3.8 GiB
  RSS on the copy).

Before the run:

1. Pick a window with a quiet disk and no session working. Check `cat /proc/pressure/io`;
   `some avg60` under about 5 % is a quiet disk.
2. Keep observability on (`bin/alpaca collect status`; `bin/alpaca collect enable` if it is
   off), so no hook writes the wiki store. Then stop the collector:
   `systemctl --user stop alpaca-<id>-collector.service` (the unit `bin/alpaca collect services`
   generated). Leave it stopped until you have checked the result.
3. Back up. `bin/alpaca backup create` packages the record and the store and checks the package.
   For a quick rollback also copy the whole vault while nothing writes it:
   `cp -a .alpaca/wiki .alpaca/wiki-before-rerender-$(date +%Y%m%d)` (about 5.5 GB on
   2026-10-07; check free space first).
4. Optional: time it on a copy first, either a restored backup
   (`bin/alpaca backup restore PATH DEST`, then run the job in DEST) or a sample with
   `.alpaca/proofs/op-008/t-334.logs/scripts/time_rerender.py` on a copy, as above.

The run (long, so detach it from the chat; a closed terminal must not stop it):

```
cd <project root>
systemd-run --user --unit alpaca-rerender --nice 19 --property IOSchedulingClass=idle \
  --working-directory "$PWD" bash -c 'bin/alpaca wiki recover --rerender-notes \
  > .alpaca/rerender-report.json 2> .alpaca/rerender-progress.log'
journalctl --user -u alpaca-rerender -f        # or: tail -f .alpaca/rerender-progress.log
```

Progress goes to stderr every 100 sessions (`wiki recovery: N/M sessions`). Stopping the unit
rolls back only the note in progress: the notes already written stay re-rendered, and a later
run skips them and rewrites the rest (after replaying every session again).

Check the result before starting the collector again:

- The exit status is 0, or 2 when only registered transcripts are missing (as on the copy).
  In `.alpaca/rerender-report.json`: `status`, `rerender_notes: true`, `kept_renderings: 0`,
  `verified_events` and `verified_transcripts` as large as expected, and no
  `missing_transcripts` you did not already know about. A failed integrity or foreign key
  check ends the run with a nonzero exit and a `drain-failed` event (where `wiki-recover`)
  instead of a report.
- The record holds a `wiki-recovered` (or `wiki-recovery-incomplete`) event with the same
  report.
- `bin/alpaca wiki status` shows the corpus; `bin/alpaca wiki context "<a known question>"`
  answers with cited notes.

Then start the collector (`systemctl --user start alpaca-<id>-collector.service`) and watch
`bin/alpaca collect status` catch up on the events recorded during the run.

Rollback (before the collector starts again, so its checkpoint has not moved past the copy):

```
systemctl --user stop alpaca-<id>-collector.service      # if it is running
mv .alpaca/wiki .alpaca/wiki-rerendered-$(date +%Y%m%d)
cp -a .alpaca/wiki-before-rerender-<date> .alpaca/wiki
systemctl --user start alpaca-<id>-collector.service
```

The record keeps the run's `wiki-recovered` event; it is history, not state. If the collector
already ran after the re-render, the restored store lacks the events it landed in the meantime:
`bin/alpaca wiki status` lists them as missing, and only a default `bin/alpaca wiki recover`
(hours) lands them again. Delete the vault copy only once you keep the re-rendered store.
