# The research engine

The research engine runs a loop of agent attempts against a frozen contract and keeps only what the
contract's own checks accept. It has two modes:

- improve: lower or raise a measured number (a size, a time, a count) under the gates the
  contract names, toward the targets the owner set;
- repair: fix bugs, each one proved by a test that fails before the fix and passes after it, while
  the old suite keeps passing.

A contract is a format 3 runbook (`runbook: 3` with a `research` block). When a run starts, the
engine freezes the contract and its pack files by hash, and from then on every accept comes from
that frozen copy: no agent sets a metric, a target, a round count or a budget. Around each attempt
sits the trust layer: the gate chain, a tamper check of the protected paths, an audit of the
attempt, and two verifiers that check the proof without seeing each other. The engine never raises
the autodrive level, never sets its own target, and never ships: adopting a champion, pushing and
publishing stay with the owner.

This guide has two parts. The operator guide says how to run the engine and where the owner
decides. The engineer guide says how to write a contract and a pack. Related pages:

- [operators.md](operators.md), section "Research runs in the session": the job loop, the guard
  and the witnesses in more depth.
- [runbook-format.md](runbook-format.md), section "The research block": the meaning, type, default
  and error code of every research field.
- [interview.md](interview.md), section "Research contracts": the research interview slots.
- [operations-hub.md](operations-hub.md): the hub pages, the research view among them.

## Operator guide

### The /autoresearch skill

The usual way to run the engine is the `/autoresearch` skill (`.claude/skills/autoresearch/SKILL.md`)
in the operator's own Claude Code session. In order, it:

1. reads the open runs and the level with `bin/alpaca research status --json`;
2. picks a contract from `bin/alpaca research contracts --json`, or fills a new one through the
   research interview, the spec step and the runbook forge;
3. runs the no-op control first when the contract's burn-in is `needed`;
4. starts the real run with `--via autoresearch`;
5. drives the run: it asks `next-job` for the jobs the runner issued, spawns one subagent of its
   own session for each job, and files each finished job with `submit`;
6. stops at every owner decision and shows the command the owner types;
7. writes the report with `report` and hands over the pointers.

A real run happens only inside the operator's session, whose subagents do the agent jobs. The engine
never starts an agent process itself, and no real run is ever launched headless: neither
`claude -p` nor `codex exec` is a way to run it. Claude Code is the supported runtime; a Codex
session gets a job's transcript witness only, as [operators.md](operators.md) explains.

### Levels

The autodrive level decides where the skill asks the operator a question, and it never changes what
the owner decides. A session run needs level L3 or above: below that, `start`, `resume` and
`extend` refuse with `RESEARCH-LEVEL-TOO-LOW` (BLOCKED, exit 2), because the loop of `next-job` and
`submit` would need a question before every call. At L1 and L2 the skill prints the start command
and stops. The owner sets the level; the skill reads it from `bin/alpaca research status --json`
(`level` and `level_source`) and never raises it. The level table is in MANUAL.md, "The autodrive
levels and what each one asks of you".

### Owner decisions

Four holds stop a run until the owner answers, at every level. Each makes the verb that meets it
exit 3 (PAUSED) and prints the command the owner types:

| hold | code | when it opens | the owner's answer |
|---|---|---|---|
| extension | `RUN-ROUNDS-REACHED` | the run reached its N rounds | `grant RUN extend --rounds M`, or `grant RUN stop` |
| budget | `RUN-BUDGET-HOLD` | a cap the contract or a grant set is reached | `grant RUN budget` with raised caps, or `grant RUN stop` |
| loosen | `CONTRACT-LOOSEN-UNGRANTED` | a runbook edit or a task override loosens a field that sets the bar | `grant --runbook PATH loosen --set FIELD=VALUE` |
| ship | `RUN-SHIP-DECISION` | `report` on a finished real run with something to ship | the owner moves the ship card with `bin/alpaca review move` |

The owner types each grant at an interactive terminal of their own, outside any agent session. The
grant checks its terms and the open hold first (`RESEARCH-HOLD-NONE` when no hold of that kind is
open, `RESEARCH-RUN-UNKNOWN` for a run id the record lacks), then asks the owner to type the run id
or the runbook path back. It refuses with `RESEARCH-OWNER-NOT-TERMINAL` (FAIL, exit 1) when an
agent marker is set in the shell or stdin is not a terminal, and with
`RESEARCH-OWNER-CONFIRM-MISMATCH` when the typed text differs. These are the owner's commands for a
run `rr-001` and the sortbench pack used in "Try it" below, with example values:

