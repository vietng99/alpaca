# File as truth

Every claim, verdict and order lives in a schema'd file; a return message carries a pointer, never the load-bearing content.

## The rule

The durable truth of the project is on disk in the record, not in a chat message. A message that reports a result carries a pointer to the file, row or command output that holds it, and the reader re-reads that pointer from disk rather than trusting the words. A claim with no resolvable pointer is not yet truth.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
`alpaca/db.py` (the append-only record) and `alpaca/gates/record_check.py`.
