"""A01 (done_bar[0], cause C8.2): a task moved from done back to open, doing, blocked or deferred makes
its op owe an AAR. Each `to` is its own form; a gate that lists only `open` lets the other three close.
Moves that do not leave done (done->done, event 44703 on the main record) or never were done
(open->doing, deferred->open) owe nothing."""
import pytest

from alpaca import cli, db
from alpaca.tests import aarowedkit as k


@pytest.mark.parametrize("to", ["open", "doing", "blocked", "deferred"])
def test_each_reopen_target_blocks_the_close_and_names_the_event(project, capsys, to):
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    ev = k.reopen(conn, "t-001", to)
    k.back_to_done(project, conn, "t-001")
    out = k.owed(capsys, k.OP)
    assert "event:%d" % ev in out, out
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, names=("reopen",), events=(ev,))


def test_a_reopen_on_the_other_op_does_not_block_this_one(project, capsys):
    conn = k.two_ops(project)
    k.finish(project, conn, "t-002")
    ev = k.reopen(conn, "t-002", "open")
    k.finish(project, conn, "t-001")
    assert "event:%d" % ev not in k.owed(capsys, k.OP)
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)


def test_moves_that_are_not_a_reopen_owe_nothing(project, capsys):
    conn = k.two_ops(project)
    assert k.move("t-001", "doing") == 0                     # open -> doing
    assert k.move("t-001", "deferred", extra=("--reactivate-when", "after the next release")) == 0
    assert k.move("t-001", "open") == 0                       # deferred -> open: from is not done
    k.finish(project, conn, "t-001")
    # the historic done->done shape (main record event 44703), written as task move writes it
    db.append_event(conn, session="cli", actor="human", kind="task-move", op=k.OP, ref="t-001",
                    data={"from": "done", "to": "done", "proof": None, "reason": "again"})
    out = k.owed(capsys, k.OP)
    assert "event:" not in out, out
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)


def test_a_reopen_older_than_200_task_moves_still_counts(project, capsys):
    """db.events returns the newest 200 rows by default (db.py events): a trigger read through it
    without a limit drops an old reopen and the op closes past it (cause C4.1)."""
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    ev = k.reopen(conn, "t-001", "open")
    k.back_to_done(project, conn, "t-001")
    for n in range(260):
        db.append_event(conn, session="w9", actor="agent", kind="task-move", op=k.OTHER, ref="t-002",
                        data={"from": "open", "to": "open", "proof": None, "reason": "noise %d" % n})
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, events=(ev,))


def test_a_reopen_clears_only_with_a_seal_written_after_it(project, capsys):
    """Order is by event id, not by ts: a reopen stamped with an older ts than the seal still owes
    when its id is newer (two rows in one second, or a skewed clock)."""
    from alpaca.clock import FixedClock
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    k.seal_aar(project, conn, capsys)
    ev = db.append_event(conn, session="cli", actor="human", kind="task-move", op=k.OP, ref="t-001",
                         data={"from": "done", "to": "open", "proof": None, "reason": "late gap"},
                         clock=FixedClock("2001-01-01T00:00:00+00:00"))["id"]
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, events=(ev,))
    assert code == cli.BLOCKED
