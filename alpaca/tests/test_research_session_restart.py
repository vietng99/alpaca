"""The three runner starts of a session run: `extend`, `resume` and `grant` (in-session-runtime.md
sections 3.4 and 16, test 19).

A session run is driven by the session that starts it. `extend` and `resume` make the start checks
first (the runtime marker, a subagent's shell, the level, the engine pin, the run's mode) so a
refusal changes nothing, and register the typing session as the driver right before the runner
starts. `grant` of an extension or a budget records the grant only, because the owner's terminal is
no driver. A run of a test project (runtime_mode test) restarts as before.
"""
from __future__ import annotations

import os

import pytest

from alpaca import cli
from alpaca.gates import verdict as vc
from alpaca.posture import level as level_mod
from alpaca.research import events, holds, owner, session_runtime
from alpaca.tests import research_session_kit as rk
from alpaca.tests.test_research_cli import (SESSION, _call, _conn, _ev, _finalize, _frozen, _grant,
                                            _hold, _start, rec)  # noqa: F401 - the fixture

DRIVER_KINDS = (events.DRIVER,)


def _session_run(rec, monkeypatch, capsys, mode="session"):
    """A run the fake runner records with `runtime_mode: <mode>`, the driving session's environment
    set (the runtime marker, a registered transcript, level L6), and the run's driver registered as
    `start` registers it. Mode None is a run recorded before the session runtime."""
    rec.fake.runtime_mode = mode
    rec.ctx = rk.prepare(monkeypatch, rec.root, sid=SESSION, level=6)
    _frozen(monkeypatch)
    rec.run = _start(capsys)
    conn = _conn(rec.root)
    try:
        session_runtime.register_driver(conn, rec.root, rec.run, SESSION, cause="start", level=6,
                                        runtime="claude", run=rec.run, op="op-001")
    finally:
        conn.close()
    return rec


@pytest.fixture
def srun(rec, monkeypatch, capsys):                       # noqa: F811
    return _session_run(rec, monkeypatch, capsys)


def _as_session(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)


def _grant_at_a_terminal(capsys, monkeypatch, run, *argv):
    code, body = _grant(capsys, monkeypatch, run, run, *argv)
    _as_session(monkeypatch)
    return code, body


def _drivers(root, run):
    return [e for e in _ev(root, events.DRIVER) if e["ref"] == run]


def test_extend_by_the_session_registers_the_driver(srun, capsys, monkeypatch):
    run = srun.run
    _hold(srun.root, run)
    code, body = _grant_at_a_terminal(capsys, monkeypatch, run, "extend", "--rounds", "4",
                                      "--no-apply")
    assert code == vc.PASS and body["grant"]["applied"] is False
    before = len(_drivers(srun.root, run))
    launches = list(srun.fake.launches)
    code, body = _call(capsys, "extend", run)
    assert code == vc.PASS, body
    drivers = _drivers(srun.root, run)
    assert len(drivers) == before + 1
    assert drivers[-1]["data"]["cause"] == "extend" and drivers[-1]["data"]["session"] == SESSION
    # the driver row is written before the runner starts, so the runner finds it as the newest
    ext, res = _ev(srun.root, events.EXTEND, run), _ev(srun.root, events.RESUME, run)
    assert len(ext) == 1 and len(res) == 1 and drivers[-1]["id"] < res[0]["id"]
    assert srun.fake.launches == launches + [(run, False)]


