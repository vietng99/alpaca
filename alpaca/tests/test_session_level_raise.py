"""t-678: `session level L<n>` (and `session start --level`) raises only on the owner's own words.

Before t-678 `bin/alpaca session level L6` recorded any level, a raise included, with no check of
who asked. Raising your own level is not allowed (CLAUDE.md rule 6), so a raise above the level in
force for the session now needs the owner's words, through the two paths of alpaca/ownerword.py:
  * inside an agent session, the session transcript holds an owner `/autodrive` call for exactly
    that level, typed after the latest level decision (the t-669 `account_raise` test); the
    decision records under the actor `owner` with that owner_word row;
  * with no agent session, the person at a terminal types the level when asked.
A drop, OFF or the same level needs no owner words. A refusal writes nothing and names its reason.
"""
import json

import pytest

from alpaca import cli, db, ownerword, project as proj
from alpaca.posture import level
from alpaca.tests.ownerword_fixtures import (EARLY, Project, owner_at_terminal, queued,
                                             tool_result, typed)

SID = "sess-1"


@pytest.fixture
def p(tmp_path, monkeypatch):
    p = Project(tmp_path, monkeypatch, sid=SID)
    proj.save(p.root, {"name": "p", "default_level": "L2"})
    return p


def autodrive(args, ts=None, name="/autodrive", **extra):
    """The transcript line Claude Code writes when the owner types `<name> <args>`."""
    text = ("<command-message>autodrive</command-message>\n<command-name>%s</command-name>\n"
            "<command-args>%s</command-args>" % (name, args))
    kw = {"ts": ts} if ts else {}
    return typed(text, entrypoint="claude-desktop", **kw, **extra)


def assistant(text):
    return {"type": "assistant", "isSidechain": False, "timestamp": "2999-01-01T00:00:00.000Z",
            "entrypoint": "cli", "message": {"role": "assistant",
                                             "content": [{"type": "text", "text": text}]}}


def rows(p):
    return p.conn.execute("SELECT COUNT(*) FROM decisions WHERE session=? AND kind=?",
                          (SID, level.DECISION_KIND)).fetchone()[0]


def latest_event(p):
    row = p.conn.execute("SELECT actor, data FROM events WHERE kind='decision' AND session=? "
                         "ORDER BY id DESC LIMIT 1", (SID,)).fetchone()
    return row["actor"], json.loads(row["data"])


def ask(p, n, goal="", **kw):
    kw.setdefault("environ", p.env)
    return level.request(p.conn, SID, n, goal, actor="claude", root=p.root, **kw)


def refused(p, n, reason, **kw):
    before = rows(p)
    with pytest.raises(ownerword.OwnerWordRefusal) as exc:
        ask(p, n, **kw)
    assert exc.value.reason == reason
    assert rows(p) == before
    return exc.value


# --------------------------------------------------------------------------- agent session: refused
def test_a_raise_with_no_owner_words_is_refused_and_writes_nothing(p):
    p.write([typed("please carry on")])
    exc = refused(p, "L6", ownerword.R_LEVEL_RAISE)
    assert "/autodrive call for L6" in exc.detail
    assert level.in_force(p.conn, SID, root=p.root) == 2


@pytest.mark.parametrize("entry", [
    assistant("/autodrive L6"),
    autodrive("L6", isSidechain=True),
    autodrive("L6", isMeta=True),
    typed("/autodrive L6", entrypoint="sdk-cli"),
    tool_result(),
    typed("why did /autodrive L6 not reach the record?"),
])
def test_text_that_is_not_an_owner_autodrive_call_does_not_raise(p, entry):
    p.write([entry])
    refused(p, "L6", ownerword.R_LEVEL_RAISE)


def test_the_owner_call_must_name_exactly_the_level_asked(p):
    p.write([autodrive("L5")])
    refused(p, "L6", ownerword.R_LEVEL_RAISE)
    refused(p, "L4", ownerword.R_LEVEL_RAISE)


def test_the_latest_owner_call_governs(p):
    p.write([autodrive("L6"), autodrive("L4")])
    refused(p, "L6", ownerword.R_LEVEL_RAISE)


def test_an_owner_call_typed_before_the_latest_level_decision_does_not_count(p):
    p.write([autodrive("L6", ts=EARLY)])
    level.set_local(p.conn, SID, 3, "a later level decision", root=p.root)
    refused(p, "L6", ownerword.R_LEVEL_RAISE)
    assert level.in_force(p.conn, SID, root=p.root) == 3


