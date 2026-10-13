"""(op-057 t-623) The task's contract is the one store of an item's owner, due, size and done-when.
The seal rewrites a contract only while its own is the newest; a contract anyone else wrote is left
as it is and the difference is noted. Overdue and Carried over read the newest contract, and a full
standing op is followed by a new one."""
from alpaca.tests.aar_items_kit import (OP, OTHER, author, by_action, contract, ok, overdue, row, run, seal,
                                        setup, standing_ops, op_ids)
from alpaca import aar, cli, taskplan


def test_overdue_follows_a_due_moved_in_the_task_contract(project, monkeypatch):
    """The task text puts the due date in the task's contract. A human who moves it there (the
    contract verb is how a task's contract changes) should not still see the item as late."""
    conn = setup(project, monkeypatch)
    author(conn, project, OP, [row("Alpha item", due="2026-10-01")])
    code, out = seal(OP)
    assert code == cli.PASS, out
    tid = by_action(conn, OP, "Alpha item")
    c = contract(conn, tid)
    expected = [l if not l.startswith("due: ") else "due: 2026-12-01" for l in c["expected"]]
    ok("task", "contract", tid, *sum([["--expected", e] for e in expected], []),
       *sum([["--done-bar", d] for d in c["done_bar"]], []), *sum([["--input", i] for i in c["input"]], []))
    assert "due: 2026-12-01" in contract(conn, tid)["expected"]
    code, out = run("status")
    late = [l for l in overdue(out) if tid in l]
    assert not late, "contract says due 2026-12-01, Overdue still says: %s" % late


def test_a_reseal_keeps_contract_lines_a_human_added(project, monkeypatch):
    """A person's contract is left as it is and the difference is noted: the re-seal does not write
    over a contract the seal did not write last, so the fail case stays."""
    conn = setup(project, monkeypatch)
    author(conn, project, OP, [row("Alpha item", due="2026-10-20", size="S")])
    code, out = seal(OP)
    assert code == cli.PASS, out
    tid = by_action(conn, OP, "Alpha item")
    c = contract(conn, tid)
    ok("task", "contract", tid, *sum([["--expected", e] for e in c["expected"]], []),
       *sum([["--done-bar", d] for d in c["done_bar"]], []), *sum([["--input", i] for i in c["input"]], []),
       "--fail-case", "the clock drifts after a suspend")
    assert contract(conn, tid)["fail_cases"] == ["the clock drifts after a suspend"]
    author(conn, project, OP, [row("Alpha item", due="2026-10-20", size="M")])
    code, out = seal(OP)
    assert code == cli.PASS, out
    after = contract(conn, tid)
    assert after.get("fail_cases") == ["the clock drifts after a suspend"], (
        "re-seal dropped the human's fail case; contract now %s\nseal said:\n%s"
        % (after, out))
    assert any(l.strip().startswith("note:") and tid in l and "size" in l for l in out.splitlines()), out
    assert "size: S" in after["expected"], after
    assert not [l for l in out.splitlines() if l.strip().startswith("updated")], out


def test_the_81st_report_is_refused_and_names_into(project, monkeypatch):
    """The standing op is opened once and reused, and each report adds one plan group to it, so
    the default path has a lifetime cap of taskplan.MAX_GROUPS (80) reports. D5: report 81 does not
    open a second standing op; the seal refuses and names --into."""
    from alpaca import taskplan
    conn = setup(project, monkeypatch)
    before = op_ids(conn)
    author(conn, project, OP, [row("Alpha item")])
    code, out = seal(OP)
    assert code == cli.PASS, out
    standing = standing_ops(conn, before)[0]
    for i in range(taskplan.MAX_GROUPS - 1):     # 79 more sealed reports, one group each
        taskplan.group(conn, standing, "aar-op-%03d" % (100 + i), "After-action items of op-%03d" % (100 + i),
                       "after-action", session="cli", actor="agent")
    author(conn, project, OTHER, [row("Bravo item")])
    code, out = seal(OTHER)
    assert code != cli.PASS and "--into" in out, out
    assert aar.latest_seal(conn, OTHER) is None
    assert standing_ops(conn, before) == [standing]


def test_a_full_into_op_is_refused_with_the_flag_named(project, monkeypatch):
    conn = setup(project, monkeypatch)
    ok("op", "new", "Full op", "--done-when", "never")
    full = op_ids(conn)[-1]
    for i in range(taskplan.MAX_GROUPS):
        taskplan.group(conn, full, "aar-op-%03d" % (100 + i), "g%d" % i, "after-action", session="cli", actor="agent")
    author(conn, project, OP, [row("Alpha item")])
    code, out = seal(OP, "--into", full)
    assert code != cli.PASS and "--into" in out and "full" in out, out
