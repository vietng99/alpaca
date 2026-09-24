# From notes to intake

This page covers the last two links of the path Alpaca follows from an idea to checked work:

```
raw notes -> input/notes/ (inbox) -> interview (signed brief) -> spec (spec-kit or OpenSpec)
  -> runbook -> intake (rows, task contracts, profile) -> runs with sealed proof
```

The inbox and the interview (`alpaca note`, `alpaca interview` and the skill `/alpaca-interview`)
are described in `docs/interview.md`.

- `alpaca start` and the skill `/alpaca-from-notes` (`.claude/skills/alpaca-from-notes/SKILL.md`) are the one entry point: they
  take raw notes to a spec, the spec to a runbook, and the runbook to intake.
- `alpaca intake` turns a spec and its runbook into the op's checklist rows, task contracts and
  profile, and keeps them in step when the spec changes.

The spec tools are described in `MANUAL.md` (Spec tools), the runbook in `docs/runbook-format.md`.

## The guided flow

In Claude Code, run `/alpaca-from-notes` with the notes: the skill ships in `.claude/skills/`, so
every clone of the project has it. From a shell, `alpaca start` prints the same steps:

```
alpaca start notes/link-shortener.md
alpaca start notes/link-shortener.md --prepare
```

`alpaca start <notes>` reads the notes (a file, the text itself, or `-` for stdin), picks the kit,
and prints the reason and the steps. It writes nothing. `--prepare` installs the kit from the
vendored copies, moves a spec-kit project to OpenSpec when that is the pick (below), and records
one `start` event with the pick, the reason and the sha256 of the notes. `--kit spec-kit` or
`--kit openspec` overrides the pick; `--json` prints `verdict`, `kit`, `mode`, `reason`,
`interview`, `steps` and `prepared`.

Raw notes come first to the inbox and the interview. While `input/interview/` holds no signed
interview (`interview: needed`), or the log changed after the last sign-off (`interview: stale`),
the steps start with `alpaca note add` and `/alpaca-interview`, which ends with
`alpaca interview signoff`. With a signed interview (`interview: signed`) the steps start at the
spec, and the spec step takes the signed file as its brief. See `docs/interview.md`.

The pick:

| the project | kit | mode |
|---|---|---|
| no spec yet | spec-kit | new: a raw idea to a first spec, with the clarify questions |
| has `specs/*/spec.md` (spec-kit) | OpenSpec | change: the project moves to OpenSpec |
| records OpenSpec, or has `openspec/specs/` | OpenSpec | change (or a new capability as an OpenSpec change) |

The steps it prints after the interview, for a new thing: `/speckit-specify <the signed brief>`, `/speckit-clarify`,
the runbook forge skill (`/alpaca-runbook-forge`), `alpaca runbook check`,
`alpaca op new` when no op is open, then
`alpaca intake <spec> <runbook> --dry-run` and the same without `--dry-run`. For a change:
`/opsx:propose <the signed brief>`, `bin/openspec validate <id> --strict`, the runbook forge skill to update
the runbook, intake of the change folder, and after the work is proven `/opsx:archive <id>`
followed by `alpaca intake openspec/specs <runbook>`, which then has nothing to change.

### Moving a spec-kit project to OpenSpec

A project uses one kit at a time. spec-kit is for day 0, OpenSpec for every change after it. The
first change moves the project: `alpaca start <notes> --prepare` (with the OpenSpec pick) installs
OpenSpec (`alpaca spec init --kit openspec --force`, recorded with `also_present: spec-kit`) and
writes each `specs/<NNN-name>/spec.md` as `openspec/specs/<name>/spec.md`:

```
### Requirement: SC-002

The system SHALL meet success criterion SC-002.

#### Scenario: SC-002

A redirect answers within 50 ms at the 95th percentile under 200 requests per second.
```

One requirement per success criterion (`SC-nnn`), per edge case (`EC-nnn`) and per functional
requirement (`FR-nnn`). An edge case moves the same way, with its text as the scenario body:

```
### Requirement: EC-001

The system SHALL handle edge case EC-001.

#### Scenario: EC-001

A URL longer than 2048 characters is refused with 400.
```