def test_a_raise_for_another_session_than_the_real_one_is_refused(p):
    p.write([autodrive("L6")])
    env = dict(p.env, CLAUDE_CODE_SESSION_ID="other-session")
    refused(p, "L6", ownerword.R_SESSION, environ=env)


def test_a_session_with_no_recorded_transcript_is_refused(p):
    p.conn.execute("DELETE FROM sessions WHERE sid=?", (SID,))
    refused(p, "L6", ownerword.R_NO_TRANSCRIPT)


# --------------------------------------------------------------------------- agent session: owner words
def test_the_owners_autodrive_l6_raises_and_is_recorded_with_the_owner_word(p):
    p.write([typed("hello"), autodrive('L6 "op-059 to the ship hold"')])
    rec = ask(p, "L6", "an agent-written goal")
    assert rec["level"] == 6 and level.in_force(p.conn, SID, root=p.root) == 6
    # the goal is the one the owner typed with the command
    assert rec["goal"] == "op-059 to the ship hold"
    actor, data = latest_event(p)
    assert actor == level.OWNER_ACTOR
    word = data["owner_word"]
    assert word["path"] == "session" and word["line"] == 2 and word["answer"] == "L6"
    assert word["transcript"] == p.transcript_of(SID)
    assert data["pointer"] == "%s:2" % p.transcript_of(SID)
    assert ownerword.decision_is_owner(p.conn, data["id"], root=p.root)


@pytest.mark.parametrize("entry", [typed("/autodrive L6"), queued("/autodrive l6"),
                                   autodrive("L6", name="/alpaca:autodrive"),
                                   typed("/alpaca:autodrive L6")])
def test_plain_queued_and_plugin_forms_of_the_owner_call_count(p, entry):
    p.write([entry])
    assert ask(p, "L6")["level"] == 6


def test_the_agent_goal_is_kept_when_the_owner_typed_none(p):
    p.write([autodrive("L6")])
    assert ask(p, "L6", "run the op")["goal"] == "run the op"


def test_one_owner_call_raises_once(p):
    """After the raise the call is older than the latest decision: a drop and a second raise need
    a new owner call."""
    p.write([autodrive("L6", ts=EARLY)])
    ask(p, "L6")
    ask(p, "L3")
    assert level.in_force(p.conn, SID, root=p.root) == 3
    refused(p, "L6", ownerword.R_LEVEL_RAISE)


# --------------------------------------------------------------------------- no owner words needed
def test_a_drop_needs_no_owner_words(p):
    level.set_local(p.conn, SID, 6, "run the op", root=p.root)
    p.write([typed("nothing about levels")])
    rec = ask(p, "L3")
    assert rec["level"] == 3 and level.in_force(p.conn, SID, root=p.root) == 3
    actor, data = latest_event(p)
    assert actor == "claude" and "owner_word" not in data


def test_the_same_level_needs_no_owner_words(p):
    level.set_local(p.conn, SID, 5, "old goal", root=p.root)
    assert ask(p, "L5", "new goal")["goal"] == "new goal"
    assert level.in_force(p.conn, SID, root=p.root) == 5


def test_the_default_level_needs_no_owner_words(p):
    assert ask(p, "L2")["level"] == 2


def test_off_needs_no_owner_words(p):
    level.set_local(p.conn, SID, 6, "run the op", root=p.root)
    ask(p, "OFF")
    assert level.in_force(p.conn, SID, root=p.root) == 2
    assert level.levels(p.conn, [SID], root=p.root)[SID]["level"] == level.OFF


def test_off_below_the_default_is_a_raise_and_needs_the_owner(p):
    level.set_local(p.conn, SID, 1, "a careful run", root=p.root)
    p.write([typed("please carry on")])
    refused(p, "OFF", ownerword.R_LEVEL_RAISE)
    assert level.in_force(p.conn, SID, root=p.root) == 1


def test_off_below_the_default_records_on_the_owners_autodrive_off(p):
    level.set_local(p.conn, SID, 1, "a careful run", root=p.root)
    p.write([autodrive("off")])
    ask(p, "OFF")
    assert level.in_force(p.conn, SID, root=p.root) == 2
    actor, data = latest_event(p)
    assert actor == level.OWNER_ACTOR and data["owner_word"]["answer"] == level.OFF


def test_a_level_off_the_band_is_refused(p):
    with pytest.raises(ValueError):
        ask(p, "L7")


