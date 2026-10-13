"""A trigger must not land where nothing can ever block on it (t-629 fail_case[0]).

Ways this still fails after it lands:
  * a session works a deferred task of a closed op and its NO-GO or FAIL goes to that closed op,
    which blocks nothing, and no open op is blocked either
  * a session holding a task of a standing op (Release watch, After-action items) attributes the
    trigger to an op that never closes
  * `aar owe <standing op> --event N` clears every real op with one agent command"""
import pytest

from alpaca import aar, cli, db, ops as _ops
from alpaca.release import followup
from alpaca.tests import aarowedkit as k

from alpaca.tests import aarheldkit as kit


@pytest.fixture(autouse=True)
def flow_profile(monkeypatch):
    kit.flow_profile(monkeypatch)


def release_watch_op(conn):
    with db.transaction(conn):
        return _ops.open_standing(conn, _ops.RELEASE_WATCH_KIND, followup.WATCH_INTENT, followup.WATCH_DONE,
                                  followup.WATCH_TITLE, session="cli", actor=followup.ACTOR)


def test_a_fail_by_a_session_working_in_a_closed_op_blocks_the_open_ops(project, capsys):
    conn = k.two_ops(project)
    assert cli.main(["task", "add", "--title", "later bit", k.OP, "a later bit", "--phase", "build"]) == 0
    extra = conn.execute("SELECT id FROM tasks WHERE op=? ORDER BY id DESC LIMIT 1", (k.OP,)).fetchone()[0]
    assert k.move(extra, "deferred", extra=("--reactivate-when", "after the next release")) == 0
    k.finish(project, conn, "t-001")
    code, out = k.close(capsys, k.OP)
    k.assert_closed(conn, code, out, k.OP)
    assert k.move(extra, "open", session="w-late") == 0
    assert k.move(extra, "doing", session="w-late") == 0
    ev = kit.fail(project, "w-late")
    assert kit.is_loose(capsys, ev)
    k.finish(project, conn, "t-002")
    code, out = k.close(capsys, k.OTHER)
    k.assert_blocked(conn, code, out, k.OTHER, names=("event:%d" % ev,))


def test_a_fail_by_a_session_holding_a_release_watch_task_stays_unattributed(project, capsys):
    conn = k.two_ops(project)
    standing = release_watch_op(conn)
    assert cli.main(["task", "add", "--title", "Release watch T+1h", standing, "run the release check"]) == 0
    tid = conn.execute("SELECT id FROM tasks WHERE op=?", (standing,)).fetchone()[0]
    assert k.move(tid, "doing", session="w-watch") == 0
    ev = kit.fail(project, "w-watch")
    assert kit.is_loose(capsys, ev)
    k.finish(project, conn, "t-001")
    code, out = k.close(capsys, k.OP)
    k.assert_blocked(conn, code, out, k.OP, names=("event:%d" % ev,))


def test_aar_owe_event_refuses_a_standing_op(project, capsys):
    conn = k.two_ops(project)
    standing = release_watch_op(conn)
    ev = kit.fail(project, "cli")
    code, out = kit.owe_event(capsys, standing, ev)
    assert code != cli.PASS and "standing op" in out, out
    assert kit.is_loose(capsys, ev)


def standing_task(conn, standing):
    assert cli.main(["task", "add", "--title", "Release watch T+1h", standing, "run the release check"]) == 0
    return conn.execute("SELECT id FROM tasks WHERE op=?", (standing,)).fetchone()[0]


def test_a_fail_by_a_session_holding_x_and_a_standing_task_goes_to_x(project, capsys):
    conn = k.two_ops(project)
    wt = standing_task(conn, release_watch_op(conn))
    assert k.move("t-001", "doing", session="w1") == 0
    assert k.move(wt, "doing", session="w1") == 0
    ev = kit.fail(project, "w1")
    assert not kit.is_loose(capsys, ev)
    due, loose = aar.owed(conn, project)
    assert [t["id"] for t in due.get(k.OP, [])] == [ev]


def test_a_nogo_by_a_session_holding_x_and_a_standing_task_goes_to_x(project, capsys):
    conn = k.two_ops(project)
    wt = standing_task(conn, release_watch_op(conn))
    assert k.move("t-001", "doing", session="w1") == 0
    assert k.move(wt, "doing", session="w1") == 0
    ev = kit.nogo(conn, "w1")
    due, loose = aar.owed(conn, project)
    assert ev not in [t["id"] for t in loose]
    assert [t["id"] for t in due.get(k.OP, [])] == [ev]


def test_a_fail_by_a_session_holding_x_and_a_task_of_a_closed_op_goes_to_x(project, capsys):
    conn = k.two_ops(project)
    assert cli.main(["task", "add", "--title", "later bit", k.OP, "a later bit", "--phase", "build"]) == 0
    extra = conn.execute("SELECT id FROM tasks WHERE op=? ORDER BY id DESC LIMIT 1", (k.OP,)).fetchone()[0]
    assert k.move(extra, "deferred", extra=("--reactivate-when", "after the next release")) == 0
    k.finish(project, conn, "t-001")
    code, out = k.close(capsys, k.OP)
    k.assert_closed(conn, code, out, k.OP)
    assert k.move(extra, "open", session="w-late") == 0
    assert k.move(extra, "doing", session="w-late") == 0
    assert k.move("t-002", "doing", session="w-late") == 0
    ev = kit.fail(project, "w-late")
    assert not kit.is_loose(capsys, ev)
    due, loose = aar.owed(conn, project)
    assert [t["id"] for t in due.get(k.OTHER, [])] == [ev]
