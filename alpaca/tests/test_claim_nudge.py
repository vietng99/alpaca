"""t-076: the UserPromptSubmit hook reminds a session that edits files without a live task claim.

The cockpit's parallel-sessions panel reads claims per session, so an unclaimed session shows no
work. The reminder fires only after an edit that no claim or task move by that session follows.
"""
import json, os, subprocess, sys
from alpaca import cli, db

ENV = lambda project, sid: {**os.environ, "CLAUDE_PROJECT_DIR": project, "ALPACA_SESSION_ID": sid,
                            "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.dirname(__file__)))}
MARK = "[alpaca claim]"


def hook(module, payload, project):
    return subprocess.run([sys.executable, "-m", module], input=json.dumps(payload), capture_output=True,
                          text=True, encoding="utf-8", cwd=project, env=ENV(project, payload["session_id"]))


def prompt(project, sid):
    out = hook("alpaca.hooks.user_prompt", {"session_id": sid, "prompt": "go"}, project).stdout
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


def edit(project, sid):
    hook("alpaca.hooks.post_tool", {"session_id": sid, "tool_name": "Edit",
                                   "tool_input": {"file_path": "/x/y.py"}}, project)


def test_no_nudge_for_a_session_that_only_talks(project):
    cli.main(["init"])
    hook("alpaca.hooks.post_tool", {"session_id": "s1", "tool_name": "Read",
                                   "tool_input": {"file_path": "/x/y.py"}}, project)
    assert MARK not in prompt(project, "s1")


def test_nudge_after_an_unclaimed_edit(project):
    cli.main(["init"])
    edit(project, "s1")
    text = prompt(project, "s1")
    assert MARK in text and "bin/alpaca task claim" in text


def test_no_nudge_while_the_session_holds_a_claim(project, monkeypatch):
    cli.main(["init"])
    cli.main(["op", "new", "x"])
    cli.main(["task", "add", "--title", "task", "op-001", "s"])
    monkeypatch.setenv("ALPACA_SESSION_ID", "s1")
    assert cli.main(["task", "claim", "t-001", "--by", "w1", "--minutes", "30"]) == 0
    edit(project, "s1")
    assert MARK not in prompt(project, "s1")
    # another session editing without its own claim is still reminded
    edit(project, "s2")
    assert MARK in prompt(project, "s2")


def test_nudge_is_scoped_to_the_editing_session(project):
    cli.main(["init"])
    edit(project, "s1")
    assert MARK not in prompt(project, "s2")
    conn = db.connect(project)
    assert db.events(conn, kind="heartbeat")[-1]["session"] == "s1"
