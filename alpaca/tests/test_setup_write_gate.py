"""The one gate in front of the verbs that write setup (op-062 t-656, round 3 of the build).

`onboard_flow.setup_write_refusal` decides for the flat `onboard`, the plain `attach init` and `new --attach`. These
tests pin every cell of verb x state x caller, then run the attack rows of the adversarial checks (the unrequested skip,
the caller that wipes its markers, the agent that skips the look) through the real verbs.

A caller is one of three:
  agent     a session id or an agent marker in the environment (a terminal on stdin makes no difference);
  stripped  no marker, no session, and no terminal on stdin (a child process of an agent);
  owner     no marker, no session, and a terminal on stdin.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from alpaca import adopt, cli, db, onboard_flow, ownerword, project
from alpaca.tests.attachkit import git, upstream  # noqa: F401  (a fixture)
from alpaca.tests.conftest import REPO
from alpaca.tests.onboardchat import Chat, chat  # noqa: F401  (a fixture)
from alpaca.tests.test_onboard_attacks import snapshot

CALLERS = {
    "agent": dict(environ={"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s1"}, isatty=lambda: True),
    "agent-no-tty": dict(environ={"CLAUDECODE": "1"}, isatty=lambda: False),
    "stripped": dict(environ={}, isatty=lambda: False),
    "owner": dict(environ={}, isatty=lambda: True),
}
STATES = ("fresh", "fresh+look", "due", "confirmed", "done", "skipped", "none-agent-onboarded", "none-owner-onboarded")

#: True = the gate refuses a caller that is not the owner's terminal. The owner's terminal is never refused.
REFUSED = {
    #                          onboard  attach  new-attach
    "fresh":                  (False,   True,   True),
    "fresh+look":             (True,    True,   True),
    "due":                    (True,    True,   True),
    "confirmed":              (True,    True,   True),
    "done":                   (False,   False,  True),
    "skipped":                (False,   False,  True),
    "none-agent-onboarded":   (False,   True,   True),
    "none-owner-onboarded":   (False,   False,  True),
}
VERBS = ("onboard", "attach", "new-attach")


def build(project_dir, monkeypatch, state, upstream_repo):
    """A studio in `state`. Returns the Chat (root, conn)."""
    c = Chat(project_dir, monkeypatch, studio=state in ("due", "confirmed", "done", "skipped"))
    if state in ("due", "confirmed", "done", "skipped"):
        if state != "due":
            db.meta_set(c.conn, adopt.FIRST_CHAT, state)
        return c
    git(c.root, "init", "-q")
    assert cli.main(["init"]) == 0
    c.conn = db.connect(c.root)
    if state == "fresh+look":
        c.look(upstream_repo.url)
    elif state.startswith("none-"):
        if state == "none-agent-onboarded":
            monkeypatch.setenv("CLAUDECODE", "1")
        else:
            for name in ownerword.REAL_SESSION_VARS:             # the owner's terminal has no session
                monkeypatch.delenv(name, raising=False)
        assert cli.main(["onboard", "--name", "x", "--who", "a:owner", "--what", "w"]) == 0
        monkeypatch.delenv("CLAUDECODE", raising=False)
        c.conn = db.connect(c.root)
    return c


@pytest.mark.parametrize("state", STATES)
def test_every_cell_of_the_table_verb_by_state_by_caller(state, project, monkeypatch, upstream):  # noqa: F811
    c = build(project, monkeypatch, state, upstream)
    try:
        for caller, kw in CALLERS.items():
            for verb, refused in zip(VERBS, REFUSED[state]):
                got = onboard_flow.setup_write_refusal(None if verb == "new-attach" else c.conn, c.root, verb, **kw)
                want = refused and caller != "owner"
                assert (got is not None) == want, "%s / %s / %s: got %r" % (state, verb, caller, got)
                if got:
                    assert got[0] in ("ONBOARD-FIRST-CHAT-OPEN", "ATTACH-NEEDS-CARD") and got[1]
    finally:
        c.close()


def test_the_caller_test_is_three_things_and_no_variable_switches_it():
    """Probe: the strings an agent could set cannot make a process the owner's terminal."""
    assert ownerword.owner_terminal(environ={}, isatty=lambda: True)
    assert not ownerword.owner_terminal(environ={}, isatty=lambda: False)
    assert not ownerword.owner_terminal(environ={"CLAUDECODE": "1"}, isatty=lambda: True)
    assert not ownerword.owner_terminal(environ={"ALPACA_SESSION_ID": "s"}, isatty=lambda: True)
    for name in ("ALPACA_NEW_ATTACH", "ALPACA_OWNER", "ALPACA_TTY", "ALPACA_ASSUME_TERMINAL", "TERM", "ALPACA_REHEARSAL"):
        assert not ownerword.owner_terminal(environ={name: "1"}, isatty=lambda: False), name


