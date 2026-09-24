# The interview: from raw pieces to a signed brief

Operators mostly hand over raw, unorganized pieces, and they do not know all that a runbook
needs. Alpaca keeps the pieces as they are, then interviews the operator until they add up to a
runbook: a done bar, the numbers, the edge cases, the known failures and what to do about each,
what must never happen, and who approves what.

```
raw pieces -> input/notes/ (inbox) -> interview (slot map, rounds, readback, sign-off)
  -> spec (spec-kit or OpenSpec) -> runbook 2 (forge, sources) -> intake -> rows with bars
```

- `alpaca note` keeps the pieces (the inbox).
- `alpaca interview` keeps the slot map, the answers, the readback and the sign-off.
- The skill `/alpaca-interview` (`.claude/skills/alpaca-interview/SKILL.md`) asks the questions.
- `alpaca start` and `/alpaca-from-notes` send raw notes here before the spec step; the rest of
  the path is in `docs/intake.md`.

## The inbox: alpaca note

```
alpaca note add "<text>"
alpaca note add --file <path>
alpaca note add -                  # read stdin
alpaca note list [--json]
```

`alpaca note add` writes `input/notes/<UTC stamp>-<sha12>.md`: a small front matter, then the body
exactly as given, byte for byte.

```
---
time: 2026-09-24T10:00:00+00:00
by: <who gave it; --by, or the session>
from: text | file:<name> | stdin
sha256: <sha256 of the body>
---
<the body>
```

A note is never rewritten. The same bytes again answer `already have <file>` and write nothing.
An empty body is refused (FAIL). Each new note records one `note` event in the record (file,
sha256, bytes). `alpaca note list` shows the notes in time order with their first line.

## The slot map: alpaca interview

A slot is one thing a runbook needs, with the question family that fills it. The default list is
`templates/interview/slots.yaml`:

| slot | fills |
|---|---|
| `goal` | what is being built or changed, for whom, and why now |
| `scope-out` | what is explicitly left out |
| `done-bar` | how anyone checks it is done without asking the owner (becomes SC items) |
| `thresholds` | every number the done bar needs (latency, size, count), with units |
| `edge-cases` | inputs and states at the boundary (becomes EC items) |
| `failures` | known ways it goes wrong, how each is recognized, what to do (becomes fail cases) |
| `never` | what must never happen, even on a retry (becomes `stop_on` checks or `absent` checks) |
| `owner-gates` | what only a person may approve, who, and on what evidence |
| `rollback` | how to undo a change and what proves the undo worked |
| `evidence` | which files prove each part |
| `knobs` | settings a retry may move, their range, and which are owner only |
| `commands` | how to build, run, test and measure |

A project adds or replaces slots in `input/interview/slots.yaml`, in the same shape. A slot whose
`id` is in the default list replaces it; a new `id` is added after the defaults:

```yaml
slots:
  - id: data-retention
    fills: how long a stored link is kept
    becomes: an SC item and a check on the cleanup job
```

A slot file that cannot be read, or a slot without an `id` or `fills`, is BLOCKED.

### The log

The state is an append-only log, `input/interview/log.jsonl`, one JSON line per change:

```
{"slot": "thresholds", "state": "answered", "value": "p95 under 50 ms at 200 rps", "source": "round:1/q2", "time": "..."}
```

`state` is one of `answered`, `default` (the operator accepted a proposed default), `waived`
(needs `reason`) and `open`. The latest line of a slot is its state; a slot with no line is open.
`source` says where the value came from: a note path (`input/notes/<file>.md`), a round and
question (`round:2/q1`), or `owner`.

### The verbs

```
alpaca interview status [--json]
alpaca interview set <slot> --answered|--default|--waived|--open --value <text> --source <ref> [--reason <text>]
alpaca interview readback [--json]
alpaca interview signoff --by <name> [--json]
```

- `status` shows each slot with its latest state and value, a progress line
  (`8/12 slots settled`), the open slots, the notes no answer cites yet, and the sign-off
  (`needed`, `signed` or `stale`). It exits 0 (PASS) only when no slot is open and 1 (FAIL)
  otherwise, so a script can gate on it.
- `set` appends one line. It refuses (FAIL) an unknown slot, a waive without a reason, an answer
  or default without a value, and a source that is not a note path of this project,
  `round:<n>/q<n>` or `owner`. It never changes an earlier line: a correction is a new line.
- `readback` gives five buckets from the log and the notes:
  - clear from the start: answered, with a note as the source;
  - added during the interview: answered in a round (or by the owner);
  - filled by default: state `default`, a default the operator never raised;
  - waived, with the reasons;
  - drifted: a slot whose value changed after it was first settled, with both values.
- `signoff --by <name>` refuses while a slot is open. It writes
  `input/interview/signed-<UTC stamp>-<sha12>.md`: a front matter with who signed, when, the
  sha256 of the log at signing (sha12 is its start), the slot list and the notes in the inbox,
  then the readback. It records one `interview-signoff` event. Signing an unchanged log again
  writes nothing. The sign-off is `stale` (in `status` and in `alpaca start --json`, with the
  reason) when a later line is in the log, when the slot map gained or lost a slot (a project
  `slots.yaml`, a product upgrade), when a slot is open, or when a note was kept after it: new raw
  input the interview has not seen. A note that was there at signing and that no answer cites
  does not change it. Read back and sign off again.

## The interview skill: /alpaca-interview

The skill runs the interview in rounds, in the style of flare
(`plugin/alpaca/skills/flare/SKILL.md`):

1. It reads every note and `alpaca interview status`, and settles what the notes answer
   (`--answered`, the note as the source). Contradictions between notes become questions.
2. It asks rounds of up to 4 questions through the interactive question tool, each with 2 to 4
   options and the recommended default first. The next round is built from the answers. A probe
   bank in the skill gives, per slot, the questions for what the operator does not know to say
   (for `edge-cases`: "what input would break this?"; for `never`: "what must never happen, even
   on a retry?").
3. It stops when every slot is answered, defaulted or waived, shows the readback, and asks
   approve, adjust or one more round. On approve it runs `alpaca interview signoff`.
4. It hands the signed file to the spec step (`/speckit-specify` for a new thing, `/opsx:propose`
   for a change): done-bar and thresholds become SC items, edge-cases become EC items numbered
   `EC-001` and on, failures and never feed the runbook's fail cases and its `stop_on` or `absent`
   checks.

With no one to answer (a run with no operator), the skill records its proposed defaults as
`default` with the source `round:<n>/q<n>`, says so in the readback, and leaves the sign-off to
the operator.

## After the sign-off

The runbook forge (`/alpaca-runbook-forge`) has two modes. Quiet mode is for a complete spec and
no interview, such as a spec a partner sends. Interview mode is for a signed interview: it takes
thresholds, failures, never, owner gates, rollback, knobs and commands from the signed slots,
writes `runbook: 2` with `source: interview:<slot>` on each value, writes fail cases with `detect`
and `then`, and asks only for what the signed slots still lack. It records each answer of its
own rounds in the slot it fills (`alpaca interview set <slot> --answered --value "<answer>"
--source round:<n>/q<n>`), so the signed brief holds every fact the runbook uses; the sign-off is
then stale until the operator reads it back and signs off again. `docs/runbook-format.md` is the
runbook reference and `docs/intake.md` the rest of the path.
