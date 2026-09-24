---
name: alpaca-interview
description: >-
  Interview the operator until raw notes add up to a runbook. Reads the notes in input/notes/,
  settles what they answer, then asks rounds of up to 4 questions with options for every slot a
  runbook needs (goal, done bar, thresholds, edge cases, failures, what must never happen, owner
  gates, rollback, evidence, knobs, commands), reads the answers back in five buckets, and signs
  the result off for the spec step. Triggers on "/alpaca-interview", "interview me for the
  runbook", "these notes are all I have", "help me say what the runbook needs", or raw notes
  that leave the done bar, the numbers or the failures unsaid.
---

# alpaca-interview

Operators hand over raw pieces and rarely know all that a runbook needs. This skill interviews
them until the pieces add up. It works the way flare does (`plugin/alpaca/skills/flare/SKILL.md`):
rounds of questions with options, each round built from the answers to the last one, then a
readback and a sign-off. It adds the slot map: the list of what a runbook needs, kept by
`alpaca interview`, so nothing is forgotten and every answer says where it came from.
`docs/interview.md` is the reference.

```
raw pieces -> input/notes/ (alpaca note add) -> this interview (slots, rounds, readback, sign-off)
  -> spec (spec-kit or OpenSpec) -> runbook 2 (/alpaca-runbook-forge) -> alpaca intake
```

The slots and what fills each one are in `templates/interview/slots.yaml` (a project may add or
replace slots in `input/interview/slots.yaml`): `goal`, `scope-out`, `done-bar`, `thresholds`,
`edge-cases`, `failures`, `never`, `owner-gates`, `rollback`, `evidence`, `knobs`, `commands`.

## How an answer is recorded

Every answer is one line in the log, written by the verb and never by hand:

```
bin/alpaca interview set <slot> --answered --value "<the answer>" --source <where it came from>
```

- `--answered`: the operator said it, or a note says it.
- `--default`: the operator accepted a default you proposed (they picked the recommended option
  and added nothing), or no one was there to answer (see below).
- `--waived --reason "<why>"`: the runbook can go without this slot; the reason is required.
- `--open`: reopen a slot that turned out wrong.

The source says where the value came from: the note path (`input/notes/<file>.md`) when a note
says it, `round:<n>/q<n>` for the round and the question that settled it, or `owner` when the
owner states it outside a round. Keep the operator's words in `--value`, and a number always with
its unit. A correction is a new `set`; the log is never edited.

## Step 1: read the notes and settle what they answer

1. `bin/alpaca note list`, then read every note file in full. A new piece the operator gives
   during the interview goes in first: `bin/alpaca note add "<text>"` (or `--file <path>`).
2. `bin/alpaca interview status` shows each slot, the open ones, and the notes no answer cites.
3. For each slot a note answers plainly, record it with the note as the source.
4. Contradictions between notes become questions. When two notes disagree (one says 50 ms, the
   other 100 ms), record neither: ask it in the first round, with both values as options and the
   note each comes from in the option's description.
5. A note that status still lists as uncited either answers a slot, contradicts one, or holds
   nothing the runbook needs. Say which in the readback.

## Step 2: ask in rounds

1. Build the round from the open slots and the contradictions. Ask **up to 4 questions** per round
   through the interactive question tool (`AskUserQuestion` in Claude Code), never as a wall of
   prose.
2. Each question has **2 to 4 options**, the **recommended default first**, labelled
   `(Recommended)`, with a one-line trade-off in each option's description. The operator can
   always type their own answer.
3. Ask in this order: contradictions, then the slots others depend on (`goal`, `done-bar`,
   `thresholds`), then the rest.
