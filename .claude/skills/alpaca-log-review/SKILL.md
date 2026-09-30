---
name: alpaca-log-review
description: >-
  Review a finished run's captured log through an independent subagent, then record the main
  session's GO or NO-GO. Use after every run a domain profile captures (PASS runs included), on
  resume when runs wait for review, and for "/alpaca-log-review <ref>", "review log <ref>" or
  "analyze log <ref>". The reviewer explains the result with checked quotes; the main session
  decides.
---

# alpaca-log-review

A run has three separate answers, and this skill keeps them apart:

- **Result**: PASS, FAIL, BLOCKED or INTERRUPTED, from the profile's grader. A review never
  changes it.
- **Review**: pending, reviewing, reviewed, failed or stale; `waiting` while the run is still
  running and `not-applicable` for a run that captures no log. A review whose submit returns
  PASS has quotes that match the log bytes; that is not the run passing.
- **Main decision**: GO or NO-GO, recorded by the main session after it reads the review.

The mechanism is `alpaca/runlog/reviewflow.py`. A domain profile wires it to its runs and
exposes commands for each step (for example `<profile> review assign`); the profile's own
instructions name them. Each step below names the `reviewflow` call the command makes. If the
profile exposes no review commands, it has not adopted this flow: say so and stop.

## Main session

1. **Find the work.** After each run finishes, and on every resume, list what waits
   (`reviewflow.pending`). Review PASS runs too. Poll a running job instead of starting
   another; a run still running is `waiting`.
2. **Assign.** Reserve a reviewer session id distinct from yours and assign the run
   (`reviewflow.assign`, with your session as parent). It records the assignment and a 30
   minute lease and returns a brief; it does not launch anything. Do not assign a run whose
   reviewer is still working under a live lease. A decided run is reassigned only by the
   session that decided it or by a main session that took it over.
3. **Dispatch.** Start a fresh native subagent (Claude Code Agent/Task tool, Codex agent tool)
   with the brief, the reference, the assignment id, its session id, the project root and
   the "Reviewer" section of this skill. Give it the raw evidence, never your diagnosis or an
   earlier review's conclusion.
4. **Read.** When it returns a review id, read the review with its At a glance block
   (`reviewflow.glance`) and open the log lines it cites. Resolve warnings, disputed findings
   and anything uncertain before deciding.
5. **Decide.** Record GO or NO-GO with a rationale that cites the evidence
   (`reviewflow.decide`). GO needs a recorded PASS, the latest intact assigned review with
   outcome `matches-verdict`, no disputed finding, and the profile's own current-evidence
   check. Use NO-GO for a failed, missing or contradictory result; when the review failed or
   the log is missing, NO-GO needs no review id.
6. **Advance or investigate.** After GO the profile's runner continues (it calls
   `reviewflow.require_go`). After NO-GO, investigate before a corrected retry within the
   owner's authorization and retry limits (the runner calls `reviewflow.require_retry`).
   Report the result, the short reason, your decision, the next action and the pointers.

Never ask the reviewer to decide its own review. Never record GO to get past a failure: a
result other than PASS cannot get GO.

## Reviewer (the subagent)

1. Use the session id and assignment id you were given for every command. Hold exactly one
   reference. Read the profile's review packet in full, then read more of the raw log if the
   excerpts do not settle the explanation. Do not read earlier reviews before you assess.
2. Write the review JSON in your scratch folder:
   - copy the reference and `log_sha256` from the packet; `reviewer`: your agent id;
   - `outcome`: `matches-verdict`, `disagrees-with-verdict` or `inconclusive`;
   - `quick_summary`: 20-360 characters, one or two sentences with the result and why. Keep
     the immediate stop apart from an underlying cause you are unsure of;
   - `next_check`: 4-600 characters, the engineer's first useful check;
   - `summary`: the fuller explanation, citing line numbers;
   - `findings`: 1 to 50 for a log with any text. Each quote is 4-400 characters copied
     exactly from lines `line` to `end_line` (at most 30 lines apart). `primary_finding`
     names the id of the finding that best shows the result (`f1`, `f2` ... in list order).
     An empty log takes zero findings and no `primary_finding`; never make up a quote;
   - any fields the profile's packet adds (a stage contract judgment, for example).
3. Submit it bound to the assignment (`reviewflow.submit`). On a refusal, reread the lines it
   names and fix only those claims. Three refused submits fail the assignment. A changed log
   needs a new assignment from the main session. Renew a long review before the lease ends
   (`reviewflow.renew`); if you cannot finish, fail it with the reason (`reviewflow.fail`).
4. Return the review id, the kept path, the short explanation and what stays uncertain. Do not
   run, fix or rerun the work, change inputs, or record GO or NO-GO.

## Recovery

- The record keeps assignments, refusals, failures and decisions; a disconnect loses none of
  them. On resume, list pending work first and reuse an accepted review.
- When the main session that assigned a finished review is gone, a new main session takes it
  over after the lease expires (`reviewflow.takeover`, with a reason). An unfinished expired
  review is reassigned instead.
- A changed log, a changed run record or an edited review file makes the review stale and
  clears the decision: assign a fresh review.
- A review submitted without an assignment (a manual `/alpaca-log-review`) stays readable but
  never satisfies the GO gate. For a manual request, follow the reviewer steps without an
  assignment and reply with the At a glance block and the review id.
- Engineers confirm or dispute findings afterwards (`alpaca.runlog.reviews.mark`); a disputed
  finding withdraws GO until it is resolved.