def test_the_real_terminal_probe_sees_no_terminal_on_dev_null_and_one_on_a_pty(tmp_path):
    """No patch: the probe a child process runs, with standard input from /dev/null and from a pseudo-terminal."""
    code = "from alpaca import ownerword; print(ownerword._stdin_is_tty())"
    env = dict(os.environ, PYTHONPATH=REPO)
    base = [sys.executable, "-c", code]
    nul = subprocess.run(base, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    assert nul.stdout.strip() == "False", nul.stdout + nul.stderr
    master, slave = os.openpty()
    try:
        tty = subprocess.run(base, env=env, stdin=slave, capture_output=True, text=True, timeout=60)
    finally:
        os.close(master)
        os.close(slave)
    assert tty.stdout.strip() == "True", tty.stdout + tty.stderr


# ------------------------------------------------------------------ the attack rows, through the real verbs

def _stripped(monkeypatch):
    for name in (*ownerword.REAL_SESSION_VARS, *ownerword.AGENT_MARKERS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ownerword, "_stdin_is_tty", lambda: False)


def test_C2a_a_stripped_child_cannot_attach_in_a_due_studio(chat, upstream, monkeypatch):  # noqa: F811
    before = snapshot(chat.root)
    _stripped(monkeypatch)
    rc, out = chat.run("attach", "init", upstream.url)
    assert rc != 0 and "ATTACH-NEEDS-CARD" in out, out
    assert snapshot(chat.root) == before and not os.path.exists(os.path.join(chat.root, "app"))


def test_C2b_a_stripped_child_cannot_write_project_yaml_mid_first_chat(project, monkeypatch, upstream):  # noqa: F811
    c = build(project, monkeypatch, "fresh+look", upstream)
    try:
        before = snapshot(c.root)
        _stripped(monkeypatch)
        rc, out = c.run("onboard", "--name", "x", "--who", "a:owner", "--what", "w", "--existing-repo")
        assert rc != 0 and "ONBOARD-FIRST-CHAT-OPEN" in out, out
        assert snapshot(c.root) == before and not project_is_onboarded(c)
    finally:
        c.close()


def project_is_onboarded(c):
    return project.is_onboarded(c.root)


def test_C9_an_agent_that_skips_the_look_cannot_onboard_flat_and_then_attach(project, monkeypatch, upstream):  # noqa: F811
    c = build(project, monkeypatch, "fresh", upstream)
    try:
        # CLAUDE.md rule 5 still lets an agent run the flat onboard in a fresh studio
        rc, out = c.run("onboard", "--name", "x", "--who", "a:owner", "--what", "w")
        assert rc == 0, out
        flat = [e for e in c.events("onboard")]
        assert flat and flat[-1]["data"].get("agent_call") is True
        before = snapshot(c.root)
        for how in ("agent", "stripped"):
            if how == "stripped":
                _stripped(monkeypatch)
            rc, out = c.run("attach", "init", upstream.url)
            assert rc != 0 and "ATTACH-NEEDS-CARD" in out, (how, out)
            assert snapshot(c.root) == before and not os.path.exists(os.path.join(c.root, "app"))
        # the owner at a terminal of their own may attach in that studio
        monkeypatch.setattr(ownerword, "_stdin_is_tty", lambda: True)
        rc, out = c.run("attach", "init", upstream.url)
        assert rc == 0, out
    finally:
        c.close()


def test_the_owners_flat_onboard_writes_no_agent_mark(project, monkeypatch):
    c = build(project, monkeypatch, "none-owner-onboarded", None)
    try:
        for name in (*ownerword.REAL_SESSION_VARS, *ownerword.AGENT_MARKERS):
            monkeypatch.delenv(name, raising=False)
        assert "agent_call" not in c.events("onboard")[-1]["data"]
    finally:
        c.close()


def test_the_new_attach_refusal_makes_no_studio_for_a_caller_without_a_terminal(tmp_path, upstream, monkeypatch):  # noqa: F811
    _stripped(monkeypatch)
    target = tmp_path / "studio"
    rc = cli.main(["new", "--name", "X", "--dir", str(target), "--attach", upstream.url, "--who", "t:owner", "--yes",
                   "--no-bootstrap", "--no-serve"])
    assert rc == cli.FAIL and not target.exists()


def test_a_real_child_with_no_markers_and_no_terminal_is_refused(chat, upstream):  # noqa: F811
    """No patch: a subprocess with stdin from /dev/null and every marker removed (the way an agent can start one)."""
    env = {k: v for k, v in os.environ.items()
           if k not in (*ownerword.REAL_SESSION_VARS, *ownerword.AGENT_MARKERS, "ALPACA_ROOT")}
    env["PYTHONPATH"] = REPO
    before = snapshot(chat.root)
    done = subprocess.run([sys.executable, "-m", "alpaca", "attach", "init", upstream.url], cwd=chat.root, env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    assert done.returncode != 0 and "ATTACH-NEEDS-CARD" in done.stdout, done.stdout + done.stderr
    assert snapshot(chat.root) == before


# ------------------------------------------------------------------ the skip needs the owner's own words (E1)

def _skip(c, quote):
    return c.run("onboard", "skip", "--quote", quote)


def test_C1_a_skip_that_quotes_any_other_owner_message_is_refused(chat, upstream):  # noqa: F811
    """The owner's opening message, or anything typed before the first chat's last step, is not a skip."""
    opening = chat.OPENING % upstream.url
    chat.look(upstream.url)                               # the owner typed the opening before the look
    before = snapshot(chat.root)
    rc, out = _skip(chat, opening)
    assert rc != 0 and "ONBOARD-NO-OWNER-PROOF" in out, out
    assert adopt.first_chat_state(chat.conn) == "due"
    # nor is a message that only holds the words: it has to be the whole message
    chat.say("please do not skip setup, I want it")
    rc, out = _skip(chat, "skip setup")
    assert rc != 0 and "ONBOARD-NO-OWNER-PROOF" in out, out
    rc, out = _skip(chat, "I want it")
    assert rc != 0 and "ONBOARD-NO-OWNER-PROOF" in out, out
    assert adopt.first_chat_state(chat.conn) == "due" and snapshot(chat.root) == before


def test_C1_a_skip_phrase_typed_before_the_last_step_of_the_first_chat_does_not_count(chat, upstream):  # noqa: F811
    chat.say("skip setup")                                # said early, then the agent went on and looked
    chat.look(upstream.url)
    rc, out = _skip(chat, "skip setup")
    assert rc != 0 and "ONBOARD-NO-OWNER-PROOF" in out, out
    assert adopt.first_chat_state(chat.conn) == "due"


def test_a_skip_phrase_typed_after_the_last_step_skips_even_after_a_sentence(chat, upstream):  # noqa: F811
    chat.look(upstream.url)
    chat.say("hmm, I am not sure I want all this")
    chat.say("Skip setup.")
    rc, out = _skip(chat, "skip setup")
    assert rc == 0, out
    assert adopt.first_chat_state(chat.conn) == "skipped"


def test_after_an_unrequested_skip_attempt_the_plain_attach_and_flat_onboard_are_still_refused(chat, upstream):  # noqa: F811
    chat.look(upstream.url)
    _skip(chat, chat.OPENING % upstream.url)
    for argv in (("attach", "init", upstream.url), ("onboard", "--name", "x", "--who", "a:owner", "--what", "w")):
        rc, out = chat.run(*argv)
        assert rc != 0, (argv, out)
    assert not os.path.exists(os.path.join(chat.root, "app"))


def test_every_skip_phrase_is_a_whole_message_form():
    from alpaca import onboard_card as card
    for phrase in card.SKIP_PHRASES:
        assert ownerword.is_sole_phrase(phrase, card.SKIP_PHRASES)
        assert not ownerword.is_sole_phrase(phrase + " but wait", card.SKIP_PHRASES)
