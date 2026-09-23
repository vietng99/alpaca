"""Phase entry by level and phase sign-out (M1.15).

Ported from the earlier harness gates/phase_gate.py and de-signed for Alpaca per spec 5.2 item 8 and D2
(spec:151). Two adaptations replace the earlier harness signing machinery wholesale:

  * `enter(conn, op, phase, level)` is a LEVEL COMPARISON, not a grant check: entry is allowed
    iff the level in force reaches the phase's declared entry level AND the previous phase is
    discharged. The earlier harness L6-grant read (`breakglass.grant_live`), the grant descriptor and the
    grant minting are all GONE (D1: no signing anywhere, no break-glass token).
  * `sign_out(conn, op, phase, state)` keeps the ported content guards 0-3 and drops the whole
    boundary-signature branch (`_verify_boundary_sig`, sigkey, the solo/dev OVERRIDDEN path) and
    the L6-grant minting a clean PLAN sign-out used to emit. The four guards, in order:
        0) the phase state is exactly plain JSON ............ BLOCKED (canonicalized fail-closed)
        1) every metric-closing step's sampled_from ran ..... BLOCKED (a coverage gap)
        2) every step carries its exit evidence ............. FAIL    (an unbacked "done")
        3) no open flow-break/fundamental/owner-choice q ..... FAIL   (an owner question unresolved)

    (TD guard 4, the tapeout OVERRIDDEN-row check, was break-glass machinery; D1 drops the
    OVERRIDDEN concept with the rest of signing, so there is no guard 4 in Alpaca.)

A skipped phase is recorded as skipped with a reason (Q9): `skip(conn, op, phase, reason)`.

Refusals surface through `alpaca.checklist.Halt` with a verdict-band code from `alpaca.gates.verdict`,
never a private copy of those numbers.
"""
from __future__ import annotations

from alpaca import db
from alpaca.checklist import Halt, verdict_row
from alpaca.gates import verdict as vc
from alpaca.phase import defaults, doors

INSTRUMENT = "phase-gate"

# ---- reason tokens the guards bind to (a caller greps ONE token, not prose) ----
R_UNKNOWN_PHASE = "PHASE-UNKNOWN"
R_ENTRY_LEVEL_TOO_LOW = "PHASE-ENTRY-LEVEL-TOO-LOW-BLOCKED"
R_ENTRY_PREV_NOT_DISCHARGED = "PHASE-ENTRY-PREVIOUS-NOT-DISCHARGED-BLOCKED"
R_STATE_NOT_CANONICAL = "PHASE-STATE-NOT-CANONICAL-BLOCKED"
R_STATE_MALFORMED = "PHASE-STATE-MALFORMED"
R_SAMPLED_NOT_RUN = "PHASE-SAMPLED-FROM-NOT-RUN-BLOCKED"
R_MISSING_EVIDENCE = "PHASE-SIGNOUT-MISSING-EXIT-EVIDENCE-REJECTED"
R_OPEN_QUESTION = "PHASE-SIGNOUT-OPEN-QUESTION-REJECTED"
R_SIGNED_OUT = "PHASE-SIGNED-OUT"
R_ENTERED = "PHASE-ENTRY-LEVEL-OK"
# M3.2: entry refused because the standing authority does not authorize it (expired, revoked,
# absent, or out of scope/level). The detail carries the alpaca.posture.authority reason token.
R_AUTHORITY_INVALID = "PHASE-ENTRY-AUTHORITY-INVALID-BLOCKED"

#: raise-to-owner question classes (autonomy-model): an OPEN one of these blocks a sign-out; a
#: 'research'-class question is decidable by the harness and does not block a close.
_BLOCKING_QUESTION_CLASSES = frozenset({"flow-break", "fundamental", "owner-choice"})


def _record(conn, kind, op, phase, session, data):
    """Append one hash-chained event; alpaca is the sole writer. Best-effort so a record that cannot
    be reached never turns a phase act into a crash."""
    try:
        db.append_event(conn, session=session or "instrument", actor=INSTRUMENT, kind=kind,
                        op=op, ref=phase, data=data)
    except Exception:
        pass


def phase_discharged(conn, op, phase) -> bool:
    """True iff every `item` obligation row for (op, phase) folds to discharged or waived. An
    empty universe is NOT discharged: a phase with no obligations has not been shown complete
    (this mirrors the door's vacuous-universal rule)."""
    return doors.phase_discharge_code(conn, op, phase) == vc.PASS


