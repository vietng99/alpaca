"""(op-057 t-623, round 2) The task's contract is the one store of an item's owner, due, size and
done-when. The aar-seal event holds each item's identity and the id of the contract event the seal
last wrote. A re-seal rewrites a contract, whole, only while that event is still the task's newest;
a contract anyone else wrote is left as it is and each differing field is noted on every re-seal.
Overdue and Carried over read the first owner, due and size line of the newest contract."""
import json

import pytest

from alpaca.tests.aar_items_kit import (OP, OTHER, author, by_action, contract, count, draft, ok, overdue,
                                        row, run, seal, section, setup, CARRIED)
from alpaca import aaritems, aar, cli, db, ops, taskcontract

OLD = "2026-10-01"          # the due the first seal files; it must never show once the contract moves


def _filed(project, monkeypatch, **kw):
    conn = setup(project, monkeypatch)
    author(conn, project, OP, [row("Alpha item", due=OLD, **kw)])
    code, out = seal(OP)
    assert code == cli.PASS, out
    return conn, by_action(conn, OP, "Alpha item")


def _person(tid, conn, **change):
    """What `task contract` does: a person records a contract with some lines replaced and a fail case."""
    c = contract(conn, tid)
    expected = [l for l in c["expected"] if l.split(":")[0] not in change]
    expected += ["%s: %s" % (k, v) for k, v in change.items() if k != "done"]
    bar = [change["done"]] if "done" in change else c["done_bar"]
    ok("task", "contract", tid, *sum([["--expected", e] for e in expected], []),
       *sum([["--done-bar", d] for d in bar], []), *sum([["--input", i] for i in c["input"]], []),
       "--fail-case", "the clock drifts")


def _notes(out):
    return [l.strip() for l in out.splitlines() if l.strip().startswith("note:")]


def _updates(out):
    return [l.strip() for l in out.splitlines() if l.strip().startswith("updated")]


@pytest.mark.parametrize("due,listed,carried", [
    ("2026-12-01", False, "2026-12-01"), ("op-060", True, "op-060"), ("t-001", True, "t-001"),
    ("v1.3", True, "v1.3"), ("2026-10-05", True, "2026-10-05"), ("2026-W39", True, "2026-W39"),
    ("Dec 1", True, "Dec 1"), ("2026-12-01 (moved)", True, "2026-12-01 (moved)")])
def test_overdue_and_carried_show_the_due_line(project, monkeypatch, due, listed, carried):
    conn, tid = _filed(project, monkeypatch)
    _person(tid, conn, due=due)
    code, out = run("status")
    lines = [l for l in overdue(out) if tid in l]
    assert bool(lines) == listed, out
    if listed and due in ("Dec 1", "2026-12-01 (moved)", "op-060", "t-001", "v1.3"):
        assert "not readable" in lines[0], lines
    assert OLD not in " ".join(lines)
    body = section(draft(project, OTHER), CARRIED) or []
    assert carried in " ".join(body) and OLD not in " ".join(body), body


def test_overdue_and_carried_with_no_due_line(project, monkeypatch):
    conn, tid = _filed(project, monkeypatch)
    c = contract(conn, tid)
    ok("task", "contract", tid, "--expected", "owner: bob", "--done-bar", c["done_bar"][0],
       "--input", c["input"][0])
    code, out = run("status")
    assert not [l for l in overdue(out) if tid in l], out
    body = " ".join(section(draft(project, OTHER), CARRIED) or [])
    assert "due (none)" in body and "owner bob" in body, body


@pytest.mark.parametrize("owner,shown", [("bob", "owner bob"), ("Alice (QA lead)", "(see its contract)"),
                                         ("t-001: done", "(see its contract)")])
def test_overdue_and_carried_show_the_owner_line(project, monkeypatch, owner, shown):
    conn, tid = _filed(project, monkeypatch)
    _person(tid, conn, owner=owner)
    code, out = run("status")
    lines = [l for l in overdue(out) if tid in l]
    assert len(lines) == 1 and shown in lines[0], out
    assert shown in " ".join(section(draft(project, OTHER), CARRIED) or [])


