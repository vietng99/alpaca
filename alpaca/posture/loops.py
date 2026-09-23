"""Bounded retries, the stuck report, and halt-this-thread (M3.4).

Sources: spec 5.6:415 (the Stop hook refuses an L6 stop without a done marker), 5.3:317-319
(bank and continue), 7.5:788-798 (failure handling); absorb-gap AG-M17 (bounded retries, then a
stuck report, then halt that thread). Alpaca-native, no vendored source.

The rule this enforces, stated once: UNATTENDED NEVER MEANS UNBOUNDED. A row that fails its
instrument keeps being retried only up to a DECLARED bound. On the bound the thread over that row
HALTS, a stuck report naming the last three attempts and their verdicts is emitted (a message, a
pad line, a banked owner-owed question), and the REST of the op keeps working. A halt stops that
one thread, never the run.

Where the count lives. Every attempt is one append-only `loop-attempt` event keyed on the pair
(row, instrument). The count is the number of those events for the pair; it is read from the
record, never from a process variable, so a crash between attempts cannot reset it (the resumed
process reads the same number). This is the whole point of counting in the record.

The two instruments this module and M3.1 keep apart (do not conflate): the LEVEL in force
(`alpaca.posture.level`) says how much the agent decides this session; the BOUND here says how many
times a failing row is retried before its thread halts. A full-autodrive level does not raise the
bound; a high level with no bound would be the unbounded loop this module forbids.

The Stop hook half (spec 5.6:415). At the full-autodrive level the Stop hook refuses a top-level
stop while a DONE MARKER is genuinely absent, so an unattended run does not stop short of its
goal. It does NOT refuse while a thread is HALTED: a halt is a bounded terminal state that has
already been surfaced (the stuck report), and forcing the run onward past it is exactly the
unbounded loop the bound exists to prevent. `stop_should_refuse` answers only this question.
"""
from __future__ import annotations

from alpaca import db, paths, project, util
from alpaca.phase import defaults
from alpaca.posture import level

#: event kinds. An attempt, a halt, and a run-level done marker each land as one hash-chained
#: event; the fold over them is the current state (there is no separate table to keep in step).
KIND_ATTEMPT = "loop-attempt"
KIND_HALT = "loop-halt"
KIND_DONE = "loop-done"

#: the default retry bound when project.yaml declares none. Three failures, then the thread halts;
#: the stuck report then names all three.
DEFAULT_BOUND = 3
PROJECT_KEY = "loop_bound"

#: the report names at most this many attempts (the last three, spec / AG-M17).
REPORT_LAST = 3

#: the full-autodrive level at or above which the Stop hook refuses a stop with no done marker.
FULL_AUTODRIVE_LEVEL = 6

#: the class a stuck-thread question is banked under. Owner-owed (routes to the owner): a stuck
#: loop is a human decision, never machine-decided even at full autodrive.
STUCK_CLASS = "stuck"


def _root(root=None) -> str:
    return root or paths.root()


# --------------------------------------------------------------------------- the retry counter
def _attempt_events(conn, ref, instrument=None) -> list:
    """Every attempt event for the (ref, instrument) pair in append order (oldest first)."""
    out = []
    for e in db.events(conn, kind=KIND_ATTEMPT, limit=10 ** 9):
        d = e["data"]
        if d.get("ref") != ref:
            continue
        if instrument is not None and d.get("instrument") != instrument:
            continue
        out.append(e)
    return out


def count(conn, ref, instrument=None) -> int:
    """How many attempts the record holds for the (ref, instrument) pair. Read from the record,
    so a crash and resume reads the same number, never zero."""
    return len(_attempt_events(conn, ref, instrument=instrument))


