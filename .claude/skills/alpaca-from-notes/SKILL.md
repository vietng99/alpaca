---
name: alpaca-from-notes
description: >-
  Take raw notes to checked work in one guided flow: keep the notes in the inbox, interview the
  operator until the notes add up to a signed brief, pick spec-kit (a new thing) or OpenSpec (a
  change to something that already has specs), drive that tool to a spec, forge the runbook, and
  run `alpaca intake` so the op gets its checklist rows, task contracts and profile. Triggers on
  "/alpaca-from-notes", "start from these notes", "turn my notes into work", "here is an idea,
  set it up", or raw notes pasted with a request to start.
---

# alpaca-from-notes

The one entry point from raw notes to intake. It chains tools Alpaca already has; it writes no
spec, runbook or row by hand. `alpaca start` is its helper: it picks the kit, prepares the
project and prints the same steps as below. `docs/intake.md` is the reference.

The Alpaca skills ship in `.claude/skills/` (this one, `alpaca-interview`, `alpaca-runbook-forge`,
`alpaca-first-chat`, `alpaca-onboard` and `alpaca-op`), so Claude Code offers each as a `/command` in any clone of the
project; the kit installs put the `/speckit-*` and `/opsx:*` ones next to them.

```
raw notes -> input/notes/ (alpaca note add) -> interview (/alpaca-interview, signed)
  -> spec (spec-kit or OpenSpec) -> runbook (alpaca-runbook-forge) -> alpaca intake -> rows, tasks, profile
```

## Step 0: keep the notes and let Alpaca pick

Keep every piece the person gave in the inbox, as it is, one piece per note (their words, never a
summary):

```
bin/alpaca note add --file <the file they gave>
bin/alpaca note add "<text they pasted>"
```

Each note lands in `input/notes/<stamp>-<sha12>.md` and is never rewritten. Then, from the
project root, with one of the notes (or the text itself):

```
bin/alpaca start input/notes/<stamp>-<sha12>.md
```

It prints the kit, the reason, and the steps. The rule:

- no spec in the project yet: **spec-kit**, a new thing;
- a spec already there (`specs/*/spec.md` or `openspec/specs/`), or the project records OpenSpec:
  **OpenSpec**, a change.

Tell the person the pick and the reason in one line. If they want the other kit, add
`--kit spec-kit` or `--kit openspec`. Do not ask when they have not objected.

`alpaca start --json` also says `interview`: `needed` (no signed interview in
`input/interview/`), `signed`, or `stale` (the log, the slot map or the inbox changed after the
last sign-off, or the notes given to `start` are not in the inbox yet).

## Step 0b: the interview

Raw notes rarely say all a runbook needs. Unless `interview` is `signed`, run `/alpaca-interview`
(`.claude/skills/alpaca-interview/SKILL.md`) now: it settles what the notes answer, asks rounds
of questions for the rest, reads the answers back and, on the person's approve, runs
`bin/alpaca interview signoff --by "<name>"`. That writes the signed brief
`input/interview/signed-<stamp>-<sha12>.md`, which the spec step takes instead of the raw notes.
When `interview` is `stale`, run the interview again from its readback and sign off again. Go on
only when `bin/alpaca interview status` shows the sign-off as signed.

## Step 1: prepare

```
bin/alpaca start input/notes/<stamp>-<sha12>.md [--kit <kit>] --prepare
```

This installs the kit from the vendored copies (offline) and records the choice. On the first
change to a spec-kit project it also moves the project to OpenSpec: each `specs/*/spec.md` is
written as `openspec/specs/<name>/spec.md`, one requirement per `SC-nnn`, `EC-nnn` and `FR-nnn`, each
scenario named by its id, so the runbook still covers the same ids and intake keeps every row.
If Claude Code does not show the new `/speckit-*` or `/opsx:*` commands yet, restart the session
once.

## Step 2a: spec-kit, a new thing

