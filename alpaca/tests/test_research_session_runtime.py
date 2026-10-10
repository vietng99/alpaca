"""The session runtime of the research engine (design/research-engine/in-session-runtime.md):
the process model, the verbs next-job and submit, the guard against a headless agent, deadlines,
stop, crash and resume, spend, and the start checks.

The scripted session (research_session_kit.py) plays the driving Claude Code session and its
subagents: it drives a real runner, in a thread of the test process, only through cli.main
(`research start`, `research next-job`, `research submit`), and its fake subagents write
transcripts in the Claude Code record shape and play the runtime's hooks through the real hook
handlers. No test starts an agent runtime or touches the network. Tests that run a round need
bubblewrap (the gates keep their jail) and skip without it.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time

import pytest

from alpaca import cli, db
from alpaca.gates import verdict as vc
from alpaca.research import (errors, events, jail, jobs, launcher, paths, runner, session_runtime)
from alpaca.research import report as research_report
from alpaca.tests import research_loopkit as kit
from alpaca.tests import research_session_kit as rk

needs_jail = pytest.mark.skipif(not jail.available()[0],
                                reason="bubblewrap is not available on this host")


@pytest.fixture(autouse=True)
def _no_thread_outlives_its_test():
    yield

    def _left():
        return [t.name for t in threading.enumerate()
                if t.name.startswith(("slot-", "verify-", "heartbeat-", "watch-", "runner-main-"))]
    # a `stop` verb sets the stop flag and returns; the runner's threads wind down a moment later,
    # longer on a loaded host. A thread that is still alive after the grace period is a leak.
    deadline = time.monotonic() + 30
    while _left() and time.monotonic() < deadline:
        time.sleep(0.2)
    left = _left()
    assert left == [], left


def _new_run(project, monkeypatch, *, rounds=1, slots=2, retries=1, usd=10, behavior=None, **kw):
    root = kit.make_project(project, rounds=rounds, slots=slots, retries=retries, usd=usd)
    # the report task seals the engine's own folder: the project holds one, as every real one does
    kit.write(root, "alpaca/keep.txt", "the engine folder of this test project\n")
    kit.git(root, "add", "-A")
    kit.git(root, "commit", "-q", "-m", "an engine folder")
    from alpaca.research import controls
    monkeypatch.setattr(controls, "burn_in", lambda conn, sha: {"state": "done", "run": "rr-0",
                                                                   "rounds": 2, "of": 2})
    session = rk.Session(monkeypatch, root, behavior=behavior, **kw)
    return root, session


def _winner(role, jobd):
    """Slot 0 of round 1 writes the target value, so the run finishes; the others lose."""
    if role in ("proposer", "implementer") and jobd.get("round") == 1:
        return {"value": 10 if jobd.get("slot") == 0 else 80}
    return {}


def _why(root, run, done=None):
    """What a failed assertion says about the run: the hold, the runner log and the refusals."""
    out = ["done: %r" % (done,)]
    try:
        out.append(open(paths.runner_log(root, run), encoding="utf-8").read()[-3000:])
    except OSError:
        pass
    conn = db.connect(root)
    try:
        for row in conn.execute("SELECT kind, data FROM events WHERE kind IN (?, ?, ?) ORDER BY id",
                                (events.HOLD, events.JOB_REFUSED, events.INFRA)).fetchall():
            out.append("%s %s" % (row["kind"], row["data"][:600]))
    finally:
        conn.close()
    return "\n".join(out)


class _Popen:
    """A recorder over subprocess.Popen: the argv of every process a test starts."""

    def __init__(self, monkeypatch):
        self.argv = []
        real = subprocess.Popen
        outer = self

        class Rec(real):
            def __init__(self, args, *a, **kw):
                outer.argv.append(args if isinstance(args, (list, tuple)) else [args])
                super().__init__(args, *a, **kw)
        monkeypatch.setattr(subprocess, "Popen", Rec)

    def agent_starts(self):
        out = []
        for argv in self.argv:
            names = [os.path.basename(str(a)) for a in argv[:3]]
            text = " ".join(str(a) for a in argv)
            if any(n in ("claude", "codex") for n in names) or "codex exec" in text \
                    or " -p " in " %s " % text and "claude" in text:
                out.append(argv)
        return out


# ================================================================ the scripted round end to end
@needs_jail
def test_scripted_round_starts_no_agent_process(project, monkeypatch):
    """One whole run through the new verbs: jobs out, scripted subagent results with transcripts in,
    gates, fingerprints, audit and both verifiers deciding, and the report task verified; and no
    process named claude or codex starts."""
    popen = _Popen(monkeypatch)
    root, session = _new_run(project, monkeypatch, behavior=_winner)
    body = session.start()
    run = body["run"]
    done = session.drive(run, timeout=900)
    assert done["action"] == "report", _why(root, run, done)
    # next-job answered `report` once, as its last answer, and only after the report task's two
    # verifier jobs had been handed and submitted by the same loop
    actions = [a for _rc, a in session.log]
    assert actions.count("report") == 1 and actions[-1] == "report", actions
    # every launch of the run is a session launch
    launches = kit.events_of(root, events.LAUNCH)
    assert launches and {e["data"]["mode"] for e in launches if e["data"].get("run") == run} \
        == {"session"}
    assert popen.agent_starts() == []
    assert not os.path.exists(os.path.join(root, ".alpaca", "research", "headless-refusals.log"))
    # the deciding rows were written by the runner's own steps
    for kind in (events.GATE, events.TAMPER, events.ACCEPT, events.AUDIT_VERDICT,
                 events.PROOF_VERDICT, events.ROUND):
        assert kit.events_of(root, kind), kind
    # the report task's verification followed the finalize event: two more verifier jobs for the
    # report task, a group of two with the run as its scope, submitted by the same loop
    fin = kit.events_of(root, events.FINALIZE, run)[-1]
    assert fin["data"]["state"] == "finished"
    job_events = kit.events_of(root, events.JOB)
    by_role = {}
    for e in job_events:
        by_role.setdefault(e["data"]["role"], []).append(e)
    assert {r: len(v) for r, v in by_role.items()} == {"proposer": 2, "implementer": 2, "notes": 2,
                                                        "auditor": 2, "verifier": 8}, \
        {r: len(v) for r, v in by_role.items()}
    start = kit.events_of(root, events.RUN_START, run)[0]["data"]
    report_task = start["report_task"]
    report_jobs = [e for e in by_role["verifier"] if e["ref"] == report_task]
    assert len(report_jobs) == 2 and all(e["id"] > fin["id"] for e in report_jobs)
    assert {e["data"]["scope"] for e in job_events} == {run}
    conn = db.connect(root)
    try:
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (report_task,)).fetchone()[0] \
            == "done"
    finally:
        conn.close()
    # the audit row and the marker removal come after the report verification
    ends = kit.events_of(root, events.VERIFY_END, report_task)
    audits = kit.events_of(root, events.DRIVER_AUDIT, run)
    assert ends and ends[-1]["data"]["outcome"] == "verified"
    assert len(audits) == 1 and audits[0]["id"] > ends[-1]["id"]
    # a round whose driver spawned nothing beyond its jobs says so (R5)
    assert audits[0]["data"]["beyond"] == {"count": 0, "strays": [], "other": 0}
    assert not os.path.exists(session_runtime.live_path(root, session.sid))
    # no job was handed twice, every accepted job has its own agent, and every end is priced
    handed = kit.events_of(root, events.JOB_HANDED)
    assert len({e["data"]["job"] for e in handed}) == len(handed) == len(job_events)
    results = kit.events_of(root, events.JOB_RESULT)
    assert len(results) == len(job_events)
    assert len({r["data"]["agent_id"] for r in results}) == len(results)
    assert all(r["data"]["measured_usage"]["priced"] and not r["data"]["unpriced_models"]
               for r in results)
    # the report names the jobs, the agents, the driver and the driver audit, from events alone
    conn = db.connect(root)
    try:
        built = research_report.build(conn, root, run)
    finally:
        conn.close()
    jobs = built["jobs"]
    assert jobs["judged"]["accepted"] == len(results) and jobs["judged"]["refused"] == 0
    assert set(jobs["agent_ids"]) == {r["data"]["agent_id"] for r in results}
    assert jobs["drivers"] and jobs["drivers"][0]["session"] == session.sid
    assert len(jobs["driver_audit"]) == 1
    md = research_report.render(built)
    assert "## Agent jobs" in md and "driver audit" in md
    # no job's folder is, or holds, a root of the files the runtime loads into an agent (section
    # 12.6): that is what keeps the own-paths rule of K8 from accepting a planted file
    from alpaca.research import session_engine as eng
    cfg = os.environ["CLAUDE_CONFIG_DIR"]
    roots = [os.path.realpath(r) for r in eng.context_root_paths(root, cfg)]
    for e in job_events:
        d = e["data"]
        for p in [d.get("cwd")] + list(d.get("read_paths") or []) + list(d.get("write_paths") or []):
            if not p:
                continue
            real = os.path.realpath(p)
            assert not any(eng.under(real, r) or eng.under(r, real) for r in roots), (e["data"]["job"], p)
    # the pair of each group ran at the same time: both start events precede both stop events
    for group in {e["data"]["group"] for e in by_role["verifier"]}:
        members = [e["data"]["job"] for e in by_role["verifier"] if e["data"]["group"] == group]
        starts = [session_ts(root, "subagent-start", r["data"]["agent_id"]) for r in results
                  if r["data"]["job"] in members]
        stops = [session_ts(root, "subagent-stop", r["data"]["agent_id"]) for r in results
                 if r["data"]["job"] in members]
        assert max(starts) < min(stops), group


def session_ts(root, kind, agent_id):
    """The record id of the hook event of one agent."""
    conn = db.connect(root)
    try:
        for row in conn.execute("SELECT id, data FROM events WHERE kind=? ORDER BY id", (kind,)):
            if json.loads(row["data"]).get("agent_id") == agent_id:
                return row["id"]
    finally:
        conn.close()
    raise AssertionError("no %s event for %s" % (kind, agent_id))
