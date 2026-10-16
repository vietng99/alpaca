"""Fixtures for the sandbox tests (op-059 t-695): a throwaway project whose op carries a signed sandbox
answer, and a session that holds a task of that op. Verbatim answers from the record, with the studio
folder swapped for a path under the test's tmp dir."""
import os
import shutil
import types

from alpaca import claims, cli, db, ownerword

OP_062 = ("Writes stay in this project root and {studio} plus the session scratchpad; "
          "no system installs; no sudo")
OP_061 = ("Read and write only inside this project root, the session scratchpad and per-agent "
          "/tmp/op61-* pytest basetemps; network only to read github.com/example-org/alpaca tags and "
          "clone public tags; no new packages")
OP_056 = ("Read and write only inside this project root and the session scratchpad; no new packages "
          "except pytest-xdist into .venv if t-613 needs it, asked first")
OP_063 = "only this project root; no installs; nothing outside the repo is touched"
OP_045 = ("Write only inside this repo, task work in worktrees under .alpaca/worktrees/op045-*; "
          "read /srv/drop-in/Arthur-Web-Package; local headless Chrome for screenshots; "
          "no new installs")
SID = "sess-aaaa1111-0000-0000-0000-000000000000"


def add_op(conn, op, *, answer=None, state="answered", source="owner", status="open", sid=SID,
           batch=True, item_event=None):
    """An op row; with `batch` a batch whose `sandbox` item has the given state and answer."""
    db.append_event(conn, session="s", actor="human", kind="op-new", ref=op, data={"intent": "x"})
    db.upsert(conn, "ops", "id", {"id": op, "intent": "x", "status": status})
    if not batch:
        return None
    data = {"op": op, "item": "sandbox", "question": "Where may the work write?", "state": state}
    if state == "answered":
        data.update(answer=answer, answer_kind="text", source=source)
    return db.append_event(conn, session=sid, actor="alpaca", kind="batch-item", op=op,
                           ref="sandbox", data=data)


def add_task(conn, tid, op, *, sid=SID, worker="claude-sess", claim=True):
    db.upsert(conn, "tasks", "id", {"id": tid, "op": op, "phase": "build", "statement": "s",
                                     "title": "t", "status": "open"})
    if claim:
        claims.take(conn, tid, worker, session=sid)


def root_of(conn):
    """The project root of an open record connection."""
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    return os.path.realpath(os.path.dirname(os.path.dirname(path)))


def confirm(conn, op, paths, *, sid=SID, kind=None):
    """The owner-confirmed path list of `op` (round 4: the only thing the guard enforces), appended as
    `alpaca sandbox set` records it on the session path: the entries as `sandbox.entries` checks and resolves
    them, and an owner_word whose quote is the command line naming them, pointing at an owner message that holds
    that line in a transcript file of the fixture (round 5: a row without that proof is ignored; round 6: a
    terminal-path row is never honored). Tests of the verb prove how the session path is found."""
    import json
    from alpaca import sandbox
    from alpaca.tests.ownerword_fixtures import typed
    kind = kind or sandbox.SET_KIND
    root = root_of(conn)
    clean, resolved = sandbox.entries(paths, root)
    line = sandbox.command_line(sandbox._VERB[kind], op, clean)
    folder = os.path.join(os.path.dirname(root), ".fx-owner-transcripts")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "%s.jsonl" % sid)
    stamp = "2026-10-16T00:00:00.000Z"
    with open(path, "ab") as fh:
        offset = fh.tell()
        fh.write((json.dumps(typed(line, ts=stamp)) + "\n").encode("utf-8"))
    with open(path, "rb") as fh:
        line_no = fh.read(offset).count(b"\n") + 1
    word = {"path": "session", "transcript": path, "line": line_no, "offset": offset, "timestamp": stamp,
            "answer": "yes", "quote": line}
    return db.append_event(conn, session=sid, actor="owner", kind=kind, op=op, ref=sandbox.ITEM,
                           data={"op": op, "paths": clean, "resolved": resolved, "owner_word": word})


def widen(conn, op, paths, *, sid=SID):
    """A proved `sandbox-widen` row (see `confirm`)."""
    from alpaca import sandbox
    return confirm(conn, op, paths, sid=sid, kind=sandbox.WIDEN_KIND)


def make(project, tmp_path, *, op="op-062", answer=None, sid=SID, claim=True, confirm_paths=None, **kw):
    """A project whose `op` holds a signed sandbox answer and whose session `sid` holds t-1 of it.
    `confirm_paths`: None confirms what the answer proposes when the owner answered it (so the guard
    tests run under the list the owner would confirm), False confirms nothing, a list confirms that list."""
    from alpaca import sandbox
    cli.main(["init"])
    root = os.path.realpath(project)
    studio = os.path.join(os.path.realpath(str(tmp_path)), "studio")
    os.makedirs(studio, exist_ok=True)
    conn = db.connect(root)
    text = answer if answer is not None else OP_062.format(studio=studio)
    ev = add_op(conn, op, answer=text, sid=sid, **kw)
    if confirm_paths is None:
        owner_answered = (ev is not None and kw.get("state", "answered") == "answered"
                          and kw.get("source", "owner") == "owner")
        confirm_paths = sandbox.proposal(sandbox.parse_answer(text, root)) if owner_answered else []
    set_ev = confirm(conn, op, confirm_paths, sid=sid) if confirm_paths else None
    if claim:
        add_task(conn, "t-1", op, sid=sid)
    return types.SimpleNamespace(root=root, conn=conn, sid=sid, op=op, studio=studio,
                                 event=ev["id"] if ev else None,
                                 set_event=set_ev["id"] if set_ev else None, other=os.path.join(
                                     os.path.realpath(str(tmp_path)), "studio-2"))


def ask(env, op, sid=None):
    return ownerword.record_ask(env.conn, sid or env.sid, "batch:%s" % op, "shown", op=op)


def use_temp_base(monkeypatch, tmp_path):
    """Point the sandbox temp base at a throwaway folder, so a test never makes `/tmp/sbx<sid8>`."""
    base = os.path.join(os.path.realpath(str(tmp_path)), "tmpbase")
    os.makedirs(base, exist_ok=True)
    monkeypatch.setattr("alpaca.sandbox.TEMP_BASE", base)
    return base


def rm_real_temp(sid):
    """Remove the real `/tmp/sbx<sid8>` a test made through a child process, only when its marker
    holds this session id (what the code under test does)."""
    from alpaca import sandbox
    path = os.path.join("/tmp", sandbox.TEMP_PREFIX + sandbox.sid8(sid))
    if sandbox._marker_of(path) == sid:
        shutil.rmtree(path, ignore_errors=True)
