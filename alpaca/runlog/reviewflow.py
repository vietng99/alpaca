"""An independent review of every captured run, then a GO or NO-GO from the main session.

A run's recorded result (PASS, FAIL, BLOCKED, INTERRUPTED) belongs to the caller's grader. This
module keeps two further layers beside it, and never changes the result:

  * review state: `pending`, `reviewing`, `reviewed`, `failed` or `stale`, for the checked review
    of the run's log (`alpaca.runlog.reviews`); `waiting` while the run has no final result and
    `not-applicable` when the caller says the run left no log to review;
  * the main decision: `GO` or `NO-GO`, recorded by the main session after it reads the review.

The loop: the main session `assign`s the run to a reviewer session with a lease and launches a
fresh native subagent with the returned brief (this module never launches an agent). The
reviewer reads the log and `submit`s a quote-checked review bound to the assignment; three
refused submits fail the assignment. The main session reads the review and `decide`s. A runner
calls `require_go` before it advances past the run and `require_retry` before it reruns it.
A reviewer never decides its own review, a result other than PASS never gets GO, and a changed
log, review or run record makes the review stale and clears the decision.

Every assignment, refusal, failure and decision is an append-only snapshot event of
`Flow.workflow_kind`; the newest one per reference is current. An OS lock per reference
serializes changes, so two sessions cannot assign the same run at once. Session names are
attribution, not authentication.

The caller owns every location and name through one `Flow`, as `reviews` does: where a
reference's log and reviews live, the record event kinds, and `subject`, which reads the run
as recorded ({"result": ..., "reason": ..., "reviewable": ...}). Everything `subject` returns
is hashed, so a decision binds the run record exactly as it was read.
"""
import contextlib
from dataclasses import dataclass
import fcntl
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Callable, Optional
import uuid

from alpaca import db
from alpaca.runlog import reviews as _kept

FINAL = ("PASS", "FAIL", "BLOCKED", "INTERRUPTED")
DECISIONS = ("GO", "NO-GO")
STATES = ("waiting", "not-applicable", "pending", "reviewing", "reviewed", "failed", "stale")
LEASE_MINUTES = 30
MAX_ATTEMPTS = 3

_now = time.time


@dataclass(frozen=True)
class Flow:
    """Where one domain keeps its runs' logs and reviews, and what its record events are called.

    `log` is the log file or a resolver called with the reference; `folder(ref)` is the directory
    that holds that reference's kept reviews and `locks` the directory for lock files, both inside
    the project root. `subject(ref)` returns the run as recorded, or None when there is no such
    run: "result" is required, "reason" is shown while no review explains the result, and
    "reviewable": False marks a run that captures no log. `refs()` lists the references
    `pending` looks at. `validate(ref, review)` may refuse a submit (it counts as a refused
    attempt) and `go_check(ref, review)` may refuse GO, for example when the run is no longer
    the current evidence; both raise ValueError. `skill` names the reviewer instructions the
    brief points to.
    """
    log: Any
    folder: Callable[[str], Any]
    subject: Callable[[str], Optional[dict]]
    locks: Any
    kind: str
    mark_kind: str
    workflow_kind: str
    ref_key: str = "receipt"
    refs: Optional[Callable[[], Any]] = None
    validate: Optional[Callable[[str, dict], None]] = None
    go_check: Optional[Callable[[str, dict], None]] = None
    skill: str = ""
    actor: str = _kept.ACTOR


def _lease(minutes):
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 240:
        raise ValueError("the lease must be 1 to 240 minutes")
    return minutes


def _who(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 200
            or any(ord(c) < 32 for c in value)):
        raise ValueError("an explicit session identity is required")
    return value


def _subject(flow, ref):
    meta = flow.subject(ref)
    if not isinstance(meta, dict) or not meta:
        raise ValueError("unknown %s %s" % (flow.ref_key, ref))
    return meta


