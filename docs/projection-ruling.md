# The store-versus-projection ruling

The question: is the wiki a store or a projection of the record? This page records the answer and
why. The gate that enforces it is `alpaca/freshness.py`.

## The contradiction

Two readings of the wiki collided. In one, the markdown files under `.alpaca/wiki/` are the store:
you read them, you edit them, they are the source. In the other, the record in `.alpaca/alpaca.db` is
the store and the markdown is a rendering of it. The same tension runs through `RESUME.md`,
`CHECKLIST.md`, `board.json` and `data.json`: each looks like a file you could open and fix,
and each is in fact a projection.

Both cannot hold. If the page is truth, a hand-edit is a correction; if the record is truth, a
hand-edit is a lie the next render erases.

## The ruling

The owning store is truth within its scope. For operational assertions, `.alpaca/alpaca.db` is the source. `RESUME.md`, `CHECKLIST.md`,
`board.json`, `data.json` and operational decision pages under `.alpaca/wiki/decisions/` are
projections rendered from that record and never hand-edited.

Rune2 has a separate, scoped store: `.alpaca/wiki/rune.db` and its ledger hold indexed
source documents, evidence-linked graph assertions and answer history. Operational event
notes are derived from Alpaca DB; transcript notes derive from preserved visible exchanges;
explicitly ingested wiki documents retain their source content in the vault. Rune2's rendered
`projection/` is downstream of that store. A Rune2 assertion does not change task state,
acceptance, policy authority or the original operational record. Corrections target the
owning source and enter Rune2 through ingestion, never by editing a rendered projection.

The dashboard's wiki page count measures operational pages. `bin/alpaca wiki status` reports
the Rune2 corpus and capture gaps separately. A document count is not a graph-fact count,
and capture is not automatic admission of lessons.

A correction targets the assertion in the record, not the rendered page. You do not fix a wrong
line in `board.json` by editing `board.json`; you move the card (one `alpaca` verb, one event), and
the projection re-renders. You do not fix a wrong statement on a wiki page by editing the page;
you write the correcting assertion into the record, and the page re-renders over it. The
rendered page is downstream of the record at every step, so editing it in place is either
erased on the next render or, worse, left to drift out of agreement with the truth.

This ruling is recorded as a decision page (`alpaca.freshness.record_ruling`) so it is durable and
resolvable, and the spec patch queue points at that decision page.

## How the ruling is enforced

The projection freshness gate (`alpaca/freshness.py`, `freshness.check`) proves it mechanically.
For every projection that exists on disk it regenerates the projection in memory from the
record - the regeneration is the trusted source, because disk never self-reports its own
freshness - and diffs that render against the disk bytes.

- A hand-edited or stale projection is a finding that names the file and the first differing
  line. `freshness.verdict_of` folds any finding to a non-PASS verdict.
- Every phase door (`alpaca/phase/doors.py`) composes the gate as a link, so a door never opens
  over a stale projection.
- The volatile lines - the generation stamp and any recorded duration, which move with the
  wall clock and carry no assertion - are declared in one place (`freshness.VOLATILE_LINES`)
  and are excluded from both the diff and every content hash. A stamp that ticks is not a
  hand-edit, and the gate does not confuse the two.

The rule in one line: the record is the only truth, the page is a rendering, and a correction
targets the assertion rather than the rendered page.
