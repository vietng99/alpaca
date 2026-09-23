---
name: flare
description: Turn a vague, blurry, or ambiguous request into a precise, signed-off spec BEFORE doing the work, by interviewing the user in adaptive multi-round bursts (4 questions per round via the interactive question tool), letting each round's follow-ups branch off the previous answers, then reading the final spec back for explicit sign-off (what was clear, what got added, what drifted, intent confirmed). Use when the user gives a long-but-fuzzy prompt, says "interview me", "flare this", "clarify before you build", or "/flare", or whenever a request is big enough that a wrong assumption would cost a rewrite. NOT for requests that are already crisp and small.
---

# flare - illuminate a vague request into a signed-off spec

A short prompt can still be vague, blurry, ambiguous. `flare` lights it up: it interviews the user in adaptive rounds until the intent is precise, then reads the spec back for sign-off before any real work starts. The point is to move the cost of a wrong assumption from "after the build" (a rewrite) to "during the interview" (one answer).

## When to use
- The user hands over a long-but-fuzzy prompt, or explicitly says "interview me", "flare this", "clarify first", or types `/flare`.
- The task is big enough that a wrong assumption would cost real rework (a feature, a design, a document, a plan, a refactor).
- **Do NOT use** for a crisp, small, unambiguous ask - just do it. If after round 1 everything is already clear, stop early and go to the readback.
- If the user types `/flare` on a request that is **already clear**, do not force rounds: skip the interview, go straight to the step-3 readback for a fast confirm, then build.

## The loop

Run interview rounds, then a spec readback, then hand off to the actual work.

### 1. Interview in rounds of 4

Use the interactive question tool (`AskUserQuestion`) - real selectable options, not a wall of prose questions. Per round:

- Ask **up to 4 questions**. Each question gets 2-4 concrete, mutually-distinct options. Always let the user pick "Other" / free-type (the tool provides this).
- Make options *opinionated*: put a recommended default first, labeled `(Recommended)`, with a one-line trade-off in each option's description. The user often clarifies fastest by rejecting a wrong-but-specific option.
- Cover the axes that actually move the build: scope, target/audience, format/output, constraints, success criteria, what to explicitly leave out.

### 2. Branch the next round off the answers

Do NOT ask a fixed script. Read the round's answers, then:

- If an answer opened a new fork, ask about that fork next round.
- If an answer closed off a whole area, skip every question you had queued for it.
- If an answer contradicts an earlier one, surface the conflict as the next question.

Keep running rounds while each new round still buys real clarity. Stop when a round would only produce trivia. No fixed round cap - let the signal decide - but the moment answers stop changing the plan, go to readback.

### 3. Read the spec back for sign-off

Before doing any real work, restate the final spec as **four explicit buckets** so the user can catch drift:

- **Clear from the start** - what the original request already pinned down.
- **Added during the interview** - decisions that did not exist in the first prompt.
- **Drifted / changed** - anything now different from what the first prompt implied, called out so silent scope-creep can't hide.
- **Intent confirmed** - the one-sentence "what we're actually building and why", in the user's own framing.

Then ask for a plain go / no-go **via the same interactive question tool** (`AskUserQuestion`, one question, not prose). Options: `Approve - build it`, `Adjust something`, `One more round`. On `Adjust`, fold the change and re-read the spec back; on `One more round`, return to step 1. Only start the real work on an explicit approve.

### 4. Hand off

On approval:
1. **Persist the signed spec.** Write the four buckets to a durable file - a scratchpad `flare-spec.md`, or a `project` memory if the work spans sessions - so the spec survives context summarization. Do NOT rely on chat context to hold it; a long build gets compacted and the spec would drift out, which is the exact blind-building failure flare exists to prevent.
2. **Build from the file.** Proceed to the work with that file as the brief; re-read it if the plan feels unmoored. If the harness has a plan mode or task list, seed it from the four buckets.

## Guarantees
- **No blind building** - real work starts only after an explicit sign-off.
- **Adaptive, not scripted** - each round is shaped by the last round's answers.
- **Drift is visible** - the readback's Added/Drifted buckets make scope-creep and misread intent impossible to miss before it costs anything.

## Anti-patterns
- Dumping 12 questions as prose in one message. Use the interactive tool, 4 at a time.
- Interviewing a request that was already clear. Skip to readback, or skip flare entirely.
- Starting work off an unconfirmed spec because the interview "felt done". The readback + sign-off is the whole point.
