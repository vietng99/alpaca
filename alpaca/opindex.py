"""The op index: a lifecycle state and a resume cursor for every op (M2.7).

Spec 5.1:207 (`ops`), 5.4:339-341 (the pad), spec:715 (the op index in the Alpaca operator row);
absorb-gap AG-A10 (op index with cursors and lifecycle states).

The index is a pure DERIVATION over ops, rows, claims and events; nothing here is a stored
column. Every op folds to exactly one state from the closed set:

    open      an op with work outstanding and nobody on it
    live      an op with a live claim on one of its rows (work in progress)
    blocked   an op with no live claim and at least one blocked / failed row
    closed    ops.status == 'closed'
    archived  the latest archive transition on the record is `op-archive` (not un-archived)

`closed`, `live` and `blocked` are read off ops.status, the live claims (M2.3 board.claim) and
the row status fold (M1.13 verdict rows). `archived` and its reverse are the only states carried
as RECORDED transitions: `archive` and `unarchive` each append one event, latest-wins, so a
parked op and its resumption are both in the ledger rather than a mutable flag.

Each op also carries its own resume cursor: the first non-done obligation row of the op (so a
second op can be resumed without rewriting the pad, which only ever names the current op). An
explicit `set_cursor` pins the cursor to a named row and is recorded as an event; a pinned
cursor that names a row which does not exist is a dangling cursor and is REPORTED, never
silently followed.

`demote_stale(conn, now)` is the op-level analogue of a lease returning a row: an op that is
live but whose cursor has not advanced within the staleness window is demoted back to open with
an event (and its stale live claims released, so the derivation folds it back to open). Under a
FixedClock the demotion is deterministic.

Interfaces (M2.7), consumed by the pad, the board, `alpaca doctor` (M4.13) and `alpaca day` (M4.6):

    list(conn, state=None) -> list[dict]
    cursor(conn, op) -> row_id | None
    demote_stale(conn, now) -> list[op_id]
"""
from __future__ import annotations

from alpaca import db, util

# The closed set of lifecycle states. The derivation never invents another.
OPEN = "open"
LIVE = "live"
BLOCKED = "blocked"
CLOSED = "closed"
ARCHIVED = "archived"
STATES = (OPEN, LIVE, BLOCKED, CLOSED, ARCHIVED)

# Recorded transitions. archived / un-archive and the staleness demotion are the only op-state
# moves that append their own event; every other state is folded from ops / rows / claims.
ARCHIVE_KIND = "op-archive"
UNARCHIVE_KIND = "op-unarchive"
DEMOTE_KIND = "op-demote"
CURSOR_KIND = "op-cursor"

# The default staleness window in seconds: a live op whose cursor has not advanced in this long
# is demoted. Callers (alpaca doctor, alpaca day) may pass their own window to demote_stale.
DEFAULT_STALENESS_SECONDS = 30 * 60

# The event kinds that count as the cursor advancing on an op: a claim starts work on the next
# row, a verdict row discharges the current one (the cursor moves on), an explicit cursor pin
# names the next row, and opening / un-archiving an op is itself fresh activity.
_ADVANCING_KINDS = ("claim", "verdict", CURSOR_KIND, UNARCHIVE_KIND, "op-open")


# --------------------------------------------------------------- small time helpers
def _parse(ts):
    """An ISO-8601 instant to a datetime, or None when it cannot be read. Never raises: a
    stamp the index cannot parse simply does not constrain staleness."""
    import datetime
    try:
        return datetime.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _seconds_between(earlier, later):
    a, b = _parse(earlier), _parse(later)
    if a is None or b is None:
        return None
    return (b - a).total_seconds()


# --------------------------------------------------------------- rows / claims / events
def _ops(conn):
    return db.rows(conn, "ops", "1=1 ORDER BY id")


