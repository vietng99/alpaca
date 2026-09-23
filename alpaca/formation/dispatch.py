"""The dispatch protocol, the sole-spawner rule, and the heavy bracket (M3.8).

Sources: spec 5.8:493-506 (dispatch protocol, heavy-verify bracket, sole-spawner), 7.3:761-771;
section 6 ADAPT row 84 (the earlier harness controller, with the two-human phase sign-off removed);
The earlier harness inventory row 84. Built on the M3.7 formation manifest and the M2.5 claims.

WHAT THIS IS. An orchestrator hands work to workers over the record, and folds their verdicts back.
Nothing here is a new store: an assignment, a spawn, a spawn refusal, a reopen and a fold are each
one hash-chained event, and the current state is a fold over them plus the M1.13 verdict rows and
the M2.5 claims. Because every hop is an event, a session peeking in reads the same dispatch state
this module folds, and a crash between hops loses nothing.

THE PROTOCOL, one hop at a time.
  * `assign(conn, op, rows, roles)` -- the ORCHESTRATOR (and only it) posts one assignment per row:
    a `dispatch-assign` event keyed on the row plus a `handoff` message to the worker. Assigning is
    spawning a worker, so it is bounded by the declared budget (below) and refused from any actor
    but the orchestrator.
  * a worker then CLAIMS its row (`alpaca.claims`, M2.5) and lands a VERDICT ROW (`verdict_row`, the
    M1.13 contract) via `land_verdict`, which also posts a `result` message pointing at the row.
  * `fold(conn, op)` reads each assigned row's folded verdict status: a discharged (or waived) row
    is ADVANCED, a failed (or blocked) row is REOPENED with the reason from its latest verdict,
    released back to the pool for a re-assignment. An open row is still pending.

THE SOLE-SPAWNER RULE (Step 4). Spawning workers is the orchestrator's alone. A budget is declared
UP FRONT (`declare_budget`); `spawn`/`assign` refuse a spawn past it, and a spawn attempted by any
other actor is recorded as a `dispatch-spawn-refused` event AND raises `SoleSpawnerRefused` -- the
refusal is on the record, never a silent no-op.

THE HEAVY BRACKET (Step 3). `heavy_bracket` runs an acceptance verdict inside a quiesce/snapshot/
stamp/dispatch/reap frame: it snapshots the chain head (the quiesce witness), stamps the op with
that snapshot, dispatches exactly ONE acceptance verdict by a blind verifier, and reaps that one
verdict event -- refusing if more or fewer than one landed.

THE BLIND-PAIR RULE carried into dispatch (Step 1's negative path). An acceptance check on a row is
an independence check: the worker who BUILT the row may not run it. `land_verdict(acceptance=True)`
refuses when the verifier is the row's own assigned worker (`SelfAcceptanceRefused`), recording the
refusal as an event.

HONEST LIMIT: this module sequences the record; it does not launch OS processes. Whether a worker
process actually stays distinct at runtime is the harness's obligation, not this fold's.
"""
from __future__ import annotations

from alpaca import claims, db, messages, util
from alpaca.checklist import verdict_row
from alpaca.gates import verdict as vc

#: the one role allowed to spawn and to post assignments. The sole-spawner rule keys on it.
ORCHESTRATOR = "orchestrator"

#: event kinds. Each hop of the protocol is one hash-chained event; there is no side table.
BUDGET_KIND = "dispatch-budget"                    # the spawn budget declared up front
ASSIGN_KIND = "dispatch-assign"                    # one row handed to one worker (a spawn)
SPAWN_KIND = "dispatch-spawn"                       # a bare spawn of a worker by the orchestrator
SPAWN_REFUSED_KIND = "dispatch-spawn-refused"      # a spawn from anywhere but the orchestrator
SELF_ACCEPTANCE_REFUSED_KIND = "dispatch-self-acceptance-refused"  # a builder judging its own row
REOPEN_KIND = "dispatch-reopen"                    # a failed row returned to the pool with a reason
ADVANCE_KIND = "dispatch-advance"                  # a discharged row confirmed done
BRACKET_KIND = "dispatch-heavy-bracket"            # the quiesce/snapshot/stamp of a heavy verify


class DispatchError(Exception):
    """Base for every refusal this module raises. Never swallowed into a silent pass."""


class SoleSpawnerRefused(DispatchError):
    """A spawn (or an assignment, which spawns) from an actor other than the orchestrator. The
    refusal is also recorded as a `dispatch-spawn-refused` event before this is raised."""


class BudgetExceeded(DispatchError):
    """A spawn past the budget declared up front. The refusal is recorded before this is raised."""


