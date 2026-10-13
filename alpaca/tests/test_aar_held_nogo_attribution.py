"""A01 (done_bar[0]; cause C3, door mismatch 1): a NO-GO makes an op owe an AAR. It goes to the op in
the event's op column, else to the op of the task the deciding session held in doing at that event.

Ways this still fails after it lands:
  * the hold is read from the board now, not replayed to the NO-GO's event id (a hold taken after
    the NO-GO attributes it; a task finished since loses it)
  * the shared `cli`/`alpaca` session attributes (many callers share it)
  * a decider holding tasks in two ops picks one of them
  * another session's hold attributes it
  * the op column loses to the hold
  * a later copy of the same decision (takeover/renew re-save, same kind/ref/decided_at) is
    attributed again from the hold at the copy's id, so a second op owes
  * the real writer (reviewflow.decide) is not covered, only hand-written rows
An attributed NO-GO is not loose, so it must block no other op."""
import pytest

from alpaca import cli
from alpaca.tests import aarowedkit as k

from alpaca.tests import aarheldkit as kit

W = "w-review-main"


@pytest.fixture(autouse=True)
def flow_profile(monkeypatch):
    kit.flow_profile(monkeypatch)


def _owed_by_op_001_only(project, conn, capsys, ev):
    assert kit.has(k.owed(capsys, k.OP), ev), k.owed(capsys, k.OP)
    assert not kit.is_loose(capsys, ev), k.owed(capsys)
    code, out = kit.close_other(project, conn, capsys)
    k.assert_closed(conn, code, out, k.OTHER)


def test_a_nogo_while_the_decider_holds_a_doing_task_owes_that_op(project, capsys):
    conn = k.two_ops(project)
    assert k.move("t-001", "doing", session=W) == 0
    ev = kit.nogo(conn, W)
    _owed_by_op_001_only(project, conn, capsys, ev)
    k.finish(project, conn, "t-001", session=W)
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, names=("no-go",), events=(ev,))
    k.seal_aar(project, conn, capsys)
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)


def test_a_nogo_while_the_decider_holds_a_claim_owes_that_op(project, capsys):
    conn = k.two_ops(project)
    assert cli.main(["--session", W, "task", "claim", "t-001", "--by", W]) == 0
    ev = kit.nogo(conn, W)
    _owed_by_op_001_only(project, conn, capsys, ev)


def test_the_real_writer_nogo_is_attributed_by_the_hold(project, capsys):
    conn = k.two_ops(project)
    assert k.move("t-001", "doing", session=W) == 0
    ev = kit.real_nogo(project, W)
    assert conn.execute("SELECT op FROM events WHERE id=?", (ev,)).fetchone()[0] is None
    _owed_by_op_001_only(project, conn, capsys, ev)
    k.finish(project, conn, "t-001", session=W)
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, events=(ev,))


def test_the_op_column_wins_over_the_hold(project, capsys):
    conn = k.two_ops(project)
    assert k.move("t-001", "doing", session=W) == 0
    ev = kit.nogo(conn, W, op=k.OTHER)
    k.finish(project, conn, "t-001", session=W)
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)
    code, out = kit.close_other(project, conn, capsys)
    k.assert_blocked(conn, code, out, k.OTHER, events=(ev,))


def test_a_hold_taken_after_the_nogo_does_not_attribute_it(project, capsys):
    """"At that time": the replay stops at the NO-GO's id. Read from the board now, t-001 would
    take it."""
    conn = k.two_ops(project)
    ev = kit.nogo(conn, W)
    assert k.move("t-001", "doing", session=W) == 0
    assert kit.is_loose(capsys, ev), k.owed(capsys)


def test_a_task_finished_before_the_nogo_does_not_attribute_it(project, capsys):
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001", session=W)
    ev = kit.nogo(conn, W)
    assert kit.is_loose(capsys, ev), k.owed(capsys)


def test_a_task_finished_after_the_nogo_keeps_it(project, capsys):
    """Read from the board now, the finished task holds nothing and the NO-GO turns loose."""
    conn = k.two_ops(project)
    assert k.move("t-001", "doing", session=W) == 0
    ev = kit.nogo(conn, W)
    k.finish(project, conn, "t-001", session=W)
    _owed_by_op_001_only(project, conn, capsys, ev)


@pytest.mark.parametrize("shared", ["cli", "alpaca"])
def test_a_nogo_by_a_shared_session_is_never_attributed(project, capsys, shared):
    conn = k.two_ops(project)
    assert k.move("t-001", "doing", session=shared) == 0
    ev = kit.nogo(conn, shared)
    assert kit.is_loose(capsys, ev), k.owed(capsys)


def test_a_decider_holding_tasks_in_two_ops_attributes_nothing_and_blocks_both(project, capsys):
    conn = k.two_ops(project)
    assert k.move("t-001", "doing", session=W) == 0
    assert k.move("t-002", "doing", session=W) == 0
    ev = kit.nogo(conn, W)
    assert kit.is_loose(capsys, ev), k.owed(capsys)
    k.finish(project, conn, "t-001", session=W)
    k.finish(project, conn, "t-002", session=W)
    for op in (k.OP, k.OTHER):
        code, out = k.close(capsys, op)
        k.assert_blocked(conn, code, out, op, events=(ev,))


def test_another_sessions_hold_does_not_attribute(project, capsys):
    conn = k.two_ops(project)
    assert k.move("t-001", "doing", session="w-other") == 0
    ev = kit.nogo(conn, W)
    assert kit.is_loose(capsys, ev), k.owed(capsys)


def test_a_later_copy_of_the_decision_is_not_attributed_again(project, capsys):
    """reviewflow re-saves the stored decision on a takeover or renew: the same fact (kind, ref,
    decided_at). The first id decides the op; the copy, recorded while W holds t-002, must not make
    op-002 owe as well."""
    conn = k.two_ops(project)
    assert k.move("t-001", "doing", session=W) == 0
    ev = kit.nogo(conn, W)
    k.finish(project, conn, "t-001", session=W)
    assert k.move("t-002", "doing", session=W) == 0
    copy = kit.nogo(conn, W)
    assert copy > ev
    k.finish(project, conn, "t-002", session=W)
    code, out = k.close(capsys, k.OTHER)
    k.assert_closed(conn, code, out, k.OTHER)
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, events=(ev,))


def test_a_go_while_holding_owes_nothing(project, capsys):
    conn = k.two_ops(project)
    assert k.move("t-001", "doing", session=W) == 0
    kit.nogo(conn, W, value="GO")
    k.finish(project, conn, "t-001", session=W)
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)
