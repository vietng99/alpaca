# MAP: the navigator

The territory and the role-by-phase router. The SessionStart hook applies this
navigator: the core set loads unconditionally, and only then does the router
narrow the reads for the role and the phase.

## 1. The boot read-order

The reads happen in this order, and the core set loads before the router narrows
anything, for every role and every phase:

1. pad: the landing pad the record renders (`alpaca/pad.py`).
2. manifest: `ALPACA-MANIFEST`, the two path classes the harness owns.
3. core set: `doctrine/CORE-CARD.md` and the CORE leaves in section 2 below,
   loaded unconditionally.
4. router row: the role-by-phase router in section 3, which narrows the reads
   only after the core set is loaded.
5. level: the autodrive level in force, read from the record.
6. modes: any project modes and presets the router selected.

The router never runs before the core set. A router that narrowed first could
drop an invariant rule; loading the core first makes that impossible.

## 2. The core set (always load)

These leaves load every session, before the router narrows anything.

| # | Leaf | Why it is core |
|---|---|---|
| 1 | `doctrine/leaves/file-as-truth.md` | Every claim, verdict and order lives in a schema'd file; a return message carries a pointer, never the load-bearing content. |
| 2 | `doctrine/leaves/provenance-or-die.md` | Every assertion carries a resolvable evidence pointer; an unevidenced claim is discarded, never laundered. |
| 3 | `doctrine/leaves/test-or-UNTESTED.md` | No unbuilt or unexercised work is ever tagged built; the honest default tag is UNTESTED. |
| 4 | `doctrine/leaves/verification-tags.md` | A verdict is a derived tag over a real run, not a human signature; a guard that cannot fail proves nothing. |
| 5 | `doctrine/leaves/halt-on-drift.md` | When the subject under check changes underfoot, the run halts rather than reporting a stale verdict. |
| 6 | `doctrine/leaves/crash-only-resume.md` | No in-memory state is assumed to survive; the run resumes from the record and a resume pad, never from a live process. |
| 7 | `doctrine/leaves/double-dispatch.md` | Two live sessions cannot silently run the same step; a claim on the record makes the collision visible. |
| 8 | `doctrine/leaves/movement-gate-pin-sentinel.md` | The gated-phase spine: work moves, a gate adjudicates, a pin fixes the verdict, a sentinel guards it against drift. |
| 9 | `doctrine/leaves/record-over-ceremony.md` | The append-only record with proof pointers replaces the old signed-checklist gate; a mark is an event, never a signature. |
| 10 | `doctrine/leaves/board-is-truth.md` | The board is a projection of the record; a skipped gate or an override shows on it forever and is never laundered into a normal discharge. |

## 3. The router (role by phase)

After the core set loads, the router narrows the long-tail reads by role and
phase. It is a retrieval optimization over the tail, never a gate on the core.

| role | phase | narrow to |
|---|---|---|
| builder | build | the leaves the task touches, plus the CORE set |
| checker | verify | the gate leaves and the verdict contract, plus the CORE set |
| operator | operate | the operator leaves (pad, resume, historian), plus the CORE set |
| owner | decide | the human decision gate and the level ladder, plus the CORE set |

## 4. Territory

Every registered leaf, one invariant line each.

