"""(t-640; done_bar[0] and done_bar[1]) "carry-over of open action items into the next AAR" is a
record trait enforced by verbs and gates. While an action item filed by another op's report is not
done, `aar check` of an unsealed or edited report, `aar review` and `aar seal` refuse a report that
leaves out '## Carried over' or drops one row, and name the row to add. With nothing to carry the
section stays absent. The check of the sealed bytes does not apply the rule."""
import re

from alpaca.tests.aar_items_kit import (CARRIED, OP, OTHER, author, block, check, defer, draft, filed, finish,
                     ids, ok, read, report, reviewed, row, run, seal, section, setup, task_add_id, write)
from alpaca import aar, aaritems, cli, db

ITEMS = [row("Alpha carried item"), row("Bravo carried item"), row("Charlie carried item")]
MISSING = "section 'Carried over' is missing"


def _sealed(project, monkeypatch, rows=ITEMS):
    conn = setup(project, monkeypatch)
    author(conn, project, OP, rows)
    assert seal(OP)[0] == cli.PASS
    return conn


def _edit(project, op, fn):
    write(report(project, op), fn(read(report(project, op))))


def _cut(text):
    """The report without its Carried over section."""
    head, sep, rest = text.partition("\n" + CARRIED + "\n")
    assert sep, "no carried section to drop"
    tail = rest.split("\n## ", 1)
    return head + ("\n## " + tail[1] if len(tail) > 1 else "\n")


def _rows(text):
    return [l for l in (section(text, CARRIED) or []) if l.startswith("- ")]


def _drafted(project, monkeypatch, rows=ITEMS):
    conn = _sealed(project, monkeypatch, rows)
    draft(project, OTHER)
    author(conn, project, OTHER, [row("Fix the op-002 gap")])
    assert len(_rows(read(report(project, OTHER)))) == len(ids(conn, OP))
    return conn


def _no_seal(conn, op):
    return aar.latest_seal(conn, op) is None


# ------------------------------------------------------------------ done_bar[0]
def test_dropping_the_section_while_items_are_open_fails_check_and_seal(project, monkeypatch):
    conn = _drafted(project, monkeypatch)
    _edit(project, OTHER, _cut)
    code, out = check(OTHER)
    assert code == cli.FAIL, out
    assert MISSING in out
    for tid in ids(conn, OP):
        assert tid in out
    code, sout = seal(OTHER)
    assert code != cli.PASS and MISSING in sout, sout
    assert _no_seal(conn, OTHER)


def test_dropping_one_row_fails_naming_that_task(project, monkeypatch):
    conn = _drafted(project, monkeypatch)
    gone = ids(conn, OP)[1]
    _edit(project, OTHER, lambda t: "\n".join(l for l in t.split("\n") if not (l.startswith("- ") and gone in l)))
    code, out = check(OTHER)
    assert code == cli.FAIL, out
    lines = [l for l in out.splitlines() if "has no row" in l]
    assert len(lines) == 1 and gone in lines[0], out
    assert "(event:%d)" % task_add_id(conn, gone) in lines[0]
    assert seal(OTHER)[0] != cli.PASS and _no_seal(conn, OTHER)


def test_an_empty_section_reports_each_open_item(project, monkeypatch):
    conn = _drafted(project, monkeypatch)
    carried = set(_rows(read(report(project, OTHER))))
    _edit(project, OTHER, lambda t: "\n".join(l for l in t.split("\n") if l not in carried))
    text = read(report(project, OTHER))
    assert not _rows(text)
    write(report(project, OTHER), text.replace("## %s\n" % CARRIED, "## %s\n\nnothing here\n" % CARRIED, 1))
    code, out = check(OTHER)
    assert code == cli.FAIL, out
    assert len([l for l in out.splitlines() if "has no row" in l]) == len(ids(conn, OP)), out


def test_a_fenced_or_commented_copy_of_a_row_does_not_count(project, monkeypatch):
    conn = _drafted(project, monkeypatch)
    victim = ids(conn, OP)[0]

    def hide(text, wrap):
        out = []
        for l in text.split("\n"):
            out += wrap(l) if l.startswith("- ") and victim in l else [l]
        return "\n".join(out)

    for wrap in (lambda l: ["```", l, "```"], lambda l: ["<!-- " + l + " -->"]):
        _edit(project, OTHER, lambda t: hide(t, wrap))
        code, out = check(OTHER)
        assert code == cli.FAIL, out
        assert any("has no row" in l and victim in l for l in out.splitlines()), out
        draft(project, OTHER, force=True)
        author(conn, project, OTHER, [row("Fix the op-002 gap")])


def test_a_row_naming_the_wrong_task_does_not_count(project, monkeypatch):
    conn = _drafted(project, monkeypatch)
    a, b = ids(conn, OP)[:2]
    _edit(project, OTHER, lambda t: "\n".join(l.replace(a, "t-999", 1) if l.startswith("- ") and l.startswith("- " + a)
                                              else l for l in t.split("\n")))
    code, out = check(OTHER)
    assert code == cli.FAIL, out
    assert "names" in out and "has no row" in out and a in out


def test_a_heading_in_another_case_is_named(project, monkeypatch):
    _drafted(project, monkeypatch)
    _edit(project, OTHER, lambda t: t.replace("## Carried over", "## carried over"))
    code, out = check(OTHER)
    assert code == cli.FAIL, out
    assert "found 'carried over'" in out


