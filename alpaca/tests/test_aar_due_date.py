"""A07 (done_bar[3]; cause C6, door mismatch 4): an action item's due must be a date YYYY-MM-DD.
`aar check` refuses any other due, and `aar seal` agrees with it (it runs the same check).

Ways this still fails after it lands:
  * only the regex changes and the week branch still accepts 2026-W43, or `aar._due` keeps an op /
    task / version branch
  * a shape-only check lets 2026-02-30 or a date with a time, slashes or no dashes through
  * check and seal disagree on a due
  * aaritems.overdue, which reads aar._due for already filed items, now raises (or breaks the status
    page) on an old item whose contract due is a week, an op, a task or a version
  * the refusal does not say which form it wants
  * the door's quoted line stays in aaritems.py"""
import pathlib

import pytest

from alpaca import aar, aaritems, cli, taskcontract
from alpaca.tests import aar_items_kit as items
from alpaca.tests.test_aar import (HEAD, OP, SEP, _no_review_gate, action_row, put, run,  # noqa: F401
                                   st)

REFUSED = ["2026-W43", "2026-W01", "t-623", "op-058", "1.3.0", "v1.3", "2026-10-13T00:00",
           "2026-10-13 10:00", "20261013", "2026/10/13", "13-10-2026", "2026-1-3", "2026-02-30",
           "2026-13-01", "\u0662\u0660\u0662\u0666-10-13", "+2026-10-13", "2026-10-13Z", "next week"]
ACCEPTED = ["2026-10-20", "**2026-10-20**", "2028-02-29"]


@pytest.mark.parametrize("due", REFUSED)
def test_check_refuses_a_due_that_is_not_a_date(st, capsys, due):
    put(st, {"Action items": "\n".join([HEAD, SEP, action_row(due=due)])})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL, (due, out)
    assert "due" in out.lower(), out


@pytest.mark.parametrize("due", ACCEPTED)
def test_check_passes_a_real_date(st, capsys, due):
    put(st, {"Action items": "\n".join([HEAD, SEP, action_row(due=due)])})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.PASS, (due, out)


def test_the_refusal_names_the_date_form(st, capsys):
    put(st, {"Action items": "\n".join([HEAD, SEP, action_row(due="2026-W43")])})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "YYYY-MM-DD" in out, out


@pytest.mark.parametrize("due", ["2026-W43", "op-058", "1.3.0", "2026-10-20"])
def test_check_and_seal_agree(st, capsys, due):
    put(st, {"Action items": "\n".join([HEAD, SEP, action_row(due=due)])})
    ccode, cout = run(capsys, "aar", "check", OP)
    scode, sout = run(capsys, "aar", "seal", OP)
    assert (ccode == cli.PASS) == (scode == cli.PASS), (due, cout, sout)


def test_the_due_reader_is_date_only():
    for due in ("2026-W43", "op-058", "t-623", "1.3.0", "v1.3"):
        assert not aar._due(due), due
    assert aar._due("2026-10-20") and aar._due("**2026-10-20**")


@pytest.mark.parametrize("old", ["2026-W40", "op-099", "t-099", "v1.2", "2025-W53"])
def test_an_item_filed_before_with_a_non_date_due_does_not_break_overdue(project, monkeypatch, old):
    """Contracts filed before t-629 may hold such a due. The reader may show the row as not
    readable or skip it; it must not raise, and status must still render."""
    conn = items.setup(project, monkeypatch)
    items.author(conn, project, items.OP, [items.row("Alpha filed earlier", due="2026-10-01")])
    code, out = items.seal(items.OP)
    assert code == cli.PASS, out
    tid = items.by_action(conn, items.OP, "Alpha filed earlier")
    item = {"owner": "agent", "due": old, "size": "S", "done_when": "ten green runs"}
    taskcontract.record(conn, tid, aaritems.contract_of(items.OP, ".alpaca/aar/op-001.md", 5, item),
                        session="s", actor="agent", root=project)
    rows = aaritems.overdue(conn)
    assert isinstance(rows, list)
    code, out = items.run("status")
    assert code == 0, out


def test_the_door_quote_is_gone():
    src = pathlib.Path(aaritems.__file__).read_text()
    assert "An op id, task id or version due is never late." not in src
