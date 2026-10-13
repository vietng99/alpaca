"""Shared helpers for the AAR hub tests (op-057 t-625). Not a test module.

The attacks build a throwaway record with the real verbs (aar owe/new/seal, task move), read it
through the real hub (hub.overview -> hub.page_overview, the body /hub/overview.json serves) and
render the real web modules in node (alpaca/web/roadmap.js). Each fact is compared with its one
source (cause C1): aar.owed for owed and its triggers, the report on disk plus a seal that still
hashes for sealed, the report file for draft, aaritems.overdue for overdue.

Names the attacks assume of the build, and nothing else about naming:
  * the visible labels are the task text's: "AAR owed", "AAR draft", "AAR sealed" (any case)
  * an owed trigger is shown with its event id written "event:N"
  * the hub reads the facts through the module attributes (aar.owed or aar.triggers, and
    aaritems.overdue), so a monkeypatch of those reaches the hub: there is one source
  * a key on an op row, task row or roadmap node that carries AAR facts has "aar" or "overdue" in its
    name, and a row with no AAR fact carries no such key (cause C4: the page stays as it is today)
  * a roadmap card is article[data-rm-node=<task id>] (roadmap.js today); its overdue mark is a
    class or visible text containing "overdue"
  * the report link is an attribute (href or data-*) naming "aar" and the op id or the report path
"""
import json
import re
import urllib.parse
from pathlib import Path

from alpaca import aar, aaritems, cli, db, hub
from alpaca.tests import aar_items_kit as items
from alpaca.tests.hub_not_now_kit import (NODE, REPO, WEB, added_lines, all_rules, base, color, contrast,  # noqa: F401
                                          declared, git, hub_js, node, over, paint, resolve, tokens, tree)

OP, OTHER = items.OP, items.OTHER
OWED = re.compile(r"\bAAR owed\b", re.I)
DRAFT = re.compile(r"\bAAR draft\b", re.I)
SEALED = re.compile(r"\bAAR sealed\b", re.I)
NOT_OWED = re.compile(r"\bnot owed\b", re.I)
OVERDUE = re.compile(r"overdue", re.I)
UNREADABLE_DUE = re.compile(r"not readable|unreadable|could not be read|cannot be read", re.I)
AARISH = re.compile(r"aar|overdue", re.I)
RENDER = "const {renderRoadmap,renderPhaseMap}=await import('./roadmap.js');"
REASON = "the rollback step was skipped in the release"
OWNER = "fixture-keeper"


# ------------------------------------------------------------------ the record
def setup(project, monkeypatch):
    """op-001 closed with t-001 done, op-002 open with t-002 open; the clock frozen at items.NOW."""
    return items.setup(project, monkeypatch)


def last(conn, kind):
    return conn.execute("SELECT MAX(id) FROM events WHERE kind=?", (kind,)).fetchone()[0]


def count(conn, kind=None):
    q = "SELECT COUNT(*) FROM events" + (" WHERE kind=?" if kind else "")
    return conn.execute(q, (kind,) if kind else ()).fetchone()[0]


def owe(conn, op=OP, reason=REASON):
    items.ok("aar", "owe", op, "--reason", reason)
    return last(conn, aar.KIND_OWE)


def draft(op=OP):
    items.ok("aar", "new", op)


def seal(conn, project, op=OP, rows=None):
    """Fill the report of `op` (drafting it when there is none) and seal it; returns the aar-seal id."""
    items.author(conn, project, op, rows or [items.row("Alpha follow-up", owner=OWNER, due="2026-12-01")])
    code, out = items.seal(op)
    assert code == cli.PASS, out
    return last(conn, aar.KIND_SEAL)


def report(project, op=OP):
    return Path(aar.report_path(str(project), op))


def rel(op=OP):
    return ".alpaca/aar/%s.md" % op


def reopen(conn, project, tid="t-002"):
    """Move `tid` to done and back to open: one reopen trigger. Returns its task-move id."""
    status = db.rows(conn, "tasks", "id=?", (tid,))[0]["status"]
    if status != "doing":
        items.ok("task", "move", tid, "doing", "--reason", "picked up for the test")
    items.finish(project, tid)
    items.ok("task", "move", tid, "open", "--reason", "found a gap after done")
    return last(conn, "task-move")


