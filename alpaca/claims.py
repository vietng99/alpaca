"""Claims, leases, expiry and takeover, keyed on the worker (M2.5).

Claims and leases, and worker-pool recovery (`doctrine/leaves/worker-pool-recovery.md`): a lease
expires and the row returns. A claim is a lease on one row held by a WORKER, not
a session: the same worker across two sessions renews the same lease, while a different worker
is a concurrency event or a takeover. Every claim carries a FENCE token, a monotonic integer
that a takeover bumps, so a late writer from a superseded lease is refused.

The record, not a column, is the truth. Claims live entirely as hash-chained events keyed on
the row id (`ref`), so a second session peeking at the record sees a live claim the instant it
is taken, before any current-state row is mutated:

    take      -> a `claim` event (board-derived `doing`) + the first `heartbeat`, then the
                 current-state mutation follows (Step 3)
    renew     -> a `heartbeat` extending the lease, refused on a stale fence (Step 2)
    expire    -> a `lease-expired` event returning the row to todo (Step 1)
    takeover  -> a `claim` event flagged takeover, keyed on the new worker, bumping the fence
    conflict  -> a `concurrency-detected` signal the resolve pass (M4.5) consumes (Step 4)

The `claim` and `claim-release` event kinds are the board's own (M2.3), reused verbatim so the
board keeps deriving `doing` and `todo` whether a claim came from the board or from here; the
lease is stamped through `util.now_iso`, so a FixedClock (M2.2) drives every instant under test.
"""
from __future__ import annotations

import datetime

from alpaca import board, db, util

# The claim event kinds. `claim` and `claim-release` are the board's vocabulary (M2.3), reused
# so one derivation serves both. `heartbeat` renews a live lease; `lease-expired` returns a row
# to todo; `concurrency-detected` is the signal the resolve pass (M4.5) reads.
CLAIM_KIND = board.CLAIM_KIND            # "claim": establishes / seizes a holder, board `doing`
RELEASE_KIND = board.RELEASE_KIND        # "claim-release": a voluntary end
HEARTBEAT_KIND = "heartbeat"             # a renew: extends the current fence's lease
EXPIRE_KIND = "lease-expired"            # an expired lease returned its row to todo
CONCURRENCY_KIND = "concurrency-detected"  # a second claim on a live row; read by M4.5

#: the default lease a claim carries, in minutes, when the caller names none.
DEFAULT_LEASE_MINUTES = board.DEFAULT_LEASE_MINUTES

# take/renew/takeover return statuses.
CLAIMED = "claimed"
CONCURRENCY = "concurrency-detected"
TAKEN = "taken-over"


class ClaimError(Exception):
    """A claim operation that cannot proceed. Base for every refusal this module raises."""


class StaleLease(ClaimError):
    """A late writer from an expired-or-superseded lease: the fence no longer matches the current
    holder, or there is no live lease to renew. The write is refused, never laundered."""


# --------------------------------------------------------------------- fold the holder
def _fold_holder(conn, row_id):
    """Fold the row's claim events into the current holder, or None when the lease was ended
    (released or expired) or never taken. Liveness is NOT applied here: the holder is returned
    whether or not its lease has passed, so `expire_due` can find an expired-but-unended lease.
    The fold is `board.fold_leases`, the one every claim view reads."""
    return board.fold_leases(conn, [row_id]).get(row_id)


def live(conn, row_id, now=None):
    """The live holder of `row_id` at `now` as {worker, fence, lease_until, since}, or None when
    the row is free (never claimed, released, or its lease has passed `now`)."""
    now = now or util.now_iso()
    holder = _fold_holder(conn, row_id)
    if holder is None:
        return None
    lu = holder["lease_until"]
    if lu is not None and lu <= now:
        return None
    return holder


def _next_fence(conn, row_id):
    """The next fence token for `row_id`: one past the count of claim events (take + takeover),
    monotonic, so a later claim always outranks an earlier one and a stale fence is detectable."""
    n = conn.execute("SELECT COUNT(*) FROM events WHERE kind=? AND ref=?",
                     (CLAIM_KIND, row_id)).fetchone()[0]
    return n + 1


def _op_of(conn, row_id):
    for table in ("rows", "tasks"):
        r = conn.execute("SELECT op FROM %s WHERE id=?" % table, (row_id,)).fetchone()
        if r is not None:
            return r["op"]
    return None


