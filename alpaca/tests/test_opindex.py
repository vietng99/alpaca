"""M2.7 proof - the op index: cursors and lifecycle states.

Asserts the Done-when on BOTH the positive and the negative path:

  * every op carries a state drawn from the closed set open | live | blocked | closed |
    archived, and the state is a DERIVATION over ops, rows, claims and events (a claim makes
    an op live, a blocked/failed row makes it blocked, a closed op is closed), never a stored
    column;
  * every op carries its own resume cursor: the first non-done obligation row of the op, or
    None when the op has no undischarged row;
  * archived and un-archive are RECORDED transitions (one event each), latest-wins;
  * staleness demotion under a FixedClock: an op marked live whose cursor has not advanced
    within the staleness window is demoted to open with an event, and an op whose cursor DID
    advance (or whose window has not passed) is left live;
  * the dangling-cursor report: a cursor that names a row which does not exist is reported,
    while a cursor that resolves is not.

The clock is injected with FixedClock so the lease/staleness timing is deterministic.
"""
import json

import pytest

from alpaca import board, clock, db, opindex, util
from alpaca.checklist import verdict_row


# --------------------------------------------------------------- fixtures / helpers
def _setup(project):
    from alpaca import cli
    cli.main(["init"])
    cli.main(["onboard", "--name", "demo", "--who", "v:owner", "--what", "w"])
    return db.connect(project)


def _op(conn, oid="op-001", intent="do the thing", status="open"):
    db.upsert(conn, "ops", "id", {"id": oid, "intent": intent, "done_when": "done",
                                  "status": status, "opened": util.now_iso(),
                                  "closed": None, "phases": "[]"})
    return oid


def _row(conn, rid, *, op="op-001", phase="build", step="s1",
         statement="do the thing properly here now"):
    r = {
        "id": rid, "kind": "item", "op": op, "phase": phase, "step": step,
        "statement": statement, "proof": "local:spec.md", "where_": "", "how": "",
        "when_": "", "why": "", "session": None, "operator": None, "status": "open",
        "tag": "Specced", "content_hash": util.sha256_hex("row/" + rid),
        "prev_hash": None, "supersedes": None,
    }
    db.upsert(conn, "rows", "id", r)
    return r


def _find(rows, op):
    for r in rows:
        if r["op"] == op:
            return r
    raise AssertionError("op %r not in the index" % op)


# --------------------------------------------------------------- the state set
def test_state_set_is_the_closed_five(project):
    conn = _setup(project)
    _op(conn, "op-001")
    _row(conn, "r-1", op="op-001")
    listed = opindex.list(conn)
    assert set(opindex.STATES) == {"open", "live", "blocked", "closed", "archived"}
    for e in listed:
        assert e["state"] in opindex.STATES


def test_open_op_with_a_plain_row_is_open(project):
    conn = _setup(project)
    _op(conn, "op-001")
    _row(conn, "r-1", op="op-001")
    assert _find(opindex.list(conn), "op-001")["state"] == "open"


def test_a_live_claim_makes_the_op_live(project):
    conn = _setup(project)
    _op(conn, "op-001")
    _row(conn, "r-1", op="op-001")
    board.claim(conn, "r-1", "worker-a", "s", minutes=60)
    assert _find(opindex.list(conn), "op-001")["state"] == "live"


def test_a_blocked_row_makes_the_op_blocked(project):
    conn = _setup(project)
    r = _op(conn, "op-001")
    row = _row(conn, "r-1", op="op-001")
    verdict_row.discharge(conn, "r-1", row["content_hash"], "alpaca-test", vc_blocked(), [], "L2", "s")
    assert _find(opindex.list(conn), "op-001")["state"] == "blocked"


def test_a_closed_op_is_closed(project):
    conn = _setup(project)
    _op(conn, "op-001", status="closed")
    _row(conn, "r-1", op="op-001")
    assert _find(opindex.list(conn), "op-001")["state"] == "closed"


def test_list_filters_by_state(project):
    conn = _setup(project)
    _op(conn, "op-001")
    _op(conn, "op-002", status="closed")
    only_open = opindex.list(conn, state="open")
    assert {e["op"] for e in only_open} == {"op-001"}
    only_closed = {e["op"] for e in opindex.list(conn, state="closed")}
    assert "op-002" in only_closed and "op-001" not in only_closed


# --------------------------------------------------------------- the per-op cursor
def test_cursor_is_the_first_non_done_row(project):
    conn = _setup(project)
    _op(conn, "op-001")
    a = _row(conn, "r-1", op="op-001")
    _row(conn, "r-2", op="op-001")
    # r-1 discharged: the cursor moves to r-2.
    verdict_row.discharge(conn, "r-1", a["content_hash"], "alpaca-test", 0, ["local:a"], "L2", "s")
    assert opindex.cursor(conn, "op-001") == "r-2"


def test_cursor_is_none_when_every_row_is_done(project):
    conn = _setup(project)
    _op(conn, "op-001")
    a = _row(conn, "r-1", op="op-001")
    verdict_row.discharge(conn, "r-1", a["content_hash"], "alpaca-test", 0, ["local:a"], "L2", "s")
    assert opindex.cursor(conn, "op-001") is None