# --------------------------------------------------------------------------- the terminal path
def test_the_owner_at_a_terminal_raises_by_typing_the_level(p):
    rec = ask(p, "L6", "terminal goal", environ={}, isatty=lambda: True, read=lambda _p: "L6")
    assert rec["level"] == 6 and rec["goal"] == "terminal goal"
    actor, data = latest_event(p)
    assert actor == level.OWNER_ACTOR
    assert data["owner_word"]["path"] == "terminal" and data["owner_word"]["quote"] == "L6"
    assert ownerword.decision_is_owner(p.conn, data["id"], root=p.root)


@pytest.mark.parametrize("environ,tty,typed_text", [
    ({}, False, "L6"),                           # stdin is not a terminal (a script, a pipe)
    ({}, True, "yes"),                           # the typed confirmation is not the level
    ({"CODEX_THREAD_ID": "t"}, True, "L6"),      # an agent with no Claude session (Codex)
    ({"CLAUDECODE": "1"}, True, "L6"),
])
def test_the_terminal_path_refuses_without_the_owner_typing_the_level(p, environ, tty, typed_text):
    refused(p, "L6", ownerword.R_NOT_TERMINAL, environ=environ, isatty=lambda: tty,
            read=lambda _p: typed_text)


# --------------------------------------------------------------------------- the CLI verbs
def invoke(monkeypatch, *args):
    if "operator" not in cli.VERB_MODULES:
        monkeypatch.setattr(cli, "VERB_MODULES", cli.VERB_MODULES + ("operator",))
    return cli.main(list(args))


@pytest.fixture
def agent(p, monkeypatch):
    monkeypatch.chdir(p.root)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    return p


def test_session_level_raise_without_owner_words_exits_blocked_with_the_reason(agent, monkeypatch,
                                                                                capsys):
    agent.write([typed("go on")])
    assert invoke(monkeypatch, "--session", SID, "session", "level", "L6") == cli.BLOCKED
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "BLOCKED" and out["reason"] == ownerword.R_LEVEL_RAISE
    assert "/autodrive L6" in out["error"]
    assert level.in_force(db.connect(agent.root), SID, root=agent.root) == 2


def test_session_level_raise_with_the_owners_call_passes(agent, monkeypatch, capsys):
    agent.write([autodrive("L6")])
    assert invoke(monkeypatch, "--session", SID, "session", "level", "L6",
                  "--goal", "run the op") == cli.PASS
    out = json.loads(capsys.readouterr().out)
    assert out["level"] == "L6"
    conn = db.connect(agent.root)
    actor, data = (lambda r: (r["actor"], json.loads(r["data"])))(conn.execute(
        "SELECT actor, data FROM events WHERE kind='decision' AND session=? ORDER BY id DESC "
        "LIMIT 1", (SID,)).fetchone())
    assert actor == "owner" and data["owner_word"]["path"] == "session"


def test_session_level_drop_and_off_pass_without_owner_words(agent, monkeypatch, capsys):
    level.set_local(agent.conn, SID, 6, "run the op", root=agent.root)
    agent.write([typed("go on")])
    assert invoke(monkeypatch, "--session", SID, "session", "level", "L4") == cli.PASS
    assert invoke(monkeypatch, "--session", SID, "session", "level", "L4") == cli.PASS
    assert invoke(monkeypatch, "--session", SID, "session", "level", "OFF") == cli.PASS
    assert level.in_force(db.connect(agent.root), SID, root=agent.root) == 2


def test_session_start_with_a_raise_and_no_owner_words_is_blocked(agent, monkeypatch, capsys):
    agent.write([typed("go on")])
    assert invoke(monkeypatch, "--session", SID, "session", "start", "--operator", "claude",
                  "--level", "L6") == cli.BLOCKED
    out = json.loads(capsys.readouterr().out)
    assert out["reason"] == ownerword.R_LEVEL_RAISE
    assert "started at L2" in out["error"]
    assert level.in_force(db.connect(agent.root), SID, root=agent.root) == 2


def test_session_level_raise_from_the_owners_terminal_passes(p, monkeypatch, capsys):
    monkeypatch.chdir(p.root)
    owner_at_terminal(monkeypatch, "L5")
    assert invoke(monkeypatch, "--session", SID, "session", "level", "L5") == cli.PASS
    assert json.loads(capsys.readouterr().out)["level"] == "L5"


def test_session_level_raise_at_a_terminal_that_types_another_level_is_blocked(p, monkeypatch,
                                                                               capsys):
    monkeypatch.chdir(p.root)
    owner_at_terminal(monkeypatch, "L6")
    assert invoke(monkeypatch, "--session", SID, "session", "level", "L5") == cli.BLOCKED
    assert json.loads(capsys.readouterr().out)["reason"] == ownerword.R_NOT_TERMINAL
