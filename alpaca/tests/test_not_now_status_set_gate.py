"""(expected[0], class A): every task-status set a reader spells out (an IN (...) list,
a `not in (...)` tuple, a SQL CASE, a sort-order dict, a `!= 'done'` test) is a place a fifth
status gets dropped or absorbed without anyone deciding it (t-616 C01). In the reader files this
task names, each such line must sit near a word that shows deferred was decided there ('deferred',
'not_now', 'Not now', or a named status set from ops), or be one of the reviewed lines below that
are right to leave deferred out. Line numbers move, so lines are matched by their text."""
import os
import re

import alpaca

REPO = os.path.dirname(os.path.dirname(os.path.abspath(alpaca.__file__)))
READERS = ["pad", "cli", "export", "hub", "hub_checklist", "taskplan", "mission", "work_record",
           "sessions_view", "ops"]
WINDOW = 8

_W = r"""['"](?:open|doing|done|blocked)['"]"""
SET = re.compile(r"""(?:\b(?:IN|in)\s*\(\s*%s|WHEN\s+%s|order\s*=\s*\{\s*%s|status['"\]\)]*\s*(?:!=|<>)\s*%s)"""
                 % (_W, _W, _W, _W))
DECIDED = re.compile(r"deferred|not_now|Not now|NOT_NOW|\bops\.[A-Z][A-Z_]+\b|\b[A-Z][A-Z_]*(?:WORKABLE|LIVE|SETTLED|ACTIVE|PARKED)[A-Z_]*\b")

# Reviewed: these leave deferred out on purpose (trace.md 1.2, 1.5, 1.7; t-616 build.md).
REVIEWED = [
    ("cli", "status IN ('open','doing')"),                       # tasks_open: workable count
    ("ops", "status IN ('open','doing')"),                       # first_open_task: Next action source
    ("ops", 't[0]["status"] != "open"'),                         # claim needs open
    ("ops", 'args.status != "doing"'),                           # move clears the claim
    ("hub_checklist", "task['last_completion'] and task['status'] != 'done'"),  # historical report note
    ("taskplan", "task['status'] != 'done' or ident not in entries"),          # evidence of done tasks
    ("taskplan", "status != 'done' and task.get('reason')"),     # inside the finished-state branch
    ("mission", "task['link_kind'] != 'fix' or task['status'] != 'done'"),     # fix credit needs done
    ("work_record", 'row.get("status") != "doing"'),             # assigned session of doing work
    ("sessions_view", 'op["status"] != "open"'),                 # an op, not a task
]


def test_every_status_set_in_a_reader_decided_deferred():
    repo = REPO
    undecided = []
    for mod in READERS:
        path = os.path.join(repo, "alpaca", mod + ".py")
        lines = open(path, encoding="utf-8").read().splitlines()
        for i, line in enumerate(lines):
            if not SET.search(line):
                continue
            if any(m == mod and s in line for m, s in REVIEWED):
                continue
            near = "\n".join(lines[max(0, i - WINDOW): i + WINDOW + 1])
            if not DECIDED.search(near):
                undecided.append("alpaca/%s.py:%d %s" % (mod, i + 1, line.strip()[:120]))
    assert undecided == [], "status sets with no deferred decision:\n" + "\n".join(undecided)


def test_the_known_drop_sites_are_still_found():
    """The gate itself must see the sites this task names (if the regex rots, it passes vacuously)."""
    probe = ["tasks = db.rows(conn, \"tasks\", \"status IN ('open','doing','blocked') ORDER BY id\")",
             "order = {\"doing\": 0, \"open\": 1, \"blocked\": 2, \"done\": 3}",
             "for t in db.rows(conn, \"tasks\", \"status != 'done' ORDER BY id\")]",
             "open_ = [t for t in tasks if t.get(\"status\") not in (\"done\", \"cancelled\", \"canceled\")]",
             "active = [row for row in tasks if row['status'] not in ('done','cancelled','canceled')]",
             "elif status in ('done', 'failed', 'skipped', 'cancelled', 'canceled'):",
             "\"SELECT * FROM tasks ORDER BY CASE status WHEN 'doing' THEN 0 WHEN 'blocked' THEN 1\""]
    assert all(SET.search(p) for p in probe), [p for p in probe if not SET.search(p)]
