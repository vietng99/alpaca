# Observability operations

The project SQLite record remains the action authority. When a domain profile records
receipts, they remain the authority for that profile's acceptance. Native transcripts and the private tool pool provide capture;
canonical analytics and the wiki are derived views. No dashboard freshness signal is
an acceptance verdict.

## Collection contract

Schema version 3 adds source expectations, source generations, committed byte cursors,
sanitized observations, capture issues, consumer checkpoints, hook outcomes and durable
preservation jobs. Migrations are additive and transactional. Existing event hashes are
unchanged; managed records now reject event UPDATE and DELETE. Writer opens repair a
lagging run census; query-only connections never migrate or repair.

`bin/alpaca collect status` reports collector heartbeat age, process identity/liveness
where Linux can verify it, source byte backlog, observations awaiting preservation,
pending preservation jobs, consumer checkpoints, retries and coverage. Read-only WAL
connections can use SQLite shared-memory coordination files; they do not modify logical
record contents or initialize a missing project database.

`bin/alpaca collect once --max-records 1000` processes a bounded record batch. Sources are
registered explicitly through session lifecycle commands or `collect register`. Codex
session start discovers only a supplied native ID with a matching project header.
Project pool discovery refuses symlink ancestry and never imports another project's
spool. Known child IDs create explicit expectations; unavailable children stay missing.
An open or unverified child inventory makes completeness unknown. A present transcript
is not proof that every tool invocation, usage charge or child session was captured.

Complete JSONL lines are ingested transactionally with their cursors. Partial tails wait;
malformed lines retain offset/hash diagnostics and explicit quarantine facts. A changed
consumed prefix starts a new source generation. Immutable observation identities make
replay safe. Private reasoning, prompts, arguments and results are absent from normalized
observation facts; authorized native files and capped private pools retain user-visible
capture. New transcript snapshots exclude private reasoning and preserve prior generations.

Hook appends serialize short-write retries and fsync under a bounded lock. Failed hook
outcomes are durable when storage permits; sanitized stderr is the fallback. The hook
wall-clock budget includes outcome recording. In collector-enabled mode, hooks append
boundaries while the collector handles snapshots and wiki/export work. Snapshot request
checkpoints acknowledge durable jobs; only a successful snapshot completes a job. One
missing source retries independently without stopping preservation of other sessions.
Derived wiki transcript summaries use content-addressed revisions. Parser corrections and
source rewrites preserve prior summaries without waiving the knowledge shrink guard.
Rejected event ingestion leaves the prior raw file intact.

## Canonical metrics and evidence

Legacy and current analytics use the same native response identity and token normalization.
Imported charge samples keep their provenance and do not silently replace native usage.
Overlapping charge scopes, missing cache categories and missing sessions remain unknown.
Current HTTP session activity uses wall time; deterministic offline exports state their
record time.

`alpaca/runlog/` is the library for job evidence; it knows no job, log or report location, so a
domain profile wires job sampling and test-report extraction to its own jobs, passing the
paths in. `alpaca.runlog.telemetry` samples the host and one job's process tree from `/proc`
into a JSON-lines file the profile names; its `window` and `summary` readers label RSS and
host CPU as sampled, keep unavailable job CPU null, and count dropped, malformed and
partial samples. `alpaca.runlog.junit.parse_junit` turns a JUnit XML report into tests and
measurements that record source digest, parser version and units, and never turns a missing
metric into zero. `alpaca.runlog.quotes.check` accepts an agent's review of a log only when the
log still has the sha256 the agent read and every quote sits inside its named lines;
`alpaca.runlog.reviews` keeps a checked review as a read-only file, records it under the
profile's event kind, appends an engineer's confirm or dispute per finding as events, and
reads a kept file that no longer matches its recorded sha256 as not intact.
`alpaca.runlog.textwindow.read_window` follows a growing log one byte window at a time and cuts
each window on a whole UTF-8 character. Alpaca core runs no job of its own, so without a profile
that wires them none of these files is written.

A review may also carry `quick_summary` (20 to 360 characters: the result and why),
`next_check` (4 to 600: the first useful check) and `primary_finding` (the id of the finding
that best shows the result). An empty log may be reviewed with zero findings.

### Review every run, then decide

`alpaca.runlog.reviewflow` turns those reviews into a loop for every captured run, PASS runs
included. The main session assigns the run to a reviewer session with a lease, launches a
fresh native subagent with the returned brief, reads the checked review and records GO or
NO-GO. The run's recorded result, the review state and the main decision stay three separate
fields:

| Layer | Values | Owner |
|---|---|---|
| Result | PASS, FAIL, BLOCKED, INTERRUPTED | the profile's grader |
| Review | waiting, pending, reviewing, reviewed, failed, stale, not-applicable | the reviewer subagent |
| Decision | GO, NO-GO | the main session |

A reviewer cannot decide its own review, a result other than PASS never gets GO, a disputed
finding withdraws GO, and a changed log, review file or run record makes the review stale and
clears the decision. Three refused submits fail an assignment; a missing log is a failed review
that can still be held with NO-GO. Every assignment, refusal, failure and decision is an
append-only snapshot event; an OS lock per reference serializes changes. The module never
launches an agent and runs no model; a run waits until an active session reviews it.