def test_an_old_payload_field_is_never_read(project, monkeypatch):
    conn, tid = _filed(project, monkeypatch)
    _person(tid, conn, due="2026-12-01")
    item = dict(aaritems.filed(conn, OP)[0], owner="agent", due=OLD, due_by=OLD, size="S", done_when="x")
    for k in ("source", "seal"):
        item.pop(k)
    db.append_event(conn, session="cli", actor="agent", kind=aar.KIND_SEAL, op=OP, ref=OP,
                    data={"filed": {"items": [item]}})
    assert aaritems.overdue(conn) == []
    assert set(aaritems.filed(conn)[0]) == set(aaritems.IDENTITY) | {"source", "seal"}


FIELDS = [("owner", "bob", "carol"), ("due", "2026-10-20", "2026-10-22"), ("size", "S", "L"),
          ("done_when", "ten green runs", "twenty green runs")]


def _edit(project, conn, field, value):
    kw = {"owner": "agent", "due": "2026-10-20", "size": "S", "done": "ten green runs"}
    kw[{"done_when": "done"}.get(field, field)] = value
    author(conn, project, OP, [row("Alpha item", **kw)])


@pytest.mark.parametrize("field,first,second", FIELDS)
def test_a_seal_owned_contract_follows_the_report(project, monkeypatch, field, first, second):
    conn = setup(project, monkeypatch)
    _edit(project, conn, field, first)
    code, out = seal(OP)
    assert code == cli.PASS, out
    tid = by_action(conn, OP, "Alpha item")
    for _ in range(3):
        before = count(conn, taskcontract.KIND)
        code, out = seal(OP)
        assert code == cli.PASS and not _notes(out) and not _updates(out), out
        assert count(conn, taskcontract.KIND) == before
    own = aaritems.filed(conn, OP)[0]["contract_event"]
    assert type(own) is int and own == taskcontract.latest(conn)[tid]["event_id"]
    _edit(project, conn, field, second)
    code, out = seal(OP)
    assert code == cli.PASS, out
    assert count(conn, taskcontract.KIND) == before + 1
    c = contract(conn, tid)
    assert c["by"] == "agent" and "owner_word" not in c
    want = "%s %s is now %s" % (field.replace("_", "-"), first, second)
    assert [l for l in _updates(out) if want in l], out
    assert not _notes(out)


@pytest.mark.parametrize("field,first,second", FIELDS)
def test_a_contract_someone_else_wrote_is_left_and_noted(project, monkeypatch, field, first, second):
    conn = setup(project, monkeypatch)
    _edit(project, conn, field, first)
    assert seal(OP)[0] == cli.PASS
    tid = by_action(conn, OP, "Alpha item")
    _person(tid, conn)                                  # a person's newest contract, fail case added
    newest = contract(conn, tid)["event_id"]
    _edit(project, conn, field, second)
    before = count(conn, taskcontract.KIND)
    for _ in range(2):
        code, out = seal(OP)
        assert code == cli.PASS, out
        notes = _notes(out)
        assert len(notes) == 1 and tid in notes[0] and "alpaca task contract" in notes[0], out
        assert field.replace("_", "-") in notes[0] and not _updates(out), out
    assert count(conn, taskcontract.KIND) == before
    c = contract(conn, tid)
    assert c["event_id"] == newest and c["fail_cases"] == ["the clock drifts"]


def test_agreeing_with_a_person_does_not_hand_the_contract_back(project, monkeypatch):
    conn, tid = _filed(project, monkeypatch)
    _person(tid, conn, due="2026-11-05")
    newest, before = contract(conn, tid)["event_id"], count(conn, taskcontract.KIND)
    author(conn, project, OP, [row("Alpha item", due="2026-11-05")])
    code, out = seal(OP)
    assert code == cli.PASS and not _notes(out) and not _updates(out), out
    author(conn, project, OP, [row("Alpha item", due="2026-11-09")])
    code, out = seal(OP)
    assert len(_notes(out)) == 1 and not _updates(out), out
    c = contract(conn, tid)
    assert c["event_id"] == newest == contract(conn, tid)["event_id"] and "due: 2026-11-05" in c["expected"]
    assert c["fail_cases"] == ["the clock drifts"] and count(conn, taskcontract.KIND) == before


