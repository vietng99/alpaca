"""A first chat to drive in tests (op-062 t-656): a studio with the first chat due, a session whose transcript
sits where Claude Code writes it, and owner and assistant lines in the shapes Claude Code writes.

    chat = Chat(project, monkeypatch)           # `project` is the conftest fixture
    chat.say("I took over this repo ...")       # an owner message, typed
    rc, out = chat.run("onboard", "look", url, "--scratch", chat.scratch, "--quote", text)
    chat.host(card_text)                        # what the assistant showed the owner

Time is a clock that moves ten seconds with every record event and every transcript line, so "typed after
the ask" is the order the lines were made in, on any machine.
"""
from __future__ import annotations

import datetime
import io
import json
import os
import shutil
from contextlib import redirect_stdout

from alpaca import cli, db, ownerword, util
from alpaca.tests import ownerword_fixtures as owf
from alpaca.tests.conftest import REPO


class TickClock:
    def __init__(self, start=None, step=10):
        self.t = start or datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        self.step = datetime.timedelta(seconds=step)

    def __call__(self):
        self.t += self.step
        return self.t.isoformat()


def assistant(text, ts, sidechain=False):
    return {"type": "assistant", "isSidechain": sidechain, "uuid": "a", "timestamp": ts, "sessionId": "s",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def make_due_studio(root, git_init=True):
    """The studio `alpaca new` leaves behind: a git repository with the shipped .gitignore and a provisional
    onboarding, so the first chat is due."""
    from alpaca.tests.attachkit import git
    if git_init:
        git(root, "init", "-q")
        shutil.copy(os.path.join(REPO, "templates", "release", "gitignore"), os.path.join(str(root), ".gitignore"))
    assert cli.main(["init"]) == 0
    assert cli.main(["onboard", "--provisional", "--name", "demo", "--who", "ada:owner", "--what", "a studio for tests"]) == 0
    return root


class Chat:
    def __init__(self, project, monkeypatch, sid="chat-1", studio=True, clock=True):
        self.root = os.path.realpath(str(project))
        if os.path.basename(self.root) == "src":
            self.root = os.path.dirname(self.root)
        self.sid = sid
        self.clock = TickClock() if clock else None
        if clock:
            util.set_clock(self.clock)
            self._undo_clock = True
        self.cfg = os.environ["CLAUDE_CONFIG_DIR"]
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
        monkeypatch.delenv("ALPACA_SESSION_ID", raising=False)
        monkeypatch.chdir(self.root)
        if studio:
            make_due_studio(self.root)
        self.conn = db.connect(self.root)
        self.env = {"CLAUDE_CONFIG_DIR": self.cfg, "CLAUDE_CODE_SESSION_ID": sid}
        self.transcript = os.path.join(self.cfg, "projects", ownerword.project_slug(self.root), sid + ".jsonl")
        os.makedirs(os.path.dirname(self.transcript), exist_ok=True)
        open(self.transcript, "a").close()
        db.upsert(self.conn, "sessions", "sid", {"sid": sid, "started": self.now(), "transcript": self.transcript})
        db.append_event(self.conn, session=sid, actor="alpaca", kind="session-start", data={"transcript": self.transcript})
        self.scratch = os.path.join(os.path.dirname(self.root), "scratch", "onboard-look")

    def now(self):
        return util.now_iso()

    def _append(self, entry):
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def say(self, text, **extra):
        """An owner message, typed."""
        self._append(owf.typed(text, ts=self.now(), **extra))
        return text

    def say_click(self, text):
        """A tool_result in the owner's shape (a click on an AskUserQuestion option): never an owner word."""
        entry = owf.tool_result(ts=self.now())
        entry["message"]["content"][0]["content"] = text
        self._append(entry)

    def host(self, text, sidechain=False):
        """Text the assistant showed the owner."""
        self._append(assistant(text, self.now(), sidechain=sidechain))
        return text

    def run(self, *argv):
        """Run `alpaca <argv>` in this process; (exit code, stdout)."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(list(argv))
        return rc, buf.getvalue()

    OPENING = "I took over this project from my old team, it is at %s . not sure where to start"
    REPLY = "1a. I want to add a hello feature. don't touch src/main.py"
    GOAL = "I want to add a hello feature"
    LIMIT = "don't touch src/main.py"

    def look(self, source, opening=None):
        """The owner names a repo; the agent looks. Returns the printed lines."""
        text = self.say(opening or self.OPENING % source)
        rc, out = self.run("onboard", "look", source, "--scratch", self.scratch, "--quote", text)
        assert rc == 0, out
        self.host("turn 1")
        return out

    def answers(self, reply=None):
        """The owner's reply 1 and the agent's records of it."""
        reply = self.say(reply or self.REPLY)
        for args in (["others-commit", "--option", "a"], ["goal", "--value", self.GOAL], ["limits", "--value", self.LIMIT]):
            rc, out = self.run("onboard", "answer", *args, "--quote", reply)
            assert rc == 0, out
        rc, out = self.run("onboard", "answer", "done-guess", "--value", "hello works", "--source", "agent")
        assert rc == 0, out
        self.answer_round_two()
        return reply

    CI_REPLY = "1a. there is no CI, I only build it on my laptop"

    def answer_round_two(self):
        """`onboard next`, as the first chat runs it; when it asks where the checks run (a repo with no CI file), the
        owner answers in a second message and the agent records it. Returns the printed lines."""
        rc, out = self.run("onboard", "next")                # exit 1 prints round 2; 0 says ready
        assert rc in (0, 1) and ("round 2" in out or "ready" in out), out
        if "[ci]" in out:
            reply = self.say(self.CI_REPLY)
            rc, done = self.run("onboard", "answer", "ci", "--option", "a", "--quote", reply)
            assert rc == 0, done
        return out

    def propose(self, show=True):
        """Run propose; the assistant shows the card unchanged (unless `show` is False). Returns (id, card)."""
        rc, out = self.run("onboard", "propose")
        assert rc == 0, out
        card = out.rstrip("\n")
        cid = card.split()[2]
        if show:
            self.host(card)
        return cid, card

    def walk_to_card(self, source, show=True):
        self.look(source)
        self.answers()
        return self.propose(show=show)

    def events(self, kind=None):
        conn = db.connect(self.root)
        try:
            return db.events(conn, kind=kind, limit=100000)
        finally:
            conn.close()

    def close(self):
        if getattr(self, "_undo_clock", False):
            util.set_clock(None)


import pytest


@pytest.fixture
def chat(project, monkeypatch):
    c = Chat(project, monkeypatch)
    yield c
    c.close()
