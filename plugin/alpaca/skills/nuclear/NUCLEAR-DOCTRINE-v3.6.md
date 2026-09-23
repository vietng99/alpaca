# Alpaca NUCLEAR doctrine

Adapted from the upstream doctrine identified by BUNDLE-MANIFEST.json. This copy contains no inherited owner grant or operation history. Use available native operator tools and the parent model unless the current owner selects another model. Bound staffing by available concurrency and the current authorized budget. Store all operation artifacts under `.alpaca/nuclear/`. Independent review must be reported as unavailable when separate reviewers cannot run.

## §0 - INVOCATION CONTRACT

**"Run nuclear doctrine on X"** = intake → **G0 triage (§0.2)** → route by tier → (T0: control pass IS the
deliverable, owner-countersigned, no op · T1: scoped strike, one round · T2: allocate the op + war-room (§0.5) →
calibrate (§8.5) → instantiate the formation (§1) with the §9 defaults → run the loop (§4) as stage-workflows
(§1.5)) → converge honestly (§4) → deliver a **verified HTML report** (§6). Owner silence = defaults. **No owner
turn is required before Round 1 or before the report is turned in.** (This clause is the whole runbook; the
v3.5 §11 step-list was a duplicate and is retired - §12.)

**Intake (seven fields; ask ≤1 question, only for an unguessable high-stakes fork):** mission · subject (READ-ONLY
unless stance `fix`) · fence · **stance** {`audit` · `decide` · `fix` (§13)} with optional **`scoped`** modifier
(a pre-declared bounded surface/round-budget, §4) · done-bar · standing orders · **budget** (agents/bytes/rounds;
§9 tier defaults apply if unstated - INV-C11).

**Standup (T2):** G0 → allocate (§0.5) → scaffold (§5) → calibrate + amendments (§8.5) → standing orders → arm
G1/G2/G3 + byte-sentinel + any freeze sentinel (§7.5) → run the loop → verified report (§6) → close (§0.5).
**Reasoning tier:** judge+red **high** (xhigh at `critical`); scouts **standard**. Crash-only from the first byte.

---

## §0.2 - G0 TRIAGE: proportionality is proven before force is raised

**No operation exists until G0 rules a tier.** The control pass replaces the never-run §14 "recommended" eval
with a mandatory, cheap, load-bearing entry ticket - and its output is real work, not overhead.

```
G0 TRIAGE (before §0.5 allocation) · auto+report:
  CONTROL PASS - one senior agent (inherited model), cap: 1 agent-run. It DOES the task (or reviews the subject
  directly), then writes  nuclear/triage/<slug>/control-pass.md :
    { task-shape · done-bar draft · deliverable-so-far pointer ·
      gap list: each gap = ONE bare claim (§5.5 firewall applies) naming
                (a) the done-bar line it threatens  (b) a severity (§3) }
  ROUTE (Orchestrator, pure arithmetic over the frozen control-pass.md):
    count(gaps >= S_block, done-bar-line-named)          [S_block = MED-HIGH, §3]
```

| Tier | Condition (re-derivable from `control-pass.md`) | Response |
|---|---|---|
| **T0 do-it** | 0 gaps ≥ S_block | **No allocation.** Control-pass output is the deliverable. Append LEDGER row `op-declined` + `control-pass.md` pointer, **owner-countersigned** - the one exit that spawns zero adversary carries the §8/G3 countersign: the owner reads `control-pass.md` and signs the decline (a verbal greenlight never closes it, §8). **T0 carries no ceremony** (§5.5). Done. |
| **T1 strike** | 1–3 gaps ≥ S_block, each done-bar-named | Scoped strike: 1 verifier + refuters per §9 early-stop **on those gaps only**, one round, no census, no class-critic. Stop = `adequate` or `ESCALATE`. |
| **T2 formation** | >3 gaps ≥ S_block, OR a gap is class-shaped (one mechanism, many sites), OR stance `fix` touching a HIGH subsystem | Full formation (§1). Control-pass becomes R1 recon input - nothing is wasted. |

**Anti-inflation tooth:** a gap that cannot name the done-bar line it threatens is **not a gap** - it is
advisory, and does not count toward tier routing. Gap claims pass the same bareness firewall as R1 claims (§5.5).
**Escalation needs evidence; de-escalation needs none** (§2 item 8). A T1 team never promotes itself (§1.5 ladder).
Gates re-derive, never trust a recorded label (**INV-C12**).

---

## §0.5 - SELF-INSTANTIATION: `nuclear/`, numbered operations, the historian

`<project>` = git root of the subject. All state under **`<project>/nuclear/`** (writes only here; the rest
READ-ONLY, §8).
```
<project>/nuclear/
  .ledger.lock
  LEDGER.md    historian: append-only event log; 8 columns | event | N | codename | UTC | subject | stance | note | prev_hash |
  HISTORIAN.md one paragraph per op, appended at close
  DOCTRINE-BACKLOG.md   machinery-facing findings parked by the scope firewall (§3) - one line each, append-only
  negative-controls/    banked once-per-instrument fail-on-demand transcripts (INV-C10-e)
  triage/<slug>/        G0 control passes (T0 leaves ONLY this + the owner-countersigned LEDGER row)
  Operation-<N>-<Codename>/   a full war-room (§5) - T1/T2 only
```
**First run:** the exclusive `mkdir nuclear/` is the init guard; the winner writes `LEDGER.md` (header +
`nuclear-initialized` row, `prev_hash = sha256("")`) + empty `HISTORIAN.md`. (Init is distinct from allocation:
T0 skips only allocation, so the tree still exists for the `op-declined` row.)