An OpenSpec scenario whose name starts with a spec-kit id keeps that id: `alpaca runbook check`
matches `covers: [SC-002]` or `covers: [EC-001]` to it, an `SC` or `EC` id is a required item and an
`FR` id an optional one, and intake keys its row by the id. An `EC` id is kept only in the shape
the move writes, a requirement named by that id; a scenario that merely starts with one under a
requirement of another name (`#### Scenario: EC-001 port taken` under `### Requirement: Startup`)
stays the scenario `Startup/EC-001 port taken`, as OpenSpec always read it. The scenario body is the criterion
text, so the rows intake made from the spec-kit spec, the edge case rows included, are kept (`=`)
with their verdicts. A runbook of format 1 does not cover edge cases, but after the move an `EC`
scenario is a required item like any other scenario: move such a runbook to `runbook: 2` and cover
its edge cases before the move. An edge case written without an `EC-nnn` id is not an item, so it
does not move; number it first (`EC-UNNUMBERED` in docs/runbook-format.md). The move never writes over an existing file,
records `moved_from` in the `spec:` block of `project.yaml`, and leaves the spec-kit files as
history. It happens once: with `moved_from` recorded, a later `--prepare` says "already moved"
and leaves the moved specs and the block as they are. Two feature folders whose names map to one
capability (`001-links` and `002-links`) are refused (BLOCKED) before anything is installed or
written; rename one of them first. A later OpenSpec change modifies `### Requirement: SC-002` under that name and keeps the
scenario name starting with `SC-002`.

## alpaca intake

```
alpaca intake <spec> <runbook> [--op <op>] [--dry-run] [--json]
```

`<spec>` is one of:

- a spec-kit `spec.md`, or the feature folder that holds it;
- an OpenSpec `spec.md`, or the `openspec/specs` folder (use the folder: the row keys then carry
  the capability, `<capability>/<requirement>/<scenario>`);
- an OpenSpec change folder `openspec/changes/<id>`. Intake reads the living specs next to it
  with the change applied in the order OpenSpec's archive applies it (RENAMED, REMOVED, MODIFIED,
  ADDED), so intake of the change now and of the archived specs later give the same rows. A delta
  that names a requirement the living spec lacks, or adds one it has, is refused. An archived
  change is refused; intake `openspec/specs` instead.

`<runbook>` is the `runbook.yaml` that covers the spec; it must sit inside the project. Relative
paths are read from the folder the command runs in. A runbook a partner sends (written with the
partner kit, `docs/runbook-format.md`) arrives outside the project: copy the delivered folder into
the project first, for example to `partner/<name>/`, keeping its layout, then run intake on the
copy. `--op` names the op; without it intake fills
the open op opened last, and refuses when none is open. The op must be open.

### What it does

1. It checks the runbook against the spec with `alpaca runbook check` (the same code). A runbook
   that is malformed, covers something the spec does not have, or leaves a success criterion or
   scenario uncovered is refused (FAIL) and nothing is written. The errors print as
   `ERROR <code> <where>: <message>`.