# a subagent's shell is not a refusal here: see test_extend_and_resume_pass_with_the_child_variable
REFUSALS = {
    "no-marker": lambda mp: [mp.delenv(n, raising=False)
                             for n in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")],
    "level-2": lambda mp: None,
    "engine-changed": lambda mp: mp.setattr(
        session_runtime, "engine_check",
        lambda *a, **kw: (_ for _ in ()).throw(session_runtime.errors.refusal(
            "RUN-ENGINE-CHANGED", "a pinned file changed"))),
    "headless-run": lambda mp: None,
}
CODES = {"no-marker": "RUN-RUNTIME-MISSING",
         "level-2": "RESEARCH-LEVEL-TOO-LOW", "engine-changed": "RUN-ENGINE-CHANGED",
         "headless-run": "RUN-HEADLESS-REFUSED"}


@pytest.mark.parametrize("verb", ["extend", "resume"])
@pytest.mark.parametrize("why", sorted(REFUSALS))
def test_extend_and_resume_make_the_start_checks_first(rec, capsys, monkeypatch, why, verb):
    # a run recorded before the session runtime has no runtime_mode: it is a headless run
    srun = _session_run(rec, monkeypatch, capsys, None if why == "headless-run" else "session")
    run = srun.run
    if verb == "extend":
        _hold(srun.root, run)
        code, _body = _grant_at_a_terminal(capsys, monkeypatch, run, "extend", "--rounds", "4",
                                           "--no-apply")
        assert code == vc.PASS
    else:
        _finalize(srun.root, run, "interrupted")
    if why == "level-2":
        conn = _conn(srun.root)
        try:
            level_mod.set(conn, SESSION, 2, "a lower level", root=srun.root)
        finally:
            conn.close()
    REFUSALS[why](monkeypatch)
    drivers, launches = len(_drivers(srun.root, run)), list(srun.fake.launches)
    code, body = _call(capsys, verb, run)
    assert code != vc.PASS and body["code"] == CODES[why], (code, body)
    # a refusal changes nothing: no extension, no resume, no driver row, no runner
    assert _ev(srun.root, events.EXTEND, run) == [] and _ev(srun.root, events.RESUME, run) == []
    assert len(_drivers(srun.root, run)) == drivers and srun.fake.launches == launches
    if verb == "extend":
        conn = _conn(srun.root)
        try:
            assert holds.open_holds(conn, run)
        finally:
            conn.close()


def test_grant_at_a_terminal_starts_no_runner_for_a_session_run(srun, capsys, monkeypatch):
    run = srun.run
    _hold(srun.root, run)
    launches = list(srun.fake.launches)
    drivers = len(_drivers(srun.root, run))
    code, body = _grant(capsys, monkeypatch, run, run, "extend", "--rounds", "4")
    assert code == vc.PASS, body
    assert body["grant"]["applied"] is False and "next" in body["grant"]
    assert "research extend" in body["grant"]["next"] and run in body["grant"]["next"]
    # the grant is on the record and nothing started: no runner, no extension, no driver
    assert srun.fake.launches == launches
    assert _ev(srun.root, events.EXTEND, run) == [] and _ev(srun.root, events.RESUME, run) == []
    assert len(_drivers(srun.root, run)) == drivers
    assert len(_ev(srun.root, events.GRANT, run)) == 1
    # `grant RUN stop` stops as it did before
    _as_session(monkeypatch)
    code, body = _grant(capsys, monkeypatch, run, run, "stop")
    assert code == vc.PASS, body
    assert [e for e in _ev(srun.root, events.STOP, run) if e["data"].get("final")]


def test_restart_verbs_unchanged_for_a_test_run(rec, capsys, monkeypatch):
    # runtime_mode test: no driver is registered, no start check runs, nothing is refused for the
    # missing session marker or the level
    _frozen(monkeypatch)
    for name in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    run = _start(capsys)
    _hold(rec.root, run)
    code, _body = _grant(capsys, monkeypatch, run, run, "extend", "--rounds", "4", "--no-apply")
    assert code == vc.PASS
    code, body = _call(capsys, "extend", run)
    assert code == vc.PASS, body
    assert [e for e in _ev(rec.root, events.DRIVER)] == []
    assert len(_ev(rec.root, events.EXTEND, run)) == 1
    _finalize(rec.root, run, "interrupted")
    code, body = _call(capsys, "resume", run)
    assert code == vc.PASS, body
    assert len(_ev(rec.root, events.RESUME, run)) >= 2


# ================================================================ the engine pin and the owner's repin
def _touch_engine():
    from alpaca.research import session_engine as eng
    target = os.path.join(os.path.realpath(eng.install_root()), "bin", "alpaca")
    os.utime(target, None)                 # a touch: the bytes are the same, the ctime is not
    return target


def _owner_terminal(monkeypatch, capsys_text=None):
    """The owner at an interactive terminal outside any agent session; the prompt names the text to
    type, and the stand-in types it."""
    import builtins
    import re
    import sys
    for name in owner.AGENT_MARKERS:
        monkeypatch.delenv(name, raising=False)

    class Tty:
        def isatty(self):
            return True
    monkeypatch.setattr(sys, "stdin", Tty())
    monkeypatch.setattr(builtins, "input", lambda prompt="": re.search(
        r"Type (\w+) to record", prompt).group(1))


@pytest.mark.parametrize("verb", ["extend", "resume"])
def test_extend_and_resume_pass_with_the_child_variable(srun, capsys, monkeypatch, verb):
    # the Desktop Code tab sets CLAUDE_CODE_CHILD_SESSION=1 in the driver's own shell (measured,
    # live-round/env-measure.txt): the variable is no sign of a subagent, so the driver passes
    run = srun.run
    if verb == "extend":
        _hold(srun.root, run)
        code, _body = _grant_at_a_terminal(capsys, monkeypatch, run, "extend", "--rounds", "4",
                                           "--no-apply")
        assert code == vc.PASS
    else:
        _finalize(srun.root, run, "interrupted")
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    code, body = _call(capsys, verb, run)
    assert code == vc.PASS, body
    assert _drivers(srun.root, run)[-1]["data"]["cause"] == verb


def test_resume_refuses_until_repin(srun, capsys, monkeypatch):
    run = srun.run
    _finalize(srun.root, run, "interrupted")
    _touch_engine()
    before = (len(_drivers(srun.root, run)), list(srun.fake.launches))
    code, body = _call(capsys, "resume", run)
    assert code != vc.PASS and body["code"] == "RUN-ENGINE-CHANGED", body
    assert (len(_drivers(srun.root, run)), srun.fake.launches) == before
    assert _ev(srun.root, events.RESUME, run) == []


def test_repin_needs_a_terminal(srun, capsys, monkeypatch):
    run = srun.run
    _finalize(srun.root, run, "interrupted")
    _touch_engine()
    # the session's own shell has an agent marker: the owner's step refuses and records nothing
    _as_session(monkeypatch)
    code, body = _call(capsys, "resume", run, "--repin-engine")
    assert code != vc.PASS and body["code"] == "RESEARCH-OWNER-NOT-TERMINAL", body
    assert _ev(srun.root, events.REPIN, run) == []
    # a terminal whose typed text differs from the one asked for records nothing either
    import builtins
    import sys
    for name in owner.AGENT_MARKERS:
        monkeypatch.delenv(name, raising=False)

    class Tty:
        def isatty(self):
            return True
    monkeypatch.setattr(sys, "stdin", Tty())
    monkeypatch.setattr(builtins, "input", lambda prompt="": "not the text")
    code, body = _call(capsys, "resume", run, "--repin-engine")
    assert code != vc.PASS, body
    assert _ev(srun.root, events.REPIN, run) == []


def test_repin_then_session_resume_passes(srun, capsys, monkeypatch):
    run = srun.run
    _finalize(srun.root, run, "interrupted")
    _touch_engine()
    drivers, launches = len(_drivers(srun.root, run)), list(srun.fake.launches)
    _owner_terminal(monkeypatch)
    code, body = _call(capsys, "resume", run, "--repin-engine")
    assert code == vc.PASS, body
    assert body["applied"] is False and body["next"] == "alpaca research resume %s" % run
    # the repin is on the record and nothing started: no runner, no driver row, no resume
    assert len(_ev(srun.root, events.REPIN, run)) == 1
    assert len(_drivers(srun.root, run)) == drivers and srun.fake.launches == launches
    assert _ev(srun.root, events.RESUME, run) == []
    # the driving session's plain resume passes the checks and registers itself as the driver
    _as_session(monkeypatch)
    code, body = _call(capsys, "resume", run)
    assert code == vc.PASS, body
    assert _drivers(srun.root, run)[-1]["data"]["cause"] == "resume"
    assert len(srun.fake.launches) == len(launches) + 1
    # a second touch after the repin is refused again
    _finalize(srun.root, run, "interrupted")
    _touch_engine()
    code, body = _call(capsys, "resume", run)
    assert code != vc.PASS and body["code"] == "RUN-ENGINE-CHANGED", body


# ------------------------------------------------------------------ t-360, a restart of the driver
def _spy_hand_off(monkeypatch):
    from alpaca.research import jobs
    calls = []
    real = jobs.hand_off

    def spy(*a, **kw):
        calls.append(a[2] if len(a) > 2 else kw.get("scope"))
        return real(*a, **kw)
    monkeypatch.setattr(jobs, "hand_off", spy)
    return calls


RESTART_ROWS = {
    "none": [],
    "resume": ["resume"],
    "resume then compact": ["resume", "compact"],
    "resume then clear": ["resume", "clear"],
}


@pytest.mark.parametrize("shape", sorted(RESTART_ROWS))
def test_next_job_of_a_restarted_driver_hands_nothing_and_answers_resume(srun, capsys, monkeypatch,
                                                                           shape):
    """in-session-runtime.md section 24.2: a session-start of the driver session (not a compaction
    or a clear) newer than its driver event is a restart of its CLI process; `next-job` then hands
    no job (the watch expires every open job at its next look) and answers `resume`. A compaction or
    a clear after the restart does not hide it (check round 1, B1). Without the restart the hand runs
    as before."""
    from alpaca import db
    run = srun.run
    _as_session(monkeypatch)
    sources = RESTART_ROWS[shape]
    restart = bool(sources)
    if restart:
        conn = _conn(srun.root)
        try:
            for source in sources:
                db.append_event(conn, session=SESSION, actor="hook", kind="session-start",
                                data={"source": source})
        finally:
            conn.close()
    calls = _spy_hand_off(monkeypatch)
    code, body = _call(capsys, "next-job", run, "--wait", "0")
    assert body is not None, code
    if restart:
        assert calls == [] and body["jobs"] == [] and body["action"] == "resume", body
    else:
        assert calls == [run], body
