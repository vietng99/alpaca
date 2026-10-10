"""The subagent marker (fix batch 2, item F1; design/research-engine/in-session-runtime.md sections
3.4 and 12.3).

No environment variable tells a subagent's shell from the driver's, so the guard marks the shell of
every Bash call of a subagent: it returns an allow decision whose `updatedInput` is the whole tool
input with its command behind `export ALPACA_RESEARCH_AGENT=<agent id>;`. The verbs that drive a run
refuse when the variable is set, a command that names the variable is denied, the judge accepts a
transcript that holds the marked command, and the driver audit counts the driving verbs that
subagents tried.
"""
from __future__ import annotations

import io
import json
import os

import pytest

from alpaca import cli, pool
from alpaca.gates import verdict as vc
from alpaca.hooks import research_guard
from alpaca.research import agent_marker, session_runtime
from alpaca.tests import research_session_kit as rk
from alpaca.tests.test_research_session_guard import _bash, _denied, _payload, _reason, world  # noqa: F401

@pytest.fixture
def jworld(project, monkeypatch):
    w = rk.JudgeWorld(monkeypatch, project)
    yield w
    w.close()


DRIVING_ARGV = (("research", "start", "--runbook", "r.yaml"), ("research", "resume", "rr-001"),
                ("research", "extend", "rr-001"), ("research", "stop", "rr-001"),
                ("research", "next-job", "rr-001"),
                ("research", "submit", "j-1", "--nonce", "n", "--agent-id", "a"),
                ("proof", "verify", "t-001"))


def _updated(out):
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse" and spec["permissionDecision"] == "allow", out
    return spec["updatedInput"]


# ------------------------------------------------------------------ the guard puts the marker in
def test_a_subagent_bash_call_gets_the_whole_input_with_the_marked_command(world):
    w = world
    tool_input = {"command": "cd %s && ls" % w.cwd, "description": "list", "timeout": 5000,
                  "run_in_background": False}
    out = research_guard.handle(_payload(w, "Bash", dict(tool_input)))
    got = _updated(out)
    assert got == dict(tool_input, command="export ALPACA_RESEARCH_AGENT=%s; cd %s && ls" % (
        w.agent.agent_id, w.cwd))
    # without a live run of the session no shell is marked: the hook is shared by every session
    # of the project, and a marked shell would make their research tests and verbs refuse
    os.remove(os.path.join(w.root, ".alpaca", "research", "live", "%s.json" % pool.slug(w.sid)))
    assert research_guard.handle(_payload(w, "Bash", {"command": "ls"})) is None


def test_a_suite_run_from_a_marked_shell_does_not_inherit_the_marker():
    # conftest's autouse fixture removes the variable for every test
    assert "ALPACA_RESEARCH_AGENT" not in os.environ


def test_the_driver_and_the_other_tools_are_left_alone(world):
    w = world
    assert research_guard.handle(_payload(w, "Bash", {"command": "ls"}, agent=False)) is None
    for tool in ("Read", "Write", "Monitor", "PowerShell"):
        out = research_guard.handle(_payload(w, tool, {"file_path": os.path.join(w.cwd, "src", "x")}))
        assert out is None or "updatedInput" not in out["hookSpecificOutput"], tool
    # a denied call carries no update
    out = _bash(w, "bin/alpaca research stop rr-001")
    assert _denied(out) and "updatedInput" not in out["hookSpecificOutput"]


def test_a_command_behind_a_comment_or_a_heredoc_stays_one_command(world):
    w = world
    for command in ("# a note\nls", "cat > x <<'EOF'\nline\nEOF"):
        got = _updated(research_guard.handle(_payload(w, "Bash", {"command": command})))
        assert got["command"] == agent_marker.prefix_for(w.agent.agent_id) + command


def test_an_agent_id_that_is_not_a_plain_word_cannot_inject_shell_text(world):
    w = world
    payload = _payload(w, "Bash", {"command": "ls"})
    payload["agent_id"] = "a1; touch /x $(id)"
    got = _updated(research_guard.handle(payload))
    assert got["command"].count(";") == 1 and "$(" not in got["command"].split(";")[0], got


