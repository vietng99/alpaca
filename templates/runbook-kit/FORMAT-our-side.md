## What happens on our side

When you send a runbook, we check it with the same code as `check_runbook.py`. Our intake then
reads the spec and the runbook together:

- each stage that has a `run` becomes one task, with the stage `inputs`, `outputs`, checks and
  `fails` as its contract; a stage that is only an owner gate becomes the approval alone, which
  is why it may not list checks (`CHECKS-WITHOUT-RUN`);
- each covered spec item becomes one checklist row, shown by the checks that cover it; a row
  covered only by an owner gate is closed by a person's review;
- each owner gate becomes a decision a person records; no agent moves it.
- each fail case with `detect` and `then` tells our runner what to do when it recognizes that
  failure; a recovery stage runs only when a fail case sends to it.

For a research runbook (`runbook: 3`) our intake does the same for its stages, adds one row for
each spec item its metrics, bug cases and parts cover, and takes its `research` block as the
contract every research run starts from. Our research engine runs the gate stages, the metrics
and the bug cases on each attempt; the contract is frozen when a run starts, so no agent can
change a target, a margin or a protected path during the run. Loosening a target, or setting
one on a metric that had none, needs the owner's recorded decision.

Our runner runs the stages in file order (a recovery stage only when a fail case sends to it),
applies the checks, the fail cases and the retry rules as this document describes, and keeps
the evidence of every attempt.