**Naming - `Operation-<N>-<Codename>`.** `<N>` = sequential per-project counter (ordering). **`<Codename>` is
EVOCATIVE, not descriptive** - an oblique, memorable word that *winks* at the mission: `Airport` (launch-readiness),
`Glass-Mirror` (self-audit), `Echo-Chamber` (auditing a self-audit), `Fission` (the doctrine splitting itself).
A flat label is a G1 calibration note. Owner-renamable at intake. T1 strikes are operations too (numbered,
ledgered) - just narrow ones.

**Atomic allocation:** `N = max(numeric prefix of Operation-<k>[-*] folders + op-allocated LEDGER rows) + 1`;
claim by number-only exclusive `mkdir nuclear/Operation-<N>` (never `-p`, never a dir-creating Write; recompute
`N` and retry on collision); write `STATE.md` stub + `CHARTER.md` (with `op_key = sha256(subject + "\n" +
mission)`, the codename, **the tier and its declared budget**); **rename** to add the codename; **append** the
chained `op-allocated` LEDGER row. A folder with no `CHARTER.md` = crash mid-claim = ABORTED, reclaim by number.

**Resume (exact key):** OPEN = an `op-allocated` row with no matching `op-closed`. An OPEN folder whose `op_key`
matches this request → resume (§5); else allocate. A stale OPEN op with a different key → offer the owner ABANDON
(append `op-abandoned`), never silently reuse. **Close** = append a chained `op-closed` row + the HISTORIAN
paragraph; never edit a row in place.

---

## §1 - FORMATION: model map, sizing, the rank that OWNS each move

**Model map** (rank floors) `[proven-at-N=1 · owner-overridable]`: inherited model = orchestrator/planner/judge/
verdict-panel/synthesis/report-judge (folds; never primary research) · inherited model = researchers/deep-auditors/red-
refuters/probe-workers/**pinner**/**integrator**/fixers/report-writer/report-checker/**control-pass**/
**frame-critic** (the truth-deciding floor) · inherited model = spotter/scouts/census (verbatim, zero judgment).