```sh owner-only
# typed by the owner at their own terminal, outside any agent session
bin/alpaca research grant rr-001 extend --rounds 3
bin/alpaca research grant rr-001 budget --usd 25 --tokens 2000000 --wall 7200
bin/alpaca research grant rr-001 stop --note "enough for this quarter"
bin/alpaca research grant --runbook packs/sortbench/improve.yaml loosen --set research.budget.usd=60
bin/alpaca research grant --runbook packs/sortbench/improve.yaml budget
bin/alpaca research grant --runbook packs/sortbench/improve.yaml reopen
```

On a run a session drives, an `extend` or `budget` grant records the decision and starts nothing:
the driving session then types `extend` or `resume`, which the grant names. A `stop` grant stops the
run at once, whether a session drives it or not. A stop at a hold is final for that runbook:
`start` refuses a new real run under it with `RESEARCH-RUNBOOK-STOPPED` (PAUSED, exit 3) until the
owner's `reopen` grant.

Budget. A run is uncapped by default, in cost and in time: every field of `research.budget` is
optional, and a run whose contract sets none is never held on spend. A cap exists only when the
runbook, its pack defaults file or a task override sets it on purpose, or when the owner grants one.
Spend is priced from the kept transcripts and reported in either case. The caps are
`research.budget.usd` in US dollars, `research.budget.tokens` and `research.budget.wall` in seconds.
Research spend also books to the project allowance in `project.yaml`, but that allowance never
holds a research run.

The guard that keeps subagents away from the record, the hooks and the engine code has two declared
residuals the owner confirms at the first ship decision: a write assembled at run time that the
guard's text check cannot see, and engine and hook files that the owner's own user can still write.

### Who may run which verb

| verb | where it runs |
|---|---|
| `start`, `resume`, `extend`, `stop`, `next-job`, `submit` | only in the session that drives the run; a shell that carries the subagent marker is refused with `RUN-DRIVER-IS-SUBAGENT` (BLOCKED, exit 2) before the verb does anything |
| `grant` | any shell may type it, but it succeeds only at the owner's interactive terminal |
| `status`, `report`, `contracts`, `template` | any shell |

The owner's record-only steps `resume --repin-engine` and `extend --repin-engine` run at the
owner's own terminal, which carries no subagent marker.

### Output and exit codes

Each verb prints lines for a person and ends with one result line,
`GATE alpaca-research-<verb>: <WORD>`. With `--json` it prints one JSON object instead, whose
`verdict` holds the word. A refusal names its reason code twice: at the end of the result line, as
`(CODE: reason)`, and in the `code` and `reason` keys of the JSON object. Every sub-verb takes
`--json`.

| exit | word | meaning |
|---|---|---|
| 0 | PASS | done |
| 1 | FAIL | refused or failed; the reason code says why |
| 2 | BLOCKED | the verb cannot run here: the level, the marker, an unreadable record, a missing tool |
| 3 | PAUSED | an owner decision is open; nothing goes on past it on that run |
| 64 | usage | the command line is wrong |

Reason codes come in families named by their first word: RUN (the run and its runner), RESEARCH
(the verbs), CONTRACT (the contract loader), JOB (the job protocol of a session run), REPAIR (repair
cases), GATE (the gate chain), ISOLATION (the attempt worktree), VERIFY (the two verifiers) and
AUDIT (the attempt audit).

### The research verbs

Each verb below has a table of its arguments, then its grammar or an example. The values in the
examples (`rr-001`, the job ids, the nonces) are examples; a real run prints its own.

#### `alpaca research start`

Freezes the contract of a format 3 runbook and starts the loop runner of a new run. No flag sets a
metric, a target, the slots K, the rounds N or a budget: they come only from the frozen contract.
Before the run opens, `start` refuses on an open hold under the runbook (`RESEARCH-HOLD-OPEN`, exit
3), on the owner's stop (`RESEARCH-RUNBOOK-STOPPED`, exit 3), on a live run of the same kind
(`RESEARCH-CONTRACT-BUSY`), and on a real run whose no-op burn-in is not done.

| flag | value | meaning |
|---|---|---|
| `--runbook` | a `runbook: 3` file, relative to the root | the contract; required |
| `--task` | a task id | the task whose contract's research part overrides the pack; a loosening there needs the owner's loosen grant |
| `--op` | an op id | the op that holds the run's attempt tasks |
| `--control` | `noop` or `random` | start a control run: the no-op agent (the burn-in) or the random-edit agent, under the same contract and gates |
| `--via` | `cli` (default) or `autoresearch` | what started the run; recorded on its start event |
| `--model` | a model alias of the shield policy, such as `opus` | one model for every agent job of the run, recorded on its start; without it each role keeps its tier |
| `--foreground` | none | run the loop runner in this process instead of in the background; the skill never passes it |
| `--json` | none | print one JSON object |