def test_an_item_filed_after_review_blocks_the_seal(project, monkeypatch):
    conn = _drafted(project, monkeypatch)
    assert reviewed(OTHER)[0] == cli.PASS
    ok("op", "new", "a third op", "--done-when", "the third op is done")
    third = db.rows(conn, "ops", "intent=?", ("a third op",))[0]["id"]
    author(conn, project, third, [row("Delta late item")])
    assert seal(third)[0] == cli.PASS
    late = ids(conn, third)[0]
    code, out = run("aar", "seal", OTHER)
    assert code != cli.PASS and late in out and "has no row; add: - %s from the %s AAR" % (late, third) in out, out
    assert _no_seal(conn, OTHER)


def test_a_deferred_or_blocked_item_is_still_required(project, monkeypatch):
    conn = _drafted(project, monkeypatch)
    a, b, c = ids(conn, OP)
    finish(project, a)
    defer(b)
    block(c)
    _edit(project, OTHER, _cut)
    code, out = check(OTHER)
    assert code == cli.FAIL and b in out and c in out and "item %s " % a not in out, out


def test_check_and_seal_agree(project, monkeypatch):
    conn = _drafted(project, monkeypatch)
    gone = ids(conn, OP)[2]
    for edit in (_cut, lambda t: "\n".join(l for l in t.split("\n") if not (l.startswith("- ") and gone in l))):
        draft(project, OTHER, force=True)
        author(conn, project, OTHER, [row("Fix the op-002 gap")])
        _edit(project, OTHER, edit)
        lines = [l for l in check(OTHER)[1].splitlines() if "carried item" in l or MISSING in l]
        assert lines
        sealed = seal(OTHER)[1]
        for l in lines:
            assert l.strip() in sealed, "%s\nnot in\n%s" % (l, sealed)


def test_the_seal_rechecks_inside_its_transaction(project, monkeypatch):
    conn = _sealed(project, monkeypatch)
    draft(project, OTHER)
    author(conn, project, OTHER, [row("Fix the op-002 gap")])
    _edit(project, OTHER, _cut)
    real = aaritems.carried
    monkeypatch.setattr(aaritems, "carried", lambda c, o: [])
    assert reviewed(OTHER)[0] == cli.PASS
    judge = aar._judge

    def wrapped(*a, **k):
        out = judge(*a, **k)
        monkeypatch.setattr(aaritems, "carried", real)
        return out

    monkeypatch.setattr(aar, "_judge", wrapped)
    code, out = run("aar", "seal", OTHER)
    assert code != cli.PASS and "has no row" in out, out
    assert _no_seal(conn, OTHER)
    assert not filed(conn, OTHER)


# ------------------------------------------------------------------ done_bar[1]
def test_nothing_to_carry_passes_without_the_section(project, monkeypatch):
    conn = setup(project, monkeypatch)
    draft(project, OTHER)
    author(conn, project, OTHER, [row("Fix the op-002 gap")])
    assert CARRIED not in read(report(project, OTHER))
    assert check(OTHER)[0] == cli.PASS
    assert seal(OTHER)[0] == cli.PASS


def test_all_items_done_passes_without_the_section(project, monkeypatch):
    conn = _sealed(project, monkeypatch)
    for tid in ids(conn, OP):
        finish(project, tid)
    draft(project, OTHER)
    author(conn, project, OTHER, [row("Fix the op-002 gap")])
    assert CARRIED not in read(report(project, OTHER))
    assert check(OTHER)[0] == cli.PASS
    assert seal(OTHER)[0] == cli.PASS


def test_a_sealed_report_keeps_its_check(project, monkeypatch):
    conn = setup(project, monkeypatch)
    draft(project, OTHER)
    author(conn, project, OTHER, [row("Fix the op-002 gap")])
    assert seal(OTHER)[0] == cli.PASS
    before = check(OTHER)
    assert before[0] == cli.PASS
    author(conn, project, OP, ITEMS)
    assert seal(OP)[0] == cli.PASS
    assert ids(conn, OP)
    assert check(OTHER) == before


def test_a_sealed_report_with_its_rows_stays_valid_after_new_items(project, monkeypatch):
    conn = _drafted(project, monkeypatch)
    assert seal(OTHER)[0] == cli.PASS
    ok("op", "new", "a third op", "--done-when", "the third op is done")
    third = db.rows(conn, "ops", "intent=?", ("a third op",))[0]["id"]
    author(conn, project, third, [row("Delta late item")])
    assert seal(third)[0] == cli.PASS
    assert check(OTHER)[0] == cli.PASS


def test_an_edited_sealed_report_names_the_missing_rows(project, monkeypatch):
    conn = _drafted(project, monkeypatch)
    assert seal(OTHER)[0] == cli.PASS
    gone = ids(conn, OP)[0]
    _edit(project, OTHER, lambda t: "\n".join(l for l in t.split("\n") if not (l.startswith("- ") and gone in l)))
    code, out = check(OTHER)
    assert code == cli.FAIL and "changed since it was sealed" in out, out
    assert "carried item %s " % gone in out and "has no row" in out
    code, sout = seal(OTHER)
    assert code != cli.PASS and "carried item %s " % gone in sout, sout
