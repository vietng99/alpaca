"""(cause C3) `aar check` runs only the grammar; filing has limits the grammar does not know. Every
gap is a report that checks green and then fails to seal, which reads as a flaky gate. For each
limit below, check and seal must agree, and a seal that passes must have filed every item (cut
where it had to, never dropped). Limits: a contract line holds 1 to 600 characters
(taskcontract.MAX_TEXT); a week 53 in a year that has none (aar._due accepts it, fromisocalendar
raises); a title holds at most ops.TITLE_MAX characters."""
import pytest

from alpaca.tests.aar_items_kit import OP, author, check, contract, contract_lines, ids, row, seal, setup, task
from alpaca import cli, ops, taskcontract

LONG = "the probe covers " + "the close path and the reopen path " * 20
CASES = {
    "a 700-character done-when": [row("First item"), row("Second item", done=LONG[:700]), row("Third item")],
    "a 700-character action": [row("First item"), row("Rework " + LONG[:693]), row("Third item")],
    "a 700-character owner": [row("First item"), row("Second item", owner="agent " * 117), row("Third item")],
    "a week 53 in a year without one": [row("First item"), row("Second item", due="2025-W53"),
                                         row("Third item")],
    "twenty-five items": [row("Item number %02d" % i) for i in range(25)],
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_check_and_seal_agree_and_a_seal_files_every_item(project, monkeypatch, name):
    conn = setup(project, monkeypatch)
    rows = CASES[name]
    author(conn, project, OP, rows)
    ccode, cout = check(OP)
    scode, sout = seal(OP)
    assert (ccode == cli.PASS) == (scode == cli.PASS), \
        "%s: check %s, seal %s\ncheck: %s\nseal: %s" % (name, ccode, scode, cout, sout)
    if scode != cli.PASS:
        assert ids(conn, OP) == [], "a refused seal left filed tasks"
        return
    got = ids(conn, OP)
    assert len(got) == len(rows), "%s: %d rows filed %d task(s)" % (name, len(rows), len(got))
    for tid in got:
        title = task(conn, tid)["title"]
        assert title and len(title) <= ops.TITLE_MAX, repr(title)
        for part, text, _origin in contract_lines(contract(conn, tid)):
            assert 1 <= len(text) <= taskcontract.MAX_TEXT, (part, len(text))
