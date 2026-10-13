# Intent fidelity: the owner's words decide intent

Op-019 stops an agent from inventing intent and calling it the owner's. The brief
(`design/intent-fidelity/brief.md`, section 1) names five problems and the work answers them
(R1 silence counted as consent, R2 an open question closed by the agent, R3 agent-written task
statements treated as the owner's, R4 an owner directive lost at a handoff, R5 design checks that
never read the brief). The parts: P1 is a question batch at the start of an op (fixes R1 and R2);
P2 is an origin label on every contract line (fixes R3); P4 is a self-check at each phase door
(fixes R5); P6 is owner orders that the gates read (fixes R4). P3, the L5 owner card for a choice
outside the signed brief, is the owner's decision D1 for L5, not a fix for one problem. The design is in
`design/intent-fidelity/design.md`.

The rule itself is one doctrine leaf, `doctrine/leaves/intent-fidelity.md`, which the owner approved
(decision dec-d63641f7b7af) before it went into `doctrine/` (design section 7). This guide explains
how the verbs work. Where the guide and the code differ, the code wins.

```
alpaca brief present|sign|show <op>      the signed brief
alpaca batch open|propose|show|answer|check|derive <op>
alpaca owner ask <subject>               ask the owner about a record id
alpaca task add|contract ... --origin    origin labels on contract lines
alpaca order add|close|list|check-note|not-order
alpaca choice <task> --outside|--covered-by|--withdraw
alpaca selfcheck brief|submit|derived|status
alpaca review move <rc-N> --accept|--reject --by owner --quote Q
```

## 1. Which ops and which level

- The batch start gate, the phase rule, the self-check and the order refusals on task moves bind
  only an op that has a batch (`alpaca batch open`; `alpaca/batch.py` task_gate, design D-1). An op
  opened before op-019, and an op a research run opens, has no batch and is not gated.
- Three things bind every op: the `owner-orders` door link and the handoff check, the origin labels,
  and the owner-word rule on owner-only writes.
- "An L5 or L6 run" means the higher of the named session's level and the level of every session
  the environment names (`alpaca/intentgate.py` level; design D-3), so `--session cli` or a made-up
  id cannot lower it. The gates are asked on `task claim` and on `task move` to doing, review or
  done. A move to open, blocked or deferred is never gated (`ACTIONS` in `alpaca/intentgate.py`). The
  providers are asked in the order batch, orders, oob, selfcheck (`PROVIDERS`).

## 2. Owner words

An owner-only write needs the owner's own words, which come by one of two paths
(`alpaca/ownerword.py`, module text).

- The terminal path: no agent marker is in the environment, the shell is a terminal, and the person
  types the id.
- The session path: the owner's message sits in the session transcript, typed after the ask.

What counts as an owner message: a typed prompt or a queued command. A sidechain entry, a meta
entry, a compact summary and a `claude -p` entry do not count.

Answer forms, after `alpaca owner ask`:

```
alpaca owner ask <subject> --text "what the owner is shown"
alpaca owner ask decision --kind K --context C --option O --choice X --consequence Y --pointer P
```

The ask prints the id and `Reply with: <id> yes | <id> no | <id>: <your answer>`. The owner replies
in the chat with `<id> yes`, `<id> no` or `<id>: <text>`.

- Assent words are yes, ok, okay, accept, approve, approved, agree, fine, go and signed. Refusal
  words are no, reject and decline (`ASSENT` and `REFUSAL` in `alpaca/ownerword.py`).
- The id matches at a token boundary, so `q1` never matches `q10`. A segment with a `?` answers
  nothing. One trailing `.` or `!` is allowed.
- The batch phrases `all yes`, `yes all`, `yes to all` and `all ok` answer a whole batch.

Writes that need owner words: `batch answer`, `brief sign --quote`, `order add`, `order close`,
`review move` of an out-of-brief card, `decide --by owner --quote`, and `task add`, `task contract` or
`task title` with `--by owner` or `--by human` (needs `--quote`). The other review card kinds
keep the older `--by` rule (design section 8).

`decide` without `--quote` still records the decision, but it is not owner-proved. A grandfather
list covers decisions that predate the rule (`ownerword-grandfathered`).

A refusal carries one of these tokens: OWNER-WORD-NOT-HUMAN, OWNER-WORD-NO-TRANSCRIPT,
OWNER-WORD-NOT-FOUND, OWNER-WORD-UNBOUND, OWNER-WORD-NOT-TERMINAL, OWNER-WORD-SESSION. A `--session`
that differs from the real session is refused.

## 3. The signed brief

```
alpaca brief present <op> <file>
alpaca brief sign <op> <file> --by owner --quote "<sha12> signed"
alpaca brief sign <op> <file> --decision <dec-id>
alpaca brief show <op>
```

