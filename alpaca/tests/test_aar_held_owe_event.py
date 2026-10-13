"""A03 (done_bar[1]; cause C2, C1 printed remedy): `aar owe <op> --event <id>` attributes one loose
trigger to one op, which then owes the report, and clears it for every other op.

Ways this still fails after it lands:
  * the attribution clears the others but the named op owes nothing (laundering)
  * a closed op takes it: every open op is cleared and nothing ever gates the report
  * a second `aar owe Y --event L` re-attributes it, or both rows count
  * an id that is not a loose trigger (an op-open event, an unknown id, a trigger already tied to
    an op, an aar-owe row) is accepted and written
  * a later copy of the same FAIL fact is "attributed" while the first id keeps blocking
  * --event requires --reason, so the remedy printed in the BLOCKED line does not run as printed
  * a plain `aar owe <op> --reason R` (no --event) is taken as an attribution of some loose trigger
Every refusal writes nothing."""
import re
import shlex

import pytest

from alpaca import cli
from alpaca.tests import aarowedkit as k

from alpaca.tests import aarheldkit as kit


def _setup(project, capsys):
    conn = k.two_ops(project)
    ev = kit.fail(project, "cli")
    assert kit.is_loose(capsys, ev), k.owed(capsys)
    return conn, ev


def _refused(conn, capsys, op, value, *extra):
    before = k.count(conn)
    code, out = kit.owe_event(capsys, op, value, *extra)
    assert code not in (cli.PASS, 0, None), (op, value, out)
    assert k.count(conn) == before, "a refused aar owe --event wrote an event"
    return out


def test_attribution_makes_the_op_owe_and_clears_the_others(project, capsys):
    conn, ev = _setup(project, capsys)
    code, out = kit.owe_event(capsys, k.OP, ev)
    assert code == cli.PASS, out
    assert not kit.is_loose(capsys, ev), k.owed(capsys)
    assert kit.has(k.owed(capsys, k.OP), ev), k.owed(capsys, k.OP)
    code, out = kit.close_other(project, conn, capsys)
    k.assert_closed(conn, code, out, k.OTHER)
    k.finish(project, conn, "t-001")
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out)
    k.seal_aar(project, conn, capsys)
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)


def test_reason_is_accepted_beside_event(project, capsys):
    conn, ev = _setup(project, capsys)
    code, out = kit.owe_event(capsys, k.OP, ev, "--reason", kit.REASON)
    assert code == cli.PASS, out
    assert not kit.is_loose(capsys, ev)


def test_a_second_attribution_is_refused(project, capsys):
    conn, ev = _setup(project, capsys)
    assert kit.owe_event(capsys, k.OP, ev)[0] == cli.PASS
    _refused(conn, capsys, k.OTHER, ev)
    code, out = kit.close_other(project, conn, capsys)
    k.assert_closed(conn, code, out, k.OTHER)
    k.finish(project, conn, "t-001")
    k.assert_blocked(conn, *k.close(capsys))


def test_a_closed_op_cannot_take_it(project, capsys):
    """A closed op would owe and block nothing, so the trigger would gate no report at all."""
    conn = k.two_ops(project)
    code, out = kit.close_other(project, conn, capsys)
    k.assert_closed(conn, code, out, k.OTHER)
    ev = kit.fail(project, "cli")
    _refused(conn, capsys, k.OTHER, ev)
    k.finish(project, conn, "t-001")
    k.assert_blocked(conn, *k.close(capsys), events=(ev,))


def test_a_closed_op_that_was_open_at_the_trigger_takes_it(project, capsys):
    """A FAIL recorded before the gate existed, under an op closed since: that op takes it and
    owes the report, and the open op is cleared. Closed directly on the record, as a close from
    before this gate would have been."""
    from alpaca import db
    conn, ev = _setup(project, capsys)
    with db.transaction(conn):
        db.append_event(conn, session="cli", actor="human", kind="op-close", op=k.OTHER, ref=k.OTHER,
                        data={"basis": "closed before the AAR gate"}, conn_in_txn=True)
        db.patch(conn, "ops", "id", k.OTHER, {"status": "closed"})
    code, out = kit.owe_event(capsys, k.OTHER, ev)
    assert code == cli.PASS, out
    listing = k.owed(capsys)
    assert re.search(r"%s \(closed\) owes an AAR" % k.OTHER, listing), listing
    k.finish(project, conn, "t-001")
    k.assert_closed(conn, *k.close(capsys))


