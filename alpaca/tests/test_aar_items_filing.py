"""(done_bar[0]; cause C3.3, C4.1, C4.4) Sealing an AAR with N action items files N tasks with
owner, due, size and done-when. Each task is mapped (group aar-<op>, after [], resources []), its
contract holds the done-when as a done bar line and owner, due and size as lines, and every line's
origin is derived=aar:<op>, never owner authority. The owner cell is the report author's word: it
names who should act, it is not a lease, so the task has no claimant. Titles fit TITLE_MAX and are
never empty or cut at a colon into one word (ops.short_title cuts at the first colon)."""
import pytest

from alpaca.tests.aar_items_kit import (OP, OTHER, author, by_action, contract, contract_lines, entry, filed, groups,
                     op_ids, op_words, row, seal, setup, standing_ops, task, STANDING)
from alpaca import cli, ops

ROWS = [("Pin the clock in the flaky test", "agent", "2026-10-20", "XL", "ten green runs in a row"),
        ("Write the fixture owner list", "owner", "2026-10-30", "2h", "the list names one owner per fixture"),
        ("Close the probe gap", "reviewer", "2026-11-06", "3d", "the probe covers the close path")]


def _sealed(project, monkeypatch, rows=ROWS, *more):
    conn = setup(project, monkeypatch)
    author(conn, project, OP, [row(a, o, d, s, w) for a, o, d, s, w in rows])
    before = op_ids(conn)
    code, out = seal(OP, *more)
    assert code == cli.PASS, out
    return conn, before, out


def test_seal_files_one_mapped_task_per_action_item(project, monkeypatch):
    conn, before, out = _sealed(project, monkeypatch)
    got = filed(conn, OP)
    assert len(got) == len(ROWS), "3 action items filed %d task(s): %s" % (len(got), got)
    new = standing_ops(conn, before)
    assert len(new) == 1, "the default target is one standing op opened once; new ops: %s" % new
    standing = new[0]
    assert STANDING in op_words(conn, standing), "the standing op is not named After-action items"
    assert {o for _t, o in got} == {standing}
    assert "aar-%s" % OP in groups(conn, standing)
    for tid, _o in got:
        e = entry(conn, tid)
        assert e["after"] == [] and e["resources"] == [], e
        t = task(conn, tid)
        assert t["status"] == "open", t
        assert t["claimant"] is None, "the owner cell became a claim lease on %s" % tid
        assert t["title"] and len(t["title"]) <= ops.TITLE_MAX, t["title"]
    assert standing in out, "the seal output does not name the op the items went to:\n" + out


def test_each_task_contract_holds_owner_due_size_and_done_when_as_derived_lines(project, monkeypatch):
    conn, _before, _out = _sealed(project, monkeypatch)
    for action, owner, due, size, done in ROWS:
        tid = by_action(conn, OP, action)
        c = contract(conn, tid)
        assert c, "%s has no task contract" % tid
        lines = contract_lines(c)
        assert any(done in text for part, text, _o in lines if part == "done_bar"), (tid, c)
        body = " ".join(text for _p, text, _o in lines)
        for value in (owner, due, size):
            assert value in body, "%s contract lines do not hold %r: %s" % (tid, value, lines)
        for part, text, origin in lines:
            assert origin.get("kind") == "derived" and origin.get("reason") == "aar:%s" % OP, \
                "%s %s line %r has origin %s, not derived=aar:%s" % (tid, part, text, origin, OP)


def test_into_files_in_that_op_and_opens_no_standing_op(project, monkeypatch):
    conn, before, out = _sealed(project, monkeypatch, ROWS, "--into", OTHER)
    got = filed(conn, OP)
    assert len(got) == len(ROWS)
    assert {o for _t, o in got} == {OTHER}
    assert standing_ops(conn, before) == [], "--into still opened a standing op"
    assert "aar-%s" % OP in groups(conn, OTHER)


@pytest.mark.parametrize("action,want", [
    ("Gate: fix the flaky probe before the release", "fix"),
    (": fix it now and say so", "fix"),
    ("Rework " + "the slow gate " * 30, "Rework"),
])
def test_titles_are_short_and_say_the_action(project, monkeypatch, action, want):
    conn, _before, _out = _sealed(project, monkeypatch, [(action, "agent", "2026-10-20", "S", "it is done")])
    tid = by_action(conn, OP, action[:40].strip())
    title = task(conn, tid)["title"]
    assert title and len(title) <= ops.TITLE_MAX, repr(title)
    assert want in title, "title %r lost the action (cut at a colon or emptied)" % title
    assert action.strip()[:40] in (task(conn, tid)["statement"] or ""), "the statement lost the action text"


def test_markdown_emphasis_in_cells_is_not_stored_as_the_due(project, monkeypatch):
    rows = [("Bold due item", "*agent*", "**2026-10-21**", "S", "it is done")]
    conn, _before, _out = _sealed(project, monkeypatch, rows)
    tid = by_action(conn, OP, "Bold due item")
    body = " ".join(text for _p, text, _o in contract_lines(contract(conn, tid)))
    assert "2026-10-21" in body
    assert "**2026-10-21**" not in body, "the due was stored with its markdown stars: %s" % body
