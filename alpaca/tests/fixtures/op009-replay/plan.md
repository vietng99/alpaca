The plan is one op with 6 steps (0-5) and 4 review points for you. It takes roughly 3-4 weeks at L2 pace, which is a rough estimate. Nothing is built yet.

| # | Step | Output | Time | Your gate |
|---|---|---|---|---|
| 0 | Open op | op plus tasks grouped by workstream, prerequisites recorded (rule 7) | 15 min | - |
| 1 | Study | report: each FeSens and HWE-Bench piece marked take / adapt / skip / Alpaca already has, cited to code | 1 day | G1 review |
| 2 | Spec | spec-kit spec, runbook, `alpaca intake` rows (Alpaca's own path) | 1 day | G2 sign |
| 3 | Build | trust layer, loop engine, operator surface (below) | 2-3 weeks | each real edit needs your OK at L2 |
| 4 | Prove | end-to-end run on a toy pack, plus controls and cheat tests | 2 days | G3 accept |
| 5 | Ship | Linux installer, docs, release through op-004 | 1 day | G4 ship |

**Step 3 workstreams, in build order:**
1. **Trust layer (first, because every later task's proof goes through it):**
   - one worktree per attempt
   - tamper fingerprinting and a purge of ignored outputs
   - a mechanical gate (fails before, passes after, old tests pass, metric measured), built on `alpaca/contracts_runner.py`
   - the 2-agent verifier, which blocks `task move done`
2. **Loop engine (after the worktree and the gate):**
   - contract schema: pack default, task override with a recorded diff
   - loop runner plus a headless agent launcher on the current runtime (`claude -p` / `codex exec`)
   - one attempt = one task, plus the accept rule
   - stop rules: targets met, or N rounds then an extension card
   - lessons fed into the next round through `alpaca/lessons.py`
3. **Operator surface (after the engine):**
   - the `/autoresearch` skill
   - a contract interview and template for engineers
   - a hub chart of the metric per attempt

**Alpaca pieces to reuse** (the study confirms each):
- `profile.py`: the domain seam.
- `contracts_runner.py`: human-owned checks that agents cannot write.
- `taskcontract.py`: done bar and fail cases.
- `review.py`: owner cards.
- `gates/monotonicity.py`: blocks a check from being removed silently.
- `formation/`: multi-agent work over one op. It does not launch agents today.

**Step 4 pass bar:**
- A do-nothing agent and a random-edit agent never win.
- Planted cheats get caught: an edited protected file, a fake tool binary on PATH, and a test that already passes on the old code.
- Every accepted attempt has a sealed proof that both verifiers agreed on.

**Open questions, settled in steps 1-2:**
- Does the loop runner run as a background Alpaca process or inside the chat? This ties to L6. You invoking `/autoresearch` sets the level; the skill cannot raise it by itself (rule 6).
- Is the verifier built as a formation or as its own module?

**Not in this op:** the CPU design pack and the CPU DV pack. Each is its own later op.

Once you approve this plan, it becomes the signed spec and /flare is done.

Next: reply "go", or name a change. On "go", step 0 opens the op, which takes about 15 min.