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

A slot file that cannot be read, or a slot without an `id` or `fills`, is BLOCKED. The project
file may also turn on slot sets the product ships (see "Research contracts" below):

```yaml
include: [research]
slots: []
```

The slot map is then the defaults, the slots of each included set in the listed order, and the
project's own slots last (a project slot replaces a set slot of the same `id` too). An unknown set
name, or a set slot whose `id` is a default slot's, is BLOCKED.

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
alpaca interview use <set> [--json]
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
- `use <set>` turns a slot set on: it adds the set to the `include:` list of
  `input/interview/slots.yaml`. It creates the file (`include: [<set>]` and `slots: []`) when there
  is none, adds the set to a one-line `include: [a, b]` list, or appends an `include: [<set>]` line
  to a file that has none; an include list of any other shape is refused (FAIL) with a message to
  edit it by hand, and nothing is written. It prints the slot count before and after, and running
  it again changes nothing. `--json` gives `verdict`, `file`, `slots` (the slot ids after the
  change) and `progress`. An unknown set is BLOCKED, and so is a set name that is not a lower-case
  letter followed by lower-case letters, digits and hyphens (`../slots`, for one); either refusal
  writes nothing.

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

## Research contracts

A research runbook (`runbook: 3`) is the contract of an improve or repair loop that agents run: its
`research` block says what is measured, what an attempt may change, what counts as better, when the
run stops and what it may spend. The twelve default slots do not ask for that, so the research
slots come as a set of their own, `templates/interview/research-slots.yaml`. Turn them on with:

```
alpaca interview use research
```

Status then lists 26 slots and fails until the fourteen research slots are settled too; readback
and sign-off carry them like any slot, and a sign-off from before `use` is stale, because the slot
map gained slots. Each research slot has, besides `id`, `fills` and `becomes`, a `fields:` list of
the format 3 fields it fills, and a `mode:` (`improve`, `repair` or `both`). A slot the run's mode
does not need is waived with the reason its `waive:` line gives.

| slot | fills | mode |
|---|---|---|
| `research-mode` | improve or repair, the pack and runbook, the question the run answers, the repository and ref when not this project at HEAD | both |
| `research-metric` | each metric: what it measures, its command and where it starts, how the number is read, its unit and direction | improve |
| `research-noise` | deterministic or noisy; for a noisy metric the repeats, the fewest successful repeats, the middle value of an even count, the margin and the seeds | improve |
| `research-artifact` | whether every component of each metric is measured on one artifact (one build, one output), and which one; when not, the metric is fixed before the sign-off | improve |
| `research-targets` | the owner-only target of each metric that has one, and where the number comes from (a spec item or an owner decision) | improve |
| `research-accept` | the accept rule, and whether the run stops or keeps looking for Pareto gains once every target is met | both |
| `research-editable` | the paths an attempt may change, as rules from the repository root | both |
| `research-protected` | the contract paths no attempt may change, the hidden, test, read, keep and output paths, and the tools and shared checkouts to fingerprint | both |
| `research-gates` | the gate stages in order, which are heavy and their lock wait, each stage's and each metric repeat's timeout | both |
| `research-stop` | the rounds N and slots K; at N the run always halts and asks the owner | both |
| `research-budget` | any cap on the money, tokens or wall time one run may spend (none set: the run is uncapped), and the free disk in GiB it needs | both |
| `research-proposal` | the pack's own proposal fields, the hypothesis categories, the lessons file and the longest note line | both |
| `research-controls` | the no-op control rounds, the random-edit control with its mutation script, gate stage and seed, and the holdout stages and paths | both |
| `research-repair` | each bug case with its report, base commit, reproducing test, timeout, repeats and suspect files; the pass-to-pass suite; a prepare stage; files that change together | repair |

`research.defaults`, `research.runtime`, `research.verifier`, `research.audit` and
`research.egress` have no slot: they are optional and a pack author sets them by hand. `covers`
has no slot, and neither do the optional `covers` and `source` pairs on the other parts: the spec
step names the spec items and the forge writes them. A metric's `source` is the exception:
`research-targets` asks it with the target.

### The fill-in contract templates

`templates/research/improve.yaml` and `templates/research/repair.yaml` hold a line for every field
of a research runbook in that mode. Each value to fill is a quoted placeholder,
`"<FILL:<field path>>"` (`[]` stands for one item of a list), and each line's comment says whether
the field is REQUIRED, REQUIRED-when a condition holds, or OPTIONAL with its default, what it
means, and the interview slot that asks for it (`slot: none` when no slot does). To fill one by
hand: copy it next to the pack's spec, fill each placeholder, copy each item marked "one item per
..." once per item, and delete every line left unfilled (an OPTIONAL line not needed, a
REQUIRED-when line whose condition does not hold, a field the pack defaults file already sets).
Then check it:

```
alpaca runbook check <the filled file> --spec <the pack's spec>
```

The format 3 check refuses a value that still holds a placeholder (`FIELD-UNFILLED`) and names any
required field missing from the merged block (`FIELD-MISSING`), so a REQUIRED line deleted by
mistake is caught. For the field reference, `research_fields()` in `alpaca/runbook.py` lists every
field with whether each mode requires it.

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
