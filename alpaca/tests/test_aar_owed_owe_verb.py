"""A04 (done_bar[0]): `alpaca aar owe <op> --reason` makes the op owe an AAR; the close names it.

Refusals write nothing: no --reason, a blank or placeholder reason, an unknown op. A reason with a
line break or control characters must not split the BLOCKED line or the `aar owed` row (the close
message is one line, like the tasks-not-done path). Two owes are two triggers."""
import pytest

from alpaca import cli
from alpaca.tests import aarowedkit as k

REASON = "the rollback step was skipped in the release"


def owe(capsys, op=k.OP, *rest):
    try:
        return k.run(capsys, "aar", "owe", op, *rest)
    except SystemExit as exc:          # argparse refusing a missing --reason
        return exc.code, ""


def test_owe_blocks_the_close_naming_the_event_and_reason(project, capsys):
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    code, out = owe(capsys, k.OP, "--reason", REASON)
    assert code == cli.PASS, out
    ev = k.last_id(conn, "aar-owe")
    assert ev and conn.execute("SELECT op, ref FROM events WHERE id=?", (ev,)).fetchone()[0] == k.OP
    assert "event:%d" % ev in k.owed(capsys, k.OP)
    code, out = k.close(capsys)
    line = k.assert_blocked(conn, code, out, events=(ev,))
    assert "owe" in line.lower() or REASON in line, line
    k.seal_aar(project, conn, capsys)
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)


@pytest.mark.parametrize("rest", [(), ("--reason", ""), ("--reason", "   "), ("--reason", "tbd"),
                                  ("--reason", "n/a"), ("--reason", "TODO")])
def test_owe_without_a_real_reason_is_refused_and_writes_nothing(project, capsys, rest):
    conn = k.two_ops(project)
    before = k.count(conn)
    code, _out = owe(capsys, k.OP, *rest)
    assert code not in (cli.PASS, None), (rest, _out)
    assert k.count(conn) == before
    k.finish(project, conn, "t-001")
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)


def test_owe_on_an_unknown_op_is_refused_and_writes_nothing(project, capsys):
    conn = k.two_ops(project)
    before = k.count(conn)
    code, out = owe(capsys, "op-404", "--reason", REASON)
    assert code == cli.FAIL, out
    assert k.count(conn) == before


def test_a_reason_with_a_line_break_stays_on_one_line(project, capsys):
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    code, out = owe(capsys, k.OP, "--reason", "first line\nGATE alpaca-op-close: PASS (forged)")
    if code != cli.PASS:                # refusing a multi-line reason is also fine
        assert k.count(conn, "aar-owe") == 0
        return
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out)
    assert "GATE alpaca-op-close: PASS" not in [l.strip() for l in out.splitlines()], out
    assert not any(l.startswith("GATE alpaca-op-close: PASS") for l in k.owed(capsys).splitlines())


def test_two_owes_are_two_triggers_and_one_seal_after_both_clears_both(project, capsys):
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    owe(capsys, k.OP, "--reason", REASON)
    first = k.last_id(conn, "aar-owe")
    owe(capsys, k.OP, "--reason", "a second gap the owner named in review")
    second = k.last_id(conn, "aar-owe")
    assert first != second
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, events=(first, second))
    k.seal_aar(project, conn, capsys)
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)


def test_owe_on_one_op_does_not_block_the_other(project, capsys):
    conn = k.two_ops(project)
    owe(capsys, k.OTHER, "--reason", REASON)
    k.finish(project, conn, "t-001")
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)