class SelfAcceptanceRefused(DispatchError):
    """A worker running the acceptance check on a row it was itself assigned to build. The blind-
    pair rule: the verifier is never the builder. Recorded before this is raised."""


# --------------------------------------------------------------------------- clock seam
class _clock_ctx:
    """Install a Clock process-wide for the duration of a write, then restore. The record writer
    and the message writer both stamp through util.now_iso, so a FixedClock passed to a verb drives
    every instant it records without each call site threading the clock."""

    def __init__(self, clock):
        self._clock = clock
        self._saved = None

    def __enter__(self):
        if self._clock is not None:
            self._saved = util.get_clock()
            util.set_clock(self._clock)
        return self

    def __exit__(self, *exc):
        if self._clock is not None:
            util.set_clock(self._saved)
        return False


# --------------------------------------------------------------------------- event readers
def _events(conn, kind):
    return db.events(conn, kind=kind, limit=10 ** 9)


def _for_op(conn, kind, op):
    return [e for e in _events(conn, kind) if (e["op"] == op or e["data"].get("op") == op)]


# --------------------------------------------------------------------------- the budget
def declare_budget(conn, op, budget, *, session=None, actor=ORCHESTRATOR) -> int:
    """Declare the spawn budget for `op` up front: at most `budget` workers may be spawned. Only
    the orchestrator declares it; a later declaration supersedes the earlier one (latest-wins)."""
    if actor != ORCHESTRATOR:
        _refuse_spawn(conn, op, actor, actor, session,
                      "only the orchestrator declares the spawn budget")
    b = int(budget)
    if b < 0:
        raise DispatchError("a spawn budget cannot be negative")
    db.append_event(conn, session=session or ORCHESTRATOR, actor=actor, kind=BUDGET_KIND,
                    op=op, data={"op": op, "budget": b})
    return b


def declared_budget(conn, op):
    """The budget declared for `op`, latest-wins, or None when none was declared (unbounded)."""
    evs = _for_op(conn, BUDGET_KIND, op)
    return evs[-1]["data"].get("budget") if evs else None


def _spawned_workers(conn, op) -> list:
    """The distinct workers spawned for `op` so far, in first-spawn order."""
    seen, out = set(), []
    for e in _for_op(conn, ASSIGN_KIND, op) + _for_op(conn, SPAWN_KIND, op):
        w = e["data"].get("worker")
        if w is not None and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def spawn_count(conn, op) -> int:
    """How many distinct workers have been spawned for `op`."""
    return len(_spawned_workers(conn, op))


# --------------------------------------------------------------------------- the sole spawner
def _refuse_spawn(conn, op, worker, by, session, reason):
    """Record a spawn refusal as an event, then raise. A refusal is never a silent no-op."""
    db.append_event(conn, session=session or by or "dispatch", actor=by or "unknown",
                    kind=SPAWN_REFUSED_KIND, op=op,
                    data={"op": op, "worker": worker, "by": by, "reason": reason})
    raise SoleSpawnerRefused(reason)


def _check_budget(conn, op, new_workers, session, by):
    """Refuse (recording it) a spawn that would take the distinct-worker count past the budget."""
    budget = declared_budget(conn, op)
    if budget is None:
        return
    already = set(_spawned_workers(conn, op))
    projected = already | {w for w in new_workers if w is not None}
    if len(projected) > budget:
        db.append_event(conn, session=session or by or "dispatch", actor=by or ORCHESTRATOR,
                        kind=SPAWN_REFUSED_KIND, op=op,
                        data={"op": op, "workers": list(new_workers), "budget": budget,
                              "reason": "spawn budget %d exceeded" % budget})
        raise BudgetExceeded(
            "spawning %s would exceed the declared budget of %d for %s"
            % (", ".join(str(w) for w in new_workers), budget, op))


def spawn(conn, op, worker, *, by=ORCHESTRATOR, role=None, session=None) -> dict:
    """Spawn one `worker` for `op`. Only the orchestrator may; a spawn by any other actor is
    recorded as a refusal event and raises SoleSpawnerRefused. A spawn past the declared budget
    raises BudgetExceeded. Returns the `dispatch-spawn` event on success."""
    if by != ORCHESTRATOR:
        _refuse_spawn(conn, op, worker, by, session,
                      "a spawn from %r is refused: only the orchestrator spawns" % by)
    _check_budget(conn, op, [worker], session, by)
    return db.append_event(conn, session=session or ORCHESTRATOR, actor=by, kind=SPAWN_KIND,
                           op=op, data={"op": op, "worker": worker, "role": role})


