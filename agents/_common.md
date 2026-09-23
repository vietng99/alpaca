# _common.md - the shared contract every agent brief references

This file carries the discipline every role obeys, factored out once so it cannot drift copy to
copy. Each role brief (orchestrator, scout, tracer, red, judge, eng, autodrive) REFERENCES this
file and then states only its role-unique contract. When a brief and this file appear to
disagree, this file governs the shared rule; the brief governs its own role.

This is not a spawnable agent. It is the common preamble.

## C1 - Read first, every session

- The record is the only truth: `.alpaca/alpaca.db` is the source, and `alpaca` is its sole writer. Read
  `RESUME.md` (a projection of the record) before acting; never hand-edit a projection.
- The formation you are in is a FILE: a manifest under `formations/` declares the roles, the
  phases it may run, its gate map, its budget class, and its output. You reason about that
  interface; you never invent a per-formation protocol.

## C2 - Provenance on every load-bearing claim

Every load-bearing claim carries an evidence pointer of a kind the record can resolve: a
`local:<path>` file, a `remote:<ref>`, a message id, or a board row id. An unevidenced claim is
discarded at the next rank. No "I remember reading something".

## C3 - Pointer returns and re-read from disk

A return message carries a verdict plus a pointer to a board row or a message, never the
load-bearing content itself (the native channel rule, spec 7.3). The consuming rank RE-READS the
cited file from disk. A dossier saves exploration; it never substitutes for it.

## C4 - Specced, Built and Verified are three different things

Never conflate a design on disk (Specced), a passing static check (Built), and a passing
behavioral probe (Verified). A causal claim carries a probe-result pointer or the literal
UNTESTED stamp. An untested causal claim cannot anchor a plan item or a decision.

## C5 - No silent caps

Whatever is deferred, sampled, truncated, unresolved, or out of scope is logged with its reason,
never quietly dropped. An empty measured population is BLOCKED, never a pass.

## C6 - One event to the orchestrator; the orchestrator is the sole serial writer

End with exactly one result for the orchestrator to serialize. You never append to the record
yourself: the orchestrator is the sole serial writer of the append-only chain. (The
orchestrator's own brief states the writer side of this rule.)

## C7 - The verdict contract

Every instrument you run ends on one of the four verdicts (PASS, FAIL, BLOCKED,
PAUSED-FOR-DECISION) or a reserved harness code; you read it, you never restate the numbers.
