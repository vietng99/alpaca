# Runbook format

A runbook is the executable plan of a domain. It lists the stages, the command each stage runs,
what each stage reads and writes, the checks that decide whether a stage passed, the knobs a
retry may move, the retry rules, and the steps only a person may approve (owner gates).

It sits in the middle of the path Alpaca follows from an idea to checked work:

```
raw notes -> spec (spec-kit or OpenSpec) -> runbook -> intake (rows, task contracts, profile) -> runs with sealed proof
```

The spec says what must hold. The runbook says how each part of that is shown, and it links
every success criterion, edge case or scenario of the spec to at least one check. Intake reads the runbook
and turns it into checklist rows and task contracts.

`alpaca runbook check <file> [--spec <path>]` reads a runbook, refuses a malformed one with a
message that names the field, and, with a spec, fails when any success criterion, edge case
(format 2) or scenario has no check. The skill `/alpaca-runbook-forge` (`.claude/skills/alpaca-runbook-forge/SKILL.md`) writes a runbook from a spec and asks
the person only for what the spec leaves out.

A worked example lives in `templates/runbook-example/`: a spec-kit spec for a small link
shortener service with numbered edge cases (`spec.md`), its format 2 runbook (`runbook.yaml`),
with a fail case that recognizes a busy port and sends to a recovery stage, and one plugin
check (`checks/status_codes.py`). `alpaca runbook check templates/runbook-example/runbook.yaml` passes
on it.

## Why one YAML file

A runbook is one `runbook.yaml` file.

- A person reads it top to bottom as the run order: stages are listed in the order they run, and
  comments (`#`) carry the reasons a table cannot.
- A machine checks it without a Markdown parser: one YAML mapping, one schema, and every error
  points at a dotted location such as `stages[3].checks[1].value`.
- Alpaca already reads YAML (`project.yaml`) and ships PyYAML, so the format adds no dependency.
- Markdown with one fenced YAML block per stage was the other option. It reads well, but the
  prose between blocks is not checked and the stage order depends on how the file is cut; a
  field that drifts out of a fence silently stops counting. One file with `description` fields
  keeps the prose next to the thing it explains.

Write the file in UTF-8. Keep a bare `on`, `off`, `yes` or `no` out of key position: YAML reads
such a key as true or false (the check reports it and names the field you meant).

## Format 1 and format 2

`runbook: 2` is the current format. It adds three things to format 1:

- numbered edge cases: the `EC-nnn` items of a spec-kit spec are items the runbook must cover
  (see "Edge cases");
- known failures with a detection and a response: `detect` and `then` on a fail case, and
  recovery stages (`recovery: true`) that a fail case sends to (see "Known failures");
- provenance: a `source` on a knob, a check, a fail case and an owner gate (see "Provenance").

A `runbook: 1` file is still read, and checked as it always was: the fields above are
`FIELD-UNKNOWN` in it, an `EC-nnn` id is not an item, and a spec-kit spec whose Edge Cases
section lists bullets gives one warning, `EC-IGNORED` ("format 1 does not cover edge cases;
move to runbook: 2"). To move a runbook to format 2, write `runbook: 2`, number the edge cases
in the spec (`- **EC-001**: ...`) and cover each one. `runbook: 3` is the research format, for a
runbook that also holds a `research` block (see "The research block"). Any other `runbook:` value
is refused (`VERSION-UNSUPPORTED`).

A `runbook: 2` file may also be a ship runbook: `kind`, `rollback` and, on a stage, `external` and
`watch` (see "Ship runbooks"). These fields are `FIELD-UNKNOWN` in a `runbook: 1` file and in a
`runbook: 3` file.

## Top level

| field | required | meaning |
|---|---|---|
| `runbook` | yes | the format version; write `runbook: 2` (a `runbook: 1` file is still read, see "Format 1 and format 2"), or `runbook: 3` for a research runbook (see "The research block") |
| `id` | yes | a short name: lower-case letters, digits, `.`, `_`, `-` |
| `title` | yes | one line a person reads |
| `description` | no | what this runbook runs and why |
| `spec` | no; yes with `runbook: 3` | the spec this runbook covers, a path relative to the runbook folder; `alpaca runbook check` reads it when `--spec` is not given |
| `owner` | no | who approves the owner gates (a role, not a secret) |
| `knobs` | no | the named settings a stage reads and a retry may move |
| `kind` | no | `plain` (the default when the field is absent) or `ship` for a release or a deploy; `runbook: 2` only; a `ship` runbook is held to the rules in "Ship runbooks" |
| `rollback` | yes with `kind: ship` | who calls a rollback and what forces one: `automatic`, `discretionary`, `authority` and `deputy`; `runbook: 2` only; see "Ship runbooks" |
| `stages` | yes | the stages, in run order; at least one |
| `research` | with `runbook: 3` | the research block of a research runbook (format 3), which also names its `spec`; a `runbook: 1` or `runbook: 2` file may not hold it |

Every path in a runbook (`spec`, `workdir`, check `path`, plugin `script`) is relative, and may
not climb out of the runbook folder with `..`. The check reads a path as written, before any
`${NAME}` in it is replaced. When a check runs, its `path` is checked again after the
replacement: it must still lead inside the runbook folder, or, when it starts with
`${EVIDENCE_DIR}`, inside the evidence folder. A path that leads anywhere else makes the check
BLOCKED, whatever a knob or variable held.

## Knobs

A knob is a named setting with an allowed range. A stage reads it as `${NAME}`; a retry rule may
move it; a check may use it as its threshold.

| field | required | meaning |
|---|---|---|
| `id` | yes | the name: letters, digits and `_`, not starting with a digit (for example `WORKERS`) |
| `description` | yes | what it changes |
| `type` | yes | `int`, `float`, `str`, `bool` or `enum` |
| `default` | yes | the value of the first attempt; must fit the type and the range |
| `min` | no | lowest allowed value (`int` and `float` only) |
| `max` | no | highest allowed value (`int` and `float` only) |
| `values` | for `enum` | the allowed values |
| `owner_only` | no | `true` when only the owner may change it; a retry may not move it |
| `source` | no | where the value comes from (format 2, see "Provenance") |

A threshold that comes from the spec (a latency bar, a clock target) is a good `owner_only` knob:
the runbook states it once, a check compares against it, and no retry can loosen it.

## Stages

| field | required | meaning |
|---|---|---|
| `id` | yes | a short name, unique in the runbook; intake uses it as the profile stage name |
| `title` | no | one line a person reads |
| `description` | no | what the stage does |
| `needs` | no | stage ids that must pass first; each must be declared earlier in the file |
| `run` | yes, unless the stage is only an `owner_gate` | the command, run by `sh -c` in `workdir` |
| `workdir` | no | the folder the command and the relative check paths start from (default: the runbook folder) |
| `timeout` | no | seconds before the command is stopped |
| `env` | no | extra environment variables, a mapping of names to text or numbers |
| `inputs` | no | the files or folders the stage reads (paths or globs) |
| `outputs` | no | what the stage writes: a path, or a mapping with `path` and `what` |
| `checks` | yes when `run` is set; not allowed without `run` | the pass checks; a stage that runs a command needs at least one, and a stage without `run` has none |
| `retry` | no | the retry rule (below); without it a stage runs once |
| `fails` | no | known ways the stage fails: mappings with `id` and `when`, and in format 2 `detect`, `then`, `covers` and `source` (see "Known failures") |
| `owner_gate` | no | a step only a person may approve (below) |
| `recovery` | no | `true` for a recovery stage (format 2): it runs only when a fail case sends to it (see "Known failures") |
| `external` | no | `true` when the stage changes something outside the project folder (`runbook: 2` only; read by the ship rules, see "Ship runbooks") |
| `watch` | no | `{after, looks_at}`: the stage judges the change again after a wait (`runbook: 2` only; see "Ship runbooks") |

A stage passes when every one of its checks passes. `needs` names only earlier stages, so the
order of the file is a valid run order and a cycle cannot be written.

An output mapping takes `path` (required) and `what` (a short description). A known failure
takes `id` and `when`, for example `{id: port-busy, when: "port 8080 is already taken"}`;
intake copies these into the task contract's fail cases. In format 2 it can also say how the
failure is recognized and what to do then (see "Known failures").

### Variables

`run`, `workdir`, `env` values, check `path`, plugin `args` and a `json-field` `value` may use
`${NAME}`. `NAME` is a knob id or one of the built-in variables:

| variable | value |
|---|---|
| `${RUNBOOK_DIR}` | the absolute path of the runbook folder |
| `${STAGE}` | the id of the stage being run |
| `${ATTEMPT}` | the attempt number, 1 for the first run |
| `${EVIDENCE_DIR}` | the folder where this attempt keeps its evidence files |

Any other name is refused (`VAR-UNKNOWN`).

## Checks

Every check has these fields:

| field | required | meaning |
|---|---|---|
| `id` | yes | a short name, unique in the whole runbook (retry rules and coverage refer to it) |
| `type` | yes | one of the check types below |
| `description` | no | what it shows |
| `covers` | no | the spec items this check shows (see "Linking checks to the spec") |
| `source` | no | where the check and its threshold come from (format 2, see "Provenance") |

A check answers with one of the four verdicts of the verdict contract (`alpaca/gates/verdict.py`):
PASS, FAIL, BLOCKED (there was nothing to judge, for example the stage never ran) or
PAUSED-FOR-DECISION.

### `exit-code`

The stage command's exit status.

| field | required | meaning |
|---|---|---|
| `expect` | no | the status that passes, 0 to 255 (default 0) |

PASS when the status equals `expect`, FAIL otherwise, BLOCKED when the command did not run.

### `file-exists`

| field | required | meaning |
|---|---|---|
| `path` | yes | the file, relative to the stage `workdir` |
| `non_empty` | no | `true` (default) also fails an empty file |

### `regex-in-file`

| field | required | meaning |
|---|---|---|
| `path` | yes | the file to search |
| `pattern` | yes | a Python regular expression, searched once over the whole file; `^` and `$` match at each line |
| `absent` | no | `true` passes when the pattern does NOT match (default `false`) |
| `ignore_case` | no | `true` ignores case |

Because the whole file is searched at once, `\s` and a class such as `[^x]` also match a line
break, so a pattern can span lines (`total\s+12` matches `total` at the end of one line and `12`
at the start of the next). Write `[ \t]` or `[^x\n]` to stay on one line.

A file that cannot be read fails the check, whether or not `absent` is set: absence cannot be
shown from a missing file.

### `json-field`

Compares one field of a JSON file with a threshold.

| field | required | meaning |
|---|---|---|
| `path` | yes | the JSON file |
| `field` | yes | a dotted path into the document; a whole-number part indexes a list (`runs.0.lost`) |
| `op` | yes | one of `==`, `!=`, `<`, `<=`, `>`, `>=` |
| `value` | yes | the threshold: a number, text, true/false, or `${KNOB}` |

`<`, `<=`, `>` and `>=` compare numbers only: a missing field, a value that is not a finite
number (text, `NaN`, `Infinity`, an object) fails the check. Quote the op in YAML (`op: "<="`).

`==` and `!=` compare numbers as numbers (`1` equals `1.0`), and true/false only with
true/false: `true` does not equal `1` and `false` does not equal `0`.

A `value` that is exactly `${KNOB}` takes the knob's value with its type (a number stays a
number). A `${NAME}` inside other text (`"v${RELEASE}"`) is replaced as text. Either way the name
must be a knob or a built-in variable (`VAR-UNKNOWN`).

