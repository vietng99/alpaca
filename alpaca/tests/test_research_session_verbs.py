"""The start checks of a session run, the two verbs and the `proof verify` verb's marker rules
(in-session-runtime.md sections 2.2, 3 and 16, tests 13 and 20).

These tests need no runner: every refusal here happens before a run is opened, and a refusal
changes nothing on the record.
"""
from __future__ import annotations

import io
import json
import os

import pytest

from alpaca import cli, db
from alpaca.gates import verdict as vc
from alpaca.posture import level as level_mod
from alpaca.research import errors, events, jail, session_runtime, verifier
from alpaca.tests import research_loopkit as kit
from alpaca.tests import research_session_kit as rk
from alpaca.tests.test_research_verifier import (Scripted, events_of, seal, stand_in_binary,
                                                 setup_project, write_report)

#: The stand-in runs the gates in the jail; with no bubblewrap its proof is never verified.
needs_jail = pytest.mark.skipif(not jail.available()[0],
                                reason="bubblewrap is not available on this host")

START_KINDS = (events.RUN_START, events.DRIVER, events.JOB)


def _kinds(root, kinds=START_KINDS):
    conn = db.connect(root)
    try:
        return [r["kind"] for r in conn.execute(
            "SELECT kind FROM events WHERE kind IN (%s)" % ",".join("?" * len(kinds)), kinds)]
    finally:
        conn.close()


def _new(project, monkeypatch, **kw):
    root = kit.make_project(project, rounds=1, slots=2)
    monkeypatch.setattr(session_runtime, "rate_card_gaps", lambda root, runtime="claude": [])
    return root, rk.Session(monkeypatch, root, **kw)


def _no_marker(mp):
    for name in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SANDBOX"):
        mp.delenv(name, raising=False)


def _no_transcript(mp, root, sid):
    conn = db.connect(root)
    try:
        conn.execute("UPDATE sessions SET transcript=NULL WHERE sid=?", (sid,))
        conn.commit()
    finally:
        conn.close()


def _set_level(mp, root, sid, n):
    conn = db.connect(root)
    try:
        level_mod.set(conn, sid, n, "a lower level for the test", root=root)
    finally:
        conn.close()


def _codex(mp, root, sid):
    for name in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID"):
        mp.delenv(name, raising=False)
    mp.setenv("CODEX_THREAD_ID", sid)


def _unmapped_codex(mp, root, sid):
    from alpaca.formation import shield
    _codex(mp, root, sid)
    policy = shield.policy

    def without_codex(*args, **kwargs):
        result = policy(*args, **kwargs)
        result["runtimes"].pop("codex", None)
        return result

    mp.setattr(shield, "policy", without_codex)


def _plain(mp, root, sid):
    mp.setattr(session_runtime, "isolation_ok", lambda: False)
    mp.setattr(session_runtime, "_ALLOW_PLAIN", False)


def _gap(mp, root, sid):
    mp.setattr(session_runtime, "rate_card_gaps", lambda root, runtime="claude": [
        {"model": "claude-x-1", "fix": "bin/alpaca analytics price-check --model claude-x-1"}])