1. `/speckit-specify <the signed brief>`: writes `specs/<NNN-name>/spec.md` with user stories,
   functional requirements (`FR-nnn`) and success criteria (`SC-nnn`). The `done-bar` and
   `thresholds` slots become success criteria, and the `edge-cases` slot becomes edge cases
   numbered `- **EC-001**: ...`. spec-kit's own template does not number edge cases (it writes
   them as questions): that rule is Alpaca's, so number each one yourself in
   `spec.md` as `- **EC-001**: ...`, `EC-002` and on (never renumber one that has an id).
   `alpaca runbook check` refuses an edge case bullet with no id (`EC-UNNUMBERED`).
2. `/speckit-clarify`: answer its questions until no `[NEEDS CLARIFICATION]` marker is left.
   Success criteria must be measurable; a number belongs in the criterion, not in a later chat.
3. The spec is `specs/<NNN-name>/spec.md`.

## Step 2b: OpenSpec, a change

1. `/opsx:propose <the signed brief>`: writes `openspec/changes/<id>/` with `proposal.md`, `tasks.md` and
   delta specs (`## ADDED`, `## MODIFIED`, `## REMOVED Requirements`).
2. `bin/openspec validate <id> --strict` must pass.
3. When a requirement came from a spec-kit criterion (`### Requirement: SC-002`), modify it under
   that same name and keep the scenario name starting with the id (`#### Scenario: SC-002`), so the
   row it changes is the row that was there.
4. The spec for the next steps is the change folder `openspec/changes/<id>`.
5. One change at a time: intake applies one change folder to the living specs. If an earlier
   change is still under `openspec/changes/` (not archived), archive it first (step 5) and take
   the living specs in, then propose this one. Do not drop the earlier change's coverage from the
   runbook to get past the check: intake would withdraw its rows.

## Step 3: the runbook

Run the runbook forge skill (`/alpaca-runbook-forge`, `.claude/skills/alpaca-runbook-forge/SKILL.md`) with the spec from step 2. With a
signed interview it works in interview mode: it takes the failures, what must never happen, the
owner gates, rollback, knobs and commands from the signed slots. For a new
thing it writes `runbook.yaml` next to the spec; for a change it updates the existing runbook so
every new or changed scenario has a check and nothing covers a removed one. A threshold that the
change moves (a latency bar) is an `owner_only` knob: change the knob's `default`, not the check.
It stops when this passes:

```
bin/alpaca runbook check <runbook.yaml> --spec <spec from step 2>
```

For a change folder the check reads the living specs with the change applied, the same way
intake does.

## Step 4: intake

An op must be open. If `bin/alpaca op list` shows none, open one with the person's one-line
intent: `bin/alpaca op new "<intent>" --done-when "<bar>"`.

```
bin/alpaca intake <spec> <runbook.yaml> --dry-run
bin/alpaca intake <spec> <runbook.yaml>
```

Show the person the dry run first when this is the first intake of the op. It lists one row per
success criterion, edge case or scenario with its bar (`+` added, `=` kept, `~` superseded, `-`
withdrawn), one task per stage and per owner gate with its contract (a recovery stage has none:
the fail case line that sends to it names it), and the profile. Then run it for real and show the
board: `bin/alpaca board show --op <op>` and `bin/alpaca task list --op <op>`.

## Step 5: after a change is built

When the change's rows are proven, `/opsx:archive <id>` folds the delta into the living specs.
Run `bin/alpaca intake openspec/specs <runbook.yaml>` once more: it must say "nothing to change".

## Never

- Never write rows, tasks or contracts by hand; intake is the one writer for them.
- Never edit an intake item file under `.alpaca/intake/`; a spec change is a new intake.
- Never change the runbook `id:` after the first intake; intake refuses it. A new id belongs in a
  new runbook file.
- Never move an owner gate, and never approve one; that stays with the owner at every level.
- Never pick the kit against the person's stated choice.
- Never write the spec from raw notes while the interview is not signed; never sign it off for
  the person.