### `plugin`

Runs a script the domain supplies. This is how a domain adds a check the generic types do not
cover.

| field | required | meaning |
|---|---|---|
| `script` | yes | the script, relative to the runbook folder; it must exist and be executable; a fixed path (no `${NAME}`) |
| `args` | no | arguments, text or numbers; `${NAME}` is replaced |
| `timeout` | no | seconds (default 60) |

#### The plugin check contract

The contract is the one `contracts/README.md` sets for acceptance scripts:

- The script is executed directly (`chmod +x`, with a `#!` line), in the stage `workdir`, with
  the `args` after its path. Any language works.
- It reads these environment variables: `ALPACA_RUNBOOK_DIR` (the runbook folder),
  `ALPACA_STAGE`, `ALPACA_CHECK` (the check id), `ALPACA_ATTEMPT`, `ALPACA_EVIDENCE_DIR`, and
  `ALPACA_KNOB_<NAME>` for every knob value of this attempt.
- Its exit status is the verdict: 0 PASS, 1 FAIL, 2 BLOCKED, 3 PAUSED-FOR-DECISION. Any other
  status, a crash, or running past the timeout reads as BLOCKED: a check that did not report a
  verdict never counts as a pass.
- The last non-empty line it prints to stdout is the reason shown with the verdict. Keep it
  short and factual ("3 of 6000 responses were not 301: 502 x3").
- It may write evidence files under `ALPACA_EVIDENCE_DIR`. It must not change the files other
  checks read.

`templates/runbook-example/checks/status_codes.py` is a complete plugin check.

## Linking checks to the spec

Each check (and each owner gate) lists in `covers` the spec items it shows. With a spec,
`alpaca runbook check` reads the spec, matches every `covers` entry to one item, and fails when a
required item is covered by nothing.

**spec-kit.** The items are the ids of the `spec.md` bullets `- **SC-001**: ...` (success
criteria, required) and `- **FR-001**: ...` (functional requirements, not required). Write the
id: `covers: [SC-001]`. A functional requirement without a check is a warning
(`FR-UNCOVERED`), not a failure. In format 2 the edge cases (`- **EC-001**: ...`) are required
items too (see "Edge cases"). `[NEEDS CLARIFICATION: ...]` markers left in the spec are
reported as a warning (`SPEC-CLARIFY`).

The template's shape is `- **SC-001**: ...`. These shapes are read the same way: the colon inside
the bold (`- **SC-001:** ...`), a `*` or `+` bullet, a numbered list (`1. **SC-001**: ...`,
`1) SC-001: ...`), no bullet (`**SC-001**: ...`), no bold (`- SC-001: ...`, `SC-001: ...`), a
heading (`### SC-001: ...`), and a table row whose first cell is the id (`| SC-001 | ... |`).
An `SC-nnn` or `FR-nnn` id that appears anywhere else in the spec, outside those shapes, still
counts: an `SC` id is a required item and an `FR` id a functional requirement, and the check
warns (`SPEC-UNPARSED`, with the line number) so the line can be rewritten in the template's
shape. A criterion therefore never drops out of coverage because of how it was written.

**OpenSpec.** The items are the scenarios: every `#### ` heading under a `### Requirement:`
block, named without its `Scenario:` prefix. Write `<requirement>/<scenario>`:

```yaml
covers: ["Session Timeout/Idle timeout"]
```

Matching ignores case and extra spaces. In a change's delta spec, scenarios under
`## ADDED Requirements` and `## MODIFIED Requirements` are required, and scenarios under
`## REMOVED Requirements` are not. A change folder (`openspec/changes/<id>`, holding
`proposal.md` and a `specs` folder) is read as the living specs next to it with the change
applied, the way `alpaca intake` reads it (`docs/intake.md`): the runbook must cover the whole spec
after the change, and a delta that does not apply is `SPEC-DELTA`.

A scenario whose name starts with a spec-kit id (`#### Scenario: SC-002`,
`#### Scenario: SC-002 redirect latency` or `#### Scenario: EC-001`) keeps that id:
`covers: [SC-002]` matches it, an `SC` or `EC` id is a required item and an `FR` id an optional
one. This is how a project that moved from spec-kit to OpenSpec keeps its runbook (see `docs/intake.md`). Pass a folder (`openspec/specs`, or a change's `specs`
folder) to read every `<capability>/spec.md` in it; the items are then
`<capability>/<requirement>/<scenario>`, and the short form `<requirement>/<scenario>` is
accepted when only one capability has it (`COVERS-AMBIGUOUS` otherwise).

Headings and ids inside fenced code blocks and HTML comments are not items, in either format.

## Edge cases

In format 2 the edge cases of a spec-kit spec are items, like the success criteria. Number them
`EC-001`, `EC-002`, ... where the spec lists them, under its `### Edge Cases` (or
`## Edge Cases`) heading:

```markdown
### Edge Cases

- **EC-001**: A URL longer than 2048 characters is refused with 400.
- **EC-002**: The same long URL posted twice gets two different codes.
```

