---
name: alpaca-onboard
description: >-
  Onboard an Alpaca project once, writing the first-chat facts into the record. Use
  after alpaca-first-chat has gathered who is working, what is being worked on, the open
  tasks, and the plain-writing choice. It runs "alpaca onboard ..." exactly once, then
  "alpaca doctor". Triggers on "/alpaca-onboard", "onboard this repo", or a hand-off from
  alpaca-first-chat.
---

# alpaca-onboard

The one-time write that turns the first-chat answers into the first record rows. It is the
only place `alpaca onboard` is run.

## Preconditions

- `alpaca-first-chat` has gathered the four facts: actor, the work line, known open tasks,
  and the plain-writing preset choice.
- The project is not already onboarded (a second run is refused by the record).

## What it does

1. Runs `alpaca onboard` once with the gathered facts. `alpaca` is the sole writer of
   `.alpaca/alpaca.db`; this appends the onboarding events inside one transaction.
2. Runs `alpaca doctor` and reports the verdict (0 ok, 1 warn, 2 error).
3. Reads `RESUME.md` back so the owner sees the rendered projection, never the DB.

## What it does NOT do

- It does not hand-edit `RESUME.md` or `analytics/`; those are projections rendered
  by `alpaca`, never edited by hand.
- It does not open an op. Opening an op is `alpaca-op`, from a stated intent.

## After

Hand off to `alpaca-op` when the owner is ready to open the first op.