def test_an_op_opened_after_the_trigger_either_refuses_or_then_owes(project, capsys):
    conn, ev = _setup(project, capsys)
    later, tid = kit.new_op(project)
    before = k.count(conn)
    code, out = kit.owe_event(capsys, later, ev)
    after = k.count(conn)
    k.finish(project, conn, tid)
    if code == cli.PASS:
        k.assert_blocked(conn, *k.close(capsys, later), later)
    else:
        assert after == before, out
        k.finish(project, conn, "t-001")
        k.assert_blocked(conn, *k.close(capsys), events=(ev,))


@pytest.mark.parametrize("what", ["op-open event", "unknown id", "attributed fail", "aar-owe row",
                                  "reopen", "not a number", "negative"])
def test_an_id_that_is_not_a_loose_trigger_is_refused(project, capsys, what):
    conn, ev = _setup(project, capsys)
    if what == "op-open event":
        value = k.op_open_event(conn, k.OP)
    elif what == "unknown id":
        value = 10 ** 7
    elif what == "attributed fail":
        assert k.move("t-002", "doing", session="w-held") == 0
        value = kit.fail(project, "w-held")
    elif what == "aar-owe row":
        assert k.run(capsys, "aar", "owe", k.OTHER, "--reason", kit.REASON)[0] == cli.PASS
        value = k.last_id(conn, "aar-owe")
    elif what == "reopen":
        k.finish(project, conn, "t-002")
        value = k.reopen(conn, "t-002")
    elif what == "not a number":
        value = "abc"
    else:
        value = -1
    _refused(conn, capsys, k.OP, value)
    assert kit.is_loose(capsys, ev)


def test_a_copy_id_is_refused_or_attributes_the_fact(project, capsys):
    """The same receipt recorded twice is one fact; the first id wins. Attributing the copy id must
    not leave the first id blocking the other ops."""
    from alpaca.release import receipts as rc
    conn = k.two_ops(project)
    receipt = rc.CheckReceipt(build={}, gates=[], status="FAIL")
    rc.record_receipt(project, "cli", receipt)
    first = k.last_id(conn, "release-check")
    rc.record_receipt(project, "cli", receipt)
    copy = k.last_id(conn, "release-check")
    assert copy > first
    before = k.count(conn)
    code, out = kit.owe_event(capsys, k.OP, copy)
    if code == cli.PASS:
        code, out = kit.close_other(project, conn, capsys)
        k.assert_closed(conn, code, out, k.OTHER)
    else:
        assert k.count(conn) == before, out
        assert kit.is_loose(capsys, first)


def test_a_plain_owe_attributes_no_loose_trigger(project, capsys):
    conn, ev = _setup(project, capsys)
    assert k.run(capsys, "aar", "owe", k.OP, "--reason", kit.REASON)[0] == cli.PASS
    assert kit.is_loose(capsys, ev)
    k.finish(project, conn, "t-002")
    k.assert_blocked(conn, *k.close(capsys, k.OTHER), k.OTHER, events=(ev,))


def test_the_remedy_printed_in_the_blocked_line_runs_as_printed(project, capsys):
    """t-620 r1: a printed command must run. Take the `alpaca aar owe ... --event ...` command out
    of op-002's BLOCKED line, fill a placeholder op with op-002, run it, and op-002 is cleared of
    the loose trigger (it then owes through the attribution)."""
    conn, ev = _setup(project, capsys)
    k.finish(project, conn, "t-002")
    line = k.assert_blocked(conn, *k.close(capsys, k.OTHER), k.OTHER, events=(ev,))
    found = re.search(r"alpaca (aar owe [^;()]*?--event\s+\S+)", line)
    assert found, line
    command = found.group(1).rstrip(".,")
    command = re.sub(r"--event\s+(<[^>]*>|ID|N|EVENT)(?!\S)", "--event %d" % ev, command)
    for placeholder in ("<op>", "OP", "<OP>", "OP-ID"):
        command = command.replace(" %s " % placeholder, " %s " % k.OTHER)
    argv = shlex.split(command)
    assert argv[2] == k.OTHER or argv[2] == k.OP, argv
    code, out = k.run(capsys, *argv)
    assert code == cli.PASS, (argv, out)
    assert not kit.is_loose(capsys, ev)