LATE = [("Alpha late by date", "2026-10-01"), ("Bravo due next week", "2026-10-20"),
        ("Kilo late and parked", "2026-09-30"), ("Lima late then done", "2026-10-02")]


def overdue_record(project, monkeypatch):
    """op-001's AAR sealed with four items filed into the standing op: Alpha late and open, Bravo
    not due, Kilo late and deferred (Not now), Lima late but done. Returns (conn, {action: task id})."""
    conn = setup(project, monkeypatch)
    items.author(conn, project, OP, [items.row(a, owner=OWNER, due=d) for a, d in LATE])
    code, out = items.seal(OP)
    assert code == cli.PASS, out
    tid = {a: items.by_action(conn, OP, a) for a, _d in LATE}
    items.defer(tid["Kilo late and parked"])
    items.finish(project, tid["Lima late then done"])
    return conn, tid


def owed_source(project):
    conn = db.connect(str(project))
    try:
        return aar.owed(conn, str(project))
    finally:
        conn.close()


def overdue_source(project):
    conn = db.connect(str(project))
    try:
        return aaritems.overdue(conn)
    finally:
        conn.close()


def standing(project):
    conn = db.connect(str(project))
    try:
        return aaritems.standing_op(conn)
    finally:
        conn.close()


# ------------------------------------------------------------------ the hub
def full(project):
    return hub.overview(Path(project))


def page(project):
    """The page body /hub/overview.json serves, through JSON as the browser gets it."""
    return json.loads(json.dumps(hub.page_overview(full(project)), default=str))


def op_row(o, op):
    return next(r for r in o["ops"] if r["id"] == op)


def aar_keys(row):
    return {k: v for k, v in row.items() if AARISH.search(k)}


def render(o, call, tmp, prelude=""):
    return node(prelude + RENDER + "out(%s);" % call, o, tmp)


def op_view(o, op, tmp, opts=None, prelude=""):
    return render(o, "renderRoadmap(DATA,%s)" % (opts or "{op:%s}" % json.dumps(op)), tmp, prelude)


def text(html):
    return tree(html).text()


def view_text(project, op, tmp):
    return text(op_view(page(project), op, tmp))


def smallest(html, rx):
    """The smallest elements whose visible text matches `rx`."""
    hits = [e for e in tree(html).walk() if rx.search(e.text())]
    return [e for e in hits if not any(rx.search(c.text()) for c in e.walk())]


def keyed_holder(el):
    """The nearest element at or above `el` that carries a data-key."""
    for e in [el, *el.ancestors()]:
        if getattr(e, "attrs", {}).get("data-key"):
            return e
    return None


def block_of(html, rx):
    """The keyed element that holds the first label matching `rx` (the AAR block of the op view)."""
    got = smallest(html, rx)
    assert got, "the view shows no %r" % rx.pattern
    return keyed_holder(got[0]), got[0]


SKIP_ATTRS = {"data-key", "data-rm-group", "data-rm-key", "class", "id", "aria-controls", "data-rm-node",
              "data-rm-sub", "data-rm-details", "data-rm-focus"}


def report_refs(html, op):
    """Attribute values (decoded) that name "aar" and either the report path or the op id."""
    out = []
    for e in tree(html).walk():
        for k, v in e.attrs.items():
            if k in SKIP_ATTRS or not v:
                continue
            d = urllib.parse.unquote(v)
            if re.search(r"aar", d, re.I) and (rel(op) in d or re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(op), d)):
                out.append(d)
    return out


def ev(text_, n):
    """True when `text_` names event:n (and not event:n0, event:n1, ...)."""
    return re.search(r"event:%d(?!\d)" % n, text_) is not None


def cards(html):
    return {e.attrs["data-rm-node"]: e for e in tree(html).walk()
            if e.tag == "article" and e.attrs.get("data-rm-node")}


def marked(card):
    return (any(OVERDUE.search(c) for e in [card, *card.walk()] for c in e.classes)
            or bool(OVERDUE.search(card.text())))


def strip_events(o):
    """`o` with no task or node key that names aar/overdue: the page as it would be with no mark."""
    o = json.loads(json.dumps(o))
    for t in o.get("tasks", []):
        for k in [k for k in t if AARISH.search(k)]:
            del t[k]
    return o
