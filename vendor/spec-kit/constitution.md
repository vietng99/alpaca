# Project Constitution

This project runs under Alpaca. Its rules already exist and live in the Alpaca files below. This
file points at them; it is not a second rulebook and adds no principles of its own.

## Where the rules live

- `CLAUDE.md`: the boot block and the rules that hold in every session (sealed proof per task,
  a pointer behind every claim, human decisions for ship and irreversible actions, writes kept
  in the project root, the human-owned authority surface).
- `doctrine/`: the doctrine leaves, registered in `doctrine/INDEX.md`, with the core set named in
  `doctrine/CORE-CARD.md` and `MAP.md`.
- `project.yaml`: this project's commands, paths, phases, style and profile.
- `contracts/`: the contracts Alpaca checks.

## Precedence

When this file, a spec-kit template or a spec-kit skill disagrees with `CLAUDE.md` or `doctrine/`,
Alpaca's rules win. Follow the Alpaca rule and note the conflict in the plan.

## Constitution Check (used by /speckit-plan)

Fill the plan's Constitution Check from Alpaca's rules, not from this file. A plan passes when:

1. every requirement and success criterion has a check that can pass or fail (a test, a command
   with an expected result, or a named human review);
2. each behavior change starts with a test that fails before the change and passes after it;
3. the work is split into tasks that can each close with a sealed proof report and saved evidence;
4. shipping, deploying, pushing to a shared remote, deleting outside the project root and spending
   money stay human decisions;
5. changes to `contracts/`, `project.yaml`, `doctrine/` or the boot block in `CLAUDE.md` go through
   the owner review Alpaca requires.

A plan that cannot meet one of these lists it under Complexity Tracking with the reason.

## Changing the rules

Change the rules in `CLAUDE.md` or `doctrine/` through Alpaca, with the owner's review. Do not
use /speckit-constitution to write new principles into this file; if it is run, keep this file
pointing at the Alpaca files above.

**Version**: 1.0.0 | **Source**: Alpaca (installed by `alpaca spec init --kit spec-kit`)
