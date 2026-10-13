"""A02 (done_bar[1], fail_case[0]; cause C3, C4, door mismatches 1 and 2): a trigger no op can be tied
to (a NO-GO or a release check FAIL) fails closed. While it is unattributed it blocks the close of
every op that was open when it was recorded (op-open event id below the trigger's id), and the
BLOCKED line names it and the way out.

Ways this still fails after it lands:
  * a loose trigger still blocks nothing (fail_case[0])
  * an op opened after the trigger is blocked too (the six historical FAILs would block every new op)
  * "open when recorded" is read from ts strings or from the ops table's `opened` text, not event ids
    (t-622 F1: mixed offsets sort wrong), so a trigger with an odd ts lands on the wrong side
  * an op closed before the trigger is named as blocked
  * a seal of op X answers a loose trigger for another op it blocks (a seal answers it for X only)
  * the BLOCKED line names the trigger but not `aar owe <op> --event <id>`, or prints only the seal
    and waive remedies of an attributed trigger
  * an open op with no op-open event (imported, hand-made) is never blocked (fail open)
  * an attributed trigger (aar owe, reopen) starts blocking other ops"""
import pytest

from alpaca import cli, db
from alpaca.clock import FixedClock
from alpaca.tests import aarowedkit as k

from alpaca.tests import aarheldkit as kit


@pytest.fixture(autouse=True)
def flow_profile(monkeypatch):
    kit.flow_profile(monkeypatch)


def _remedy(line, ev):
    low = line.lower()
    assert "aar owe" in low and "--event" in low, line
    assert kit.has(line, ev), line


@pytest.mark.parametrize("how", ["fail by cli", "fail by alpaca", "fail by a session holding nothing",
                                 "fail by a session in two ops", "nogo by nobody"])
def test_a_loose_trigger_blocks_every_op_open_when_it_was_recorded(project, capsys, how):
    conn = k.two_ops(project)
    if how == "fail by cli":
        ev = kit.fail(project, "cli")
    elif how == "fail by alpaca":
        ev = kit.fail(project, "alpaca")
    elif how == "fail by a session holding nothing":
        ev = kit.fail(project, "w-idle")
    elif how == "fail by a session in two ops":
        assert k.move("t-001", "doing", session="w-two") == 0
        assert k.move("t-002", "doing", session="w-two") == 0
        ev = kit.fail(project, "w-two")
    else:
        ev = kit.nogo(conn, "w-idle")
    assert kit.is_loose(capsys, ev), k.owed(capsys)
    for op, tid in ((k.OP, "t-001"), (k.OTHER, "t-002")):
        k.finish(project, conn, tid, session="w-two" if how == "fail by a session in two ops" else None)
        code, out = k.close(capsys, op)
        line = k.assert_blocked(conn, code, out, op, events=(ev,))
        _remedy(line, ev)
    assert kit.blocks_named(capsys, ev) >= {k.OP, k.OTHER}, k.owed(capsys)


def test_an_op_opened_after_the_trigger_is_not_blocked(project, capsys):
    conn = k.two_ops(project)
    ev = kit.fail(project, "cli")
    later, tid = kit.new_op(project)
    assert k.op_open_event(conn, later) > ev
    k.finish(project, conn, tid)
    code, out = k.close(capsys, later)
    k.assert_closed(conn, code, out, later)
    assert later not in kit.blocks_named(capsys, ev)
    assert kit.has(k.owed(capsys), ev), "the loose trigger is no longer listed"


def test_fails_recorded_before_any_op_opened_block_no_later_op(project, capsys):
    """The shape of the six historical release-check FAILs: op NULL, recorded before the ops that
    are open now. They stay listed and block none of them."""
    assert cli.main(["init"]) == cli.PASS
    conn = db.connect(project)
    old = [kit.fail(project, "cli"), kit.fail(project, "w-gone"), kit.raw_fail(conn, ref="hist-1")]
    assert cli.main(["op", "new", "add the shout verb", "--done-when", "the verb prints upper case"]) == 0
    assert cli.main(["task", "add", "--title", "shout verb", k.OP, "write the shout verb", "--phase", "build"]) == 0
    k.finish(project, conn, "t-001")
    out = k.owed(capsys)
    for ev in old:
        assert kit.has(out, ev), out
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)


