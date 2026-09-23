---
name: nuclear
description: >-
  Invoke the NUCLEAR DOCTRINE (v3.6) - the self-executing adversarial audit/decide/fix
  formation - on a subject. Runs `audit`, `decide`, and `fix` (fix = verified copy +
  owner-signed cutover, doctrine §13). PROPORTIONATE BY CONSTRUCTION: every invocation
  starts with a G0 triage control pass (doctrine §0.2) that routes to T0 (the control
  pass IS the deliverable - no operation), T1 (a scoped strike, one round), or T2 (the
  full red/blue/eng+judge campaign with its own war-room). Use when the owner says "run
  nuclear doctrine on X", "nuclear this", or wants a rigorous, gated verdict. On T1/T2
  this skill self-instantiates: it creates (first use) or reuses the project's `nuclear/`
  folder, atomically allocates the next numbered `Operation-N-Codename` with its own
  crash-only war-room, calibrates the formation to the terrain WITHIN a declared budget
  (INV-C11), and drives each round as three main-chat-orchestrated stage-workflows (main
  chat = the sole-spawner Orchestrator). T1/T2 end in a verified `deliverables/REPORT.html`;
  T0 ends in `nuclear/triage/<slug>/control-pass.md`. The doctrine is the source of
  truth; this skill is the orchestrator front-end.
---

## Alpaca operator adapter

Keep operation artifacts inside `.alpaca/nuclear/`. Use the active operator native delegation tools for the named roles and frozen handoffs. References to Workflow, SubAgent, and SendMessage describe orchestration stages, not a dependency on a particular operator API. If independent agents are unavailable, report that limitation and do not claim independent concurrence. Actual owner authorization and host permissions remain binding.


# nuclear

Stand up and drive a **nuclear doctrine** engagement at the smallest tier that fits.
This skill is the front-end; the runbook, defaults, and invariants live in the doctrine -
read it, do not restate it.

**Read first (every invocation):** `NUCLEAR-DOCTRINE-v3.6.md` (co-located with this
skill; supersedes v3.5 - single source of truth). It ships every value.

## Architecture (do not violate)

- **G0 before force.** No operation exists until the triage control pass (doctrine §0.2)
  rules a tier. **Escalation needs evidence; de-escalation is free** (§2.8). T0 = most
  tasks: the control pass output is the deliverable, ledger row `op-declined`, done.
- **Main chat = the Orchestrator** - the *sole spawner* and *sole serial writer* of
  `war-log.jsonl`. It never judges/diagnoses/fixes primary content; all judgment
  lives in the adversarial ranks. It is cheaply re-entrant after a crash.
- **Chain of command** (doctrine §1.5): owner → orchestrator → stage-workflows → ranks.
  No lateral comms (frozen artifacts only - that is what keeps Red blind); no skip-level
  spawning; every escalation rung is granted by the level above, never self-granted.
  One bypass lane: the frame-critic reports raw to the owner.
- **Each round is THREE stage-workflows** (native operator delegation), split at the frozen
  handoffs - RECON → (main-chat firewall) → RED → FOLD - so main chat interposes at
  the point the bare-claims firewall must fire. The loop is main chat re-invoking the
  round per round and judging the fold between rounds (doctrine §1.5).
- **Consult inherited model** at the judge / verdict-panel / synthesis / report-judge ranks
  (using the inherited parent model); inherited model at control-pass/frame-critic/research/red/verify/report-write;
  inherited model at spotter/scouts (doctrine §1 model map).

## Procedure (the standup runbook - doctrine §0, §0.2, §11)

1. **Intake** - capture the 7 fields (mission · subject · fence · stance · done-bar ·
   standing orders · **budget**). Ask AT MOST one question, only for an unguessable
   high-stakes fork. Never re-explain the method. Stances: `audit` · `decide` · `fix`
   (load doctrine §13). Optional `scoped` modifier (doctrine §4).
2. **G0 triage** (doctrine §0.2) - one senior control pass DOES the task or reviews the
   subject; each gap = one bare claim naming the done-bar line it threatens + severity.
   Route by arithmetic: **0 gaps ≥ S_block → T0 (stop here; deliver the control pass;
   append `op-declined`)** · 1–3 named → **T1 strike** · >3 / class-shaped / fix-on-HIGH
   → **T2 formation**. Control-pass output feeds T1/T2 as recon input - never wasted.
3. **Allocate** (doctrine §0.5; T1/T2 only) - resolve `<project>` = git root; ensure
   `<project>/nuclear/` + historian exist (init under `.ledger.lock` if first run);
   compute `N`; **claim by number-only exclusive `mkdir nuclear/Operation-<N>`** (never
   `mkdir -p`; on collision recompute `N` and retry); write the STATE+CHARTER stub
   (with `op_key`, **tier, declared budget** - INV-C11), then **rename** to add the
   codename; append the `op-allocated` ledger row under the lock. If an existing OPEN
   op's `op_key` matches, **resume it**.
4. **Scaffold** the war-room (doctrine §5); publish `dashboard.html`; **arm the
   byte-sentinel** (war-room bytes vs budget, §7.5).
