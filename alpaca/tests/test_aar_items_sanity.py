"""(sanity) The kit itself works on the tree before t-623: the fixture closes op-001, a filled
report checks and seals, the two-op flow drafts op-002, and the readers the attacks use answer.
These must PASS before and after the build; a failure here means an attack below is misbuilt."""
from alpaca.tests.aar_items_kit import (NOW, OP, OTHER, author, check, draft, heads, op_ids, overdue, resume, row,
                     seal, section, setup, snapshot)
from alpaca import aar, cli


def test_a_filled_report_checks_and_seals(project, monkeypatch):
    conn = setup(project, monkeypatch)
    text = author(conn, project, OP, [row("Pin the clock"), row("Name a fixture owner")])
    assert check(OP)[0] == cli.PASS
    assert len(aar.parse_actions(text)) == 2
    code, out = seal(OP)
    assert code == cli.PASS, out
    assert aar.latest_seal(conn, OP) is not None


def test_the_open_second_op_can_be_drafted_and_checked(project, monkeypatch):
    conn = setup(project, monkeypatch)
    d = draft(project, OTHER)
    assert heads(d)[:len(aar.SECTIONS)] == list(aar.SECTIONS)
    author(conn, project, OTHER, [row("Write the op-002 note")])
    code, out = check(OTHER)
    assert code == cli.PASS, out


def test_readers_answer_on_the_fixture(project, monkeypatch):
    conn = setup(project, monkeypatch)
    assert op_ids(conn) == ["op-001", "op-002"]
    snap = snapshot(conn)
    assert snap["tasks"] == 2
    text = resume(project)
    assert section(text, "## Not now") is not None
    assert overdue("## Overdue AAR items\n\n- t-001 x\n## Next\n") == ["", "- t-001 x"]
    assert NOW[:10] == "2026-10-13"
