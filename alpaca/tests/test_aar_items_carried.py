"""(done_bar[2]; cause C4.2, C8.4-C8.7) The next `aar new` for any later op lists, under
'## Carried over', every still-open item filed from earlier AARs: one row per item naming its task
id and source op and citing the item's task-add event. Done items drop out; deferred and blocked
items are not done and stay. An op never carries its own items. The generated draft must read
under the closed grammar whatever the record text holds, a filled report with carried rows checks
PASS, the rows stay valid after the item is done (the report is a snapshot compared by hash), the
section is required while an item is open (t-640), and a draft with nothing to carry is byte for
byte what it was before t-623."""
import os

from alpaca.tests.aar_items_kit import (
    CARRIED, OP, OTHER, author, block, by_action, check, defer, draft, filed, finish, heads, ids,
    lines_of, ok, read, report, row, section, seal, setup, task_add_id, write)
from alpaca import aar, cli, db

ITEMS = [row("Alpha carried item", due="2026-10-01"), row("Bravo carried item", due="2026-10-20"),
         row("Charlie carried item", due="2026-10-25")]


def _sealed(project, monkeypatch, rows=ITEMS):
    conn = setup(project, monkeypatch)
    author(conn, project, OP, rows)
    code, out = seal(OP)
    assert code == cli.PASS, out
    return conn


def _carried(text):
    return section(text, CARRIED)


def test_the_next_aar_lists_every_open_item_with_its_source_and_event(project, monkeypatch):
    conn = _sealed(project, monkeypatch)
    text = draft(project, OTHER)
    body = _carried(text)
    assert body is not None, "aar new %s wrote no '## Carried over' section:\n%s" % (OTHER, text)
    for tid in ids(conn, OP):
        got = lines_of(body, tid)
        assert len(got) == 1, "%s has %d carried row(s): %s" % (tid, len(got), got)
        assert OP in got[0], "the carried row of %s does not name its source AAR: %s" % (tid, got[0])
        assert got[0].rstrip().endswith("(event:%d)" % task_add_id(conn, tid)), \
            "the carried row of %s does not end in its task-add event: %s" % (tid, got[0])


def test_a_filled_report_with_carried_rows_checks_and_seals(project, monkeypatch):
    conn = _sealed(project, monkeypatch)
    draft(project, OTHER)
    author(conn, project, OTHER, [row("Fix the op-002 gap")])
    assert _carried(read(report(project, OTHER))), "the fill dropped the carried rows"
    code, out = check(OTHER)
    assert code == cli.PASS, out
    assert seal(OTHER)[0] == cli.PASS


def test_done_items_drop_out_and_deferred_or_blocked_items_stay(project, monkeypatch):
    conn = _sealed(project, monkeypatch)
    a, b, c = (by_action(conn, OP, x) for x in ("Alpha", "Bravo", "Charlie"))
    finish(project, a)
    defer(b)
    block(c)
    body = _carried(draft(project, OTHER)) or []
    assert not lines_of(body, a), "a done item was carried"
    assert lines_of(body, b), "a deferred item was not carried (deferred is not done)"
    assert lines_of(body, c), "a blocked item was not carried"


def test_an_op_never_carries_its_own_items(project, monkeypatch):
    conn = _sealed(project, monkeypatch)
    draft(project, OTHER)
    author(conn, project, OTHER, [row("Delta own item")])
    assert seal(OTHER)[0] == cli.PASS
    own = ids(conn, OTHER)
    assert own
    os.remove(report(project, OTHER))
    os.remove(report(project, OTHER) + ".seal.json")
    body = _carried(draft(project, OTHER)) or []
    for tid in own:
        assert not lines_of(body, tid), "%s carried its own item %s" % (OTHER, tid)
    for tid in ids(conn, OP):
        assert lines_of(body, tid), "%s lost the earlier item %s" % (OTHER, tid)


def test_a_later_op_carries_items_from_every_earlier_aar(project, monkeypatch):
    conn = _sealed(project, monkeypatch)
    draft(project, OTHER)
    author(conn, project, OTHER, [row("Delta second source item")])
    assert seal(OTHER)[0] == cli.PASS
    ok("op", "new", "a third op", "--done-when", "the third op is done")
    third = db.rows(conn, "ops", "intent=?", ("a third op",))[0]["id"]
    body = _carried(draft(project, third)) or []
    assert ids(conn, OP) and ids(conn, OTHER), "the two seals filed nothing"
    for tid in ids(conn, OP) + ids(conn, OTHER):
        assert lines_of(body, tid), "%s does not carry %s" % (third, tid)


