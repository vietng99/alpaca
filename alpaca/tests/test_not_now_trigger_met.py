"""(done_bar[2], classes D3, D4, F2, F3): a structured trigger that has come true is
marked 'trigger met: reconsider' on that task's entry in RESUME.md, the status text and
CHECKLIST.md, and data.json carries a true met flag; a trigger that has not come true, and free
text whatever it says, never is. Met is evaluated live on each render (a done task moved back
makes it unmet again) and is never recursive (a pair of tasks waiting on each other renders)."""
import pytest

from alpaca.tests.not_now_kit import (not_now, blocks, checklist, data, defer, finish, freeze, marked, met_flags, new_op,
                  not_now_entries, ok, raw_defer, resume, run, section, setup)
from alpaca import db

TODAY = "2026-10-13T12:00:00+07:00"


def _world(project, monkeypatch, state):
    """op-001 with t-001 (the deferred subject), t-002 and t-003; op-002 open or closed; t-002 in
    `state`."""
    freeze(monkeypatch, TODAY)
    conn = setup(project, 3)
    op2 = new_op()
    assert op2 == "op-002"
    if state.get("op-002") == "closed":
        ok("op", "close", "op-002", "--basis", "b")
    t2 = state.get("t-002", "open")
    if t2 == "done":
        finish(project, "t-002")
    elif t2 == "blocked":
        ok("task", "move", "t-002", "blocked", "--reason", "stuck")
    elif t2 == "deferred":
        defer("t-002", "parked too", "when someone asks")
    return conn


def _all_views(project):
    out = run("status")[1]
    return {"resume": resume(project), "status": out, "checklist": checklist(project),
            "data": data(project)}


def _met_in_text(text, tid="t-001"):
    return any(marked(b) for b in blocks(text, tid))


MET = [
    ("after:t-002", {"t-002": "done"}),
    ("AFTER:T-002", {"t-002": "done"}),
    ("op:op-002 closed", {"op-002": "closed"}),
    ("date:2026-10-12", {}),
    ("date:2026-10-13", {}),
    ("date:2001-01-01", {}),
    ("date:2099-01-01 or after:t-002", {"t-002": "done"}),
    ("when legal signs, op:op-002 closed", {"op-002": "closed"}),
]

UNMET = [
    ("after:t-002", {"t-002": "open"}),
    ("after:t-002", {"t-002": "blocked"}),
    ("after:t-002", {"t-002": "deferred"}),
    ("op:op-002 closed", {}),
    ("date:2026-10-14", {}),
    ("when legal signs, after:t-002", {}),
    # free text, never met even when what it describes is true
    ("after t-002 is done", {"t-002": "done"}),
    ("date 2020-01-01", {}),
    ("once op-002 is closed", {"op-002": "closed"}),
    ("2020-01-01", {}),
    ("t-002 done", {"t-002": "done"}),
]


@pytest.mark.parametrize("when,state", MET)
def test_met_trigger_is_marked_everywhere(project, monkeypatch, when, state):
    _world(project, monkeypatch, state)
    defer("t-001", "parked", when)
    views = _all_views(project)
    for name in ("resume", "status"):
        assert _met_in_text("\n".join(section(views[name]) or []), "t-001"), (name, when)
    assert _met_in_text(views["checklist"]), ("checklist", when)
    rows = not_now_entries(views["data"], "t-001")
    assert rows and all(True in met_flags(r) for r in rows), ("data.json", when, rows)


@pytest.mark.parametrize("when,state", UNMET)
def test_unmet_or_free_text_trigger_is_never_marked(project, monkeypatch, when, state):
    _world(project, monkeypatch, state)
    defer("t-001", "parked", when)
    views = _all_views(project)
    for name in ("resume", "status", "checklist"):
        assert not marked(views[name]), (name, when)
    rows = not_now_entries(views["data"], "t-001")
    assert rows and all(True not in met_flags(r) for r in rows), ("data.json", when, rows)


def test_marker_belongs_to_the_task_whose_trigger_is_met(project, monkeypatch):
    _world(project, monkeypatch, {"t-002": "done"})
    defer("t-001", "parked", "after:t-002")
    defer("t-003", "parked", "when the owner asks")
    body = not_now(resume(project))
    assert _met_in_text("\n".join(body), "t-001")
    assert not _met_in_text("\n".join(body), "t-003"), body


def test_met_is_evaluated_live_not_cached(project, monkeypatch):
    conn = _world(project, monkeypatch, {"t-002": "done"})
    defer("t-001", "parked", "after:t-002")
    assert _met_in_text("\n".join(not_now(resume(project))))
    db.patch(conn, "tasks", "id", "t-002", {"status": "open"})
    assert not marked(resume(project))
    assert all(True not in met_flags(r) for r in not_now_entries(data(project), "t-001"))


def test_tasks_waiting_on_each_other_render_and_stay_unmet(project, monkeypatch):
    _world(project, monkeypatch, {})
    defer("t-001", "parked", "after:t-002")
    defer("t-002", "parked", "after:t-001")
    views = _all_views(project)
    assert not marked(views["resume"]) and not marked(views["checklist"])
    body = not_now(views["resume"])
    assert blocks(body, "t-001") and blocks(body, "t-002")


def test_hand_written_bad_trigger_is_listed_and_never_met(project, monkeypatch):
    """D4: a trigger the verb would refuse (an impossible date) set on the record by hand."""
    conn = _world(project, monkeypatch, {})
    raw_defer(conn, "t-001", "parked", "date:2026-02-30")
    text = resume(project)
    assert blocks(section(text) or [], "t-001") and not marked(text)
