# Alpaca doctrine registry (INDEX)

This is the registry of the harness doctrine, one file per leaf under
`doctrine/leaves/`. The set is the doctrine named in the spec: the kept generic
leaves, the Alpaca operator leaves, and the new leaves.

This table is DERIVED, not asserted. Each row's Digest cell is the first 12 hex
of the sha256 over the leaf's own raw bytes. The instrument
`alpaca/gates/doctrine_registration_check.py` re-derives the leaf population from the
filesystem, requires exactly one row here per leaf, resolves every pointer, and
re-binds every Digest cell against the bytes of the leaf it names. A digest typed
here that the leaf's bytes do not support is a FAIL, not a fact. Registration is
checked in both directions: every leaf on disk has a row, and every row points at
a leaf that exists.

| # | Leaf | One line | Digest |
|---|---|---|---|
| 1 | [file-as-truth](leaves/file-as-truth.md) | Every claim, verdict and order lives in a schema'd file; a return message carries a pointer, never the load-bearing content. | ea189ac91a16 |
| 2 | [provenance-or-die](leaves/provenance-or-die.md) | Every assertion carries a resolvable evidence pointer; an unevidenced claim is discarded, never laundered. | c2f0c068c3c9 |
| 3 | [test-or-UNTESTED](leaves/test-or-UNTESTED.md) | No unbuilt or unexercised work is ever tagged built; the honest default tag is UNTESTED. | 5706df341f8e |
| 4 | [verification-tags](leaves/verification-tags.md) | A verdict is a derived tag over a real run, not a human signature; a guard that cannot fail proves nothing. | b521f8ad01b4 |
| 5 | [halt-on-drift](leaves/halt-on-drift.md) | When the subject under check changes underfoot, the run halts rather than reporting a stale verdict. | 16a6689b0f32 |
| 6 | [crash-only-resume](leaves/crash-only-resume.md) | No in-memory state is assumed to survive; the run resumes from the record and a resume pad, never from a live process. | ea32b4b2b6d9 |
| 7 | [double-dispatch](leaves/double-dispatch.md) | Two live sessions cannot silently run the same step; a claim on the record makes the collision visible. | 9b645c13c177 |
| 8 | [movement-gate-pin-sentinel](leaves/movement-gate-pin-sentinel.md) | The gated-phase spine: work moves, a gate adjudicates, a pin fixes the verdict, a sentinel guards it against drift. | 560c8ac9dfa6 |
| 9 | [audit-first-primacy](leaves/audit-first-primacy.md) | Understand the ground truth before changing it; no symptom-fix ahead of the audit that explains it. | 2d32057f204f |
| 10 | [hitl-decision-gate](leaves/hitl-decision-gate.md) | A boundary that only a human may cross pauses for a recorded decision, exit 3, never an auto-advance. | 54858e954d4d |
| 11 | [harness-navigation](leaves/harness-navigation.md) | A loading discipline for a many-file tree: load the invariant core every time, retrieve the long tail on demand. | 6d9485caa343 |
| 12 | [harness-distillation](leaves/harness-distillation.md) | An op produces three separable things, and only the deliverable ships; the lessons and the record are distilled forward. | 79aa4042cd95 |
| 13 | [workspace-isolation](leaves/workspace-isolation.md) | Each unit of work runs in its own isolated tree; a builder never writes into another's live workspace. | 9a02ec02c1ba |
| 14 | [plugin-not-welded](leaves/plugin-not-welded.md) | Target-specific behavior lives behind a seam as a plugin; the generic core never hardcodes a domain value. | b91cbd9d1936 |
| 15 | [blind-pairs](leaves/blind-pairs.md) | Independence is engineered: a checker never sees the builder's narrative, so shared bias cannot pass unchallenged. | 0fd60bf93287 |
| 16 | [budget-survival](leaves/budget-survival.md) | A run never spends its account to the wall; it keeps a reserve so it can always resume. | 24900db53c25 |
| 17 | [concurrency-tiers](leaves/concurrency-tiers.md) | Heavy operations are classified into tiers so what may overlap overlaps and what must serialize does. | b3177344215d |
| 18 | [watchdog-liveness](leaves/watchdog-liveness.md) | A long run proves it is alive by a heartbeat; a missed beat is a liveness fault the record shows. | 10e29dff51ae |
| 19 | [heartbeat-os-kicker](leaves/heartbeat-os-kicker.md) | Re-entry is guarded across a restart by an account-independent timer, not by a lock a crash could strand. | 1e66cc95ace3 |
| 20 | [quota-reset-recovery](leaves/quota-reset-recovery.md) | A run waits out a quota reset from the real reset header, not a guessed delay, then resumes cost-aware. | 802ada9f6fef |
| 21 | [worker-pool-recovery](leaves/worker-pool-recovery.md) | A worker that dies mid-hold releases its claim by lease expiry; the pool refills without a stranded step. | 485f1b936e6e |
| 22 | [autodrive-levels](leaves/autodrive-levels.md) | One unified ladder names the autonomy postures; a level is granted by a human and never self-raised. | 4a1bd01abfef |
| 23 | [lessons-write-gate](leaves/lessons-write-gate.md) | A lesson is admitted only through a gate with a shrink floor; the store grows on evidence, not on every thought. | 026062db7771 |
| 24 | [spec-driven-checklist](leaves/spec-driven-checklist.md) | Work is driven by a checklist derived from the spec; a discharged item moves to the discharged store, never a signed sink. | 3414b7d6169e |
| 25 | [context-clearing](leaves/context-clearing.md) | An agent evicts spent context in-window the same way file-as-truth evicts it across a boundary: keep the pointer, drop the body. | afd351664d34 |
| 26 | [landing-pad-resume](leaves/landing-pad-resume.md) | Every session lands on one rendered pad that says where the project is and what is owed; resume reads the pad, it does not race. | 33a6ae6ea6f3 |
| 27 | [intent-is-the-done-bar](leaves/intent-is-the-done-bar.md) | A task is done when its recorded intent is met with a proof pointer, not when the author feels finished. | ec7ec11715c5 |
| 28 | [write-ahead-capture](leaves/write-ahead-capture.md) | The intent to act is written before the act, so a crash between the two is recoverable rather than invisible. | dffd62e14444 |
| 29 | [historian](leaves/historian.md) | Capture is dumb and continuous; sorting into a per-op record is a separate, cadenced pass, and nothing is lost in between. | cd581d56e838 |
| 30 | [record-over-ceremony](leaves/record-over-ceremony.md) | The append-only record with proof pointers replaces the old signed-checklist gate; a mark is an event, never a signature. | f904d7d31469 |
| 31 | [level-gated-advance](leaves/level-gated-advance.md) | What an agent may decide next is gated by the recorded level in force, and the gate reads the level from the record, not the environment. | 2cf668e84839 |
| 32 | [board-is-truth](leaves/board-is-truth.md) | The board is a projection of the record; a skipped gate or an override shows on it forever and is never laundered into a normal discharge. | baab68f1b857 |
| 33 | [plain-language](leaves/plain-language.md) | Shipped prose is plain: no ceremony words, no filler; the ban list is data and the check is one lint. | f65f6161bb5f |
| 34 | [serial-with-resolve](leaves/serial-with-resolve.md) | Two writers may reach for one reference; the collision is recorded and a later pass resolves it, rather than blocked by a hard lock. | a5e8901ccc0b |
| 35 | [two-change-classes](leaves/two-change-classes.md) | Every write is agent-writable or human-owned by path; a human-owned write is a review card up to L4 and a recorded diff at L5+. | 97be037698f3 |
| 36 | [operate-in-place](leaves/operate-in-place.md) | The store is history the operator is mutating past, not a clone to promote from; a pre-image is snapshotted with a mandatory note before any non-idempotent act. | ae7c4074419f |
| 37 | [dumb-capture-agentic-sort](leaves/dumb-capture-agentic-sort.md) | Capture writes raw with zero judgment; only the sort pass judges, surfaces the unsorted bucket with a reason, and never drops a note. | 85f3e641ef9e |