2. **Rows.** One checklist row per required item: each spec-kit `SC-nnn`, each edge case `EC-nnn`
   when the runbook is `runbook: 2` (`docs/runbook-format.md`, edge cases), each OpenSpec scenario
   (not under `## REMOVED Requirements`, and not named by an `FR` id). Functional requirements get
   no row. For each item intake writes an intake item file, a one-row acceptance table (item
   format 2):

   ```
   Intake item file, format 2. alpaca intake wrote this file ...

   | key | criterion | shown by | bar | stage | source | kind | supersedes |
   |---|---|---|---|---|---|---|---|
   | SC-002 | A redirect answers within 50 ms at the 95th percentile under 200 requests per second. | load-test/redirect-p95, load-test/redirect-codes | load-test/redirect-p95: json-field out/load.json redirect.p95_ms <= 50 (knob P95_LIMIT_MS, owner only); load-test/redirect-codes: plugin checks/status_codes.py out/load.json 301 exits 0 | 4/5 load-test | spec:SC-002 | check | - |
   ```

   The file sits under `.alpaca/intake/<op>/<runbook id>/items/` and its name ends with the first
   twelve hex digits of its own sha256, so it is never rewritten. `supersedes` is `-` for a first
   row and the id of the row it replaces otherwise. The criterion is plain text: markdown emphasis
   (`**WHEN**`) is dropped and it ends with a period. The row id digests the file bytes, so the
   layout is part of each row's identity; the first line names the layout's format number, and a
   change to the layout is a new format number and a documented change (format 1 to 2 is below).
   The acceptance-table parser (`alpaca/checklist/artifact.py`) reads it, synthesis
   (`alpaca/checklist/synthesis.py`) makes the row, and the bridge (`alpaca/checklist/bridge.py`)
   lands it in the record.

   The columns:

   | column | what it holds |
   |---|---|
   | `shown by` | the parts that cover the item, in covering order: a check as `<stage>/<check>`, a fail case as `the fail case <id> of stage <stage>`, an owner gate as `the owner gate of stage <id>` |
   | `bar` | what each covering part asks, joined with `; ` in covering order (below) |
   | `stage` | the run-order position and id of the first covering stage, `4/5 load-test`; a recovery stage has no position and shows as `recovery <id>` |
   | `source` | the `source` values of the covering parts, each once, joined with `, `; `-` when none has one |
   | `kind` | `check` when a check or a fail case covers the item, `review` when only an owner gate does |

   The bar of each part (from `alpaca.runbook.bar_parts`):

   | part | bar |
   |---|---|
   | `exit-code` check | `<stage>/<check>: exit-code exit == <expect>` |
   | `file-exists` check | `<stage>/<check>: file-exists <path> exists, non-empty` (`exists` alone with `non_empty: false`) |
   | `regex-in-file` check | `<stage>/<check>: regex-in-file <path> matches /<pattern>/` (`does not match` with `absent: true`) |
   | `json-field` check | `<stage>/<check>: json-field <path> <field> <op> <value>` |
   | `plugin` check | `<stage>/<check>: plugin <script> <args> exits 0` |
   | fail case | `fail <stage>/<id>: detect <the detect, written as a check> then <retry, stop, ask-owner or run <stage>>` |
   | owner gate | `owner approves: <approve text>` |

   A `${KNOB}` in a check is written with the knob's default and the knob is named after the
   part, with `owner only` when it is: `redirect.p95_ms <= 50 (knob P95_LIMIT_MS, owner only)`.

   An item shown by at least one check or fail case is a `check` row, discharged by a run; an
   item only an owner gate judges is a `review` row, discharged by the owner's decision. The
   statement of a check row is `<key>: <criterion> Bar: <bar>.`, of a review row
   `<key>: <criterion> Judged by the owner at <shown by>.`; the phase is `verify`, and the proof
   pointer names the item file.
3. **Task contracts.** One task per runbook stage that runs a command, and one per owner gate
   ("Owner approval: <stage title>"), in run order. Each gets a contract (`alpaca task contract`):
   the stage `inputs` and the stages it `needs` are the input, the `outputs` are the expected
   output, one done-bar line per check with its threshold (a `${KNOB}` threshold, in a json-field
   `value` or in a plugin's `args`, shows the knob's value next to it), and the fail cases from `fails`, `stop_on` and the attempt limit. The contract names the
   stage, and its source names the runbook and the stage index.

   Each fail case is one fail case line. A fail case with only `id` and `when` is
   `<id>: <when>`. One with a `detect` and a `then` also says how the failure is recognized and
   what happens then:

   ```
   import-error: the app package does not import (...); detect (regex-in-file): out/junit.xml
     matches /ModuleNotFoundError|ImportError|SyntaxError/; then stop: the stage ends FAIL without
     another attempt
   ```

   `then: retry` reads "then retry: another attempt through the retry rule of the stage", and
   `then: ask-owner` "then ask the owner: the stage pauses for a decision". `then: {run: <id>}`
   names the recovery stage, its command and its checks, since that stage has no task of its own:

   ```
   port-busy: port 8080 is already taken, ...; detect (regex-in-file): out/load.log matches
     /Address already in use/; then run the recovery stage free-port: python3 tools/free_port.py
     8080 --out out/free-port.json, checks free-port/port-free (json-field): out/free-port.json
     field free == True; after it passes, then this stage runs again as one more attempt, and when
     it fails this stage ends FAIL
   ```

   A recovery stage gets no task: it runs only when a fail case sends to it, never on its own, so
   it is shown in the fail case line of every stage that sends to it. Its own fail cases follow
   that line, one line each (`port-busy, in the recovery stage free-port: <its fail case line>`),
   so each part of a row's bar is in some contract. It stays a profile stage
   (step 4), so a contract or a proof can still name it.