5. **Calibrate** (doctrine §8.5; T2 only - a T1 strike's map is the control pass) -
   Spotter terrain-read → `battle-map.md`; staff to the actual sectors **within the
   declared budget** (a plan whose arithmetic exceeds it is re-planned or owner-signed
   at the door, never discovered mid-flight); spawn the **frame-critic** (reads subject
   + mission ONLY; its `frame-check.md` goes raw to the owner, never folded); record
   deviations as amendments `A-nn`.
6. **Arm G1/G2/G3** (+ G2F/freeze sentinel for fix stance) under `gates/` with filled
   boolean conditions (doctrine §7.5). G1 opens Round 1.
7. **Run the loop** (doctrine §4) as stage-workflows - freeze each handoff, provenance +
   Specced/Built/Verified on every claim, verifier re-reads disk first-hand, blind-red
   per the **§9 early-stop floor** (2 agree → bank; split → stakes ceiling; HIGH at
   critical → always ceiling), judge **stamps `facing`** (MACHINERY-facing findings go
   to `nuclear/DOCTRINE-BACKLOG.md`, no in-op wave - §3 scope firewall), judge writes
   the recomputed diff into `phases/rN/round-result.json`, Orchestrator **GC-sweeps
   scratch after each fold** and checks spend (INV-C11). Loop until a §4 `stop_reason`
   re-derives - **`adequate` is first-class** (done-bar met = legitimate stop; no
   refuter-exhaustion required). T1 stops only `adequate` or `ESCALATE` (to a fresh T2).
   Write `CONVERGENCE-DECISION.md`.
8. **Report** (doctrine §6) - `deliverables/REPORT.html` under the write → check →
   verify → rewrite → judge loop. Deliver iff `TURN-IN`, OR loop==3 with a mandatory
   `REPORT-CAVEAT` block. Includes tier + G0 arithmetic, budget declared-vs-spent, and
   the frame-check verbatim. Always delivered, never silently withheld.
9. **Close** - **append** an `op-closed` historian row under `.ledger.lock`; append the
   `HISTORIAN.md` paragraph. G3 is the owner's countersign on the deliverable.

## Non-negotiables (doctrine, quick index)

| Rule | Where |
|---|---|
| No operation before G0 rules a tier; a gap that names no done-bar line is not a gap | §0.2 |
| Escalation carries the burden of proof; de-escalation is free; no self-promotion T1→T2 | §2.8, §1.5 |
| Every load-bearing move has an owning rank + a written artifact + a tally | §1 TOOTH 2 |
| Every refuter writes `verdicts/rN/red/*.json`; fold BLOCKED if absent (G2-b) | §1, §7.5 |
| Refuter early-stop: 2 agree → bank; split → stakes ceiling; HIGH@critical → ceiling | §9 |
| Stop only with `stop_reason` ∈ {adequate, oracle-dry, cap, scoped, owner-halt}; G2-a RE-DERIVES from disk | §4 |
| MACHINERY-facing findings → DOCTRINE-BACKLOG, no in-op wave (scope firewall) | §3 |
| Frame-critic reads subject+mission only; output raw to owner, never folded | §1 |
| Budget declared at intake; overrun = budget-halt, owner-signed extension only | §7 C11 |
| One sandbox worktree per class; scratch inside; GC after each fold; byte-sentinel armed | §5, §7.5 |
| No destructive commands in any subagent brief; cleanup = Orchestrator GC only | §8 |
| Frozen handoffs; the ENFORCING bareness firewall (reject-not-edit) - applies to G0 gaps too | §5.5 |
| Ledgers prev_hash-chained; `verify_chain()`; no placeholder hashes | §7 C6 |
| Gates = boolean DoD (count/pointer-backed) + bidirectional freeze sentinel | §7.5 |
| Calibrate-then-staff within budget; deviations = numbered amendments, never silent drift | §8.5 |
| Provenance-or-die · Specced≠Built≠Verified · universal negative-control | §2 |
| Write-fence: writes only under `<project>/nuclear/`; subject READ-ONLY | §8 |
| Machine proposes, owner signs; no verbal greenlight closes a gate | §8 |
| Every closing metric carries `sampled_from` + passes the wrong-side negative control | §7 C10 |
| Mandatory deliverable: T0 `control-pass.md` · T1/T2 verified `deliverables/REPORT.html` | §6, §9 |

## Done

T0: the control pass answers the mission and the `op-declined` ledger row points at it.
T1/T2: the operation stops under a re-derivable §4 `stop_reason` (`adequate` counts) and
turns in `deliverables/REPORT.html` (ruled `TURN-IN`, or delivered with a `REPORT-CAVEAT`
block at loop==3 - never silently withheld); the historian ledger gets an appended
`op-closed` row with the report pointer. A done-bar that proves unreachable on the chosen
route is a route finding, not a failure - escalate it through the adversarial-verify loop
rather than forcing a green.

## Cross-references

| Routes into | Invoked by |
|---|---|
| `NUCLEAR-DOCTRINE-v3.6.md` (runbook, defaults §9, invariants §7 incl C11 budget) | owner: "run nuclear doctrine on X" / "/nuclear" |