```sh driver
# typed in the driving session only (the /autoresearch skill does this)
bin/alpaca research start --runbook packs/sortbench/improve.yaml --control noop --via autoresearch --json
bin/alpaca research start --runbook packs/sortbench/improve.yaml --via autoresearch --json
bin/alpaca research start --runbook packs/sortbench/improve.yaml --task t-012 --op op-003 --model opus --json
```

#### `alpaca research status`

Folds one run, or every run, from the record: state, round, champion against baseline and target,
budget spent, open holds, whether the runner is alive, and the level in force. For a session run it
adds the driver, the jobs out and the state of the engine pin. With no run it exits 0 whatever the
holds; with a run it exits 3 while a hold is open on it, 1 for a failed run and
`RESEARCH-RUN-UNKNOWN` (exit 1) for an id the record lacks.

| flag | value | meaning |
|---|---|---|
| run (positional, optional) | a run id such as `rr-001` | the one run to show; left out, every run |
| `--json` | none | print one JSON object; with no run it carries `level`, `level_source` and `runs` |

```text synopsis
alpaca research status [<run>] [--json]
```

#### `alpaca research stop`

Stops a run and signals its runner. A run waiting at an extension or budget hold is the owner's
question, so stopping it there is the owner's answer: it asks for the owner's confirmation at a
terminal and, in any other shell, exits 3 with `RESEARCH-OWNER-GRANT-MISSING` and the command the
owner types. A finished run is refused with `RESEARCH-RUN-FINISHED`.

| flag | value | meaning |
|---|---|---|
| run (positional) | a run id | the run to stop |
| `--reason` | text | why; recorded on the stop event |
| `--json` | none | print one JSON object |

```sh driver
# driving session only; the owner says when a run with no hold stops
bin/alpaca research stop rr-001 --reason "the owner asked to stop" --json
```

#### `alpaca research extend`

Lifts an extension hold that the owner's grant answered, records the new round count and resumes
the run. Without a terminal-confirmed grant it exits 3 with `RESEARCH-OWNER-GRANT-MISSING`; on a
run with no extension hold it refuses with `RESEARCH-RUN-NOT-HALTED`.

| flag | value | meaning |
|---|---|---|
| run (positional) | a run id | the run at its extension hold |
| `--decision` | a decision id | the owner's grant to use; left out, the newest grant that answers the open hold |
| `--no-resume` | none | lift the hold and record the extension, but do not restart the runner; `resume` starts it later |
| `--model` | a model alias | the model the run's start recorded; another model is refused with `RUN-MODEL-MISMATCH` |
| `--repin-engine` | none | the owner's record-only step after a stop with `RUN-ENGINE-CHANGED`: records a new pin of the engine files and starts nothing |
| `--context-path` | a file, repeatable | with `--repin-engine`: a file the runtime loaded that no root holds, kept in the new pin |
| `--json` | none | print one JSON object |

```sh driver
# driving session, after the owner's extension grant
bin/alpaca research extend rr-001 --json
bin/alpaca research extend rr-001 --decision dec-0a1b2c3d4e5f --no-resume --model opus --json
# typed by the owner in a terminal of their own, after RUN-ENGINE-CHANGED
bin/alpaca research extend rr-001 --repin-engine --context-path notes/agent-context.md --json
```

#### `alpaca research resume`

Starts a halted, paused, killed or interrupted run again: after a crash, after a budget grant, or
after a hold the owner lifted. It refuses a finished, stopped or failed run
(`RESEARCH-RUN-FINISHED`), a run whose runner is alive (`RESEARCH-RUN-ACTIVE`) and a run still at its
extension hold (`RESEARCH-HOLD-OPEN`, exit 3). A session run checks the engine pin again; a changed
engine file ends in `RUN-ENGINE-CHANGED`, which the owner answers with `--repin-engine`.

| flag | value | meaning |
|---|---|---|
| run (positional) | a run id | the run to start again |
| `--decision` | a decision id | the owner's budget grant to use; left out, the newest one that answers the open hold |
| `--model` | a model alias | the model the run's start recorded; another model is refused with `RUN-MODEL-MISMATCH` |
| `--repin-engine` | none | the owner's record-only step after `RUN-ENGINE-CHANGED`: records a new engine pin and starts nothing |
| `--context-path` | a file, repeatable | with `--repin-engine`: a file the runtime loaded that no root holds |
| `--json` | none | print one JSON object |

```sh driver
# driving session
bin/alpaca research resume rr-001 --json
bin/alpaca research resume rr-001 --decision dec-0a1b2c3d4e5f --model opus --json
# typed by the owner in a terminal of their own
bin/alpaca research resume rr-001 --repin-engine --context-path notes/agent-context.md
```