4. Ask for what the operator does not know to say: take the questions from the probe bank below
   and fit them to the notes. A probe names a concrete case ("an empty file", "two requests at
   once"); it never asks "anything else?".
5. After the round, record each answer with `--source round:<n>/q<n>` (round 2, question 3 is
   `round:2/q3`). The recommended option picked with nothing added is `--default`; another option
   or the operator's own words is `--answered`; "not needed here" is `--waived` with their reason.
6. Build the next round from the answers. An answer that opens a new fork gets a question next
   round; an answer that closes an area drops every question queued for it; an answer that
   contradicts an earlier one becomes the next question.
7. Stop when `bin/alpaca interview status` exits 0: every slot answered, defaulted or waived.

## Step 3: read back and sign off

1. Run `bin/alpaca interview readback` and show the operator its five buckets:
   - **Clear from the start**: answered by the notes;
   - **Added during the interview**: answered in a round or by the owner;
   - **Filled by default**: defaults nobody raised; name each one;
   - **Waived**: left out, with the reasons;
   - **Drifted**: a slot whose value changed after it was first settled, with both values.
   Add the notes that answer nothing, from step 1.
2. Ask one question with the question tool: **Approve** (recommended), **Adjust** something, or
   **One more round**.
3. On Adjust, record the change with `set` and read back again. On One more round, go back to
   step 2. On Approve:

```
bin/alpaca interview signoff --by "<the operator's name>"
```

It refuses while a slot is open, writes `input/interview/signed-<stamp>-<sha12>.md` (the readback
and the sha256 of the log) and records the sign-off. A later `set` makes the sign-off stale, and
`alpaca interview status` says so: read back and sign off again.

## Step 4: hand the signed file to the spec step

The signed file is the brief for the spec. A new thing: `/speckit-specify` with the signed brief.
A change to something that has specs: `/opsx:propose` with it. Carry the slots over like this:

| slot | goes to |
|---|---|
| `goal`, `scope-out` | the summary, the user stories and what is out of scope |
| `done-bar`, `thresholds` | success criteria, one per measurable bar, the number and unit in the text: `- **SC-001**: ...` |
| `edge-cases` | edge cases numbered `- **EC-001**: ...`, `EC-002` and on, in readback order; an id is never renumbered |
| `failures` | runbook fail cases, each with `detect` (how it is recognized) and `then` (retry, stop, ask the owner, or run a recovery stage) |
| `never` | a `stop_on` check in the retry block, or a check with `absent: true` |
| `owner-gates`, `rollback`, `evidence`, `knobs`, `commands` | the runbook: owner gates, a rollback stage or gate, check paths, knobs and stage commands |

Then `/alpaca-runbook-forge` writes the runbook in interview mode, with
`source: interview:<slot>` on every value it takes from a slot, and `/alpaca-from-notes` goes on
to intake.

## With no one to answer

In a run with no one to answer the rounds, still build the rounds and pick the recommended option
for each question. Record each as `--default --source round:<n>/q<n>`, and say so at the top of
the readback: every value in Filled by default was chosen with no one to answer. Do not sign off:
stop after the readback and say that the sign-off waits for the operator.

## Probe bank

Questions for what the operator does not know to say, per slot. Fit them to the notes; ask the
ones the notes leave open.

### `goal`

- Who uses this first, and what do they do today instead?
- Why now: what happens if this waits a month?
- What would make you call it a failure even if it runs?

### `scope-out`

- What will people ask for next that this does not do?
- Is there a part you already decided to leave for later?

### `done-bar`

- How would someone else know it is done without asking you?
- What would you look at first to believe it works?
- Which test or report would you show someone who doubts it?

### `thresholds`

- What number would make you say "too slow" or "too big", and in which unit?
- Under what load or size must that number hold?
- May a retry move the number, or is it yours alone to change?

### `edge-cases`

- What input would break this?
- What happens with nothing: an empty input, a missing file, zero items?
- What happens at the limit: the largest input, two requests at once, the same request twice?

### `failures`

- What went wrong the last time something like this was done?
- How would you notice it went wrong: a log line, an exit code, a number in a report?
- When it goes wrong, should we retry, stop, ask you, or run a fix first?

### `never`

- What must never happen, even on a retry?
- What data must never be lost, overwritten or sent out?

### `owner-gates`

- Which step do you want to approve yourself before it happens?
- Who else may approve it, and what do they read first?

### `rollback`

- If this goes wrong in use, how do we undo it?
- What shows that the undo worked?

### `evidence`

- Which file would you open to check each part?
- Where does the test or the measurement write its result?

### `knobs`

- Which setting may a retry change, and within what range?
- Which settings may only you change?

### `commands`

- How do you build it, run it and test it today?
- How do you measure the numbers in the done bar?

## Never

- Never record as `--answered` what the operator did not say and no note says.
- Never choose a threshold and record it as an answer; a proposed number is a `--default` and shows
  in the readback.
- Never edit or delete a line of `input/interview/log.jsonl` or a note; a correction is a new
  `set`, a new piece a new note.
- Never sign off for the operator, and never without their Approve.
- Never ask more than 4 questions at once, or a question a note already answers.