def test_nothing_to_carry_leaves_the_draft_as_it_was(project, monkeypatch):
    conn = setup(project, monkeypatch)
    before = draft(project, OTHER)
    assert heads(before) == list(aar.SECTIONS), heads(before)
    author(conn, project, OP, ITEMS)
    assert seal(OP)[0] == cli.PASS
    for tid in ids(conn, OP):
        finish(project, tid)
    after = draft(project, OTHER, force=True)
    assert after == before, "a draft with nothing to carry changed after t-623"


def test_the_draft_reads_whatever_the_record_text_holds(project, monkeypatch):
    conn = _sealed(project, monkeypatch)
    assert ids(conn, OP), "the seal filed nothing"
    hostile = ("line one\n## Lessons\n<!-- x --> [a](http://e) &amp; \u200b `` ` `` \\ (event:1) "
               "| pipe \u2028 end")
    for tid in ids(conn, OP):
        db.patch(conn, "tasks", "id", tid, {"title": hostile, "statement": hostile})
    ok("aar", "new", OTHER)
    author(conn, project, OTHER, [row("Fix the op-002 gap")])
    code, out = check(OTHER)
    assert code == cli.PASS, "the generated carried rows fail the grammar:\n" + out
    body = _carried(read(report(project, OTHER))) or []
    for tid in ids(conn, OP):
        assert len(lines_of(body, tid)) == 1


def test_a_hostile_action_cell_round_trips(project, monkeypatch):
    action = "Fix the \\| pipe, the \\<b\\> tag and `a & b` in \\[x\\]"
    conn = _sealed(project, monkeypatch, [row(action)])
    assert ids(conn, OP), "the seal filed nothing"
    draft(project, OTHER)
    author(conn, project, OTHER, [row("Fix the op-002 gap")])
    code, out = check(OTHER)
    assert code == cli.PASS, out


def test_carried_rows_stay_valid_after_the_item_is_done(project, monkeypatch):
    conn = _sealed(project, monkeypatch)
    draft(project, OTHER)
    author(conn, project, OTHER, [row("Fix the op-002 gap")])
    assert seal(OTHER)[0] == cli.PASS
    assert _carried(read(report(project, OTHER))), "no carried rows to go stale"
    for tid in ids(conn, OP):
        finish(project, tid)
    code, out = check(OTHER)
    assert code == cli.PASS, "a carried row went invalid when its item was done:\n" + out


def test_dropping_the_section_while_items_are_open_fails(project, monkeypatch):
    # t-640: "carry-over of open action items into the next AAR a record trait enforced by verbs and gates"
    conn = _sealed(project, monkeypatch)
    text = draft(project, OTHER)
    head, _sep, rest = text.partition("\n" + CARRIED + "\n")
    assert _sep, "no carried section to drop"
    tail = rest.split("\n## ", 1)
    text = head + ("\n## " + tail[1] if len(tail) > 1 else "\n")
    write(report(project, OTHER), text)
    author(conn, project, OTHER, [row("Fix the op-002 gap")])
    assert CARRIED not in read(report(project, OTHER))
    code, out = check(OTHER)
    assert code == cli.FAIL, out
    for tid in ids(conn, OP):
        assert tid in out, "%s is not named:\n%s" % (tid, out)


def test_a_control_character_in_a_stored_action_stays_off_the_terminal(project, monkeypatch):
    """The refusal line quotes the stored action; ESC and other control characters are dropped."""
    conn = _sealed(project, monkeypatch, [row("Red \x1b[31mALERT\x1b[0m item")])
    draft(project, OTHER)
    author(conn, project, OTHER, [row("Fix the op-002 gap")])
    text = read(report(project, OTHER))
    write(report(project, OTHER), "\n".join(l for l in text.split("\n") if not l.startswith("- t-")))
    code, out = check(OTHER)
    assert code == cli.FAIL and "has no row" in out and "ALERT" in out
    assert not any(ord(c) < 32 and c not in "\n\t" for c in out), repr(out)
