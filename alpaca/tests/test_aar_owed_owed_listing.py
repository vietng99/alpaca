"""A10 (task text, causes C3, C8.4): `alpaca aar owed [<op>]` lists the owed triggers with their event
ids. With no op it covers every op in one listing, closed ops included (not silently dropped), and
an unattributed section for NO-GO and release FAIL rows no op can be tied to. With an op it lists
only that op's triggers. An unknown op is refused. Paid triggers (before the last valid seal) are
not listed as owed."""
from alpaca import cli
from alpaca.release import receipts as rc
from alpaca.tests import aarowedkit as k


def test_owed_without_an_op_lists_every_op_and_the_unattributed(project, capsys):
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    k.finish(project, conn, "t-002")
    a = k.reopen(conn, "t-001", "open")
    assert k.run(capsys, "aar", "owe", k.OTHER, "--reason", "the rollback was skipped")[0] == cli.PASS
    b = k.last_id(conn, "aar-owe")
    rc.record_receipt(project, "w-nobody", rc.CheckReceipt(build={}, gates=[], status="FAIL"))
    c = k.last_id(conn, "release-check")
    out = k.owed(capsys)
    for ev in (a, b, c):
        assert "event:%d" % ev in out, (ev, out)
    assert k.OP in out and k.OTHER in out and "unattributed" in out.lower(), out


def test_owed_with_an_op_lists_only_that_op(project, capsys):
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    k.finish(project, conn, "t-002")
    a = k.reopen(conn, "t-001", "open")
    b = k.reopen(conn, "t-002", "open")
    out = k.owed(capsys, k.OP)
    assert "event:%d" % a in out and "event:%d" % b not in out, out


def test_owed_on_an_unknown_op_is_refused(project, capsys):
    k.two_ops(project)
    code, out = k.run(capsys, "aar", "owed", "op-404")
    assert code == cli.FAIL, out


def test_paid_triggers_are_not_listed_as_owed(project, capsys):
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    a = k.reopen(conn, "t-001", "open")
    k.back_to_done(project, conn, "t-001")
    k.seal_aar(project, conn, capsys)
    assert "event:%d" % a not in k.owed(capsys, k.OP)
    assert "event:%d" % a not in k.owed(capsys)


def test_a_closed_op_with_an_old_trigger_is_shown(project, capsys):
    """History from before the gate (main record: release FAILs on op-004, op-008, op-045, all
    closed): the listing keeps them visible instead of dropping closed ops."""
    conn = k.two_ops(project)
    k.finish(project, conn, "t-002")
    a = k.reopen(conn, "t-002", "open")
    k.back_to_done(project, conn, "t-002")
    conn.execute("UPDATE ops SET status='closed' WHERE id=?", (k.OTHER,))
    conn.commit()
    assert "event:%d" % a in k.owed(capsys)
