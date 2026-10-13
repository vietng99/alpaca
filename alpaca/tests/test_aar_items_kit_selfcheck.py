"""(kit self-check; passes before and after the build) The a07 edits that are not about the event
cite are refused by the Timeline grammar today, with the line they name, so a Carried over section
shaped like the Timeline can refuse them too. The kit readers (filed, by_action, contract_lines,
entry) and the a05 forging code work over a filing done by hand with the internal API the task
names (taskplan.group, ops.add_task with a mapping, taskcontract.record with derived=aar:<op>).
Note the phase: every op opened by `op new` has a batch (ops.py:279-280), so add_task needs one."""
import re
import pytest
from alpaca.tests.aar_items_kit import (OP, OTHER, author, by_action, check, contract, contract_lines, entry, filed,
                     read, report, resume, row, setup, task, write)
from alpaca.tests import test_aar_items_carried_grammar as g
from alpaca import aar, cli, db, ops, taskcontract, taskplan


@pytest.fixture
def tl(project, monkeypatch):
    conn = setup(project, monkeypatch)
    author(conn, project, OP, [row("Alpha")])
    lines = read(report(project, OP)).split("\n")
    at = lines.index("## Timeline")
    rows = [i for i in range(at + 1, len(lines)) if lines[i].startswith("- ")]
    end = next(i for i in range(at + 1, len(lines)) if lines[i].startswith("## "))
    rows = [i for i in rows if i < end]
    return {"project": project, "conn": conn, "lines": lines, "at": at, "rows": rows}


@pytest.mark.parametrize("name", [n for n in sorted(g.EDITS) if "cit" not in n and "naming" not in n and "no event" not in n])
def test_timeline_refuses_the_same_edit(tl, name):
    lines, named = g.EDITS[name](list(tl["lines"]), tl)
    write(report(tl["project"], OP), "\n".join(lines))
    code, out = check(OP)
    assert code == cli.FAIL, out
    assert re.search(r"\bline %d\b" % named, out), out


def simulate_filing(conn, project, src, into):
    text = read(report(project, src))
    items = aar.parse_actions(text)
    taskplan.group(conn, into, "aar-%s" % src, "after-action items of %s" % src, "learning", session="p", actor="agent")
    for it in items:
        tid = ops.add_task(conn, into, it["action"], ops.short_title(it["action"]) or "x", phase="build", session="p", actor="agent",
                           extra={"aar": {"op": src, "due": it["due"].strip(" *_"), "owner": it["owner"]}},
                           mapping={"group": "aar-%s" % src, "after": [], "resources": []})
        taskcontract.record(conn, tid, {"input": [".alpaca/aar/%s.md line %d" % (src, it["line"])],
                                        "expected": ["owner: %s" % it["owner"], "due: %s" % it["due"].strip(" *_"),
                                                     "size: %s" % it["size"]],
                                        "done_bar": [it["done_when"]], "origin": "derived=aar:%s" % src},
                            session="p", actor="agent")


def test_kit_readers_over_a_hand_filing(project, monkeypatch):
    conn = setup(project, monkeypatch)
    author(conn, project, OP, [row("Mike late twin", owner="fixture-keeper", due="2026-10-02", size="XL"),
                               row("Alpha late", due="**2026-10-01**")])
    simulate_filing(conn, project, OP, OTHER)
    got = filed(conn, OP)
    assert len(got) == 2 and {o for _t, o in got} == {OTHER}
    tid = by_action(conn, OP, "Mike late twin")
    lines = contract_lines(contract(conn, tid))
    assert any("fixture-keeper" in t for _p, t, _o in lines)
    assert all(o["kind"] == "derived" and o["reason"] == "aar:op-001" for _p, _t, o in lines), lines
    assert entry(conn, tid)["after"] == []
    # the forging code of a05
    import copy
    src = tid
    op = task(conn, src)["op"]
    add = next(e for e in db.events(conn, kind="task-add", limit=10 ** 9) if e["ref"] == src)
    forged = "t-%03d" % (conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] + 1)
    with db.transaction(conn):
        db.append_event(conn, session="hand", actor="hand", kind="task-add", op=op, ref=forged,
                        data=copy.deepcopy(add["data"]), conn_in_txn=True)
        db.upsert(conn, "tasks", "id", dict(task(conn, src), id=forged, title="forged twin"))
        taskplan.map_task(conn, op, forged, {"group": entry(conn, src)["group"], "after": [], "resources": []},
                          session="hand", actor="hand", in_transaction=True)
    c = {k: v for k, v in (contract(conn, src) or {}).items() if k not in ("event_id", "at", "by")}
    db.append_event(conn, session="hand", actor="hand", kind="task-contract", op=op, ref=forged, data=c)
    assert len(filed(conn, OP)) == 3
    assert "## Recent events" in resume(project)
