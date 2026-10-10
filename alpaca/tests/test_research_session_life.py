"""Deadlines, stop, the driver watch and the engine watch of a session run
(design/research-engine/in-session-runtime.md sections 10 and 16, tests 9, 10 and 17).

A real runner runs in a thread of the test process; the scripted driving session hands out the first
jobs and then does what the test says: stops the run, ends the session, or touches a pinned file.
Each test ends with no runner thread left, which the autouse fixture of the runtime tests checks.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from alpaca import db
from alpaca.gates import verdict as vc
from alpaca.research import events, jobs, paths, session_runtime
from alpaca.tests import research_session_kit as rk
from alpaca.tests.test_research_session_runtime import (_new_run, _no_thread_outlives_its_test,  # noqa: F401
                                                        _why, needs_jail)


@pytest.fixture(autouse=True)
def _fast_watch(monkeypatch):
    monkeypatch.setattr(session_runtime, "WATCH_S", 0.2)


def _wait(session, run, states, timeout=120.0):
    """Poll `research status` until the run's state is one of `states`."""
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        rc, body = session.verb("status", run)
        info = (body or {}).get("run") or {}
        last = info.get("state")
        if last in states:
            return info
        time.sleep(0.2)
    raise AssertionError("run %s is %s, not one of %s" % (run, last, states))


def _handed(session, run):
    """The jobs of the first next-job answer that spawns something: handed, nobody working."""
    end = time.monotonic() + 120
    while time.monotonic() < end:
        rc, body = session.next_jobs(run, wait=1.0)
        if body and body["action"] == "spawn" and body["jobs"]:
            return body["jobs"]
    raise AssertionError("no job was handed in time")


def _events(root, kind):
    return rk.events_of(root, kind)


def _live(root, session):
    return os.path.exists(session_runtime.live_path(root, session.sid))


@needs_jail
def test_stop(project, monkeypatch):
    root, session = _new_run(project, monkeypatch, behavior=lambda role, jobd: {})
    run = session.start()["run"]
    entries = _handed(session, run)
    assert _live(root, session)
    # the agents did their jobs and ended, and the driver has not submitted yet: the stop expires
    # the open jobs, and the agents that ran are priced by the reconcile, never judged
    agents = session.spawn(entries, run)
    rc, body = session.verb("stop", run, "--reason", "test stop")
    assert rc == vc.PASS, body
    stopped = _wait(session, run, ("stopped",))
    assert stopped["state"] == "stopped"
    expired = _events(root, events.JOB_EXPIRED)
    assert {e["data"]["job"] for e in expired} >= {e["job"] for e in entries}
    assert {e["data"]["reason"] for e in expired} <= {"stopped", "finalize"}
    # an expired job counts as a lost try, never as a result
    assert _events(root, events.JOB_RESULT) == []
    # the run is over: next-job answers report at once and a late claim is refused
    rc, body = session.next_jobs(run)
    assert body["action"] == "report", body
    first = entries[0]
    rc, body = session.verb("submit", first["job"], "--nonce", first["nonce"],
                            "--agent-id", agents[0].agent_id)
    assert rc != vc.PASS and body["code"] in ("JOB-STATE", "JOB-EXPIRED"), body
    assert _events(root, events.JOB_RESULT) == []
    # the marker is gone once the run has finalized
    end = time.monotonic() + 30
    while _live(root, session) and time.monotonic() < end:
        time.sleep(0.2)
    assert not _live(root, session)


