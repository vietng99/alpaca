"""A11 (causes C9, C4.2): the gate runs on every op close and `aar owed` derives every op, from the
record each time. On the main record that is 261k events, 140k of them heartbeats.

- Neither path calls aar.select (it loads every event with JSON decode), nor db.events with no kind.
- `aar owed` with no op derives all ops in one pass: its query count does not grow with the op count.
- An error inside the derivation never closes the op (op close wraps intent.judge in
  try/except: pass; the gate must not be wrapped that way).
- A malformed trigger row (data not JSON, `to` not a string) neither crashes into a close nor
  hides the other triggers."""
import pytest

from alpaca import aar, cli, db
from alpaca.tests import aarowedkit as k

REASON = "the rollback step was skipped in the release"


def test_owed_and_close_never_scan_the_whole_record(project, capsys, monkeypatch):
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    k.run(capsys, "aar", "owe", k.OP, "--reason", REASON)
    real = db.events

    def guarded(c, *, kind=None, session=None, limit=200):
        assert kind is not None or session is not None, "a whole-record db.events scan"
        return real(c, kind=kind, session=session, limit=limit)

    def no_select(*a, **kw):
        raise AssertionError("aar.select loads every event")

    monkeypatch.setattr(aar, "select", no_select)
    monkeypatch.setattr(db, "events", guarded)
    k.owed(capsys)
    k.owed(capsys, k.OP)
    code, out = k.close(capsys)
    assert code == cli.BLOCKED, out


def test_owed_for_all_ops_does_not_query_once_per_op(project, capsys, monkeypatch):
    conn = k.two_ops(project)
    real_connect = db.connect

    def selects_for_owed():
        seen = []

        def counting(*a, **kw):
            c = real_connect(*a, **kw)
            c.set_trace_callback(lambda s: seen.append(s) if "SELECT" in s.upper() else None)
            return c
        monkeypatch.setattr(db, "connect", counting)
        k.owed(capsys)
        monkeypatch.setattr(db, "connect", real_connect)
        return len(seen)

    few = selects_for_owed()
    for n in range(40):
        assert cli.main(["op", "new", "filler op %d" % n, "--done-when", "nothing"]) == 0
    many = selects_for_owed()
    assert many <= few + 10, (few, many)


def test_an_error_in_the_derivation_never_closes(project, capsys, monkeypatch):
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    k.run(capsys, "aar", "owe", k.OP, "--reason", REASON)

    def boom(*a, **kw):
        raise RuntimeError("the record could not be read")

    monkeypatch.setattr(aar, "latest_seal", boom)
    monkeypatch.setattr(aar, "check", boom)
    try:
        code, out = k.close(capsys)
    except RuntimeError:
        code, out = None, ""
    assert code != cli.PASS, out
    assert k.count(conn, "op-close", k.OP) == 0
    assert k.op_status(conn) == "open"


def test_a_malformed_trigger_row_does_not_hide_the_good_one(project, capsys):
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    db.append_event(conn, session="cli", actor="human", kind="task-move", op=k.OP, ref="t-001",
                    data={"from": "done", "to": ["open"], "proof": None, "reason": "odd"})
    db.append_event(conn, session="cli", actor="human", kind="task-move", op=k.OP, ref="t-001",
                    data=["not", "a", "dict"])
    good = db.append_event(conn, session="cli", actor="human", kind="task-move", op=k.OP, ref="t-001",
                           data={"from": "done", "to": "open", "proof": None, "reason": "a gap"})["id"]
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, events=(good,))


def test_owed_on_a_big_record_is_quick(project, capsys):
    """Coarse: 30k heartbeat rows plus 2k task moves; `aar owed` and the close gate stay well under
    the bound on a loaded host. The real probe (cause P10) times a scratch copy of the main record."""
    import time
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    with db.transaction(conn):
        for n in range(30000):
            db.append_event(conn, session="hb", actor="alpaca", kind="heartbeat", op=k.OTHER, ref="t-002",
                            data={"n": n}, conn_in_txn=True)
        for n in range(2000):
            db.append_event(conn, session="w9", actor="agent", kind="task-move", op=k.OTHER, ref="t-002",
                            data={"from": "open", "to": "open", "proof": None, "reason": "n%d" % n},
                            conn_in_txn=True)
    t0 = time.monotonic()
    k.owed(capsys)
    code, out = k.close(capsys)
    took = time.monotonic() - t0
    assert code == cli.PASS, out
    assert took < 5.0, took
