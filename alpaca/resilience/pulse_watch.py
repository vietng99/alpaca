"""Liveness from real progress: healthy | HUNG | DEAD (M3.12).

Ported from the earlier harness ops/pulse-watch.py (doctrine/watchdog-liveness.md), against the RECORD rather
than a war-room's PULSE.md/STATE.md/war-log.jsonl. The idea is unchanged: judge liveness only
from REAL progress, never from a worker-controllable counter (the Goodhart clause). The port maps
the war-log line count and the STATE step ordinal onto the record's own progress sources:

  progress = (events count, excluding this watchdog's own beats) + (the row cursor, the rows table)

Both are monotonic non-decreasing and neither is written by the code under watch to game the
signal. This watchdog's own beat events are excluded from the count so a beat cannot inflate its
own progress (the no-op-inflation limitation the earlier harness named but could not close is closed here by
construction: the watchdog does not count itself).

The state machine (watchdog-liveness.md): a beat that sees strictly more progress than the last is
HEALTHY. One beat with no new progress is HUNG (a stall, not fatal). A second consecutive stall is
DEAD. The very first observed beat has no baseline and is HEALTHY (never a false DEAD). The count
of consecutive stalls is read from the beat trail in the record, so a crash and resume reads the
same trail, never a reset.

This module classifies only; it never acts on a DEAD verdict. Acting on DEAD (fencing a row and
handing it to a fresh worker) is the dispatch protocol's job over the board (alpaca.claims takeover),
composed with this classifier in the M3 acceptance.
"""
from __future__ import annotations

from alpaca import db

#: this watchdog's own beat, one append-only event carrying the progress it observed. Excluded
#: from the progress count so a beat never inflates its own signal.
KIND_PULSE = "resilience-pulse"

#: the classification tokens, matching the interface (healthy lower-case, HUNG and DEAD upper).
HEALTHY = "healthy"
HUNG = "HUNG"
DEAD = "DEAD"

#: consecutive stalled beats before DEAD is declared: 1 miss = HUNG, 2nd consecutive miss = DEAD
#: (watchdog-liveness.md's two-consecutive-missed-interval rule).
MISSED_FOR_DEAD = 2


def progress(conn) -> int:
    """Real progress from the record: the events count (excluding this watchdog's own beats) plus
    the row cursor (the rows table). Both are monotonic and neither is a counter the watched code
    can set at will, which is the whole point of reading progress from the record."""
    n_events = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind != ?", (KIND_PULSE,)).fetchone()[0]
    n_rows = conn.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
    return int(n_events) + int(n_rows)


def beat(conn, *, session=None, actor="pulse-watch") -> int:
    """Observe one beat: snapshot the current real progress into the beat trail and return it.

    The snapshot is one append-only event keyed on this watchdog's kind, so the trail survives a
    crash. The snapshot is taken BEFORE the beat event is written, and the beat kind is excluded
    from `progress`, so a beat never counts toward the number it records."""
    p = progress(conn)
    db.append_event(conn, session=session or "pulse-watch", actor=actor or "pulse-watch",
                    kind=KIND_PULSE, data={"prog": p})
    return p


def _snapshots(conn) -> list:
    """Every beat's recorded progress, oldest first: the trail the classifier reads."""
    return [int(e["data"].get("prog", 0)) for e in db.events(conn, kind=KIND_PULSE, limit=10 ** 9)]


def _trailing_stalls(snaps) -> int:
    """How many of the most recent beats saw no new progress (a decrease counts as a stall, never
    as extra progress: progress is monotonic non-decreasing upstream)."""
    stalls = 0
    for i in range(len(snaps) - 1, 0, -1):
        if snaps[i] > snaps[i - 1]:
            break
        stalls += 1
    return stalls


def delta_class(current, last) -> str:
    """Pure one-beat classifier over a progress delta, factored out so it can be exercised
    able-to-fail in `selftest` without a live record:

      last is None -> HEALTHY : the first observed beat, nothing to compare against (never prog=0).
      current > last -> HEALTHY : progress strictly increased.
      otherwise -> "STALLED" : progress unchanged, or (a doctrine violation upstream) decreased.
    """
    if last is None:
        return HEALTHY
    if current > last:
        return HEALTHY
    return "STALLED"


def classify(conn, *, missed_for_dead=MISSED_FOR_DEAD) -> str:
    """Classify the most recent beat as healthy | HUNG | DEAD from the record's progress trail.

    Fewer than two beats is HEALTHY (no baseline). Otherwise the number of trailing stalled beats
    decides: zero is HEALTHY, one is HUNG, two-or-more consecutive is DEAD.
    """
    snaps = _snapshots(conn)
    if len(snaps) < 2:
        return HEALTHY
    stalls = _trailing_stalls(snaps)
    if stalls == 0:
        return HEALTHY
    if stalls >= missed_for_dead:
        return DEAD
    return HUNG


def selftest() -> int:
    """Able-to-pass + able-to-fail controls for the liveness classifier, ported from the earlier harness
    ops/pulse-watch.py. Exercises the pure delta classifier and the record-backed state machine
    (HUNG then DEAD from stalled progress) on a scratch store. Returns 0 on all-PASS, else 1."""
    import os
    import shutil
    import tempfile

    res = []
    res.append(("delta able-to-pass: progress increased -> HEALTHY",
                delta_class(5, 3) == HEALTHY))
    res.append(("delta able-to-fail: progress unchanged -> STALLED",
                delta_class(3, 3) == "STALLED"))
    res.append(("delta able-to-fail: progress decreased -> STALLED (never 'extra progress')",
                delta_class(2, 3) == "STALLED"))
    res.append(("no-baseline: last None -> HEALTHY (not prog=0)",
                delta_class(5, None) == HEALTHY))

    base = tempfile.mkdtemp(prefix="pulse-watch-selftest-")
    try:
        conn = db.connect(base)
        res.append(("empty record: fewer than two beats -> healthy",
                    classify(conn) == HEALTHY))
        beat(conn)
        db.append_event(conn, session="s", actor="a", kind="heartbeat", data={"n": 1})
        beat(conn)
        res.append(("state machine able-to-pass: progress advanced -> healthy",
                    classify(conn) == HEALTHY))
        beat(conn)
        res.append(("state machine: first stall -> HUNG", classify(conn) == HUNG))
        beat(conn)
        res.append(("state machine: second consecutive stall -> DEAD", classify(conn) == DEAD))
        db.append_event(conn, session="s", actor="a", kind="heartbeat", data={"n": 2})
        beat(conn)
        res.append(("state machine: real work resumes -> healthy again", classify(conn) == HEALTHY))
        conn.close()
    finally:
        shutil.rmtree(base, ignore_errors=True)

    all_pass = all(ok for _, ok in res)
    _print_table("pulse-watch", res)
    return 0 if all_pass else 1


def _print_table(name, res) -> None:
    print("\n  %s selftest" % name)
    print("  " + "-" * 48)
    for label, ok in res:
        print("  %s  %s" % ("PASS" if ok else "FAIL", label))
    if all(ok for _, ok in res):
        print("SELFTEST PASS: every %s case passed" % name)
    else:
        n = sum(1 for _, ok in res if not ok)
        print("SELFTEST FAIL: %d of %d case(s) did not pass" % (n, len(res)))