@needs_jail
def test_driver_watch(
        project, monkeypatch):
    root, session = _new_run(project, monkeypatch, behavior=lambda role, jobd: {})
    run = session.start()["run"]
    entries = _handed(session, run)
    session.spawn(entries, run)         # the agents ran and ended; the driver files nothing
    conn = db.connect(root)
    try:
        db.append_event(conn, session=session.sid, actor="hook", kind="session-end", data={})
    finally:
        conn.close()
    body = _wait(session, run, ("interrupted",))
    assert body["state"] == "interrupted"
    marker = os.path.join(paths.run_dir(root, run), "driver-gone.json")
    assert os.path.isfile(marker)
    expired = _events(root, events.JOB_EXPIRED)
    assert {e["data"]["job"] for e in expired} >= {e["job"] for e in entries}
    assert {e["data"]["reason"] for e in expired} >= {"driver-gone"}
    # next-job of the same session now says resume; status names the code
    rc, nxt = session.next_jobs(run)
    assert nxt["action"] == "resume", nxt
    fin = _events(root, events.FINALIZE)
    assert fin and fin[-1]["data"]["state"] == "interrupted" and fin[-1]["data"]["code"] == "RUN-DRIVER-GONE"
    # the expiry did not spend the tries of the verification that was open, and no audit was
    # blocked: a run that is ending leaves both resumable (the launch returns killed, the retry
    # loops of the verifier and the audit see `stopping` and record nothing)
    assert _events(root, events.VERIFY_END) == [] and _events(root, events.VERIFY_REFUSED) == []
    assert _events(root, events.AUDIT_VERDICT) == [] and _events(root, events.PROOF_VERDICT) == []


@needs_jail
def test_a_touched_engine_file_ends_the_run_interrupted_and_resume_refuses(project, monkeypatch):
    root, session = _new_run(project, monkeypatch, behavior=lambda role, jobd: {})
    run = session.start()["run"]
    session.spawn(_handed(session, run), run)
    conn = db.connect(root)
    try:
        pin = jobs.pin_of(jobs.newest_pin(conn, run, run))
    finally:
        conn.close()
    from alpaca.research import session_engine as eng
    install = os.path.realpath(eng.install_root())
    target = os.path.join(install, "bin", "alpaca")
    assert target in pin["files"], "the pin holds no bin/alpaca"
    os.utime(target, None)                     # a touch: the content is the same, the ctime is not
    _wait(session, run, ("interrupted",))
    assert os.path.isfile(os.path.join(paths.run_dir(root, run), "engine-changed.json"))
    # the next call of the driver is told to resume, and `resume` refuses until the owner's repin
    rc, nxt = session.next_jobs(run)
    assert nxt["action"] == "resume", nxt
    rc, body = session.verb("resume", run)
    assert rc != vc.PASS and body["code"] == "RUN-ENGINE-CHANGED", body


def _driver_snapshot(monkeypatch):
    """A synthetic stored snapshot in the test's config folder, which register_driver stores."""
    from alpaca.research import session_engine as eng
    snap = os.path.join(eng.config_dir(), "shell-snapshots", "snapshot-bash-1700000000801-drvL08.sh")
    os.makedirs(os.path.dirname(snap), exist_ok=True)
    with open(snap, "w", encoding="utf-8") as fh:
        fh.write("export PATH=/usr/bin\n")
    monkeypatch.setattr(eng, "find_driver_snapshot", lambda pid=None: snap)
    return snap


def _graces(monkeypatch):
    """Record each call of the watch's one-look grace and what it answered."""
    seen, real = [], jobs.snapshot_wait

    def spy(bad, drv, state):
        got = real(bad, drv, state)
        seen.append(([c["path"] for c in bad], got))
        return got
    monkeypatch.setattr(jobs, "snapshot_wait", spy)
    return seen