`present` copies the file and prints its sha256. `sign` records the owner's signature, either by a
quote of the owner's message or by an owner-proved decision whose body names the file's sha256.
`show` lists what was signed and says `changed` when the live file differs from the signed copy.

The signed copy lives under `.alpaca/briefs/<op>/<sha256>.md`. Quotes and the self-check read that
copy, and it is hashed again on every read (`alpaca/brief.py`, design D-16). The limit is 1 MiB.

## 4. The start-of-op batch

```
alpaca batch open <op> [--plan F] [--question Q]...
alpaca batch propose <op> <item> --text T [--from REF]
alpaca batch show <op> [--json]
alpaca batch answer <op> [item] (--yes|--no|--text T|--all-yes|--reply) --by owner --quote Q
alpaca batch check <op>
alpaca batch derive <op> <item> --text T --reason R
```

The batch holds six class items (runtime, credentials, sandbox, spend, external, deleting; from
`templates/interview/intent-slots.yaml`) and the plan's questions q1, q2 and so on. A plan question
is a sentence that ends with `?`. A question written without one goes in with `--question`.

- The agent pre-fills a proposal with `batch propose`. A proposal is never an answer.
- `batch show` prints the batch to the owner and records the owner-ask.
- The owner answers yes, no, or with their own answer. A `no` to an item that had a proposal
  declines the proposal: the item goes back to open with the declined text kept, and it stays
  unanswered until the owner gives another answer (`answer` in `alpaca/batch.py`). A `no` to an
  item that had no proposal is the answer. `--yes` records the proposal text as the last
  `batch show` printed it, and is refused when the proposal changed after that show.
- An unanswered item counts as no.
- `batch check` prints PASS when every item is answered and the op has a signed brief, else FAIL.
  The verb only prints; it writes nothing.

The start gate at L5 and L6: a claim, or a move to doing, review or done, is refused with
BATCH-UNANSWERED, which names the open items, or with BRIEF-UNSIGNED. The first such claim or move
that finds the batch passing writes the `batch-cleared` event (`task_gate` in `alpaca/batch.py`).
After that the batch is a start condition and not a standing stop (design D-13).

Late items are items added after `batch-cleared`. An item added after `batch check` printed PASS
but before the first claim or move at L5 or L6 is part of the start batch, not late.

- At L6 the claim is refused with BATCH-LATE-ITEM until the agent answers the item with
  `batch derive`. That records the agent's answer and its reason on the batch item, which
  `batch show` prints as derived by the agent, and as a derived choice that `selfcheck derived` and
  the pad count list (section 8). `derive` works at L6 only
  (`DERIVE_LEVEL` in `alpaca/batch.py`).
- At L5 an out-of-brief card with the subject `<op>:<item>` is raised and the claim goes on.
  Nothing waits for that card. This is a gap against the brief, listed in section 12.

On an op with a batch, `task add` needs `--phase` from requirement, design, build, verify or
release. Without it the refusal is TASK-PHASE-REQUIRED, and an unknown phase gives
TASK-PHASE-UNKNOWN (design D-19).

A short run: `op new` opens the batch with the six class items (runtime, credentials, sandbox,
spend, external, deleting). `batch open op-020 --plan plan.md` adds the plan's questions (q1, ...).
The agent runs `batch propose op-020 <item> --text ...` for every item, then `batch show op-020`.
The owner replies in the chat `all yes` (only valid when every waiting item had a proposal) or
answers each id (`runtime yes`, `q1: use the staging host`). The agent records the reply with
`batch answer op-020 --all-yes` or `--reply`, with `--by owner --quote` and the owner's whole
message (these two forms take no item). A `no` to a proposal in a `--reply` message leaves that item
open, so the agent proposes again and runs `batch show` again. The owner signs the brief (section 3). Then
`batch check op-020` prints PASS. With an item still open or the brief unsigned it prints FAIL.
The batch is cleared by the first claim or move at L5 or L6 after that, not by the verb.

## 5. Origin labels on contract lines

Each done-bar, input, expected and fail-case line of a task contract has one origin
(`alpaca/taskcontract.py`).

| origin | needs |
|---|---|
| `owner-quote=<source>` | a quote of a signed brief file, of `session:<sid>`, or of `dec:<id>`; the trimmed line must be a byte-for-byte substring of the source |
| `decision=<dec-id>` | a recorded decision; an unknown id refuses with ORIGIN-DECISION-UNKNOWN |
| `derived=<reason>` | a one-line reason of 1 to 200 characters |
| `agent-proposed` | nothing; this is the default |

A failing quote refuses the whole write (QUOTE-SOURCE-MISMATCH, QUOTE-SOURCE-UNSIGNED;
`alpaca/gates/quote_check.py`). Matching is on the whole trimmed line.

