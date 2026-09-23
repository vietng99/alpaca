"""A durable checker that fires a resume only when it is safe to (M3.12).

Ported from the earlier harness ops/kicker-check.py (doctrine/os-durable-timers.md), against the RECORD rather
than a war-room marker file and a STATE.md dispatch-ledger section. The mechanism is unchanged: a
scheduler-invoked checker asks "has the run gone quiet past its Grace window, and is the work
actually up for grabs?" and fires a resume ONLY when both hold. It never writes the record; it
reads it and, on FIRE, hands off to the resume driver (the sole writer).

The two file sources map onto the record:

  * the marker's last-touch  -> the record's last event timestamp (the last-touch of the store).
  * the dispatch-ledger claim -> alpaca.board's live claims. A live claim on any row means a worker
                                 still holds the work, so the kicker stands down (REFUSED) rather
                                 than double-dispatch a merely-slow-but-alive worker.

Fails safe: an empty or unreadable record is STATE-UNREADABLE and does nothing, never a blind fire
against state it cannot resolve. A false REFUSED (stand down when it could have fired) is the safe
failure direction; a false FIRE (fire while a live claim holds the work) is the unsafe one this
checker is built to avoid.
"""
from __future__ import annotations

import datetime

from alpaca import board, db, paths, project

#: the classification tokens (the ported vocabulary).
NO_FIRE = "NO-FIRE"
FIRE = "FIRE"
REFUSED = "REFUSED"
UNREADABLE = "STATE-UNREADABLE"

#: the Grace window when project.yaml declares none, in seconds. A run quiet longer than this is a
#: candidate for a resume; the project may override it with `kicker_grace_seconds`.
DEFAULT_GRACE_SECONDS = 300
PROJECT_KEY = "kicker_grace_seconds"


def grace_window(root=None) -> int:
    """The Grace window: project.yaml `kicker_grace_seconds`, else the default. A malformed value
    falls back to the default rather than crashing the caller. Read from the file, never code."""
    try:
        raw = project.load(str(root or paths.root())).get(PROJECT_KEY)
    except Exception:
        raw = None
    try:
        g = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_GRACE_SECONDS
    return g if g > 0 else DEFAULT_GRACE_SECONDS


def _age_seconds(last_ts, now_ts):
    """Elapsed seconds between the record's last-touch and `now`, or None when either cannot be
    parsed (treated by the caller as unreadable, fails safe)."""
    try:
        a = datetime.datetime.fromisoformat(last_ts)
        b = datetime.datetime.fromisoformat(now_ts)
    except (TypeError, ValueError):
        return None
    return (b - a).total_seconds()


def check(root, *, now=None, grace_seconds=None, conn=None) -> str:
    """Run the kicker check once and return one of NO-FIRE | FIRE | REFUSED | STATE-UNREADABLE.

    `now` is an ISO instant (a FixedClock's stamp under test), defaulting to the process clock.
    `grace_seconds` overrides the project value for the call. `conn` reuses an open connection.
    """
    from alpaca import util
    close = False
    if conn is None:
        conn = db.connect(str(root))
        close = True
    try:
        now = now or util.now_iso()
        grace = grace_seconds if grace_seconds is not None else grace_window(root)

        last = db.last_event(conn)
        if last is None:
            return UNREADABLE                       # no last-touch to judge: fails safe
        age = _age_seconds(last["ts"], now)
        if age is None:
            return UNREADABLE
        if age <= grace:
            return NO_FIRE                          # normal cadence, standing down

        # Grace exceeded: the work is only up for grabs when no live claim holds a row.
        if board.live_claims(conn, now=now):
            return REFUSED                          # a live claim holds the work: never double-fire
        return FIRE
    finally:
        if close:
            conn.close()


def selftest() -> int:
    """Negative-control suite for the fire decision, ported from the earlier harness ops/kicker-check.py against
    the record. The load-bearing control is the live claim: a claim whose lease outlasts `now`
    must REFUSE (never FIRE); a released or expired claim must FIRE; an empty record is UNREADABLE.
    Returns 0 on all-PASS, else 1."""
    import shutil
    import tempfile

    res = []
    now = "2026-09-16T01:00:00+00:00"

    base = tempfile.mkdtemp(prefix="kicker-selftest-")
    try:
        # empty record -> UNREADABLE (fails safe).
        conn = db.connect(base)
        res.append(("empty record -> STATE-UNREADABLE (fails safe)",
                    check(base, now=now, grace_seconds=300, conn=conn) == UNREADABLE))

        # a fresh touch within Grace -> NO-FIRE.
        db.append_event(conn, session="s", actor="a", kind="heartbeat", data={"n": 1},
                        clock=lambda: "2026-09-16T00:59:50+00:00")
        res.append(("touched within Grace -> NO-FIRE",
                    check(base, now=now, grace_seconds=300, conn=conn) == NO_FIRE))
        conn.close()

        # quiet past Grace, ledger clear -> FIRE.
        b2 = tempfile.mkdtemp(prefix="kicker-selftest-")
        conn2 = db.connect(b2)
        db.append_event(conn2, session="s", actor="a", kind="heartbeat", data={"n": 1},
                        clock=lambda: "2026-09-16T00:00:00+00:00")
        res.append(("quiet past Grace, ledger clear -> FIRE",
                    check(b2, now=now, grace_seconds=300, conn=conn2) == FIRE))

        # a live claim holds the work -> REFUSED (never a double-fire). The load-bearing control.
        db.append_event(conn2, session="w1", actor="w1", kind=board.CLAIM_KIND, ref="R1",
                        data={"worker": "w1", "lease_until": "2026-09-16T02:00:00+00:00"},
                        clock=lambda: "2026-09-16T00:00:01+00:00")
        res.append(("quiet past Grace, live claim holds a row -> REFUSED (never a double-fire)",
                    check(b2, now=now, grace_seconds=300, conn=conn2) == REFUSED))

        # the claim's lease has passed -> the row is up for grabs again -> FIRE.
        db.append_event(conn2, session="w1", actor="w1", kind=board.RELEASE_KIND, ref="R1",
                        data={"worker": "w1"}, clock=lambda: "2026-09-16T00:00:02+00:00")
        res.append(("claim released -> the row is up for grabs -> FIRE",
                    check(b2, now=now, grace_seconds=300, conn=conn2) == FIRE))
        conn2.close()
        shutil.rmtree(b2, ignore_errors=True)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print("\n  kicker-check selftest")
    print("  " + "-" * 48)
    for label, ok in res:
        print("  %s  %s" % ("PASS" if ok else "FAIL", label))
    all_pass = all(ok for _, ok in res)
    print("SELFTEST PASS: every kicker-check case passed" if all_pass
          else "SELFTEST FAIL: %d of %d case(s) did not pass"
               % (sum(1 for _, ok in res if not ok), len(res)))
    return 0 if all_pass else 1