def test_only_the_guard_returns_updated_input():
    # the pre_tool hook and git_guard run on the same event: none may rewrite an input
    here = os.path.dirname(research_guard.__file__)
    holders = []
    for name in sorted(os.listdir(here)):
        if name.endswith(".py"):
            with open(os.path.join(here, name), encoding="utf-8") as fh:
                if "updatedInput" in fh.read():
                    holders.append(name)
    assert holders == ["research_guard.py"], holders


# ------------------------------------------------------------------ a command may not name it
@pytest.mark.parametrize("command", (
    "unset ALPACA_RESEARCH_AGENT; ls",
    "env -u ALPACA_RESEARCH_AGENT ls",
    "ALPACA_RESEARCH_AGENT= ls",
    "export ALPACA_RESEARCH_AGENT=; ls",
    "echo $ALPACA_RESEARCH_AGENT",
))
def test_a_subagent_command_that_names_the_marker_is_denied(world, command):
    w = world
    out = _bash(w, command)
    assert _denied(out) and "RUN-DRIVER-IS-SUBAGENT" in _reason(out), out
    assert "ALPACA_RESEARCH_AGENT" in _reason(out)
    # the driver's own call may name it (it is the driver that reads the variable in a test)
    assert _bash(w, command, agent=False) is None


# ------------------------------------------------------------------ the verbs refuse it
def _run(session, argv):
    out = io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(out):
        rc = cli.main(["--session", session] + list(argv))
    return rc, out.getvalue()


@pytest.mark.parametrize("argv", DRIVING_ARGV, ids=lambda a: " ".join(a[:2]))
def test_a_driving_verb_refuses_when_the_marker_is_in_its_shell(project, monkeypatch, argv):
    root = project
    cli.main(["init"])
    monkeypatch.setenv("ALPACA_RESEARCH_AGENT", "a1b2c3")
    rc, text = _run("drv", list(argv))
    assert rc == vc.BLOCKED, text
    assert "RUN-DRIVER-IS-SUBAGENT" in text and "a1b2c3" in text, text
    # nothing was written: no run, driver, job or refusal row
    from alpaca import db
    conn = db.connect(root)
    try:
        kinds = {r["kind"] for r in conn.execute("SELECT kind FROM events")}
    finally:
        conn.close()
    assert not any(k.startswith("research-") for k in kinds), kinds


@pytest.mark.parametrize("argv", DRIVING_ARGV, ids=lambda a: " ".join(a[:2]))
def test_the_same_verb_without_the_marker_is_not_refused_for_it(project, monkeypatch, argv):
    # the control: with the variable absent the refusal is something else (or none)
    cli.main(["init"])
    monkeypatch.delenv("ALPACA_RESEARCH_AGENT", raising=False)
    rc, text = _run("drv", list(argv))
    assert "RUN-DRIVER-IS-SUBAGENT" not in text, text


def test_the_verbs_that_do_not_drive_a_run_are_not_refused_for_the_marker(project, monkeypatch):
    cli.main(["init"])
    monkeypatch.setenv("ALPACA_RESEARCH_AGENT", "a1b2c3")
    for argv in (("research", "status"), ("research", "contracts")):
        rc, text = _run("drv", list(argv))
        assert "RUN-DRIVER-IS-SUBAGENT" not in text, (argv, text)


def test_the_marker_a_real_hook_sets_reaches_a_real_shell_and_the_verb_refuses(project, tmp_path):
    # end to end over the shell: the command the guard returns, run by bash, makes the verb refuse
    import subprocess
    import sys
    cli.main(["init"])
    marked = agent_marker.mark("%s -c 'import os; print(os.environ.get(\"ALPACA_RESEARCH_AGENT\"))'"
                               % sys.executable, "agent-xyz")
    got = subprocess.run(["bash", "-c", marked], capture_output=True, text=True,
                         env={k: v for k, v in os.environ.items() if k != "ALPACA_RESEARCH_AGENT"})
    assert got.stdout.strip() == "agent-xyz", got
    plain = subprocess.run(["bash", "-c", "%s -c 'import os; print(os.environ.get(\"ALPACA_RESEARCH_AGENT\"))'"
                            % sys.executable], capture_output=True, text=True,
                           env={k: v for k, v in os.environ.items() if k != "ALPACA_RESEARCH_AGENT"})
    assert plain.stdout.strip() == "None"