Two ways to set an origin:

- The flag: `alpaca task add ... --origin SPEC` and `alpaca task contract <id> --origin SPEC` set
  the origin of every line given by flag in that call.
- The file: with `--file`, a line may be an object with `text`, `origin` and one of `source`, `ref`
  or `reason`; a top-level `origin` is the default for plain text lines.

A line with no label reads as agent-proposed. `alpaca task contract <id> --show` prints each line
with its origin, and a decision origin carries `(owner-proved)` or `(not owner-proved)`. It ends
with this rule: "A contract line labelled agent-proposed (or with no origin) is never owner
authority; cite the signed brief, an owner-quote line, an owner-proved decision or a batch answer
instead." No checker may cite an agent-proposed line as owner authority.

A task an agent adds is recorded with the agent as actor. `--by owner` or `--by human` needs
`--quote`.

## 6. Owner orders

```
alpaca order add <op> --quote Q --door <boundary|any> --by owner [--lifted-by Q]
alpaca order close <ord-N> --by owner --quote "ord-N resume"
alpaca order not-order <op> --quote Q --reason R
alpaca order list [--op OP] [--open]
alpaca order check-note <file> [--op OP]
```

The quote has at least three words and is a substring of one owner message. The boundaries are
requirement->design, design->build, build->verify and verify->release, or `any`. Close words are
close, lift, done and resume, besides the usual assent words. `not-order` records the agent's
reading that a message is not an order, as a derived choice for the owner to read. `--lifted-by` records the order already
closed and records the reading as a derived choice.

An open order has these effects at every level, L6 included (`alpaca/orders.py`, module text):

- The door link `owner-orders` is paused while an open order binds that boundary or `any`.
- On an op with a batch, claims and moves of tasks past the order's boundary are refused (ORDER-OPEN;
  design D-14 and D-18).
- A `msg post --kind handoff` that drops an open order is refused at post (ORDER-DROPPED; design
  D-8). A landing or handoff note file is checked by `alpaca order check-note <file>`, and at
  `proof seal` when the report cites it as local evidence and its file name holds "landing" or
  "handoff". A note that is never cited and never checked is not refused. The landing pad lists
  the open orders.

The self-check packet carries the owner's chat messages, so the checker can report an order that no
`ord-N` row records (design D-17). The agent then records it, records it lifted, or records
`not-order`.

## 7. The L5 card (out-of-brief)

Who raises one:

- `alpaca choice <task> --text T --outside` at L5, with the task id as the subject;
- a door self-check finding at L5, with the subject `<op>:<boundary>:<event id>:<n>`;
- a late batch item at L5, with the subject `<op>:<item>`.

What waits: a task the card names. Its claim and its move to doing, review or done are refused with
OUT-OF-BRIEF-CARD-OPEN. A door card pauses that door and the phase change behind it. A late-item
card names no task, so nothing waits. Other tasks go on.

The owner answers with `alpaca review move <rc-N> --accept|--reject --by owner --quote "rc-N yes"`.
Free text is refused (REVIEW-OUT-OF-BRIEF-DECISION) and so is an agent move
(REVIEW-MOVE-NOT-A-HUMAN).

After a reject the task keeps waiting until `alpaca choice <task> --withdraw <rc-N>`. The rejected
choice goes into the next packet's `owner.json`, so a deliverable that repeats it is a mismatch.

`alpaca choice <task> --covered-by REF` records the agent's claimed basis. It is a claim: the next
packet lists it as `covered-by-claim` and the checker reads it again. At any level other than L5,
`--outside` records a derived choice and nothing stops.

## 8. The self-check at phase doors, and derived choices

The self-check binds at L5 and L6 on an op with a batch: the door link `intent-selfcheck` and the
phase change itself (a claim, or a move to doing, review or done, asks every earlier boundary whose closing phase
has tasks in the op; a task with no phase comes after every boundary; design D-12).
Below L5 it is not asked.

```
alpaca selfcheck brief <op> <boundary> [--deliverable P]...
alpaca selfcheck submit <op> <boundary> <result.json>
alpaca selfcheck derived [--op OP]
alpaca selfcheck status <op> [boundary]
```

