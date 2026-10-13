"""Shared setup for the t-623 attack files: AAR action items become tasks, overdue items show in
status and RESUME.md, and the next `aar new` lists open items under Carried over. Not a test module.

The attacks drive the verbs through cli.main on a throwaway project (the `project` fixture) and
read the record with SQL and the readers that already exist (taskplan.latest, taskcontract.latest,
pad.render, freshness.check). Names they assume of the build, and nothing else about naming:
  * `alpaca aar seal <op> [--into <op>]` (the task text names --into)
  * every filed task is mapped in group aar-<source op> of the op it went to (task text)
  * the status / RESUME heading for overdue items reads "## Overdue AAR items" (compared without
    case; trace.md section 4)
  * the draft heading for carried items is exactly "## Carried over" (task text)
  * a carried row names its task id and its source op and ends in (event:N), N being that task's
    task-add event (trace.md section 5)
Seams read or wrapped: aar.SEAL_HOOKS, aar.KIND_SEAL, aar.latest_seal, aar.SECTIONS,
ops.current_op, intake._open_op, db.upsert and db.append_event (wrapped to inject a failure).
Every seal here goes through the t-624 review first (`reviewed`, aarreviewkit.accept), so a refused
seal is refused for the reason its attack names, never for a missing review.
"""
import io
import json
import os
import re
from contextlib import redirect_stderr, redirect_stdout

from alpaca import aar, cli, clock, db, paths, taskcontract, taskplan, util
from alpaca.tests import aarreviewkit, proofkit
from alpaca.tests.test_aar import HEAD, SEP, closed_op, event_id, fill, read, report, sections, write

OP, OTHER = "op-001", "op-002"
NOW = "2026-10-13T10:00:00+00:00"
TODAY = NOW[:10]
OVERDUE = "## Overdue AAR items"
CARRIED = "## Carried over"
STANDING = "after-action items"


# ------------------------------------------------------------------ verbs
def freeze(monkeypatch, iso=NOW):
    """A fixed process clock: record stamps and the wall clock read the same."""
    monkeypatch.setattr(util, "_CLOCK", clock.FixedClock(iso))


def run(*argv):
    """(exit code, stdout+stderr) of one verb; an argparse exit is folded into its code."""
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(out):
        try:
            code = cli.main([str(a) for a in argv])
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return code, out.getvalue()


def attempt(*argv):
    """(code, output, exception): a crash is an outcome the attack may accept or refuse."""
    try:
        code, out = run(*argv)
    except Exception as exc:          # noqa: BLE001 - the attack inspects the record afterwards
        return None, "", exc
    return code, out, None


def ok(*argv):
    code, out = run(*argv)
    assert code == 0, "%s -> %s\n%s" % (" ".join(map(str, argv)), code, out)
    return out


def reviewed(op):
    """Record an accepted review of the report of `op` as it is now; (code, output). A report that
    fails the content rules gets no review, and the seal that follows names its problems."""
    root = paths.root()
    return aarreviewkit.accept(root, op, os.path.dirname(root))[:2]


def seal(op, *more):
    """aar seal after the review it needs (`reviewed`)."""
    reviewed(op)
    return run("aar", "seal", op, *more)


def check(op):
    return run("aar", "check", op)


def finish(project, tid):
    return ok("task", "move", tid, "done", "--proof", proofkit.seal_for(project, tid))


def defer(tid, reason="waiting on the vendor", when="when the vendor ships v2"):
    return ok("task", "move", tid, "deferred", "--reason", reason, "--reactivate-when", when)


def block(tid, reason="the vendor is late"):
    return ok("task", "move", tid, "blocked", "--reason", reason)


# ------------------------------------------------------------------ reports
def row(action, owner="agent", due="2026-10-20", size="S", done="ten green runs", kind="fix"):
    return "| %s | %s | %s | %s | %s | %s |" % (action, owner, due, size, done, kind)


def table(*rows):
    return "\n".join([HEAD, SEP] + list(rows))


def setup(project, monkeypatch, iso=NOW):
    """init, op-001 closed with t-001 done, op-002 open with t-002 open (test_aar.closed_op)."""
    freeze(monkeypatch, iso)
    return closed_op(project)