def test_a_payload_without_contract_event_is_never_owned(project, monkeypatch):
    conn, tid = _filed(project, monkeypatch)
    item = {k: v for k, v in aaritems.filed(conn, OP)[0].items() if k not in ("source", "seal", "contract_event")}
    db.append_event(conn, session="cli", actor="agent", kind=aar.KIND_SEAL, op=OP, ref=OP,
                    data={"filed": {"items": [item]}})
    before = count(conn, taskcontract.KIND)
    author(conn, project, OP, [row("Alpha item", due="2026-10-22")])
    code, out = seal(OP)
    assert code == cli.PASS and len(_notes(out)) == 1, out
    assert count(conn, taskcontract.KIND) == before


@pytest.mark.parametrize("what", ["long", "shift"])
def test_an_unchanged_report_writes_nothing(project, monkeypatch, what):
    conn = setup(project, monkeypatch)
    done = "x" * 700 if what == "long" else "ten green runs"
    author(conn, project, OP, [row("Alpha item", done=done)])
    assert seal(OP)[0] == cli.PASS
    tid = by_action(conn, OP, "Alpha item")
    if what == "shift":
        author(conn, project, OP, [row("Alpha item", done=done)],
               over={"Summary": "A paragraph.\n\nAnother paragraph that moves the table down."})
    before = count(conn, taskcontract.KIND)
    code, out = seal(OP)
    assert code == cli.PASS and count(conn, taskcontract.KIND) == before, out
    assert not _notes(out) and not _updates(out), out


def test_a_second_done_bar_line_is_no_difference(project, monkeypatch):
    conn, tid = _filed(project, monkeypatch)
    c = contract(conn, tid)
    ok("task", "contract", tid, *sum([["--expected", e] for e in c["expected"]], []),
       "--done-bar", c["done_bar"][0], "--done-bar", "and a second check",
       "--input", c["input"][0])
    code, out = seal(OP)
    assert not _notes(out) and not _updates(out), out
    author(conn, project, OP, [row("Alpha item", due=OLD, done="twenty green runs")])
    code, out = seal(OP)
    assert len(_notes(out)) == 1, out


def test_a_failed_contract_write_names_the_task_and_seals_nothing(project, monkeypatch):
    conn, tid = _filed(project, monkeypatch)
    author(conn, project, OP, [row("Alpha item", due="2026-10-22")])
    real = taskcontract.record

    def boom(c, task, *a, **k):
        raise ValueError("the contract store is full")
    monkeypatch.setattr(taskcontract, "record", boom)
    counts = lambda: [count(conn, k) for k in (aar.KIND_SEAL, taskcontract.KIND, "task-add")]
    before = counts()
    code, out = seal(OP)
    assert code != cli.PASS and tid in out, out
    assert counts() == before
    monkeypatch.setattr(taskcontract, "record", real)
    code, out = seal(OP)
    assert code == cli.PASS and count(conn, taskcontract.KIND) == before[1] + 1, out


def test_a_damaged_newest_contract_is_noted_not_refused(project, monkeypatch):
    conn, tid = _filed(project, monkeypatch)
    db.append_event(conn, session="cli", actor="agent", kind=taskcontract.KIND, op=None, ref=tid,
                    data={"expected": "owner: bob", "done_bar": None})
    before = count(conn, taskcontract.KIND)
    author(conn, project, OP, [row("Alpha item", due="2026-10-22")])
    code, out = seal(OP)
    assert code == cli.PASS and _notes(out), out
    assert count(conn, taskcontract.KIND) == before


def test_a_seal_of_another_op_reuses_the_standing_op(project, monkeypatch):
    conn, tid = _filed(project, monkeypatch)
    standing = aaritems.standing_op(conn)
    author(conn, project, OTHER, [row("Bravo item")])
    assert seal(OTHER)[0] == cli.PASS
    assert aaritems.standing_op(conn) == standing
    row_ = conn.execute("SELECT status FROM ops WHERE id=?", (standing,)).fetchone()
    assert row_[0] == "open"
