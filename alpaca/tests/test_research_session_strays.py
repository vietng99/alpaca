"""Check round 2 of op-009 t-173, adversarial lens: the two attacks that spend or choose a draw
through the driver (in-session-runtime.md section 12.1, A37 and A38; section 12.2, R5).

A37. A driver that read a refuting verifier verdict files a claim built to fail, or none. The
job ends refused or expired with no launch end, and the verifier's judge used to record
VERIFY-NO-VERDICT with no refutation, so the refutation was neither counted against LOST_MAX nor
kept as dropped. It now returns VERIFY-LOST with the refutation of the last served submission.

A38. A twin agent of a job still runs when the judge decides. K14 reads stopped agents only, so
the twin was never booked and `status` had no count of it. `extra_agents` lists the driver's
agents beyond the jobs, `reconcile` prices a stray that stopped, and `status`, the driver audit
and the report carry the count.
"""
from __future__ import annotations

import json
import types

import pytest

from alpaca import db
from alpaca.research import cli, events, jobs, report, spend, verdicts, verifier
from alpaca.tests import research_session_kit as rk


# ---------------------------------------------------------------------------- A37
def _ledger(*bodies):
    return {"requests": {n: {"kind": "submit", "body": json.dumps(b), "line": "ok"}
                         for n, b in enumerate(bodies, 1)}}


REFUTING = {"claims": [{"id": "c1", "verdict": "refuted"}, {"id": "c2", "verdict": "supported"}]}
LOST = {"outcome": "lost", "infra": "lost", "launch_end": None}


def _ctx(project):
    return {"folder": str(project), "error": None, "scope": [{"id": "c1"}, {"id": "c2"}],
            "runtime": "claude"}


def test_a37_a_refuting_submission_of_a_job_with_no_launch_end_is_a_lost_try_with_a_dropped_refutation(
        project):
    conn = db.connect(project)
    try:
        got = verifier._judge(conn, str(project), _ctx(project), LOST, _ledger(REFUTING))
    finally:
        conn.close()
    assert got[0] == "refused" and got[1] == "VERIFY-LOST"
    assert got[3] == ["c1"]


def test_a37_the_last_served_submission_decides_what_is_dropped(project):
    supported = {"claims": [{"id": "c1", "verdict": "supported"}]}
    conn = db.connect(project)
    try:
        got = verifier._judge(conn, str(project), _ctx(project), LOST,
                              _ledger(REFUTING, supported))
    finally:
        conn.close()
    assert got[1] == "VERIFY-LOST" and got[3] == []


def test_a37_a_supporting_submission_of_a_lost_job_is_still_a_lost_try(project):
    conn = db.connect(project)
    try:
        got = verifier._judge(conn, str(project), _ctx(project), LOST,
                              _ledger({"claims": [{"id": "c1", "verdict": "supported"}]}))
    finally:
        conn.close()
    assert got[1] == "VERIFY-LOST" and got[3] == []


def test_a37_control_a_job_with_no_launch_end_and_no_submission_is_no_verdict(project):
    conn = db.connect(project)
    try:
        got = verifier._judge(conn, str(project), _ctx(project), LOST, {"requests": {}})
        again = verifier._judge(conn, str(project), _ctx(project), LOST, None)
    finally:
        conn.close()
    assert got[1] == "VERIFY-NO-VERDICT" and again[1] == "VERIFY-NO-VERDICT"
    assert got[3] == [] and again[3] == []


@pytest.mark.parametrize("ledger,counted", [(_ledger(REFUTING), 1), ({"requests": {}}, 0)])
def test_a37_the_lost_bound_counts_the_vetoed_try_and_not_a_try_that_submitted_nothing(
        project, ledger, counted):
    conn = db.connect(project)
    try:
        got = verifier._judge(conn, str(project), _ctx(project), LOST, ledger)
        verifier._refuse(conn, "t-1", None, "s", "vf-1", got[1], got[2], slot="v1", rnd=0, try_=1,
                         extra={"refuted": got[3]})
        assert len(verdicts.lost_and_dropped(conn, "vf-1")) == counted
    finally:
        conn.close()


# ---------------------------------------------------------------------------- A38
@pytest.fixture
def world(project, monkeypatch):
    w = rk.JudgeWorld(monkeypatch, project)
    yield w
    w.close()


def _job(w):
    jid = w.issue("implementer")
    w.hand()
    return jid


def _accepted_with_twin(w, *, twin_stops: bool):
    jid = _job(w)
    first = w.run_agent(jid)
    twin = w.agent(jid)
    twin.start()
    twin.work()
    twin.write()                       # running: a transcript, no stop
    n = w.claim(jid, first)
    c, p = w.judge(jid, n)
    assert p is None, p
    w.accept(c)
    if twin_stops:
        twin.stop()
    return jid, first, twin


def _late_rows(root):
    conn = db.connect(root)
    try:
        return [json.loads(r["data"]) for r in conn.execute(
            "SELECT data FROM events WHERE kind=? ORDER BY id", (events.JOB_LATE,)).fetchall()]
    finally:
        conn.close()


