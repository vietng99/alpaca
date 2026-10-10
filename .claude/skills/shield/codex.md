# Native Codex execution

Read this with [SKILL.md](SKILL.md). It adapts the shared procedure to Codex's tools. Resolve
policy from the trusted project root before dispatch:

```sh
bin/alpaca-python -m alpaca.formation.shield builder-verifier --runtime codex --json
```

Choose the actual formation in place of `builder-verifier`. Save its JSON in the task's logs
under `.alpaca/`. Check `runtime` is `codex` and every planned model is in the native tool's
current allowlist. A missing model or delegation tool is a reported blocker. Keep the owner's
selected mapping; do not substitute a model silently. The `session` role is the main chat and
is never spawned.

## Native calls

The main orchestrator calls `collaboration.spawn_agent` directly for each seat. These tools
are not available inside `functions.exec` or a shell. Use the native tool schema exposed in the
current session; Claude's `Workflow`, `agent()` and `parallel()` are not Codex tools.

Supply these arguments to each spawn:

- `task_name`: a distinct role, stage and round name.
- `message`: the charter, source authority, input artifact paths, allowed read/write scope,
  output path and required result fields. Include the intent rule from shared section 3.8.
- `model`: the exact model from the resolved formation role, think step or current mode's
  ladder model. Build fixers use their formation tier and any recorded promotion. Design work
  uses `ladder.models.design`.
- `reasoning_effort`: `medium` for ordinary low/mid work, `high` for high/top judgment and design,
  or `low` for the mechanical regression runner. An explicit owner effort choice takes precedence.
- `fork_turns: "none"`: supply the needed artifacts in the brief. Model overrides require a
  clean or limited fork; blind roles always use a clean fork.

Use fresh agents for independent approaches, lenses and confirmers. Give blind lenses the
deliverable and requirements without the builder's narrative or sibling conclusions. The
confirmer may read passing check reports as specified by the shared procedure. State write
fences explicitly; a clean context does not isolate the shared filesystem. Independent contexts
using models from one provider are not independent model families.

Save each returned agent identity alongside its planned role, model, effort and report path.
`collaboration.wait_agent` and completion messages report progress; read the output artifact
before deriving the next action. Bound waits so the owner receives progress updates. Use
`collaboration.send_message` for corrections to an ordinary worker, `followup_task` for ordinary
continued work, and `interrupt_agent` to stop a worker. None substitutes for a fresh blind seat.
An assignment record in `alpaca.formation.dispatch` is not evidence that a native call ran.

## Run the shared stages

Keep a task state file under `.alpaca/` with the resolver snapshot, current round, mode, think
brief, pending agents, failed lenses, investigator reports, class-review history, redesign count,
fixer tier and final verdict. Resume from the state and existing agents before dispatching again.

1. Choose the think size using `ladder.think.size_for` and the task's adversarial/follow-up flags,
   or the shared skill's documented `off` exception. Iterate `ladder.think.groups[size]` in order.
   Skip a `needs_probe` step only when the task has no probe. Finish every seat in a group before
   advancing to the next group. Reuse the completed design think brief for the build loop.
2. Check available native worker capacity before each group. The live limit includes the main
   orchestrator. When a group exceeds capacity, run bounded batches in that same logical group;
   keep every seat and preserve blindness across batches. Record the adaptation. Do not claim
   that sequential batches ran simultaneously.
3. After implementation, follow shared sections 3 and 4 using `ladder.models[mode]`,
   `ladder.cause_actions` and the effective cap, the lesser of the selected formation's cap and
   `ladder.round_cap`. Run regression each round and the required lenses on frozen inputs. Read
   the reports before calling the next role; changed inputs invalidate affected checks.
4. On failure below the cap, finish the investigator before composing the next fix brief.
   Feed its report and `ladder.fix_rules` to the fixer. Use the existing Python helpers
   `next_step`, `class_check_due`, `class_action`, `max_redesigns` and `promote` in
   `alpaca.formation.shield` when determining transitions; pass the actual task state and project
   root. Read `cause_actions`, not its legacy `causes` view. Keep the attack freeze, failed-lens
   rules, class-review limits and confirmer-reopen rules of the shared procedure.
5. A redesign runs `ladder.think.redesign_size`, preserves the total round count and rechecks
   every lens. A confirmer reopen requires a fix and full recheck. Stop at the effective cap or
   an owner-only contract gate with the evidence. Preserve any existing spending gates.
6. After all checks pass, dispatch the current mode's blind confirmer. Record completion only
   after reading its confirmation and satisfying the task's evidence/proof requirements.

`round-loop.js` remains the Claude Workflow reference. Its scenario tests check reference
behavior; they do not prove execution of a native Codex failure or redesign branch. Report which
native stages actually ran, actual spawns by model and rounds used, without inventing token or
cost measurements.

## Research jobs and discovery

For an engine-issued research job, the autoresearch protocol owns dispatch: pass its
`spawn_prompt` exactly, without prepending a second generic brief, and use its resolved model.
Follow its job/group/nonce submission and fresh-agent rules; ordinary-worker messaging above
does not permit changing or reusing a research job's agent. The engine owns the verifier ladder
for that job. Shield model resolution alone does not verify research transcript judging, rate
cards, accounting or the full autoresearch run.

The regular project entry `.agents/skills/shield/SKILL.md` points to this directory. It needs no
global installation or second copy under `.codex/skills`. Validate both skill entrypoints and
their relative links. A running session may retain its prior skill catalog; verify fresh-session
discovery when the runtime exposes it, otherwise report that loader recognition is unverified.