- `doctrine/leaves/file-as-truth.md`: Every claim, verdict and order lives in a schema'd file; a return message carries a pointer, never the load-bearing content.
- `doctrine/leaves/provenance-or-die.md`: Every assertion carries a resolvable evidence pointer; an unevidenced claim is discarded, never laundered.
- `doctrine/leaves/test-or-UNTESTED.md`: No unbuilt or unexercised work is ever tagged built; the honest default tag is UNTESTED.
- `doctrine/leaves/verification-tags.md`: A verdict is a derived tag over a real run, not a human signature; a guard that cannot fail proves nothing.
- `doctrine/leaves/halt-on-drift.md`: When the subject under check changes underfoot, the run halts rather than reporting a stale verdict.
- `doctrine/leaves/crash-only-resume.md`: No in-memory state is assumed to survive; the run resumes from the record and a resume pad, never from a live process.
- `doctrine/leaves/double-dispatch.md`: Two live sessions cannot silently run the same step; a claim on the record makes the collision visible.
- `doctrine/leaves/movement-gate-pin-sentinel.md`: The gated-phase spine: work moves, a gate adjudicates, a pin fixes the verdict, a sentinel guards it against drift.
- `doctrine/leaves/audit-first-primacy.md`: Understand the ground truth before changing it; no symptom-fix ahead of the audit that explains it.
- `doctrine/leaves/hitl-decision-gate.md`: A boundary that only a human may cross pauses for a recorded decision, exit 3, never an auto-advance.
- `doctrine/leaves/harness-navigation.md`: A loading discipline for a many-file tree: load the invariant core every time, retrieve the long tail on demand.
- `doctrine/leaves/harness-distillation.md`: An op produces three separable things, and only the deliverable ships; the lessons and the record are distilled forward.
- `doctrine/leaves/workspace-isolation.md`: Each unit of work runs in its own isolated tree; a builder never writes into another's live workspace.
- `doctrine/leaves/plugin-not-welded.md`: Target-specific behavior lives behind a seam as a plugin; the generic core never hardcodes a domain value.
- `doctrine/leaves/blind-pairs.md`: Independence is engineered: a checker never sees the builder's narrative, so shared bias cannot pass unchallenged.
- `doctrine/leaves/budget-survival.md`: A run never spends its account to the wall; it keeps a reserve so it can always resume.
- `doctrine/leaves/concurrency-tiers.md`: Heavy operations are classified into tiers so what may overlap overlaps and what must serialize does.
- `doctrine/leaves/watchdog-liveness.md`: A long run proves it is alive by a heartbeat; a missed beat is a liveness fault the record shows.
- `doctrine/leaves/heartbeat-os-kicker.md`: Re-entry is guarded across a restart by an account-independent timer, not by a lock a crash could strand.
- `doctrine/leaves/quota-reset-recovery.md`: A run waits out a quota reset from the real reset header, not a guessed delay, then resumes cost-aware.
- `doctrine/leaves/worker-pool-recovery.md`: A worker that dies mid-hold releases its claim by lease expiry; the pool refills without a stranded step.
- `doctrine/leaves/autodrive-levels.md`: One unified ladder names the autonomy postures; a level is granted by a human and never self-raised.
- `doctrine/leaves/lessons-write-gate.md`: A lesson is admitted only through a gate with a shrink floor; the store grows on evidence, not on every thought.
- `doctrine/leaves/spec-driven-checklist.md`: Work is driven by a checklist derived from the spec; a discharged item moves to the discharged store, never a signed sink.
- `doctrine/leaves/context-clearing.md`: An agent evicts spent context in-window the same way file-as-truth evicts it across a boundary: keep the pointer, drop the body.
- `doctrine/leaves/landing-pad-resume.md`: Every session lands on one rendered pad that says where the project is and what is owed; resume reads the pad, it does not race.
- `doctrine/leaves/intent-is-the-done-bar.md`: A task is done when its recorded intent is met with a proof pointer, not when the author feels finished.
- `doctrine/leaves/write-ahead-capture.md`: The intent to act is written before the act, so a crash between the two is recoverable rather than invisible.
- `doctrine/leaves/historian.md`: Capture is dumb and continuous; sorting into a per-op record is a separate, cadenced pass, and nothing is lost in between.
- `doctrine/leaves/record-over-ceremony.md`: The append-only record with proof pointers replaces the old signed-checklist gate; a mark is an event, never a signature.
- `doctrine/leaves/level-gated-advance.md`: What an agent may decide next is gated by the recorded level in force, and the gate reads the level from the record, not the environment.
- `doctrine/leaves/board-is-truth.md`: The board is a projection of the record; a skipped gate or an override shows on it forever and is never laundered into a normal discharge.
- `doctrine/leaves/plain-language.md`: Shipped prose is plain: no ceremony words, no filler; the ban list is data and the check is one lint.
- `doctrine/leaves/serial-with-resolve.md`: Two writers may reach for one reference; the collision is recorded and a later pass resolves it, rather than blocked by a hard lock.
- `doctrine/leaves/two-change-classes.md`: Every write is agent-writable or human-owned by path; a human-owned write is a review card up to L4 and a recorded diff at L5+.

