---
name: alpaca-runbook-forge
description: >-
  Write an Alpaca runbook from a spec. Use when a spec-kit spec.md (FR-nnn, SC-nnn) or an
  OpenSpec spec (### Requirement / #### Scenario) exists and the work needs its executable plan:
  stages, commands, pass checks, knobs, retry rules and owner gates. Reads the spec and the repo,
  asks the person only for what they leave out, writes runbook.yaml, and runs
  `alpaca runbook check` until every success criterion, edge case or scenario is covered. For work
  measured by a number or proved by a failing test it writes a research runbook (`runbook: 3`, a
  `research` block) and never a target the spec does not state. Triggers on
  "/alpaca-runbook-forge", "forge a runbook", "write the runbook for this spec", "turn this spec
  into a runbook".
---

# alpaca-runbook-forge

The runbook forge. It turns a spec into a runbook in the format of `docs/runbook-format.md`
(`runbook: 2`), with every success criterion (spec-kit `SC-nnn`), edge case (spec-kit `EC-nnn`)
or scenario (OpenSpec `<requirement>/<scenario>`) linked to a check, a fail case or an owner
gate. It asks the person as little as possible: the spec and the repository answer most
questions, and a question is only asked when neither does. When agents are to improve a measured
number in a loop, or fix reported bugs each proved by a failing test, the runbook is a research
runbook (`runbook: 3`) with a `research` block: step 4a says how to write it.

## Two modes

- **Quiet mode**: the spec is complete and no interview exists, for example a spec a partner
  sends. Follow the steps below as they are. The runbook is still `runbook: 2`, with `source`
  where you know it (`spec:<item id>`, or `default` for a default you chose).
- **Interview mode**: the operator was interviewed and signed the result off, so
  `input/interview/` holds a signed file (`signed-<stamp>-<sha12>.md`) and
  `alpaca interview status` (it only reads) shows the sign-off as signed, not stale. A stale
  sign-off goes back to the interview (`/alpaca-interview`) before any runbook is written. In
  interview mode the steps below change in four places:
  - Step 2: take the thresholds, failures, never, owner gates, rollback, knobs and commands from
    the signed slots first, then from the spec and the repository.
  - Step 3: ask only for what the signed slots still lack (a waived slot is not asked again), in
    rounds of up to 4 questions through the interactive question tool, each with 2 to 4 options
    and the recommended default first; build the next round from the answers. Record each answer
    in the slot it fills, as the interview does:
    `alpaca interview set <slot> --answered --value "<the answer>" --source round:<n>/q<n>`
    (number these rounds after the interview's last one), so the signed brief holds every fact
    the runbook depends on. That makes the sign-off stale: show `alpaca interview readback`, and
    the operator signs off again (`/alpaca-interview`, step 3) before the runbook takes the
    answers as `source: interview:<slot>`.
  - Step 4: put `source: interview:<slot>` on each knob, check, fail case and owner gate whose
    value came from a slot (`source: spec:<item id>` when it came from the spec,
    `note:input/notes/<file>` when a raw note states it, `owner:<decision ref>` when the owner
    decided it, `source: default` for a default you chose). Each failure in the signed slots
    becomes a fail case with `detect` and `then` (step 4 says how). Each "never" becomes a check
    listed in the retry block's `stop_on`, or a `regex-in-file` check with `absent: true`.
  - Step 5: the edge-cases slot became `EC-nnn` items of the spec; each needs cover like a
    success criterion.

## Before you start

- You need a spec. If the person only has raw notes, write the spec first (spec-kit for a new
  thing, OpenSpec for a change to something that exists; on the Alpaca side, the interview
  `/alpaca-interview` comes before it), then come back here.
- Read `docs/runbook-format.md` once. Keep `templates/runbook-example/` open as the model of a
  finished runbook.
- Forging writes a plan. Do not run the stages, and do not run any `alpaca` verb that writes the
  record. The only verb this skill runs is `alpaca runbook check`, which is read-only.
- Interview mode adds one exception: the answers of the forge's own rounds go into the interview
  log with `alpaca interview set` (a project file, not the record), and `alpaca interview status`
  and `alpaca interview readback` show them. The sign-off stays with the operator.

## Step 1: list what must be covered

Write a first draft that holds only the top-level fields and one placeholder stage, next to the
spec, then let the checker list the items for you. Run it from the folder that holds both files:
a relative path is read from the folder you run the command in.

```
alpaca runbook check runbook.yaml --spec spec.md
```

Every `SPEC-UNCOVERED` line is one item the runbook must cover. Copy them into a gap table, one
row per item:

| item | what shows it | check type | what the spec gives | what is missing |
|---|---|---|---|---|

`FR-UNCOVERED` warnings are functional requirements: cover them where a check already shows
them, but they do not block. A `SPEC-UNPARSED` warning is an `SC`, `EC` or `FR` id written in a
shape the checker does not know; it still counts, and the line it names is worth rewriting as
`- **SC-001**: ...` in the spec.

Edge cases are items too: every `EC-nnn` under the spec's Edge Cases heading is a row of the
table. An edge case bullet with no id is an `EC-UNNUMBERED` error: number it in the spec
(`- **EC-004**: ...`, the next free number) and never renumber an edge case that has an id,
because the id keys its checklist row.

## Step 2: fill the table from the spec and the repo

For each row, take every answer you can find before asking anyone:

- **Thresholds.** A number in the criterion ("under 50 ms", "95% of days", "zero lost") is the
  threshold. Put it in an `owner_only` knob and compare against `${KNOB}`, with a comment naming
  the item it comes from. A value comes from the spec first, then the person's answers. Never
  choose a threshold yourself: when neither gives one, ask; with no one to ask, cover the item
  with an owner gate whose `approve` says what must be decided.
- **Commands.** Look for the build, test and run commands in the repository: `Makefile`,
  `package.json` scripts, `pyproject.toml`, `README`, CI files, existing scripts. A command found
  there is used as is.
- **Evidence files.** A test runner's report (JUnit XML, a JSON summary) or a file the command
  writes becomes the check `path`.
- **Check type.** Prefer the generic types: `exit-code` for "it runs", `file-exists` for "it
  produced X", `regex-in-file` for a log or report line, `json-field` for a number against a
  threshold. Use a `plugin` only when none of these can say it; write the script to the plugin
  check contract (exit 0 PASS, 1 FAIL, 2 BLOCKED, 3 PAUSED; last stdout line is the reason) and
  `chmod +x` it.
- **Owner gates.** Any step that deploys, publishes, pushes to a shared remote, deletes outside
  the project or spends money is an owner gate, always, without asking. A criterion only a person
  can judge ("a new member can start it in 10 minutes") is covered by an owner gate's `covers`.

## Step 3: ask only for the gaps

Collect the cells still empty after step 2 and ask them in ONE message, numbered, each with the
default you will use if the person does not care. Ask only about these five kinds of thing:

1. **Commands** the repo does not show (how to start the service, how to load-test it).
2. **Thresholds** the spec states in words but not in numbers ("fast", "most of the time").
3. **Knobs**: which setting may move when a check fails, and its allowed range.
4. **Retry limits**: how many attempts, and which failures should stop at once.
5. **Owner gates**: who approves, and what they look at, when the spec does not say.

Do not ask what the spec or the repo already answers, do not ask about YAML fields, and do not
ask one question per message. If the person answers "use the defaults", use them and say so in
the runbook comments.

Example of a good batch (the spec gave the thresholds; the repo had no load tool):

```
I read spec.md (4 success criteria) and the repo. Five things are not in either:
1. How do I start the service for the load test? Default: `python3 -m app --port 8080`.
2. SC-002 says "under 50 ms at p95": which endpoint? Default: GET /<code>.
3. If the latency check fails, may I raise WORKERS? Default: yes, by 2, range 1..8.
4. How many load-test attempts? Default: 3, and stop at once on any 5xx.
5. Who approves the production release? Default: the service owner, after reading out/load.json.
```

## Step 4: write the runbook

Write `runbook.yaml` next to the spec (or in the domain folder), following the format:

- `runbook: 2` at the top (`runbook: 3` for a research runbook, whose block step 4a writes);
- stages in run order; a stage that runs a command has `needs`, `run`, `inputs`, `outputs` and
  at least one check; a stage that is only an owner gate has `owner_gate` and no `run` and no
  `checks` (it runs nothing, so the checker refuses checks there with `CHECKS-WITHOUT-RUN`; a
  check that must follow the approval goes in the stage whose `run` produces what it reads);
- every check that shows a spec item (a success criterion, an edge case) lists it in `covers`;
- thresholds from the spec as `owner_only` knobs, and the knobs a retry may move with a range;
- a `retry` block only where the person agreed to retries, with `on_fail`, `stop_on` and `move`;
- `fails` for the known failure modes the person or the repo mentioned: each has `id` and
  `when`, and, when you know how the failure shows, a `detect` (a check of type `exit-code`,
  `file-exists`, `regex-in-file`, `json-field` or `plugin`, without `id`, `covers` or `source`,
  that passes when the failure happened) with a `then`: `retry` (another attempt within the
  stage's `retry` limits), `stop` (the stage fails at once), `ask-owner` (the stage pauses for a
  person), or `{run: <stage id>}`. A fail case that detects and answers an edge case lists it in
  its own `covers` (for example "the port is already taken" is covered by the fail case that
  recognizes a busy port);
- a recovery stage for each `{run: <stage id>}`: a stage with `recovery: true`, a `run` and at
  least one check, no `needs`, no `owner_gate`, and no other stage lists it in `needs`. It runs
  only when a fail case sends to it; after it passes the failed stage runs again, and that run
  counts as an attempt, so the failed stage needs `retry.max_attempts` of 2 or more. A fail case
  of a recovery stage never sends to a recovery stage, itself or another. A recovery stage is
  never an owner gate: an undo or restore a person must approve first is a fail case with
  `then: ask-owner` plus a stage with `owner_gate` (in the run order, or in a rollback runbook of
  its own);
- `source` on a knob, a check, a fail case and an owner gate when you know where the value comes
  from: `spec:<item id>`, `note:input/notes/<file>`, `interview:<slot id>`, `owner:<decision
  ref>`, or `default` for a default you chose (any other text is a `SOURCE-SHAPE` warning);
- `owner_gate` on every irreversible step, with `approve` in plain words and the `evidence` the
  owner reads;
- a comment on every value you choose that neither the spec nor the person gave, only for a
  setting that is not a pass bar (a timeout, a port, an attempt count, a knob range):
  `# default chosen by the agent: <why>`.

Set `spec:` to the spec path so a later check needs no flag.

## Step 4a: the research block (research runbooks only)

Some specs describe work that agents improve in a loop: a number to push down or up ("at most
18000 comparisons", "median wall time at most 120 ms"), or reported bugs, each proved by a test
that fails before the fix. For such a spec write a research runbook: `runbook: 3` at the top
instead of `runbook: 2`, `spec:` naming the spec (required here), the stages of step 4, and a
`research` block. The section "The research block" of `docs/runbook-format.md` lists every field
and code. Any other spec keeps `runbook: 2` and skips this step. In interview mode the research
slots (`research-mode` to `research-repair`) hold the operator's signed answers; in quiet mode
the spec and the repository are all you have.

Write the block field by field:

- **Mode and repository.** `repair` when the work is a set of bug reports, `improve` otherwise.
  The `research-mode` slot also names the repository and the ref the run works on when they are
  not this project at its current commit: write them as `repo.path` and `repo.ref`. Leave `repo`
  out only for this project at its current commit, which the engine fills (`.` and `HEAD`).
- **Defaults.** When the runbook folder holds the pack's defaults file (a YAML file with a
  `research` mapping and an optional `pack` name, such as `pack.yaml`), name it in `defaults:`
  and write only what the runbook adds or changes; the file gives the rest. A list the runbook
  writes replaces the file's list, except `metrics`, `categories`, `proposal`, `coupled` and
  `repair.cases`, which merge item by item on `id` (`field` for `proposal`), so to add a
  protected path write the file's paths as well. With no defaults file, the runbook holds the
  whole block.
- **Metrics** (improve). One metric per success criterion that states a measured number. Its
  `covers` names that criterion, plus an edge case the metric shows (a run that prints no result
  gets no score); `unit` and `direction` come from the wording ("at most" and "under" are
  `lower`, "at least" is `higher`); `command` is the repository's measuring command; `parser` is
  `markers` when the command prints `METRIC:` lines, else `json-field` with the `path` and `field`
  of the file it writes; `kind` is `deterministic` when the same tree always gives the same
  number, else `noisy`.
- **Targets.** Write a `target` only when the criterion states the number, always with
  `source: spec:<item id>` of that criterion, and quote the criterion in a comment on the line.
  The check takes a target whose source is `spec:<item id>` or `owner:<decision ref>` and
  refuses any other (`TARGET-SOURCE`), but it cannot see whether the spec states the number, so
  that part is yours. With no number in the criterion, write no target: ask for one, and with no
  one to ask, leave the metric without a target, or cover the criterion with an owner gate whose
  `approve` says what must be decided. `owner:<decision ref>` is only for a target the owner
  decided and recorded.
- **Bar values you never pick.** A noisy metric's `repeats`, `min_ok` and `margin`,
  `stop.rounds`, `stop.slots` and the `budget` come from the spec, a signed slot or the pack's
  defaults file; otherwise ask, as for a threshold. A noisy metric without its three values
  stops you with a question. A value taken from a signed slot gets a comment line above it
  naming the slot (`# min_ok and margin: interview:research-noise`), because the `source` of the
  metric or part that holds it names the spec item it covers.
- **Accept rule.** `deficit-pareto` when some metric has a target, `champion` for one metric
  with no target, `repair` in repair mode. Write `after_targets` (`stop` or `pareto`) only when a
  metric has a target, from the spec or the `research-accept` slot.
- **Covers and source on the parts.** List on each part the SC or EC it shows, with
  `source: spec:<item id>` of that item: on `stop` the criterion that says when a run stops; on
  `paths` an edge case that refuses an edit outside the editable paths or to the tests; on
  `repair.pass_to_pass` the criterion that old tests keep passing; on each repair case the SC and
  EC of its bug; on `accept`, `budget`, `fingerprint`, `controls` and `holdout` the item that
  states their rule, when one does. A metric and a repair case always cover at least one required
  item (`COVERS-NONE`); a part that shows no item gets no `covers`.
- **Protected paths.** `paths.protected` always holds the runbook folder and the eval
  configuration the stages read (build files, test configuration), as rules from the repository
  root, in the runbook or in the defaults file it names. Name a test configuration file that the
  test tool loads from any folder, such as `conftest.py` for pytest, by its bare name, which
  matches it at any depth. `paths.editable` names only what an attempt may change.
- **Tools and stages.** `fingerprint.tools` lists every program the stages and metrics run. Every
  stage with a `run` has a `timeout`. Every stage that runs a command and is not a recovery stage
  is named once: in `gates` (the build and test stages, in run order), in `holdout.stages` (run on
  the final champion only) or as `repair.prepare`. An owner-gate stage (no `run`) and a recovery
  stage (`recovery: true`) are named in none of them (`STAGE-ROLE`). A gate stage's `needs` names
  only earlier gate stages or the prepare stage; a holdout stage's names only gate stages, earlier
  holdout stages or the prepare stage; the prepare stage has no `needs` (`GATES-ORDER`). A fail
  case answers `retry`, `stop` or `{run: <stage id>}`, never `ask-owner`: the gate chain never
  waits for a person.
- **Generated held-out data.** When a program generates the held-out data from a seed, put a
  `recipe.json` (`generator`, `option`, `visible`) under `holdout.paths`, ask the operator for a
  held-out seed drawn with at least 64 random bits, and never write that seed or a bound on it in
  the runbook, the spec, a test or any other file an attempt reads; state no number measured on the
  held-out data in such a file either. When the pack holds several contracts, each runbook lists the
  other contracts' held-out and hidden paths in `paths.hidden` (the research block of the runbook
  format).
- **Categories, controls and workdir.** `categories` (required in improve mode) come from the
  `research-proposal` slot, with any proposal fields the pack adds; with no one to ask, write
  them from the spec, since a category text is a setting that is not a bar. `controls` come from
  the `research-controls` slot: `noop_rounds` (at least 2) and `random`, with `mutate`, `syntax`
  and `seed` when `random` is true; without a signed answer, ask, as for a threshold. Write a
  metric's `workdir` only when its command must start somewhere other than the tree's copy of
  the runbook folder.
- **Repair cases.** One case per bug report: `id`, `bug_report` (the report the notes name, in
  the runbook folder), `base` (a 40-digit commit id, never a branch or tag), `fail_to_pass`
  (`origin: store` with the `tests` the supplied reproducer turns from fail to pass, or
  `origin: generate`) and `suspect_files`. `repair.pass_to_pass` is the old test suite, with its
  `min_tests` and `timeout`.
- **Settings that are not a bar.** A stage or metric `timeout`, `disk` or a category text you may
  choose, with `# default chosen by the agent: <why>` on the line. Write no `source: default` for
  them: the `source` of the metric, case or part that holds one stays the spec item.
- Leave out what the engine fills when neither the block nor its defaults file sets it:
  `runtime`, `verifier`, `audit` and `egress`, and `repo` for this project at its current commit.

Then check it as step 5 says: coverage now counts metrics (`metric:<id>`), repair cases
(`case:<id>`) and parts (`research:<part>`) as well as checks, fail cases and owner gates.

### A worked research runbook

This spec, `improve-spec.md` in the folder `packs/sortbench/` of the repository:

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

gives this `improve.yaml` next to it. The folder also holds the pack's defaults file
`pack.yaml`, which protects the runbook folder and `pyproject.toml` and sets `fingerprint.tools`
(`python3`), the budget, `disk`, the lessons file and the agent timeouts and retries, so the
runbook names it in `defaults:` and repeats none of them. The spec states the two targets
(SC-001, SC-002), the 5 repeats (SC-002) and the 5 rounds (SC-004); the signed slots gave the
rest, as the comments say.

```yaml
# Research contract, improve mode, forged from improve-spec.md.
runbook: 3
id: sortbench-improve
title: Fewer comparisons and less wall time for sortlib.sort
spec: improve-spec.md

stages:
  - id: build
    title: the package compiles
    run: python3 -m compileall -q ${WORKTREE}/src/sortlib
    timeout: 120  # default chosen by the agent: a compile of one package takes seconds
    checks:
      - {id: build-exit, type: exit-code}
  - id: unit
    title: the correctness tests, run from the contract copy
    needs: [build]
    run: >-
      python3 ${CONTRACT_DIR}/packs/sortbench/tests/run_tests.py
      --src ${WORKTREE}/src --json ${EVIDENCE_DIR}/unit.json
    timeout: 600  # default chosen by the agent: the suite runs in under a minute
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
      direction: lower  # SC-001: "at most"
      kind: deterministic
      timeout: 300  # default chosen by the agent: one pass over the fixed inputs
      target: 18000  # SC-001: "at most 18000 comparisons"
      covers: [SC-001, EC-002]
      source: spec:SC-001
    - id: wall_ms
      description: wall time of one benchmark pass
      command: >-
        python3 ${CONTRACT_DIR}/packs/sortbench/bench/measure.py --src ${WORKTREE}/src
        --metric wall_ms --json ${EVIDENCE_DIR}/wall-${REPEAT}.json
      parser: json-field
      path: ${EVIDENCE_DIR}/wall-${REPEAT}.json
      field: wall_ms
      unit: ms
      direction: lower  # SC-002: "at most"
      kind: noisy
      repeats: 5  # SC-002: "over 5 runs"
      # min_ok and margin: interview:research-noise
      min_ok: 3
      margin: 4
      timeout: 120  # default chosen by the agent: one pass takes about a second
      target: 120  # SC-002: "at most 120 ms"
      covers: [SC-002]
      source: spec:SC-002
  accept:
    rule: deficit-pareto
    after_targets: stop  # SC-004: "stops when both targets are met"
  paths:
    editable: [src/sortlib/]
    covers: [EC-003]
    source: spec:EC-003
  gates:
    - {stage: build}
    - {stage: unit}
  stop:
    rounds: 5  # SC-004: "after 5 rounds"
    # slots: interview:research-stop
    slots: 2
    covers: [SC-004]
    source: spec:SC-004
  # categories: interview:research-proposal
  categories:
    - {id: fewer-compares, text: change the algorithm or its cut-over points so it compares less}
    - {id: small-inputs, text: add or tune a path for short runs and small lists}
  # noop_rounds and random: interview:research-controls
  controls:
    noop_rounds: 2
    random: false
```

With no number in a criterion there is no target. Had SC-001 read

```markdown
- **SC-001**: The sort benchmark makes fewer comparisons on the fixed input set than the base tree.
```

the metric would be written without one, and `accept.rule` would stay `deficit-pareto`, since
`wall_ms` keeps its target:

```yaml
    - id: compares
      description: comparisons the sort makes on the fixed input set
      command: >-
        python3 ${CONTRACT_DIR}/packs/sortbench/bench/measure.py --src ${WORKTREE}/src
        --metric compares
      parser: markers
      unit: comparisons
      direction: lower  # SC-001: "fewer"
      kind: deterministic
      timeout: 300  # default chosen by the agent: one pass over the fixed inputs
      covers: [SC-001, EC-002]
      source: spec:SC-001
```

## Step 5: check until it passes

From the same folder:

```
alpaca runbook check runbook.yaml --spec spec.md
```

Fix every `ERROR` line and run it again. For a `SPEC-UNCOVERED` item: add the check that shows
it or the fail case that detects and answers it, or, when only a person can judge it, add it to
an owner gate's `covers`. Never add an item to
a `covers` list of a check that does not show it just to make the check pass. Stop when the last
line is `GATE alpaca-runbook-check: PASS`.

## Step 6: report

Tell the person, in a few lines:

- the runbook path and the check result (the `GATE` line);
- the coverage: each item and the checks, fail cases (`fail:<stage>/<id>`) or gates
  (`gate:<stage>`) that cover it, and in a research runbook the metrics (`metric:<id>`), repair
  cases (`case:<id>`) and parts (`research:<part>`) (`--json` prints it);
- in a research runbook, each target with the criterion that states it, and each bar value a
  signed slot gave;
- the questions you asked, their answers, and the defaults you used;
- the warnings left (`FR-UNCOVERED`, `SPEC-CLARIFY`, `SOURCE-SHAPE`, `SOURCE-MISSING`) and what
  would clear them.

## Never

- Never invent a threshold the spec does not give; ask, with a default the person may accept,
  and with no one to ask leave it to an owner gate.
- Never write a research `target` the spec does not state, and never pick a noisy metric's
  `repeats`, `min_ok` or `margin`, the rounds, the slots or the budget yourself; ask, and with no
  one to ask leave the metric without a target or the item to an owner gate.
- Never let a retry move an `owner_only` knob or loosen a threshold.
- Never mark an item covered by a check that does not show it.
- Never put a secret in a runbook; name the variable or the file that holds it.
