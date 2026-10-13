"""(cause C2) Seal never reads the seal state, so an edited report that is valid again re-seals
(test_aar.py test_reseal_after_an_edit_clears_the_change). Filing runs on every re-seal and must
decide, per row, whether it is already filed. The key must not be the line number (it moves with
any edit above the table) and must not merge two rows that say the same thing. An edit the
filing does not apply must be said, not dropped in silence."""
from alpaca.tests.aar_items_kit import (OP, OTHER, author, check, event_id, filed, finish, ids, row, seal, setup, snapshot,
                     task)
from alpaca import cli

TWO = [row("First action item", due="2026-10-20"), row("Second action item", due="2026-10-22")]


def _sealed(project, monkeypatch, rows=TWO, *more):
    conn = setup(project, monkeypatch)
    author(conn, project, OP, rows)
    code, out = seal(OP, *more)
    assert code == cli.PASS, out
    return conn


def test_reseal_unchanged_files_nothing_more(project, monkeypatch):
    conn = _sealed(project, monkeypatch)
    first = ids(conn, OP)
    assert len(first) == 2, "the first seal filed %d task(s)" % len(first)
    base = snapshot(conn)
    code, out = seal(OP)
    assert code == cli.PASS, out
    assert ids(conn, OP) == first
    assert snapshot(conn)["tasks"] == base["tasks"], "an unchanged re-seal filed duplicates"


def test_reseal_after_a_line_shift_files_nothing_more(project, monkeypatch):
    """Lines above the table move every row's line number; the items are the same items."""
    conn = _sealed(project, monkeypatch)
    first = ids(conn, OP)
    assert len(first) == 2, "the first seal filed %d task(s)" % len(first)
    p = "event:%d" % event_id(conn, "op-open", op=OP)
    author(conn, project, OP, TWO, over={
        "What went well": "- The done bar was named up front (%s)\n- A new line here (%s)\n"
                          "- And one more (%s)" % (p, p, p)})
    code, out = seal(OP)
    assert code == cli.PASS, out
    assert ids(conn, OP) == first, "moving the rows down a few lines filed them again"


def test_reseal_with_an_added_row_files_only_that_row(project, monkeypatch):
    conn = _sealed(project, monkeypatch)
    first = ids(conn, OP)
    author(conn, project, OP, TWO + [row("Third action item")])
    code, out = seal(OP)
    assert code == cli.PASS, out
    now = ids(conn, OP)
    assert set(first) < set(now) and len(now) == 3, (first, now)


def test_two_rows_with_the_same_action_text_are_two_items_or_a_check_problem(project, monkeypatch):
    conn = setup(project, monkeypatch)
    rows = [row("Pin the clock", owner="agent", due="2026-10-20"),
            row("Pin the clock", owner="reviewer", due="2026-11-20")]
    author(conn, project, OP, rows)
    ccode, cout = check(OP)
    scode, sout = seal(OP)
    if ccode == cli.PASS:
        assert scode == cli.PASS, "check said PASS and seal refused:\n" + sout
        assert len(ids(conn, OP)) == 2, "two action rows filed %d task(s)" % len(ids(conn, OP))
    else:
        assert scode == cli.FAIL and ids(conn, OP) == [], "check refused the duplicate but seal filed it"


def test_an_edited_due_on_reseal_is_filed_or_said(project, monkeypatch):
    conn = _sealed(project, monkeypatch)
    first = ids(conn, OP)
    author(conn, project, OP, [row("First action item", due="2026-12-24"), TWO[1]])
    code, out = seal(OP)
    assert code == cli.PASS, out
    now = ids(conn, OP)
    if now == first:
        kept = [t for t in first if "First action item" in (task(conn, t)["statement"] or "")]
        assert kept and kept[0] in out, (
            "the due of an already filed item changed and the seal said nothing about %s:\n%s"
            % (kept, out))
    else:
        assert len(now) == 3, now


def test_reseal_with_another_into_moves_and_duplicates_nothing(project, monkeypatch):
    conn = _sealed(project, monkeypatch)
    first = filed(conn, OP)
    code, out = seal(OP, "--into", OTHER)
    assert code == cli.PASS, out
    assert filed(conn, OP) == first, "a re-seal with another --into filed or moved items: %s" % filed(conn, OP)


def test_a_done_item_is_not_filed_again(project, monkeypatch):
    conn = _sealed(project, monkeypatch)
    first = ids(conn, OP)
    finish(project, first[0])
    code, out = seal(OP)
    assert code == cli.PASS, out
    assert ids(conn, OP) == first