CASES = {
    "no-marker": (lambda mp, root, sid: _no_marker(mp), "RUN-RUNTIME-MISSING"),
    "no-transcript": (_no_transcript, "RUN-NO-TRANSCRIPT"),
    "native-codex-not-ready": (_codex, "RUN-CODEX-NOT-READY"),
    "no-shield-map-for-codex": (_unmapped_codex, "RUN-RUNTIME-UNMAPPED"),
    "plain-interpreter": (_plain, "RUN-ISOLATION-MISSING"),
    "no-rate-card": (_gap, "RUN-RATECARD-MISSING"),
    "level-2": (lambda mp, root, sid: _set_level(mp, root, sid, 2), "RESEARCH-LEVEL-TOO-LOW"),
    "level-1": (lambda mp, root, sid: _set_level(mp, root, sid, 1), "RESEARCH-LEVEL-TOO-LOW"),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_start_refusals(project, monkeypatch, name):
    root, session = _new(project, monkeypatch)
    change, code = CASES[name]
    change(monkeypatch, root, session.sid)
    rc, body = session.verb("start", "--runbook", kit.RUNBOOK, "--via", "autoresearch")
    assert rc == vc.BLOCKED and body["code"] == code, (rc, body)
    # a refusal opens no run, registers no driver, issues no job and leaves no live marker
    assert _kinds(root) == []
    assert not os.path.isdir(os.path.join(root, ".alpaca", "research", "live")) or \
        os.listdir(os.path.join(root, ".alpaca", "research", "live")) == []
    # the code is in the design's table with the BLOCKED band
    from alpaca.research import errors
    assert errors.CODES[code] == vc.BLOCKED


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_start_admission_under_the_kit(project, monkeypatch, runtime):
    root, session = _new(project, monkeypatch)
    if runtime == "codex":
        _codex(monkeypatch, root, session.sid)
    conn = db.connect(root)
    try:
        before = conn.total_changes
        if runtime == "codex":
            with pytest.raises(errors.ResearchRefusal) as caught:
                session_runtime.drive_checks(conn, root, session.sid, level=6)
            assert caught.value.reason == "RUN-CODEX-NOT-READY"
            assert errors.CODES[caught.value.reason] == vc.BLOCKED
        else:
            got = session_runtime.drive_checks(conn, root, session.sid, level=6)
            assert got["runtime"] == "claude" and got["witness"] == "hook"
        assert conn.total_changes == before
    finally:
        conn.close()
    assert _kinds(root) == []


def test_next_job_and_submit_refuse_an_unknown_run_and_an_unknown_job(project, monkeypatch):
    root, session = _new(project, monkeypatch)
    rc, body = session.verb("next-job", "rr-404", "--wait", "0")
    assert rc != vc.PASS and body["code"].startswith(("RESEARCH-", "RUN-", "JOB-")), body
    rc, _body = session.verb("next-job")
    assert rc == vc.USAGE                      # neither a run nor --verification
    rc, body = session.verb("submit", "t-404-implementer-t1", "--nonce", "0" * 32, "--agent-id",
                            "a-none")
    assert rc != vc.PASS and body["code"].startswith("JOB-"), body
    assert _kinds(root, (events.JOB_CLAIM, events.JOB_RESULT)) == []


def test_the_child_session_variable_is_no_sign_of_a_subagent(project, monkeypatch):
    # CLAUDE_CODE_CHILD_SESSION=1 is set in the driver's own shell in the Desktop Code tab and in a
    # subagent's shell alike (live-round/env-measure.txt), so no verb refuses on it: a subagent is
    # stopped at the tool boundary by the guard (test_research_session_guard.py)
    root, session = _new(project, monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    conn = db.connect(root)
    try:
        got = session_runtime.drive_checks(conn, root, session.sid, level=6)
    finally:
        conn.close()
    assert got["runtime"] == "claude"
    conn = db.connect(root)
    try:
        events.emit(conn, events.RUN_START, run="rr-001", ref="rr-001", op=None, session="s",
                    data={"runtime_mode": "session", "mode": "improve"})
    finally:
        conn.close()
    rc, body = session.verb("next-job", "rr-001", "--wait", "0")
    assert body.get("code") not in ("JOB-IN-SUBAGENT", "RUN-DRIVER-IS-SUBAGENT"), body
    rc, body = session.verb("submit", "j-1", "--nonce", "n", "--agent-id", "a")
    assert body.get("code") not in ("JOB-IN-SUBAGENT", "RUN-DRIVER-IS-SUBAGENT"), body


# ------------------------------------------------------------------ test 20, the proof verify verb
@pytest.fixture
def vworld(project, tmp_path, monkeypatch):
    root = setup_project(project)
    monkeypatch.setattr(session_runtime, "rate_card_gaps", lambda root, runtime="claude": [])
    ctx = rk.prepare(monkeypatch, root, sid="vf-session", level=6)
    ctx["root"] = root
    ctx["binary"] = stand_in_binary(tmp_path)
    return ctx


def _verify(*argv):
    out = io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(out):
        rc = cli.main(["--session", "vf-session", "proof", "verify", "t-001"] + list(argv))
    return rc, out.getvalue()


def test_verify_verb_marker_and_order(vworld, monkeypatch):
    root = vworld["root"]
    write_report(root)
    seal(root)
    # neither marker, even with --runtime claude: nobody would take the jobs
    _no_marker(monkeypatch)
    rc, out = _verify("--runtime", "claude")
    assert rc == vc.BLOCKED and "VERIFY-RUNTIME-UNKNOWN" in out
    assert events_of(root, "research-verify-open") == [] and _kinds(root) == []
    # With both markers, explicit Codex remains blocked before verifier dispatch.
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CODEX_THREAD_ID", "vf-session")
    reached = []
    monkeypatch.setattr(verifier, "_verb_run", lambda *a, **kw: reached.append(
        (a[4], kw.get("session_mode"))) or vc.PASS)
    rc, out = _verify("--runtime", "codex")
    assert rc == vc.BLOCKED and "RUN-CODEX-NOT-READY" in out, out
    assert reached == []
    assert events_of(root, "research-verify-open") == [] and _kinds(root) == []
    rc, out = _verify("--runtime", "claude")
    assert rc == vc.PASS and reached == [("claude", True)], out


def test_verify_verb_refusals_of_the_start_checks_write_nothing(vworld, monkeypatch):
    root = vworld["root"]
    write_report(root)
    seal(root)
    _set_level(monkeypatch, root, "vf-session", 2)
    rc, out = _verify()
    assert rc == vc.BLOCKED and "a session run needs L3 or above" in out, out
    assert events_of(root, "research-verify-open") == [] and _kinds(root) == []
    _set_level(monkeypatch, root, "vf-session", 6)
    # CLAUDE_CODE_CHILD_SESSION=1 is no sign of a subagent: the driver's verify goes on to the run
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    reached = []
    monkeypatch.setattr(verifier, "_verb_run", lambda *a, **kw: reached.append(1) or vc.PASS)
    rc, out = _verify("--runtime", "claude")
    assert rc == vc.PASS and reached == [1], out


def test_a_seal_with_no_tree_registers_no_driver(vworld, monkeypatch):
    # the driver registration is after `open` returns, so a refusal of open leaves no driver row
    root = vworld["root"]
    write_report(root)
    seal(root, verify=False)
    rc, out = _verify()
    assert rc == vc.BLOCKED and "VERIFY-TREE" in out
    assert events_of(root, "research-verify-open") == [] and _kinds(root) == []


@needs_jail
def test_with_the_verb_hook_set_the_stand_in_runs_with_no_job_and_no_driver(vworld, monkeypatch):
    root = vworld["root"]
    write_report(root)
    seal(root)
    stand_in = Scripted(root)
    monkeypatch.setitem(verifier._hooks, "verb_launch", stand_in)
    monkeypatch.setattr(verifier, "_resolve_binary", lambda runtime: vworld["binary"])
    rc, out = _verify()
    assert rc == vc.PASS, out
    assert _kinds(root, (events.JOB, events.DRIVER)) == [] and stand_in.specs


# ================================================================ the rate cards (section 9, test 23)
def _job_result_naming(root, *models):
    conn = db.connect(root)
    try:
        events.emit(conn, events.JOB_RESULT, run="rr-001", ref="t-900", op=None, session="s",
                    data={"job": "j-x", "scope": "rr-001", "models_seen": list(models),
                          "agent_id": "a-x"})
    finally:
        conn.close()


def test_ratecard_preflight(project, monkeypatch):
    from alpaca.analytics import ratecards
    root = kit.make_project(project, rounds=1, slots=2)
    session = rk.Session(monkeypatch, root)
    # a result of an earlier run names a model that has no card: start refuses, prints the command
    # that records one, and writes nothing
    _job_result_naming(root, "claude-sonnet-9")
    gaps = session_runtime.rate_card_gaps(root, "claude")
    assert [g["model"] for g in gaps] == ["claude-sonnet-9"] and "price-check" in gaps[0]["fix"]
    rc, body = session.verb("start", "--runbook", kit.RUNBOOK, "--via", "autoresearch")
    assert rc == vc.BLOCKED and body["code"] == "RUN-RATECARD-MISSING", body
    assert "price-check" in body["reason"] and "claude-sonnet-9" in body["reason"]
    assert events.RUN_START not in _kinds(root) and events.DRIVER not in _kinds(root)
    # `extend`, `resume` and the proof verb make the same check through drive_checks
    conn = db.connect(root)
    try:
        with pytest.raises(session_runtime.errors.ResearchRefusal) as exc:
            session_runtime.drive_checks(conn, root, session.sid, level=6)
        assert exc.value.reason == "RUN-RATECARD-MISSING"
    finally:
        conn.close()
    # a model whose family no role uses does not count, and the built-in models pass
    _job_result_naming(root, "claude-opus-5-5", "claude-haiku-4-5-20251001", "gpt-9-turbo")
    assert [g["model"] for g in session_runtime.rate_card_gaps(root, "claude")] == ["claude-sonnet-9"]
    # after a card is recorded the same call passes
    ratecards.record(root, "claude-sonnet-9", provider="anthropic",
                     rates=("3", ".3", "3.75", "6", None, "15"),
                     source_url="https://platform.claude.com/docs/en/about-claude/pricing",
                     method="agent-entered")
    assert session_runtime.rate_card_gaps(root, "claude") == []
    conn = db.connect(root)
    try:
        assert session_runtime.drive_checks(conn, root, session.sid, level=6)["runtime"] == "claude"
    finally:
        conn.close()
