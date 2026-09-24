## What happens on our side

When you send a runbook, we check it with the same code as `check_runbook.py`. Our intake then
reads the spec and the runbook together:

- each stage that has a `run` becomes one task, with the stage `inputs`, `outputs`, checks and
  `fails` as its contract; a stage that is only an owner gate becomes the approval alone, which
  is why it may not list checks (`CHECKS-WITHOUT-RUN`);
- each covered spec item becomes one checklist row, shown by the checks that cover it; a row
  covered only by an owner gate is closed by a person's review;
- each owner gate becomes a decision a person records; no agent moves it.

Our runner runs the stages in file order, applies the checks and the retry rules as this
document describes, and keeps the evidence of every attempt.