def attempt(conn, ref, instrument=None, *, verdict=None, detail=None, session=None,
            actor="instrument") -> int:
    """Record one failed attempt of `instrument` over row `ref` and return the running count.

    The count is kept in the append-only record, keyed on the pair (ref, instrument), so a crash
    between attempts never resets it. `verdict` is the failing verdict (a contract band name or
    number); `detail` is an optional human pointer. Returns the new count.
    """
    n = count(conn, ref, instrument=instrument) + 1
    rec = {"ref": str(ref), "instrument": instrument, "n": n,
           "verdict": None if verdict is None else str(verdict),
           "detail": None if detail is None else str(detail)}
    db.append_event(conn, session=session or "instrument", actor=actor or "instrument",
                    kind=KIND_ATTEMPT, ref=str(ref), data=rec)
    return n


def bound(root=None) -> int:
    """The declared retry bound: project.yaml `loop_bound`, else the default (three). A malformed
    value falls back to the default rather than crashing the caller."""
    try:
        raw = project.load(_root(root)).get(PROJECT_KEY)
    except Exception:
        raw = None
    try:
        b = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_BOUND
    return b if b > 0 else DEFAULT_BOUND


def exhausted(conn, ref, instrument=None, *, root=None) -> bool:
    """True once the (ref, instrument) pair has reached its declared bound: the point at which
    the thread over that row should halt."""
    return count(conn, ref, instrument=instrument) >= bound(root)


# --------------------------------------------------------------------------- the stuck report
def stuck_report(conn, ref, instrument=None, *, root=None) -> dict:
    """The report for a stuck (ref, instrument) pair: the row, the instrument, the count, the
    bound, and the LAST three attempts with their verdicts in record order.

    Naming the last three (never all of them) keeps the report bounded no matter how many retries
    ran; the pointers (row, instrument, count, bound) locate the thread on the board.
    """
    evs = _attempt_events(conn, ref, instrument=instrument)
    last = evs[-REPORT_LAST:]
    attempts = [{"n": e["data"].get("n"), "verdict": e["data"].get("verdict"),
                 "detail": e["data"].get("detail"), "ts": e["ts"]} for e in last]
    return {"ref": str(ref), "instrument": instrument, "count": len(evs),
            "bound": bound(root), "last_three": attempts}


def _report_lines(rep) -> list:
    """The stuck report as human lines, shared by the message body and the pad block."""
    head = ("thread halted: row %s / instrument %s failed %d time(s), bound %s"
            % (rep["ref"], rep["instrument"], rep["count"], rep["bound"]))
    lines = [head]
    for a in rep["last_three"]:
        tail = (" -- %s" % a["detail"]) if a.get("detail") else ""
        lines.append("  attempt %s: %s%s" % (a["n"], a.get("verdict") or "(no verdict)", tail))
    return lines


# --------------------------------------------------------------------------- halt this thread
def _halt_events(conn) -> list:
    return db.events(conn, kind=KIND_HALT, limit=10 ** 9)


def is_halted(conn, ref) -> bool:
    """True when a halt has been recorded for row `ref`."""
    return any(e["data"].get("ref") == str(ref) for e in _halt_events(conn))


def halted(conn) -> list:
    """The halted rows, in the order they first halted (deduplicated)."""
    seen, out = set(), []
    for e in _halt_events(conn):
        r = e["data"].get("ref")
        if r is not None and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def halt(conn, ref, reason, *, instrument=None, session=None, actor="loops", root=None,
         emit=True) -> dict:
    """Halt the thread over row `ref` and surface why. Stops THAT thread only.

    Records the halt with its stuck report, and (when `emit`) posts the report as a `blocker`
    message, and banks an owner-owed question that blocks the halted row. The pad line is a
    projection (`pad_lines`) rendered from the halt event, so it needs no separate write. Leaves
    every other row of the op runnable: a halt is never a run-wide stop.
    """
    root = _root(root)
    rep = stuck_report(conn, ref, instrument=instrument, root=root)
    rec = {"ref": str(ref), "instrument": instrument, "reason": str(reason), "report": rep}
    db.append_event(conn, session=session or "loops", actor=actor or "loops",
                    kind=KIND_HALT, ref=str(ref), data=rec)
    if emit:
        from alpaca import messages
        body = "%s\n%s" % (str(reason), "\n".join(_report_lines(rep)))
        try:
            # a blocker on the board, addressed to the owner; pointers travel IN the body (a
            # bare row ref need not be a resolvable message pointer), so no pointer is attached.
            messages.post(conn, "loops", "owner", "blocker", body,
                          session=session or "loops", root=root)
        except Exception:
            pass
        try:
            _bank_stuck(conn, ref, rep, session=session)
        except Exception:
            pass
    return rec