An `EC-nnn` id is read in every shape an `SC-nnn` id is, and one written anywhere else in the
spec still counts, with the `SPEC-UNPARSED` warning. Every edge case is required: one that
nothing covers is `SPEC-UNCOVERED`, like a success criterion. The check that shows it covers it
(`covers: [EC-001]`); so does the fail case that detects and answers it (an edge case such as
"the port is already taken" is covered by the fail case that recognizes a busy port, see "Known
failures"), and so does an owner gate.

A bullet under the Edge Cases heading that carries no `EC-nnn` id is an error, `EC-UNNUMBERED`,
with its line number and its text. The reader never numbers edge cases by their position: an id
must stay with its edge case when the list changes, because intake keys a row by it. Only the
top-level bullets count; an indented bullet is a detail of the one above it. The section ends at
the next heading of the same or a higher level.

In format 1 edge cases are not items. An Edge Cases section with bullets gives the warning
`EC-IGNORED`, and a `covers` entry that names an `EC-nnn` id is `COVERS-UNKNOWN`.

OpenSpec is the same in both formats: an edge case there is a scenario, and every scenario is
already required. A spec-kit edge case moved to OpenSpec becomes `### Requirement: EC-001` with
`#### Scenario: EC-001` (its text is the scenario body) and keeps its id, so `covers: [EC-001]`
still finds it. Only that shape keeps an `EC-nnn` id: a scenario whose name merely starts with
one under a requirement of another name (`#### Scenario: EC-001 port taken` under
`### Requirement: Startup`) stays the scenario `Startup/EC-001 port taken`.

**Owner gates cover too.** Some criteria can only be judged by a person (for example "a new
team member can start the service in under 10 minutes"). Put those in the `covers` list of the
owner gate that judges them. Coverage then shows `gate:<stage id>` as the one covering it.

**Fail cases cover too (format 2).** A fail case that detects a failure and answers it shows
how the runbook handles that situation. Put the items it shows in its `covers` list; coverage
then shows `fail:<stage>/<fail id>` as the one covering it. Only a fail case with a `detect`
covers: one without is never recognized, so a `covers` list there is an error
(`COVERS-NO-DETECT`) and counts for nothing.

A `covers` entry that names nothing in the spec is an error (`COVERS-UNKNOWN`, with the closest
item named). A spec with no success criterion and no scenario is refused (`SPEC-EMPTY`): an
empty list is never a pass.

## Retry rules

| field | required | meaning |
|---|---|---|
| `max_attempts` | yes | total runs, the first one included, 1 to 20 |
| `on_fail` | no | the check ids whose FAIL allows another attempt (default: every check of the stage) |
| `stop_on` | no | the check ids whose FAIL ends the stage at once, with no retry |
| `move` | no | the knob to change before the next attempt: `knob` (a number knob that is not `owner_only`) and `by` (the step; negative lowers it) |

After each attempt the stage stops when:

1. every check passed;
2. a check is BLOCKED or PAUSED-FOR-DECISION (a rerun does not supply a missing input or a decision);
3. a check listed in `stop_on` failed;
4. a check not listed in `on_fail` failed;
5. `max_attempts` runs are used; or
6. the `move` would take the knob outside its `min`..`max`.

Otherwise the knob moves by `by` and the stage runs again. Without `move` the next attempt runs
with unchanged inputs, which only makes sense for a stage that fails for reasons outside the
runbook (a network hiccup). `alpaca.runbook.next_attempt` implements this rule.

In format 2 a fail case that recognizes the failure decides before this rule (see "Known
failures").

Example: raise the worker count by 2 while the latency check fails, at most three runs, and stop
at once if any response had the wrong status:

```yaml
retry:
  max_attempts: 3
  on_fail: [redirect-p95]
  stop_on: [redirect-codes]
  move: {knob: WORKERS, by: 2}
```

## Known failures

A stage lists the ways it is known to fail in `fails`. In format 1 a fail case has `id` and
`when` only. In format 2 a fail case can also say how the failure is recognized and what to do
then:

| field | required | meaning |
|---|---|---|
| `id` | yes | a short name, unique in the stage |
| `when` | yes | the failure, in words |
| `detect` | no | a check of one of the types above (`exit-code`, `file-exists`, `regex-in-file`, `json-field`, `plugin`), without `id` and without `covers`; the failure is recognized when this check would PASS |
| `then` | yes when `detect` is set | `retry`, `stop`, `ask-owner`, or `{run: <stage id>}` |
| `covers` | no | the spec items this failure handling shows (see "Edge cases") |
| `source` | no | where the fail case comes from (see "Provenance") |

What each answer does:

- `retry`: another attempt, through the retry rule of the stage: its `stop_on`, its
  `max_attempts` and the range of its `move` knob still hold, and a BLOCKED or
  PAUSED-FOR-DECISION check still stops the stage. `on_fail` need not list the failed check: the
  fail case names this failure as one a new attempt may fix. A stage with no `retry` block has
  one attempt, so there `retry` ends the stage with FAIL.
- `stop`: the stage ends with FAIL at once.
- `ask-owner`: the stage ends with PAUSED-FOR-DECISION; a person decides what happens next.
- `{run: <stage id>}`: the recovery stage named there runs. After it passes, the failed stage
  runs again, and that run counts as an attempt of the failed stage, so it needs an attempt left
  (`retry.max_attempts` of 2 or more; with one attempt the recovery stage could never run, and the
  check refuses it with `RECOVERY-NO-ATTEMPT`). A recovery stage that fails ends the failed stage
  with FAIL. A check listed in `stop_on` that failed still stops the stage: it never runs again,
  recovered or not.

After an attempt that did not pass, the first fail case in file order whose `detect` passed
decides. When no fail case is recognized, the retry rule decides, as above. When every check
passed, the stage passed, whatever a `detect` would say. A fail case with `then` and no
`detect` is never recognized; give it the `detect` that shows the failure.
`alpaca.runbook.respond` implements it.

A `detect` is a check, so the check rules apply to it, with the paths relative to the stage
`workdir`. A `detect` that breaks one is `DETECT-INVALID`, and the message names the rule it
broke: a missing `pattern`, a field a detect does not have (`id`, `covers`, `source`), a plugin
script that does not exist.

### Recovery stages

A recovery stage is a stage with `recovery: true`. It follows the stage rules, and these:

- it has a `run` and at least one check (`RUN-MISSING`, `CHECKS-EMPTY`), and it is never an owner
  gate (an `owner_gate` there is `FIELD-UNKNOWN`);
- it is left out of the run order: it runs only when a fail case sends to it, never on its own,
  and intake gives it no place in the run order;
- no stage lists it in `needs`, and it has no `needs` itself (`RECOVERY-NEEDED`);
- `then: {run: <id>}` names a stage of the runbook (`RECOVERY-UNKNOWN`) that has
  `recovery: true` (`RECOVERY-NOT-MARKED`), and a fail case of a recovery stage may not send to
  that same stage (`RECOVERY-SELF`) or to another recovery stage (`RECOVERY-CHAIN`): a recovery
  stage that fails ends the failed stage with FAIL, so its own fail cases answer `retry`, `stop`
  or `ask-owner`;
- the stage whose fail case sends to it has `retry.max_attempts` of 2 or more
  (`RECOVERY-NO-ATTEMPT`), since the rerun after the recovery is one of its attempts.

It may be declared anywhere in the file; a comment next to it helps the person who reads the
run order. From the worked example:

```yaml
fails:
  - id: port-busy
    when: port 8080 is already taken, so the service under test never starts
    detect: {type: regex-in-file, path: out/load.log, pattern: 'Address already in use'}
    then: {run: free-port}
    covers: [EC-003]
```

## Provenance

In format 2 a knob, a check, a fail case and an owner gate may say where they come from, in
`source` (text). The shapes:

| source | means |
|---|---|
| `spec:<item id>` | a spec item states it, for example `spec:SC-002` |
| `note:input/notes/<file>` | a raw note in the project's inbox states it |
| `interview:<slot id>` | an answer of the interview settled it, for example `interview:thresholds` |
| `owner:<decision ref>` | the owner decided it; the reference names the decision |
| `default` | a default: nobody stated it |

Any other text is a warning, `SOURCE-SHAPE`; it does not change the verdict. When the check reads
a spec (`--spec`, or the `spec:` field) without `--no-files`, a `note:` path must exist. It is
looked for under the runbook folder and each folder above it, up to the project root, the first
folder that holds `project.yaml` (`SOURCE-MISSING`, a warning). A `source` says where a value
came from; it is not part of what a check asks.

## Owner gates

An owner gate is a step only a person may approve. Nothing in Alpaca moves it on its own, at any
autodrive level.

| field | required | meaning |
|---|---|---|
| `approve` | yes | what the owner looks at and approves, in one or two sentences |
| `evidence` | no | the files the owner reads before approving |
| `covers` | no | the spec items this approval judges |
| `source` | no | where the gate comes from (format 2, see "Provenance") |

A stage with an `owner_gate` waits for the approval before its `run` starts, and its checks judge
that `run`. A stage may be only a gate (no `run`, no `checks`), for example a release sign-off.
Such a stage runs nothing, so a check there would never be judged: the check refuses it
(`CHECKS-WITHOUT-RUN`). A number the owner should see before approving goes in `evidence`; a
threshold a machine should judge goes in a check of the stage whose `run` writes the number.

## Ship runbooks

A ship runbook is a release or a deploy: it changes something outside the project folder, and a
person must be able to undo that change and see whether it held. Write `kind: ship` at the top of
a `runbook: 2` file and the checker holds it to the rules below. `kind` is `plain` or `ship`,
exactly those two words in lower case; with no `kind` the runbook is plain and checks as it always
did. No plain runbook needs any of the fields in this section.

The rules, each reported as its own finding so one run names every gap:

- a `rollback` block is present (`SHIP-NO-ROLLBACK`);
- at least one stage that has a `run` says `external: true` (`SHIP-NO-EXTERNAL`);
- every `external: true` stage has a recovery entry (`SHIP-NO-RECOVERY`);
- one stage has `watch: {after: 1h, ...}` and one has `watch: {after: 24h, ...}`
  (`SHIP-NO-WATCH`, which names the missing window);
- a watch follows the change: the stage reaches an `external: true` stage through `needs`,
  directly or through other stages (`WATCH-BEFORE-CHANGE`).

### Which stages change something outside the project folder

The rule is the `external: true` flag on the stage. The author states it: the stage deploys,
publishes, pushes to a shared remote, or writes a service or a database the project does not own.
The checker does not guess from the command, which it cannot decide, and an `owner_gate` is not the
mark: a gate runs nothing and changes nothing, the stage after it does. Marking every stage that
has a `run` would ask a recovery entry of build and test stages too. `external: true` needs a
`run` (a stage that is only an owner gate may not say it). On a recovery stage the flag is allowed
and means nothing more, since a recovery stage has no recovery of its own.

### The recovery entry

An external stage has a recovery entry when one of its fail cases has a `detect` and answers
`then: {run: <recovery stage id>}`, and the target is a stage marked `recovery: true` (see "Known
failures"). A fail case without `detect` is never recognized by the runner, so it does not count,
and neither do `then: retry`, `then: stop` and `then: ask-owner`. The existing rule applies: the
stage needs `retry.max_attempts` of 2 or more, because the recovery stage runs and then the stage
runs once more. That is the cost of an entry: the answer is "undo, then run again". There is no
"undo, then stop" answer.

### `rollback`

| field | required | meaning |
|---|---|---|
| `automatic` | yes | the ids of the checks whose failure forces a rollback; at least one, and each is a check of a stage in the run order (not of a recovery stage) |
| `discretionary` | yes | the conditions the authority judges, as text; `[]` says there are none |
| `authority` | yes | who calls the rollback, a role as in `owner` |
| `deputy` | yes | who calls it when the authority cannot; not the same person as `authority` (`ROLLBACK-DEPUTY-SAME`; the comparison ignores case and edge spaces) |

The shape of the block is checked whenever it is present, in a plain runbook too. A check in
`automatic` forces an undo only through a fail case of its own stage that sends to a recovery
stage; the checker does not tie the two together.

### `watch`

| field | required | meaning |
|---|---|---|
| `after` | yes | the wait after the change, as text: a whole number and a unit, `m`, `h` or `d` (`1h`, `24h`, `90m`) |
| `looks_at` | yes | the ids of checks of this stage that the watch judges; at least one |

`after` is read exactly. YAML reads an unquoted `1:00` as the number 60, so a number is refused
(`FIELD-TYPE`); `1H`, `01h`, `0h`, `1.5h` and `1 h` are refused too (`WATCH-AFTER-INVALID`).
`60m` counts as `1h` and `1d` counts as `24h`, to the second; `2h` counts as neither. A stage holds one
`watch`, so each window is its own stage. A `watch` needs a stage with a `run` that is not a
recovery stage (`FIELD-UNKNOWN` otherwise). The list is `looks_at` and not `checks`, which on a
stage already means the check mappings. A name in `looks_at` that is not a check of the same stage
is `WATCH-CHECK-UNKNOWN`.

A watch counts toward the 1h or 24h rule only when it reaches the change through `needs`. Writing
the watch stage before the deploy, or leaving the deploy out of its `needs`, is
`WATCH-BEFORE-CHANGE`, and that watch then does not count.

### Plain runbooks that carry ship parts

A `runbook: 2` file with no `kind` that has a `rollback` block, a `watch` or an `external: true`
stage passes, with one warning, `SHIP-PARTS-UNCHECKED`: the ship rules did not run. Add
`kind: ship` if it ships. An explicit `kind: plain` says the author decided, and gives no warning.

### What the checker does not do

It checks shape and presence only. Nothing waits for `after`, and nothing reads `rollback` while a
run goes on: no step calls a rollback. The block records who decides and what triggers it, so the
decision is written down before the release, not after.

### Example

```yaml
runbook: 2
id: ship-demo
title: Ship demo
kind: ship
owner: release owner
rollback:
  automatic: [health-1h, errors-24h]
  discretionary:
    - support tickets about the new release rise
  authority: release owner
  deputy: on-call engineer
stages:
  - id: build
    run: make build
    checks:
      - id: build-exit
        type: exit-code
  - id: deploy
    title: Deploy
    needs: [build]
    external: true
    run: make deploy
    owner_gate:
      approve: deploy the built release to production
    retry:
      max_attempts: 2
    checks:
      - id: deploy-exit
        type: exit-code
    fails:
      - id: deploy-broke
        when: the deploy leaves the service unhealthy
        detect:
          type: exit-code
        then: {run: undo-deploy}
  - id: watch-1h
    needs: [deploy]
    run: make health
    checks:
      - id: health-1h
        type: exit-code
    watch:
      after: 1h
      looks_at: [health-1h]
  - id: watch-24h
    needs: [watch-1h]
    run: make errors
    checks:
      - id: errors-24h
        type: exit-code
    watch:
      after: 24h
      looks_at: [errors-24h]
  - id: undo-deploy
    recovery: true
    run: make rollback
    checks:
      - id: undo-exit
        type: exit-code
```

## The research block

`runbook: 3` is the research format. A research runbook is the contract of an automated research
loop: agents propose a change, make it in an attempt worktree, and the gates, the metrics and the
accept rule decide whether it stays. The block says what is measured, what an attempt may change,
which gates it must pass, when an attempt is accepted and when the run stops. It has two modes:
`improve` works toward measured targets, and `repair` fixes reported bugs, each proved by a test
that fails before the fix and passes after it.

Format 3 is format 2 plus a required `research` block and a required `spec`. A runbook that has no
research block stays `runbook: 2`. The check reads a file as format 3 only when both hold:

| file | result |
|---|---|
| `runbook: 3` with a `research` key | read as format 3 |
| `runbook: 3` with no `research` key | `VERSION-UNSUPPORTED` at `runbook`: a runbook without a research block is `runbook: 2`; the rest of the file is read as format 2 |
| `runbook: 1` or `runbook: 2` with a `research` key | `FIELD-UNKNOWN` at `research` |
| any other value | `VERSION-UNSUPPORTED` |

Every rule of this section applies only to a file read as format 3. A format 1 or format 2 file
checks exactly as before: the same verdict, codes, locations and messages.

### What format 3 changes in the rest of the file

- `spec` is required, so coverage is always checked (see "Coverage in format 3" below).
- Every stage with `run` has a `timeout` (`TIMEOUT-MISSING`).
- Every stage with `run` has exactly one research role: a gate stage (named in `research.gates`),
  a holdout stage (named in `research.holdout.stages`) or the prepare stage
  (`research.repair.prepare`). A stage in two roles, a role given to a recovery stage, to a stage
  without `run` or to a stage with an `owner_gate`, and an `owner_gate` on a stage with `run` are
  `STAGE-ROLE`. A stage with `run` that no research field names and that is not a recovery stage
  would never run, so it is `STAGE-UNUSED`. An owner gate stays possible on a stage that has no
  `run` and no role, as a review outside the attempt loop.
- A gate stage `needs` only earlier gate stages or the prepare stage. A holdout stage `needs` only
  gate stages, earlier holdout stages or the prepare stage. The prepare stage has no `needs`.
  Anything else is `GATES-ORDER`.
- A fail case never answers `ask-owner` (`THEN-ASK-OWNER`): the gate chain never waits for a
  person, and the owner's decisions are holds of the run.
- Variables. Besides the format 2 variables, every command may use `${WORKTREE}`, the root of the
  tree the command runs against (the attempt worktree for a gate stage and a metric, a fresh tree
  of the final champion for a holdout stage, the case's repository for the prepare stage), and
  `${CONTRACT_DIR}`, the run's frozen copy of the protected files. A metric's `command` and `path`
  may also use `${REPEAT}` (1 for the first repeat) and `${SEED}` (the repeat's seed, only when
  `seeds` is set). A fail-to-pass `command` may also use `${REPRODUCER_DIR}`, the frozen copy of the
  case's proved reproducer. Any other name is `VAR-UNKNOWN`. A command starts in that tree's copy of
  the runbook folder unless it names a `workdir`.
- Knobs. A knob named in a metric's `command`, `path` or `workdir`, in a fail-to-pass `command`, in
  the pass-to-pass `command`, or in the `value` or `path` of a check of a gate or holdout stage must
  be `owner_only: true` (`KNOB-MOVES-BAR`). So no retry can move what an attempt is measured with or
  the threshold it must meet. A retry `move` lasts only for the retries of the stage that made it;
  every other stage, every metric repeat and every measurement of the champion starts from the
  knob values the run froze.
- Placeholders. A value that is exactly `<FILL:...>`, the placeholder of the fill-in template, is
  `FIELD-UNFILLED` anywhere in the runbook or its defaults file, whatever the field's type. It gets
  no other code, its field counts as present, and a rule that needs its value skips it.
- The numbers of the block (targets, margins, repeats, caps) are literal numbers, never a
  `${NAME}`.

Paths in the block follow one of three grammars:

- A root rule is written from the root of the repository the run works on (`research.repo.path`),
  which is also the root of every attempt worktree: `folder/`, `dir/file` or a bare name, with `*`
  and `?` inside one part. It may not be empty or absolute, and may not hold a `..` part, a
  backslash, `**`, `~` or `${`. An editable rule may not admit the whole tree (`.`, `./`, `*`,
  `*/`). Otherwise the rule is `PATHS-RULE-INVALID`. The lists that admit or keep a path
  (`research.paths.editable`, `research.paths.keep`, `research.paths.outputs`,
  `research.repair.cases[].suspect_files`, `research.coupled[].files`) match strictly: from the
  root, case kept, so `src/` admits neither `tests/src/x` nor `SRC/x`. The lists that deny or flag a
  path (`research.paths.protected`, `research.paths.hidden`, `research.paths.tests`,
  `research.holdout.paths`, `research.audit.build_files`) match the way protected paths are
  matched everywhere: case ignored, and a rule that names one folder or a bare name also hits at
  any depth, so a wider match denies more.
- A location (`research.repo.path`, `research.paths.read`, `research.fingerprint.shared`) names a
  folder on the host: an absolute path, or a path relative to the project root that may start with
  `../`. It may not be empty or hold `~` or `${` (`PATHS-RULE-INVALID`).
- A file of the pack (`research.defaults`, `research.lessons.file`,
  `research.repair.cases[].bug_report`, `research.controls.mutate`) is a path in the runbook folder,
  with the `PATH-ESCAPES` rule of every runbook path.

The runbook folder lies inside `research.repo.path`, so its files are in every attempt worktree and
in the frozen copy. The check refuses a runbook folder outside it (`PACK-OUTSIDE-REPO`) when it can
resolve that path: an absolute one always, a relative one from the project root, found as for a
`note:` source (see "Provenance").

### The block at a glance

```yaml
research:
  mode: improve | repair
  defaults: <path in the runbook folder>          # the pack defaults file
  repo: {path: <location>, ref: <git ref>}        # the repository the run works on
  metrics:                                        # improve only
    - id: <slug>
      description: <text>
      command: <text>
      workdir: <path in the runbook folder>
      parser: markers | json-field
      path: <path>                                # json-field only
      field: <dotted path>                        # json-field only
      unit: <text>
      direction: higher | lower
      kind: deterministic | noisy
      repeats: <1..50>
      min_ok: <1..repeats>
      median: worse | middle
      margin: <number>
      seeds: [<int>, ...]
      timeout: <seconds>
      target: <number>
      artifact: <text>
      covers: [<spec item>, ...]
      source: <provenance>
  accept: {rule: champion | deficit-pareto | repair, after_targets: stop | pareto, covers, source}
  paths: {editable: [..], protected: [..], hidden: [..], tests: [..], read: [..], keep: [..],
          outputs: [..], covers, source}
  fingerprint: {tools: [..], shared: [..], covers, source}
  gates: [{stage: <stage id>, heavy: true | false, lock_timeout: <seconds>}, ...]
  stop: {rounds: <1..100>, slots: <1..16>, covers, source}
  budget: {usd: <number>, tokens: <int>, wall: <seconds>, covers, source}
  disk: <GiB>
  proposal: [{field, type, required, values, max, description}, ...]
  categories: [{id, text}, ...]
  lessons: {file: <path>, note_chars: <40..2000>}
  controls: {noop_rounds: <2..10>, random: true | false, mutate: <script>, syntax: <stage id>,
             seed: <int>, covers, source}
  holdout: {stages: [..], paths: [..], covers, source}
  repair:                                         # repair only
    prepare: <stage id>
    pass_to_pass: {command, min_tests, timeout, covers, source}
    cases:
      - id: <slug>
        bug_report: <path in the runbook folder>
        base: <40 hex digits>
        fail_to_pass: {origin: store | generate, command, tests: [..], timeout, repeats}
        suspect_files: [..]
        covers: [..]
        source: <provenance>
  coupled: [{id, files: [..], note}, ...]
  runtime: {timeouts: {<role>: <seconds>}, effort: {<role>: low | medium | high}, retries: <0..10>}
  verifier: {timeout: <seconds>, rerun_timeout: <seconds>}
  audit: {hints: <text>, assert_tokens: [..], build_files: [..], steering: [..]}
  egress: {allow: [<host name>, ...]}
```

### Fields

Rules that hold for every row, so the table does not repeat them:

- A key a mapping does not have is `FIELD-UNKNOWN` and is not read further. A required field
  missing from every layer is `FIELD-MISSING`. A value of the wrong kind is `FIELD-TYPE` or
  `FIELD-NOT-LIST`; empty text or an empty required list is `FIELD-EMPTY`; a whole number outside
  its range is `RANGE`. The table names the other codes.
- "improve" or "repair" in the required column means required in that mode and `FIELD-UNKNOWN` in
  the other one. A mapping is required in a mode when a field under it is.
- `covers` and `source` may sit on every metric, every repair case, `research.repair.pass_to_pass`
  and the parts `accept`, `paths`, `fingerprint`, `stop`, `budget`, `controls` and `holdout`.
  `covers` lists spec items as a check's `covers` does (`COVERS-UNKNOWN`, `COVERS-AMBIGUOUS`), and
  on a metric or a repair case it is required in effect (`COVERS-NONE`). `source` takes the shapes
  of "Provenance" (`SOURCE-SHAPE` and `SOURCE-MISSING`, warnings). Both are written only in the
  runbook itself: the defaults file may not hold either.
- A timeout is whole seconds from 1 to 7 days, as a stage's `timeout`.
- The default column is what the engine layer fills when no layer sets the field.

| field | required | value | default | meaning, and its own codes |
|---|---|---|---|---|
| `research.mode` | yes | `improve` or `repair` | none | which loop runs; another word: `VALUE-UNKNOWN` |
| `research.defaults` | no | path in the runbook folder | none | the pack defaults file (see "The defaults file and the layers"); unreadable, not YAML, or no `research` mapping: `DEFAULTS-UNREADABLE` |
| `pack` | no | slug | none | a top key of the defaults file, not of the block: the pack's name; `ID-INVALID` |
| `research.repo` | no | mapping | made by the engine | the repository the run works on |
| `research.repo.path` | no | location | `.`, the project root | the repository the stripped clone is cut from; it holds the runbook folder (`PACK-OUTSIDE-REPO`); `PATHS-RULE-INVALID` |
| `research.repo.ref` | no | text | `HEAD` | the ref the run starts from, resolved to a commit when the contract is frozen |
| `research.metrics` | improve | list of mappings keyed by `id`, at least one | none | the measured axes |
| `research.metrics[].id` | improve | slug | none | the axis name, and the id a `METRIC:` marker line carries; `ID-INVALID`, `ID-DUPLICATE` |
| `research.metrics[].description` | no | text | none | what it measures |
| `research.metrics[].command` | improve | text | none | the measuring command, run once per repeat; `VAR-UNKNOWN`, `KNOB-MOVES-BAR` |
| `research.metrics[].workdir` | no | path in the runbook folder | the tree's copy of the runbook folder | where the command starts; `PATH-ESCAPES`, `KNOB-MOVES-BAR` |
| `research.metrics[].parser` | improve | `markers` or `json-field` | none | how the number is read: a `METRIC: <id> <number>` line between the result markers, or a field of a JSON file; `VALUE-UNKNOWN` |
| `research.metrics[].path` | with `json-field` | path in the runbook folder or under `${EVIDENCE_DIR}` | none | the JSON file the command writes; set with `markers`: `FIELD-UNKNOWN`; `PATH-ESCAPES`, `VAR-UNKNOWN`, `KNOB-MOVES-BAR` |
| `research.metrics[].field` | with `json-field` | dotted path | none | the number's field, read as the `json-field` check reads it |
| `research.metrics[].unit` | improve | text | none | the unit of the number |
| `research.metrics[].direction` | improve | `higher` or `lower` | none | which way is better; `VALUE-UNKNOWN` |
| `research.metrics[].kind` | improve | `deterministic` or `noisy` | none | the noise class; `VALUE-UNKNOWN` |
| `research.metrics[].repeats` | noisy | whole number 1 to 50, at least 2 when noisy | 1 when deterministic | measurements per tree |
| `research.metrics[].min_ok` | noisy | whole number up to `repeats`, at least 2 when noisy | `repeats` when deterministic | the fewest successful repeats that still give a score |
| `research.metrics[].median` | no | `worse` or `middle` | `worse` | for an even count of repeats: the worse of the two middle values, or their mean; `VALUE-UNKNOWN` |
| `research.metrics[].margin` | noisy | number of `unit`, 0 or more, above 0 when noisy | 0 when deterministic | an attempt must beat the champion by more than this; a repeat spread wider than it leaves the verdict untested |
| `research.metrics[].seeds` | no | list of whole numbers, one per repeat | none | the seed of each repeat, given as `${SEED}` |
| `research.metrics[].timeout` | improve | seconds | none | per repeat |
| `research.metrics[].target` | no | number | none: the axis has no target | the owner-only target of this axis; it needs a `source` of shape `spec:<item id>` or `owner:<decision ref>` (`TARGET-SOURCE`) |
| `research.metrics[].artifact` | no | text | none | the one build output every part of the metric is measured on |
| `research.metrics[].covers` | improve, in effect | list of spec items | none | the items the metric shows; `COVERS-NONE` |
| `research.metrics[].source` | with `target` | provenance | none | where the metric and its target come from; `TARGET-SOURCE` |
| `research.accept` | yes | mapping | none | the accept rule |
| `research.accept.rule` | yes | `champion`, `deficit-pareto` or `repair` | none | `champion`: improve mode, one metric, no target; `deficit-pareto`: improve mode with a target; `repair`: repair mode. `VALUE-UNKNOWN`; a rule that does not fit the block: `ACCEPT-RULE-MISMATCH` |
| `research.accept.after_targets` | no | `stop` or `pareto` | `stop` when a metric has a target, else none | once every target is met: stop, or run every round and accept only strict Pareto dominance; set with no target: `ACCEPT-RULE-MISMATCH`; `VALUE-UNKNOWN` |
| `research.accept.covers`, `research.accept.source` | no | as above | none | the spec items the accept rule shows |
| `research.paths` | yes | mapping | none | what an attempt may and may not touch |
| `research.paths.editable` | yes | list of root rules, at least one | none | what an attempt may change, matched strictly; `PATHS-RULE-INVALID`; an editable rule and a protected, hidden or holdout rule that name the same path, or one a folder rule that holds the other: `PATHS-OVERLAP` |
| `research.paths.protected` | yes | list of root rules, at least one | none | the contract paths, copied into `${CONTRACT_DIR}`; they must hold the runbook folder; `PATHS-RULE-INVALID`, `PATHS-OVERLAP` |
| `research.paths.hidden` | no | list of root rules | none | published results and notes left out of the attempt's tree and denied to agents; `PATHS-RULE-INVALID`, `PATHS-OVERLAP` |
| `research.paths.tests` | no | list of root rules | none | where the tests live, so the audit tells an edit to a test from an edit to the code; `PATHS-RULE-INVALID` |
| `research.paths.read` | no | list of locations | none | read paths outside the tree an agent may read; `PATHS-RULE-INVALID` |
| `research.paths.keep` | no | list of root rules | none | ignored outputs the purge keeps; `PATHS-RULE-INVALID` |
| `research.paths.outputs` | no | list of root rules | none | ignored outputs the eval may create; `PATHS-RULE-INVALID` |
| `research.paths.covers`, `research.paths.source` | no | as above | none | the spec items the path rules show, such as an edge case that refuses an edit to the tests |
| `research.fingerprint` | yes | mapping | none | what is hashed before and after each agent and the eval |
| `research.fingerprint.tools` | yes, may be `[]` | list of command names | none | tools found on the agent's search path and hashed; not a bare command name: `TOOL-INVALID` |
| `research.fingerprint.shared` | no | list of locations | none | shared checkouts outside the tree to hash; `PATHS-RULE-INVALID` |
| `research.fingerprint.covers`, `research.fingerprint.source` | no | as above | none | the spec items the fingerprint shows |
| `research.gates` | yes | list of mappings, at least one | none | the gate chain, in run order; `GATES-ORDER` |
| `research.gates[].stage` | yes | stage id | none | a stage the chain runs in every attempt; `STAGE-UNKNOWN`, `STAGE-ROLE` |
| `research.gates[].heavy` | no | true or false | false | the stage takes the machine lock |
| `research.gates[].lock_timeout` | no | seconds | 14400 | how long a heavy stage waits for the machine lock before the gate is BLOCKED |
| `research.stop` | yes | mapping | none | when the run stops |
| `research.stop.rounds` | yes | whole number 1 to 100 | none | N: at N the run halts and asks the owner |
| `research.stop.slots` | yes | whole number 1 to 16 | none | K attempts per round; in repair mode, per open case |
| `research.stop.covers`, `research.stop.source` | no | as above | none | the spec items the stop rule shows |
| `research.budget` | no | mapping | none: the run is uncapped | the run's caps, each set on purpose; past one the run pauses for the owner. A run with no cap is never held on cost or time, and its spend is still priced, recorded and reported |
| `research.budget.usd` | no | number above 0, at most 1000000 | none: no cap | the money cap; with it or the token cap set, a launch left unpriced also pauses the run |
| `research.budget.tokens` | no | whole number 1000 to 10^12 | none: no cap | the token cap |
| `research.budget.wall` | no | whole seconds, 60 to 30 days | none: no cap | the wall-time cap |
| `research.budget.covers`, `research.budget.source` | no | as above | none | the spec items the budget shows |
| `research.disk` | yes | number of GiB above 0, at most 100000 | none | free disk the run needs where its worktrees land; text such as `20G` is `FIELD-TYPE` |
| `research.proposal` | no | list of field specs keyed by `field` | none | the pack's proposal fields beyond the engine's base fields |
| `research.proposal[].field` | yes | name: a lower-case letter, then letters, digits or `_` | none | the proposal key; `ID-INVALID`, `ID-DUPLICATE`; a base field name: `PROPOSAL-FIELD-RESERVED` |
| `research.proposal[].type` | yes | `str`, `text`, `int`, `float`, `bool`, `enum`, `list` or `paths` | none | the value's type; `VALUE-UNKNOWN` |
| `research.proposal[].required` | no | true or false | true | the proposal must hold it |
| `research.proposal[].values` | with `enum` | list of text, at least one | none | the allowed words; with another type: `FIELD-UNKNOWN` |
| `research.proposal[].max` | no | whole number 1 to 100000 | 200 (`str`), 4000 (`text`), 50 (`list`, `paths`) | most characters or items; on another type: `FIELD-UNKNOWN` |
| `research.proposal[].description` | no | text | none | what the field asks, shown to the proposer |
| `research.categories` | improve | list of `{id, text}`, at least one | none | hypothesis categories; slot s gets category s modulo the list length; optional in repair mode |
| `research.categories[].id` | yes | slug | none | the id a proposal names; `ID-INVALID`, `ID-DUPLICATE` |
| `research.categories[].text` | yes | text | none | what the proposer is shown |
| `research.lessons` | no | mapping | none | the pack's lessons for the proposer |
| `research.lessons.file` | no | path in the runbook folder | none | the lessons, carried into proposer prompts as untrusted text; absent: `FILE-MISSING`; under a hidden or holdout rule: `PATHS-OVERLAP`; `PATH-ESCAPES` |
| `research.lessons.note_chars` | no | whole number 40 to 2000 | 300 | the longest note line the notes writer may post |
| `research.controls` | yes | mapping | none | the controls run before a real run |
| `research.controls.noop_rounds` | yes | whole number 2 to 10 | none | rounds of the no-op control under this contract |
| `research.controls.random` | yes | true or false | none | also run the random-edit control |
| `research.controls.mutate` | with `random: true` | script path in the runbook folder | none | the pack's mutation operators, run as a plugin script; `PATH-ESCAPES`, `PLUGIN-SCRIPT-VARIABLE`, `PLUGIN-MISSING`, `PLUGIN-NOT-EXECUTABLE` |
| `research.controls.syntax` | with `random: true` | gate stage id | none | the check a mutation must pass before it counts; `STAGE-UNKNOWN`; not a gate stage: `STAGE-ROLE` |
| `research.controls.seed` | no | whole number 0 to 2^31 | 0 | the random control's first seed |
| `research.controls.covers`, `research.controls.source` | no | as above | none | the spec items the controls show |
| `research.holdout` | no | mapping | empty lists | checks that run on the final champion only |
| `research.holdout.stages` | no | list of stage ids | none | stages run only on the final champion; `STAGE-UNKNOWN`, `STAGE-ROLE`, `GATES-ORDER` |
| `research.holdout.paths` | no | list of root rules | none | held-out data: stripped from the attempt's tree, left out of `${CONTRACT_DIR}`, reads denied; a generated set keeps its recipe under these paths (below); `PATHS-RULE-INVALID`, `PATHS-OVERLAP` |
| `research.holdout.covers`, `research.holdout.source` | no | as above | none | the spec items the holdout checks show |
| `research.repair` | repair | mapping | none | the repair set |
| `research.repair.prepare` | no | stage id | none | a stage run once per case on its base, whose output is committed as the case's baseline; `STAGE-UNKNOWN`, `STAGE-ROLE`, `GATES-ORDER` |
| `research.repair.pass_to_pass` | repair | mapping | none | the old test suite every fix must keep passing |
| `research.repair.pass_to_pass.command` | repair | text | none | the old suite, run from `${CONTRACT_DIR}`; `VAR-UNKNOWN`, `KNOB-MOVES-BAR` |
| `research.repair.pass_to_pass.min_tests` | repair | whole number 1 to 100000 | none | the fewest tests that must pass on the base |
| `research.repair.pass_to_pass.timeout` | repair | seconds | none | per run of the suite |
| `research.repair.pass_to_pass.covers`, `research.repair.pass_to_pass.source` | no | as above | none | the spec items the old suite shows, such as a criterion that old tests keep passing |
| `research.repair.cases` | repair | list of mappings keyed by `id`, at least one | none | the bugs of this repair set |
| `research.repair.cases[].id` | repair | slug | none | the case id; it names the case's reproducer folder; `ID-INVALID`, `ID-DUPLICATE` |
| `research.repair.cases[].bug_report` | repair | path in the runbook folder | none | the owner's bug report; absent: `FILE-MISSING`; under a hidden or holdout rule: `PATHS-OVERLAP`; `PATH-ESCAPES` |
| `research.repair.cases[].base` | no | 40 hex digits | the run's base commit | the commit the case starts from; a branch, a tag or a short id: `BASE-NOT-COMMIT` |
| `research.repair.cases[].fail_to_pass` | repair | mapping | none | how the case's reproducer is run |
| `research.repair.cases[].fail_to_pass.origin` | repair | `store` or `generate` | none | the owner's proved reproducer from the reproducer store, or one agents write and review; `VALUE-UNKNOWN` |
| `research.repair.cases[].fail_to_pass.command` | no | text | `sh ${REPRODUCER_DIR}/run.sh` | how the gate runs the reproducer; `VAR-UNKNOWN`, `KNOB-MOVES-BAR` |
| `research.repair.cases[].fail_to_pass.tests` | with `origin: store` | list of test names | none | the `TEST:` names that fail on the base and pass on the fix |
| `research.repair.cases[].fail_to_pass.timeout` | no | seconds | 1200 | per run of the reproducer |
| `research.repair.cases[].fail_to_pass.repeats` | no | whole number 1 to 20 | 1 | runs per tree; every run must show the verdict |
| `research.repair.cases[].suspect_files` | no | list of root rules | none | where the bug is believed to be, a hint for the repair prompt; `PATHS-RULE-INVALID` |
| `research.repair.cases[].covers` | repair, in effect | list of spec items | none | the success criteria and edge cases of the case's bug; `COVERS-NONE` |
| `research.repair.cases[].source` | no | provenance | none | where the case comes from |
| `research.coupled` | no | list of `{id, files, note}` | none | groups of files that must change together |
| `research.coupled[].id` | yes | slug | none | the group's name; `ID-INVALID`, `ID-DUPLICATE` |
| `research.coupled[].files` | yes | list of root rules, at least two | none | the files of the group; fewer than two: `RANGE`; `PATHS-RULE-INVALID` |
| `research.coupled[].note` | no | text | none | why they change together, shown in the repair prompt |
| `research.runtime` | no | mapping | made by the engine | how the agents are launched |
| `research.runtime.timeouts` | no | mapping of role to seconds | see each role | the launch timeout of each agent role; an unknown role: `FIELD-UNKNOWN` |
| `research.runtime.timeouts.proposer` | no | seconds | 1200 | the agent that writes the proposal |
| `research.runtime.timeouts.implementer` | no | seconds | 1800 | the agent that makes the change |
| `research.runtime.timeouts.notes` | no | seconds | 240 | the agent that writes the round's notes |
| `research.runtime.timeouts.audit` | no | seconds | 900 | the agent that audits the diff |
| `research.runtime.timeouts.reproducer` | no | seconds | 1800 | the agent that writes a generated reproducer |
| `research.runtime.timeouts.reviewer` | no | seconds | 900 | the agent that reviews a generated reproducer |
| `research.runtime.effort` | no | mapping of role to `low`, `medium` or `high` | none: each runtime's own | the reasoning effort of each role; an unknown role: `FIELD-UNKNOWN`; another word: `VALUE-UNKNOWN` |
| `research.runtime.effort.proposer`, `research.runtime.effort.implementer`, `research.runtime.effort.notes`, `research.runtime.effort.audit`, `research.runtime.effort.reproducer`, `research.runtime.effort.reviewer` | no | `low`, `medium` or `high` | none | the effort of that role |
| `research.runtime.retries` | no | whole number 0 to 10 | 2 | infrastructure retries per launch |
| `research.verifier` | no | mapping | made by the engine | the claim verifiers |
| `research.verifier.timeout` | no | seconds | 1800 | one verifier pass |
| `research.verifier.rerun_timeout` | no | seconds | 1200 | each evidence command a verifier runs again |
| `research.audit` | no | mapping | made by the engine | the pack's wording for the audit |
| `research.audit.hints` | no | text | none | what test targeting and a build bypass look like in this domain |
| `research.audit.assert_tokens` | no | list of text | none | tokens that mark an assertion in the pack's languages |
| `research.audit.build_files` | no | list of root rules | none | build and configuration files whose edit may bypass the build; `PATHS-RULE-INVALID` |
| `research.audit.steering` | no | list of text | none | extra phrases that mark text written to sway a reviewer |
| `research.egress` | no | mapping | made by the engine | network hosts an agent may reach |
| `research.egress.allow` | no | list of host names | none | hosts beyond the runtime's own an agent may reach on port 443; not a lower-case host name with no scheme, port or path: `HOST-INVALID` |

These rules are the engine's and have no field, so a key for one is `FIELD-UNKNOWN`: the halt at N
and the owner's extension, the verifier's round cap, the eval without network, the purge keep list
of the engine, the result markers grammar (`ALPACA-RESULTS-BEGIN`, `TEST: <name> ... PASS`,
`METRIC: <id> <number>`, `ALPACA-RESULTS-END`), and where the reproducer store lies.

Generated held-out data. When a program cuts the held-out data from a seed, an agent that can run it
with that seed gets the held-out bytes without reading a held-out path. State the recipe, the generator
and the held-out seed, only in a file under a `research.holdout.paths` rule, never in a file an attempt
can read. Draw the held-out seed at random with at least 64 bits of entropy, from a generator whose
state is at least as wide, and write no bound on it where an attempt can read it. A held-out total in a
readable file, such as a baseline figure or a threshold, confirms a guessed seed: over a small seed
space it turns a search into a sure find, and at 64 bits the search cannot run. Tracked held-out files
also sit in the repository's history; a pack that needs them hidden from an agent's shell keeps them
off the machine.

Name the recipe file `recipe.json`: a JSON object with `generator` (the program's path from the
repository root, with a distinctive file name of four or more characters), `option` (its seed option,
default `--seed`, or `null` for a positional seed), `visible` (the seeds of the visible data) and,
optionally, `seed`, which the engine never reads. The audit hands the auditor every call that runs,
imports or copies that program other than one read or one run with a visible seed, and every file in
an attempt's own folders that equals a held-out file; neither stops the attempt, and a call that hides
the program's name is left to the auditor's judgment. The one run the audit leaves quiet is a plain
Python run, `python3 <path>/<name>.py --seed <visible>` or `python3 -m <package>.<name> --seed
<visible>`, alone or beside reads, `cd`, `mkdir -p`, `cp` and other plain Python runs; a pack that shows
how to cut its visible data shows that form, since a wrapper such as `uv run`, `nice` or a shell script
makes the honest run a candidate too.

When several contracts share one pack folder, each contract's clone strips only its own held-out and
hidden rules, so each runbook also lists the other contracts' held-out and hidden paths as hidden.

### The defaults file and the layers

A pack whose runbooks share settings names one YAML file in each runbook's block
(`defaults: pack.yaml`). It lies in the runbook folder or below it and holds:

```yaml
pack: <slug>          # optional: the pack's name
research:             # required: any research field except mode, defaults, covers and source
  ...
```

Any other top key is `FIELD-UNKNOWN`, and so are `mode`, `defaults`, and `covers` and `source` on
any part, metric or case: coverage is each runbook's own, since two runbooks that share the file
cover two specs. Paths in it are read from the runbook folder of the runbook being checked. The
check always reads it, with `--no-files` too, because it is part of the contract. A finding in it
points at the file, for example `pack.yaml:research.budget.usd`.

The block is built from layers, merged in this order: the engine's defaults (the default column
above, only for optional fields), the defaults file, the runbook's own block. On our side a task
may add a fourth layer, its override, when a run starts. The merge rule:

- a mapping merges key by key;
- a keyed list merges item by item on its key: `research.metrics` and `research.repair.cases` on
  `id`, `research.categories` and `research.coupled` on `id`, `research.proposal` on `field`; a new
  key is appended, and no layer can drop an item;
- every other list, and every scalar, is replaced whole.

Conditional defaults apply after each merge: a deterministic metric with no `repeats`, `min_ok` or
`margin` gets 1, its `repeats` and 0; `research.accept.after_targets` gets `stop` only when some
metric has a `target`; a proposal field of type `str`, `text`, `list` or `paths` with no `max`
gets 200, 4000, 50 or 50.

The check validates each layer's shape where it is written, then the merged block's required
fields and the rules across fields, then coverage.

### Coverage in format 3

The items are those of format 2: the success criteria and edge cases of a spec-kit spec, or the
scenarios of an OpenSpec spec, are required; functional requirements are not. The coverers are
those of format 2 (a check by its id, a fail case with `detect`, the owner gate of a stage without
`run`) plus the block's:

| coverer | named in coverage | shows |
|---|---|---|
| a metric's `covers` | `metric:<id>` | the number, its noise rule and its target |
| a repair case's `covers` | `case:<id>` | the case's fail-to-pass test and the old suite |
| `research.repair.pass_to_pass.covers` | `research:pass-to-pass` | old tests still pass |
| `research.accept.covers` | `research:accept` | the accept rule |
| `research.stop.covers` | `research:stop` | N, K, and the halt at N |
| `research.budget.covers` | `research:budget` | the caps and the pause past them |
| `research.paths.covers` | `research:paths` | what an attempt may and may not change |
| `research.fingerprint.covers` | `research:fingerprint` | the tools and checkouts hashed |
| `research.controls.covers` | `research:controls` | the no-op and random-edit controls |
| `research.holdout.covers` | `research:holdout` | the checks run on the final champion only |

- Every required item needs at least one coverer, else `SPEC-UNCOVERED`, whose message names the
  research coverers too. A `covers` entry that names nothing is `COVERS-UNKNOWN`.
- Every metric and every repair case covers at least one required item, else `COVERS-NONE`: what
  is measured and what is fixed trace to the spec, so a metric nobody asked for cannot enter the
  accept rule. A metric or case whose `covers` names only functional requirements, or has no
  `covers`, is refused. One whose `covers` holds an entry that names nothing gets
  `COVERS-UNKNOWN` for that entry instead.
- A metric with a `target` has a `source` of shape `spec:<item id>` or `owner:<decision ref>`
  (`TARGET-SOURCE`), so every target names where it came from; a target is never a default.
- A `note:` source on a metric, a case or a part is looked up as any other (`SOURCE-MISSING`).

### Error codes

The research codes, in the format of every other check error. Two names sit next to format 2 codes
with other meanings: `VALUE-UNKNOWN` is a research field's word outside its list, not a check's
threshold (`VALUE-NOT-NUMBER`), and `FILE-MISSING` is a file the block names, not the runbook file
itself (`FILE-UNREADABLE`, which makes the verdict BLOCKED).

| code | meaning | fields |
|---|---|---|
| `FIELD-UNFILLED` | a `<FILL:...>` placeholder of the fill-in template is still in the file; the value gets no other code | any value of the runbook or its defaults file |
| `VALUE-UNKNOWN` | a field that takes one of a fixed list of words got another; the message names the list | `research.mode`, `research.metrics[].parser`, `research.metrics[].direction`, `research.metrics[].kind`, `research.metrics[].median`, `research.accept.rule`, `research.accept.after_targets`, `research.proposal[].type`, `research.repair.cases[].fail_to_pass.origin`, `research.runtime.effort` |
| `DEFAULTS-UNREADABLE` | the defaults file cannot be read, is not YAML, or has no `research` mapping | `research.defaults` |
| `TARGET-SOURCE` | a metric with a `target` has no `source` of shape `spec:<item id>` or `owner:<decision ref>` | `research.metrics[].target`, `research.metrics[].source` |
| `ACCEPT-RULE-MISMATCH` | the accept rule does not fit the block: `champion` needs improve mode, exactly one metric and no target; `deficit-pareto` needs improve mode and a target; `repair` needs repair mode; `after_targets` needs a target | `research.accept.rule`, `research.accept.after_targets` |
| `PATHS-RULE-INVALID` | a root rule or a location breaks its grammar, or an editable rule admits the whole tree | every path list of the block |
| `PATHS-OVERLAP` | an editable rule and a protected, hidden or holdout rule name the same path, or one is a folder rule that holds the other; or the lessons file or a bug report, which reach agent prompts, lies under a hidden or holdout rule | `research.paths.editable`, `research.paths.protected`, `research.paths.hidden`, `research.holdout.paths`, `research.lessons.file`, `research.repair.cases[].bug_report` |
| `TOOL-INVALID` | a fingerprint tool that is not a bare command name (letters, digits, `.`, `_`, `+`, `-`) | `research.fingerprint.tools` |
| `HOST-INVALID` | an egress host that is not a host name | `research.egress.allow` |
| `STAGE-UNKNOWN` | a research field names a stage the runbook does not declare | `research.gates[].stage`, `research.holdout.stages`, `research.repair.prepare`, `research.controls.syntax` |
| `STAGE-ROLE` | a stage in two research roles; a role given to a recovery stage, a stage without `run` or a stage with an `owner_gate`; `research.controls.syntax` naming a stage that is not a gate stage; an `owner_gate` on a stage with `run` | stages and the fields above |
| `STAGE-UNUSED` | a stage with `run` that is not a recovery stage and that no research field names | `stages[]` |
| `GATES-ORDER` | a gate or holdout stage `needs` a stage its role may not need, or the prepare stage has `needs` | `stages[].needs` |
| `TIMEOUT-MISSING` | a stage with `run` has no `timeout` | `stages[].timeout` |
| `THEN-ASK-OWNER` | a fail case answers `ask-owner` | `stages[].fails[].then` |
| `KNOB-MOVES-BAR` | a knob that is not `owner_only` is named where it would move what an attempt is measured or tested with, or its threshold | metric commands, paths and workdirs, fail-to-pass and pass-to-pass commands, gate and holdout check values and paths |
| `PACK-OUTSIDE-REPO` | the runbook folder does not lie inside `research.repo.path` | `research.repo.path` |
| `PROPOSAL-FIELD-RESERVED` | a pack proposal field reuses a base field name | `research.proposal[].field` |
| `FILE-MISSING` | a file the block names does not exist (skipped with `--no-files`) | `research.lessons.file`, `research.repair.cases[].bug_report` |
| `BASE-NOT-COMMIT` | a case `base` that is not 40 hex digits; a branch or tag could move after the contract is frozen | `research.repair.cases[].base` |
| `COVERS-NONE` | with a spec, a metric or a repair case has no `covers`, or its `covers` names no required spec item | `research.metrics[].covers`, `research.repair.cases[].covers` |

No research warning code is added. The generic codes the block also gives are those of format 2:
`FIELD-MISSING`, `FIELD-UNKNOWN`, `FIELD-EMPTY`, `FIELD-TYPE`, `FIELD-NOT-LIST`, `RANGE`,
`ID-INVALID`, `ID-DUPLICATE`, `PATH-ESCAPES`, `VAR-UNKNOWN`, the `PLUGIN-*` codes, `SPEC-MISSING`,
`SPEC-UNCOVERED`, `COVERS-UNKNOWN`, `COVERS-AMBIGUOUS`, and the warnings `SOURCE-SHAPE` and
`SOURCE-MISSING`.

### A worked improve example

A small pack, `packs/sortbench/`, improves a sort routine in `src/sortlib/`. The spec,
`improve-spec.md`:

```markdown
# Feature Specification: a faster sortlib.sort

## User Scenarios & Testing

### Edge Cases

- **EC-001**: An empty list and a one-element list come back unchanged.
- **EC-002**: A benchmark run that prints no result markers gets no score.
- **EC-003**: A change that edits the benchmark, the tests or this pack is refused.

## Requirements

- **FR-001**: sortlib.sort keeps its signature and returns a new sorted list.

## Success Criteria

- **SC-001**: The sort benchmark makes at most 18000 comparisons on the fixed input set.
- **SC-002**: The median wall time of the sort benchmark over 5 runs is at most 120 ms.
- **SC-003**: Every correctness test of the pack passes on the champion.
- **SC-004**: A run stops when both targets are met, or after 5 rounds, when it asks the owner.
```

The defaults file the pack's runbooks share, `pack.yaml`:

```yaml
# Settings every research runbook of the sortbench pack shares.
pack: sortbench
research:
  paths:
    protected: [packs/sortbench/, pyproject.toml]
  fingerprint:
    tools: [python3]
  budget:
    usd: 40
    tokens: 3000000
    wall: 14400
  disk: 2
  lessons:
    file: lessons.md
  runtime:
    timeouts: {proposer: 900, implementer: 1800}
    retries: 2
```

The runbook, `improve.yaml`:

```yaml
# Research contract, improve mode, for the sortbench pack.
runbook: 3
id: sortbench-improve
title: Fewer comparisons and less wall time for sortlib.sort
description: >-
  Agents change src/sortlib/ to cut the comparisons and the wall time of the sort benchmark,
  while every correctness test of the pack keeps passing.
spec: improve-spec.md
owner: the sortlib maintainer

stages:
  - id: build
    title: the package compiles
    run: python3 -m compileall -q ${WORKTREE}/src/sortlib
    timeout: 120
    checks:
      - {id: build-exit, type: exit-code}
  - id: unit
    title: the correctness tests, run from the contract copy
    needs: [build]
    run: >-
      python3 ${CONTRACT_DIR}/packs/sortbench/tests/run_tests.py
      --src ${WORKTREE}/src --json ${EVIDENCE_DIR}/unit.json
    timeout: 600
    checks:
      - id: unit-pass
        type: json-field
        path: ${EVIDENCE_DIR}/unit.json
        field: failed
        op: "=="
        value: 0
        covers: [SC-003, FR-001]
        source: spec:SC-003
      - id: unit-edge
        type: json-field
        path: ${EVIDENCE_DIR}/unit.json
        field: edge_cases_passed
        op: "=="
        value: true
        covers: [EC-001]
        source: spec:EC-001
  - id: holdout-bench
    title: the benchmark on the held-out input sets, on the final champion only
    needs: [build]
    run: python3 bench/measure.py --src ${WORKTREE}/src --inputs holdout --json ${EVIDENCE_DIR}/holdout.json
    timeout: 900
    checks:
      - {id: holdout-ran, type: file-exists, path: "${EVIDENCE_DIR}/holdout.json"}

research:
  mode: improve
  defaults: pack.yaml
  metrics:
    - id: compares
      description: comparisons the sort makes on the fixed input set
      command: >-
        python3 ${CONTRACT_DIR}/packs/sortbench/bench/measure.py --src ${WORKTREE}/src
        --metric compares
      parser: markers
      unit: comparisons
      direction: lower
      kind: deterministic
      timeout: 300
      target: 18000
      artifact: one run of src/sortlib over the fixed inputs in bench/inputs
      covers: [SC-001, EC-002]
      source: spec:SC-001
    - id: wall_ms
      description: wall time of one benchmark pass
      command: >-
        python3 ${CONTRACT_DIR}/packs/sortbench/bench/measure.py --src ${WORKTREE}/src
        --metric wall_ms --seed ${SEED} --json ${EVIDENCE_DIR}/wall-${REPEAT}.json
      parser: json-field
      path: ${EVIDENCE_DIR}/wall-${REPEAT}.json
      field: wall_ms
      unit: ms
      direction: lower
      kind: noisy
      repeats: 5
      # min_ok and margin: interview:research-noise
      min_ok: 3
      median: worse
      margin: 4
      seeds: [11, 12, 13, 14, 15]
      timeout: 120
      target: 120
      covers: [SC-002]
      source: spec:SC-002
  accept:
    rule: deficit-pareto
    after_targets: stop
  paths:
    editable: [src/sortlib/]
    covers: [EC-003]
    source: spec:EC-003
  gates:
    - {stage: build}
    - {stage: unit}
  stop:
    rounds: 5
    # slots: interview:research-stop
    slots: 2
    covers: [SC-004]
    source: spec:SC-004
  proposal:
    - field: risk
      type: enum
      values: [low, medium, high]
      description: how likely the change is to break a correctness test
  categories:
    - {id: fewer-compares, text: change the algorithm or its cut-over points so it compares less}
    - {id: small-inputs, text: add or tune a path for short runs and small lists}
    - {id: less-work, text: "remove copies, allocations and repeated key calls"}
  controls:
    noop_rounds: 2
    random: true
    mutate: bench/mutate.py
    syntax: build
    seed: 100
  holdout:
    stages: [holdout-bench]
    paths: [packs/sortbench/holdout/]
```

SC-002 states the 5 repeats and the 120 ms target, and SC-004 the 5 rounds. The spec states no
`min_ok`, `margin` or slot count, so the owner's answers to the interview gave them, and a comment
line above each names the slot in the `interview:<slot id>` shape; the metric's `source` stays
`spec:SC-002`, which its target needs. The budget, the protected paths and the tools come from
`pack.yaml`. Checked from the pack folder with `--spec improve-spec.md`:

```
runbook: improve.yaml
spec: improve-spec.md (spec-kit, 8 item(s), 7 required)
coverage: 7 of 7 required item(s) covered
GATE alpaca-runbook-check: PASS
```

SC-001 and EC-002 are covered by `metric:compares`, SC-002 by `metric:wall_ms`, SC-003 by
`unit-pass`, EC-001 by `unit-edge`, SC-004 by `research:stop` and EC-003 by `research:paths`;
FR-001 by `unit-pass`, so no `FR-UNCOVERED` warning. Delete `covers: [SC-002]` from `wall_ms` and
the check fails twice: `SPEC-UNCOVERED` for SC-002, and `COVERS-NONE` at
`research.metrics[1].covers`.

## `alpaca runbook check`

```
alpaca runbook check <file> [--spec <path>] [--no-files] [--json]
```

- `--spec` takes a spec-kit `spec.md`, an OpenSpec `spec.md` (main or delta), or an OpenSpec
  folder. Without it the runbook's own `spec:` field is used; without either, only the format is
  checked (and `covers` lists get a `COVERS-WITHOUT-SPEC` warning).
- `--no-files` skips the plugin script look-ups, for a runbook written before its scripts, and
  in a research runbook the `FILE-MISSING` look-ups too.
- `--json` prints `verdict`, `runbook`, `spec`, `errors`, `warnings` and `coverage` as JSON.
- A relative `<file>` or `--spec` path is read from the folder you run the command in, also
  through `bin/alpaca` (which starts Python in the install root; `bin/alpaca-python` passes the
  folder it was called from as `ALPACA_CALLER_CWD`). So `alpaca runbook check runbook.yaml
  --spec spec.md` works from the folder that holds both files.
- Exit status: 0 PASS, 1 FAIL (malformed, or an item uncovered), 2 BLOCKED (the runbook file
  cannot be read), 64 usage error.

The verb reads files and writes nothing, not even to the record.

Every error is reported at once, one per line: `ERROR <code> <where>: <message>`.

| code | meaning |
|---|---|
| `FILE-UNREADABLE` | the runbook file cannot be read (BLOCKED) |
| `YAML-SYNTAX` | the file is not valid YAML; the line and column are named |
| `NOT-MAPPING` | the top of the file is not a mapping |
| `KEY-DUPLICATE` | a key is given twice in one mapping |
| `FIELD-MISSING` | a required field is absent |
| `FIELD-UNKNOWN` | a field this block does not have (a near name is suggested) |
| `FIELD-EMPTY` | a required text or list is empty |
| `FIELD-TYPE` | a field has the wrong kind of value |
| `FIELD-NOT-LIST` | a field that takes a list got something else |
| `VERSION-UNSUPPORTED` | `runbook:` is not a version this reader knows |
| `ID-INVALID` | an id does not follow the naming rule |
| `ID-DUPLICATE` | a stage, check, knob or failure id is used twice |
| `NEEDS-UNKNOWN` | `needs` names a stage that is not declared earlier |
| `RUN-MISSING` | a stage has neither `run` nor `owner_gate` |
| `CHECKS-EMPTY` | a stage runs a command but has no check |
| `CHECKS-WITHOUT-RUN` | a stage without `run` lists `checks` (nothing would judge them) |
| `CHECK-TYPE-UNKNOWN` | a check `type` outside the list above |
| `REGEX-INVALID` | a `pattern` does not compile |
| `OP-UNKNOWN` | a `json-field` `op` outside the list above |
| `VALUE-NOT-NUMBER` | an ordering op with a threshold that is not a number |
| `RANGE` | a number outside its allowed range (or `min` above `max`, or a `by` of 0) |
| `PATH-ESCAPES` | an absolute path, or one that leaves the runbook folder |
| `PLUGIN-MISSING` | a plugin script does not exist |
| `PLUGIN-NOT-EXECUTABLE` | a plugin script is not executable |
| `PLUGIN-SCRIPT-VARIABLE` | a plugin `script` holds `${NAME}` (only `args` are replaced) |
| `KNOB-TYPE-UNKNOWN` | a knob `type` outside the list above |
| `KNOB-DEFAULT-TYPE` | a knob default does not fit its type |
| `KNOB-DEFAULT-RANGE` | a knob default is outside `min`..`max` or not one of `values` |
| `KNOB-UNKNOWN` | a retry moves a knob that is not declared |
| `KNOB-OWNER-ONLY` | a retry moves an `owner_only` knob |
| `KNOB-NOT-NUMBER` | a retry moves a knob that is not `int` or `float` |
| `MOVE-NOT-INT` | a retry moves an `int` knob by a fraction |
| `RETRY-CHECK-UNKNOWN` | `on_fail` or `stop_on` names a check that is not in the stage |
| `RETRY-OVERLAP` | a check is in both `on_fail` and `stop_on` |
| `VAR-UNKNOWN` | `${NAME}` names no knob and no built-in variable |
| `SPEC-MISSING` | the spec cannot be read |
| `SPEC-MIXED` | a spec folder holds a spec that is not OpenSpec |
| `SPEC-EMPTY` | the spec has nothing to cover |
| `SPEC-UNCOVERED` | a success criterion, edge case or scenario is covered by no check, fail case or owner gate (in format 3, nor by a metric, a repair case or a research part) |
| `COVERS-UNKNOWN` | a `covers` entry names nothing in the spec |
| `COVERS-AMBIGUOUS` | a short OpenSpec key matches scenarios in more than one capability |
| `SPEC-DELTA` | an OpenSpec change folder does not apply to its living specs (it names a requirement they lack, or adds one they have) |
| `EC-UNNUMBERED` | a bullet under an Edge Cases heading has no `EC-nnn` id (format 2); the line and the text are named |
| `THEN-MISSING` | a fail case has `detect` and no `then` |
| `THEN-UNKNOWN` | `then` is not `retry`, `stop`, `ask-owner` or `{run: <stage id>}` |
| `RECOVERY-UNKNOWN` | `then: {run: ...}` names no stage of the runbook |
| `RECOVERY-NOT-MARKED` | `then: {run: ...}` names a stage without `recovery: true` |
| `RECOVERY-NEEDED` | a stage `needs` a recovery stage, or a recovery stage has `needs` |
| `RECOVERY-SELF` | a fail case of a recovery stage sends to that same stage |
| `RECOVERY-CHAIN` | a fail case of a recovery stage sends to another recovery stage |
| `RECOVERY-NO-ATTEMPT` | a fail case sends to a recovery stage from a stage with one attempt (no `retry`, or `max_attempts: 1`) |
| `DETECT-INVALID` | a `detect` breaks a check rule; the message names the rule |
| `COVERS-NO-DETECT` | a fail case without `detect` lists `covers`; only a fail case that detects covers |
| `KIND-UNKNOWN` | `kind` is text other than `plain` or `ship` |
| `SHIP-NO-ROLLBACK` | a `kind: ship` runbook has no `rollback` block |
| `SHIP-NO-EXTERNAL` | a `kind: ship` runbook has no stage that runs a command and says `external: true` |
| `SHIP-NO-RECOVERY` | an `external: true` stage has no recovery entry (a fail case with `detect` and `then: {run: <recovery stage id>}`) |
| `SHIP-NO-WATCH` | a `kind: ship` runbook has no valid `watch` of `1h` or of `24h` that follows the change; the message names the window |
| `WATCH-AFTER-INVALID` | `watch.after` is text that is not a whole number and `m`, `h` or `d` |
| `WATCH-CHECK-UNKNOWN` | `watch.looks_at` names a check that is not in the same stage |
| `WATCH-BEFORE-CHANGE` | a stage with a `watch` does not reach an `external: true` stage through `needs` |
| `ROLLBACK-CHECK-UNKNOWN` | `rollback.automatic` names a check that no stage of the run order has |
| `ROLLBACK-DEPUTY-SAME` | `rollback.deputy` names the same person as `rollback.authority` |
| `FIELD-UNFILLED` | format 3: a `<FILL:...>` template placeholder is still in the file |
| `VALUE-UNKNOWN` | format 3: a research field's word is outside its list (not a check threshold, which is `VALUE-NOT-NUMBER`) |
| `DEFAULTS-UNREADABLE` | format 3: the defaults file cannot be read or has no `research` mapping |
| `TARGET-SOURCE` | format 3: a metric's target has no `spec:` or `owner:` source |
| `ACCEPT-RULE-MISMATCH` | format 3: the accept rule does not fit the mode and the targets |
| `PATHS-RULE-INVALID` | format 3: a path rule or location breaks its grammar |
| `PATHS-OVERLAP` | format 3: an editable rule meets a protected, hidden or holdout rule, or a prompt file lies under a hidden or holdout rule |
| `TOOL-INVALID` | format 3: a fingerprint tool is not a bare command name |
| `HOST-INVALID` | format 3: an egress host is not a host name |
| `STAGE-UNKNOWN` | format 3: a research field names an undeclared stage |
| `STAGE-ROLE` | format 3: a stage has two research roles, or a role it may not have |
| `STAGE-UNUSED` | format 3: a stage with `run` has no research role |
| `GATES-ORDER` | format 3: a gate, holdout or prepare stage `needs` a stage its role may not need |
| `TIMEOUT-MISSING` | format 3: a stage with `run` has no `timeout` |
| `THEN-ASK-OWNER` | format 3: a fail case answers `ask-owner` |
| `KNOB-MOVES-BAR` | format 3: a knob that is not `owner_only` could move a measurement or a gate threshold |
| `PACK-OUTSIDE-REPO` | format 3: the runbook folder lies outside `research.repo.path` |
| `PROPOSAL-FIELD-RESERVED` | format 3: a pack proposal field reuses a base field name |
| `FILE-MISSING` | format 3: a lessons file or bug report the block names does not exist (not the runbook file itself, which is `FILE-UNREADABLE`) |
| `BASE-NOT-COMMIT` | format 3: a repair case `base` is not a 40-digit commit id |
| `COVERS-NONE` | format 3: a metric or a repair case covers no required spec item |

Warnings (`WARN <code> <where>: <message>`) do not change the verdict: `FR-UNCOVERED`,
`SPEC-CLARIFY`, `COVERS-WITHOUT-SPEC`, `SPEC-UNPARSED` (an id found outside the known item
shapes; it is still counted, see "Linking checks to the spec"), `EC-IGNORED` (a format 1
runbook read with a spec that lists edge cases), and `SOURCE-SHAPE` and `SOURCE-MISSING` (see
"Provenance"), and `SHIP-PARTS-UNCHECKED` (a `runbook: 2` file with ship parts and no `kind`, see
"Ship runbooks").

## The partner kit: `alpaca runbook kit`

```
alpaca runbook kit [--out <dir>] [--zip]
```

A partner who has none of Alpaca writes a runbook with the partner kit: a folder,
`alpaca-runbook-kit-v<format>`, that they hand to their own agent (Claude Code, Codex, Cursor,
any agent that reads `AGENTS.md`, or a chat model). The verb builds it from this product's own
files, so the kit cannot drift from what `alpaca runbook check` and `alpaca intake` take:

| kit file | what it is |
|---|---|
| `check_runbook.py` | this checker in one file: `alpaca/runbook.py` with the verdict contract and the OpenSpec change reader of `alpaca/intake.py` inlined, Python 3.9 or later and PyYAML; the same options, messages, codes and exit status as `alpaca runbook check`, plus 65 when PyYAML is missing. Only the reading side is carried: `evaluate`, `next_attempt` and `respond`, which run checks and decide the next step, and `bar_parts`, which writes the bar of an intake row, stay out |
| `runbook.schema.json` | a JSON Schema (draft 2020-12) generated from the field tables of `alpaca/runbook.py`, for editors and other tools, with a definition per part of the research block; a block that names a defaults file is checked for its shape only, since the file may give any field; the checker stays the authority |
| `FORMAT.md` | this document, rewritten for the partner: product paths removed, intake marked as our side |
| `AGENTS.md`, `CLAUDE.md`, `.claude/skills/runbook-forge/SKILL.md` | the steps of `/alpaca-runbook-forge` for any agent, and as a Claude Code skill, the research step (step 4a) with its worked research runbook included |
| `README.md` | one page for the person: what to hand the agent, how to check, what to send back |
| `templates/`, `example/` | an empty runbook, a spec template, a note on OpenSpec; the worked example of `templates/runbook-example/` |
| `VERSION`, `LICENSE`, `SHA256SUMS` | the format version, the research format (`research_format: 3`) and the product commit, the MIT license, a checksum of every file |

`--out` is the folder the kit folder goes in (default: the current folder). A kit folder built
earlier is replaced; anything else in its place is refused (BLOCKED). `--zip` also writes
`<kit>.zip`, the same bytes on every build of the same sources. Before it answers PASS the verb
runs the built `check_runbook.py` on the example and the template, the way a partner runs it.

The kit-only texts live in `templates/runbook-kit/` (its `ABOUT.md` maps every kit file to its
source). When this document or the forge skill changes so that a rule of the builder
(`alpaca/runbook_kit.py`) no longer fits, the build stops and names the rule, instead of
shipping a half rewritten text. Exit status: 0 PASS, 1 FAIL (the kit could not be built from
these sources), 2 BLOCKED, 64 usage error.

A partner sends back the runbook, its spec and its plugin scripts, in their own folder layout.
`alpaca intake` takes a runbook only from inside the project, so copy the delivered folder into
the project first (for example to `partner/<name>/`, keeping its layout), run
`alpaca runbook check` on the copy, then intake it.

## What intake takes from a runbook

`alpaca intake <spec> <runbook>` (`docs/intake.md`) builds on pieces Alpaca already has, and the
runbook fields line up with them:

- Each stage `id` becomes a profile stage (`alpaca/profile.py`, `stages()`), so
  `alpaca task add --stage` and the hub can name it.
- Each stage with a `run` gives one task contract (`alpaca/taskcontract.py`): `inputs` gives the
  input lines, `outputs` the expected lines, the checks (with their thresholds) the done bar, and
  `fails` the fail cases. A stage that is only an owner gate gives the owner's decision alone,
  which is why it may not list checks.
- Each covered spec item becomes one checklist row whose evidence is the checks that cover it; a
  row covered only by an owner gate is discharged by review, the others by a run.
- `alpaca.runbook.bar_parts` gives the bar each check, fail case and owner gate sets (knob values
  put in, the knob named, owner-only marked), each stage's place in the run order (a recovery
  stage has none) and the `source` values, so a row states what shows its item. For a research
  runbook, called with the expanded block (`block=`), it also gives the bar of every metric
  (`metric:<id>`), repair case (`case:<id>`), the old suite (`research:pass-to-pass`) and part
  (`research:<part>`), each with no stage, since the research engine runs it.
- A research runbook's block is the pack-level contract; `alpaca/research/contract.py` adds a
  task's override as a fourth layer and freezes the contract when a run starts.
- Owner gates become decisions the owner records; they are never moved by an agent.

## Where the pieces come from

The format carries what Alpaca already had instead of adding parallel ideas: the four verdicts
and their exit codes (`alpaca/gates/verdict.py`), the acceptance-script contract of
`contracts/README.md` (for plugin checks), the stage list of the domain profile, the four parts
of a task contract, and the refusal of an empty population from the checklist engine
(`docs/spec-shape.md`).
