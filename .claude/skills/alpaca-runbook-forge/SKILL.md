---
name: alpaca-runbook-forge
description: >-
  Write an Alpaca runbook from a spec. Use when a spec-kit spec.md (FR-nnn, SC-nnn) or an
  OpenSpec spec (### Requirement / #### Scenario) exists and the work needs its executable plan:
  stages, commands, pass checks, knobs, retry rules and owner gates. Reads the spec and the repo,
  asks the person only for what they leave out, writes runbook.yaml, and runs
  `alpaca runbook check` until every success criterion or scenario is covered. Triggers on
  "/alpaca-runbook-forge", "forge a runbook", "write the runbook for this spec", "turn this spec
  into a runbook".
---

# alpaca-runbook-forge

The runbook forge. It turns a spec into a runbook in the format of `docs/runbook-format.md`,
with every success criterion (spec-kit `SC-nnn`) or scenario (OpenSpec
`<requirement>/<scenario>`) linked to a check or an owner gate. It asks the person as little as
possible: the spec and the repository answer most questions, and a question is only asked when
neither does.

## Two modes

- **Quiet mode**: the spec is complete and no interview exists, for example a spec a partner
  sends. Follow the steps below as they are.
- **Interview mode**: the operator was interviewed and signed the result off, so
  `input/interview/` holds a signed file (`signed-<stamp>-<sha12>.md`) and
  `alpaca interview status` (it only reads) shows the sign-off as signed, not stale. A stale
  sign-off goes back to the interview (`/alpaca-interview`) before any runbook is written. In
  interview mode the steps below change in four places:
  - Step 2: take the thresholds, failures, never, owner gates, rollback, knobs and commands from
    the signed slots first, then from the spec and the repository.
  - Step 3: ask only for what the signed slots still lack (a waived slot is not asked again), in
    rounds of up to 4 questions through the interactive question tool, each with 2 to 4 options
    and the recommended default first; build the next round from the answers.
  - Step 4: write `runbook: 2`. Put `source: interview:<slot>` on each knob, check, fail case and
    owner gate whose value came from a slot (`source: spec:<item id>` when it came from the spec,
    `source: default` for a default you chose). Each failure becomes a fail case with `detect` (a
    check of a generic type that passes when the failure has happened) and `then` (`retry`,
    `stop`, `ask-owner`, or `{run: <stage id>}` for a stage marked `recovery: true` that fixes
    the cause before the stage runs again). Each "never" becomes a check listed in the retry
    block's `stop_on`, or a `regex-in-file` check with `absent: true`.
  - Step 5: every edge case (`EC-nnn`) needs cover like a success criterion: the check or the
    fail case that shows it lists it in `covers`.

## Before you start

- You need a spec. If the person only has raw notes, write the spec first (spec-kit for a new
  thing, OpenSpec for a change to something that exists; on the Alpaca side, the interview
  `/alpaca-interview` comes before it), then come back here.
- Read `docs/runbook-format.md` once. Keep `templates/runbook-example/` open as the model of a
  finished runbook.
- Forging writes a plan. Do not run the stages, and do not run any `alpaca` verb that writes the
  record. The only verb this skill runs is `alpaca runbook check`, which is read-only.

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
them, but they do not block. A `SPEC-UNPARSED` warning is an `SC` or `FR` id written in a shape
the checker does not know; it still counts, and the line it names is worth rewriting as
`- **SC-001**: ...` in the spec.

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

- stages in run order; a stage that runs a command has `needs`, `run`, `inputs`, `outputs` and
  at least one check; a stage that is only an owner gate has `owner_gate` and no `run` and no
  `checks` (it runs nothing, so the checker refuses checks there with `CHECKS-WITHOUT-RUN`; a
  check that must follow the approval goes in the stage whose `run` produces what it reads);
- every check that shows a spec item lists it in `covers`;
- thresholds from the spec as `owner_only` knobs, and the knobs a retry may move with a range;
- a `retry` block only where the person agreed to retries, with `on_fail`, `stop_on` and `move`;
- `fails` for the known failure modes the person or the repo mentioned;
- `owner_gate` on every irreversible step, with `approve` in plain words and the `evidence` the
  owner reads;
- a comment on every value you choose that neither the spec nor the person gave, only for a
  setting that is not a pass bar (a timeout, a port, an attempt count, a knob range):
  `# default chosen by the agent: <why>`.

Set `spec:` to the spec path so a later check needs no flag.

## Step 5: check until it passes

From the same folder:

```
alpaca runbook check runbook.yaml --spec spec.md
```

Fix every `ERROR` line and run it again. For a `SPEC-UNCOVERED` item: add the check that shows
it, or, when only a person can judge it, add it to an owner gate's `covers`. Never add an item to
a `covers` list of a check that does not show it just to make the check pass. Stop when the last
line is `GATE alpaca-runbook-check: PASS`.

## Step 6: report

Tell the person, in a few lines:

- the runbook path and the check result (the `GATE` line);
- the coverage: each item and the checks or gates that cover it (`--json` prints it);
- the questions you asked, their answers, and the defaults you used;
- the warnings left (`FR-UNCOVERED`, `SPEC-CLARIFY`) and what would clear them.

## Never

- Never invent a threshold the spec does not give; ask, with a default the person may accept,
  and with no one to ask leave it to an owner gate.
- Never let a retry move an `owner_only` knob or loosen a threshold.
- Never mark an item covered by a check that does not show it.
- Never put a secret in a runbook; name the variable or the file that holds it.
