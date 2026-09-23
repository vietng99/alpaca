---
name: sang
description: Use when a plan, proposal, design, argument, or decision must earn approval from a separate adversarial reviewer before it proceeds, including whether the work should exist at all. Triggers on /sang, "red-team this plan", "poke holes in this", "devil's advocate", "should we even build this", "make it earn approval", "adversarial review", or any request for a second agent to attack a plan's necessity and reasoning rather than polish its execution.
---

# sang - a proposal earns approval from a separate adversarial reviewer

A separate reviewer agent attacks the proposal, starting with whether it should exist. You (the orchestrator) answer each objection with evidence, a revision, or an explicit risk acceptance, and can be wrong about any of them, as can the reviewer. An **objection ledger** with stable IDs carries every objection to a closed status. The review stops at **review saturation** (another round could produce no objection able to change the decision) or at the round limit, with one of four verdicts. The verdict is advisory: it authorizes no edit, deploy, spend, or other external action.

Saturation is not proof of correctness. Agreement between two agents establishes nothing on its own; the ledger is the evidence that the review worked.

## Invocation

`/sang [--rounds N] <proposal, or path to it>`. Default limit: 3 rounds. Cost measured in testing: 45k to 75k tokens per reviewer round on a one-page packet, about 300k for a full 3-round run. The limit is a budget, never an approval: hitting it with open objections yields a non-Proceed verdict.

## Steps

### 1. Assemble the packet

One document, written to the scratchpad as `sang-packet.md`:

- **Proposal**: what will be done, as given.
- **Objective**: the problem it solves and how success is measured.
- **Constraints**: stated by the user. Kept as given.
- **Supporting reasoning**: the argument for it.
- **Evidence**: facts, measurements, paths the reviewer may inspect (read-only).
- **Decision-maker**: who can accept a risk (the user, unless they named someone).
- **Environment note**: any file, command, or log the reviewer may read to check a claim.

The packet carries the proposal as it was given, including any "this is already decided" language. It carries no opinion of yours about the proposal. The reviewer forms its round-1 assessment before it sees any response.

### 2. Dispatch the reviewer

Launch a **fresh** independent agent with the available operator delegation tool. If independent delegation is unavailable, report the limitation and conduct a self-review without claiming independence. Prompt = the full text of `reviewer-brief.md` in this skill's folder, then the packet, then `Round: 1 of N`. Keep the agent's handle; later rounds continue the same agent with the operator message tool so it keeps its own ledger.

No agent delegation available? Run the brief yourself in one pass, label every output `SELF-REVIEW (no separate agent)`, and say so in the final report. Never present a single assistant writing both sides as an independent review.

### 3. Answer round

Copy the reviewer's ledger into `sang-ledger.md` in the scratchpad (it must survive context compaction). For each objection choose exactly one response and write the reason beside it:

| Response | Use when | Effect on status |
|---|---|---|
| **Rebut** | You have evidence or an argument the objection did not account for. Cite it. Check a checkable fact now (read the file, run the query) instead of asserting it. | Reviewer decides: WITHDRAWN or stays OPEN |
| **Revise** | The objection is right and a change fixes it. Show the change. | RESOLVED after reviewer confirms the change addresses it without a new problem |
| **Accept** | The risk is real and the decision-maker takes it knowingly. Only the decision-maker can; if they are absent, record NEEDS-DECISION instead. | ACCEPTED (names who accepted) |
| **Concede** | The objection stands and no change fixes it. | OPEN, feeds the verdict |

Evaluate before you respond: the reviewer can be wrong. A confident objection with no target, reason, consequence, and resolution condition is sent back for those four fields, not answered.

### 4. Next round

the operator message tool to the same reviewer: the updated ledger with your responses, the current proposal (revised if it changed), and `Round: k of N`. The reviewer re-examines the **current** proposal against the **original** objective, updates every status with a reason, and may add objections only with a named trigger (a revision or newly inspected evidence).

### 5. Stop check (after every round)

Stop when any line matches:

- **Saturation**: every objection is WITHDRAWN, RESOLVED, or ACCEPTED, and this round added no blocking or material objection.
- **Fact-bound**: the remaining disagreement turns on an observable fact. Check it now if cheap; otherwise name the test and stop.
- **Priority-bound**: the remaining disagreement is a values or priority call. Name who must choose and stop.
- **Round limit** reached.

Repeating an argument is not resolution. An unresolved objection stays OPEN however repetitive the exchange became.

### 6. Verdict

Derived from the ledger, not from tone:

| Verdict | Ledger state |
|---|---|
| **Proceed** | Saturation reached. No open objection justifies blocking or changing the current proposal within the reviewed scope. |
| **Revise** | Open objections whose resolution condition is a specific change to the proposal. |
| **Reject** | An open blocking objection on necessity, on failing the objective, or on being worse than a viable alternative under the stated constraints, that survived rebuttal. |
| **Needs evidence or a decision** | An open material objection that turns on a fact nobody has, or on a priority nobody present can set. Name the test, measurement, or person. |

If your ledger reading and the reviewer's stated verdict differ, report both and the reason.

## Ledger

One row per objection, ID never reused:

```
| ID | Target | Kind | Severity | Status | Why status changed |
| O1 | "retries fix flakiness" | untested assumption | blocking | WITHDRAWN r2 | re-run data shows 70% pass, so retry(3) hides the race (reviewer kept) |
```

Kind: demonstrated / untested assumption / plausible risk / preference. Severity: blocking / material / minor. Status: OPEN / WITHDRAWN / RESOLVED / ACCEPTED / NEEDS-DECISION.

## Report

```
## Sang: <Proceed | Revise | Reject | Needs evidence or a decision>
Scope: <what was reviewed, under which constraints, rounds used, saturation reached or limit hit, separate agent or SELF-REVIEW>
Strongest remaining objections: <IDs + one line each, or "none open">
Changes made during review: <or "none">
Why the important objections closed: <ID: reason>
Still untested: <assumptions the review could not check>
Ledger: <full table>
```

Each report section is at most five lines; the ledger carries the detail. An approval states its scope: "Proceed with the revised plan under the stated constraints. O1 (necessity) and O3 (dependency) were withdrawn on evidence. O4 (capacity) is ACCEPTED by the owner pending the load test before full rollout."

## Red flags

- "Both agents agree, so the plan is good." Agreement is not evidence; the ledger is.
- The reviewer prompt lists the flaws you already suspect. That anchors the reviewer to your view; the brief plus the packet is the whole prompt.
- One round, then a report. Round-1 objections are untested until they meet a rebuttal.
- A ledger row vanished, or a resolved objection reappears without new evidence.
- A new bar appears after the old bar was met.
- You accepted every objection without checking one, or rejected every objection without conceding one.
- The round limit arrived and the report reads like an approval.
- The reviewer's alternative got less scrutiny than the proposal.