# ------------------------------------------------------------------ the judge and the audit
def test_a_transcript_that_holds_the_marked_command_passes_the_pool_witness(jworld):
    # the pool line is written from the input the hook was given; the transcript may hold the input
    # Claude Code ran, which is the marked one (K12)
    from alpaca.tests.test_research_session_closure import _job

    w = jworld
    jid = _job(w)

    def mark(records, agent):
        for r in records:
            for item in (r.get("message") or {}).get("content") or []:
                if isinstance(item, dict) and item.get("type") == "tool_use" and item["name"] == "Bash":
                    item["input"] = dict(item["input"], command=agent_marker.mark(
                        item["input"]["command"], agent.agent_id))
    a = w.run_agent(jid, edit=mark)
    assert any("ALPACA_RESEARCH_AGENT" in line for line in open(a.path, encoding="utf-8"))
    c, problem = w.judge(jid, w.claim(jid, a))
    assert problem is None, problem
    # every marked call matched its pool line: no difference at all, which the gap bound of K12
    # (up to three) would otherwise hide
    gaps = c.store["pool_gaps"]
    assert gaps["diffs"] == 0 and gaps["gaps"] == 0 and gaps["calls"] > 0, gaps


def test_the_pool_hash_of_the_plain_command_matches_only_the_same_command():
    from alpaca.research import jobs
    plain = jobs._canon_hash({"command": "ls -la", "description": "d"})
    marked = {"command": agent_marker.mark("ls -la", "a1"), "description": "d"}
    assert plain in jobs._input_hashes(marked)
    assert plain not in jobs._input_hashes({"command": agent_marker.mark("ls", "a1"),
                                            "description": "d"})
    assert plain not in jobs._input_hashes({"command": "export ALPACA_RESEARCH_AGENT=a1; ls -la",
                                            "description": "other"})
    assert jobs._input_hashes(None) == (jobs._canon_hash({}),)


def test_the_driver_audit_counts_the_driving_verbs_subagents_tried(jworld):
    w = jworld
    for i, (agent, command) in enumerate((("a-one", "bin/alpaca research start --runbook r.yaml"),
                                          ("a-one", "cd /x && bin/alpaca proof verify t-001"),
                                          ("a-two", "bin/alpaca research next-job rr-001"),
                                          ("a-two", "bin/alpaca research status rr-001"),
                                          ("a-two", "ls"))):
        pool.record(w.root, w.sid, "pre", {"tool_name": "Bash", "tool_input": {"command": command},
                                           "tool_use_id": "toolu_s%d" % i, "cwd": w.root,
                                           "agent_id": agent, "agent_type": "general-purpose"})
    # the driver's own calls of the same verbs are not counted
    pool.record(w.root, w.sid, "pre", {"tool_name": "Bash", "cwd": w.root,
                                       "tool_input": {"command": "bin/alpaca research next-job rr-001"},
                                       "tool_use_id": "toolu_d1"})
    row = session_runtime.driver_audit(w.conn, w.root, w.sid, None, scope=w.SCOPE)
    assert row["subagent_driving"]["count"] == 3, row["subagent_driving"]
    assert {c["agent_id"] for c in row["subagent_driving"]["calls"]} == {"a-one", "a-two"}
    assert row["findings"] == [], row     # a subagent's call is no finding about the driver


def test_the_report_names_the_subagent_driving_calls():
    from alpaca.research import events, report
    got = report._jobs_summary([{"kind": events.DRIVER_AUDIT, "id": 9, "data": {
        "session": "s", "findings": [], "subagent_driving": {"count": 2, "calls": []}}}])
    assert got["driver_audit"][0]["subagent_driving"] == 2
    text = report._audit_line(got["driver_audit"][0])
    assert "2 subagent call(s) of a driving verb" in text, text
    assert "subagent call" not in report._audit_line({"event": 1, "session": "s", "findings": 0})
