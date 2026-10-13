"""(t-616 B01, class C): a projection renders the record and never writes to it. With
deferred tasks on the record (met and unmet triggers), rendering RESUME, CHECKLIST, data.json, the
hub overview, the task plan and `status --json` appends no event: a status-like event after the
deferral would also flip the research fold (research/state.py _task_status) away from deferred."""
from pathlib import Path

from alpaca.tests.not_now_kit import checklist, data, defer, event_count, finish, nodes, resume, run, setup
from alpaca import hub


def test_rendering_with_deferred_tasks_writes_nothing(project):
    conn = setup(project, 3)
    finish(project, "t-001")
    defer("t-002", "parked", "after:t-001")
    defer("t-003", "parked", "when the owner asks")
    before = event_count(conn)
    resume(project); checklist(project); data(project)
    hub.overview(Path(project)); nodes(project, conn)
    assert run("status", "--json")[0] == 0
    assert event_count(conn) == before
    from alpaca.research import state
    assert state._task_status(conn, "t-002") == "deferred"
