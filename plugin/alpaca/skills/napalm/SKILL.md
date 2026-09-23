---
name: napalm
description: >-
  Invoke the NAPALM DOCTRINE (v1.1) - Nuclear's lighter BFS sibling, a self-executing
  council-of-experts sweep - on a WIDE subject. PROPORTIONATE BY CONSTRUCTION: every
  invocation opens with a G0 control-pass (§0) that reads breadth × stakes off the frozen
  gap list and routes on the 2×2 - narrow+cheap → T0 do-it (the control-pass IS the
  deliverable), narrow+expensive → decline to NUCLEAR (depth-shaped), wide+cheap → NAPALM
  sweep, wide+expensive → NAPALM first then escalate the hot spots UP. Use when the owner
  says "run napalm on X", "napalm this", or wants a WIDE surface swept many lenses at once
  at lower stakes. On a Napalm route this skill self-instantiates: it creates (first use)
  or reuses the project's `napalm/` root, convenes a council of distinct-lens seats
  calibrated to the actual surface WITHIN a declared budget (INV-C11), freezes a coverage
  map, runs ONE parallel BFS sweep + ONE round of different-lens cross-examination, folds
  (consensus / split / escalate), and hands hot spots UP to Nuclear as frozen packets.
  Napalm is BFS and CANNOT drill by construction; Nuclear is DFS. Napalm feeds Nuclear; it
  never becomes it. A Napalm run ends in one honest `deliverables/REPORT.html` with the
  `CONCURRED, not PROVEN` stamp on everything it certified on its own authority. The
  doctrine is the source of truth; this skill is the orchestrator front-end.
---

## Alpaca operator adapter

Keep operation artifacts inside `.alpaca/napalm/`. Use the active operator native delegation tools for the named roles and frozen handoffs. References to Workflow, SubAgent, and SendMessage describe orchestration stages, not a dependency on a particular operator API. If independent agents are unavailable, report that limitation and do not claim independent concurrence. Actual owner authorization and host permissions remain binding.


# napalm

Stand up and drive a **napalm doctrine** council sweep across a **wide** surface at the
smallest tier that fits. This skill is the front-end; the runbook, defaults, and
invariants live in the doctrine - read it, do not restate it.

**Read first (every invocation):** `NAPALM-DOCTRINE-v1.1.md` beside this file. The bundled adaptation contains the current procedure and no prior operation result or inherited owner grant.

**Parent doctrine:** Napalm inherits Nuclear's truth+verify kernel **by reference** and
escalates INTO it. When a finding needs drilling, read `../nuclear/NUCLEAR-DOCTRINE-v3.6.md` - the
escalation packet IS a Nuclear v3.6 control-pass gap set; the seam is version-pinned and
**fails closed** on mismatch (owner re-pins). **Nuclear is DFS, Napalm is BFS. Napalm
feeds Nuclear; it never becomes it.**

## Architecture (do not violate)

- **G0 before force.** No council exists until the control-pass rules the route from disk
  (doctrine §0). The Convener does the opening survey directly and lists bare gaps - exactly
  as Nuclear's control-pass does - then reads **breadth × stakes** off the 2×2. **A subject
  with one live facet is not a council** - convening one anyway is a proportionality error
  caught at N0. **Escalation carries the burden of proof; de-escalation is free** (LAW-RATCHET).