def author(conn, project, op, rows, over=None):
    """Write a filled report for `op` whose Action items table holds `rows`; returns the text.
    Drafts it first when no report exists; a section the fill does not name keeps its text."""
    path = report(project, op)
    if not os.path.exists(path):
        ok("aar", "new", op)
    body = {"Action items": table(*rows)}
    body.update(over or {})
    text = fill(read(path), event_id(conn, "op-open", op=op), body)
    write(path, text)
    return text


def draft(project, op, force=False):
    ok(*(["aar", "new", op] + (["--force"] if force else [])))
    return read(report(project, op))


def heads(text):
    return [name for name, _body in sections(text)]


# ------------------------------------------------------------------ the record
def op_ids(conn):
    return [r[0] for r in conn.execute("SELECT id FROM ops ORDER BY id")]


def filed(conn, src):
    """[(task id, op)] of every task mapped in group aar-<src>, over every op's latest plan."""
    out = []
    for o in op_ids(conn):
        try:
            plan = taskplan.latest(conn, o)
        except ValueError:
            continue
        out += [(t["id"], o) for t in plan.get("tasks", []) if t.get("group") == "aar-%s" % src]
    return sorted(out)


def ids(conn, src):
    return [t for t, _o in filed(conn, src)]


def entry(conn, tid):
    op = task(conn, tid)["op"]
    return next(t for t in taskplan.latest(conn, op)["tasks"] if t["id"] == tid)


def groups(conn, op):
    return [g["id"] for g in taskplan.latest(conn, op)["groups"]]


def task(conn, tid):
    return db.rows(conn, "tasks", "id=?", (tid,))[0]


def by_action(conn, src, action):
    """The filed task whose statement or title holds `action` (the row it came from)."""
    hits = [t for t in ids(conn, src)
            if action in (task(conn, t)["statement"] or "") or action in (task(conn, t)["title"] or "")]
    assert len(hits) == 1, "action %r maps to %d filed task(s): %s" % (action, len(hits), hits)
    return hits[0]


def contract(conn, tid):
    return taskcontract.latest(conn).get(tid)


def contract_lines(c):
    """[(part, text, origin)] of a stored contract."""
    origins = taskcontract.origins_of(c)
    return [(part, text, origins[part][i]) for part in taskcontract.PARTS
            for i, text in enumerate(c.get(part) or [])]


def count(conn, kind):
    return conn.execute("SELECT COUNT(*) FROM events WHERE kind=?", (kind,)).fetchone()[0]


def snapshot(conn):
    """What a seal may write: tasks, ops and the event kinds of filing and sealing."""
    return {"tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "ops": tuple(op_ids(conn)),
            "events": {k: count(conn, k) for k in ("task-add", "task-plan", "task-contract",
                                                  "op-open", aar.KIND_SEAL)}}


def task_add_id(conn, tid):
    return event_id(conn, "task-add", ref=tid)


def standing_ops(conn, before):
    """The ops opened since `before` (a set of op ids)."""
    return sorted(set(op_ids(conn)) - set(before))


def op_words(conn, op):
    """Intent, done-when and every title the record gives `op`, lower case, for the name check."""
    words = [str(v) for v in db.rows(conn, "ops", "id=?", (op,))[0].values() if v]
    for e in db.events(conn, limit=10 ** 9):
        if e.get("op") == op and e["kind"] in ("op-open", "op-title"):
            words.append(json.dumps(e.get("data") or {}))
    return " ".join(words).lower()


# ------------------------------------------------------------------ projections
def resume(project):
    from alpaca import pad
    return pad.render(project)


def section(text, heading):
    """The lines under `heading` (compared without case) up to the next '## ' heading; None when
    the heading is absent."""
    lines = (text or "").splitlines()
    want = heading.strip().lower()
    at = next((i for i, l in enumerate(lines) if l.strip().lower() == want), None)
    if at is None:
        return None
    out = []
    for line in lines[at + 1:]:
        if line.startswith("## "):
            break
        out.append(line)
    return out


def overdue(text):
    return section(text, OVERDUE)


def has_id(line, tid):
    return re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(tid), line) is not None


def lines_of(lines, tid):
    return [l for l in (lines or []) if has_id(l, tid)]


def listed(lines, tid):
    return bool(lines_of(lines, tid))