# --------------------------------------------------------------------------- enter
def enter(conn, op, phase, level=None, *, session="instrument", record=True,
          now=None, scope=None) -> dict:
    """Authorize ENTRY into `phase` by a level comparison (replaces the earlier harness grant check).

    Allowed iff the level in force reaches the phase's declared entry level AND the previous
    phase on the ladder is discharged. The first phase has no previous phase, so it is gated on
    the level alone. Returns {allowed, phase, level, reason} on success; raises Halt(BLOCKED)
    when entry is refused (level too low, or the previous phase not yet discharged).

    M3.1: when `level` is not supplied the gate reads the level in force FROM THE RECORD for
    `session` (`alpaca.posture.level.in_force`), never from the environment. An explicit level is
    still honoured, so existing callers are unaffected.

    M3.2: when `now` is supplied the gate also consults the standing authority
    (`alpaca.posture.authority.verify`) for (level, scope, now) and BLOCKs entry when no authority
    authorizes it (expired, revoked, absent, or out of scope/level). This is additive: with the
    default `now=None` the authority is not consulted and the behaviour is exactly M3.1's.
    """
    if phase not in defaults.LADDER:
        raise Halt(vc.BLOCKED, R_UNKNOWN_PHASE,
                   "%r is not a phase on the ladder %s" % (phase, list(defaults.LADDER)))
    if level is None:
        from alpaca.posture import level as posture_level
        level = "L%d" % posture_level.in_force(conn, session)
    lvl = defaults.level_num(level)
    declared = defaults.phase_entry_level(phase)
    if lvl < declared:
        raise Halt(vc.BLOCKED, R_ENTRY_LEVEL_TOO_LOW,
                   "cannot enter %r: the level in force is %s (rank %d) but %r declares entry "
                   "level %d; raise the level or record an owner go" % (phase, level, lvl, phase,
                                                                        declared))
    prev = defaults.previous_phase(phase)
    if prev is not None and not phase_discharged(conn, op, prev):
        raise Halt(vc.BLOCKED, R_ENTRY_PREV_NOT_DISCHARGED,
                   "cannot enter %r: the previous phase %r is not discharged (every obligation "
                   "row of %r must fold to discharged or waived first)" % (phase, prev, prev))
    if now is not None:
        from alpaca.posture import authority
        v = authority.verify(conn, level, scope, now)
        if not v.ok:
            raise Halt(vc.BLOCKED, R_AUTHORITY_INVALID,
                       "cannot enter %r: the standing authority does not authorize it (%s)"
                       % (phase, v.reason))
    if record:
        _record(conn, "phase-enter", op, phase, session,
                {"phase": phase, "level": level, "previous": prev, "reason": R_ENTERED})
    return {"allowed": True, "phase": phase, "level": level, "reason": R_ENTERED}


# --------------------------------------------------------------------------- sign_out
def canonical_state(state):
    """Project `state` to a FRESH plain-JSON copy through native type accessors, or BLOCK.

    Every guard reads the RETURNED copy, never the caller's object. A dict/list SUBCLASS that
    overrides `get()` / `items()` / `__iter__` / `__getitem__` could otherwise show the guards
    one set of steps or questions and hold another; rebuilding through `dict.items` and
    `list.__getitem__` (the non-overridable views) defeats that. A state that is not exactly
    plain JSON is refused fail-closed rather than scanned.
    """
    if state is None:
        return {}
    if not isinstance(state, dict):
        raise Halt(vc.BLOCKED, R_STATE_MALFORMED, "phase state must be a JSON object")
    return _canon(state, "state")


def _canon(obj, where):
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return str(obj)
    if isinstance(obj, dict):
        out = {}
        for k, v in dict.items(obj):          # NON-OVERRIDABLE view of the real storage
            if type(k) is not str:
                raise Halt(vc.BLOCKED, R_STATE_NOT_CANONICAL,
                           "%s has a %s key; only str keys are plain JSON"
                           % (where, type(k).__name__))
            out[k] = _canon(v, "%s.%s" % (where, k))
        return out
    if isinstance(obj, list):
        n = list.__len__(obj)                 # NON-OVERRIDABLE length + indexing
        return [_canon(list.__getitem__(obj, i), "%s[%d]" % (where, i)) for i in range(n)]
    raise Halt(vc.BLOCKED, R_STATE_NOT_CANONICAL,
               "%s is a %s, which is not plain JSON; the phase state store is append-only JSON, "
               "so a state that cannot be represented as plain JSON cannot close a boundary"
               % (where, type(obj).__name__))


