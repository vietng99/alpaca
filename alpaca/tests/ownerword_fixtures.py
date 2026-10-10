"""Fixtures for the owner-word tests (op-019 t-201): a project root, a session whose transcript sits
where Claude Code writes it, and transcript lines in the two owner-message shapes (built from the
real lines: a typed prompt of session 4d8376e4, a queued_command of session 89d39740 line 2196)."""
import json
import os

from alpaca import db, ownerword

LATE = "2999-01-01T00:00:00.000Z"
EARLY = "2000-01-01T00:00:00.000Z"


def typed(text, ts=LATE, entrypoint="cli", **extra):
    entry = {"parentUuid": "p", "isSidechain": False, "promptId": "x", "type": "user",
             "message": {"role": "user", "content": text}, "uuid": "u", "timestamp": ts,
             "origin": {"kind": "human"}, "promptSource": "typed", "userType": "external",
             "entrypoint": entrypoint, "sessionId": "s"}
    entry.update(extra)
    return entry


def queued(text, ts=LATE, entrypoint="cli", **extra):
    entry = {"parentUuid": "p", "isSidechain": False,
             "attachment": {"type": "queued_command", "prompt": text, "source_uuid": "s",
                            "commandMode": "prompt", "origin": {"kind": "human"},
                            "timestamp": ts, "humanTurn": True},
             "type": "attachment", "uuid": "a", "timestamp": ts, "userType": "external",
             "entrypoint": entrypoint, "sessionId": "s"}
    entry.update(extra)
    return entry


def tool_result(ts=LATE):
    return {"type": "user", "origin": {"kind": "human"}, "timestamp": ts, "entrypoint": "cli",
            "message": {"role": "user", "content": [{"type": "tool_result", "content": "q1 yes"}]}}


class Project:
    """A tmp project with one session and its transcript at the place Claude Code writes it."""

    def __init__(self, tmp_path, monkeypatch, sid="sess-1"):
        self.root = os.path.realpath(str(tmp_path / "proj"))
        os.makedirs(self.root)
        with open(os.path.join(self.root, "ALPACA-MANIFEST"), "w") as fh:
            fh.write("alpaca\n")
        self.cfg = os.path.realpath(str(tmp_path / "cfg"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", self.cfg)
        self.sid = sid
        self.conn = db.connect(self.root)
        self.env = {"CLAUDE_CONFIG_DIR": self.cfg, "CLAUDE_CODE_SESSION_ID": sid}
        self.add_session(sid)

    def transcript_of(self, sid):
        return os.path.join(self.cfg, "projects", ownerword.project_slug(self.root), sid + ".jsonl")

    def add_session(self, sid):
        path = self.transcript_of(sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "a").close()
        db.upsert(self.conn, "sessions", "sid", {"sid": sid, "started": "2026-10-04T00:00:00",
                                                 "transcript": path})
        db.append_event(self.conn, session=sid, actor="alpaca", kind="session-start",
                        data={"transcript": path})
        return path

    def write(self, entries, sid=None):
        with open(self.transcript_of(sid or self.sid), "w") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")

    def ask(self, subject, text="asked", sid=None):
        return ownerword.record_ask(self.conn, sid or self.sid, subject, text)

    def write_as(self, subject, answer, quote, **kw):
        kw.setdefault("environ", self.env)
        return ownerword.human_write(self.conn, self.sid, "owner", subject=subject, answer=answer,
                                     quote=quote, root=self.root, **kw)

    def op(self, op="op-1"):
        db.append_event(self.conn, session="s", actor="human", kind="op-new", ref=op,
                        data={"intent": "x"})
        db.upsert(self.conn, "ops", "id", {"id": op, "intent": "x", "status": "open"})
        return op
