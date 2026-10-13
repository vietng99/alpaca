"""A05 (done_bar[1], causes C4.3, C4.6, C8.3): op close on an op owing an unsealed AAR exits BLOCKED
(cli.BLOCKED, the `GATE alpaca-op-close: BLOCKED (` line of the tasks-not-done path), names the
trigger, writes nothing; after `aar seal` it closes.

Siblings: an unsealed draft does not clear; a seal clears only the triggers before it (a later
trigger owes again); a sealed report edited or deleted after the seal does not clear (aar check
already calls it changed or gone); the tasks gate still speaks first and the AAR gate still runs
once the tasks are done; the op-close event data keeps its shape."""
import os

from alpaca import cli
from alpaca.tests import aarowedkit as k

REASON = "the rollback step was skipped in the release"


def owed_op(project, capsys):
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    code, out = k.run(capsys, "aar", "owe", k.OP, "--reason", REASON)
    assert code == cli.PASS, out
    return conn, k.last_id(conn, "aar-owe")


def test_blocked_then_sealed_then_closed(project, capsys):
    conn, ev = owed_op(project, capsys)
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, events=(ev,))
    assert "aar seal %s" % k.OP in out, out          # says how to clear it
    k.seal_aar(project, conn, capsys)
    assert "event:%d" % ev not in k.owed(capsys, k.OP)
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)
    data = k.db.events(conn, kind="op-close", limit=10)[-1]["data"]
    assert set(data) == {"basis"}, data


def test_a_draft_that_is_not_sealed_does_not_clear(project, capsys):
    conn, ev = owed_op(project, capsys)
    code, out = k.run(capsys, "aar", "new", k.OP)
    assert code == cli.PASS, out
    k.write(k.report(project, k.OP), k.fill(k.read(k.report(project, k.OP)), k.op_open_event(conn, k.OP)))
    code, out = k.run(capsys, "aar", "check", k.OP)
    assert code == cli.PASS, out                      # a passing check is not a seal
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, events=(ev,))


def test_a_trigger_after_the_seal_owes_again(project, capsys):
    conn, ev = owed_op(project, capsys)
    k.seal_aar(project, conn, capsys)
    code, out = k.run(capsys, "aar", "owe", k.OP, "--reason", "a second gap found after the seal")
    later = k.last_id(conn, "aar-owe")
    code, out = k.close(capsys)
    line = k.assert_blocked(conn, code, out, events=(later,))
    assert "event:%d" % ev not in line, line          # the paid one is not named again
    k.seal_aar(project, conn, capsys)                  # re-seal the same, still valid report
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)


def test_a_sealed_report_edited_after_the_seal_does_not_clear(project, capsys):
    conn, ev = owed_op(project, capsys)
    k.seal_aar(project, conn, capsys)
    path = k.report(project, k.OP)
    k.write(path, k.read(path) + "\n")
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out)
    k.seal_aar(project, conn, capsys)
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)


def test_a_sealed_report_deleted_after_the_seal_does_not_clear(project, capsys):
    conn, ev = owed_op(project, capsys)
    k.seal_aar(project, conn, capsys)
    os.remove(k.report(project, k.OP))
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out)


def test_a_hand_written_seal_event_with_no_report_does_not_clear(project, capsys):
    """An agent can append an aar-seal row through db (or a future verb that writes one by mistake):
    the gate trusts a seal only when aar check agrees the report is that sealed file."""
    conn, ev = owed_op(project, capsys)
    k.db.append_event(conn, session="cli", actor="agent", kind="aar-seal", op=k.OP, ref=k.OP,
                      data={"format": 2, "path": ".alpaca/aar/%s.md" % k.OP, "sha256": "0" * 64})
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out)


def test_tasks_gate_speaks_first_then_the_aar_gate(project, capsys):
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    ev = k.reopen(conn, "t-001", "open")
    code, out = k.close(capsys)
    assert code == cli.BLOCKED and "tasks not done" in out, out
    k.back_to_done(project, conn, "t-001")
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, events=(ev,))


def test_a_refused_close_writes_no_event_at_all(project, capsys):
    conn, ev = owed_op(project, capsys)
    before = k.count(conn)
    code, out = k.close(capsys)
    assert code == cli.BLOCKED
    assert k.count(conn) == before, "a refused close wrote an event"
