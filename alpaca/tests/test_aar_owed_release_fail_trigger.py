"""A03 (done_bar[0], cause C2): a release check FAIL receipt makes an op owe an AAR.

release-check events carry op NULL (receipts.record_receipt). The FAIL goes to the op whose task
the recording session held in `doing` at that event, by task move or by claim; with no such op, or
more than one, it is listed by `aar owed` as unattributed and blocks the close of every op open then
(t-629). Siblings that must not
attribute: a session that finished its task before the FAIL (task move done writes no
claim-release), a released claim, another session's hold, the shared fallback session `cli`.
A PASS, and a FAIL of any other receipt kind (preflight, build), is not a trigger."""
import pytest

from alpaca import cli
from alpaca.release import receipts as rc
from alpaca.tests import aarowedkit as k

W = "w-release-1"


def check(project, session, status="FAIL"):
    rc.record_receipt(project, session, rc.CheckReceipt(build={}, gates=[], status=status))
    return k.last_id(k.db.connect(project), "release-check")


def attributed_to(project, ev):
    """The op the fold ties the trigger event `ev` to, or None (unattributed)."""
    from alpaca import aar, db
    return {t["id"]: t["op"] for t in aar.triggers(db.connect(project), project)}[ev]


def owed_line_has(capsys, ev, op=None):
    return "event:%d" % ev in k.owed(capsys, op)


def test_a_fail_while_the_session_holds_a_doing_task_is_owed_by_that_op(project, capsys):
    conn = k.two_ops(project)
    assert k.move("t-001", "doing", session=W) == 0
    ev = check(project, W)
    k.finish(project, conn, "t-001", session=W)
    assert owed_line_has(capsys, ev, k.OP)
    assert not owed_line_has(capsys, ev, k.OTHER)
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, names=("release",), events=(ev,))
    k.seal_aar(project, conn, capsys)
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)


def test_a_fail_while_the_session_holds_a_claim_is_owed_by_that_op(project, capsys):
    conn = k.two_ops(project)
    assert cli.main(["--session", W, "task", "claim", "t-001", "--by", W]) == 0
    ev = check(project, W)
    k.finish(project, conn, "t-001", session=W)
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, events=(ev,))


@pytest.mark.parametrize("status", ["PASS", "pass", "ok"])
def test_a_check_that_did_not_fail_owes_nothing(project, capsys, status):
    conn = k.two_ops(project)
    assert k.move("t-001", "doing", session=W) == 0
    check(project, W, status)
    k.finish(project, conn, "t-001", session=W)
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)


def test_a_fail_of_another_receipt_kind_owes_nothing(project, capsys):
    conn = k.two_ops(project)
    assert k.move("t-001", "doing", session=W) == 0
    rc.record_receipt(project, W, rc.PreflightReceipt(source_sha="0" * 40, gates=[], status="FAIL"))
    rc.record_receipt(project, W, rc.BuildReceipt(source_hash="x", tree_hash="x", source_sha="x", tree="x",
                                                  inventory={}, status="FAIL"))
    k.finish(project, conn, "t-001", session=W)
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)


def test_a_fail_after_the_task_moved_to_done_is_unattributed(project, capsys):
    """task move done clears the claimant but writes no claim-release; a replay that ends a hold
    only on claim-release keeps the finished task held and blames its op (cause C2.2)."""
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001", session=W)
    ev = check(project, W)
    out = k.owed(capsys)
    assert "event:%d" % ev in out and "unattributed" in out.lower(), out
    assert attributed_to(project, ev) is None
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, events=(ev,))


def test_a_fail_after_the_claim_was_released_is_unattributed(project, capsys):
    from alpaca import claims
    conn = k.two_ops(project)
    assert cli.main(["--session", W, "task", "claim", "t-001", "--by", W]) == 0
    claims.release(conn, "t-001", W, session=W)
    ev = check(project, W)
    assert attributed_to(project, ev) is None
    k.finish(project, conn, "t-001", session=W)
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, events=(ev,))


def test_a_fail_by_a_session_that_holds_tasks_in_two_ops_is_unattributed(project, capsys):
    conn = k.two_ops(project)
    assert k.move("t-001", "doing", session=W) == 0
    assert k.move("t-002", "doing", session=W) == 0
    ev = check(project, W)
    out = k.owed(capsys)
    assert "event:%d" % ev in out and "unattributed" in out.lower(), out
    assert attributed_to(project, ev) is None
    k.finish(project, conn, "t-001", session=W)
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, events=(ev,))


def test_two_held_tasks_of_one_op_still_attribute(project, capsys):
    conn = k.two_ops(project)
    assert cli.main(["task", "add", "--title", "more", k.OP, "a second task", "--phase", "build"]) == 0
    assert k.move("t-001", "doing", session=W) == 0
    assert k.move("t-003", "doing", session=W) == 0
    ev = check(project, W)
    assert owed_line_has(capsys, ev, k.OP)


def test_a_fail_by_a_session_that_holds_nothing_is_unattributed_even_if_another_does(project, capsys):
    conn = k.two_ops(project)
    assert k.move("t-001", "doing", session=W) == 0
    ev = check(project, "w-other")
    assert attributed_to(project, ev) is None
    assert "event:%d" % ev in k.owed(capsys)


@pytest.mark.parametrize("fallback", ["cli", "alpaca"])
def test_a_fail_under_a_shared_fallback_session_is_never_attributed(project, capsys, fallback):
    """`cli` is what every verb records with no session (aar.py, ops.py `args.session or "cli"`) and
    `alpaca` is the lease sweeper's: many callers share them, so a hold under them proves nothing."""
    conn = k.two_ops(project)
    assert k.move("t-001", "doing", session=fallback) == 0
    ev = check(project, fallback)
    assert attributed_to(project, ev) is None
    assert "event:%d" % ev in k.owed(capsys)


def test_more_than_200_receipts_after_the_fail_do_not_hide_it(project, capsys):
    conn = k.two_ops(project)
    assert k.move("t-001", "doing", session=W) == 0
    ev = check(project, W)
    for _ in range(210):
        check(project, "w-noise", "PASS")
    k.finish(project, conn, "t-001", session=W)
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, events=(ev,))
