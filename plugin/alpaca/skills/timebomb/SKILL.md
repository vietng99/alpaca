---
name: timebomb
description: Preserve the current Alpaca operation before context or usage limits interrupt work.
---

# Alpaca interruption checkpoint

Read exact remaining limits only when the operator exposes them. Never estimate missing token or reset counters. Write `bin/alpaca --session SESSION_ID session checkpoint --note "current step; evidence; exact next action"` before stopping. Read `bin/alpaca status` to identify an already-running job and its evidence. On return, use the same session ID and resume from the recorded state. Never launch a duplicate backend job because the chat was interrupted.

This package has no account-wide quota poller and does not read another application's private usage cache. Use an operator's native wait or continuation tool when available; persistent job state remains in this project.