4. **Profile.** The runbook's stage ids become the project's profile stages, so a contract can
   name one. When `project.yaml` names no profile, intake writes `intake_profile.py` at the project
   root (a `RunbookProfile` that lists the runbooks intake has read) and sets
   `profile: intake_profile` with a line edit that keeps the file's comments, the comments of a
   `profile:` key it replaces included (a trailing `# ...` and the comment lines under it). It refuses (BLOCKED)
   instead of writing over an `intake_profile.py` it did not write, and instead of rewriting a
   `project.yaml` the line edit cannot handle. `alpaca doctor` then checks each runbook and each plugin check script
   (present and executable). When `project.yaml` names a profile of its own, intake leaves it and
   refuses (BLOCKED) unless that profile already declares every stage of the runbook.
5. One `intake` event records the run: the op, the runbook, the spec, the item format
   (`item_format: 2`), one entry per key (`row`, `step`, and for a live item its `bar`, `stage` and
   `source`), the run order of the stages (`run_order`: each stage's position and the stages it
   `needs`), the task of each stage, and what changed. The latest event for (op, runbook id) is
   what the next run compares against.

`--dry-run` does steps 1 to 4 on paper: it prints every row, task and profile change it would
make and writes nothing, not even the item files.

### A spec change

Run intake again with the changed spec (or the change folder) and the runbook. It compares each
item with the row the previous intake recorded for its key:

| item | what intake does | mark |
|---|---|---|
| same text, covering parts and bar as its current row | nothing: the row, its id and its verdicts stay | `=` kept |
| only its `stage` or `source` changed (a stage added or moved before it, a new `source`) | the row, its id and its verdicts stay; the intake event records the new stage and sources | `=` kept |
| text, covering parts or bar changed | a new row that cites the current one (`supersedes`), frozen and landed through `alpaca/checklist/supersession.py` and the bridge; the old row stays in the record, pointed forward by `superseded_by`, and drops off the board | `~` superseded |
| gone from the spec | a withdrawal row that cites the old one, plus a waiver ("the spec no longer has this item") at the level in force, so it shows as done | `-` withdrawn |
| new in the spec | a new row | `+` added |

A bar change is any change to what a covering part asks: a threshold (`value`, `expect`), a knob
default the bar uses, an `op`, a `path`, a `field`, a `pattern` or `absent`, a check `type`, a
fail case's `detect` or `then`, an owner gate's `approve` text. Each one supersedes exactly the
rows whose bar names that part. A knob that no check names (one the `run` command uses, say)
changes no bar. A knob's `owner_only` says who may change it, not what a check judges: the bar
shows it (`owner only`), but flipping it alone keeps the row.

"Current row" is the head of the key's supersession chain. An item that goes back to text it had
before (a reverted change, or a removed item restored with the same text) is compared with the
current row, not with the old one, so it is a change: a new row that cites the current row. Its
item file names that row in `supersedes`, so it never derives the frozen old row's id again.

Verdicts bind a row id and its content hash, so a kept row keeps its verdicts and a superseded
row's verdicts stay with the old row: the new row starts open and needs its own proof. That holds
for a revert too: the verdicts of the first row stay with it, and the new row needs its own proof. A task whose
stage changed gets a new contract (the newest is current); a new stage gets a new task; a stage
no longer in the runbook is listed as `!` and its task is left for a person to close or block.
A rerun with nothing changed writes nothing at all (`nothing to change`).

The key of an item is its spec-kit id (`SC-002`, also when an OpenSpec scenario carries one), or
else the OpenSpec scenario id. A renamed scenario or requirement is a new key: its old row is
withdrawn and the new one added.

The runbook `id:` keys the item folder, the rows' baseline and the tasks. Keep it once the runbook
has been taken in: when the latest intake of the same runbook file into the op used another id,
intake refuses (BLOCKED) and names the old id, since a new id would add a second set of rows and
tasks next to the first. Put the id back. A new id in a new runbook file (or in the same runbook
moved to a new path) is taken in as a second runbook: its rows and tasks are added, and the rows
and tasks of the old id stay open until you close or withdraw them by hand. Intake names each spec
item that another runbook id of the op already holds with a live row, with the warning
`KEY-HELD-BY-ANOTHER-RUNBOOK <key>` (in the dry run too), so the two sets never pass unnoticed.