@needs_jail
def test_an_in_process_removal_of_the_driver_snapshot_ends_the_run_at_the_watch(project, monkeypatch):
    """t-360 check round 2 N1: the driver's CLI lives (no restart row) and its stored snapshot is
    removed; the CLI then runs a login shell. The watch waits one look for a restart row, then ends
    the run interrupted with the context marker, and resume refuses until the owner's repin."""
    root, session = _new_run(project, monkeypatch, behavior=lambda role, jobd: {})
    snap = _driver_snapshot(monkeypatch)
    seen = _graces(monkeypatch)
    run = session.start()["run"]
    session.spawn(_handed(session, run), run)
    os.remove(snap)
    _wait(session, run, ("interrupted",))
    body = eng_read(paths.run_dir(root, run))
    assert body.get("kind") == "context", body
    assert snap in [c["path"] for c in body.get("changed") or []], body
    assert not os.path.isfile(os.path.join(paths.run_dir(root, run), "driver-gone.json"))
    assert seen[:2] == [([snap], True), ([snap], False)], seen
    rc, nxt = session.next_jobs(run)
    assert nxt["action"] == "resume", nxt
    rc, body = session.verb("resume", run)
    assert rc != vc.PASS and body["code"] == "RUN-ENGINE-CHANGED", body


@needs_jail
def test_the_driver_snapshot_removal_at_a_restart_ends_the_run_driver_gone(project, monkeypatch):
    """The measured restart: the CLI removes its snapshot at exit and the hook writes session-end;
    the run ends RUN-DRIVER-GONE with no context marker, so the resume needs no repin."""
    root, session = _new_run(project, monkeypatch, behavior=lambda role, jobd: {})
    snap = _driver_snapshot(monkeypatch)
    run = session.start()["run"]
    session.spawn(_handed(session, run), run)
    os.remove(snap)
    conn = db.connect(root)
    try:
        db.append_event(conn, session=session.sid, actor="hook", kind="session-end", data={})
    finally:
        conn.close()
    _wait(session, run, ("interrupted",))
    assert os.path.isfile(os.path.join(paths.run_dir(root, run), "driver-gone.json"))
    assert not os.path.isfile(os.path.join(paths.run_dir(root, run), "engine-changed.json"))
    fin = _events(root, events.FINALIZE)
    assert fin and fin[-1]["data"]["code"] == "RUN-DRIVER-GONE", fin[-1:]


def eng_read(run_dir):
    from alpaca.research import session_engine as eng
    return eng.read_json(os.path.join(run_dir, "engine-changed.json")) or {}


@needs_jail
def test_crash_resume(project, monkeypatch):
    """The same session types `resume` after a later session-start: the open jobs had expired, the
    retry of each slot is a new job with a fresh nonce, and the driver row says `resume`."""
    root, session = _new_run(project, monkeypatch, behavior=lambda role, jobd: {})
    run = session.start()["run"]
    first = _handed(session, run)
    session.spawn(first, run)
    conn = db.connect(root)
    try:
        db.append_event(conn, session=session.sid, actor="hook", kind="session-end", data={})
    finally:
        conn.close()
    _wait(session, run, ("interrupted",))
    conn = db.connect(root)
    try:
        db.append_event(conn, session=session.sid, actor="hook", kind="session-start", data={})
    finally:
        conn.close()
    # the preflight of a session run resolves no runtime binary: a driver with no `claude` on its
    # PATH can resume (test_resume_needs_no_binary)
    from alpaca.research import launcher

    def no_binary(*a, **kw):
        raise AssertionError("resume resolved a runtime binary")
    monkeypatch.setattr(launcher, "resolve_binary", no_binary)
    # the agents that ran before the driver left were priced, never judged: a late verifier job
    # writes research-job-late and no launch end, and the verification takes no verdict from it
    late = _events(root, events.JOB_LATE)
    assert {l["data"]["job"] for l in late} >= {e["job"] for e in first if e["role"] == "verifier"}
    assert all(l["data"]["priced"] for l in late)
    ended = {e["data"].get("job") for e in _events(root, events.LAUNCH_END)}
    assert not ended & {e["job"] for e in first}
    rc, body = session.verb("resume", run)
    assert rc == vc.PASS, body
    drivers = [e for e in _events(root, events.DRIVER) if e["ref"] == run]
    assert drivers[-1]["data"]["cause"] == "resume" and len(drivers) == 2
    try:
        second = _handed(session, run)
    except AssertionError:
        import sys
        conn = db.connect(root)
        for r in conn.execute("SELECT id, kind, ref, substr(data,1,260) d FROM events WHERE id>60 ORDER BY id"):
            print("DBG", r["id"], r["kind"], r["ref"], r["d"], file=sys.stderr)
        raise AssertionError(_why(root, run))
    old_jobs = {e["job"] for e in first}
    assert old_jobs.isdisjoint({e["job"] for e in second}), (old_jobs, [e["job"] for e in second])
    # a fresh nonce for the same slot's retry, and the expired job's own nonce is no key to the new one
    assert {e["nonce"] for e in first}.isdisjoint({e["nonce"] for e in second})
    assert all(e["job"].endswith("t2") for e in second if e["role"] == "proposer"), \
        [e["job"] for e in second]
    rc, body = session.verb("stop", run, "--reason", "test over")
    assert rc == vc.PASS, body
    _wait(session, run, ("stopped",))


