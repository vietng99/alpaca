"""(done_bar[1]; cause C4.1, C4.3, C7) An item past due shows in `alpaca status` and RESUME.md
under the Overdue AAR items heading. Overdue = due before today by the record clock (the newest
event's date, as ops.not_now reads it), task not done. A date and an ISO week (due at its Sunday)
can be late; an op id, a task id or a version never is. Markdown stars around a due are not part of
it. Deferred and blocked items are not done, so they stay listed. The list is a pure function of
the record (freshness stays green across midnight with no new event), each entry is one line that
forms no record_check status claim, a broken record row degrades one entry and never the page, and
only filed AAR items are read: a contract line that merely says "due:" is not an AAR item."""
import copy
import datetime
import os

import pytest

from alpaca.tests.aar_items_kit import (NOW, OP, OTHER, author, block, by_action, check, contract, count, defer, entry,
                     filed, finish, freeze, ids, lines_of, listed, ok, overdue, resume, row, run,
                     seal, setup, task, task_add_id)
from alpaca import cli, clock, db, export, freshness, pad, taskplan, util
from alpaca.gates import record_check

OWNER = "fixture-keeper"
CASES = [("Alpha late by date", "2026-10-01", True),
         ("Bravo due today", "2026-10-13", False),
         ("Charlie due next week", "2026-10-20", False),
         ("Delta late by a day", "2026-10-12", True),
         ("Echo due tomorrow", "2026-10-14", False),
         ("Foxtrot due in a month", "2026-11-13", False),
         ("Golf due at year end", "2026-12-31", False),
         ("Hotel due next year", "2027-01-01", False),
         ("India late in bold", "**2026-10-02**", True),
         ("Juliet future in bold", "**2026-12-01**", False)]


def _sealed(project, monkeypatch, cases=CASES, iso=NOW):
    conn = setup(project, monkeypatch, iso)
    author(conn, project, OP, [row(a, owner=OWNER, due=d) for a, d, _late in cases])
    code, out = seal(OP)
    assert code == cli.PASS, out
    return conn, {a: by_action(conn, OP, a) for a, _d, _late in cases}


def test_week_arithmetic_used_by_the_cases():
    assert datetime.date.fromisocalendar(2026, 40, 7).isoformat() == "2026-10-04"
    assert datetime.date.fromisocalendar(2026, 42, 7).isoformat() == "2026-10-18"


def test_status_and_resume_list_exactly_the_late_items(project, monkeypatch):
    conn, tid = _sealed(project, monkeypatch)
    code, out = run("status")
    assert code == 0, out
    body = overdue(out)
    assert body is not None, "alpaca status prints no '## Overdue AAR items' section:\n" + out
    for action, due, late in CASES:
        assert listed(body, tid[action]) == late, "%s (due %s) listed=%s, want %s:\n%s" % (
            tid[action], due, listed(body, tid[action]), late, "\n".join(body))
    disk = open(os.path.join(project, "RESUME.md"), encoding="utf-8").read()
    assert overdue(disk) == body, "the printed status and the RESUME.md it wrote disagree"


@pytest.mark.parametrize("old,late,readable", [("2026-W40", True, True), ("2026-W42", False, True),
                                               ("op-099", True, False), ("v1.2", True, False)])
def test_a_due_filed_before_t_629_is_read_as_it_was(project, monkeypatch, old, late, readable):
    """aar check takes only a date now, but a contract filed earlier may hold a week, an op id or a
    version: a week is late after its Sunday; the others name no day and list as not readable."""
    from alpaca import aaritems, taskcontract
    conn, tid = _sealed(project, monkeypatch, [("Alpha filed earlier", "2026-12-01", False)])
    src = tid["Alpha filed earlier"]
    item = {"owner": OWNER, "due": old, "size": "S", "done_when": "ten green runs"}
    taskcontract.record(conn, src, aaritems.contract_of(OP, ".alpaca/aar/op-001.md", 5, item),
                        session="s", actor="agent", root=project)
    got = [r for r in aaritems.overdue(conn) if r["id"] == src]
    assert bool(got) == (late or not readable), got
    if got:
        assert (got[0]["days_late"] is not None) == readable, got