#### `alpaca research report`

Builds the run report from the record alone and writes it; "The run report" below says what it
holds. It exits 1 for a failed run, 3 while a hold is open (the ship hold included) and 0 otherwise.

| flag | value | meaning |
|---|---|---|
| run (positional) | a run id | the run to report |
| `--out` | a folder inside the root | where report.md and report.json go; the default is `.alpaca/reports/research/<run>/` |
| `--json` | none | print one JSON object with the paths and hashes |

```text synopsis
alpaca research report <run> [--out <dir>] [--json]
```

#### `alpaca research grant`

The owner's verb. It records an owner decision and a `research-grant` event, after the checks of
"Owner decisions" above and the owner's typed confirmation. With a run it answers that run's hold
and applies the answer at once: extend and resume, resume, or stop. Two cases are recorded only:
`extend` and `budget` on a session run, and `extend` and `budget` with `--no-apply`. A `stop` is
applied at once on every run, a session run included; `--no-apply` with it is a usage error (exit
64).

| flag | value | meaning |
|---|---|---|
| words (positional) | `RUN extend`, `RUN budget` or `RUN stop`; or, with `--runbook`, `loosen`, `budget` or `reopen` | the run and the gate the grant answers |
| `--runbook` | a runbook path | the contract form: a grant on the contract, not on a run |
| `--task` | a task id | with `--runbook`, for `loosen` and `budget`: the task layer of the contract |
| `--rounds` | a whole number | with `extend`: the new round count |
| `--usd` | a number | with `budget`: the new dollar cap, in US dollars |
| `--tokens` | a whole number | with `budget`: the new token cap |
| `--wall` | whole seconds | with `budget`: the new wall-time cap |
| `--set` | `FIELD=VALUE`, repeatable | with `loosen`: each loosened field and the value the owner accepts |
| `--note` | text | recorded with the grant |
| `--no-apply` | none | with `RUN extend` or `RUN budget`: record the grant and leave the run held, for `extend` or `resume` to apply later |
| `--json` | none | print one JSON object |

```text synopsis
alpaca research grant <run> extend --rounds <n> [--note <text>] [--no-apply] [--json]
alpaca research grant <run> budget [--usd <usd>] [--tokens <n>] [--wall <seconds>] [--note <text>] [--no-apply] [--json]
alpaca research grant <run> stop [--note <text>] [--json]
alpaca research grant --runbook <file> [--task <task>] loosen --set <field=value> [--note <text>] [--json]
alpaca research grant --runbook <file> [--task <task>] budget [--note <text>] [--json]
alpaca research grant --runbook <file> reopen [--note <text>] [--json]
```

#### `alpaca research contracts`

Lists the format 3 runbooks that git tracks in the project (outside `.alpaca/`, `vendor/`,
`templates/` and the reference clones), each with its id, mode, the result of the runbook check,
K and N, the contract and pack hashes, the burn-in state, its runs, its open holds and whether a
reopen is needed. It exits 2 when one of them cannot be read, and 0 otherwise.

| flag | value | meaning |
|---|---|---|
| `--json` | none | print one JSON object with a `contracts` list |

```text synopsis
alpaca research contracts [--json]
```

#### `alpaca research next-job`

The driving session asks for the jobs the runner has issued. The answer's `action` is `spawn` (the
jobs in `jobs`, one subagent each), `wait`, `hold` (exit 3: an owner decision), `resume` (exit 2:
the runner is gone and the run should be resumed) or `report` (the runner has exited and no job is
open). Only the session that drives the run may ask (`JOB-NOT-DRIVER`). `expired` lists jobs whose
deadline passed; their agents should be stopped.

| flag | value | meaning |
|---|---|---|
| run (positional, optional) | a run id | the run to ask about; give it or `--verification`, not both |
| `--verification` | a verification id | the jobs of a proof verification outside a run |
| `--wait` | seconds, at most 300 (default 0) | wait this long for a job to hand out |
| `--limit` | a whole number (default 8) | the most jobs handed out in one answer |
| `--list` | none | list the open jobs and hand out nothing; never prints a spawn prompt |
| `--with-prompt` | none | add each job's prompt text to the answer (for a scripted session) |
| `--json` | none | print one JSON object |

```sh driver
# driving session
bin/alpaca research next-job rr-001 --wait 45 --json
bin/alpaca research next-job rr-001 --list
bin/alpaca research next-job rr-001 --limit 2 --with-prompt --json
bin/alpaca research next-job --verification vf-t-031-4512 --wait 45 --json
```

#### `alpaca research submit`