@needs_jail
def test_unpriced_result_holds_with_a_fix(project, monkeypatch):
    """A subagent ran on a model of the job's family that has no rate card: its end is booked unpriced
    with the model named, the next launch holds on the budget, and `next-job` answers `hold` with the
    command that records the card (section 9)."""
    root, session = _new_run(project, monkeypatch, behavior=lambda role, jobd: {},
                             model_of=lambda role, jobd: "claude-sonnet-9" if role == "verifier"
                             and jobd.get("verifier_slot") == "v1" else None)
    run = session.start()["run"]
    done = session.drive(run, timeout=300, until=lambda body: body["action"] in ("hold", "report"))
    assert done["action"] == "hold", done
    hold = done["hold"]
    assert hold["gate"] == "budget" and hold["code"] == "RUN-BUDGET-HOLD"
    assert any("price-check" in cmd and "claude-sonnet-9" in cmd for cmd in hold["fix"]), hold
    results = [r for r in _events(root, events.JOB_RESULT) if r["data"].get("unpriced_models")]
    assert results and results[0]["data"]["unpriced_models"][0]["model"] == "claude-sonnet-9"
    assert results[0]["data"]["measured_usage"]["priced"] is False
    rc, body = session.verb("stop", run, "--reason", "test over")
    assert rc in (vc.PASS, vc.PAUSED), body


@needs_jail
def test_resume_by_a_new_session_takes_its_own_baseline(project, monkeypatch):
    """The session that drove the run ended. A new session, with its own transcript and its own
    folder of the runtime, types `resume`: the checks pass, the new driver row carries the new
    session, and the old session is no longer the one `next-job` answers."""
    root, session = _new_run(project, monkeypatch, behavior=lambda role, jobd: {})
    run = session.start()["run"]
    session.spawn(_handed(session, run), run)
    conn = db.connect(root)
    try:
        db.append_event(conn, session=session.sid, actor="hook", kind="session-end", data={})
    finally:
        conn.close()
    _wait(session, run, ("interrupted",))
    new_sid = "new0-1111-2222-3333-444455556666"
    new = rk.Session(monkeypatch, root, sid=new_sid, behavior=lambda role, jobd: {})
    rc, body = new.verb("resume", run)
    assert rc == vc.PASS, body
    drivers = [e for e in _events(root, events.DRIVER) if e["ref"] == run]
    assert drivers[-1]["data"]["session"] == new_sid and drivers[-1]["data"]["cause"] == "resume"
    from alpaca.research import session_engine as eng
    assert eng.load_ledger(drivers[-1]["data"]["context"])["driver_session"] == new_sid
    assert eng.load_ledger(drivers[0]["data"]["context"])["driver_session"] == session.sid
    # the new session is the driver; the old one is refused by the verbs
    rc, body = session.verb("next-job", run, "--wait", "0")
    assert rc != vc.PASS and body["code"] == "JOB-NOT-DRIVER", body
    assert _handed(new, run)
    rc, body = new.verb("stop", run, "--reason", "test over")
    assert rc == vc.PASS, body
    _wait(new, run, ("stopped",))
