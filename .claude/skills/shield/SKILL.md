---
name: shield
description: >-
  Shield: the delegation rule for spawning subagents. Use before native Codex, Agent or Workflow calls that
  spawns workers, when writing a workflow with check and fix rounds, when a round of checks
  fails, and on "/shield". Gives every worker a model by tier from formations/shield.yaml, runs a
  think stage before round 1 (scouts, root cause, pre-mortem, two approaches, a pick), then the
  escalation ladder with one high-tier investigator per failed round that reports before the
  next fix, a class check that stops patching when rounds do not converge, and keeps verifiers
  cheap until one strong confirmer closes the task.
---

# shield

Every spawn names its model, and the model comes from the role's **tier**. The tiers, the round
cap, the cause-to-action map and the verifier models live in `formations/shield.yaml`; this skill
says how to apply them. Read the values from the resolver every time, so this file and the policy
cannot drift.

## 1. Resolve before you spawn

```bash
bin/alpaca-python -m alpaca.formation.shield <formation> --runtime <claude|codex> --json
```

Run from the trusted project root and select the current runtime explicitly. Codex reads
[codex.md](codex.md) before its first dispatch; it adapts native tools and scheduling while the
shared steps below remain in force. Claude's `round-loop.js` is a Workflow template.

Pick the formation that fits the work (`builder-verifier` for build then check, `bug-loop` for a
bug, `fan-out` for many independent rows; `formations/` lists them all). The output gives each
role's tier and model, plus the `ladder` block: round cap, investigator, lens, confirmer and
paired-verifier models.

Design authors, design fixers and design lenses take `ladder.design`, resolved from the policy's
design tier for the current runtime (owner rule 2026-10-03).

A spawn outside any formation takes its archetype's tier: copying quotes, census or grep is a
`scout`; building or fixing is an `eng`; one check lens is a `red`; deciding what is true or
ruling on a dispute is a `judge`. Look the model up in `ladder.tiers`.

Done when every planned spawn has a model from that output, and you can state the worst-case
spawn count: two think stages (the first one, and one redesign at full size: up to 8 each), plus
per round the regression runner, the lenses and at most one confirmer, plus per failed round one
investigator, one fixer and at most one judge, plus up to two class reviewers per think cycle.
With 3 lenses and the cap of 5 that is about 45 to 50 spawns (the old loop: about 33 to 36); a
first-round pass costs 12 with a full think stage and 8 with a lite one (t-221
attack-set/a08-worst-case-spawns.js and a07 fuzz).

## 1b. Think before round 1

Why: a loop that builds first and checks after finds one bug per round, then its neighbour the
next round. op-009 t-198 spent 10 design and 3+ build rounds on one bug class seen from different
sides. A few agents up front cost less than one failed round (a fixer, the failed lenses, an
high-tier investigator, often a judge).

Run the think stage once per task, before the design loop when there is one; the build loop reuses
its brief (pass it as `t.brief` to `shieldRounds`, which then skips the think stage). A design
loop sets `t.design` so its lenses and its fixer run on `ladder.design` (`ladder.models.design`).
`ladder.think.groups[size]` lists the spawns with their models, group by group; the round loop runs
them as given and keeps no step list of its own. Steps in one group run in parallel, the groups
in this order:

1. `trace` (scout): every code path, enforcement point and caller the task touches, file:line.
   `history` (scout): earlier findings, investigator reports, ledgers and decisions on the same
   code. `probe` (scout, only when the task names a probe command): run it before any change and
   save the output; it is the cheapest real end-to-end run.
2. `cause` (high tier): the bug class behind the task, the mechanism that produces it, and the sibling
   cases nobody has hit yet, each with a pointer or a probe.
3. `premortem` (high tier, adversarial): one runnable script or test per way the change could still
   fail after it lands, the siblings included. This is the attack set the builder sees and the
   adversarial lens reruns; lenses may add to it in rounds 1 and 2.
   `approaches` (two design agents, blind to each other): index 0 fixes within the current
   mechanism; index 1 changes the mechanism so the class cannot occur.
