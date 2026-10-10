"""Record event kinds of the research engine, one constant each, and emit().
Design: design/research-engine/architecture.md sections 8.3 and 12.3."""
from __future__ import annotations

import re

from alpaca import db
from alpaca.gates import verdict as vc
from alpaca.research import errors

# engine core (t-100 unless noted)
RUN_START = "research-run-start"
RUNNER = "research-runner"
ROUND_OPEN = "research-round-open"
ROUND = "research-round"
MERGE = "research-merge"
FINALIZE = "research-finalize"
INFRA = "research-infra"
CONTRACT = "research-contract"                  # t-096 contract.land, t-103 intake; design t-088
CONTRACT_DRIFT = "research-contract-drift"      # t-096
LAUNCH = "research-launch"                      # t-098
LAUNCH_END = "research-launch-end"              # t-098
PROPOSAL = "research-proposal"
WORKTREE = "research-worktree"                  # t-091
TAMPER = "research-tamper"                      # t-092
DIFF = "research-diff"                          # t-091
DIFF_REFUSED = "research-diff-refused"          # t-100, the FR-018 check of judge and record
PURGE = "research-purge"                        # t-091
GATE = "research-gate"                          # t-093
ACCEPT = "research-accept"
ATTEMPT = "research-attempt"
# the verifier and the audit: t-089's kinds (verifier.md section 7)
VERIFY_OPEN = "research-verify-open"            # t-094
VERIFY_ASSIGN = "research-verify-assign"        # t-094
VERIFY_REFUSED = "research-verify-refused"      # t-094
VERIFY_REVIEW = "research-verify-review"        # t-094, written by reviews.submit
VERIFY_APPLY_FAILURE = "research-verify-apply-failure"  # t-094
PROOF_VERDICT = "research-proof-verdict"        # t-094
VERIFY_ROUND = "research-verify-round"          # t-094
PROOF_EXTEND = "research-proof-extend"          # t-094
PROOF_RULING = "research-proof-ruling"          # t-094
VERIFY_END = "research-verify-end"              # t-094
AUDIT_REVIEW = "research-audit-review"          # t-095, written by reviews.submit
AUDIT_VERDICT = "research-audit-verdict"        # t-095
AUDIT_REFUSED = "research-audit-refused"        # t-095
REPRODUCER = "research-reproducer"              # t-101
BASELINE = "research-baseline"                  # t-101, the repair task's base pass-to-pass set
PREPARE = "research-prepare"                    # t-101, the case's prepare stage and its commit
REPAIR_RESULT = "research-repair-result"        # t-101, the repair task's outcome for the verifiers
HOLDOUT = "research-holdout"                    # t-102
HOLD = "research-hold"                          # t-100, design t-090
HOLD_LIFTED = "research-hold-lifted"            # t-100, design t-090
# the verbs (t-105, design t-090)
STATUS = "research-status"
STOP = "research-stop"
EXTEND = "research-extend"
RESUME = "research-resume"
REPORT = "research-report"
GRANT = "research-grant"
REFUSED = "research-refused"
# the session runtime (design/research-engine/in-session-runtime.md section 3.3): the jobs the
# driving session spawns subagents for, the driver itself and the owner's repin
JOB = "research-job"
JOB_HANDED = "research-job-handed"
JOB_CLAIM = "research-job-claim"
JOB_RESULT = "research-job-result"
JOB_REFUSED = "research-job-refused"
JOB_EXPIRED = "research-job-expired"
JOB_LATE = "research-job-late"
DRIVER = "research-driver"
DRIVER_AUDIT = "research-driver-audit"
REPIN = "research-repin"
GUARD_DENY = "research-guard-deny"               # in-session-runtime.md section 18.1

ALL_KINDS = (RUN_START, RUNNER, ROUND_OPEN, ROUND, MERGE, FINALIZE, INFRA, CONTRACT,
             CONTRACT_DRIFT, LAUNCH, LAUNCH_END, PROPOSAL, WORKTREE, TAMPER, DIFF, DIFF_REFUSED, PURGE,
             GATE, ACCEPT, ATTEMPT,
             VERIFY_OPEN, VERIFY_ASSIGN, VERIFY_REFUSED, VERIFY_REVIEW, VERIFY_APPLY_FAILURE,
             PROOF_VERDICT, VERIFY_ROUND, PROOF_EXTEND, PROOF_RULING, VERIFY_END, AUDIT_REVIEW,
             AUDIT_VERDICT, AUDIT_REFUSED, REPRODUCER, BASELINE, PREPARE, REPAIR_RESULT, HOLDOUT,
             HOLD, HOLD_LIFTED, STATUS, STOP, EXTEND, RESUME, REPORT, GRANT, REFUSED,
             JOB, JOB_HANDED, JOB_CLAIM, JOB_RESULT, JOB_REFUSED, JOB_EXPIRED, JOB_LATE, DRIVER,
             DRIVER_AUDIT, REPIN, GUARD_DENY)
# written by alpaca.runlog.reviews.submit, never passed to emit()
REVIEW_KINDS = (VERIFY_REVIEW, AUDIT_REVIEW)

ACTOR = "research-runner"
WORKER_PREFIX = ""                               # the claims worker is the bare run id


def runner_session(run: str) -> str:
    return "research-%s" % run


# t-089's reason codes (verifier.md section 7), which errors.CODES does not restate
T089_REASON = re.compile(r"(VERIFY|AUDIT)-[A-Z0-9]+(-[A-Z0-9]+)*")


def emit(conn, kind: str, *, run: str | None, ref: str, op: str | None, session: str,
         actor: str = ACTOR, code: int | None = None, reason: str | None = None,
         data: dict | None = None, in_txn: bool = False) -> dict:
    """Append one research event. data gains `run` when run is given (every kind but CONTRACT,
    which precedes any run). A band code `code` (an exit code of alpaca/gates/verdict.py) gains
    `verdict` (vc.name_of(code)), so the hub classifies failures, and `exit` (int(code)); the
    integer never goes in `code`. A reason `reason` (a key of errors.CODES, or a t-089 VERIFY-*
    or AUDIT-* code) gains `code`, the string; a kind may pass either keyword alone or both, and
    section 8.3 names which kinds pass which. `code` and `exit` come only from the keywords, so
    data holding either is refused, and data holding `verdict` is refused when code is given, so
    the word always matches the band (a writer that sets its own `verdict` word passes no code
    and gets no `exit`). Raises ValueError for a kind outside ALL_KINDS or in REVIEW_KINDS, an
    unknown reason, or data breaking those rules. in_txn joins the caller's db.transaction."""
    if kind not in ALL_KINDS or kind in REVIEW_KINDS:
        raise ValueError("unknown research event kind %r" % (kind,))
    if reason is not None and reason not in errors.CODES and not T089_REASON.fullmatch(reason):
        raise ValueError("unknown reason code %r" % (reason,))
    body = dict(data or {})
    if "code" in body or "exit" in body or (code is not None and "verdict" in body):
        raise ValueError("data carries a key emit owns: %r" % (sorted(body),))
    if run is not None:
        body["run"] = run
    if code is not None:
        body["verdict"] = vc.name_of(code)
        body["exit"] = int(code)
    if reason is not None:
        body["code"] = reason
    return db.append_event(conn, session=session, actor=actor, kind=kind, op=op, ref=ref,
                           data=body, conn_in_txn=in_txn)