`selfcheck brief` writes a packet under `.alpaca/selfcheck/<op>/<boundary-slug>-<n>/`: the signed
brief copy, the runbook, `owner.json` (owner answers, open and lifted orders, owner messages,
rejected cards, covered-by claims), the deliverables (the op's work so far, design D-19),
`prompt.md` with a nonce, and `packet.json` with a sha256 per file. Builder notes and proof files are
left out. A deliverable over 2 MiB is listed as dropped.

The main session dispatches a fresh subagent with `prompt.md`. Alpaca launches no agent. The
subagent writes `result.json`; the finding kinds are mismatch, outside-brief and dropped-order.
`selfcheck submit` checks every quote byte for byte, checks from the transcript that a separate agent
wrote the result, records a `selfcheck` event, and records one derived choice per outside-brief
finding.

Results:

- At L6 a mismatch holds while its deliverable quote is still there, and a dropped order holds until
  it is accounted for. The door fails and the work goes back for rework without asking the owner.
  An outside-brief finding never fails the door at L6; it becomes a derived choice.
- At L5 each finding raises a card. A mismatch whose quote is still there, or a dropped order not
  accounted for, fails the door as at L6 (SELFCHECK-HOLDING). Otherwise the door pauses while a
  card of it is open, and a rejected card's finding fails the door while its quote is still there.
- With no current PASS the door is BLOCKED at L5 and FAILs at L6.

`selfcheck derived` lists the derived choices for the owner, and the pad shows "derived choices: N
for the owner to read". What writes a derived choice: `choice --outside` at any level other than L5, `order not-order`,
`order add --lifted-by`, outside-brief findings at `selfcheck submit`, and `batch derive` (the batch
item records the same answer, section 4). A choice nobody reports is caught only when the
next self-check finds it. The code does not record every choice.

## 9. What stops for the owner, by level

| event | below L5 | L5 | L6 |
|---|---|---|---|
| start of an op with a batch | not gated | refused until every item is answered and the brief is signed | same as L5 |
| late batch item | not gated | out-of-brief card; nothing waits | claim refused until `batch derive` |
| choice outside the brief | not gated; `--outside` records a derived choice | card; the named task waits | derived choice; nothing stops |
| door self-check finding | not asked | card; held mismatch or dropped order fails the door, else the door pauses while a card is open | mismatch or dropped order: door fails, rework; outside-brief: derived choice |
| open owner order | the door link pauses | the door link pauses | the door link pauses |

At every level the floor that was there before still holds: shipping and irreversible external
actions are human decisions (CLAUDE.md boot rule 3). This op does not change that.

## 10. Reason tokens

| token | where it comes from |
|---|---|
| BATCH-UNANSWERED, BRIEF-UNSIGNED, BATCH-LATE-ITEM | the start gate and late items (`alpaca/batch.py`) |
| TASK-PHASE-REQUIRED, TASK-PHASE-UNKNOWN | `task add` on a batch op |
| ORDER-OPEN, ORDER-DROPPED | `alpaca/orders.py` |
| OUT-OF-BRIEF-CARD-OPEN, OUT-OF-BRIEF-CHOICE | `alpaca/oob.py` |
| SELFCHECK-NO-BRIEF, -BUILDER-NOTES, -NO-PACKET, -PACKET-CHANGED, -RESULT, -QUOTE-NOT-FOUND, -BRIEF-SHA, -PASS-REFUSED, -NOT-SEPARATE, -MISSING, -HOLDING | `alpaca/selfcheck.py` |
| QUOTE-SOURCE-MISMATCH, QUOTE-SOURCE-UNSIGNED, ORIGIN-DECISION-UNKNOWN | origin labels |
| REVIEW-MOVE-NOT-A-HUMAN, REVIEW-OUT-OF-BRIEF-DECISION | `alpaca/review.py` |
| OWNER-WORD-* (six) | section 2 |

The two door link names are `owner-orders` and `intent-selfcheck`.

## 11. The doctrine leaf

The rule is `doctrine/leaves/intent-fidelity.md`, registered with its INDEX.md row and MAP.md line.
It was drafted at `design/intent-fidelity/leaf-draft.md` and written into `doctrine/` only after an
owner-proved approval decision (dec-d63641f7b7af, design section 7); the apply script
(`.alpaca/proofs/op-019/logs/t-205/apply-leaf.sh`) refuses a decision that does not name the leaf's
sha256. Then
`alpaca/gates/doctrine_registration_check.py` re-derives its registration and its digest. That gate
checks the registration and the bytes, not the wording.

## 12. Honest limits

- The same Unix user can write a transcript and the record database, so an owner word proves where
  the text sits, not who typed it.
- `session level` drop-and-raise is visible but not prevented.
- A Codex session records owner words only at the terminal, and the terminal path leaves no file to
  read again.
- Module-level callers can move other card kinds with any actor.
- Question extraction needs a `?`.
- `--covered-by` is a claim.
- An order quote proves the owner typed the words, not that they are an order for this op.
- The checker's isolation is the packet and the prompt; it can open other files.
- A hash check fails the read of an edited brief copy; it does not stop the edit.
- Owner-quote matching is on the whole trimmed line.
- A late batch item at L5 raises a card but nothing waits (section 4).