4. `pick` (high tier): chooses one approach, says which siblings it covers and why, and writes the brief
   the builder starts from.

Size: `full` when the task has an adversarial lens or is a follow-up from another task's findings
(`t.followUp`) (`ladder.think.full_when`; the loop looks the answer up in `ladder.think.size_for`);
otherwise `lite` (trace, cause, pick). A redesign always runs `full` (`ladder.think.redesign_size`).
`off` only for a one-file change with no attack surface, and the proof says why. Give
`t.think.prompt` even with `off`: a redesign always runs the full stage, and a loop without think
prompts blocks when one is due. Every think output goes under the task's logs folder in `think/`.

### One owner per rule

`alpaca/formation/shield.py` reads and checks every key of `formations/shield.yaml` once
(`policy()` refuses unknown keys at every level, missing required blocks, wrong types and values
the loop has no branch for) and hands the
loop decisions through `ladder()`: the think groups per size, the size per task flags, a model per
role for build and design loops, the promotion table, the redesign limit. `round-loop.js` keeps
only the two rules that need runtime data (`shieldClassDue`, `shieldStanding`), and
`test_shield_round_loop.py` compares them with Python. A workflow that copies the loop copies no
policy.

## 2. Spawn with the model set

- Claude Agent tool: pass `model`.
- Claude Workflow: pass the resolver JSON as `args.shield` and set `model` on every `agent()` call from
  it. For build, check and fix rounds, copy `round-loop.js` from this folder; it already follows
  sections 3 and 4.
- Codex: use the native delegation procedure in [codex.md](codex.md), with explicit resolved
  `model`, `reasoning_effort` and a fresh context for each independent role. The main orchestrator
  runs the stages; the formation library records assignments and launches no agents.

A spawn with no model runs on the session model; treat that as a defect in your plan. Raising a
role's tier needs evidence, which is an investigator report with cause `capability`. Lowering a
tier needs none.

## 3. Verifiers