def test_an_entry_names_its_id_due_owner_and_source(project, monkeypatch):
    _conn, tid = _sealed(project, monkeypatch, CASES[:1])
    got = lines_of(overdue(resume(project)), tid["Alpha late by date"])
    assert len(got) == 1, got
    for word in ("2026-10-01", OWNER, OP):
        assert word in got[0], "the overdue entry does not name %r: %s" % (word, got[0])


def test_a_done_item_leaves_the_list(project, monkeypatch):
    _conn, tid = _sealed(project, monkeypatch, CASES[:1])
    finish(project, tid["Alpha late by date"])
    assert not listed(overdue(resume(project)), tid["Alpha late by date"])


def test_deferred_and_blocked_items_stay_listed(project, monkeypatch):
    cases = [("Alpha late by date", "2026-10-01", True), ("Kilo late too", "2026-09-30", True)]
    _conn, tid = _sealed(project, monkeypatch, cases)
    defer(tid["Alpha late by date"])
    block(tid["Kilo late too"])
    body = overdue(resume(project))
    assert listed(body, tid["Alpha late by date"]), "a deferred late item dropped out (deferred is not done)"
    assert listed(body, tid["Kilo late too"]), "a blocked late item dropped out"


def test_no_item_lists_no_task(project, monkeypatch):
    setup(project, monkeypatch)
    body = overdue(resume(project))
    assert not any("t-0" in l for l in (body or [])), body


def test_the_record_clock_not_the_wall_clock_decides(project, monkeypatch):
    freeze(monkeypatch, "2026-10-13T23:50:00+07:00")
    conn = setup(project, monkeypatch, "2026-10-13T23:50:00+07:00")
    author(conn, project, OP, [row("Lima due today", due="2026-10-13")])
    assert seal(OP)[0] == cli.PASS
    tid = by_action(conn, OP, "Lima due today")
    pad.write(project)
    export.write(project)
    assert freshness.check(project) == [], "setup: projections fresh right after writing"
    monkeypatch.setattr(util, "_CLOCK", clock.FixedClock("2026-10-14T00:10:00+07:00"))
    assert freshness.check(project) == [], "a clock-only date flip made a projection stale"
    ok("msg", "post", "--from", "w1", "--to", "all", "--kind", "note", "a later event")
    assert listed(overdue(resume(project)), tid), "the record moved past the due and the item is not late"


def _claims(project, monkeypatch, cases, owner="open"):
    conn = setup(project, monkeypatch)
    author(conn, project, OP, [row(a, owner=owner, due=d) for a, d in cases])
    assert seal(OP)[0] == cli.PASS
    body = overdue(resume(project))
    for tid in ids(conn, OP):
        assert len(lines_of(body, tid)) == 1, "an overdue entry is not one line: %s" % lines_of(body, tid)
    return [c for c in record_check.extract_claims("\n".join(body)) if c.value in record_check.STATUS_WORDS]


def test_entries_form_no_status_claim_and_stay_one_line(project, monkeypatch):
    """Free text that starts like a claim: quoting it after a fixed label (t-617 not_now_words)
    is enough here."""
    bad = _claims(project, monkeypatch, [("op-001: blocked until the vendor ships", "2026-10-01"),
                                         ("t-002 -> failed twice \\| with a \\<b\\> tag", "2026-10-02")])
    assert bad == [], "an overdue entry reads as a status claim: %s" % bad