def test_open_when_recorded_is_read_by_event_id_not_by_timestamp(project, capsys):
    """A FAIL stamped in 2000 but appended after op-001 opened blocks op-001. A FAIL stamped in
    2999 but appended before op-003 opened does not block op-003."""
    conn = k.two_ops(project)
    early = kit.raw_fail(conn, ref="ts-early", clock=FixedClock("2000-01-01T00:00:00+00:00"))
    late = kit.raw_fail(conn, ref="ts-late", clock=FixedClock("2999-01-01T00:00:00+00:00"))
    later, tid = kit.new_op(project)
    k.finish(project, conn, "t-001")
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, events=(early, late))
    k.finish(project, conn, tid)
    code, out = k.close(capsys, later)
    k.assert_closed(conn, code, out, later)


def test_a_mixed_offset_trigger_time_does_not_move_it(project, capsys):
    """A ts written with a +14:00 offset reads as later text than the instant it names (t-622 F1);
    the id order is what counts."""
    conn = k.two_ops(project)
    ev = kit.raw_fail(conn, ref="ts-offset", clock=FixedClock("2026-01-01T00:00:00+14:00"))
    k.finish(project, conn, "t-001")
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, events=(ev,))


def test_an_op_closed_before_the_trigger_is_not_named(project, capsys):
    conn = k.two_ops(project)
    code, out = kit.close_other(project, conn, capsys)
    k.assert_closed(conn, code, out, k.OTHER)
    ev = kit.fail(project, "cli")
    named = kit.blocks_named(capsys, ev)
    assert k.OP in named and k.OTHER not in named, k.owed(capsys)


def test_a_seal_of_the_op_answers_a_loose_trigger_for_that_op(project, capsys):
    """t-633: the signed brief says op close refuses until the owed AAR is sealed or the owner waives
    it, so a seal answers a loose trigger for its own op and for no other op it blocks."""
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    k.finish(project, conn, "t-002")
    ev = kit.fail(project, "cli")
    k.seal_aar(project, conn, capsys)
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)
    code, out = k.close(capsys, k.OTHER)
    line = k.assert_blocked(conn, code, out, op=k.OTHER, events=(ev,))
    _remedy(line, ev)


def test_an_open_op_with_no_op_open_event_is_blocked(project, capsys):
    """An op row with no op-open event (imported, hand-made) has no known open time: fail closed
    (cause C3), never "was not open"."""
    conn = k.two_ops(project)
    db.upsert(conn, "ops", "id", {"id": "op-077", "intent": "imported op", "done_when": "x",
                                  "status": "open", "opened": "2026-01-01T00:00:00+00:00"})
    ev = kit.fail(project, "cli")
    code, out = k.close(capsys, "op-077")
    k.assert_blocked(conn, code, out, "op-077", events=(ev,))


def test_attributed_triggers_do_not_block_other_ops(project, capsys):
    conn = k.two_ops(project)
    code, out = k.run(capsys, "aar", "owe", k.OP, "--reason", kit.REASON)
    assert code == cli.PASS, out
    k.finish(project, conn, "t-001")
    k.reopen(conn, "t-001")
    code, out = kit.close_other(project, conn, capsys)
    k.assert_closed(conn, code, out, k.OTHER)


def test_a_loose_trigger_on_a_project_with_no_open_op_is_still_listed(project, capsys):
    """The one case the rule leaves with no close to refuse (cause, fail_case[0] section): it must
    at least stay on the `aar owed` list, never vanish."""
    assert cli.main(["init"]) == cli.PASS
    ev = kit.fail(project, "cli")
    out = k.owed(capsys)
    assert kit.has(out, ev) and "unattributed" in out.lower(), out