def _bank_stuck(conn, ref, rep, *, session=None):
    from alpaca import questions
    text = ("row %s / instrument %s is stuck after %d attempt(s) at bound %s; the last verdicts "
            "were %s -- an owner decision is owed" % (
                rep["ref"], rep["instrument"], rep["count"], rep["bound"],
                ", ".join("%s=%s" % (a["n"], a.get("verdict") or "?") for a in rep["last_three"])))
    return questions.bank(conn, text, STUCK_CLASS, blocks=[str(ref)], session=session,
                          actor="loops")


def pad_lines(conn, root=None) -> list:
    """The pad block naming every halted thread and its stuck-report pointers, empty when no
    thread is halted. Shared by the pad renderer so the pad and the board name the same thread."""
    events = _halt_events(conn)
    if not events:
        return []
    lines = ["## HALTED threads (bounded retries reached)", "",
             "A thread halted after its instrument failed the declared number of times. An owner "
             "decision is owed; do not silently re-run.", ""]
    for r in halted(conn):
        latest = None
        for e in events:
            if e["data"].get("ref") == r:
                latest = e
        rep = (latest["data"].get("report") if latest else None) or {}
        lines.append("- HALT row %s / instrument %s (%d attempt(s), bound %s)" % (
            r, rep.get("instrument"), rep.get("count", 0), rep.get("bound")))
        for a in rep.get("last_three", []):
            lines.append("    - attempt %s: %s" % (a.get("n"), a.get("verdict") or "(no verdict)"))
    return lines


# --------------------------------------------------------------------------- the done marker
def mark_done(conn, session, *, ref=None, actor="agent") -> dict:
    """Record a run-level done marker for `session`: the run declares itself finished. The Stop
    hook reads this to decide whether refusing a top-level stop is still warranted."""
    rec = {"session": str(session), "ref": None if ref is None else str(ref)}
    db.append_event(conn, session=str(session), actor=actor or "agent",
                    kind=KIND_DONE, ref=str(session), data=rec)
    return rec


def done_marker_present(conn, session) -> bool:
    """True when a done marker has been recorded for `session`."""
    for e in db.events(conn, kind=KIND_DONE, limit=10 ** 9):
        if e["data"].get("session") == str(session):
            return True
    return False


# --------------------------------------------------------------------------- the Stop hook half
def stop_should_refuse(conn, session, *, root=None):
    """Whether the Stop hook should refuse a top-level stop for `session`: (refuse, reason).

    Refuses ONLY when the level in force is full autodrive AND a done marker is genuinely absent
    AND no thread is halted. A halted thread is a bounded terminal state, already surfaced, so the
    hook does not force the run onward past it: unattended never means unbounded. Below full
    autodrive the hook never refuses.
    """
    root = _root(root)
    try:
        rank = level.in_force(conn, session, root=root)
    except Exception:
        return (False, "")
    if rank < FULL_AUTODRIVE_LEVEL:
        return (False, "")
    if done_marker_present(conn, session):
        return (False, "the run recorded a done marker")
    if halted(conn):
        return (False, "a thread is halted; the halt is the bounded terminal state, do not force "
                       "the run onward (unattended is never unbounded)")
    return (True, "L%d stop refused: no done marker and no halt; the run has not reached its goal"
                  % rank)
