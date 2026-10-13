"""t-623 beyond the attack files: seal's transaction boundary, an unexpected error, and the readers
that must not take the standing After-action items op for the op being worked on."""
import pytest

from alpaca import aar, aaritems, cli, db, ops
from alpaca.tests.aar_items_kit import (OP, OTHER, author, ok, op_ids, reviewed, row, seal, setup, snapshot,
                                        standing_ops)


def test_seal_refuses_to_run_inside_a_transaction(project, monkeypatch):
    conn = setup(project, monkeypatch)
    author(conn, project, OP, [row("Add the missing gate")])
    before = snapshot(conn)
    with db.transaction(conn):
        with pytest.raises(RuntimeError):
            aar.seal(conn, project, OP)
    assert snapshot(conn) == before


def test_an_unexpected_error_propagates_and_writes_nothing(project, monkeypatch):
    conn = setup(project, monkeypatch)
    author(conn, project, OP, [row("Add the missing gate"), row("Second item")])
    assert reviewed(OP)[0] == cli.PASS
    before = snapshot(conn)
    calls = []
    real = ops.add_task

    def boom(*a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return real(*a, **kw)
    monkeypatch.setattr(ops, "add_task", boom)
    with pytest.raises(RuntimeError):
        aar.seal(conn, project, OP)
    assert snapshot(conn) == before
    monkeypatch.setattr(ops, "add_task", real)
    code, out = seal(OP)
    assert code == cli.PASS, out
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE title LIKE '%gate%' OR title LIKE 'Second%'").fetchone()[0] == 2


def test_intake_refuses_when_only_the_standing_op_is_open(project, monkeypatch):
    from alpaca import intake
    conn = setup(project, monkeypatch)
    ok("task", "move", "t-002", "doing")
    ok("task", "move", "t-002", "done", "--proof", __import__("alpaca.tests.proofkit", fromlist=["x"]).seal_for(project, "t-002"))
    ok("op", "close", OTHER, "--basis", "done")
    author(conn, project, OP, [row("Add the missing gate")])
    before = op_ids(conn)
    assert seal(OP)[0] == cli.PASS
    assert standing_ops(conn, before)
    with pytest.raises(intake.IntakeError):
        intake._open_op(conn, None)


def test_the_standing_op_is_found_by_marker_only(project, monkeypatch):
    conn = setup(project, monkeypatch)
    assert aaritems.standing_op(conn) is None
    author(conn, project, OP, [row("Add the missing gate")])
    assert seal(OP)[0] == cli.PASS
    sid = aaritems.standing_op(conn)
    assert sid and sid not in (OP, OTHER)
