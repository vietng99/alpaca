"""(task text "any new section or line form you add must be a named kind in the classifier, never a
free-text regex"; cause C8.1-C8.3) '## Carried over' is legal only as the rows aar new writes:
one-line items, each ending in (event:N) where N is the task-add of an item filed from ANOTHER
op's sealed AAR, and naming that task. Today the only place that shapes an unknown section is the
"not a section of this report" branch of _judge; making the heading legal must not make its body
free text. Each hand edit below must fail `aar check` (with the line number where one applies);
the unedited filled report must pass."""
import re

import pytest

from alpaca.tests.aar_items_kit import (CARRIED, OP, OTHER, author, check, draft, event_id, ids, read, report, row,
                     seal, setup, task_add_id, write)
from alpaca import cli


@pytest.fixture
def base(project, monkeypatch):
    conn = setup(project, monkeypatch)
    author(conn, project, OP, [row("Alpha carried item"), row("Bravo carried item")])
    assert seal(OP)[0] == cli.PASS
    draft(project, OTHER)
    author(conn, project, OTHER, [row("Fix the op-002 gap")])
    text = read(report(project, OTHER))
    code, out = check(OTHER)
    assert code == cli.PASS, "baseline: the generated carried rows must pass:\n" + out
    lines = text.split("\n")
    at = lines.index(CARRIED)
    rows = []
    for i in range(at + 1, len(lines)):
        if lines[i].startswith("## "):
            break
        if lines[i].startswith("- "):
            rows.append(i)
    assert len(rows) == 2, "baseline: two carried rows expected, got %d" % len(rows)
    return {"project": project, "conn": conn, "lines": lines, "at": at, "rows": rows}


def _edit(b, fn):
    """Apply fn(lines, base) -> (new lines, 1-based line to name or None); write; check."""
    lines, named = fn(list(b["lines"]), b)
    write(report(b["project"], OTHER), "\n".join(lines))
    code, out = check(OTHER)
    return code, out, named


def _cite(line, n):
    return re.sub(r"\(event:\d+\)\s*$", "(event:%d)" % n, line)


def _first_row(fn):
    def apply(lines, b):
        i = b["rows"][0]
        lines[i] = fn(lines[i], b)
        return lines, i + 1
    return apply


def _insert_after_first(extra):
    def apply(lines, b):
        i = b["rows"][0]
        lines.insert(i + 1, extra)
        return lines, i + 2
    return apply


def _insert_before_rows(extra):
    def apply(lines, b):
        i = b["rows"][0]
        lines.insert(i, extra)
        lines.insert(i + 1, "")
        return lines, i + 1
    return apply


EDITS = {
    "a paragraph": _insert_before_rows("Some prose about the carried items."),
    "a row with no event": _first_row(lambda l, b: re.sub(r"\s*\(event:\d+\)\s*$", "", l)),
    "a row citing the op-open event": _first_row(
        lambda l, b: _cite(l, event_id(b["conn"], "op-open", op=OTHER))),
    "a row citing a plain task": _first_row(lambda l, b: _cite(l, task_add_id(b["conn"], "t-002"))),
    "a row citing no event at all": _first_row(lambda l, b: _cite(l, 999999)),
    "a row naming another task than its event": _first_row(
        lambda l, b: l.replace(ids(b["conn"], OP)[0], "t-999", 1)),
    "a continuation line": _insert_after_first("  and a second line under the row"),
    "a nested item": _insert_after_first("  - a nested item"),
    "an underline": _insert_after_first("==="),
    "a sub-heading": _insert_before_rows("### More items"),
    "raw html": _first_row(lambda l, b: l.replace("- ", "- <b>bold</b> ", 1)),
}


@pytest.mark.parametrize("name", sorted(EDITS))
def test_a_hand_edit_of_the_carried_section_fails_check(base, name):
    code, out, named = _edit(base, EDITS[name])
    assert code == cli.FAIL, "%s under '## Carried over' passed check:\n%s" % (name, out)
    if named:
        assert re.search(r"\bline %d\b" % named, out), "the problem does not name line %d:\n%s" % (named, out)


def test_the_section_twice_fails_check(base):
    def twice(lines, b):
        return lines + ["", CARRIED, "", lines[b["rows"][0]], ""], None
    code, out, _n = _edit(base, twice)
    assert code == cli.FAIL, out


def test_a_heading_in_another_case_is_not_the_section(base):
    def lower(lines, b):
        lines[b["at"]] = "## carried over"
        return lines, None
    code, out, _n = _edit(base, lower)
    assert code == cli.FAIL, out