def _bind(meta):
    raw = json.dumps(meta, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _log_sha(flow, ref):
    return hashlib.sha256(Path(_kept._log(flow.log, ref)).read_bytes()).hexdigest()


def _reviews(root, flow, ref):
    return _kept.reviews(root, ref, folder=flow.folder(ref), kind=flow.kind, mark_kind=flow.mark_kind,
                         ref_key=flow.ref_key)["reviews"]


def _read(root, flow, ref):
    conn = db.connect_readonly(str(Path(root).resolve()))
    try:
        row = conn.execute("SELECT data FROM events WHERE kind=? AND ref=? ORDER BY id DESC LIMIT 1",
                           (flow.workflow_kind, ref)).fetchone()
    finally:
        conn.close()
    return json.loads(row["data"]) if row else {}


def _save(root, flow, ref, data, session):
    return _kept._event(root, session, flow.workflow_kind, ref, dict(data, **{flow.ref_key: ref}),
                        actor=flow.actor)


@contextlib.contextmanager
def _lock(root, flow, ref):
    _, folder = _kept._inside(root, flow.locks)
    folder.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(ref.encode()).hexdigest()[:32] + ".lock"
    with (folder / name).open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _unreviewed_no_go(decision):
    """A NO-GO recorded without a review (the review failed) survives a failed state."""
    d = decision or {}
    return d if d.get("value") == "NO-GO" and d.get("review") is None else None


def state(root, flow, ref):
    """The run's result, review state, current decision and next action. Reads only."""
    ref = str(ref)
    meta = _subject(flow, ref)
    stored = _read(root, flow, ref)
    a = stored.get("assignment")
    out = {"ref": ref, "result": meta.get("result"), "review_state": "pending",
           "reason": str(meta.get("reason") or ""), "assignment": a, "review_id": None,
           "decision": stored.get("decision"), "disputed": 0,
           "next_action": "Main session: assign an independent reviewer"}
    current = _bind(meta)
    bound = {x.get("subject_sha256") for x in (out["decision"] or {}, a or {})} - {None}
    if bound and bound != {current}:
        return dict(out, review_state="stale", reason="The run record changed after the review started",
                    previous_decision=out["decision"], decision=None,
                    next_action="Main session: assign a fresh review of the changed run")
    if out["result"] not in FINAL:
        return dict(out, review_state="waiting", next_action="Wait for the run to finish")
    if meta.get("reviewable") is False:
        return dict(out, review_state="not-applicable",
                    next_action="This run captures no log; use its recorded checks")
    try:
        sha = _log_sha(flow, ref)
    except (OSError, ValueError) as exc:
        return dict(out, review_state="failed", reason="Captured log unavailable: %s" % exc,
                    previous_decision=out["decision"], decision=_unreviewed_no_go(out["decision"]),
                    next_action="Recover the captured log or record NO-GO; do not advance")
    kept = _reviews(root, flow, ref)
    own = next((r for r in kept if a and r.get("assignment") == a["id"]), None)
    # a fresh assignment owns the handoff even when an older review is still kept
    if a and not own:
        if stored.get("failure"):
            return dict(out, review_state="failed", reason=stored["failure"],
                        decision=_unreviewed_no_go(out["decision"]),
                        next_action="Main session: assign a replacement reviewer or record NO-GO")
        if a["expires_at"] <= _now():
            return dict(out, review_state="pending", reason="The review lease expired", decision=None,
                        next_action="Main session: reassign the review")
        return dict(out, review_state="reviewing", decision=None, next_action="Wait for the assigned reviewer")
    latest = kept[0] if kept else None
    if not latest:
        return out
    out["review_id"] = latest.get("id")
    if not latest.get("intact") or latest.get("log_sha256") != sha:
        return dict(out, review_state="stale", reason="The kept review or the captured log changed",
                    previous_decision=out["decision"], decision=None,
                    next_action="Main session: assign a fresh review")
    out.update(review_state="reviewed", next_action="Main session: read the review and record GO or NO-GO")
    if not a or latest.get("assignment") != a.get("id"):
        out["next_action"] = ("Main session: assign an independent reviewer; a review without an "
                              "assignment does not satisfy the gate")
    if out["decision"] and out["decision"].get("review") != latest.get("id"):
        out.update(decision=None, reason="A newer review needs a main-session decision")
    out["disputed"] = sum((f.get("mark") or {}).get("mark") == "disputed" for f in latest.get("findings", []))
    if out["disputed"] and out["decision"] and out["decision"]["value"] == "GO":
        out.update(previous_decision=out["decision"], decision=None, reason="The review has disputed findings",
                   next_action="Resolve the disputed findings before GO")
    if out["decision"]:
        out["next_action"] = ("Advance after the runner's own gate checks" if out["decision"]["value"] == "GO"
                              else "Investigate; do not advance")
    return out


def pending(root, flow, *, refs=None, session=None):
    """The runs that still need a review or a decision, as `state` rows, in reference order.

    `refs` defaults to `flow.refs()`. With `session`, runs assigned by another main session are
    left out. A run still running, or one that captures no log, is not pending.
    """
    if refs is None:
        if flow.refs is None:
            raise ValueError("pass refs, or give the Flow a refs reader")
        refs = flow.refs()
    out = []
    for ref in sorted({str(r) for r in refs or ()}):
        try:
            s = state(root, flow, ref)
        except ValueError:
            continue
        if s["review_state"] in ("waiting", "not-applicable"):
            continue
        if session and (s.get("assignment") or {}).get("parent_session") not in (None, session):
            continue
        if not s["decision"]:
            out.append(s)
    return out


def brief(flow, ref, assignment):
    """The instructions the main session hands its reviewer subagent."""
    return ("Review %s %s as session %s under assignment %s%s. Read the log and the run's evidence "
            "yourself, before any earlier review. Submit one quote-checked review bound to this "
            "assignment. Do not run, fix or rerun the work, and do not record GO or NO-GO. Return the "
            "review id, a short explanation and what stays uncertain."
            % (flow.ref_key, ref, assignment["reviewer_session"], assignment["id"],
               (", following " + flow.skill) if flow.skill else ""))


def assign(root, flow, ref, *, parent_session, reviewer_session, minutes=LEASE_MINUTES):
    """Record that `reviewer_session` reviews this run for `parent_session`, with a lease.

    Returns the assignment and the brief for the subagent. A live assignment still reviewing is
    refused; a reviewed one is replaced only by its own main session, and a decided one only by the
    session that decided it or the assignment's main session; an expired, failed or stale one is
    replaced. A missing log still assigns, as a failed review,
    so the main session can record NO-GO.
    """
    ref = str(ref)
    parent, reviewer = _who(parent_session), _who(reviewer_session)
    if parent == reviewer:
        raise ValueError("the reviewer session must differ from the main session")
    _lease(minutes)
    with _lock(root, flow, ref):
        s = state(root, flow, ref)
        if s["review_state"] in ("waiting", "not-applicable"):
            raise ValueError(s["next_action"])
        decided = s.get("decision") or {}
        owners = (decided.get("session"), (s.get("assignment") or {}).get("parent_session"))
        if s["review_state"] == "reviewed" and decided and parent not in owners:
            raise ValueError("%s %s already has a %s from %s; take it over before assigning a new review"
                             % (flow.ref_key, ref, decided.get("value"), decided.get("session")))
        try:
            sha, failure = _log_sha(flow, ref), None
        except (OSError, ValueError) as exc:
            sha, failure = None, "Captured log unavailable: %s" % exc
        old = _read(root, flow, ref)
        prev = old.get("assignment") or {}
        live = prev.get("expires_at", 0) > _now() and not old.get("failure")
        if live and (s["review_state"] == "reviewing"
                     or (s["review_state"] == "reviewed" and parent != prev.get("parent_session"))):
            raise ValueError("%s %s is already assigned; use its review or wait for the lease to expire"
                             % (flow.ref_key, ref))
        a = {"id": uuid.uuid4().hex, "fence": prev.get("fence", 0) + 1, "parent_session": parent,
             "reviewer_session": reviewer, "expires_at": _now() + minutes * 60, "attempts": 0,
             "log_sha256": sha, "subject_sha256": _bind(_subject(flow, ref))}
        _save(root, flow, ref, {"assignment": a, "decision": None, "failure": failure}, parent)
        return dict(a, brief=brief(flow, ref, a))


def _owned(root, flow, ref, assignment, session):
    data = _read(root, flow, ref)
    a = data.get("assignment") or {}
    if a.get("id") != assignment or a.get("reviewer_session") != _who(session):
        raise ValueError("only the assigned reviewer may submit, renew or fail this assignment")
    if a.get("expires_at", 0) <= _now():
        raise ValueError("the review assignment expired; ask the main session to reassign it")
    return data, a


def renew(root, flow, ref, assignment, *, session, minutes=LEASE_MINUTES):
    """The assigned reviewer extends its lease; returns the assignment."""
    ref = str(ref)
    _lease(minutes)
    with _lock(root, flow, ref):
        data, a = _owned(root, flow, ref, assignment, session)
        a["expires_at"] = _now() + minutes * 60
        _save(root, flow, ref, data, session)
        return a


def takeover(root, flow, ref, *, session, reason):
    """A new main session takes over an expired assignment whose review was accepted.

    A decision already recorded stays until the new main session records its own."""
    ref = str(ref)
    session = _who(session)
    if not str(reason or "").strip():
        raise ValueError("a takeover needs a reason")
    with _lock(root, flow, ref):
        data = _read(root, flow, ref)
        a = data.get("assignment")
        if not a or a["expires_at"] > _now():
            raise ValueError("only an expired assignment can be taken over")
        if session == a["reviewer_session"]:
            raise ValueError("the reviewer cannot become the main session")
        own = next((r for r in _reviews(root, flow, ref) if r.get("assignment") == a["id"]), None)
        if not own or not own.get("intact") or state(root, flow, ref)["review_state"] != "reviewed":
            raise ValueError("an unfinished or stale review is reassigned, not taken over")
        a.update(parent_session=session, expires_at=_now() + LEASE_MINUTES * 60)
        data["takeover_reason"] = str(reason).strip()[:2000]
        _save(root, flow, ref, data, session)
        return a


def submit(root, flow, ref, review, *, assignment, session):
    """The assigned reviewer's checked review; a resubmit of an accepted one returns it again."""
    ref = str(ref)
    with _lock(root, flow, ref):
        data, a = _owned(root, flow, ref, assignment, session)
        own = next((r for r in _reviews(root, flow, ref) if r.get("assignment") == assignment), None)
        if own and own.get("intact"):
            try:
                same = own.get("log_sha256") == _log_sha(flow, ref)
            except (OSError, ValueError):
                same = False
            if same:
                return {"verdict": "PASS", "review": own["id"], "path": own["path"],
                        "findings": len(own["findings"])}
        if data.get("failure"):
            raise ValueError("this review failed; the main session must assign a replacement")
        try:
            if a.get("subject_sha256") != _bind(_subject(flow, ref)):
                raise ValueError("the run record changed since the assignment; ask for a new assignment")
            if not isinstance(review, dict) or review.get("log_sha256") != a.get("log_sha256"):
                raise ValueError("log_sha256 is not the log this assignment was given; ask for a new assignment")
            if flow.validate:
                flow.validate(ref, review)
            result = _kept.submit(root, ref, review, log=flow.log, folder=flow.folder(ref), kind=flow.kind,
                                  ref_key=flow.ref_key, session=session, actor=flow.actor,
                                  context={"assignment": assignment, "parent_session": a["parent_session"],
                                           "reviewer_session": a["reviewer_session"]})
        except Exception as exc:   # every refusal counts, whatever the checker raised
            reason = str(exc) if isinstance(exc, (ValueError, OSError)) else "%s: %s" % (type(exc).__name__, exc)
            a["attempts"] = a.get("attempts", 0) + 1
            data["last_refusal"] = reason[:2000]
            if a["attempts"] >= MAX_ATTEMPTS:
                data["failure"] = "Review refused %d times; last: %s" % (a["attempts"], reason[:1900])
            _save(root, flow, ref, data, session)
            raise ValueError(reason) from exc
        data.update(review=result["review"], decision=None)
        _save(root, flow, ref, data, session)
        return result


def fail(root, flow, ref, assignment, reason, *, session):
    """The assigned reviewer gives up with a reason; the run shows Review failed."""
    ref = str(ref)
    if not str(reason or "").strip():
        raise ValueError("a failure reason is required")
    with _lock(root, flow, ref):
        data, _ = _owned(root, flow, ref, assignment, session)
        if any(r.get("assignment") == assignment for r in _reviews(root, flow, ref)):
            raise ValueError("this assignment already has an accepted review; it cannot fail now")
        data["failure"] = str(reason).strip()[:2000]
        _save(root, flow, ref, data, session)
    return state(root, flow, ref)


def _validated(root, flow, ref, review_id, s):
    if s["review_state"] != "reviewed":
        raise ValueError("review %s: %s" % (s["review_state"], s["reason"] or s["next_action"]))
    r = _reviews(root, flow, ref)[0]
    if r.get("id") != review_id:
        raise ValueError("decide on the latest intact review, %s" % r.get("id"))
    a = s.get("assignment") or {}
    if not r.get("assignment") or r["assignment"] != a.get("id"):
        raise ValueError("an independently assigned review is required")
    if r.get("reviewer_session") == a.get("parent_session"):
        raise ValueError("the reviewer cannot act as the main session")
    return r


def _go_checks(flow, ref, r):
    result = _subject(flow, ref).get("result")
    if result != "PASS":
        raise ValueError("GO needs a recorded PASS; this run is %s" % result)
    if r.get("outcome") != "matches-verdict":
        raise ValueError("the review does not support the recorded result")
    if any((f.get("mark") or {}).get("mark") == "disputed" for f in r.get("findings", [])):
        raise ValueError("the review has disputed findings")
    if flow.go_check:
        flow.go_check(ref, r)


def decide(root, flow, ref, review_id, decision, rationale, *, session):
    """The main session's GO or NO-GO on the latest assigned review.

    NO-GO without a review is allowed only when the review failed (the log is missing, or the
    reviewer gave up), so a broken run can always be held. GO needs a recorded PASS, an intact
    assigned review that supports it, no disputed finding and the caller's `go_check`.
    """
    ref = str(ref)
    session = _who(session)
    if decision not in DECISIONS:
        raise ValueError("the decision must be GO or NO-GO")
    if not isinstance(rationale, str) or not 4 <= len(rationale.strip()) <= 2000:
        raise ValueError("a rationale of 4 to 2000 characters is required")
    with _lock(root, flow, ref):
        data = _read(root, flow, ref)
        a = data.get("assignment") or {}
        if session != a.get("parent_session") or session == a.get("reviewer_session"):
            raise ValueError("only the assigned main session may decide")
        s = state(root, flow, ref)
        if review_id is None and decision == "NO-GO" and s["review_state"] == "failed":
            r = {}
        else:
            r = _validated(root, flow, ref, review_id, s)
        if decision == "GO":
            _go_checks(flow, ref, r)
        bound = _bind(_subject(flow, ref))
        if a.get("subject_sha256") != bound:
            raise ValueError("the run record changed after the assignment; assign a fresh review")
        d = {"value": decision, "review": review_id, "rationale": rationale.strip(), "session": session,
             "log_sha256": r.get("log_sha256"), "subject_sha256": bound, "decided_at": _now()}
        data["decision"] = d
        event = _save(root, flow, ref, data, session)
        return dict(d, event=event.get("id"))


def require_go(root, flow, ref):
    """Raise ValueError unless the run holds a current GO; call it before advancing past the run.
    A run that captures no log (`reviewable: False`) needs no GO, but it still needs PASS."""
    ref = str(ref)
    s = state(root, flow, ref)
    if s["review_state"] == "not-applicable":
        if s["result"] != "PASS":
            raise ValueError("%s %s is %s; a run with no log to review advances only on PASS"
                             % (flow.ref_key, ref, s["result"]))
        return s
    d = s.get("decision") or {}
    if d.get("value") != "GO":
        raise ValueError("%s %s needs a main-session GO; %s" % (flow.ref_key, ref, s["next_action"]))
    r = _validated(root, flow, ref, d.get("review"), s)
    _go_checks(flow, ref, r)
    if d.get("log_sha256") != r.get("log_sha256"):
        raise ValueError("the decision no longer binds this log")
    return s


def require_retry(root, flow, ref):
    """Raise ValueError unless the run was reviewed and decided; call it before rerunning it."""
    ref = str(ref)
    s = state(root, flow, ref)
    if s["review_state"] == "not-applicable":
        return s
    d = s.get("decision") or {}
    if d.get("value") not in DECISIONS:
        raise ValueError("a review and a main decision are required before rerunning %s %s"
                         % (flow.ref_key, ref))
    if s["review_state"] == "failed" and d["value"] == "NO-GO":
        return s
    _validated(root, flow, ref, d.get("review"), s)
    return s


def glance(root, flow, ref, *, review=None):
    """The At a glance facts for one run, from recorded facts and the checked review only.

    `review` selects a kept review id (default: the newest). Nothing is inferred: without a
    review, `why` is the recorded reason, never a diagnosis.
    """
    ref = str(ref)
    s = state(root, flow, ref)
    kept = _reviews(root, flow, ref)
    if review:
        r = next((x for x in kept if x.get("id") == review), None)
        if not r:
            raise ValueError("no review %s for %s %s" % (review, flow.ref_key, ref))
    else:   # the newest review speaks for the run only while the state names it
        r = kept[0] if kept and kept[0].get("id") == s.get("review_id") else None
    current = bool(r) and r.get("id") == s.get("review_id")
    d = s.get("decision") or {}
    if r and d.get("review") != r.get("id"):
        d = {}
    usable = bool(r) and r.get("intact") is not False and (not current or s["review_state"] == "reviewed")
    why = ((r.get("quick_summary") or r.get("summary")) if usable else "") or s["reason"] or "No review yet"
    primary = next((f for f in r.get("findings", []) if f["id"] == r.get("primary_finding")), None) if usable else None
    warnings = []
    if r and r.get("intact") is False:
        warnings.append("The kept review file no longer matches the record; do not rely on it")
    if r and not current:
        warnings.append("An earlier review; the run's review state is %s" % s["review_state"])
    if s.get("disputed") and current:
        warnings.append("%d finding(s) disputed; resolve them before GO" % s["disputed"])
    return {"ref": ref, "result": s["result"], "why": why[:360],
            "review": s["review_state"] if (current or not r) else "historical",
            "decision": d.get("value") or ("awaiting decision" if (current or not r) else "none for this review"),
            "decision_reason": d.get("rationale", ""),
            "evidence": {"line": primary["line"], "end_line": primary["end_line"]} if primary else None,
            "next_check": (r.get("next_check") or "") if usable else "",
            "next_action": s["next_action"], "warnings": warnings}


def glance_lines(g):
    """`glance` as the fixed text block that heads a text review."""
    out = ["AT A GLANCE", "Result: %s" % (g["result"] or "unknown"), "Why: %s" % g["why"],
           "Review: %s" % g["review"], "Main decision: %s" % g["decision"]]
    if g["decision_reason"]:
        out.append("Decision reason: %s" % g["decision_reason"])
    if g["evidence"]:
        e = g["evidence"]
        out.append("Evidence: line %d" % e["line"] if e["end_line"] == e["line"]
                   else "Evidence: lines %d to %d" % (e["line"], e["end_line"]))
    if g["next_check"]:
        out.append("Next check: %s" % g["next_check"])
    out.append("Next action: %s" % g["next_action"])
    out += ["WARNING: %s" % w for w in g["warnings"]]
    return out
