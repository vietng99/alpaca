"""op-008 t-065 fix-r1: a status history does not depend on capture order across sessions (FR-14,
ARCH 8.1: recovery and the evaluation rebuild fill the graph as capture does), and the withdrawal of
an event also withdraws the Alpaca source copy made from it (FR-9, OB-07)."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import capture, store                                  # noqa: E402
from test_record_extract import _at                                        # noqa: E402

from alpaca.wiki import service                                            # noqa: E402


def _moves(project):
    _at(project, "task-move", {"from": "doing", "to": "done"}, "2026-03-01T10:00:01+07:00",
        session="s-a", ref="t-9", actor="human")
    _at(project, "task-move", {"from": "done", "to": "doing"}, "2026-03-01T10:00:02+07:00",
        session="codex-b", ref="t-9", actor="human")
    _at(project, "task-move", {"from": "doing", "to": "done"}, "2026-03-01T10:00:03+07:00",
        session="s-a", ref="t-9", actor="human")


def _history(project):
    with store(project) as conn:
        rows = conn.execute(
            "SELECT obj_node, valid_from, valid_until FROM edges WHERE predicate='status' "
            "AND subj_node='t-9' AND status='active' ORDER BY valid_from").fetchall()
    return [tuple(r) for r in rows]


def test_status_history_is_the_same_in_record_order_and_in_hook_order(project, tmp_path):
    _moves(project)
    capture(project)
    record_order = _history(project)
    assert [r[0] for r in record_order] == ["status:done", "status:doing", "status:done"]
    assert record_order[-1][2] is None


def test_hook_order_capture_keeps_the_later_move(project):
    _moves(project)
    for sid in ("s-a", "codex-b"):      # A's Stop hook lands before the other session's events
        service.capture(project, sessions=[sid], session_events=True, uncaptured_only=True,
                        transcripts=False, deadline_s=None)
    hist = _history(project)
    assert [r[0] for r in hist] == ["status:done", "status:doing", "status:done"], hist
    assert [r[2] is None for r in hist] == [False, False, True]
    assert hist[0][2] == hist[1][1] and hist[1][2] == hist[2][1]


def test_event_withdrawal_hides_its_decision_source_copy(project):
    d = _at(project, "decision", {"id": "dec-aaaaaa000006", "choice": "use hunter2-orchid as password"},
            "2026-03-01T10:00:00+07:00", ref="dec-aaaaaa000006")
    capture(project)
    with store(project) as conn:
        conn.execute("INSERT INTO exclusions(source_ref,reason,requested_by,record_event_id,recorded_at) "
                     "VALUES(?,?,?,?,?)", ("event:%d" % d, "test", "owner", 1, "2026-10-02T00:00:00+00:00"))
    from alpaca.wiki.servable import servable_ref
    with store(project) as conn:
        assert not servable_ref(conn, "decision:dec-aaaaaa000006")
    assert "hunter2" not in service.context(project, "what did we decide about the password", max_chars=4000)
    r = service.query(project, "dec-aaaaaa000006")
    assert all(s["source_ref"] != "decision:dec-aaaaaa000006" for s in r.get("sources", []))


def _status_after(project):
    with store(project) as conn:
        rows = conn.execute(
            "SELECT obj_node FROM edges WHERE predicate='status' AND subj_node='t-9' "
            "AND status='active' AND valid_until IS NULL").fetchall()
    return sorted(r[0] for r in rows)


def _forget_versions(conn):
    """Make the store look as B0 left it: no source_versions row at all."""
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' "
                                "AND tbl_name='source_versions'").fetchall():
        conn.execute("DROP TRIGGER %s" % name)
    conn.execute("DELETE FROM source_versions")


def _same_second(project):
    ts = "2026-03-01T10:00:00+07:00"
    _at(project, "task-move", {"from": "doing", "to": "done"}, ts, session="codex-b", ref="t-9", actor="human")
    _at(project, "task-move", {"from": "doing", "to": "review"}, ts, session="s-a", ref="t-9", actor="human")


def test_same_second_moves_from_two_sessions_keep_the_record_order(project):
    """The later event id is current whichever session reaches the wiki first (ARCH 8.1)."""
    _same_second(project)
    capture(project)
    assert _status_after(project) == ["status:review"]


def test_same_second_moves_in_hook_order_give_the_record_order_status(project):
    _same_second(project)
    for sid in ("s-a", "codex-b"):      # the later event's session lands first
        service.capture(project, sessions=[sid], session_events=True, uncaptured_only=True,
                        transcripts=False, deadline_s=None)
    assert _status_after(project) == ["status:review"]


def test_withdrawal_hides_the_copy_when_the_note_has_no_source_version_row(project):
    """A note captured before the copies existed has no source_versions row (the live store)."""
    d = _at(project, "decision", {"id": "dec-aaaaaa000007", "choice": "use hunter3-orchid as password"},
            "2026-03-01T10:00:00+07:00", ref="dec-aaaaaa000007")
    capture(project)
    with store(project) as conn:
        _forget_versions(conn)
        conn.execute("INSERT INTO exclusions(source_ref,reason,requested_by,record_event_id,recorded_at) "
                     "VALUES(?,?,?,?,?)", ("event:%d" % d, "test", "owner", 1, "2026-10-02T00:00:00+00:00"))
    from alpaca.wiki.servable import servable_ref
    with store(project) as conn:
        assert not servable_ref(conn, "decision:dec-aaaaaa000007")
    assert "hunter3" not in service.context(project, "what did we decide about the password", max_chars=4000)


def test_withdrawing_the_copy_hides_the_event_note(project):
    d = _at(project, "decision", {"id": "dec-aaaaaa000008", "choice": "use hunter4-orchid as password"},
            "2026-03-01T10:00:00+07:00", ref="dec-aaaaaa000008")
    capture(project)
    with store(project) as conn:
        _forget_versions(conn)
        conn.execute("INSERT INTO exclusions(source_ref,reason,requested_by,record_event_id,recorded_at) "
                     "VALUES(?,?,?,?,?)", ("decision:dec-aaaaaa000008", "test", "owner", 1,
                                           "2026-10-02T00:00:00+00:00"))
    from alpaca.wiki.servable import servable_ref
    with store(project) as conn:
        assert not servable_ref(conn, "event:%d" % d)