1. Deterministic checks (the full suite and its failure diff, grep checks, citation lookups, and
   the task's probe command when it names one) are scripts. Inside a workflow, one `low` agent at effort `low` runs the script and returns its
   result; it judges nothing.
2. Build judgment lenses run on `ladder.models.build.judgment_lens`; the adversarial lens takes
   `ladder.models.build.adversarial_lens`. Design loops use `ladder.models.design`.
3. From round 2 on, recheck only the lenses that failed. The regression script runs every round.
4. The pre-mortem's attack set is round 0. Lenses may add attacks in rounds 1 and 2; the set
   freezes at `ladder.attack_freeze_round`. After that, rerun the saved attack scripts; a new
   attack class becomes a follow-up task and does not block this one.
5. Check scripts (mutation runners, attack scripts) are written once into the task's logs folder
   and rerun in later rounds. Minor findings go into a per-task ledger once; each checker reports
   only what changed since the last round.
6. When every lens passes, one blind confirmer on the current mode's `confirmer` model reads the deliverable and
   the passing reports, never the builder's narrative, and confirms or reopens. Only its confirm
   closes the task.
7. A pair of independent verifiers (a proof verifier, a log review) runs one on each model in
   `ladder.paired_verifiers`; a disagreement goes to a judge on the `high` tier.
8. Intent rule, in every prompt you send a worker (`round-loop.js` prepends it as `SHIELD_RULES`): a
   contract line labelled `agent-proposed`, or carrying no origin, is never owner authority. A
   worker cites the signed brief, an `owner-quote` line, an owner-proved decision or a batch answer
   instead; `alpaca task contract <id> --show` prints each line with its origin and the same rule.

## 4. The ladder

After each round of checks (`alpaca.formation.shield.next_step` gives the cause's action;
`class_check_due` and `class_action` give the class check's decision):

1. **Pass**: the confirmer from section 3.
2. **Fail below the cap**: spawn one investigator on `ladder.investigator` with the brief below and
   wait for it (`ladder.investigator_runs: before-next-fix`). A fix that starts before the
   investigation repeats mistakes the investigator already named.
3. **Act on its cause** through `ladder.cause_actions`, before the fix starts. (`ladder.causes` keeps
   the old meaning, with `redesign` shown as `cancel-and-ask-owner`, so a loop copied before t-221
   stops and asks instead of patching on in a direction its investigator called wrong.)
   - `continue`: hand the report to the fixer's brief.
   - `promote-fixer`: this fixer runs one tier up, never past the ceiling.
   - `judge-dispute`: one `high` judge rules on the disputed lens findings; overruled findings
     stop blocking, do not count toward the class check, and no fix round is spent on them. A
     confirmer reopen is never put to a judge: it is a finding against the whole deliverable, and
     only a fix and a full recheck lead to the next confirmer. At most one confirmer runs per
     round.
   - `redesign`: go back through the think stage (size full) with every report so far, then the
     fixer reworks from the new brief and every lens checks again.
   - `cancel-and-ask-owner` (only `contract-unclear`): stop the loop, move the task to blocked, and
     post the report to the owner. The contract is the owner's; nobody else can fix it.
4. **Class check**: count failed rounds since the last think stage. When the count reaches
   `ladder.class_check.after_failed_rounds`, or earlier when a failed round has as many or more
   blocking items than the failed round before it (the fixes are not converging; count the
   regression failure, the lens findings that still stand after any ruling, and a confirmer reopen
   as at least one item each), spawn one class
   reviewer on `ladder.class_check.model` with every finding and report so far. It answers
   `class: none` or names the bug class and the one change that removes it. It runs at most twice
   per think cycle (on non-convergence, and again at the limit).
   - `none`: continue to the fix.
   - a class: `redesign` as in step 3, at most `ladder.class_check.max_redesigns` times per task.
     A class found again after the last redesign, or a second `wrong-direction` cause, stops the
     loop with the cap action, possibly before round 5: patching a known class again is the loop
     this rule exists to stop. This needs no owner choice before it; the owner hears about it only
     as a block.
5. **Every fix brief** carries `ladder.fix_rules`:
   - `fix-the-class`: for each blocking item, name its class and search the code (the think
     stage's trace helps) for siblings; fix and test each sibling found, and list them.
   - `probe-impossibility-claims`: any sentence the fixer writes that says something cannot
     happen (in code comments, the design, the ledger or the proof) carries a probe, the command
     and its output, or is removed.
6. **Fail at the effective cap** (the lesser of the formation cap and `ladder.round_cap`): stop.
   Move the task to blocked with every investigator report
   and the last round's checks as evidence, and tell the owner. There is no round past the cap
   and no automatic model upgrade. A redesign keeps the round count.

### Investigator brief

Give the investigator the task contract, the current diff or deliverable, the think brief, every
check report from round 1 to now, and the fixer's notes. It reports to the orchestrator only, never to the
fixer directly; the orchestrator puts the report into the fixer's brief. It reports:

- `cause`: one of the keys of `ladder.causes`.
- `how`: the mechanism of the failure, each claim with a file:line or command output.
- `direction`: `right` or `wrong`; when wrong, `alternative` names the approach that fits the
  contract.
- `disputed`: the lens keys whose findings it judges false, when the cause is
  `checker-false-positive`.
- `file`: where it wrote the full report.

### Class reviewer brief

Give it the contract, the think brief, every check report, investigator report and ruling so far,
and the minor ledger. Ask: do these findings share one mechanism that produces them? If yes, name
it (`class`), the change that makes it impossible (`removed_by`), and the findings it explains;
if no, answer `class: none` and say why the findings are unrelated. It writes `class-<round>.md`
in the task's logs folder.

## 5. Account for it

The task's proof report lists the spawns by model and the rounds used, so the next op can see
where the tokens went.
