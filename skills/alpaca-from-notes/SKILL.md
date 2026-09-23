---
name: alpaca-from-notes
description: >-
  Take raw notes to checked work in one guided flow: pick spec-kit (a new thing) or OpenSpec (a
  change to something that already has specs), drive that tool to a spec, forge the runbook, and
  run `alpaca intake` so the op gets its checklist rows, task contracts and profile. Triggers on
  "/alpaca-from-notes", "start from these notes", "turn my notes into work", "here is an idea,
  set it up", or raw notes pasted with a request to start.
---

# alpaca-from-notes

The one entry point from raw notes to intake. It chains tools Alpaca already has; it writes no
spec, runbook or row by hand. `alpaca start` is its helper: it picks the kit, prepares the
project and prints the same steps as below. `docs/intake.md` is the reference.

The Alpaca skills live in `skills/`. Claude Code offers a project skill as a `/command` when its
folder is under `.claude/skills/`; the kit installs put the `/speckit-*` and `/opsx:*` ones there.
For `skills/alpaca-runbook-forge/SKILL.md` and this file, read the file and follow it, or copy the
folder into `.claude/skills/` first.

```
raw notes -> spec (spec-kit or OpenSpec) -> runbook (alpaca-runbook-forge) -> alpaca intake -> rows, tasks, profile
```

## Step 0: save the notes and let Alpaca pick

Save the notes the person gave as they are (for example `notes/<short-name>.md`; keep their
words). Then, from the project root:

```
bin/alpaca start notes/<short-name>.md
```

It prints the kit, the reason, and the steps. The rule:

- no spec in the project yet: **spec-kit**, a new thing;
- a spec already there (`specs/*/spec.md` or `openspec/specs/`), or the project records OpenSpec:
  **OpenSpec**, a change.

Tell the person the pick and the reason in one line. If they want the other kit, add
`--kit spec-kit` or `--kit openspec`. Do not ask when they have not objected.

## Step 1: prepare

```
bin/alpaca start notes/<short-name>.md [--kit <kit>] --prepare
```

This installs the kit from the vendored copies (offline) and records the choice. On the first
change to a spec-kit project it also moves the project to OpenSpec: each `specs/*/spec.md` is
written as `openspec/specs/<name>/spec.md`, one requirement per `SC-nnn` and `FR-nnn`, each
scenario named by its id, so the runbook still covers the same ids and intake keeps every row.
If Claude Code does not show the new `/speckit-*` or `/opsx:*` commands yet, restart the session
once.

## Step 2a: spec-kit, a new thing

1. `/speckit-specify <the notes>`: writes `specs/<NNN-name>/spec.md` with user stories,
   functional requirements (`FR-nnn`) and success criteria (`SC-nnn`).
2. `/speckit-clarify`: answer its questions until no `[NEEDS CLARIFICATION]` marker is left.
   Success criteria must be measurable; a number belongs in the criterion, not in a later chat.
3. The spec is `specs/<NNN-name>/spec.md`.

## Step 2b: OpenSpec, a change

1. `/opsx:propose <the notes>`: writes `openspec/changes/<id>/` with `proposal.md`, `tasks.md` and
   delta specs (`## ADDED`, `## MODIFIED`, `## REMOVED Requirements`).
2. `bin/openspec validate <id> --strict` must pass.
3. When a requirement came from a spec-kit criterion (`### Requirement: SC-002`), modify it under
   that same name and keep the scenario name starting with the id (`#### Scenario: SC-002`), so the
   row it changes is the row that was there.
4. The spec for the next steps is the change folder `openspec/changes/<id>`.

## Step 3: the runbook

Run the runbook forge skill (`skills/alpaca-runbook-forge/SKILL.md`) with the spec from step 2. For a new
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
success criterion or scenario (`+` added, `=` kept, `~` superseded, `-` withdrawn), one task per
stage and per owner gate with its contract, and the profile. Then run it for real and show the
board: `bin/alpaca board show --op <op>` and `bin/alpaca task list --op <op>`.

## Step 5: after a change is built

When the change's rows are proven, `/opsx:archive <id>` folds the delta into the living specs.
Run `bin/alpaca intake openspec/specs <runbook.yaml>` once more: it must say "nothing to change".

## Never

- Never write rows, tasks or contracts by hand; intake is the one writer for them.
- Never edit an intake item file under `.alpaca/intake/`; a spec change is a new intake.
- Never move an owner gate, and never approve one; that stays with the owner at every level.
- Never pick the kit against the person's stated choice.
