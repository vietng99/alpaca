"""(task text "created once (look it up by a stable marker, not by title text) and reused"; cause
C5) The standing op 'After-action items' opens at the first seal with no --into and is reused by
every later seal. A user op that happens to carry the same words is not it, a rename does not lose
it, and a closed one never takes new tasks. Opened at the first seal it is the NEWEST open op, so
every reader that means "the op being worked on" (ops.current_op: RESUME Current op and the Next
action fallback; intake._open_op: intake with no --op) must not pick it over the real work op."""
import json

from alpaca.tests.aar_items_kit import (OP, OTHER, author, filed, finish, ids, ok, op_ids, op_words, resume, row, run,
                     seal, section, setup, standing_ops, STANDING)
from alpaca import cli, db, ops, surface


def _first(project, monkeypatch):
    conn = setup(project, monkeypatch)
    author(conn, project, OP, [row("First op-001 item"), row("Second op-001 item")])
    before = op_ids(conn)
    code, out = seal(OP)
    assert code == cli.PASS, out
    new = standing_ops(conn, before)
    assert len(new) == 1, new
    return conn, before, new[0]


def test_the_second_aar_reuses_the_standing_op(project, monkeypatch):
    conn, before, standing = _first(project, monkeypatch)
    author(conn, project, OTHER, [row("First op-002 item")])
    code, out = seal(OTHER)
    assert code == cli.PASS, out
    assert standing_ops(conn, before) == [standing], "a second standing op was opened"
    assert {o for _t, o in filed(conn, OTHER)} == {standing}


def test_a_user_op_with_the_same_title_is_not_adopted(project, monkeypatch):
    conn = setup(project, monkeypatch)
    ok("op", "new", "After-action items", "--done-when", "never", "--title", "After-action items")
    decoy = op_ids(conn)[-1]
    author(conn, project, OP, [row("First op-001 item")])
    before = op_ids(conn)
    code, out = seal(OP)
    assert code == cli.PASS, out
    got = {o for _t, o in filed(conn, OP)}
    assert decoy not in got, "the seal adopted a user op by its title: %s" % decoy
    assert len(standing_ops(conn, before)) == 1


def test_a_renamed_standing_op_is_still_found(project, monkeypatch):
    conn, before, standing = _first(project, monkeypatch)
    ok("op", "title", standing, "Learning backlog")
    author(conn, project, OTHER, [row("First op-002 item")])
    assert seal(OTHER)[0] == cli.PASS
    assert standing_ops(conn, before) == [standing], "renaming the standing op lost it"
    assert {o for _t, o in filed(conn, OTHER)} == {standing}


def test_the_standing_op_is_named_after_action_items(project, monkeypatch):
    conn, _before, standing = _first(project, monkeypatch)
    assert STANDING in op_words(conn, standing)


def test_a_closed_standing_op_never_takes_new_tasks(project, monkeypatch):
    conn, before, standing = _first(project, monkeypatch)
    for tid in ids(conn, OP):
        finish(project, tid)
    ok("op", "close", standing, "--basis", "every after-action item is done")
    author(conn, project, OTHER, [row("First op-002 item")])
    code, out = seal(OTHER)
    if code == cli.PASS:
        for tid, op in filed(conn, OTHER):
            status = db.rows(conn, "ops", "id=?", (op,))[0]["status"]
            assert status == "open", "%s was filed into the closed op %s" % (tid, op)
    else:
        assert filed(conn, OTHER) == [], out


def test_current_op_stays_the_work_op(project, monkeypatch):
    conn, _before, standing = _first(project, monkeypatch)
    cur = ops.current_op(conn)
    assert cur and cur["id"] == OTHER, "current_op picked %s, the standing op is %s" % (cur and cur["id"], standing)
    body = section(resume(project), "## Current op") or []
    first = next((l for l in body if l.strip()), "")
    assert first.startswith(OTHER), "RESUME Current op names %r" % first


def test_intake_with_no_op_still_targets_the_work_op(project, monkeypatch):
    from alpaca import intake
    conn, _before, standing = _first(project, monkeypatch)
    got = intake._open_op(conn, None)
    assert got == OTHER, "intake with no --op would add rows to %s (the standing op is %s)" % (got, standing)


def test_next_action_does_not_point_at_an_item_while_work_is_open(project, monkeypatch):
    from alpaca import pad
    conn, _before, standing = _first(project, monkeypatch)
    db.meta_set(conn, "onboarded", "1")     # the fixture project is not onboarded; that line would win
    line = pad.next_action(project, conn=conn, expire=False)
    assert "t-002" in line, "Next action names %r while op-002's t-002 is open" % line


def test_status_json_keeps_its_declared_keys(project, monkeypatch):
    _first(project, monkeypatch)
    code, out = run("status", "--json")
    assert code == 0, out
    got = set(json.loads(out))
    declared = set(surface.TABLE["status"]["keys"])
    assert got - {"op_index"} <= declared, "status --json grew undeclared keys: %s" % (got - declared)