**Standing adversarial roles:** blind Red (refuters blind to each other + to Blue's narrative); a class-critic
(blind to the current finding set, emits candidates only - the judge computes new-vs-known, §4); and at T2 the
**frame-critic** (below). **The frame-critic reads its subject + mission ONLY** - the single statement of its
read-scope, cited wherever it recurs (§1.5, §9): the point is a head **outside the doctrine's frame**, reporting
raw to the owner and **never folded by the judge it critiques** (folding it would recreate the capture the role
exists to break).

**Blind-rank read-scope is enforced by the Orchestrator's brief, not mechanically** - a blind rank handed the
wrong artifact reads it; say so (INV-C8 honesty; §5.5, §10).

### TOOTH 2 - a rank OWNS each move + writes an ON-DISK artifact (round index `rN`; the judge refuses to fold a finding whose artifacts are absent)

| Owned move | Owner | Act | Artifact |
|---|---|---|---|
| **Control pass** (G0) | **Control** (inherited model, senior, solo) | do the task / review directly; list bare gaps | `nuclear/triage/<slug>/control-pass.md` |
| Map terrain | **Spotter** (inherited model) | sector the subject, rate HIGH/normal/cold | `battle-map.md` |
| Extract | **Scout** (inherited model, blind pair) | verbatim, zero judgment | `phases/rN/scouts/<sector>-{A,B}.jsonl` |
| Diagnose | **Researcher** (inherited model) | author each claim BARE at source | `phases/rN/claims.jsonl` |
| **Re-verify on disk first-hand** | **Verifier** (inherited model) | re-locate each claim's evidence itself, not echoing pointers | `verdicts/rN/<sector>.md` + tally |
| **Refute** | **Red** (inherited model, blind, §9 early-stop) | break each finding ≥ S_block | **one file per refuter** `verdicts/rN/red/<finding>.<n>.json` (`blind:true` + own evidence). **Absent ⇒ cannot fold.** |
| Judge / **new-vs-known diff** / **facing stamp** | **Judge** (inherited model) | fold; class+severity; **stamp `facing: SUBJECT\|MACHINERY`** (§3); write the recomputed diff | `verdicts/rN/master-assessment.md` + `phases/rN/round-result.json` |
| Emit candidates | **Class-critic** (inherited model, blind; T2 only) | sweep a NAMED surface, candidates only | `phases/rN/class-critic-<c>.md` |
| **Challenge the frame** | **Frame-critic** (inherited model, T2 only; read-scope per above) | rule: frame wrong? done-bar wrong? tier disproportionate? | `frame-check.md` - delivered RAW to the owner (fold-1 note + report appendix); **never folded** |
| **Pin** (fix stance) | **Pinner** (inherited model) | reproduce each target as a deterministic failing pin | `fixes/<class>/pin/` (no-repro → `fixes/unreproduced.md`) |
| **Integrate** (fix stance) | **Integrator** (inherited model) | derive `merge-order.md` by dependency-then-severity; merge serially; full regression after each | `fixes/merge-order.md` |
| Record | **Historian** (main chat) | sole serial writer | `war-log.jsonl` |

**Agent-return schema:** one war-log event `{actor, action, phase, subject, evidence[], note}` + a pointer;
never load-bearing content. Mid-round need for more eyes = a **request block** in the note; the Orchestrator
spawns a fresh instance next stage.

**Gate-runner carve-out:** the Orchestrator "never judges primary content" - BUT it MAY *read a gate condition
off a frozen artifact* the ranks already wrote (the judge's `round-result.json` recomputed diff and its counts,
the control-pass gap count, the spend counters) - it does NOT re-classify. The boundary is **read + count +
compare-equal, never classify or diagnose** (this is INV-C12 in the Orchestrator's hands).

**Sizing** - G0 tier first, then `base(target)` within T2; concurrency ≤16, waves beyond. **T1: ≤6 agents total,
1 round.** T2: small ≤2 files (R1 3–5, skip census, 1 critic) · medium ≤~15 files (R1 ~10, 1 critic) · large
whole tree (R1 ~27 = 10 census + 8 probe + 9 chain, 2 critics). stance: `audit`=base · `decide`=base−census ·
`fix`=base + 1 fixer per class (§13). stakes: `routine`/`elevated`/`critical` set the refuter **ceiling** -
§9 early-stop sets the floor: **run 2 blind refuters; agreement → bank the verdict; split → escalate to the
stakes ceiling.** Exception: HIGH findings at `critical` stakes always get the full ceiling (no early-stop).
HIGH subsystems rated at calibration, listed in CHARTER.

**Roster reads:** core = {this doctrine, CHARTER}; non-blind ranks also read STATE+PULSE. **Blind ranks (Red,
class-critic) read neither STATE nor PULSE** - only CHARTER + their frozen input + the READ-ONLY subject.

---

## §1.5 - ARCHITECTURE: main-chat Orchestrator; chain of command; each round = 3 stage-workflows

Substrate: a workflow agent cannot spawn. So **main chat = the Orchestrator** - sole spawner, sole serial writer
of `war-log.jsonl`, cheaply re-entrant after a crash; it never judges primary content (but may re-derive gate
conditions from artifacts, §1). **Each round = THREE stage-workflows split at the frozen handoffs:** (1) **RECON**
(spotter/scouts→researchers→verifiers) → main-chat **FIREWALL** (§5.5) → (2) **RED** (blind refuters per §9
early-stop, each writing its `red/*.json`; + class-critic) → (3) **FOLD** (inherited model judge folds, stamps facing,
writes the recomputed diff + counts to `round-result.json`, verifies G2-b red-file counts). The loop = main chat
re-invoking the round, reading `round-result.json`, deciding CONTINUE/CONVERGE (§4). Round state lives on disk -
crash-only.

**The chain of command (tiers set width, never wiring):**
```
OWNER      - signs; the only rank above the machine
ORCHESTRATOR (main chat) - sole spawner · sole war-log writer · reads gates, never judges
STAGE WORKFLOWS          - RECON → firewall → RED → FOLD
RANKS      - write artifacts; never talk laterally; never spawn
```
Two invariants make it a chain: **no lateral comms** (ranks communicate only through frozen artifacts - this is
what keeps Red blind) and **no skip-level spawning** (every unit traces to one spawn event in the war-log).
Downward orders are artifacts too (briefs, CHARTER, standing orders) - the Orchestrator never whispers an
instruction that is not on disk.

**Main chat spawns every rank of every stage.** The Orchestrator is the sole spawner at each frozen handoff;
"stage-workflow" names the **handoff boundary between rank groups**, not a self-spawning agent - a workflow agent
cannot spawn, so the boundary is exactly where main chat re-invokes, never where a rank promotes or spawns itself.

**The escalation ladder - every rung is a request the level ABOVE grants, never self-granted:**

| Rung | Raised by | Decided by | Evidence required |
|---|---|---|---|
| more eyes mid-round | any rank (request block) | Orchestrator | stated reason in the artifact |
| refuter 2 → stakes ceiling | Orchestrator (arithmetic: split vote) | Orchestrator | disagreement on disk |
| **tier T1 → T2** | T1 verifier/red: `ESCALATE` verdict | Orchestrator **re-derives** (counts gaps ≥ S_block in T1 artifacts) | named done-bar lines threatened |
| budget extension | Orchestrator (spend counter trips, INV-C11) | **Owner** - HALT until signed | spend ledger + why |
| fence / standing-order change | any | **Owner** - HARD-STOP amendment | A-nn + cost-of-not |
| frame wrong / wrong tier | frame-critic | **Owner directly** (the one bypass lane - an inspector general outside line command) | `frame-check.md` |
| sentinel trip | mechanical | **Owner** - HALT + stuck-report | signature diff |

**No self-promotion:** a T1 team never becomes the T2 team. T1 rules `ESCALATE`; the Orchestrator re-derives
from T1's artifacts; T2 stands up **fresh**, with T1 output as recon input. Never trust the label of the rank
that wants the promotion.

---

## §2 - METHOD

1. **Provenance-or-die** - every claim carries a resolvable pointer; unevidenced → discarded by the next rank.
2. **Specced ≠ Built ≠ Verified** - *Specced* = design on disk · *Built* = runs/static-checks pass · *Verified* =
   a behavioral probe passed. Stamp every claim; never launder a tag. **A required metric that is Specced but has
   no working instrument is SPECCED-ONLY and cannot close a gate.**
3. **Universal negative-control** - a guard is VERIFIED only if a sandboxed control makes it **fail on demand**
   (fault-injection on sandbox copies only). HIGH findings: the control is independently reproduced by a 2nd agent.
   **This law binds nuclear's OWN instruments too** - a gate whose count is unchanged with the measured side
   removed is VOID (§7 C10-e).
4. **Blind red waves** - refuters per §9 early-stop, refute-posture, blind, **each leaving an on-disk verdict
   file**. Judge folds: ≥majority-refute → DOWNGRADE/REFUTE; 0 → CONFIRM; mixed → CONFIRM-NARROWED. Weigh a
   reproduced refutation over vote count.
5. **Byte-exact chain verification** - recount/re-hash/re-read from disk yourself.
6. **PROBE-BLOCKED is honest, not a pass** - no silent caps.
7. **Boundary-provenance (INV-C10)** - a verdict must name which side of the trust boundary it was sampled
   from, and pass a wrong-side negative control. A metric identical when the measured side is off is VOID.
8. **Proportionality (the Hallmark rule).** Force follows evidence: **escalation carries the burden of
   proof; de-escalation is free.** Any rank may recommend less force with no burden; more force must name the
   done-bar lines at risk. A finding that cannot name the done-bar line it threatens is advisory - it draws no
   wave (§3). The machinery never audits itself inside a subject-op (§3 scope firewall). *(This is the sole
   canonical statement of the burden; §0.2 and §3 cite it, never restate it.)*

---

## §3 - SEVERITY + FINDINGS + CLASSES + the SCOPE FIREWALL

Severity = `max` of {impact, reachability, reversibility}: HIGH (wrong verdict / silent loss / safety) · MED-HIGH
(integrity hole, no wrong verdict) · MED (real gap) · LOW-MED/LOW (hygiene). **S_block = MED-HIGH**: ≥ gets the
refuter wave (§9 early-stop) + gates verify-exit; below = single vote. Boundary calls rated by 2 agents, round UP
on disagreement. **A finding ≥ S_block MUST name the done-bar line it threatens; one that cannot is advisory**
(logged, no wave, does not block). **Every confirmed finding gets a root-cause CLASS** (the mechanism, not the
symptom); two findings share a class iff one categorical fix prevents both. **Deliverables by CLASS - cure the
plague, not the instance.** Verdict: `VERIFIED-OK · DEFECT · UNUSED · SPECCED-ONLY · UNVERIFIABLE`. Red status:
`CONFIRMED · CONFIRMED-NARROWED · DOWNGRADED · REFUTED`.

**SCOPE FIREWALL (starves the echo chamber structurally).** At fold the judge stamps every finding
**`facing: SUBJECT | MACHINERY`**:

- **SUBJECT-facing** (threatens a done-bar line of THIS op) → normal pipeline: wave, fold, fix.
- **MACHINERY-facing** (a defect in nuclear's own gates/ledgers/provenance/firewall apparatus) → **no refuter
  wave in this op.** One line appended to `nuclear/DOCTRINE-BACKLOG.md` (event `finding-backlogged`); a separate
  doctrine-op cures them in batch (record the current operation separately). §6 L-forces
  the backlog count+pointer into the report and G3-b re-resolves it.

**The stamp is itself measured (NF-05, v3.6).** A `facing` stamp on any finding ≥ S_block **draws 2 raters,
rounding toward SUBJECT on disagreement** (the §3 boundary panel, generalized to the stamp). **Auto-flag (free
boolean):** a MACHINERY stamp on a finding that *names a done-bar line* is internally contradictory - a done-bar
line is a SUBJECT threat by this section - and auto-flags for re-stamp. **Residual:** past the two-rater draw and
the contradiction auto-flag, nothing re-audits the stamp itself; a mis-stamped MED still routes to backlog with
only report count+pointer visibility - stated (§10).

---

## §4 - THE LOOP + TERMINATION (semantic stop-gate; `adequate` is first-class; no silent cap)

Each round runs the three stages (§1.5). Route-backs capped at 3/finding (a capped finding = `UNRESOLVED-CAPPED`
with its reason, never dropped).

**An operation stops ONLY with a `stop_reason` from this closed set, recorded in `round-result.json`, and G2-(a)
RE-DERIVES its firing condition from disk (INV-C12) - it never trusts the recorded label:**

| `stop_reason` | Fires when | G2-(a) re-derives by |
|---|---|---|
| **`adequate`** | every done-bar line green with a count/pointer AND 0 open SUBJECT-facing findings ≥ S_block AND the judge rules further rounds disproportionate | reading the done-bar checklist (each line count/pointer-backed) + the open-findings count in `round-result.json` - arithmetic |
| `oracle-dry` | 2 consecutive rounds with no new class ≥ MED (all critics) AND no new HIGH (trailing window = **2 rounds**) | reading the judge's **recomputed new-vs-known diff** in the last 2 `round-result.json` rows (the class-critic swept-surface confirmation is **advisory**, not the firing condition) |
| `cap` | `max_audit_loops` (5) hit | counting `loop-round-open` events |
| `scoped` | intake pre-declared `scoped` (bound to `op_key`) and the declared surface/round budget is spent | reading the CHARTER pre-declaration + comparing the declared surface inventory to the swept-sector list |
| `owner-halt` | the owner explicitly halts | a `halt` war-log event **carrying an owner-authorization token** (an `owner:` actor + the token in `evidence[]`, cross-checked against `gates/owner-halt-token.txt`; a self-issued halt FAILs) |
| **`ESCALATE`** (T1 only, transition not terminal) | a T1 strike can neither close its gaps nor prove them class-shaped | the §1.5 gap-count re-derivation the ladder already defines - the Orchestrator re-derives and stands up a fresh T2 |

A `stop_reason` whose firing condition cannot be re-derived from disk = **GATE-ERROR (fail-closed, INV-C2)**.
Every stop writes **`CONVERGENCE-DECISION.md`** (the `stop_reason`, class inventory, severity trend, explicit
residual-risk, **budget spent vs declared**). Any stop other than `oracle-dry`/`adequate` prints a **"stopped
under `<reason>`, not converged"** banner wherever completeness would otherwise be implied. `adequate` prints its
own honest line: **"done-bar met; not exhaustively swept."**

---

## §5 - WAR-ROOM (crash-only, per operation)
```
CHARTER.md STATE.md PULSE.md war-log.jsonl dashboard.html battle-map.md frame-check.md
gates/  G1.md G2.md G3.md  (+ G2F.md for fix stance, + sentinels incl. byte-sentinel)
phases/rN/  scouts/ claims.jsonl class-critic-<c>.md round-result.json
verdicts/rN/  <sector>.md red/<finding>.<n>.json master-assessment.md stuck-report.md
verdicts/rF/  red/fix-<class>.<n>.json        (fix stance)
fixes/<class>/  pin/ attempt-<n>.md   fixes/merge-order.md   fixes/unreproduced.md   (fix stance)
CONVERGENCE-DECISION.md  evidence/  sandbox/  deliverables/
```
**STATE header carries:** tier · declared budget `{agents, bytes, rounds}` · spend counters (updated by the
Orchestrator at every spawn/fold - INV-C11).

**SANDBOX HYGIENE (the 5.9 GB rule).** **ONE worktree/copy per class**, at `sandbox/<class>/`; refuters
and fixers get **scratch subdirectories inside it**, never full-tree clones of their own. The Orchestrator **GCs
scratch after each fold** (event `gc-swept`; evidence needed later is copied to `evidence/` first - pointers
outlive scratch). The **byte-sentinel** (§7.5) arms at op start: war-room size > declared byte budget = HALT +
stuck-report.

**PULSE:** `HH:MM| <phase> prog:<n> <HEALTHY|HUNG|DEAD> - <note>` (prog monotonic); one beat per stage
launch/return + every ≤30 min detached work. **war-log event** `{ts, actor, action, status, phase, subject,
evidence[], note, prev_hash}`, `action` ∈ the closed set (`nuclear-initialized · op-allocated · op-declined ·
op-abandoned · standing-order · calibration-amended · spotter-mapped · gate-armed · gate-passed · gate-failed ·
sentinel-armed · sentinel-halt · phase-launched · phase-complete · probe · chain-resolved · claim-refuted ·
deviation-found · verdict-issued · loop-round-open · tier-routed · tier-escalated · budget-halt · budget-extended
· frame-check-filed · finding-backlogged · gc-swept · fix-attempt · fix-green · ledger-migrated · halt ·
halt-cleared · correction · state-captured · report-drafted · report-verified · artifact-published · op-closed`).
*(The probe/gate/fix pass|fail|blocked variants are collapsed into `{action, status}` - `loop-round-open` and
`halt`, which gates consume, keep their own verbs.)* **Chained (INV-C6):** each `prev_hash = sha256(the exact
previous line)`; genesis = `sha256("")`. No placeholder hashes. **Resume:** CHARTER → PULSE → STATE →
`round-result.json` → run → append (chained) → flip.

---

## §5.5 - TOOTH 1: FREEZE-ARTIFACT HANDOFFS + the ENFORCING firewall (content floor)

Each stage freezes a schema'd artifact; the next reads only it (scouts→`scouts/*.jsonl`; research→`claims.jsonl`
bare; red+verify→`red/*` + tallies; judge→`master-assessment.md`). Main chat interposes at each freeze.

**The bare-claims firewall enforces both FORM and CONTENT.** A claim row = `{claim_id, statement, evidence[],
test, sector}`, no narrative/confidence field. At the RECON→RED freeze the validator REJECTS (kicks back to the
researcher; never edits) any `statement` that: exceeds ~2 clauses / runs multiple sentences; carries any
epistemic-stance qualifier (illustratively `I think|believe|likely|probably|clearly|obviously|appears|evidently|
apparently|should|presumably|seems` - reject any such qualifier, the list is not exhaustive); carries a reasoning
connective with a conclusion (`therefore|thus|because…so`); OR asserts more than ONE checkable mechanism, or none
with a concrete referent (a symbol/path/value). A statement is one falsifiable assertion with ≥1 referent.
**The validator applies to every claim-shaped artifact - G0 gaps and R1 claims alike** (single statement of
scope). Red is handed only the validated bare set. *Honest label (INV-C8):* advisory-strong (validator +
born-bare schema + separate stage-workflows), not a chmod barrier; the residual (a hint smuggled via evidence
ordering) is stated.

**Firewall-split children re-enter bare (NF-13, v3.6).** When the firewall splits a compound claim, each child is
re-authored BARE under the claim schema and **rated fresh before it sizes anything** - the parent's severity is
withheld, never inherited; a boundary child draws the §3 two-rater panel. (Verified-at-N=0; a clarification of
the existing firewall, not a new gate.)

**T0 carries no ceremony (NF-12, v3.6).** The firewall's kickback loop and the full war-room provenance chain are
**T1+ only**. A T0 decline records a plain `op-declined` LEDGER row (still INV-C6-chained) + the `control-pass.md`
pointer + the owner countersign (§0.2) - no firewall kickback, no STATE/CHARTER scaffold, no chain ceremony past
the ledger row itself. Ceremony without a downstream blind consumer is subtracted; the countersign is the integrity.

---

## §6 - OBSERVABILITY + the MANDATORY VERIFIED HTML REPORT

Maintain `dashboard.html`, republished each fold (renders the chained war-log as of the last fold - a snapshot,
integrity-checkable via INV-C6; its absence is stated, never implied). **Every T1/T2 run ends in
`deliverables/REPORT.html`** (a T0 run's deliverable is `control-pass.md` + the owner-countersigned `op-declined`
row): mission/subject/fence, **tier + why (the G0 arithmetic)**, formation fielded (agent counts), **budget
declared vs spent**, calibration, gate record, class inventory (per-class verdict + severity + fix + evidence
pointers), the **frame-check verbatim** (appendix - never paraphrased by the judge), backlogged machinery-facing
findings (count + pointer), `stop_reason` + CONVERGENCE-DECISION + residual risk, PROBE-BLOCKED honesty, and -
where the stop ∉ {`oracle-dry`, `adequate`} - a "stopped under `<reason>`, not converged" banner.

**Self-scale figures are rendered, not asserted (NF-14, v3.6).** Agent counts and budget declared-vs-spent are
RENDERED from frozen ledgers (the war-log spawn census, the STATE spend counters) - never hand-tallied - and are
**excluded from the G3-b pointer denominator** so the verify-loop cannot move the count it audits. An unledgered
figure is dropped, not estimated.

**Nuclear-on-the-report (terminates):** write → check (re-resolve every pointer AND re-derive support) → verify
(Red samples body for laundered tags) → rewrite → judge. **Deliver iff ruling `TURN-IN` OR loop==3** (then a
mandatory `REPORT-CAVEAT` block; log `report-verified: CAVEATED`). Always delivered.

---

## §7 - GUARDRAIL INVARIANTS

- **C1 canonical signature** - one `canon()`, full-signature keying, never a single token.
- **C2 fail-closed** - empty/absent → GATE-ERROR, never a pass.
- **C3 signed provenance** - exemption/pin/instrument lists live in an owner-committed manifest whose integrity is
  verified at gate start against an anchor the producer cannot write: the manifest must be **git-committed by the
  owner** (the commit is the countersignature) AND its blob-sha pinned in an owner-controlled location **outside
  the producer-writable tree**. Every banked verdict carries the producing sha. *Limit:* the **INV-C8
  countersignature-root limit** (stated once below) - this is a human-countersignature root, not PKI.
- **C4 no default-masking** - a hiding mask is default-OFF or stamps a `<class>-deferred` flag.
- **C5 one orphan-safe launcher** - `start_new_session` + group-kill on timeout + instruction cap.
- **C6 ledger integrity** - every append-only ledger (`war-log.jsonl` AND `nuclear/LEDGER.md`, whose rows carry a
  trailing `prev_hash` column) is chained: `prev_hash = sha256(previous line/row bytes)`, genesis `sha256("")`.
  `verify_chain()` re-reads and recomputes; resume/record-check/dashboard call it first. A pre-chain ledger is
  migrated once (back-fill + a `ledger-migrated` event); until then it is flagged `unchained-legacy`, not failed.
  No placeholder hashes.
- **C7 reserve-then-admit** - admission under one lock on a reservations ledger.
- **C8 real confidentiality + the countersignature-root limit (canonical home).** A barrier is a mechanism; if
  only advisory, say advisory (as §5.5 does). **Countersignature-root limit:** C3 and C10(ii) both root in the
  owner's git-commit discipline + external pin, NOT cryptographic identity. Absent PKI, a producer-writable or
  unanchored manifest collapses the provenance it roots to **advisory (INV-C8), not mechanical** - say so. C3 and
  C10 cite this clause; neither restates it.
- **C9 separation-of-duties, honest** - bind writes to author where mechanizable; state the LLM residual.
- **C10 boundary-provenance.** A verdict/metric/coverage number MUST carry a **`sampled_from`** field naming
  which side of the trust boundary produced it (e.g. `DUT` vs `golden/oracle`), and a gate that closes on it MUST
  run the **wrong-side negative control**: perturb or disable the side the metric claims to measure - **the number
  must change; if it is identical with that side off/absent, the metric is VOID (fail-closed).** Corollaries:
  (a) a required metric Specced with no working DUT-side instrument is SPECCED-ONLY and cannot close; (b) *choosing
  what to sample* is as dangerous as *changing the oracle* - both owner-gated, both draw a probe; (c) a re-reported
  metric must prove its database was updated by the run it claims (freshness anchor); (d) the anchor must be
  producer-UNcontrollable - `sampled_from` derived from any producer-written signal (label, filename, path glob,
  self-attested record) is forgeable. Boundary-provenance approaches mechanical enforcement only by (i) the gate
  itself EXECUTING the wrong-side control **with a C3-trusted instrument**, OR (ii) binding the metric to a signed
  owner-committed instrument sha **whose manifest is externally anchored per INV-C3** - both root in the INV-C8
  countersignature limit; if unanchored, route (ii) is **advisory**, and a DUT-closure metric with neither route
  is **UNVERIFIABLE → BLOCK**. **(e) The instruments obey their own law (v3.6, the BL-01 cure - an inward
  generalization, not a new subsystem).** Boundary-provenance turns inward: every gating instrument (freeze-
  sentinel, bare-claims firewall, gate-runner, fold-verifier) is itself measured - **a gate-closing count that
  reads identical with its measured side absent is VOID → fail-closed**, and this applies to the gates' OWN
  counts, not only to the subject's metrics. Each instrument is banked ONCE with a sandboxed negative-control
  transcript proving it fails on demand (`nuclear/negative-controls/<instrument>.txt`; no per-op wave - one
  artifact, so the §3 firewall is not re-tripped). *An instrument that has never failed has never been tested.*
- **C11 budget (the Hallmark invariant).** Every T1/T2 op declares `{max_agents, max_bytes, max_rounds}` at
  intake (§9 tier defaults if unstated), recorded in CHARTER + STATE. The Orchestrator updates spend counters at
  every spawn and fold. **Spend > budget = `budget-halt` (fail-closed): HALT until the owner signs an extension
  (`budget-extended` event carrying the owner token) - never silent continuation, never a silent cap: the halt
  records what was pending and why.** The byte-sentinel (§7.5) is C11's mechanical arm. A formation plan whose
  arithmetic exceeds the declared budget before launch is re-planned or owner-signed at the door.
- **C12 re-derivation law (v3.6, canonical - consolidates the ≥5 scattered "gates re-derive, never trust a
  label" echoes into one named home).** Every gate re-derives its firing condition from a frozen artifact by
  read + count + compare; **a recorded label is never trusted → fail-closed** on a mismatch or an absent input
  (INV-C2). §0.2 routing, §4 termination, the §1 gate-runner carve-out, and every §7.5 gate are instances of
  C12 and CITE it rather than restating it. C12 renames a law v3.5 already ran everywhere; it adds no machinery.

---

## §7.5 - TOOTH 3: GATES (boolean DoD) + G2F + sentinels

Every op instantiates G0/G1/G2/G3 (fix stance adds G2F) under `gates/`; each condition backed by a count/pointer;
each prints one line `GATE <name>: PASS|FAIL|BLOCKED`. **BLOCKED-after-green → FAIL.** All gates are INV-C12
instances.
```
G0 TRIAGE (opens allocation) · auto+report: control-pass.md exists · every counted gap is bare + done-bar-named ·
   tier arithmetic recorded (the route is re-derivable from control-pass.md - the class-shaped and fix-on-HIGH
   disjuncts are NOT yielded by the gap count alone)
G1 CALIBRATION (opens R1) · auto+report: (a) battle-map, every file in one sector, unfiled=0 (T1: the map IS
   control-pass.md, §8.5)  (b) HIGH subsystems listed  (c) sized + amendments A-nn recorded  (d) budget declared
   in CHARTER (INV-C11)   [note-only: codename evocative]
G2 VERIFY-EXIT (opens report) · notify · prints PASS|FAIL|BLOCKED:
   (a) stop_reason re-derived from disk per §4 (INV-C12, not membership)                     round-result.json
   (b) FOR EACH SUBJECT-facing finding f >= S_block: count non-empty red-file CONTENT - a stated verdict + an
       evidence pointer distinct from the finding's own - in verdicts/rN/red/<f>.*.json >= the §9 floor
       (2 agreed, or the stakes ceiling on split / HIGH-at-critical); NEVER bare file existence   per-finding
   (c) every VERIFIED-OK backed by a fired negative control; every metric passes INV-C10 (incl. C10-e for the
       op's OWN instruments)                                                                  sandbox/, sampled_from
   (d) spend <= budget, or a signed budget-extended event covers the overrun (INV-C11)        STATE counters
G2F FIX VERIFY-EXIT (fix stance) · notify: per fixed class - refuter floor met in verdicts/rF/red/ AND its pin
   now green AND regression green
G3 REPORT (closes op) · HARD-STOP-owner: (a) REPORT.html TURN-IN or REPORT-CAVEAT  (b) every report pointer
   re-resolves + is supported (self-scale figures excluded from the denominator, §6)  (c) frame-check.md present
   verbatim in the appendix (T2)
```
**Freeze sentinel (crown jewel).** An op that freezes an expected signature (red or green) arms a sentinel;
**any signature change in EITHER direction = HALT + stuck-report** (a surprise-green flip is HALT-until-proven).
Certs anchor to source state, never a prior PASS; wall-clock is not a sentinel. **D6 honesty:** an op that arms no
sentinel says so; a `fix`-stance op MUST arm + defend a sentinel (the pinned defect + the green baseline). Each
sentinel is itself negative-controlled once per INV-C10-e (it must be shown it CAN fire, not only that it is
"armed"). **Byte-sentinel (mandatory T1+T2):** war-room bytes vs declared budget, checked at every fold + every
GC sweep; overrun = HALT + stuck-report (INV-C11). *Limit:* the surprise-green branch is `[PROVISIONAL]`.

---

## §8 - PROTOCOLS
No-interrupt (pre-authorized live jobs never signalled/killed; read-only snapshots). No contention (never take a
scarce seat; preflight + orphan scan before heavy launches). **Write-fence** - all writes under `<project>/
nuclear/`; the rest READ-ONLY unless stance `fix` (§13, verified copy only). **No destructive commands in any
subagent brief** (write to a fresh path, never delete; cleanup belongs to the Orchestrator's GC sweep alone).
**Machine proposes, owner signs** - subject-manifest changes + cutovers land `PROPOSED - awaiting owner sign`;
the historian appends and calibration amendments (`auto+report`) need no owner turn; **a verbal/unsigned
greenlight never closes a gate - a signature forces a read**; G3 is the owner countersign; the T0 op-declined row
is owner-countersigned (§0.2).

---

## §8.5 - TOOTH 4: CALIBRATE-THEN-STAFF + amendment ledger
T2 only (a T1 strike's map IS the control pass). Before Round 1: (1) the Spotter maps the subject →
`battle-map.md` (sectors + HIGH/normal/cold; cold/vendor = inventory-only). (2) Staff base counts to the real
sector count - **within the declared budget (INV-C11); a plan that exceeds it is re-planned or owner-signed
before launch.** (3) Record every deviation as a numbered amendment `A-nn` in CHARTER (`decision · rationale ·
evidence · date · sign-rule` default `auto+report`; HARD-STOP only if it touches the fence/standing-orders -
**and the cost of NOT amending**). Terrain wins, but only through a ratified amendment, never silent drift.

---

## §9 - DEFAULTS TABLE (operating knobs)

| Name | Default |
|---|---|
| `<project>` / nuclear home | git root of subject / `<project>/nuclear/` |
| op naming / claim | `Operation-<N>-<Codename>` (N ordering, codename EVOCATIVE); number-only exclusive mkdir then rename |
| ledger | append-only, `.ledger.lock`, prev_hash-chained; `op_key = sha256(subject + "\n" + mission)` |
| stance / stakes defaults | `audit` / `routine` |
| **G0 tier thresholds** | 0 gaps ≥ S_block → T0 (owner-countersigned decline) · 1–3 named → T1 · >3, class-shaped, or fix-on-HIGH → T2 |
| **tier budgets (INV-C11)** | T1: 6 agents · 200 MB · 1 round - T2: §1 sizing agents · 2 GB · 5 rounds (owner-overridable at intake) |
| **refuter floor / early-stop** | run 2 blind; agree → bank; split → stakes ceiling. HIGH at `critical`: always the ceiling |
| stop-reason set | `{adequate, oracle-dry, cap, scoped, owner-halt}` + T1 `ESCALATE` (transition); each G2-a-re-derived from disk (INV-C12) |
| model map | inherited model / inherited model / inherited model (§1); Spotter = inherited model (verbatim/measure floor) |
| base Recon (T2) | small 3–5 · medium ~10 · large ~27 (10 census + 8 probe + 9 chain) |
| class-critics (T2) | small 1 · medium 1 · large 2 (+1 critical) |
| **frame-critic** | 1 per T2 op, at R1; reads subject + mission only; output raw to owner |
| stakes → refuter ceiling | routine 3 · elevated 3+Nv2 · critical 5+Nv2 |
| N-version | 2, on HIGH subsystems at elevated+ |
| S_block / severity floor / trailing-window | MED-HIGH / MED / 2 rounds |
| caps (route-back / audit / fix) | 3 / 5 / 3 |
| red/ rule | one file per refuter, per-finding CONTENT count (verdict + distinct pointer); fold blocked if absent/empty (G2-b) |
| **scope firewall** | judge stamps `facing` (≥S_block → 2 raters, round toward SUBJECT; contradiction auto-flag); MACHINERY → `DOCTRINE-BACKLOG.md`, no in-op wave |
| **sandbox / GC** | one worktree per class; refuter scratch inside it; GC after each fold (`gc-swept`); evidence copied out first |
| firewall | form + content bareness validator, reject-not-edit (§5.5); applies to G0 gaps + R1 claims; split-children re-rated fresh |
| **instrument negative-control (INV-C10-e)** | one banked fail-on-demand transcript per gating instrument under `nuclear/negative-controls/`; VOID-if-unchanged-with-measured-side-absent applies to the gates' own counts |
| reasoning-tier | judge+red **high** (xhigh at critical); scouts **standard** |
| SNAPSHOT_REF | `git -C <subject> rev-parse HEAD` at op-allocate, recorded in CHARTER (fix stance) |
| report verify-loop cap | 3, then REPORT-CAVEAT; self-scale figures rendered from ledgers, excluded from G3-b denominator |
| concurrency | 16 simultaneous, waves beyond |
| gates | G0 auto+report · G1 auto+report · G2 notify · G3 HARD-STOP-owner (+ G2F fix); all INV-C12 instances |
| INV-C10 | closing metric carries `sampled_from`; mechanical only via a C3-trusted executed control OR an externally-anchored signed binding - else advisory; **(e) binds the op's own instruments** |
| **INV-C11** | budget declared at intake; spend counters in STATE; overrun = budget-halt, owner-signed extension only |
| **INV-C12** | every gate re-derives from a frozen artifact; a recorded label is never trusted (fail-closed) |
| PULSE cadence | one beat per stage launch/return + every ≤30 min of detached work (§5) |
| boundary-severity call | a MED/MED-HIGH boundary finding rated by 2 independent agents, rounds UP on disagreement (§3) |
| mandatory deliverable | T0: `control-pass.md` + owner-countersigned `op-declined` row · T1/T2: verified `deliverables/REPORT.html` |

---

## §10 - HONESTY LEDGER

Record only evidence observed during this operation. Distinguish demonstrated defects, untested assumptions, unresolved risks, and accepted limitations. Never carry another project verdict or owner authorization into this run.

## §11 - RETIRED (v3.6)
The v3.5 step-by-step RUNBOOK lived here. **It duplicated §0 + §0.2 + §1.5 and is retired** (subtraction pass,
§12). The single operative statement of the run is §0's first line; the loop is §1.5; the gate order is §7.5.

---

## §12 - SOURCE PROVENANCE

The upstream path and original digest are preserved in `../../BUNDLE-MANIFEST.json`. This adaptation supplies the procedure, not a previous operation result.

## §13 - FIX STANCE: the fix movement
A `fix` op runs `audit` to convergence (or a declared `scoped` audit of a supplied finding set), then, in a
**verified copy** - **one per class**: `sandbox/<class>/worktree/` (a git worktree at `SNAPSHOT_REF`
[git-worktree admin metadata under `.git/worktrees/` is a declared A-nn write-fence exception], or a plain copy;
the live subject stays READ-ONLY; refuters/fixers use scratch subdirs inside the class copy, never fresh
full-tree clones - §5 hygiene): (1) **Pinner** reproduces each target class as a deterministic failing pin
(`fixes/<class>/pin/`; no-repro → `unreproduced.md`, not a target). (2) **Fixer wave** - 1 fixer/class,
order-is-law: confirm the pin fails → implement → its **negative control fires** and the defect no longer
reproduces → `fixes/<class>/attempt-<n>.md`; `max_fix_loops=3` then a stuck-report. (3) **Arm the freeze
sentinel** on the now-green pins AND the green baseline (mandatory, §7.5; negative-controlled per INV-C10-e).
(4) **Integrator** derives `merge-order.md` (dependency-then-severity), merges serially, full regression after
each. (5) **Blind-red the fixes** - refuter floor per §9 early-stop, each writing `verdicts/rF/red/fix-<class>.<n>.json`;
**G2F** gates on them + green pin + green regression. (6) **One-command cutover, owner-signed**
(`deliverables/cutover.sh`, dry-run default, `--apply`, `--rollback`, backs up first); `PROPOSED - awaiting owner
sign`. The generational build is a `fix` op whose cutover assembles a *new generation directory* - same
machinery, the verified copy is the next generation. (This doctrine file is itself such a generation: v3.5 →
a new version, proposed as a new file, never an in-place rewrite of prior evidence.)

---

## §14 - RETIRED (v3.6 → folded into §10)
The formal head-to-head eval lived here. **Its one live claim - the formal scored comparison on an INDEPENDENT
subject has never been run - is now in §10**, at the point where the honesty ledger states the doctrine's
unproven value delta and the lineage's honest next stop_reason (`go run it on something real`).