def _steps_of(state):
    steps = state.get("steps", [])
    if not isinstance(steps, list):
        raise Halt(vc.BLOCKED, R_STATE_MALFORMED, "state.steps must be a list")
    return steps


def _questions_of(state):
    qs = state.get("questions", [])
    if not isinstance(qs, list):
        raise Halt(vc.BLOCKED, R_STATE_MALFORMED, "state.questions must be a list")
    return qs


def assert_sampled_from_ran(state):
    """Guard 1. Every step that CLOSES a metric must have run its `sampled_from` wrong-side
    control; a metric-closing step whose control never ran is a coverage gap the sign-out cannot
    see past -> BLOCKED (not FAIL: there is no verdict to trust)."""
    not_run = [step.get("id", "?") for step in _steps_of(state)
               if step.get("closes_metric") and step.get("sampled_from_ran") is not True]
    if not_run:
        raise Halt(vc.BLOCKED, R_SAMPLED_NOT_RUN,
                   "step(s) %s close a metric but their sampled_from wrong-side control did not "
                   "run; a metric whose wrong-side control never fired is not cleared"
                   % ", ".join(not_run))


def assert_exit_evidence(state):
    """Guard 2. Every step carries its exit evidence; a step missing it -> FAIL (its own claim of
    done is unbacked)."""
    missing = [step.get("id", "?") for step in _steps_of(state)
               if not str(step.get("exit_evidence") or "").strip()]
    if missing:
        raise Halt(vc.FAIL, R_MISSING_EVIDENCE,
                   "step(s) %s are missing their exit evidence; a phase cannot sign out over a "
                   "step whose done is unbacked" % ", ".join(missing))


def assert_no_open_blocking_question(state):
    """Guard 3. A phase cannot close with an OPEN owner-class question (flow-break / fundamental
    / owner-choice) unresolved -> FAIL."""
    open_blocking = []
    for q in _questions_of(state):
        cls = str(q.get("class") or "").strip().lower()
        status = str(q.get("status") or "").strip().lower()
        if cls in _BLOCKING_QUESTION_CLASSES and status != "resolved":
            open_blocking.append("%s[%s]" % (q.get("id", "?"), cls))
    if open_blocking:
        raise Halt(vc.FAIL, R_OPEN_QUESTION,
                   "open owner-class question(s) %s; a phase cannot sign out with a flow-break, "
                   "fundamental or owner-choice question unresolved" % ", ".join(open_blocking))


def sign_out(conn, op, phase, state, *, mode="review", session="instrument", record=True) -> dict:
    """Close a phase boundary. Runs the ported content guards 0-3 IN ORDER (block-then-reject: a
    coverage gap outranks a content refusal), then records the clean close as an event. There is
    no boundary-signature branch and no grant minting (D1). Returns {verdict: PASS, reason,
    phase, mode} on a clean close; raises Halt on any guard.
    """
    if phase not in defaults.LADDER:
        raise Halt(vc.BLOCKED, R_UNKNOWN_PHASE,
                   "%r is not a phase on the ladder %s" % (phase, list(defaults.LADDER)))
    state = canonical_state(state)            # guard 0  BLOCKED
    assert_sampled_from_ran(state)            # guard 1  BLOCKED
    assert_exit_evidence(state)               # guard 2  FAIL
    assert_no_open_blocking_question(state)   # guard 3  FAIL
    if record:
        _record(conn, "phase-signout", op, phase, session,
                {"phase": phase, "mode": mode, "reason": R_SIGNED_OUT})
    return {"verdict": vc.PASS, "reason": R_SIGNED_OUT, "phase": phase, "mode": mode}


# --------------------------------------------------------------------------- skip
def skip(conn, op, phase, reason, *, session="instrument") -> dict:
    """Record a SKIPPED phase, with a reason (Q9). A skip is a visible event on the record, never
    an invisible bypass; a skip with no reason is refused."""
    if phase not in defaults.LADDER:
        raise Halt(vc.BLOCKED, R_UNKNOWN_PHASE,
                   "%r is not a phase on the ladder %s" % (phase, list(defaults.LADDER)))
    if not reason or not str(reason).strip():
        raise Halt(vc.BLOCKED, "PHASE-SKIP-REQUIRES-REASON",
                   "a skipped phase must carry a reason; a bare skip is a silent bypass")
    _record(conn, "phase-skip", op, phase, session, {"phase": phase, "reason": reason})
    return {"phase": phase, "skipped": True, "reason": reason}