def _rows_for(conn, op):
    """The head obligation rows of `op`: verdict rows and superseded originals excluded."""
    from alpaca.checklist import verdict_row
    allrows = db.rows(conn, "rows", "op=?", (op,))
    superseded = {r.get("supersedes") for r in allrows if r.get("supersedes")}
    return [r for r in allrows
            if r.get("id") not in superseded and (r.get("kind") or "item") != verdict_row.KIND]


def _row_exists(conn, row_id):
    return bool(db.rows(conn, "rows", "id=?", (row_id,)))


def _events_for_op(conn, op):
    return [e for e in db.events(conn, limit=10 ** 9) if e.get("op") == op]


def _live_ops(conn):
    """The set of op ids that currently hold a live claim on one of their rows. Derived through
    the board's own live-claim reader (M2.3), so both paths agree on what 'live' means."""
    from alpaca import board
    live = board.live_claims(conn)
    out = set()
    for row_id in live:
        r = db.rows(conn, "rows", "id=?", (row_id,))
        if r and r[0].get("op"):
            out.add(r[0]["op"])
    return out


def _archive_state(conn, op):
    """True when the latest archive transition on `op` is an archive not yet un-archived."""
    latest = None
    for e in _events_for_op(conn, op):
        if e["kind"] in (ARCHIVE_KIND, UNARCHIVE_KIND):
            latest = e["kind"]
    return latest == ARCHIVE_KIND


# --------------------------------------------------------------- the state derivation
def state(conn, op) -> str:
    """Fold one op to its lifecycle state. Pure over ops, rows, claims and events."""
    row = db.rows(conn, "ops", "id=?", (op,))
    status = row[0]["status"] if row else None
    if status == "closed":
        return CLOSED
    if _archive_state(conn, op):
        return ARCHIVED
    if op in _live_ops(conn):
        return LIVE
    from alpaca.checklist import verdict_row
    for r in _rows_for(conn, op):
        fold = verdict_row.status_fold(conn, r["id"])
        if fold in (verdict_row.BLOCKED_STATUS, verdict_row.FAILED):
            return BLOCKED
    return OPEN


# --------------------------------------------------------------- the resume cursor
def _pinned_cursor(conn, op):
    """The latest explicitly pinned cursor row_id for `op`, or None when none was pinned."""
    pinned = None
    for e in _events_for_op(conn, op):
        if e["kind"] == CURSOR_KIND:
            pinned = (e["data"] or {}).get("row_id")
    return pinned


def _derived_cursor(conn, op):
    """The first non-done obligation row of `op` in row-id order, or None when all are done."""
    from alpaca.checklist import verdict_row
    rows = sorted(_rows_for(conn, op), key=lambda r: str(r.get("id")))
    for r in rows:
        fold = verdict_row.status_fold(conn, r["id"])
        if fold not in (verdict_row.DISCHARGED, verdict_row.WAIVED_STATUS):
            return r["id"]
    return None


def cursor(conn, op):
    """The op's resume cursor: an explicit pin if one was recorded (returned even when it is
    dangling, so the report can catch it), otherwise the first non-done row, otherwise None."""
    pinned = _pinned_cursor(conn, op)
    if pinned is not None:
        return pinned
    return _derived_cursor(conn, op)


def set_cursor(conn, op, row_id, *, session=None, actor="alpaca") -> dict:
    """Pin `op`'s resume cursor to `row_id`, one recorded event. The row is not required to
    exist: a pin that names a missing row is deliberately allowed so `dangling_cursors` can
    report it (absence blocks, spec:807, is surfaced here as a report, not a silent follow)."""
    return db.append_event(conn, session=session or "alpaca", actor=actor, kind=CURSOR_KIND,
                           op=op, ref=row_id, data={"row_id": row_id})


def dangling_cursors(conn) -> list:
    """[(op, row_id)] for every op whose current cursor names a row that does not exist."""
    out = []
    for o in _ops(conn):
        c = cursor(conn, o["id"])
        if c is not None and not _row_exists(conn, c):
            out.append((o["id"], c))
    return out


