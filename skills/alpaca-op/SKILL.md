---
name: alpaca-op
description: >-
  Run one Alpaca op from intent to close over the board. Use to open an op from a
  stated intent, add and claim its tasks, move them to done with a sealed proof report, and
  close the op behind the human decisions that stay human. Triggers on "/alpaca-op",
  "open an op", "run this op", "close the op", or campaign-style multi-task work under
  the harness.
---

# alpaca-op

The unit of work. One op is one milestone or one campaign; its tasks are the rows.
This skill drives an op through the board and keeps every claim tied to a proof.

## Opening

- An op opens only from a stated intent. Record the intent line verbatim; it is the
  op's done-bar authority.
- `alpaca op new` opens it. The level in force and the standing authority id are read from
  the record, never from the environment.

## Working the board

- `alpaca task add` files a task; `alpaca task claim` takes it; `alpaca task move <id> <state>`
  advances it.
- `task add` requires `--title`: a short name of at most 60 characters, e.g. "login page first release". The
  statement is the full description with the pass criteria. Rename with `alpaca task title <id> "<title>"`.
- A task is done only with a sealed proof report. Run `alpaca proof new <id>`, write the report the
  way an engineer documents work (what was done, how, where, the result, deviations, how to
  reproduce, and the evidence list), run `alpaca proof seal <id>`, then
  `alpaca task move <id> done --proof local:<report>`. A `remote:` ref belongs inside the report's
  Evidence list; alone it does not close a task. No sealed report, not done: absence blocks.
- Every claim carries a pointer to a file, a command output, or a record row.

## Messages, claims, decisions

- `alpaca msg post` / `alpaca msg read` carry cross-agent messages over the board.
- Decisions that change posture or authority are recorded rows, not prose.

## Closing

- Ship, release close, and irreversible external actions (push to a shared remote,
  deploy, delete outside the root, spend money) are recorded human decisions at every
  autodrive level. An agent never auto-advances them.
- `alpaca op close` closes the op with its proof pointer once every task is done.

## Never

- Never leave the project root. Never hand-edit `RESUME.md` or `analytics/`.
- Never move the ship review card by an agent; the owner moves it.
