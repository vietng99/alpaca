"""(task text "Sealing must stay atomic"; cause C1, C3.4, C3.5, C6, C9.5) A seal either files every
action item and records the aar-seal event, or leaves no seal. The task lets the build pick one of
two models, so each failure attack accepts both and pins what each must keep:
  all-or-nothing: after the failure there is no seal, no task, no plan group, no contract, no op;
  resumable:      after the failure there is no seal, and the re-seal ends with exactly N tasks,
                  one group and one standing op, never a duplicate.
Refusals before filing (a hook, an unknown, closed or source --into op, a batch op, a full plan)
leave the record as it was and exit FAIL, never a traceback."""
import pytest

from alpaca.tests.aar_items_kit import (OP, OTHER, attempt, author, filed, finish, groups, ids, op_ids, ok, reviewed,
                     row, seal, setup, snapshot, standing_ops)
from alpaca import aar, batch, cli, db, taskplan

THREE = [row("First action item"), row("Second action item"), row("Third action item")]


def _ready(project, monkeypatch, rows=THREE):
    """op-001's report filled and its review accepted, so a seal below is refused only for the
    reason the test names."""
    conn = setup(project, monkeypatch)
    author(conn, project, OP, rows)
    assert reviewed(OP)[0] == cli.PASS
    return conn


def _no_half_seal(conn, base, ops_before, n=3):
    """The invariant after an injected failure, for either model, then the clean re-seal."""
    assert aar.latest_seal(conn, OP) is None, "the AAR is sealed although filing failed"
    if not ids(conn, OP):
        assert snapshot(conn) == base, "all-or-nothing left writes behind: %s -> %s" % (base, snapshot(conn))
    code, out = seal(OP)
    assert code == cli.PASS, out
    assert len(ids(conn, OP)) == n, "the re-seal ended with %d filed task(s), not %d" % (len(ids(conn, OP)), n)
    assert snapshot(conn)["tasks"] == base["tasks"] + n, "a duplicate task was filed on the re-seal"
    new = standing_ops(conn, ops_before)
    assert len(new) == 1, "the failed seal and the re-seal opened %d standing ops: %s" % (len(new), new)
    assert groups(conn, new[0]).count("aar-%s" % OP) == 1


def test_a_failure_on_the_second_task_leaves_no_half_seal(project, monkeypatch):
    conn = _ready(project, monkeypatch)
    base, ops_before = snapshot(conn), op_ids(conn)
    real, fired = db.upsert, []

    def boom(c, table, key, values):
        if table == "tasks" and not db.rows(c, "tasks", "id=?", (values.get("id"),)):
            fired.append(values.get("id"))
            if len(fired) == 2:
                raise RuntimeError("injected: the second filed task")
        return real(c, table, key, values)
    monkeypatch.setattr(db, "upsert", boom)
    code, out, exc = attempt("aar", "seal", OP)
    monkeypatch.setattr(db, "upsert", real)
    assert len(fired) >= 2, "the injection never fired: filing did not add task rows through db.upsert"
    assert code != cli.PASS, "seal said PASS after a filing failure:\n" + out
    _no_half_seal(conn, base, ops_before)


def test_a_failure_at_the_seal_event_leaves_no_half_seal(project, monkeypatch):
    conn = _ready(project, monkeypatch)
    base, ops_before = snapshot(conn), op_ids(conn)
    real = db.append_event

    def boom(*a, **k):
        if k.get("kind") == aar.KIND_SEAL:
            raise RuntimeError("injected: the aar-seal append")
        return real(*a, **k)
    monkeypatch.setattr(db, "append_event", boom)
    code, out, exc = attempt("aar", "seal", OP)
    monkeypatch.setattr(db, "append_event", real)
    assert code != cli.PASS, out
    _no_half_seal(conn, base, ops_before)


def test_a_refusing_hook_files_nothing(project, monkeypatch):
    conn = _ready(project, monkeypatch)
    base = snapshot(conn)
    monkeypatch.setattr(aar, "SEAL_HOOKS", aar.SEAL_HOOKS + [lambda *a: ["the review is not accepted"]])
    code, out = seal(OP)
    assert code == cli.FAIL, out
    assert snapshot(conn) == base, "a hook refused the seal but filing wrote: %s" % snapshot(conn)