- **Convener = the Orchestrator** - the *sole spawner* and *sole serial writer* of the event
  log (Nuclear's orchestrator invariant, preserved). It never judges primary content; it may
  read a gate condition off a frozen artifact (count + compare, never classify).
- **Flat by design - THREE ranks + TWO roles** (doctrine §1): Convener (inherited model, solo) ·
  Council Experts (inherited model, 1/seat) · Synthesizer (inherited model). Roles: **cross-examiner** (every
  expert plays it for its paired peer) + **frame-critic** (one bounded seat, subject+mission
  ONLY, raw to owner). A fourth **rank** is a step back toward Nuclear (LAW-RATCHET);
  chain-composition is a **conditional PASS, not a rank and not a role**.
- **No lateral comms** - frozen artifacts only; seats do not read each other mid-sweep (this
  is what keeps the cross-exam honest). No skip-level spawning; every escalation rung is
  granted by the level above, never self-granted. One bypass lane: the **frame-critic reports
  raw to the owner**, never folded.
- **The run is ONE parallel BFS sweep + ONE round of different-lens cross-exam** - structurally
  fixed at 2 rounds (doctrine §8). Napalm **CANNOT drill**: a second cross-exam is an
  ESCALATION, not a round; cross-exam does not recurse.
- **Model map** (doctrine §1): **inherited model** where judgment/adversarial lens is load-bearing
  (Convener, experts, frame-critic); **inherited model** at the Synthesizer fold; **inherited model** permissible
  ONLY for a mechanical coverage-instrument seat that *measures*, never judges.

## Procedure (the standup runbook - doctrine §0, §2, §3)

1. **Intake** - capture the 6 fields (mission · subject **READ-ONLY** · fence · **domain**
   {security · code · decision · research · composite} · done-bar · **budget** {seats/bytes}).
   Ask AT MOST one question, only for an unguessable high-stakes fork. Never re-explain the
   method. §Defaults apply on owner silence (INV-C11 inherited).
2. **G0 control-pass** (doctrine §0) - the Convener surveys the subject directly and lists
   bare gaps naming the done-bar line each threatens + severity, then routes by the
   **breadth × stakes** 2×2 (LAW-COUNT - `expensive = ≥1 control-pass gap at HIGH OR a
   one-way-door line named`): **narrow+cheap → T0 do-it** (control-pass IS the deliverable -
   freeze it at `napalm/triage/<slug>/control-pass.md`) ·
   **narrow+expensive → decline to NUCLEAR** · **wide+cheap → NAPALM** · **wide+expensive →
   NAPALM first, escalate hot spots**. The same gap list feeds the sweep as *facets to cover*.
3. **Allocate** (Napalm route only) - resolve `<project>` = git root (if the subject is not in a
   git repo, its top-level directory; record the chosen root in `STATE.md`); ensure
   `<project>/napalm/LEDGER.md` + `HISTORIAN.md` exist (first run: the winner of an exclusive
   `mkdir napalm/` writes the LEDGER.md header + a `napalm-initialized` row with
   `prev_hash = sha256("")` + an empty HISTORIAN.md - Nuclear §0.5's prev_hash-chained event-log
   mirrored onto `napalm/`; **read `../nuclear/NUCLEAR-DOCTRINE-v3.6.md` §0.5 at standup**, not only on
   escalation). Compute `op_key = sha256(subject + "\n" + mission)`; **if the LEDGER shows an
   OPEN op - an `op-allocated` row with no matching `op-closed` - whose `op_key` matches, resume
   it and skip allocation.** Else claim the op-home **`napalm/<op-slug>/`** (`<op-slug>` =
   kebab-cased mission stub, ≤5 words; exclusive `mkdir`, never `-p`; on collision with a dir
   whose `op_key` differs, retry with a `-2`/`-3` suffix); write the `STATE.md` stub (`op_key`,
   **route + declared budget** - INV-C11); append the prev_hash-chained `op-allocated` LEDGER
   row under `napalm/.ledger.lock` (exclusive-create; release after the append).
4. **Scaffold** the op-home (doctrine §Kernel write-fence: writes ONLY under
   `napalm/<op-slug>/`; subject **READ-ONLY**); **arm the byte-sentinel** (op-home bytes vs
   `max_bytes`, checked at each fold).
5. **Convene the roster** (doctrine §2) - CLASSIFY the domain → pull the roster TEMPLATE →
   DECOMPOSE the actual surface into facets → BIND each template seat to facets that **exist**
   (a template seat with no facet is **not convened** - filler forbidden) → ADD swing seats
   only on demand, each **naming the facet the base roster missed** (LAW-RATCHET) → FREEZE the
   roster, stamping each seat the **four orthogonality teeth** (distinct discipline · disjoint
   owned facet · distinct hunted failure · distinct required evidence slice). 3 seats (small)
   to 7–9 (broad), within budget. Spawn the **frame-critic** (subject+mission only). Assign
   the §4 cross-exam pairing (paired against the peer whose objective function most conflicts).
6. **Freeze the COVERAGE MAP** (doctrine §3, LAW-PARTITION) - every facet owned by **exactly
   one seat**, `unfiled = 0` and `double-owned = 0`; cold/inventory-only surfaces **NAMED as
   inventory-only**, never quietly dropped; an **unowned partition is a finding, not a silent
   skip.** "Coverage-complete" is *counted*, not vibed.
7. **Run the sweep + cross-exam** (doctrine §3, §4) as the two structurally-fixed rounds -
   **all seats file in PARALLEL** (each = one Convener-spawned subagent, charter + subject
   READ-ONLY, writing its frozen memo under `napalm/<op-slug>/findings/`), each in its own
   deliverable shape (a seat with nothing real produces a **visibly empty path**, not a checkbox); optional **chain-composition PASS**
   re-reads MED findings for cross-seat kill-chains (a composed HIGH takes its one different-lens
   cross-examiner INSIDE the single §4 round). Then **ONE round of different-lens cross-exam**:
   every finding ≥ **S_block (MED-HIGH)** gets exactly ONE `lens(examiner) ≠ lens(author)`
   challenger who **re-resolves the pointer first-hand with its OWN evidence slice** (echoing
   the author's pointer is a **VOID concur that does not bank**, §4a), and for any HIGH **names
   the trigger or breaks one load-bearing link** (§4b). **≥1 cross-exam per sweep must challenge
   the DECOMPOSITION** (§4c - a threat *between* the frozen facets; a named one re-opens the
   coverage map). `LAW-NOLAUNDER`: an unresolvable pointer / unnameable trigger **auto-downgrades,
   never launders upward.** Every banked finding wears **`CONCURRED, not PROVEN`**.
8. **Fold** (doctrine §5, Synthesizer/inherited model) - stamp each finding `CONCURRED / SPLIT / ESCALATE`;
   write the **coverage + consensus tally** and the **DISSENT LEDGER verbatim** (SPLITs tagged
   escalate-to-Nuclear · seat-owned KILLs tagged documented-open - never averaged into a mush);
   freeze **escalation PACKETS** for every SPLIT / HIGH-DEEP / **MANDATORY-escalate-class** finding
   (§6, fail-closed at N1-d). **Napalm never self-promotes** - it hands a frozen packet to
   Nuclear's control-pass door (Nuclear re-derives the tier T1/T2 and stands up FRESH) and keeps
   sweeping in parallel; the proven verdict folds back and re-ranks.
9. **Converge + report** (doctrine §5, §7, §9) - stop only under a re-derivable `stop_reason`
   ∈ `{swept, swept-with-splits, scoped, owner-halt}` (no oracle-dry / cap - those are depth
   stops). Write `CONVERGENCE-DECISION` (stop_reason · coverage tally · dissent ledger ·
   escalation packets · **escalation fraction** · budget spent vs declared). **If escalation
   fraction > 40%** of block findings, print the **`G0 MIS-ROUTE`** banner - the surface is
   likely narrow+expensive or pervasively mandatory-class; owner re-decides the route. Deliver
   **one honest `deliverables/REPORT.html`** (doctrine §9's ten items, incl. the frame-check
   verbatim + its N1 disposition, and the `CONCURRED, not PROVEN` residual). Append the
   `op-closed` ledger + historian row. N2 is the owner's countersign.

## Non-negotiables (doctrine, quick index)

| Rule | Where |
|---|---|
| No council before G0 rules the route; a one-facet subject is a T0 do-it, not a council | §0, §8 |
| Route by breadth × stakes 2×2; expensive = ≥1 HIGH gap OR a one-way-door line named (countable) | §0 LAW-COUNT |
| Escalation carries the burden of proof; de-escalation is free; addition names what it uniquely owns | §8 LAW-RATCHET |
| Coverage map: every facet → exactly one seat, unfiled=0, double-owned=0; unowned = a finding | §3 LAW-PARTITION |
| Every seat = 4 orthogonality teeth; a seat may not cite a peer's evidence as primary; filler forbidden | §2 |
| Parallel BFS sweep; no lateral comms; a silent seat is NOT coverage; empty facet = visibly empty path | §3 |
| Every finding ≥ S_block (MED-HIGH) → exactly ONE different-lens cross-examiner | §4 |
| Cross-exam carries the challenger's OWN evidence slice; echoing the author's pointer = VOID concur | §4a |
| HIGH finding → challenger names the trigger OR breaks one load-bearing link (unpointed check ≠ rebuttal) | §4b |
| ≥1 cross-exam per sweep challenges the DECOMPOSITION (a between-facet threat re-opens the map) | §4c |
| Unresolvable pointer / unnameable trigger auto-downgrades, never launders upward | §Kernel LAW-NOLAUNDER |
| Every banked finding stamped `CONCURRED, not PROVEN`; Napalm never certifies the expensive verdicts | §6 LAW-AUTHORITY |
| Napalm CANNOT drill: 1 sweep + 1 cross-exam, structurally fixed; a 2nd cross-exam is an ESCALATION | §8 |
| SPLIT / HIGH-DEEP / MANDATORY-escalate class → frozen packet → Nuclear's door; Nuclear stands up fresh | §6 |
| MANDATORY-escalate is fail-closed at N1-d (RCE · authz bypass · tenant break · mass-exfil · … ) | §6 |
| Dissent is a DELIVERABLE - SPLITs + seat-owned KILLs ride verbatim, never averaged | §5 |
| Gates N0/N1/N2 are boolean, re-derived from disk (LAW-COUNT); fail-closed on unre-derivable | §7 |
| Napalm's OWN gate instruments obey the inward VOID-if-unmoved law (INV-C10-e inward, never executed) | §7 |
| Frame-critic reads subject+mission only; raw to owner, never folded; its objection carries an N1 disposition | §1, §7 N1-e |
| Budget declared at intake; byte-sentinel armed; overrun = owner-signed extension only | §Defaults, INV-C11 |
| Write-fence: writes ONLY under `<project>/napalm/<op-slug>/`; subject READ-ONLY; no destructive commands | §Kernel |
| Provenance-or-die · honest tags Specced≠Built≠Verified · code-ladder ceiling = Specced (Napalm never executes; `Built` only by relaying a cited resolvable static-check quote - reported, never certified; `Verified` is Nuclear's alone) | §Kernel, §6, §Defaults |
| Escalation fraction > 40% → `G0 MIS-ROUTE` banner; owner re-decides the route | §7 |
| Escalation packets target `../nuclear/NUCLEAR-DOCTRINE-v3.6.md`; version mismatch fails closed, owner re-pins | §6, §Honesty |

## Done

**T0** (narrow+cheap route): the control-pass answers the mission and IS the deliverable, frozen
at `napalm/triage/<slug>/control-pass.md` - no council, no allocation. **Napalm route:** the sweep stops under a re-derivable §7 `stop_reason`
(`swept` counts; `swept-with-splits` when ≥1 finding escalated) and turns in one honest
`deliverables/REPORT.html` - coverage map + per-seat findings (honest-tagged, never laundered)
+ cross-exam outcomes + the **DISSENT LEDGER** + the **frame-check verbatim** + the escalation
packets + the **`CONCURRED, not PROVEN`** residual + any `swept, not saturated` banner. The
ledger gets an appended `op-closed` row; N2 is the owner's countersign. **Coverage-complete
always means the DECLARED surface, not the universe** - the map's holes are the true residual,
named. A finding that needs drilling is **escalated to Nuclear, not forced green** - Napalm
surfaces splits, it does not settle them.

## Cross-references

| Routes into | Invoked by |
|---|---|
| `NAPALM-DOCTRINE-v1.1.md` (defaults, invariants, the ten-item report §9) | owner: "run napalm on X" / "napalm this" |
| a **Nuclear T1/T2 strike** at every escalation seam (§6) - the packet IS a Nuclear v3.6 control-pass gap set | Napalm hands a frozen hot spot UP; Nuclear stands up fresh + re-derives the tier |
| `../nuclear/NUCLEAR-DOCTRINE-v3.6.md` (the DFS parent; seam version-pinned, fails closed on mismatch) | G0 routing `narrow × expensive` → decline to Nuclear from the start |
| the sibling **nuclear** skill (`name: nuclear`; front-end for the DFS parent, invoked `run nuclear doctrine on X` - `nuclear/` is Nuclear's op-home, not a skill dir) | owner routes a depth-shaped subject to Nuclear instead |