The driving session files its claim that a subagent finished a job, and waits for the runner's
judge. The judge checks the claim against the two witnesses (the hook pool and the kept transcript)
and accepts it or refuses it with a JOB code. Exit 2 with `retriable` true, `JOB-PENDING` or
`JOB-GROUP-WAIT` means: the claim is filed, repeat the call later. Exit 1 means the try is lost and
the runner retries the job by itself.

| flag | value | meaning |
|---|---|---|
| job (positional) | a job id from `next-job` | the job the agent finished |
| `--nonce` | the job's nonce from `next-job` | required |
| `--agent-id` | the subagent's id | required |
| `--transcript` | a file | the agent's transcript; it must be the file the hook registered |
| `--final` | a file | the agent's final message as the driver saved it; it must equal the transcript's |
| `--usage-json` | a JSON file | the usage the driver read, recorded beside the usage measured from the transcript |
| `--tool` | a tool name | the tool call that spawned the agent, recorded with the claim |
| `--tool-use-id` | an id | that tool call's id; it must be the agent's own |
| `--diagnose` | none | run every check of the judge and print the table of results, not only up to the first failure |
| `--wait` | seconds, at most 300 (default 60) | how long to wait for the judge's answer |
| `--json` | none | print one JSON object |

```sh driver
# driving session; the job id, nonce and agent id come from next-job and the spawn
bin/alpaca research submit rr-001-r1-s1-proposer --nonce 4f2a9c71 --agent-id a17c3e --diagnose --json
bin/alpaca research submit rr-001-r1-s1-implementer --nonce 9b03d5e2 --agent-id a2b4d6 --usage-json usage.json --transcript agent.jsonl --final final.txt --tool Agent --tool-use-id toolu_01 --wait 120 --json
```

#### `alpaca research template`

Prints the fill-in contract template of a mode, or copies it to a new file. It never overwrites a
file: an existing `--out` path is refused with `RESEARCH-TEMPLATE-EXISTS` (exit 1).

| flag | value | meaning |
|---|---|---|
| `--mode` | `improve` or `repair` | which template; required |
| `--out` | a path inside the root | copy the template there instead of printing it |
| `--json` | none | print one JSON object; without `--out` it names the template path instead of printing the text |

```text synopsis
alpaca research template --mode improve|repair [--out <file>] [--json]
```

### The run report

`bin/alpaca research report rr-001` reads only the run's own events and writes
`.alpaca/reports/research/<run>/report.md` and `report.json` (or the `--out` folder), so the same
events always give the same bytes. The Markdown report has these sections: the run, how it ended,
the repair tasks (repair mode) or the metrics and rounds (improve mode), the champion lineage, the
attempts by outcome, the verifier, audit and tamper counts, the agent jobs, the holds and grants,
the budget, the ship card, how to reproduce it and the evidence. When a finished real run has
something to ship (an improve champion that differs from its base commit, or a resolved repair
task), the first `report` opens the ship hold, once per run, and exits 3 until the owner moves the
card.

### The research run view

The hub shows every run on its Research page, fed by `/hub/research.json`, which is built from the
record alone. [operations-hub.md](operations-hub.md) describes the page, the one-run view, the
answers of the route and the column sources.

## Engineer guide

### The fill-in contract template

`bin/alpaca research template --mode improve --out <file>` copies `templates/research/improve.yaml`
(or `templates/research/repair.yaml` for `--mode repair`) to a new file. The template holds one line
for every field of a research runbook in that mode. Each value to fill is a quoted placeholder,
`"<FILL:path>"`, and each line's comment marks the field REQUIRED, REQUIRED-when a condition holds,
or OPTIONAL with its default, and names the interview slot that asks for it. Fill each placeholder,
copy each item marked "one item per ..." once per item, and delete every line you leave unfilled.
Then check the file with `bin/alpaca runbook check <file> --spec <spec>`, which refuses a value that
still holds a placeholder (`FIELD-UNFILLED`) and names any required field that is missing
(`FIELD-MISSING`).

The research interview asks the same questions in rounds: `bin/alpaca interview use research` turns
on the fourteen research slots of `templates/interview/research-slots.yaml`, and the interview skill
then asks them with the default slots.

### The research block of runbook format 3

A format 3 runbook keeps the format 2 stages and checks and adds one `research` block. The table
below names every field of that block, as `research_fields()` in `alpaca/runbook.py` reads them.
"Required" in a column means `bin/alpaca runbook check` refuses the merged block of that mode
without the field. An item field of an optional list reads "no" here even when it is needed inside
each entry: the row of the list itself says when the list is required, and
[runbook-format.md](runbook-format.md) marks such item fields as required inside an entry. The
meaning, type, default and error code of each field are in that file, section "The research block".

