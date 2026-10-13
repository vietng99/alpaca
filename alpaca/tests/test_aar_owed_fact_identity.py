"""A09 (cause C1): event identity is not fact identity. Two writers append the same fact again with a
new event id:
- reviewflow re-saves the stored dict, decision included, on takeover and renew (reviewflow.py
  _save writes dict(data, ...) of the newest event of the ref);
- release.record_receipt re-appends a release-check event when the same receipt file is recorded
  again with identical content.
A gate that counts rows sees the copy after the seal and blocks a close that was already paid.
A trigger is the fact, dated by its first event: NO-GO key (kind, ref, decision.decided_at),
release FAIL key (ref, sha256)."""
import pytest

from alpaca import db, profile
from alpaca.release import receipts as rc
from alpaca.tests import aarowedkit as k

FLOW = "run-review-flow"
W = "w-release-1"


class Fake(profile.Profile):
    def __init__(self, **hooks):
        self.__dict__.update(hooks)


@pytest.fixture(autouse=True)
def flow_profile(monkeypatch):
    monkeypatch.setattr(profile, "load", lambda r: Fake(events=lambda: {"review_flow_kinds": [FLOW]}))


def test_a_copied_nogo_after_the_seal_does_not_owe_again(project, capsys):
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    data = {"decision": {"value": "NO-GO", "session": "main", "decided_at": "2026-10-13T10:00:00+00:00",
                         "rationale": "the run exited before step two"}, "receipt": "j-1"}
    first = db.append_event(conn, session="main", actor="agent", kind=FLOW, op=k.OP, ref="j-1", data=data)["id"]
    k.seal_aar(project, conn, capsys)
    takeover = dict(data, assignment={"reviewer_session": "w2", "expires_at": 0})
    copy = db.append_event(conn, session="main", actor="agent", kind=FLOW, op=k.OP, ref="j-1",
                           data=takeover)["id"]
    out = k.owed(capsys, k.OP)
    assert "event:%d" % copy not in out and "event:%d" % first not in out, out
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)


def test_a_new_nogo_on_the_same_run_after_the_seal_owes_again(project, capsys):
    """The sibling the key must not swallow: a second decision (new decided_at) is a new fact."""
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    data = {"decision": {"value": "NO-GO", "session": "main", "decided_at": "2026-10-13T10:00:00+00:00",
                         "rationale": "the run exited before step two"}, "receipt": "j-1"}
    db.append_event(conn, session="main", actor="agent", kind=FLOW, op=k.OP, ref="j-1", data=data)
    k.seal_aar(project, conn, capsys)
    again = {"decision": dict(data["decision"], decided_at="2026-10-13T12:00:00+00:00",
                              rationale="the retry failed the same way"), "receipt": "j-1"}
    ev = db.append_event(conn, session="main", actor="agent", kind=FLOW, op=k.OP, ref="j-1", data=again)["id"]
    code, out = k.close(capsys)
    k.assert_blocked(conn, code, out, events=(ev,))


def test_the_same_fail_receipt_recorded_twice_is_one_trigger(project, capsys):
    """The copy lands while the same session holds another task of the same op, so a row counter
    would attribute it to op-001 again; the fact key (ref, sha256) says it is the paid one."""
    conn = k.two_ops(project)
    assert k.cli.main(["task", "add", "--title", "more", k.OP, "a second task", "--phase", "build"]) == 0
    assert k.move("t-001", "doing", session=W) == 0
    receipt = rc.CheckReceipt(build={}, gates=[], status="FAIL")
    rc.record_receipt(project, W, receipt)
    first = k.last_id(conn, "release-check")
    k.finish(project, conn, "t-001", session=W)
    k.seal_aar(project, conn, capsys)
    assert k.move("t-003", "doing", session=W) == 0
    rc.record_receipt(project, W, receipt)                 # identical content: a second event
    copy = k.last_id(conn, "release-check")
    assert copy != first
    k.finish(project, conn, "t-003", session=W)
    assert "event:%d" % copy not in k.owed(capsys, k.OP)
    code, out = k.close(capsys)
    k.assert_closed(conn, code, out)