# --------------------------------------------------------------------------- assignments
def _roles_map(rows, roles) -> dict:
    """Normalise `roles` to a {row_id: worker} map. Accepts a dict, a list aligned with `rows`, or
    a single worker string applied to every row."""
    if isinstance(roles, dict):
        return {r: roles[r] for r in rows}
    if isinstance(roles, str):
        return {r: roles for r in rows}
    roles = list(roles)
    if len(roles) != len(rows):
        raise DispatchError("roles list has %d entries for %d rows" % (len(roles), len(rows)))
    return dict(zip(rows, roles))


def assign(conn, op, rows, roles, *, by=ORCHESTRATOR, session=None, clock=None) -> list:
    """The orchestrator posts one assignment per row. Each assignment is a `dispatch-assign` event
    keyed on the row plus a `handoff` message to the worker, and each is a spawn bounded by the
    declared budget. A non-orchestrator caller is refused (recorded + SoleSpawnerRefused); a set of
    assignments past the budget is refused (recorded + BudgetExceeded). Returns the assignments as
    a list of {row, worker, role, event}."""
    rows = list(rows)
    mapping = _roles_map(rows, roles)
    if by != ORCHESTRATOR:
        _refuse_spawn(conn, op, [mapping[r] for r in rows], by, session,
                      "assignments come from the orchestrator alone; %r may not post them" % by)
    _check_budget(conn, op, [mapping[r] for r in rows], session, by)
    out = []
    with _clock_ctx(clock):
        for r in rows:
            worker = mapping[r]
            ev = db.append_event(conn, session=session or ORCHESTRATOR, actor=ORCHESTRATOR,
                                 kind=ASSIGN_KIND, op=op, ref=r,
                                 data={"op": op, "row": r, "worker": worker})
            try:
                messages.post(conn, ORCHESTRATOR, worker, "handoff",
                              "assigned row %s in %s" % (r, op), pointer=r,
                              session=session or ORCHESTRATOR)
            except Exception:
                pass
            out.append({"row": r, "worker": worker, "role": worker, "event": ev})
    return out


def worker_of(conn, op, row_id):
    """The worker currently assigned to `row_id` in `op`, latest-wins, or None when unassigned."""
    latest = None
    for e in _for_op(conn, ASSIGN_KIND, op):
        if e["ref"] == row_id or e["data"].get("row") == row_id:
            latest = e["data"].get("worker")
    return latest


# --------------------------------------------------------------------------- verdicts
def land_verdict(conn, row_id, content_hash, worker, verdict, *, evidence=(), reason=None,
                 acceptance=False, level="L2", op=None, session=None, clock=None) -> dict:
    """A worker lands a verdict row on `row_id` (the M1.13 contract) plus a `result` message.

    `verdict` is a verdict-band code (PASS / FAIL / BLOCKED / PAUSED). When `acceptance` is set the
    landing is an acceptance check, so the blind-pair rule applies: the verifier `worker` may NOT be
    the row's own assigned builder -- that is recorded as a refusal event and raises
    SelfAcceptanceRefused. On a non-PASS verdict the `reason` is carried in the verdict row's
    evidence and the result message, so the fold can reopen the row WITH its reason.
    Returns {verdict_event, message}."""
    op = op or _op_of_row(conn, row_id)
    if acceptance:
        builder = worker_of(conn, op, row_id)
        if builder is not None and builder == worker:
            db.append_event(conn, session=session or worker, actor=worker,
                            kind=SELF_ACCEPTANCE_REFUSED_KIND, op=op, ref=row_id,
                            data={"op": op, "row": row_id, "worker": worker,
                                  "reason": "a worker may not run the acceptance check on a row it "
                                            "built (blind-pair rule)"})
            raise SelfAcceptanceRefused(
                "%r built %s; the acceptance check must be run by a different worker" % (worker, row_id))
    ev_list = list(evidence)
    if reason is not None:
        ev_list = ev_list + ["reason: %s" % reason]
    with _clock_ctx(clock):
        vev = verdict_row.discharge(conn, row_id, content_hash, worker, verdict, ev_list,
                                    level, session or worker)
        body = "%s on %s%s" % (vc.name_of(verdict), row_id,
                               ("" if reason is None else " -- %s" % reason))
        msg = None
        try:
            msg = messages.post(conn, worker, op or ORCHESTRATOR, "result", body, pointer=row_id,
                                session=session or worker)
        except Exception:
            pass
    return {"verdict_event": vev, "message": msg}


def _op_of_row(conn, row_id):
    r = conn.execute("SELECT op FROM rows WHERE id=?", (row_id,)).fetchone()
    return r["op"] if r is not None else None