def test_hooks_run_before_any_task_is_filed(project, monkeypatch):
    conn = _ready(project, monkeypatch)
    seen = []
    monkeypatch.setattr(aar, "SEAL_HOOKS", aar.SEAL_HOOKS + [lambda c, *a: seen.append(len(ids(c, OP))) or []])
    code, out = seal(OP)
    assert code == cli.PASS, out
    assert seen == [0], "a seal hook ran after filing (it saw %s filed task(s)); a refusing hook " \
                        "there would leave tasks with no seal" % seen
    assert len(ids(conn, OP)) == 3


@pytest.mark.parametrize("target", ["op-999", "op-1", "../op-002", ""])
def test_into_an_unknown_op_is_refused_and_files_nothing(project, monkeypatch, target):
    conn = _ready(project, monkeypatch)
    base = snapshot(conn)
    code, out, exc = attempt("aar", "seal", OP, "--into", target)
    assert exc is None, "seal --into %r crashed: %r" % (target, exc)
    assert code != cli.PASS, out
    assert snapshot(conn) == base and aar.latest_seal(conn, OP) is None


def test_into_a_closed_op_is_refused_and_files_nothing(project, monkeypatch):
    conn = _ready(project, monkeypatch)
    project_root = project
    finish(project_root, "t-002")
    ok("op", "close", OTHER, "--basis", "the other work is done")
    base = snapshot(conn)
    code, out, exc = attempt("aar", "seal", OP, "--into", OTHER)
    assert exc is None, repr(exc)
    assert code == cli.FAIL, out
    assert snapshot(conn) == base and aar.latest_seal(conn, OP) is None


def test_into_the_source_op_is_refused(project, monkeypatch):
    """Items filed into the op they review block its own close and join its own Timeline
    (cause C6); refuse it."""
    conn = setup(project, monkeypatch)
    author(conn, project, OTHER, [row("Fix the op-002 gap")])
    assert reviewed(OTHER)[0] == cli.PASS
    base = snapshot(conn)
    code, out, exc = attempt("aar", "seal", OTHER, "--into", OTHER)
    assert exc is None, repr(exc)
    assert code == cli.FAIL, out
    assert snapshot(conn) == base and aar.latest_seal(conn, OTHER) is None


def test_into_an_op_with_a_batch_is_a_clean_outcome(project, monkeypatch):
    """batch.require_phase raises TASK-PHASE-* for a task with no phase on an op with a batch, and
    EVERY op opened by `op new` has one (ops.py:279-280 open_batch). op-002 has it already; the hand
    item only makes that explicit. A01 test_into_files_in_that_op requires PASS here, so filing must
    pass a phase of batch.LADDER; this attack pins that a refusal, if any, is clean."""
    conn = _ready(project, monkeypatch)
    db.append_event(conn, session="hand", actor="hand", kind=batch.ITEM_KIND, op=OTHER, ref="b-1",
                    data={"id": "b-1", "title": "a batch item"})
    assert batch.has_batch(conn, OTHER)
    base = snapshot(conn)
    code, out, exc = attempt("aar", "seal", OP, "--into", OTHER)
    assert exc is None, "seal into a batch op crashed: %r" % exc
    if code == cli.PASS:
        assert len(filed(conn, OP)) == 3 and {o for _t, o in filed(conn, OP)} == {OTHER}
    else:
        assert code == cli.FAIL, out
        assert snapshot(conn) == base and aar.latest_seal(conn, OP) is None


def test_a_full_plan_is_a_clean_outcome(project, monkeypatch):
    """taskplan allows MAX_GROUPS groups per op plan; one group per AAR fills the standing op."""
    conn = _ready(project, monkeypatch, [row("First action item")])
    before = op_ids(conn)
    assert seal(OP)[0] == cli.PASS
    standing = standing_ops(conn, before)[0]
    for i in range(taskplan.MAX_GROUPS - len(groups(conn, standing))):
        taskplan.group(conn, standing, "filler-%02d" % i, "filler group", "build",
                       session="hand", actor="hand")
    assert len(groups(conn, standing)) == taskplan.MAX_GROUPS
    author(conn, project, OTHER, [row("Fix the op-002 gap"), row("Fix the second op-002 gap")])
    assert reviewed(OTHER)[0] == cli.PASS
    base = snapshot(conn)
    code, out, exc = attempt("aar", "seal", OTHER)
    assert exc is None, "a full plan crashed the seal: %r" % exc
    assert code == cli.FAIL, out                 # D5: never a second standing op
    assert snapshot(conn) == base and aar.latest_seal(conn, OTHER) is None
    assert "--into" in out, "the refusal does not name --into:\n" + out
    assert standing_ops(conn, before) == [standing]
