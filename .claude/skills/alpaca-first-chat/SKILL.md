---
name: alpaca-first-chat
description: >-
  First chat on an Alpaca project that is not onboarded yet. Use at the very start of work on
  a repo that runs under Alpaca: it asks who is working, what is being worked on, the open
  tasks, and whether to enable the plain-writing preset, then hands off to alpaca-onboard.
  Triggers on "/alpaca-first-chat", "start alpaca", "set up the harness", or a SessionStart
  that reports the project is not onboarded. (Not the `alpaca intake` verb, which turns a spec
  and a runbook into checklist rows.)
---

# alpaca-first-chat

The first-chat door. Alpaca never opens an op on its own and never guesses who is at the
keyboard. This skill gathers the four facts the record needs before any work is
claimed, then hands off to `alpaca-onboard` to write them once.

## When to use

- A fresh Alpaca checkout whose `RESUME.md` or SessionStart hook reports it is not
  onboarded.
- The owner types `/alpaca-first-chat` or asks to set the harness up.

## What it asks

1. Who is working on this (the actor id the record attributes rows to).
2. What is being worked on (the one line that becomes the first op's intent).
3. The open tasks, if any are already known.
4. Whether to enable the plain-writing preset (the `sam` shipped mechanism).

## What it does NOT do

- It does not run any `alpaca` write verb itself. The single write is `alpaca onboard`,
  owned by `alpaca-onboard`.
- It does not open an op. An op opens only from a stated intent, by the owner.

## Hand-off

Once the four facts are in hand, invoke `alpaca-onboard` with them. After onboarding,
run `alpaca doctor` and read `RESUME.md`.