# --------------------------------------------------------------- archived / un-archive
def archive(conn, op, *, session=None, reason=None, actor="human") -> dict:
    """Record an archive transition for `op`, one event. Latest-wins with unarchive."""
    return db.append_event(conn, session=session or "alpaca", actor=actor, kind=ARCHIVE_KIND,
                           op=op, data={"reason": reason})


def unarchive(conn, op, *, session=None, reason=None, actor="human") -> dict:
    """Record an un-archive transition for `op`, one event. Returns the op to its derived
    state (open / live / blocked), latest-wins with archive."""
    return db.append_event(conn, session=session or "alpaca", actor=actor, kind=UNARCHIVE_KIND,
                           op=op, data={"reason": reason})


# --------------------------------------------------------------- staleness demotion
def _cursor_advanced_at(conn, op):
    """The ISO instant the op's cursor last advanced: the latest timestamp among the advancing
    events on the op, or None when there is none."""
    latest = None
    for e in _events_for_op(conn, op):
        if e["kind"] in _ADVANCING_KINDS:
            if latest is None or e["ts"] > latest:
                latest = e["ts"]
    return latest


def is_stale(conn, op, now, window_seconds=DEFAULT_STALENESS_SECONDS) -> bool:
    """True when `op` is live and its cursor has not advanced within `window_seconds` of `now`."""
    if op not in _live_ops(conn):
        return False
    advanced = _cursor_advanced_at(conn, op)
    if advanced is None:
        return True
    gap = _seconds_between(advanced, now)
    if gap is None:
        return False
    return gap > window_seconds


def demote_stale(conn, now, window_seconds=DEFAULT_STALENESS_SECONDS) -> list:
    """Demote every stale live op back to open, one `op-demote` event each, and release its
    stale live claims so the derivation folds it back to open (the op-level analogue of a lease
    returning a row). Returns the demoted op ids, in id order."""
    from alpaca import board
    demoted = []
    for o in _ops(conn):
        op = o["id"]
        if not is_stale(conn, op, now, window_seconds):
            continue
        with db.transaction(conn):
            db.append_event(conn, session="alpaca", actor="alpaca", kind=DEMOTE_KIND, op=op,
                            data={"from": LIVE, "to": OPEN, "reason": "cursor stale",
                                  "window_seconds": window_seconds, "now": now},
                            conn_in_txn=True)
        live = board.live_claims(conn)
        for row_id, held in live.items():
            r = db.rows(conn, "rows", "id=?", (row_id,))
            if r and r[0].get("op") == op:
                board.release(conn, row_id, held.get("worker") or "alpaca", "alpaca",
                              reason="op %s demoted: cursor stale" % op)
        demoted.append(op)
    return demoted


# --------------------------------------------------------------- projections
def entry(conn, op) -> dict:
    """One index row for `op`: its lifecycle state, its resume cursor and whether the cursor
    resolves. JSON-serialisable, so `alpaca status --json`, the board and the page can read it."""
    c = cursor(conn, op)
    row = db.rows(conn, "ops", "id=?", (op,))
    intent = row[0]["intent"] if row else None
    return {
        "op": op,
        "intent": intent,
        "state": state(conn, op),
        "cursor": c,
        "cursor_ok": c is None or _row_exists(conn, c),
    }


def list(conn, state=None) -> list:
    """Every op with its lifecycle state and resume cursor, in id order. `state` filters to one
    lifecycle state. A pure derivation: nothing here is a stored column."""
    out = [entry(conn, o["id"]) for o in _ops(conn)]
    if state is not None:
        out = [e for e in out if e["state"] == state]
    return out


def status_index(conn) -> dict:
    """The index as a JSON-ready payload for `alpaca status --json`: the per-op entries plus a
    count by state and the dangling-cursor report. Every value is a plain str / int / None."""
    entries = list(conn)
    counts = {s: 0 for s in STATES}
    for e in entries:
        counts[e["state"]] = counts.get(e["state"], 0) + 1
    return {
        "ops": entries,
        "counts": counts,
        "dangling_cursors": [{"op": op, "row_id": rid} for op, rid in dangling_cursors(conn)],
    }
