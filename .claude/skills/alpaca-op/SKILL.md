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
- Always pass `--title`: a short name of at most 60 characters, e.g. `--title "Watchdog"`. The
  Roadmap labels the op's map "Operation map Op21 - Watchdog"; the intent stays the full
  description. Without `--title` the name is derived from the intent (the part before the first
  colon) and `op new` says so. Rename with `alpaca op title <op> "<name>"`.
- `op new` pins the new op, so its operation map shows on the Roadmap. Pass `--no-pin` for an op
  that should not get a map; `alpaca op unpin <op>` removes one later.

## Working the board

- Organize every new task into a named feature/workstream and work area when creating it; record actual prerequisites (or independence) and exclusive resources explicitly. Use mapped `task add`; update relationships with `task map` as the plan changes. See `docs/operations-hub.md` under "Maintaining a useful checklist" for commands. Do not use phases as feature groups or infer dependencies from task numbers.

- `alpaca task add` files a task; `alpaca task claim` takes it; `alpaca task move <id> <state>`
  advances it.
- `task add` requires `--title`: a short name of at most 60 characters, e.g. "login page first release". The
  statement is the full description with the pass criteria. Rename with `alpaca task title <id> "<title>"`.
- A task is done only with a sealed proof report. Run `alpaca proof new <id>`, write the report the
  way an engineer documents work (a 40-600 character At a glance summary first, then what was done, how, where, the result, deviations, how to
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
- `alpaca op close` closes the op with its proof pointer once every task is done or deferred with
  a trigger. A deferred task needs `--reason` and `--reactivate-when`; only a trigger that names
  `after:t-NNN`, `op:op-NNN closed` or `date:YYYY-MM-DD` ever fires, and free text never does. Op
  close lists a deferred task and does not wait for it. Moving a done task to deferred counts as
  moving it back from done, so the op then owes a report. Defer a task because it should wait,
  never to get the op closed.
- The AAR gate: `op close` is BLOCKED while `alpaca aar owed <op>` lists a trigger. A task moved
  back from done and an `alpaca aar owe` always count for the op. A NO-GO counts for the op in its
  event's op column, else for the one open op of the tasks the deciding session held in doing; a
  release check FAIL counts for the one open op of the tasks the recording session held (never the
  shared `cli` or `alpaca` session, never a standing op). One that no op can be tied to is
  unattributed and blocks the close of every op open when it was recorded. After a NO-GO or a
  failed release check, run plain `alpaca aar owed`; if the trigger belongs to your op, run
  `alpaca aar owe <op> --event <id>`, which makes your op owe the report and clears it for the
  others. Otherwise sealing the report of each op it blocks answers it for that op, or the owner
  waives it per op. A seal answers every trigger counted for the op up to the seal; one recorded
  after the seal needs a new seal. Write the report with `alpaca aar new <op>`, fix what
  `alpaca aar check <op>` names, have it reviewed as `.claude/skills/alpaca-aar/SKILL.md` says
  (`alpaca aar review`, a fresh subagent, `alpaca aar accept`), then run `alpaca aar seal <op>`; its action items become tasks,
  in the op `--into` names or the standing After-action items op (there is only one; when its plan
  is full the seal refuses and names `--into`, and its own report files into itself). Editing the report after the
  seal brings the gate back until you seal it again.
  Only the owner waives a report: run `alpaca aar waive <op> --by owner --quote "<op>: waive"` only
  after the owner's own message in this session is exactly `<op>: waive`. Never type that message
  or the quote for the owner.

## Never

- Never leave the project root. Never hand-edit `RESUME.md` or `analytics/`.
- Never move the ship review card by an agent; the owner moves it.
- Never waive an after-action report on your own judgement; it takes the owner's own message.
