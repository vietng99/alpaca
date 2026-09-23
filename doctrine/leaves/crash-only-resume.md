# Crash-only resume

No in-memory state is assumed to survive; the run resumes from the record and a resume pad, never from a live process.

## The rule

A session can die at any point from a usage limit, an error, or a kill. Crash-only design writes every load-bearing step to the record before it acts, so a successor rebuilds the true state from disk and resumes without repeating a completed step or racing an open one.

## Instrument

The code is the mechanism; this leaf is the reason, so the two never drift. Enforced by
`alpaca/token.py` (completion token and resume check) and `alpaca/pad.py`.