# --------------------------------------------------------------- archived / un-archive
def test_archive_and_unarchive_are_recorded_transitions(project):
    conn = _setup(project)
    _op(conn, "op-001")
    _row(conn, "r-1", op="op-001")
    before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    opindex.archive(conn, "op-001", session="s", reason="parked")
    assert _find(opindex.list(conn), "op-001")["state"] == "archived"
    after = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert after == before + 1  # exactly one event
    opindex.unarchive(conn, "op-001", session="s", reason="resumed")
    assert _find(opindex.list(conn), "op-001")["state"] == "open"  # latest-wins


# --------------------------------------------------------------- staleness demotion
def test_stale_live_op_is_demoted_to_open_with_an_event(project):
    conn = _setup(project)
    try:
        util.set_clock(clock.FixedClock(start="2026-01-01T00:00:00+00:00"))
        _op(conn, "op-001")
        _row(conn, "r-1", op="op-001")
        board.claim(conn, "r-1", "worker-a", "s", minutes=10)
        assert _find(opindex.list(conn), "op-001")["state"] == "live"
        before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        # now is far past the staleness window and the cursor never advanced.
        now = "2026-01-01T05:00:00+00:00"
        demoted = opindex.demote_stale(conn, now, window_seconds=1800)
    finally:
        util.set_clock(None)
    assert demoted == ["op-001"]
    assert _find(opindex.list(conn), "op-001")["state"] == "open"
    after = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert after > before  # the demotion is recorded
    kinds = [e["kind"] for e in db.events(conn, limit=10 ** 9)]
    assert opindex.DEMOTE_KIND in kinds


def test_fresh_live_op_is_not_demoted(project):
    conn = _setup(project)
    try:
        util.set_clock(clock.FixedClock(start="2026-01-01T00:00:00+00:00"))
        _op(conn, "op-001")
        _row(conn, "r-1", op="op-001")
        board.claim(conn, "r-1", "worker-a", "s", minutes=60)
        # now is inside the staleness window: nothing is demoted. Assert while the FixedClock
        # is still installed, so the live-claim reader sees the same now the claim was leased at.
        now = "2026-01-01T00:10:00+00:00"
        demoted = opindex.demote_stale(conn, now, window_seconds=1800)
        assert demoted == []
        assert _find(opindex.list(conn), "op-001")["state"] == "live"
    finally:
        util.set_clock(None)


def test_advancing_the_cursor_keeps_a_live_op_live(project):
    conn = _setup(project)
    try:
        util.set_clock(clock.FixedClock(start="2026-01-01T00:00:00+00:00"))
        _op(conn, "op-001")
        a = _row(conn, "r-1", op="op-001")
        _row(conn, "r-2", op="op-001")
        board.claim(conn, "r-2", "worker-a", "s", minutes=600)
        util.set_clock(clock.FixedClock(start="2026-01-01T04:59:00+00:00"))
        # the cursor advances late in the window: r-1 discharged just before now.
        verdict_row.discharge(conn, "r-1", a["content_hash"], "alpaca-test", 0, ["local:a"], "L2", "s")
        now = "2026-01-01T05:00:00+00:00"
        demoted = opindex.demote_stale(conn, now, window_seconds=1800)
        assert demoted == []
        assert _find(opindex.list(conn), "op-001")["state"] == "live"
    finally:
        util.set_clock(None)


# --------------------------------------------------------------- the dangling cursor
def test_a_cursor_naming_a_nonexistent_row_is_reported(project):
    conn = _setup(project)
    _op(conn, "op-001")
    _row(conn, "r-1", op="op-001")
    # pin the cursor to a row that was never committed.
    opindex.set_cursor(conn, "op-001", "r-ghost", session="s")
    dangling = opindex.dangling_cursors(conn)
    assert ("op-001", "r-ghost") in dangling
    entry = _find(opindex.list(conn), "op-001")
    assert entry["cursor"] == "r-ghost"
    assert entry["cursor_ok"] is False


def test_a_cursor_that_resolves_is_not_dangling(project):
    conn = _setup(project)
    _op(conn, "op-001")
    _row(conn, "r-1", op="op-001")
    opindex.set_cursor(conn, "op-001", "r-1", session="s")
    assert opindex.dangling_cursors(conn) == []
    entry = _find(opindex.list(conn), "op-001")
    assert entry["cursor"] == "r-1" and entry["cursor_ok"] is True


# --------------------------------------------------------------- status exposure
def test_status_index_is_json_serialisable(project):
    conn = _setup(project)
    _op(conn, "op-001")
    _row(conn, "r-1", op="op-001")
    payload = opindex.status_index(conn)
    # round-trips through json cleanly, so alpaca status --json can carry it.
    assert json.loads(json.dumps(payload)) == payload
    assert any(o["op"] == "op-001" for o in payload["ops"])


def vc_blocked():
    from alpaca.gates import verdict as vc
    return vc.BLOCKED