def test_a38_a_twin_that_stops_after_the_accepted_result_is_listed_priced_and_booked(world):
    w = world
    jid, first, twin = _accepted_with_twin(w, twin_stops=False)
    assert jobs.extra_agents(w.conn, w.SCOPE) == []          # still running: no stop to read
    twin.stop()
    listed = jobs.extra_agents(w.conn, w.SCOPE)
    assert [(x["agent_id"], x["job"]) for x in listed] == [(twin.agent_id, jid)]
    booked = lambda: w.conn.execute(          # noqa: E731
        "SELECT COUNT(*) FROM events WHERE kind='budget-spend' AND ref=?", (w.attempt,)).fetchone()[0]
    before = booked()
    done = jobs.reconcile(w.conn, w.root, w.SCOPE, session=w.SCOPE)
    assert done == [{"job": jid, "agent_id": twin.agent_id, "stray": True}]
    late = _late_rows(w.root)
    assert [(d["agent_id"], d["stray"], d["job"]) for d in late] == [(twin.agent_id, True, jid)]
    assert late[0]["priced"] is True and late[0]["tokens"] > 0
    assert booked() == before + 1                # the twin's cost is booked under the job's attempt
    assert jobs.extra_agents(w.conn, w.SCOPE) == []          # booked once, not listed again
    assert jobs.reconcile(w.conn, w.root, w.SCOPE, session=w.SCOPE) == []
    assert len(_late_rows(w.root)) == 1


@pytest.mark.parametrize("stray,unpriced", [(True, 1), (False, 0)])
def test_a38_a_stray_row_adds_tokens_and_does_not_price_the_key_of_the_job_itself(project, stray,
                                                                                  unpriced):
    conn = db.connect(project)
    try:
        run, key = "rr-001", "t-900/implementer/t1"
        events.emit(conn, events.LAUNCH, run=run, ref="t-900", op=None, session="s",
                    data={"key": key, "runtime": "claude", "max_usd": 1.0})
        events.emit(conn, events.INFRA, run=run, ref="t-900", op=None, session="s", code=2,
                    data={"key": key, "class": "lost", "job": "j-1"})
        events.emit(conn, events.JOB_LATE, run=run, ref="t-900", op=None, session="s",
                    data={"job": "j-1", "scope": run, "key": key, "agent_id": "a1", "priced": True,
                          "tokens": 50, "cost_usd": 0.1, **({"stray": True} if stray else {})})
        got = spend.totals(conn, run)
    finally:
        conn.close()
    assert got["tokens_spent"] == 50 and got["unpriced"] == unpriced


def test_a38_the_first_agent_and_a_job_still_open_are_not_beyond_anything(world):
    w = world
    jid = _job(w)
    a = w.run_agent(jid)
    other = w.agent(jid)               # a second agent of a job that is still open
    other.start()
    other.work()
    other.write()
    other.stop()
    assert jobs.extra_agents(w.conn, w.SCOPE) == []          # the judge reads an open job's agents
    w.claim(jid, a)
    assert jobs.extra_agents(w.conn, w.SCOPE) == []


def test_a38_an_agent_that_names_no_job_is_counted_and_not_priced(world, tmp_path):
    w = world
    path = tmp_path / "agent-own.jsonl"
    path.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "explore"}})
                    + "\n")
    from alpaca.hooks import subagent_stop
    subagent_stop.handle({"session_id": w.sid, "agent_id": "aown1", "agent_type": "Explore",
                          "cwd": w.root, "agent_transcript_path": str(path)})
    listed = jobs.extra_agents(w.conn, w.SCOPE)
    assert [(x["agent_id"], x["job"]) for x in listed] == [("aown1", None)]
    assert jobs.reconcile(w.conn, w.root, w.SCOPE, session=w.SCOPE) == []
    assert _late_rows(w.root) == []


def test_a38_status_lists_the_count_of_agents_beyond_the_jobs(world, tmp_path):
    w = world
    jid, first, twin = _accepted_with_twin(w, twin_stops=True)
    v = types.SimpleNamespace(conn=w.conn, root=w.root)
    fold = {"driver": {"session": w.sid, "runtime": "claude"}}
    body = cli._session_status(v, w.SCOPE, fold, {})
    assert body["beyond"] == {"count": 1, "strays": [{"agent_id": twin.agent_id, "job": jid}],
                              "other": 0}
    lines = cli._session_status_lines(dict(fold, **body))
    assert any("agents beyond the jobs: 1 (1 stray of a judged job, 0 not a job)" in ln
               for ln in lines), lines
    jobs.reconcile(w.conn, w.root, w.SCOPE, session=w.SCOPE)
    assert cli._session_status(v, w.SCOPE, fold, {})["beyond"]["count"] == 0


def test_a38_the_report_names_the_stray_agents_priced_after_the_judge(world):
    w = world
    jid, first, twin = _accepted_with_twin(w, twin_stops=True)
    jobs.reconcile(w.conn, w.root, w.SCOPE, session=w.SCOPE)
    rows = [{"kind": events.JOB_LATE, "id": 1, "data": d} for d in _late_rows(w.root)]
    got = report._jobs_summary(rows)
    assert got["strays"] == [twin.agent_id] and twin.agent_id in got["agent_ids"]
    # a run with no stray carries no key: the report of such a run is unchanged
    assert "strays" not in report._jobs_summary(
        [{"kind": events.JOB_RESULT, "id": 2, "data": {"agent_id": "a1", "outcome": "ok"}}])


def test_a38_the_driver_audit_row_carries_the_count_beyond_the_jobs(world):
    from alpaca.research import session_runtime
    w = world
    jid, first, twin = _accepted_with_twin(w, twin_stops=True)
    row = session_runtime.driver_audit(w.conn, w.root, w.sid, None, scope=w.SCOPE)
    assert row["beyond"] == {"count": 1, "strays": [twin.agent_id], "other": 0}
    got = report._jobs_summary([{"kind": events.DRIVER_AUDIT, "id": 9,
                                 "data": {"session": w.sid, "findings": [], "beyond": row["beyond"]}}])
    assert got["driver_audit"][0]["beyond"] == 1
