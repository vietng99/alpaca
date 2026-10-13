"""A02 (done_bar[0], cause C2.6): a NO-GO decision of a review flow makes the op owe an AAR.

reviewflow._save writes the event with op NULL (reviews._event is called without op), and no profile
in this tree ties a run ref to an op. So: event.op set -> that op owes; op NULL -> the one op of the
tasks the deciding session held in doing then (t-629); else listed by `aar owed` as unattributed
with its event id, blocking every op open when it was recorded. A GO, a flow event
with no decision, and a NO-GO-looking value outside `decision.value` are not triggers.

The review-flow kinds come from the profile (hub.py reads profile...events()["review_flow_kinds"]);
each test installs a profile naming `run-review-flow`, the kind test_runlog_reviewflow.py uses."""
import json

import pytest

from alpaca import cli, db, profile
from alpaca.tests import aarowedkit as k

FLOW = "run-review-flow"


class Fake(profile.Profile):
    def __init__(self, **hooks):
        self.__dict__.update(hooks)


@pytest.fixture(autouse=True)
def flow_profile(monkeypatch):
    monkeypatch.setattr(profile, "load", lambda r: Fake(events=lambda: {"review_flow_kinds": [FLOW]}))


def decision(conn, value, *, op=k.OP, ref="j-1", at="2026-10-13T10:00:00+00:00"):
    return db.append_event(conn, session="main", actor="agent", kind=FLOW, op=op, ref=ref,
                           data={"decision": {"value": value, "session": "main", "decided_at": at,
                                              "rationale": "the run exited before step two"},
                                 "receipt": ref})["id"]


def test_a_nogo_on_the_op_blocks_the_close_and_names_it(project, capsys):
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    ev = decision(conn, "NO-GO")
    assert "event:%d" % ev in k.owed(capsys, k.OP)
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, names=("no-go",), events=(ev,))
    k.seal_aar(project, conn, capsys)
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)


def test_go_and_undecided_flow_events_owe_nothing(project, capsys):
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    decision(conn, "GO")
    db.append_event(conn, session="main", actor="agent", kind=FLOW, op=k.OP, ref="j-2",
                    data={"assignment": {"expires_at": 0}, "decision": None})
    db.append_event(conn, session="main", actor="agent", kind=FLOW, op=k.OP, ref="j-3",
                    data={"note": "NO-GO", "decision": {"value": "go"}})
    db.append_event(conn, session="main", actor="agent", kind="msg", op=k.OP, ref=k.OP,
                    data={"body": "decision NO-GO was discussed", "decision": {"value": "NO-GO"}})
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)


def real_nogo(project, session):
    """The real writer: reviewflow.decide on a failed review, op column NULL. Returns the event id."""
    from alpaca.runlog import reviewflow as rf
    from alpaca.runlog import quotes
    import os
    os.makedirs(os.path.join(project, "runs", "logs"), exist_ok=True)
    rid = "c" * 32
    with open(os.path.join(project, "runs", rid + ".json"), "w") as fh:
        json.dump({"result": "FAIL"}, fh)
    f = rf.Flow(log=lambda ref: quotes.log_path(os.path.join(project, "runs", "logs"), ref),
                folder=lambda ref: os.path.join(project, "runs", "reviews", ref),
                subject=lambda ref: {"result": "FAIL"}, locks=os.path.join(project, "runs", "locks"),
                kind="run-review", mark_kind="run-review-mark", workflow_kind=FLOW, refs=lambda: [rid])
    rf.assign(project, f, rid, parent_session=session, reviewer_session="worker")
    rf.decide(project, f, rid, None, "NO-GO", "Recover the log before any retry", session=session)
    conn = db.connect(project)
    ev = conn.execute("SELECT MAX(id) FROM events WHERE kind=? AND json_extract(data,'$.decision.value')"
                      "='NO-GO'", (FLOW,)).fetchone()[0]
    assert ev, "the fixture did not record a NO-GO"
    assert conn.execute("SELECT op FROM events WHERE id=?", (ev,)).fetchone()[0] is None
    return ev


def test_a_nogo_with_no_op_and_no_hold_blocks_the_ops_open_then(project, capsys):
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    ev = real_nogo(project, "main")
    out = k.owed(capsys)
    assert "event:%d" % ev in out and "unattributed" in out.lower(), out
    assert "blocks op-001, op-002" in out, out
    line = k.assert_blocked(conn, *k.close(capsys), events=(ev,))
    assert "aar owe <op> --event <id>" in line, line


def test_a_nogo_while_the_decider_holds_a_doing_task_owes_that_op(project, capsys):
    """done_bar[0]: the deciding session holds t-001 (op-001) in doing, so op-001 owes the report."""
    conn = k.two_ops(project)
    assert k.move("t-001", "doing", session="main") == 0
    ev = real_nogo(project, "main")
    assert "event:%d" % ev in k.owed(capsys, k.OP)
    assert "event:%d" % ev not in k.owed(capsys, k.OTHER)
    k.finish(project, conn, "t-002")
    k.assert_closed(conn, *k.close(capsys, k.OTHER), k.OTHER)
    k.finish(project, conn, "t-001", session="main")
    k.assert_blocked(conn, *k.close(capsys), events=(ev,))
    k.seal_aar(project, conn, capsys)
    k.assert_closed(conn, *k.close(capsys))


def test_a_nogo_by_a_shared_session_is_never_attributed(project, capsys):
    conn = k.two_ops(project)
    assert k.move("t-001", "doing", session="cli") == 0
    ev = real_nogo(project, "cli")
    from alpaca import aar
    assert {t["id"]: t["op"] for t in aar.triggers(conn, project)}[ev] is None
    assert "blocks op-001, op-002" in k.owed(capsys)


def test_a_nogo_by_a_session_holding_tasks_in_two_ops_blocks_both(project, capsys):
    conn = k.two_ops(project)
    assert k.move("t-001", "doing", session="main") == 0
    assert k.move("t-002", "doing", session="main") == 0
    ev = real_nogo(project, "main")
    assert "blocks op-001, op-002" in k.owed(capsys)


def test_a_hold_taken_after_the_nogo_does_not_attribute_it(project, capsys):
    conn = k.two_ops(project)
    ev = real_nogo(project, "main")
    assert k.move("t-001", "doing", session="main") == 0
    assert "blocks op-001, op-002" in k.owed(capsys)


def test_a_nogo_on_a_project_with_no_review_flow_profile_does_not_crash_the_close(project, capsys,
                                                                                  monkeypatch):
    """The generic harness wires no Flow: profile.load gives no review_flow_kinds. The gate reads
    nothing for NO-GO and the close goes on; it must not raise on the empty kind list (IN ())."""
    monkeypatch.setattr(profile, "load", lambda r: Fake(events=lambda: {}))
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)
