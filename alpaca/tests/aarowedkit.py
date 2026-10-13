"""Shared helpers for the t-622 aar owed tests: two ops with one task each, the CLI runner, a sealed
AAR on an open op (reviewed and accepted first, as the t-624 seal gate asks), and the record reads
every attack asserts on. Not a test file."""
import os

from alpaca import cli, db
from alpaca.tests import aarreviewkit, proofkit
from alpaca.tests.test_aar import fill, read, report, write

OP, OTHER = "op-001", "op-002"
BASIS = "the verb prints upper case in the smoke run"
BLOCKED_HEAD = "GATE alpaca-op-close: BLOCKED ("


def run(capsys, *argv):
    capsys.readouterr()
    code = cli.main([str(a) for a in argv])
    got = capsys.readouterr()
    return code, got.out + got.err


def count(conn, kind=None, op=None):
    q, p = "SELECT COUNT(*) FROM events WHERE 1=1", []
    if kind:
        q += " AND kind=?"; p.append(kind)
    if op:
        q += " AND op=?"; p.append(op)
    return conn.execute(q, p).fetchone()[0]


def last_id(conn, kind=None):
    q = "SELECT MAX(id) FROM events" + (" WHERE kind=?" if kind else "")
    return conn.execute(q, (kind,) if kind else ()).fetchone()[0]


def two_ops(project):
    """op-001 with t-001 and op-002 with t-002, both open, both tasks open. Returns the connection."""
    assert cli.main(["init"]) == cli.PASS
    assert cli.main(["op", "new", "add the shout verb", "--done-when", "the verb prints upper case"]) == 0
    assert cli.main(["task", "add", "--title", "shout verb", OP, "write the shout verb", "--phase", "build"]) == 0
    assert cli.main(["op", "new", "an unrelated op", "--done-when", "nothing to see"]) == 0
    assert cli.main(["task", "add", "--title", "other work", OTHER, "other work here", "--phase", "build"]) == 0
    return db.connect(project)


def move(tid, status, *, session=None, proof=None, reason="moved for the test", extra=()):
    argv = (["--session", session] if session else []) + ["task", "move", tid, status, "--reason", reason]
    if proof:
        argv += ["--proof", proof]
    argv += list(extra)
    return cli.main(argv)


def finish(project, conn, tid, *, session=None):
    """Move `tid` to doing (when it is not) and then to done with a sealed proof."""
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    if row["status"] != "doing":
        assert move(tid, "doing", session=session) == 0
    ptr = proofkit.seal_for(project, tid, conn=conn)
    assert move(tid, "done", session=session, proof=ptr, reason="the work is finished") == 0


def reopen(conn, tid, to="open", *, session=None):
    """Move a done task back to `to`; returns the task-move event id of the reopen."""
    extra = ()
    if to == "deferred":
        extra = ("--reactivate-when", "after the next release")
    assert move(tid, to, session=session, reason="found a gap after done", extra=extra) == 0
    return last_id(conn, "task-move")


def back_to_done(project, conn, tid, *, session=None):
    """Bring a reopened task back to done, the way an agent would before closing the op."""
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    if row["status"] in ("deferred", "blocked"):
        assert move(tid, "open", session=session) == 0
    finish(project, conn, tid, session=session)


def op_open_event(conn, op):
    return conn.execute("SELECT MIN(id) FROM events WHERE kind='op-open' AND op=?", (op,)).fetchone()[0]


def review(project, op=OP):
    """Record an accepted review of the report of `op` as it is now (aarreviewkit.accept)."""
    code, out, _path = aarreviewkit.accept(project, op, os.path.dirname(project))
    assert code == cli.PASS, out


def seal_aar(project, conn, capsys, op=OP):
    """Draft (when there is no report), fill, review and seal the AAR of `op`; returns the aar-seal
    id."""
    path = report(project, op)
    if not os.path.exists(path):
        code, out = run(capsys, "aar", "new", op)
        assert code == cli.PASS, out
        write(path, fill(read(path), op_open_event(conn, op)))
    review(project, op)
    code, out = run(capsys, "aar", "seal", op)
    assert code == cli.PASS, out
    return last_id(conn, "aar-seal")


def close(capsys, op=OP):
    return run(capsys, "op", "close", op, "--basis", BASIS)


def op_status(conn, op=OP):
    return conn.execute("SELECT status FROM ops WHERE id=?", (op,)).fetchone()["status"]


def assert_blocked(conn, code, out, op=OP, *, names=(), events=()):
    """The close was refused the way the tasks-not-done path refuses, and nothing was written."""
    assert code == cli.BLOCKED, out
    line = [l for l in out.splitlines() if l.startswith(BLOCKED_HEAD)]
    assert len(line) == 1, out
    low = line[0].lower()
    for word in names:
        assert word.lower() in low, (word, out)
    for ev in events:
        assert "event:%d" % ev in line[0], (ev, out)
    assert count(conn, "op-close", op) == 0
    assert op_status(conn, op) == "open"
    return line[0]


def assert_closed(conn, code, out, op=OP):
    assert code == cli.PASS, out
    assert count(conn, "op-close", op) == 1
    assert op_status(conn, op) == "closed"


def owed(capsys, op=None):
    code, out = run(capsys, *(["aar", "owed"] + ([op] if op else [])))
    assert code in (cli.PASS, cli.BLOCKED), (code, out)
    return out