| field | required in improve | required in repair |
|---|---|---|
| `research.mode` | yes | yes |
| `research.defaults` | no | no |
| `research.repo` | no | no |
| `research.repo.path` | no | no |
| `research.repo.ref` | no | no |
| `research.metrics` | yes | no |
| `research.metrics[].covers` | yes | no |
| `research.metrics[].source` | no | no |
| `research.metrics[].id` | yes | no |
| `research.metrics[].description` | no | no |
| `research.metrics[].command` | yes | no |
| `research.metrics[].workdir` | no | no |
| `research.metrics[].parser` | yes | no |
| `research.metrics[].path` | no | no |
| `research.metrics[].field` | no | no |
| `research.metrics[].unit` | yes | no |
| `research.metrics[].direction` | yes | no |
| `research.metrics[].kind` | yes | no |
| `research.metrics[].repeats` | no | no |
| `research.metrics[].min_ok` | no | no |
| `research.metrics[].median` | no | no |
| `research.metrics[].margin` | no | no |
| `research.metrics[].seeds` | no | no |
| `research.metrics[].timeout` | yes | no |
| `research.metrics[].target` | no | no |
| `research.metrics[].artifact` | no | no |
| `research.accept` | yes | yes |
| `research.accept.covers` | no | no |
| `research.accept.source` | no | no |
| `research.accept.rule` | yes | yes |
| `research.accept.after_targets` | no | no |
| `research.paths` | yes | yes |
| `research.paths.covers` | no | no |
| `research.paths.source` | no | no |
| `research.paths.editable` | yes | yes |
| `research.paths.protected` | yes | yes |
| `research.paths.hidden` | no | no |
| `research.paths.tests` | no | no |
| `research.paths.read` | no | no |
| `research.paths.keep` | no | no |
| `research.paths.outputs` | no | no |
| `research.fingerprint` | yes | yes |
| `research.fingerprint.covers` | no | no |
| `research.fingerprint.source` | no | no |
| `research.fingerprint.tools` | yes | yes |
| `research.fingerprint.shared` | no | no |
| `research.gates` | yes | yes |
| `research.gates[].stage` | yes | yes |
| `research.gates[].heavy` | no | no |
| `research.gates[].lock_timeout` | no | no |
| `research.stop` | yes | yes |
| `research.stop.covers` | no | no |
| `research.stop.source` | no | no |
| `research.stop.rounds` | yes | yes |
| `research.stop.slots` | yes | yes |
| `research.budget` | no | no |
| `research.budget.covers` | no | no |
| `research.budget.source` | no | no |
| `research.budget.usd` | no | no |
| `research.budget.tokens` | no | no |
| `research.budget.wall` | no | no |
| `research.disk` | yes | yes |
| `research.proposal` | no | no |
| `research.proposal[].field` | no | no |
| `research.proposal[].type` | no | no |
| `research.proposal[].required` | no | no |
| `research.proposal[].values` | no | no |
| `research.proposal[].max` | no | no |
| `research.proposal[].description` | no | no |
| `research.categories` | yes | no |
| `research.categories[].id` | yes | no |
| `research.categories[].text` | yes | no |
| `research.lessons` | no | no |
| `research.lessons.file` | no | no |
| `research.lessons.note_chars` | no | no |
| `research.controls` | yes | yes |
| `research.controls.covers` | no | no |
| `research.controls.source` | no | no |
| `research.controls.noop_rounds` | yes | yes |
| `research.controls.random` | yes | yes |
| `research.controls.mutate` | no | no |
| `research.controls.syntax` | no | no |
| `research.controls.seed` | no | no |
| `research.holdout` | no | no |
| `research.holdout.covers` | no | no |
| `research.holdout.source` | no | no |
| `research.holdout.stages` | no | no |
| `research.holdout.paths` | no | no |
| `research.coupled` | no | no |
| `research.coupled[].id` | no | no |
| `research.coupled[].files` | no | no |
| `research.coupled[].note` | no | no |
| `research.runtime` | no | no |
| `research.runtime.timeouts` | no | no |
| `research.runtime.timeouts.proposer` | no | no |
| `research.runtime.timeouts.implementer` | no | no |
| `research.runtime.timeouts.notes` | no | no |
| `research.runtime.timeouts.audit` | no | no |
| `research.runtime.timeouts.reproducer` | no | no |
| `research.runtime.timeouts.reviewer` | no | no |
| `research.runtime.effort` | no | no |
| `research.runtime.effort.proposer` | no | no |
| `research.runtime.effort.implementer` | no | no |
| `research.runtime.effort.notes` | no | no |
| `research.runtime.effort.audit` | no | no |
| `research.runtime.effort.reproducer` | no | no |
| `research.runtime.effort.reviewer` | no | no |
| `research.runtime.retries` | no | no |
| `research.verifier` | no | no |
| `research.verifier.timeout` | no | no |
| `research.verifier.rerun_timeout` | no | no |
| `research.audit` | no | no |
| `research.audit.hints` | no | no |
| `research.audit.assert_tokens` | no | no |
| `research.audit.build_files` | no | no |
| `research.audit.steering` | no | no |
| `research.egress` | no | no |
| `research.egress.allow` | no | no |
| `research.repair` | no | yes |
| `research.repair.cases` | no | yes |
| `research.repair.cases[].covers` | no | yes |
| `research.repair.cases[].source` | no | no |
| `research.repair.cases[].id` | no | yes |
| `research.repair.cases[].bug_report` | no | yes |
| `research.repair.cases[].base` | no | no |
| `research.repair.cases[].fail_to_pass` | no | yes |
| `research.repair.cases[].fail_to_pass.origin` | no | yes |
| `research.repair.cases[].fail_to_pass.command` | no | no |
| `research.repair.cases[].fail_to_pass.tests` | no | no |
| `research.repair.cases[].fail_to_pass.timeout` | no | no |
| `research.repair.cases[].fail_to_pass.repeats` | no | no |
| `research.repair.cases[].suspect_files` | no | no |
| `research.repair.pass_to_pass` | no | yes |
| `research.repair.pass_to_pass.covers` | no | no |
| `research.repair.pass_to_pass.source` | no | no |
| `research.repair.pass_to_pass.command` | no | yes |
| `research.repair.pass_to_pass.min_tests` | no | yes |
| `research.repair.pass_to_pass.timeout` | no | yes |
| `research.repair.prepare` | no | no |

