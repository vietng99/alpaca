"""A06 (fail_cases[0] as the design reads it, causes C8.1, C8.4, C3): fail_cases[0] says "an op with no
trigger is blocked at close". Owed is derived from triggers, and every existing close test closes an
op with no AAR, so this attack pins the reading the trace and cause notes chose: no trigger -> no
AAR needed -> the close goes through exactly as before, and `aar owed` lists nothing for it. If the
orchestrator rules the line literal instead, this file is the one to flip.

Siblings: an imported op with rows but no events, an op whose only events are ordinary task moves,
and a trigger on an already closed op (cannot block anything, but `aar owed` still lists it)."""
from alpaca import cli, db
from alpaca.tests import aarowedkit as k


def test_an_op_with_no_trigger_closes_without_an_aar(project, capsys):
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    assert "event:" not in k.owed(capsys, k.OP)
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)
    assert k.count(conn, "aar-seal") == 0 and k.count(conn, "aar-new") == 0


def test_an_op_with_no_tasks_and_no_trigger_closes(project, capsys):
    conn = k.two_ops(project)
    assert cli.main(["op", "new", "an empty op", "--done-when", "nothing"]) == 0
    code, out = k.close(capsys, "op-003")
    k.assert_closed(conn, code, out, op="op-003")


def test_an_imported_op_with_rows_but_no_events_closes(project, capsys):
    conn = k.two_ops(project)
    db.upsert(conn, "ops", "id", {"id": "op-090", "intent": "imported", "status": "open"})
    assert "event:" not in k.owed(capsys, "op-090")
    code, out = k.close(capsys, "op-090")
    assert code == cli.PASS, out


def test_a_trigger_on_a_closed_op_is_listed(project, capsys):
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)
    ev = k.reopen(conn, "t-001", "open")
    assert "event:%d" % ev in k.owed(capsys, k.OP)
    assert "event:%d" % ev in k.owed(capsys)