def _latest_verdict_reason(conn, row_id):
    """The reason on the latest verdict row of `row_id`, recovered from its `reason:` evidence
    line, or None. Lets the fold reopen a failed row WITH the reason its verdict carried."""
    evs = verdict_row._verdict_events(conn, row_id)
    if not evs:
        return None
    data = evs[-1]["data"]
    if data.get("reason"):
        return data["reason"]
    for e in data.get("evidence") or []:
        if isinstance(e, str) and e.startswith("reason: "):
            return e[len("reason: "):]
    return None


# --------------------------------------------------------------------------- the fold
def _assigned_rows(conn, op) -> list:
    """The rows assigned in `op`, in first-assignment order (deduplicated)."""
    seen, out = set(), []
    for e in _for_op(conn, ASSIGN_KIND, op):
        r = e["ref"] or e["data"].get("row")
        if r is not None and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def fold(conn, op, *, session=None, now=None) -> dict:
    """Fold the op's assigned rows by their verdict status: advance the discharged (or waived)
    ones, reopen the failed (or blocked) ones WITH their recorded reason, releasing them back to
    the pool for a re-assignment. An open row is still pending. Returns
    {advanced, reopened:[{row, reason}], pending}."""
    advanced, reopened, pending = [], [], []
    for row_id in _assigned_rows(conn, op):
        status = verdict_row.status_fold(conn, row_id)
        if status in (verdict_row.DISCHARGED, verdict_row.WAIVED_STATUS):
            db.append_event(conn, session=session or ORCHESTRATOR, actor=ORCHESTRATOR,
                            kind=ADVANCE_KIND, op=op, ref=row_id,
                            data={"op": op, "row": row_id, "status": status})
            advanced.append(row_id)
        elif status in (verdict_row.FAILED, verdict_row.BLOCKED_STATUS):
            reason = _latest_verdict_reason(conn, row_id)
            db.append_event(conn, session=session or ORCHESTRATOR, actor=ORCHESTRATOR,
                            kind=REOPEN_KIND, op=op, ref=row_id,
                            data={"op": op, "row": row_id, "status": status, "reason": reason})
            worker = worker_of(conn, op, row_id)
            if worker is not None and claims.live(conn, row_id, now) is not None:
                try:
                    claims.release(conn, row_id, worker, session=session or ORCHESTRATOR, now=now)
                except Exception:
                    pass
            reopened.append({"row": row_id, "reason": reason})
        else:
            pending.append(row_id)
    return {"advanced": advanced, "reopened": reopened, "pending": pending}


# --------------------------------------------------------------------------- the heavy bracket
def heavy_bracket(conn, op, row_id, content_hash, verifier, verdict, *, evidence=(), reason=None,
                  level="L2", session=None, clock=None) -> dict:
    """Run ONE acceptance verdict inside the heavy-verify frame (Step 3): quiesce, snapshot, stamp
    the op, dispatch, reap one verdict event.

      * QUIESCE + SNAPSHOT: capture the current chain head hash -- the witness that the record is
        settled at the moment the bracket opens.
      * STAMP: append a `dispatch-heavy-bracket` event stamping `op` with that snapshot.
      * DISPATCH: land exactly one acceptance verdict by `verifier` (a blind verifier, refused if it
        is the row's own builder).
      * REAP: confirm exactly ONE verdict event landed for the row during the bracket; more or fewer
        is a refusal.

    Returns {snapshot, stamp, verdict_event, reaped}."""
    head = db.last_event(conn)
    snapshot = head["hash"] if head else db.GENESIS
    before = len(verdict_row._verdict_events(conn, row_id))
    with _clock_ctx(clock):
        stamp = db.append_event(conn, session=session or ORCHESTRATOR, actor=ORCHESTRATOR,
                                kind=BRACKET_KIND, op=op, ref=row_id,
                                data={"op": op, "row": row_id, "snapshot": snapshot,
                                      "verifier": verifier})
        res = land_verdict(conn, row_id, content_hash, verifier, verdict, evidence=evidence,
                           reason=reason, acceptance=True, level=level, op=op, session=session)
    reaped = len(verdict_row._verdict_events(conn, row_id)) - before
    if reaped != 1:
        raise DispatchError(
            "the heavy bracket reaped %d verdict events for %s; exactly one is required"
            % (reaped, row_id))
    return {"snapshot": snapshot, "stamp": {"op": op, "snapshot": snapshot, "event": stamp},
            "verdict_event": res["verdict_event"], "reaped": reaped}
