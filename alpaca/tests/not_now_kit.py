"""Shared helpers for the t-617 Not now tests (Not now list in every projection). Not a test module.

Each attack drives the real verbs through cli.main on a throwaway project (the `project`
fixture), then reads the projections the way their writers build them: pad.render (RESUME.md and
the `alpaca status` text), pad.render_checklist (CHECKLIST.md), export.render (data.json),
hub.overview, taskplan.project, mission.project.

Names the attacks assume of the build (nothing else about naming is assumed):
  * the RESUME / status heading is exactly '## Not now' (the task text names it)
  * the marker for a met structured trigger reads 'trigger met: reconsider' (compared without case)
  * data.json carries, for each deferred task, a dict with "id" plus the reason text, the trigger
    text and a boolean under a key whose name contains "met" (key names are the builder's)
  * a taskplan node of a deferred task has a label that says "Not now" (compared without case)
"""
import io
import json
import re
from contextlib import redirect_stdout
from pathlib import Path

from alpaca import cli, clock, db, util

HEAD = "## Not now"
MARK = "trigger met: reconsider"
R1, W1 = "waiting on the vendor", "when the vendor ships v2"


def run(*argv):
    """(exit code, stdout) of one verb; an argparse exit is folded into its code."""
    out = io.StringIO()
    with redirect_stdout(out):
        try:
            code = cli.main([str(a) for a in argv])
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return code, out.getvalue()


def ok(*argv):
    code, out = run(*argv)
    assert code == 0, "%s -> %s\n%s" % (" ".join(map(str, argv)), code, out)
    return out


def setup(project, tasks=1, onboard=True, phase="build"):
    """init (+ onboard) + op-001 + `tasks` open tasks t-001.. in it. Returns a connection."""
    ok("init")
    if onboard:
        ok("onboard", "--name", "demo", "--who", "v:owner", "--what", "w")
    ok("op", "new", "the first op", "--done-when", "done")
    for i in range(tasks):
        add_task("op-001", "task %d" % (i + 1), "statement %d" % (i + 1), phase)
    return db.connect(project)


def add_task(op, title, statement, phase="build"):
    return ok("task", "add", "--title", title, op, statement, "--phase", phase)


def new_op(intent="the second op"):
    out = ok("op", "new", intent, "--done-when", "done")
    m = re.search(r"op-\d+", out)
    return m.group(0) if m else None


def defer(tid, reason=R1, when=W1):
    return ok("task", "move", tid, "deferred", "--reason", reason, "--reactivate-when", when)


def finish(project, tid):
    from alpaca.tests import proofkit
    return ok("task", "move", tid, "done", "--proof", proofkit.seal_for(project, tid))


def raw_defer(conn, tid, reason=R1, when=W1, frm="open"):
    """A deferral written the way ops.py writes it, for records the verb would refuse (a bad
    trigger set by hand, a mission root that never ran init)."""
    db.patch(conn, "tasks", "id", tid, {"status": "deferred", "claimant": None, "lease_until": None})
    db.append_event(conn, session="hand", actor="hand", kind="task-move", ref=tid,
                    data={"from": frm, "to": "deferred", "proof": None, "reason": reason,
                          "reactivate_when": when})


def freeze(monkeypatch, iso):
    """Install a fixed process clock (record stamps and the wall clock read the same)."""
    monkeypatch.setattr(util, "_CLOCK", clock.FixedClock(iso))


def open_phase(conn, op="op-001", boundary="design->build"):
    db.append_event(conn, session="planner", actor="operator", kind="phase-advance", op=op,
                    data={"boundary": boundary, "mode": "human-go"})


def plan(conn, entries, op="op-001", group="build"):
    """Record a task plan: entries is a list of (id, [after ids])."""
    from alpaca import taskplan
    data = {"version": 1, "revision": 0, "groups": [{"id": group, "title": "Implementation"}],
            "tasks": [dict(id=i, group=group, after=list(a), resources=[], requires_stages=[],
                           hold=None) for i, a in entries]}
    return taskplan.record(conn, op, data, session="planner", actor="planner")


# ------------------------------------------------------------------ projections
def resume(project):
    from alpaca import pad
    return pad.render(project)


def checklist(project):
    from alpaca import pad
    return pad.render_checklist(project)


def data(project):
    from alpaca import export
    return json.loads(export.render(project))


def nodes(project, conn):
    from alpaca import taskplan, work_record
    view = taskplan.project(conn, work_record.tasks(conn), root=Path(project), stages=())
    return {n["id"]: n for op in view["operations"] for g in op["groups"] for n in g["nodes"]}


# ------------------------------------------------------------------ text helpers
def section(text, heading=HEAD):
    """The lines under `heading` up to the next '## ' heading; None when the heading is absent."""
    lines = text.splitlines()
    if heading not in lines:
        return None
    i = lines.index(heading) + 1
    out = []
    while i < len(lines) and not lines[i].startswith("## "):
        out.append(lines[i])
        i += 1
    return out


def not_now(text):
    """The '## Not now' section lines; fails the attack with a plain message when it is absent."""
    got = section(text)
    assert got is not None, "no '## Not now' section"
    return got


def has_id(line, tid):
    return re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(tid), line) is not None


def first_id(line):
    """The first task id on a line: the task a list entry is about (a trigger such as after:t-002
    comes after it)."""
    m = re.search(r"(?<![\w-])t-\d+(?![\w-])", line)
    return m.group(0) if m else None


def entries(lines, tid):
    """The unindented lines whose first task id is `tid`: the entries about that task."""
    if isinstance(lines, str):
        lines = lines.splitlines()
    return [l for l in lines if l[:1] not in (" ", "\t") and first_id(l) == tid]


def blocks(lines, tid):
    """Each entry about `tid` plus the indented lines right under it, joined: one block per entry."""
    if isinstance(lines, str):
        lines = lines.splitlines()
    out = []
    for i, line in enumerate(lines):
        if line[:1] in (" ", "\t") or first_id(line) != tid:
            continue
        got = [line]
        j = i + 1
        while j < len(lines) and lines[j][:1] in (" ", "\t") and lines[j].strip():
            got.append(lines[j])
            j += 1
        out.append("\n".join(got))
    return out


def marked(text):
    return MARK in (text or "").lower()


def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from strings(v)


def not_now_entries(payload, tid):
    """data.json dicts for `tid` that carry a met flag: the Not now rows, wherever they sit."""
    return [d for d in walk(payload) if d.get("id") == tid and met_flags(d)]


def met_flags(d):
    return [v for k, v in d.items() if "met" in str(k).lower() and isinstance(v, bool)]


def event_count(conn):
    return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
