# Runbook format

A runbook is the executable plan of a domain. It lists the stages, the command each stage runs,
what each stage reads and writes, the checks that decide whether a stage passed, the knobs a
retry may move, the retry rules, and the steps only a person may approve (owner gates).

It sits in the middle of the path Alpaca follows from an idea to checked work:

```
raw notes -> spec (spec-kit or OpenSpec) -> runbook -> intake (rows, task contracts, profile) -> runs with sealed proof
```

The spec says what must hold. The runbook says how each part of that is shown, and it links
every success criterion or scenario of the spec to at least one check. Intake reads the runbook
and turns it into checklist rows and task contracts.

`alpaca runbook check <file> [--spec <path>]` reads a runbook, refuses a malformed one with a
message that names the field, and, with a spec, fails when any success criterion or scenario has
no check. The skill `skills/alpaca-runbook-forge/SKILL.md` writes a runbook from a spec and asks
the person only for what the spec leaves out.

A worked example lives in `templates/runbook-example/`: a spec-kit spec for a small link
shortener service (`spec.md`), its runbook (`runbook.yaml`) and one plugin check
(`checks/status_codes.py`). `alpaca runbook check templates/runbook-example/runbook.yaml` passes
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

## Top level

| field | required | meaning |
|---|---|---|
| `runbook` | yes | the format version; write `runbook: 1` |
| `id` | yes | a short name: lower-case letters, digits, `.`, `_`, `-` |
| `title` | yes | one line a person reads |
| `description` | no | what this runbook runs and why |
| `spec` | no | the spec this runbook covers, a path relative to the runbook folder; `alpaca runbook check` reads it when `--spec` is not given |
| `owner` | no | who approves the owner gates (a role, not a secret) |
| `knobs` | no | the named settings a stage reads and a retry may move |
| `stages` | yes | the stages, in run order; at least one |

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
| `checks` | yes when `run` is set | the pass checks; a stage that runs a command needs at least one |
| `retry` | no | the retry rule (below); without it a stage runs once |
| `fails` | no | known ways the stage fails: mappings with `id` and `when` |
| `owner_gate` | no | a step only a person may approve (below) |

A stage passes when every one of its checks passes. `needs` names only earlier stages, so the
order of the file is a valid run order and a cycle cannot be written.

An output mapping takes `path` (required) and `what` (a short description). A known failure
takes `id` and `when`, for example `{id: port-busy, when: "port 8080 is already taken"}`;
intake copies these into the task contract's fail cases.

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
| `script` | yes | the script, relative to the runbook folder; it must exist and be executable |
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
(`FR-UNCOVERED`), not a failure. `[NEEDS CLARIFICATION: ...]` markers left in the spec are
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
`## REMOVED Requirements` are not. Pass a folder (`openspec/specs`, or a change's `specs`
folder) to read every `<capability>/spec.md` in it; the items are then
`<capability>/<requirement>/<scenario>`, and the short form `<requirement>/<scenario>` is
accepted when only one capability has it (`COVERS-AMBIGUOUS` otherwise).

Headings and ids inside fenced code blocks and HTML comments are not items, in either format.

**Owner gates cover too.** Some criteria can only be judged by a person (for example "a new
team member can start the service in under 10 minutes"). Put those in the `covers` list of the
owner gate that judges them. Coverage then shows `gate:<stage id>` as the one covering it.

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

Example: raise the worker count by 2 while the latency check fails, at most three runs, and stop
at once if any response had the wrong status:

```yaml
retry:
  max_attempts: 3
  on_fail: [redirect-p95]
  stop_on: [redirect-codes]
  move: {knob: WORKERS, by: 2}
```

## Owner gates

An owner gate is a step only a person may approve. Nothing in Alpaca moves it on its own, at any
autodrive level.

| field | required | meaning |
|---|---|---|
| `approve` | yes | what the owner looks at and approves, in one or two sentences |
| `evidence` | no | the files the owner reads before approving |
| `covers` | no | the spec items this approval judges |

A stage with an `owner_gate` waits for the approval before its `run` starts. A stage may be only
a gate (no `run`, no checks), for example a release sign-off.

## `alpaca runbook check`

```
alpaca runbook check <file> [--spec <path>] [--no-files] [--json]
```

- `--spec` takes a spec-kit `spec.md`, an OpenSpec `spec.md` (main or delta), or an OpenSpec
  folder. Without it the runbook's own `spec:` field is used; without either, only the format is
  checked (and `covers` lists get a `COVERS-WITHOUT-SPEC` warning).
- `--no-files` skips the plugin script look-ups, for a runbook written before its scripts.
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
| `CHECK-TYPE-UNKNOWN` | a check `type` outside the list above |
| `REGEX-INVALID` | a `pattern` does not compile |
| `OP-UNKNOWN` | a `json-field` `op` outside the list above |
| `VALUE-NOT-NUMBER` | an ordering op with a threshold that is not a number |
| `RANGE` | a number outside its allowed range (or `min` above `max`, or a `by` of 0) |
| `PATH-ESCAPES` | an absolute path, or one that leaves the runbook folder |
| `PLUGIN-MISSING` | a plugin script does not exist |
| `PLUGIN-NOT-EXECUTABLE` | a plugin script is not executable |
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
| `SPEC-UNCOVERED` | a success criterion or scenario is covered by no check or owner gate |
| `COVERS-UNKNOWN` | a `covers` entry names nothing in the spec |
| `COVERS-AMBIGUOUS` | a short OpenSpec key matches scenarios in more than one capability |

Warnings (`WARN <code> <where>: <message>`) do not change the verdict: `FR-UNCOVERED`,
`SPEC-CLARIFY`, `COVERS-WITHOUT-SPEC` and `SPEC-UNPARSED` (an id found outside the known item
shapes; it is still counted, see "Linking checks to the spec").

## What intake takes from a runbook

Intake builds on pieces Alpaca already has, and the runbook fields line up with them:

- Each stage `id` becomes a profile stage (`alpaca/profile.py`, `stages()`), so
  `alpaca task add --stage` and the hub can name it.
- Each stage gives one task contract (`alpaca/taskcontract.py`): `inputs` gives the input lines,
  `outputs` the expected lines, the checks (with their thresholds) the done bar, and `fails` the
  fail cases.
- Each covered spec item becomes one checklist row whose evidence is the checks that cover it; a
  row covered only by an owner gate is discharged by review, the others by a run.
- Owner gates become decisions the owner records; they are never moved by an agent.

## Where the pieces come from

The format carries what Alpaca already had instead of adding parallel ideas: the four verdicts
and their exit codes (`alpaca/gates/verdict.py`), the acceptance-script contract of
`contracts/README.md` (for plugin checks), the stage list of the domain profile, the four parts
of a task contract, and the refusal of an empty population from the checklist engine
(`docs/spec-shape.md`).
