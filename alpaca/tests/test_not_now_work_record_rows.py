"""(classes A2, C4): work_record.tasks feeds the hub, the checklist, taskplan and the
mission. Its history entries copy from/to/reason/proof but not reactivate_when, so a history view
shows a deferral with no trigger; its 'reason' key is the task's why and must stay so (taskplan
blocked nodes and hub TASK_TEXT read it); its ORDER BY CASE puts deferred with done."""
from alpaca.tests.not_now_kit import R1, W1, defer, finish, setup
from alpaca import db, work_record


def test_history_entry_of_a_deferral_carries_the_trigger(project):
    setup(project, 1)
    defer("t-001")
    conn = db.connect(project)
    row = next(t for t in work_record.tasks(conn) if t["id"] == "t-001")
    moves = [h for h in row["history"] if h.get("to") == "deferred"]
    assert moves and moves[-1]["reason"] == R1 and moves[-1].get("reactivate_when") == W1, moves


def test_reason_key_stays_the_why(project):
    conn = setup(project, 1)
    db.patch(conn, "tasks", "id", "t-001", {"why": "the purpose of the task"})
    defer("t-001")
    row = next(t for t in work_record.tasks(conn) if t["id"] == "t-001")
    assert row["reason"] == "the purpose of the task"


def test_deferred_rows_sort_before_done(project):
    conn = setup(project, 2)
    finish(project, "t-001")
    defer("t-002")
    ids = [t["id"] for t in work_record.tasks(conn)]
    assert ids.index("t-002") < ids.index("t-001"), ids