def _lease_from(now, minutes):
    """An ISO instant `minutes` from `now`, at whole-second resolution (matches the board)."""
    base = datetime.datetime.fromisoformat(now)
    return (base + datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")


# --------------------------------------------------------------------- current-state sync
def _has_task(conn, row_id):
    return conn.execute("SELECT 1 FROM tasks WHERE id=?", (row_id,)).fetchone() is not None


def _sync_task_doing(conn, row_id, worker, lease_until, now):
    """Mirror a live claim onto a tracker task's current-state row, when the row is a task. This
    is the current-state MUTATION that follows the claim event and the first heartbeat (Step 3)."""
    if _has_task(conn, row_id):
        db.patch(conn, "tasks", "id", row_id,
                 {"status": "doing", "claimant": worker, "lease_until": lease_until, "updated": now})


def _return_task_todo(conn, row_id, now):
    if _has_task(conn, row_id):
        db.patch(conn, "tasks", "id", row_id,
                 {"status": "open", "claimant": None, "lease_until": None, "updated": now})


# --------------------------------------------------------------------- verbs
def take(conn, row_id, worker, minutes=DEFAULT_LEASE_MINUTES, *, session=None, reason=None,
         now=None):
    """Take a lease on `row_id` for `worker`. Keyed on the worker, not the session.

    A free row is claimed: the `claim` event and the first `heartbeat` land first, then the
    current-state mutation. A row already held by the SAME worker renews in place (same fence).
    A row held by a DIFFERENT LIVE worker is NOT overwritten: a `concurrency-detected` signal is
    recorded for the resolve pass (M4.5) and the incumbent is left untouched.
    """
    now = now or util.now_iso()
    session = session or worker
    incumbent = live(conn, row_id, now)
    if incumbent is not None and incumbent["worker"] != worker:
        ev = _signal_concurrency(conn, row_id, incumbent, worker, session, now)
        return {"status": CONCURRENCY, "worker": incumbent["worker"], "challenger": worker,
                "fence": incumbent["fence"], "lease_until": incumbent["lease_until"], "event": ev}
    if incumbent is not None and incumbent["worker"] == worker:
        return renew(conn, row_id, worker, fence=incumbent["fence"], minutes=minutes,
                     session=session, now=now)
    fence = _next_fence(conn, row_id)
    lease_until = _lease_from(now, minutes)
    op = _op_of(conn, row_id)
    with db.transaction(conn):
        claim_ev = db.append_event(
            conn, session=session, actor=worker, kind=CLAIM_KIND, op=op, ref=row_id,
            data={"worker": worker, "fence": fence, "lease_until": lease_until, "reason": reason},
            conn_in_txn=True, clock=lambda: now)
        heartbeat_ev = db.append_event(
            conn, session=session, actor=worker, kind=HEARTBEAT_KIND, op=op, ref=row_id,
            data={"worker": worker, "fence": fence, "lease_until": lease_until},
            conn_in_txn=True, clock=lambda: now)
        _sync_task_doing(conn, row_id, worker, lease_until, now)   # the mutation, AFTER the events
    return {"status": CLAIMED, "worker": worker, "fence": fence, "lease_until": lease_until,
            "claim": claim_ev, "heartbeat": heartbeat_ev}


def renew(conn, row_id, worker, *, fence=None, minutes=DEFAULT_LEASE_MINUTES, session=None,
          now=None):
    """Extend a live lease `worker` holds on `row_id` with a heartbeat. Refused (StaleLease) when
    there is no live lease, when it is held by another worker, or when a named `fence` no longer
    matches the current holder, which is exactly a late writer from a superseded lease (Step 2)."""
    now = now or util.now_iso()
    session = session or worker
    current = live(conn, row_id, now)
    if current is None:
        raise StaleLease("no live lease on %s to renew" % row_id)
    if current["worker"] != worker:
        raise StaleLease("%s is held by %s, not %s" % (row_id, current["worker"], worker))
    if fence is not None and fence != current["fence"]:
        raise StaleLease("fence %r on %s is stale; current fence is %r (a takeover superseded it)"
                         % (fence, row_id, current["fence"]))
    lease_until = _lease_from(now, minutes)
    op = _op_of(conn, row_id)
    with db.transaction(conn):
        hb = db.append_event(
            conn, session=session, actor=worker, kind=HEARTBEAT_KIND, op=op, ref=row_id,
            data={"worker": worker, "fence": current["fence"], "lease_until": lease_until},
            conn_in_txn=True, clock=lambda: now)
        _sync_task_doing(conn, row_id, worker, lease_until, now)
    return {"status": CLAIMED, "worker": worker, "fence": current["fence"],
            "lease_until": lease_until, "heartbeat": hb}


def takeover(conn, row_id, worker, minutes=DEFAULT_LEASE_MINUTES, *, session=None, now=None):
    """Seize `row_id` for `worker`, keyed on the worker. Records a `claim` event flagged takeover
    that bumps the fence past the previous holder's, so the previous holder becomes a late writer
    and its renew is refused. Used after a worker is declared DEAD or its lease has expired."""
    now = now or util.now_iso()
    session = session or worker
    prev = _fold_holder(conn, row_id)
    prev_worker = prev["worker"] if prev else None
    prev_fence = prev["fence"] if prev else None
    fence = _next_fence(conn, row_id)
    lease_until = _lease_from(now, minutes)
    op = _op_of(conn, row_id)
    with db.transaction(conn):
        ev = db.append_event(
            conn, session=session, actor=worker, kind=CLAIM_KIND, op=op, ref=row_id,
            data={"worker": worker, "fence": fence, "lease_until": lease_until, "takeover": True,
                  "prev_worker": prev_worker, "prev_fence": prev_fence},
            conn_in_txn=True, clock=lambda: now)
        db.append_event(
            conn, session=session, actor=worker, kind=HEARTBEAT_KIND, op=op, ref=row_id,
            data={"worker": worker, "fence": fence, "lease_until": lease_until},
            conn_in_txn=True, clock=lambda: now)
        _sync_task_doing(conn, row_id, worker, lease_until, now)
    return {"status": TAKEN, "worker": worker, "fence": fence, "prev_worker": prev_worker,
            "prev_fence": prev_fence, "lease_until": lease_until, "event": ev}


def expire_due(conn, now=None):
    """Return every row whose lease has passed `now` to todo, each with a `lease-expired` event.

    A tracker task returns to `open`; a derived board row simply stops deriving `doing`. Already
    ended leases (released or expired) are skipped. Returns the freed row ids, oldest first."""
    now = now or util.now_iso()
    rows = [r["ref"] for r in conn.execute(
        "SELECT DISTINCT ref FROM events WHERE kind=? AND ref IS NOT NULL ORDER BY ref",
        (CLAIM_KIND,))]
    freed = []
    for row_id in rows:
        holder = _fold_holder(conn, row_id)
        if holder is None:
            continue
        lu = holder["lease_until"]
        if lu is None or lu > now:
            continue
        op = _op_of(conn, row_id)
        with db.transaction(conn):
            db.append_event(
                conn, session="alpaca", actor="alpaca", kind=EXPIRE_KIND, op=op, ref=row_id,
                data={"worker": holder["worker"], "fence": holder["fence"], "lease_until": lu},
                conn_in_txn=True, clock=lambda: now)
            _return_task_todo(conn, row_id, now)
        freed.append(row_id)
    return freed


def release(conn, row_id, worker, *, session=None, now=None):
    """End `worker`'s lease on `row_id` voluntarily, one event, returning the row to todo."""
    now = now or util.now_iso()
    session = session or worker
    op = _op_of(conn, row_id)
    with db.transaction(conn):
        ev = db.append_event(
            conn, session=session, actor=worker, kind=RELEASE_KIND, op=op, ref=row_id,
            data={"worker": worker}, conn_in_txn=True, clock=lambda: now)
        _return_task_todo(conn, row_id, now)
    return ev


# --------------------------------------------------------------------- keep a working session's leases
#: keep_alive renews a lease once no more than this many minutes of it remain, so a working
#: session writes one renewal per task per half lease, however many calls it makes.
RENEW_WITHIN_MINUTES = DEFAULT_LEASE_MINUTES // 2

#: the reason a claim taken again by keep_alive carries in the record.
KEEP_ALIVE_REASON = "keep_alive: the session that held this lease is working again"


def _moved_since(conn, row_id, event_id):
    """True when a `task-move` on `row_id` came after event `event_id`: somebody decided where
    the task goes after its lease ended, and keep_alive leaves that decision alone."""
    return conn.execute("SELECT 1 FROM events WHERE kind='task-move' AND ref=? AND id>? LIMIT 1",
                        (row_id, event_id)).fetchone() is not None


def keep_alive(conn, session, *, minutes=DEFAULT_LEASE_MINUTES, now=None):
    """Keep the task leases `session` holds while it works. Its own hooks call this on every tool
    call and every prompt (t-027); a lease otherwise ran out after an hour of work and the cockpit
    showed the working session as holding no task.

    Only a task whose lease this session last took or renewed is touched, and only one of these:
      * a task in `doing` with a live lease that has RENEW_WITHIN_MINUTES or less left: renewed
        on its fence;
      * a task in `doing` whose lease already ran out, with no release or expiry since: taken
        again with a new fence, which is what `alpaca task claim` would do;
      * a task the lease sweep (`lease-expired`) returned to `open`, with no task move and no
        other claim since: taken again the same way.
    A lease another session holds or took over, a released row, a task moved by hand after its
    lease ended and a finished task are never touched. With nothing due this is two indexed
    reads. Returns one {row, action, fence, lease_until} per row written.
    """
    now = now or util.now_iso()
    mine = [r[0] for r in conn.execute(
        "SELECT DISTINCT ref FROM events WHERE session=? AND kind IN (?,?) AND ref IS NOT NULL",
        (session, CLAIM_KIND, HEARTBEAT_KIND))]
    if not mine:
        return []
    due = {r[0]: r[1] for r in conn.execute(
        "SELECT id, status FROM tasks WHERE id IN (%s) AND ((status='doing' AND lease_until IS "
        "NOT NULL AND lease_until <= ?) OR (status='open' AND claimant IS NULL))"
        % ",".join("?" * len(mine)), mine + [_lease_from(now, RENEW_WITHIN_MINUTES)])}
    if not due:
        return []
    done = []
    for row_id, h in sorted(board.fold_leases(conn, list(due), ended=True).items()):
        if h["session"] != session or h["lease_until"] is None:
            continue
        try:
            if due[row_id] == "doing" and not h["ended"] and h["lease_until"] > now:
                res, action = renew(conn, row_id, h["worker"], fence=h["fence"], minutes=minutes,
                                    session=session, now=now), "renewed"
            elif (due[row_id] == "doing" and not h["ended"]) or (
                    due[row_id] == "open" and h["ended"] == EXPIRE_KIND
                    and not _moved_since(conn, row_id, h["ended_id"])):
                res, action = take(conn, row_id, h["worker"], minutes=minutes, session=session,
                                   reason=KEEP_ALIVE_REASON, now=now), "re-claimed"
            else:
                continue
        except ClaimError:
            continue
        if res.get("status") != CLAIMED:
            continue
        done.append({"row": row_id, "action": action, "fence": res["fence"],
                     "lease_until": res["lease_until"]})
    return done


# --------------------------------------------------------------------- the concurrency signal
def _signal_concurrency(conn, row_id, incumbent, challenger, session, now):
    """Record a `concurrency-detected` event: the incumbent kept the lease, a challenger tried to
    take it. This is a SIGNAL, not a resolution; M4.5 reads it through `concurrency_signals`."""
    ev = db.append_event(
        conn, session=session, actor=challenger, kind=CONCURRENCY_KIND,
        op=_op_of(conn, row_id), ref=row_id,
        data={"row_id": row_id, "incumbent": incumbent["worker"],
              "incumbent_fence": incumbent["fence"], "challenger": challenger,
              "lease_until": incumbent["lease_until"]},
        clock=lambda: now)
    # M4.5: mirror the collision into the resolve log (alpaca/resolve.py), the named typed artifact
    # the resolve pass consumes. The incumbent's live claim is recorded first, so the log shows
    # the claim a second session saw in the gap (Step 2: claim and first beat land before the
    # first mutation), then the `concurrent-detected` entry naming both writers. Lazy import to
    # avoid a cycle; guarded so a log hiccup never turns a claim into a failure.
    try:
        from alpaca import resolve
        resolve.log(conn, resolve.CLAIM, row_id,
                    {"worker": incumbent["worker"], "fence": incumbent["fence"],
                     "lease_until": incumbent["lease_until"]},
                    session=session, actor=incumbent["worker"], now=now)
        resolve.log(conn, resolve.CONCURRENT, row_id,
                    {"ref": row_id, "incumbent": incumbent["worker"], "challenger": challenger,
                     "writers": [{"worker": incumbent["worker"]}, {"worker": challenger}]},
                    session=session, actor=challenger, now=now)
    except Exception:
        pass
    return ev


def concurrency_signals(conn):
    """Every concurrency-detected signal on the record, oldest first, for the resolve pass (M4.5)."""
    return db.events(conn, kind=CONCURRENCY_KIND, limit=10 ** 9)