### The results block a runner prints

A stage or metric command that reports tests or a number prints one results block. The gate chain
makes a fresh token for every run of the command and hands it over on file descriptor 3, whose
number is also in the environment variable `ALPACA_RESULTS_FD`. The runner reads the token, closes
descriptor 3 before it starts any code it tests, runs that code in a child process, and prints:

- `ALPACA-RESULTS-BEGIN` on a line of its own (no token);
- one `TEST: <name> ... PASS <token>` line per test, with PASS, FAIL or SKIP;
- one `METRIC: <id> <number> <token>` line per metric;
- `EVIDENCE: <path> <sha256> <token>` for a file a json-field metric reads;
- `ALPACA-RESULTS-END <token>` to close the block.

A marker line without the token, or an `ALPACA-RESULTS-END` line with no token or another one, is a
conflict, and the gate chain records the attempt as `GATE-MARKERS`. The token keeps tested code
from printing lines that count: the child never holds descriptor 3, so a line it prints carries no
token. A small runner in Python:

```python
import os
import subprocess
import sys

# the token comes on the descriptor ALPACA_RESULTS_FD names; read it and close it first
with os.fdopen(int(os.environ["ALPACA_RESULTS_FD"]), "r") as fd:
    token = fd.readline().strip()

# the tested code runs in a child process that never holds the descriptor
child = subprocess.run([sys.executable, "-c", "print(sum(range(10)))"],
                       capture_output=True, text=True)
answer = child.stdout.strip() or "0"
result = "PASS" if child.returncode == 0 and answer == "45" else "FAIL"

print("ALPACA-RESULTS-BEGIN")
print(f"TEST: sum_of_ten ... {result} {token}")
print(f"METRIC: answer {answer} {token}")
print(f"ALPACA-RESULTS-END {token}")
```

Its output, with `<token>` standing for the run's token:

```text
ALPACA-RESULTS-BEGIN
TEST: sum_of_ten ... PASS <token>
METRIC: answer 45 <token>
ALPACA-RESULTS-END <token>
```

### The reference pack