OpenSpec changes go in one at a time. Intake applies one change folder to the living specs, so a
second change proposed while the first is still under `openspec/changes/` does not see the first
one's requirements, and the runbook check refuses it (`COVERS-UNKNOWN`). Archive the first change
and take `openspec/specs` in, then take the next change in. Do not drop the first change's
coverage from the runbook to get past the check: intake would then withdraw and waive its rows.

### Moving from item format 1

Intake before item format 2 wrote item files without the `bar`, `stage` and `source` columns, the
statement `<key>: <criterion> Shown by <checks>.`, and an intake event whose entries hold only
`row` and `step`. Intake never rewrites such a row. At the first intake with item format 2, for
each key whose previous entry has no `bar`:

- when the criterion and the covering parts (`shown by`) are unchanged, intake keeps the row
  (`=`), with its id, its format 1 item file and its verdicts, records the current bar in the new
  intake event as the baseline, and warns `BAR-BASELINE <key>`;
- when they changed, the row is superseded as usual, and the new row has item format 2.

From then on the recorded bar is what the next intake compares against: a bar change supersedes
the row, the new row cites the format 1 row, and its item file has format 2. The baseline is the
bar at that first intake, so a threshold changed in the same step as the move is taken as the
baseline, not as a change. Run intake once with the runbook the rows were made from, then change
the runbook. The warning comes once per key: the next run has a bar to compare and nothing to
record.

A withdrawal is waived at the level in force for the session. When that level cannot be read (a
`default_level` in `project.yaml` that is not a level), intake refuses (BLOCKED) before it writes
anything.

### Refusals

| exit | when |
|---|---|
| 1 FAIL | the runbook fails `alpaca runbook check` against the spec; two items share a key; a change folder does not apply to the living specs; the spec cannot be parsed |
| 2 BLOCKED | no open op (or the named op is closed); the runbook is outside the project; the project's own profile lacks runbook stages; a row to supersede does not match the intake item it names, or supersession refuses the chain; the runbook id changed since the last intake of that file; the level in force cannot be read for a waiver; `project.yaml` cannot take `profile:` by a line edit (a flow mapping, say), since intake never rewrites the whole file; an `intake_profile.py` that intake did not write is in the way |
| 64 | a spec or runbook argument is missing |

### Output

The text form lists the rows in run order (the stage position, then the key; withdrawn rows last),
each with its mark (`+`, `=`, `~`, `-`), its row ids and its stage, and the bar on the line under
it; then the tasks, the profile with its stages and plugin checks, and it ends with
`GATE alpaca-intake: PASS`. `--json` prints `verdict`, `op`, `spec`, `spec_shape`, `format`,
`runbook`, `runbook_id`, `stages`, `required`, `rows` (`added`, `kept`, `superseded`,
`withdrawn`, each list in run order; a row of a live item carries `key`, `row`, `step`,
`shown_by`, `bar`, `stage` and `source`), `tasks` (`added`, `contracts`, `kept`, `orphaned`),
`profile`, `warnings`, `dry_run` and `changed`.

To see what intake made: `alpaca board show --op <op>` (the rows as cards), `alpaca task list --op
<op>`, and `alpaca task contract <task> --show`.

### The board

`alpaca board show --op <op>` lists the intake rows of the op first, in run order (the runbooks in
the order they were first taken in, then the stage position, then the key), then the op's other
rows. An intake row shows its key and stage after the tag, and an open row (todo or doing) shows
`waiting on <stage>` when a stage that its first covering stage `needs` still has a row that is not
done:

```
  [todo   ] check.sc-001-d780d8479c6f Specced SC-001 [2/5 unit-test]
  [todo   ] check.sc-003-ffe9f9bf4771 Specced SC-003 [3/5 restart-test] waiting on unit-test
```

Only the stages named in `needs` count, in the order `needs` lists them, and only rows of the same
op and runbook. `--json` carries the same on each card as `intake` (`key`, `runbook`, `stage`,
`waiting_on`; null for a row intake did not make). Both come from the latest intake event, so a
project intaken before item format 2 shows no stage and no marker until its next intake.

## Where the pieces come from

Intake adds no parallel checklist engine. It carries the acceptance-table parser, synthesis, the
bridge, supersession and verdict rows of `alpaca/checklist/`, `ops.add_task` and
`alpaca/taskcontract.py` for tasks, the profile seam of `alpaca/profile.py`, and the runbook check
of `alpaca/runbook.py`. The one addition to synthesis is `{cell:<column>}` in an obligation
template, which puts a cell of the acceptance table into the row statement.
