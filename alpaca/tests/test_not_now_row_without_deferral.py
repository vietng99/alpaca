"""(classes C2, C3, E): ops.deferral returns None for a row set to deferred some other
way, or whose newest move is not a deferral. Such a row is still deferred on tasks.status, so
every projection lists it under Not now (reason and trigger shown as not recorded), never marks it
met, and never shows a stale reason from an older deferral. Projections key on tasks.status."""
from pathlib import Path

from alpaca.tests.not_now_kit import (R1, W1, blocks, checklist, data, defer, entries, marked, met_flags,
                  not_now_entries, ok, resume, section, setup)
from alpaca import db, hub, ops


def _check_listed(project, tid, stale=()):
    text = resume(project)
    body = section(text)
    assert body is not None and entries(body, tid), "deferred row dropped from RESUME Not now"
    assert not marked(text)
    for s in stale:
        assert s not in "\n".join(body), "stale deferral text %r shown" % s
    payload = data(project)
    rows = not_now_entries(payload, tid)
    assert rows and all(True not in met_flags(r) for r in rows), rows
    for s in stale:
        assert not any(s in str(r) for r in rows)
    cl = checklist(project)
    assert blocks(cl, tid) and not marked(cl)
    hub.overview(Path(project))


def test_row_deferred_with_no_event(project):
    conn = setup(project, 2)
    db.patch(conn, "tasks", "id", "t-002", {"status": "deferred"})
    assert ops.deferral(conn, "t-002") is None
    _check_listed(project, "t-002")


def test_row_deferred_after_a_reopen_shows_no_stale_reason(project):
    conn = setup(project, 2)
    defer("t-002", "an old reason", "after:t-001")
    ok("task", "move", "t-002", "open")
    db.patch(conn, "tasks", "id", "t-002", {"status": "deferred"})
    assert ops.deferral(conn, "t-002") is None
    _check_listed(project, "t-002", stale=("an old reason",))


def test_newest_move_to_deferred_with_blank_trigger(project):
    conn = setup(project, 2)
    defer("t-002", "an old reason", "after:t-001")
    db.append_event(conn, session="hand", actor="hand", kind="task-move", ref="t-002",
                    data={"from": "deferred", "to": "deferred", "proof": None, "reason": "x",
                          "reactivate_when": "  "})
    assert ops.deferral(conn, "t-002") is None
    _check_listed(project, "t-002", stale=("an old reason",))


def test_projections_follow_the_row_not_the_event(project):
    """C3: the newest move went to deferred but the row moved on (hand edit): no Not now entry."""
    conn = setup(project, 2)
    defer("t-002")
    db.patch(conn, "tasks", "id", "t-002", {"status": "open"})
    body = section(resume(project)) or []
    assert not entries(body, "t-002")
    assert not not_now_entries(data(project), "t-002")