The development tree of Alpaca holds one reference pack, used to accept the engine end to end. It
is not part of an installed copy, so no command in this guide names it. It has one improve runbook
that shrinks a small compression codec (the number is the compressed size plus the size of the
codec's own source), one repair runbook over five fixed bugs of a small open-source library at
pinned commits, a pack defaults file both runbooks share, a spec for each runbook, the improve
verifier scripts (the measuring script, the unit tests, a source check, a repeat check and the
random-edit mutation script), a held-out corpus used only on the final champion, the repair test
runner and case setup scripts, the bug reports, and a hidden folder with the case data agents never
see. The code the improve loop edits lies outside the pack folder, because the pack folder itself is
protected.

### Adding a domain pack

A pack is a folder of one or two format 3 runbooks and the files they use. The sortbench pack that
ships with the tests, `alpaca/tests/fixtures/research/sortbench`, is a small worked example; "Try
it" below copies it into a project. To add one:

1. Make the pack folder, for example `packs/<pack>/`, and write its defaults file `pack.yaml`. Its
   top keys are `pack` (an optional name) and `research` (any research field except `mode`,
   `defaults`, `covers` and `source`); any other top key is refused.
2. Write a spec for each runbook, then the runbook: `runbook: 3`, the stages its metrics and gates
   run, and a `research` block with `research.mode` and `research.defaults: pack.yaml`. Start from
   `bin/alpaca research template` or from the research interview.
3. Write the verifier scripts the stages call. Each prints the results block above, with the token.
4. Protect the pack: list the pack folder in `research.paths.protected`, put the case data agents
   must not see under a folder named in `research.paths.hidden`, and list the tools the gates run in
   `research.fingerprint.tools`.
5. Check each runbook with `bin/alpaca runbook check <runbook> --spec <spec>` until it ends in
   `GATE alpaca-runbook-check: PASS`.
6. Commit the pack: `contracts` lists only runbooks that git tracks.
7. Take it in with `bin/alpaca intake <spec> <runbook>`, then see it with
   `bin/alpaca research contracts`.

## Try it

The blocks below run as written, top to bottom, in a new project made with `bin/alpaca new` (no
packs folder yet). A `# exit: N` note gives the exit code a line has; a line with none exits 0. They
use only the verbs any shell may run. The six verbs that drive a run, and a grant that succeeds,
need a live session and the owner, so they are not here.

Write a contract template, and see that a second copy is refused:

```sh scratch
bin/alpaca research template --mode improve --out drafts/improve.yaml
bin/alpaca research template --mode improve --out drafts/improve.yaml   # exit: 1
bin/alpaca research template --mode repair --json
```

Copy the sortbench pack into the project, check both runbooks, and commit the pack:

```sh scratch
mkdir -p packs && cp -r alpaca/tests/fixtures/research/sortbench packs/sortbench
bin/alpaca runbook check packs/sortbench/improve.yaml --spec packs/sortbench/improve-spec.md
bin/alpaca runbook check packs/sortbench/repair.yaml --spec packs/sortbench/repair-spec.md
git add packs && git commit -q -m "the sortbench research pack"
```

List the contracts and the runs; a new project has no run yet:

```sh scratch
bin/alpaca research contracts
bin/alpaca research contracts --json > contracts.json
bin/alpaca research status
bin/alpaca research status --json
bin/alpaca research status rr-001   # exit: 1
bin/alpaca research report rr-001   # exit: 1
```

The `status` and `report` lines for `rr-001` end in `RESEARCH-RUN-UNKNOWN`. A grant checks its
hold before it asks at the terminal, so these refuse the same way at any shell: the first with
`RESEARCH-RUN-UNKNOWN`, the second with `RESEARCH-HOLD-NONE`, the third with exit 64, since a run
grant needs its gate word, and the fourth with exit 64, since `--no-apply` goes only with `extend`
or `budget`.

```sh scratch
bin/alpaca research grant rr-001 stop < /dev/null   # exit: 1
bin/alpaca research grant --runbook packs/sortbench/improve.yaml budget < /dev/null   # exit: 1
bin/alpaca research grant rr-001 < /dev/null   # exit: 64
bin/alpaca research grant rr-001 stop --no-apply < /dev/null   # exit: 64
```

## Credits

The engine re-implements ideas from two public projects. The studies of both were written first,
and the engine was built from those studies.

- FeSens auto-arch-tournament, https://github.com/FeSens/auto-arch-tournament, by Felipe Sens
  Bonetto, Apache-2.0, studied at commit `3a506537e7f323d56daf36990b8d7eafc87df129`. The improve
  loop's tournament of agent attempts against a measured number comes from this project. FeSens
  also runs the site hwebench.com for it, which is not the HWE-Bench benchmark below.
- HWE-Bench by pku-liang, https://github.com/pku-liang/hwe-bench, Apache-2.0, studied at commit
  `10c78a87e1f92695d78d15b1464a6107dcac8837`. The repair loop's fail-to-pass and pass-to-pass
  tests come from this benchmark. Its paper is arXiv 2604.14709 v3,
  https://arxiv.org/abs/2604.14709, by Cui, Hou, Luo, Yin and Liang, under CC BY 4.0; cite the
  paper when you cite the benchmark. Its dataset is `henryen/hwe-bench` on Hugging Face, Apache-2.0
  per its card at revision `82a42e0a05719366a326e09ddc668ea0d46c91f6`; Alpaca uses no rows of it.

No reference code, prompt text or data is copied into Alpaca. The engine re-implements the ideas
from written studies of the two projects, and its prompts, tests and packs are Alpaca's own. For
that reason `THIRD-PARTY-NOTICES.md` carries no entry for either project.