A profile adopts the loop in four places:

1. Build one `reviewflow.Flow`: the log resolver, the review folder per reference, a lock
   folder, three event kinds (kept review, mark, workflow), `subject(ref)` returning the run as
   recorded (`result`, `reason`, and `reviewable: False` for a run that captures no log), `refs()`
   for `pending`, and optionally `go_check` (refuse GO when the run is no longer the current
   evidence) and `validate` (extra review fields the profile requires).
2. Expose verbs through the profile's `verbs()` hook for `pending`, `assign`, `submit`,
   `renew`, `fail`, `takeover`, `decide` and a text view that starts with
   `reviewflow.glance_lines`.
3. Call `reviewflow.require_go` before the runner advances past a run and
   `reviewflow.require_retry` before it reruns one. A batch that pauses for review keeps its
   cursor and releases its locks while it waits.
4. Add `workflow: reviewflow.state(...)` to the review payload a profile route serves; the run
   log panel then opens with an At a glance block (result, why, review state, decision, next
   check, evidence line) above the stage contract, and folds the full review under it.

The operator steps are in `.claude/skills/alpaca-log-review/SKILL.md` and `docs/operators.md`.

`bin/alpaca artifact catalog` inventories existing archives and recovers only source bytes
matching their historical hashes. Changed or missing originals remain distinct from
retained objects. New approved results are preserved in the content-addressed object
store before a later run can replace them. Passing result pins and proof retention stay
protected. The default CAS limit is 1 GiB per object and 20 GiB total; a budget refusal is
explicit unavailability. Legacy archive directories are reported separately from this
budget. `bin/alpaca artifact retention-dry-run` reports candidates and never deletes them.

## Supervised services

`bin/alpaca collect services --port <port>` generates project-specific user units under
`.alpaca/services`: a remote dashboard, collector, backup service and six-hour backup timer.
Inspect those files, then install the generated units under `~/.config/systemd/user/`, run
`systemctl --user daemon-reload`, and enable/start the dashboard, collector and timer.
`bin/alpaca collect enable` switches expensive native-hook projections to the collector.
The services restart after failures with backoff; systemd journals sanitized diagnostics.
The project-local SQLite runtime is loaded by `bin/alpaca-python`; see
`setup/sqlite-runtime.md` for the pinned build, checksum checks and rollback.

Remote mode requires configured dashboard credentials on every request and rechecks open
streams. Removing credentials fails closed. Credentials belong in the existing private
configuration, never in generated unit text, proof evidence, or backups. HTTP health uses
allowlisted metadata and does not expose raw source locators or capture payloads. Existing
authenticated rendered conversation views remain available. Public domain availability
must be tested separately from local HTTP and collector health.

## Backup and restore

`bin/alpaca backup create` takes coordinated main/wiki SQLite snapshots, briefly holding
both write guards only during the database copies. It then copies stable approved files,
including pending tool/hook spools, receipts, object manifests and sealed evidence. It
reconstructs the wiki ledger from committed verified database rows. Raw files can be ahead
of the database watermark and replay idempotently. A package is published only after
hash, integrity, event chain, wiki chain, proof availability and cursor checks pass.

`bin/alpaca collect backup` is the scheduled entry point. It checks a 20 GiB local backup
budget and reserves space for another snapshot; exhaustion records a visible failure and
never deletes old backups. Interrupted `.pending-*` directories remain unpublished evidence
and count toward storage use. Inspect storage and choose retention or another destination
before removing them. The timer creates a local recovery point every six hours when the
host is available. This provides no protection against losing the entire machine.

`bin/alpaca backup verify PATH` checks the package again. `bin/alpaca backup restore PATH DEST`
requires an empty destination outside the source project and backup. Restore rebases only
project-local source locators, disables external provider registrations until explicitly
rebound, and never resumes a long-running job automatically. A persistent restore-disabled capability
prevents automatic native discovery from re-enabling those sources; an explicit locator
registration is required to clear it. Retained historical proof gaps remain declared;
structural recovery success is not a claim that missing originals were recovered. Inspect
`.alpaca/restore.json`, bind approved sources, revalidate generations, then run collection and
wiki replay before trusting derived views. Restart services only after their runtime and
credentials have been provisioned at the destination.

Off-host backups and multi-host transport require a chosen destination and remain
separate deployment work.

## Deployment limits and recovery cadence

The generated local schedule is six hours. A shorter recovery point or a guaranteed
observation latency is not provided; measure snapshot and restore times on your own
filesystem before relying on them. Historical probe sessions remain preserved and
identifiable. Their existing source registrations are collected; they are not evidence
that a work session is complete.

The local authenticated origin and any tunnel in front of it are supervised separately.
Check public HTTPS with a browser-style request: an unauthenticated request should return
401 and the configured login 200. Some proxies reject default Python or curl request
profiles, so use a browser-compatible profile for monitoring.