def test_status_words_inside_the_text_form_no_claim(project, monkeypatch):
    """Sibling of the t-617 quoting rule: a claim can start mid-text ('gate = open'), where a
    leading quote does not help. RESUME.md is a record_check projection (record_check.py
    _PROJECTIONS), so such a line fails RECORD-UNBACKED-CLAIM at every door."""
    bad = _claims(project, monkeypatch, [("Keep the gate = open until the vendor ships", "2026-10-01"),
                                         ("Say why review -> waived last time", "2026-10-02")])
    assert bad == [], "an overdue entry reads as a status claim: %s" % bad


@pytest.mark.parametrize("mutate", [lambda v: "2026-13-45", lambda v: 20261002, lambda v: None,
                                    lambda v: [v], lambda v: "2026-W53"])
def test_a_broken_item_row_does_not_break_the_page(project, monkeypatch, mutate):
    """A copy of a real filed item with its due made unreadable (any storage layout: the due
    string is replaced wherever the copy holds it) degrades that entry, never the page."""
    cases = [("Alpha late by date", "2026-10-01", True), ("Mike late twin", "2026-10-02", True)]
    conn, tid = _sealed(project, monkeypatch, cases)
    src = tid["Mike late twin"]
    op = task(conn, src)["op"]

    def swap(value):
        if isinstance(value, dict):
            return {k: swap(v) for k, v in value.items()}
        if isinstance(value, list):
            return [swap(v) for v in value]
        if value == "2026-10-02":
            return mutate(value)
        if isinstance(value, str) and "2026-10-02" in value:
            return value.replace("2026-10-02", "2026-13-45")
        return value
    add = next(e for e in db.events(conn, kind="task-add", limit=10 ** 9) if e["ref"] == src)
    forged = "t-%03d" % (conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] + 1)
    with db.transaction(conn):
        db.append_event(conn, session="hand", actor="hand", kind="task-add", op=op, ref=forged,
                        data=swap(copy.deepcopy(add["data"])), conn_in_txn=True)
        row_ = dict(task(conn, src), id=forged, title="forged twin")
        db.upsert(conn, "tasks", "id", row_)
        taskplan.map_task(conn, op, forged, {"group": entry(conn, src)["group"], "after": [], "resources": []},
                          session="hand", actor="hand", in_transaction=True)
    c = {k: v for k, v in (contract(conn, src) or {}).items() if k not in ("event_id", "at", "by")}
    if c:
        db.append_event(conn, session="hand", actor="hand", kind="task-contract", op=op, ref=forged,
                        data=swap(c))
    text = resume(project)
    body = overdue(text)
    assert body is not None, "the overdue heading vanished"
    assert listed(body, tid["Alpha late by date"]), "one broken item hid a good one:\n" + "\n".join(body)
    assert "## Recent events" in text


@pytest.mark.parametrize("origin", [None, "derived=aar:op-001"])
def test_a_plain_task_whose_contract_says_due_is_not_an_aar_item(project, monkeypatch, origin):
    conn = setup(project, monkeypatch)
    argv = ["task", "add", OTHER, "write the vendor note", "--title", "vendor note", "--phase", "build",
            "--expected", "due: 2026-01-01", "--expected", "owner: agent", "--done-bar", "the note is sent"]
    if origin:
        argv += ["--origin", origin]
    ok(*argv)
    tid = db.rows(conn, "tasks", "statement=?", ("write the vendor note",))[0]["id"]
    assert not listed(overdue(resume(project)), tid), \
        "a task that only says 'due:' in its contract is listed as an overdue AAR item"


def test_a_week_53_in_a_year_without_one_never_crashes(project, monkeypatch):
    conn = setup(project, monkeypatch)
    author(conn, project, OP, [row("November no such week", due="2025-W53")])
    ccode, cout = check(OP)
    scode, sout = seal(OP)
    assert (ccode == cli.PASS) == (scode == cli.PASS), "check and seal disagree on 2025-W53:\n%s\n%s" % (cout, sout)
    text = resume(project)
    assert overdue(text) is not None
    code, out = run("status")
    assert code == 0, out
