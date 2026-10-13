"""The action items of a sealed after-action report, as tasks (op-057 t-623).

  aar seal <op>   files each row of the Action items table as a task: in the op named by --into,
                  else in the standing After-action items op, opened at the first seal that needs
                  it. The task sits in the plan group aar-<op>, with its owner, due and size as
                  expected lines and its done-when as the done bar, every line derived=aar:<op>.
  status, RESUME  list the filed items that are past due and not done under Overdue AAR items.
  aar new <op>    lists every filed item of another op that is not done under Carried over.

Decisions, so a later reader does not have to guess:
- Filing and the seal event share one transaction that aar.seal opens, so a seal that fails to
  file leaves nothing behind. A re-seal is also idempotent: an item is known by the sha256 of its
  source op, its action text and its place among equal rows, so a line shift files nothing, an
  added row files one task, and two equal rows are two items.
- The task's contract is the one store of an item's owner, due, size and done-when. The aar-seal
  event holds each item's identity and the id of the contract event the seal last wrote. A
  re-seal rewrites a contract, whole, only while that event is still the task's newest. Once
  anyone else records one, the seal leaves it as it is and prints a note per field that differs,
  on every re-seal until they agree. A row gone from the report leaves its task open and is named.
- The standing op is found by an aar-items-op event written with its open, never by its title.
  There is one: a seal never opens a second while one is open. When its plan has no room the
  seal refuses and names --into, and its own report files its items into itself. ops.current_op, the default intake op and
  `alpaca start` skip it, and a task of it is the Next action only when no other task is open.
- A task of an op with a batch takes the phase build. Contract lines over 600 characters are cut
  with "...", titles over 60 are cut at a word, so check and seal agree.
- Not done means any status but done: open, doing, blocked and deferred all count.
- Overdue compares a date due with the date of the record's newest event (the record clock), never
  the wall clock. aar check accepts only a date YYYY-MM-DD; a week due filed before op-057 t-629 is
  late after its Sunday.
- Overdue and Carried over read the first owner, due and size line of the newest contract. A
  missing line shows as (none). An owner that is not a short token shows as (see its contract).
  A due that names no day the grammar accepts is listed under Overdue as not readable.
- An Overdue line holds no title or other report text, and shows the owner only as a short token
  without : = | >, so no line can read as a status claim to the record check.
"""
import datetime
import hashlib
import json
import re

from alpaca import aar, db, ops, proof, sessions_view, taskcontract, taskplan

STANDING_TITLE = "After-action items"
STANDING_INTENT = "After-action items: the action items filed from sealed after-action reports"
STANDING_DONE = "it stays open as the home of filed action items"
GROUP = "aar-%s"                       # the plan group of one AAR's items, in the op they go to
PHASE = "build"                        # the phase a filed task takes in an op with a batch
_ESC = re.compile(r"\\([!-/:-@\[-`{-~])")   # a CommonMark backslash escape of ASCII punctuation
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_WEEK = re.compile(r"(\d{4})-W(\d{2})")
_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@+ -]{0,39}")   # no : = | > so no claim can form
IDENTITY = ("key", "task", "op", "line", "action", "type", "contract_event")   # all a seal stores per item
NONE = "(none)"                        # a contract line that is not there
SEE = "(see its contract)"             # a line that is there but is not shown


# --------------------------------------------------------------------------- decoding (pure)
def plain(cell):
    """A table cell as one line of text: whitespace folded, backslash escapes removed."""
    return _ESC.sub(r"\1", " ".join(str(cell or "").split()))


def token(cell):
    """plain(cell) without the stars and underscores aar._due and aar._size strip."""
    return plain(cell).strip(" *_")


def key_of(source, action, k):
    """The identity of one action item: its source op, its action text and its place among rows
    with the same text. The line number is not part of it."""
    text = " ".join(plain(action).split()).casefold()
    return hashlib.sha256(("%s\n%s\n%d" % (source, text, k)).encode("utf-8")).hexdigest()


def due_by(due):
    """The last day a due allows, YYYY-MM-DD: the date, or the Sunday of a week. None for anything
    else (an op id, task id or version filed before t-629), which overdue lists as not readable."""
    if _DAY.fullmatch(due or ""):
        try:
            return datetime.date.fromisoformat(due).isoformat()
        except ValueError:
            return None
    m = _WEEK.fullmatch(due or "")
    if m:
        try:
            return datetime.date.fromisocalendar(int(m.group(1)), int(m.group(2)), 7).isoformat()
        except ValueError:
            return None
    return None


def title_of(action, source, line):
    """A task title of at most ops.TITLE_MAX characters: the action, cut at a word with "..."."""
    text = plain(action)
    if not text:
        return "action item from the %s AAR line %d" % (source, line)
    if len(text) <= ops.TITLE_MAX:
        return text
    cut = text[:ops.TITLE_MAX - 3]
    space = cut.rfind(" ")
    return (cut[:space] if space > 30 else cut) + "..."


def fit(text):
    """`text` cut to what one contract line holds, ending in "..." when it was cut."""
    text = str(text)
    return text if len(text) <= taskcontract.MAX_TEXT else text[:taskcontract.MAX_TEXT - 3] + "..."


def contract_of(source, rel, line, item):
    """The contract of a filed task. One function, so filing and updating write the same shape.
    The owner cell is the report author's word: a derived line, not a claim and not an owner quote."""
    return {"input": [fit("%s line %d" % (rel, line))],
            "expected": [fit("owner: " + item["owner"]), fit("due: " + item["due"]),
                         fit("size: " + item["size"])],
            "done_bar": [fit(item["done_when"])],
            "origin": "derived=aar:%s" % source}


# --------------------------------------------------------------------------- the readers
def filed(conn, source=None):
    """Every filed item, first seen first: the aar-seal events fold per (source, key), the latest
    seal of a source winning. Each item is the identity of the item plus "source" and "seal" (event
    id); its owner, due, size and done-when are in the task's contract."""
    sql, args = "SELECT id, ref, data FROM events WHERE kind=?", [aar.KIND_SEAL]
    if source is not None:
        sql, args = sql + " AND ref=?", args + [source]
    out = {}
    for row in conn.execute(sql + " ORDER BY id", args):
        try:
            data = json.loads(row["data"])
        except (TypeError, ValueError):
            continue
        items = data.get("filed", {}).get("items") if isinstance(data, dict) and isinstance(
            data.get("filed"), dict) else None
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and isinstance(item.get("key"), str):
                out[(row["ref"], item["key"])] = dict({k: item.get(k) for k in IDENTITY},
                                                  source=row["ref"], seal=row["id"])
    return list(out.values())


def _tasks(conn):
    return {r["id"]: r for r in conn.execute("SELECT id, op, status FROM tasks")}


def _open(task):
    """The one place that says "not done": open, doing, blocked and deferred all count."""
    return task is not None and task["status"] != "done"


def _text(value):
    return value if isinstance(value, str) else ""


def _task(tasks, tid):
    """The task row for a stored task id, or None: a damaged seal payload may hold any value."""
    return tasks.get(tid) if isinstance(tid, str) else None


def _line(lines, name):
    """(index, value) of the first `name: value` line of a contract part, or (None, None)."""
    prefix = name + ": "
    for i, text in enumerate(lines if isinstance(lines, list) else []):
        if isinstance(text, str) and text.startswith(prefix):
            return i, text[len(prefix):].strip()
    return None, None


def _event(value):
    """A stored contract event id, or None: a damaged payload may hold any value (True == 1)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def fields_of(contract):
    """{"owner", "due", "size", "done_bar"} as a stored contract holds them: the text of the first
    `owner: `, `due: ` and `size: ` expected line (None when there is none) and the done bar lines.
    The one reader of these fields, so Overdue, Carried over and the re-seal read a contract alike."""
    c = contract if isinstance(contract, dict) else {}
    out = {f: _line(c.get("expected"), f)[1] for f in ("owner", "due", "size")}
    bar = c.get("done_bar")
    out["done_bar"] = [t.strip() for t in bar if isinstance(t, str)] if isinstance(bar, list) else []
    return out


def _differs(have, want):
    """The fields where the contract `have` does not hold what the report `want` says; both come
    from fields_of. The done-when agrees when it is one of the done bar lines, so a person may add
    a bar line without a note on every seal."""
    out = [f for f in ("owner", "due", "size") if have[f] != want[f]]
    if not set(want["done_bar"]) <= set(have["done_bar"]):
        out.append("done_when")
    return out


def _said(value):
    """A field value for the seal output: one line of at most 60 characters, (none) when missing."""
    if isinstance(value, list):
        value = " / ".join(value)
    text = " ".join(str(value or "").split())
    if not text:
        return NONE
    return text if len(text) <= 60 else text[:57] + "..."


def _key(field):
    return "done_bar" if field == "done_when" else field


def _owner_shown(owner, other):
    """The owner as a reader shows it: (none) without a line, the token, else `other`."""
    return NONE if owner is None else owner if _OWNER.fullmatch(owner) else other


# --------------------------------------------------------------------------- the standing op
def standing_op(conn):
    """The id of the open After-action items op, found by its marker event, or None."""
    return ops.standing_op(conn, ops.STANDING_KIND)


def open_standing(conn, session):
    """Open the standing op and mark it. Called only inside seal's transaction, and never while
    one is open: there is one standing After-action items op."""
    have = standing_op(conn)
    if have is not None:
        raise ValueError("the standing After-action items op %s is open; a second one is never "
                         "opened" % have)
    return ops.open_standing(conn, ops.STANDING_KIND, STANDING_INTENT, STANDING_DONE, STANDING_TITLE,
                             session=session, actor="agent")


# --------------------------------------------------------------------------- filing
def _room(conn, op, source, n):
    """True when the plan of `op` can take the group of `source` (if it lacks one) and n tasks."""
    plan = taskplan.latest(conn, op)
    groups = [g.get("id") for g in plan["groups"]]
    return ((GROUP % source in groups or len(groups) < taskplan.MAX_GROUPS)
            and len(plan["tasks"]) + n <= taskplan.MAX_TASKS)


def _default_fits(conn, skip, source, n):
    """True when a seal without --into would file n items: the standing op (never `skip`, the op
    --into named) has room, or there is no standing op and the items fit one plan."""
    have = standing_op(conn)
    if have is None:
        return n <= taskplan.MAX_TASKS
    return have != skip and _room(conn, have, source, n)


def _target(conn, source, into, rows_new, session):
    """(target op, opened op id or None). Refusals leave nothing written."""
    if into is not None:
        row = aar.op_row(conn, into)
        if row["status"] == "closed":
            raise proof.Refusal(["--into %s: the op is closed; pass an open op" % row["id"]])
        if row["id"] == source:
            raise proof.Refusal(["the action items of %s cannot be filed into %s itself; pass "
                                 "--into another open op" % (source, source)])
        if rows_new and not _room(conn, row["id"], source, len(rows_new)):
            msg = "--into %s: its plan is full (at most %d groups and %d tasks); pass --into another open op" % (
                row["id"], taskplan.MAX_GROUPS, taskplan.MAX_TASKS)
            if _default_fits(conn, row["id"], source, len(rows_new)):
                msg += ", or leave --into off"
            raise proof.Refusal([msg])
        return row["id"], None
    if not rows_new:
        return None, None
    target = standing_op(conn)
    if target is None:
        if len(rows_new) > taskplan.MAX_TASKS:
            raise proof.Refusal(["the %d action items of %s do not fit one plan (at most %d tasks); "
                                 "pass --into an open op with room" % (
                                     len(rows_new), source, taskplan.MAX_TASKS)])
        target = open_standing(conn, session)
        return target, target
    if not _room(conn, target, source, len(rows_new)):
        raise proof.Refusal(["the standing After-action items op %s has no room for the %d action "
                             "items of %s (at most %d groups and %d tasks); pass --into another "
                             "open op" % (target, len(rows_new), source, taskplan.MAX_GROUPS,
                                          taskplan.MAX_TASKS)])
    return target, None


def file_items(conn, root, source, text, *, into=None, session="cli"):
    """File the Action items of the report `text` of op `source`. Runs inside seal's transaction.
    Returns {"into", "opened", "new", "kept", "updated", "differs", "gone", "items"}; see seal_lines."""
    if not conn.in_transaction:
        raise RuntimeError("file_items runs inside seal's transaction")
    from alpaca import batch
    rel = aar._rel(root, aar.report_path(root, source))
    seen, items, want = {}, [], {}
    for row in aar.parse_actions(text):
        norm = " ".join(plain(row["action"]).split()).casefold()
        k = seen[norm] = seen.get(norm, -1) + 1
        key = key_of(source, row["action"], k)
        items.append({"key": key, "task": None, "op": None, "line": row["line"],
                      "action": plain(row["action"]), "type": token(row["type"]),
                      "contract_event": None})
        want[key] = {"owner": token(row["owner"]), "due": token(row["due"]),
                     "size": token(row["size"]), "done_when": plain(row["done_when"])}
    before = {f["key"]: f for f in filed(conn, source)}
    fresh = [i for i in items if i["key"] not in before]
    target, opened = _target(conn, source, into, fresh, session)
    out = {"into": target, "opened": opened, "new": [], "kept": [], "updated": [], "differs": [],
           "gone": [], "items": items}
    if fresh:
        gid = GROUP % source
        if gid not in [g["id"] for g in taskplan.latest(conn, target)["groups"]]:
            taskplan.group(conn, target, gid, "After-action items of %s" % source, "after-action",
                           session=session, actor="agent")
        phase = PHASE if batch.has_batch(conn, target) else None
    tasks = _tasks(conn)
    contracts = taskcontract.latest(conn)      # read here, inside the transaction, never earlier
    for item in items:
        old = before.get(item["key"])
        new = contract_of(source, rel, item["line"], want[item["key"]])
        if old is None:
            tid = ops.add_task(conn, target, item["action"], title_of(item["action"], source, item["line"]),
                               phase=phase,
                               why="from the after-action report of %s, line %d" % (source, item["line"]),
                               session=session, actor="agent",
                               extra={"aar": {"source": source, "key": item["key"], "line": item["line"]}},
                               mapping={"group": gid, "after": [], "resources": []})
            try:
                ev = taskcontract.record(conn, tid, new, session=session, actor="agent", root=root)
            except ValueError as exc:
                raise ValueError("%s (line %d): %s" % (tid, item["line"], exc)) from exc
            item["task"], item["op"], item["contract_event"] = tid, target, ev["id"]
            out["new"].append(tid)
            continue
        tid = item["task"] = old.get("task")
        item["op"] = old.get("op")
        mine = item["contract_event"] = _event(old.get("contract_event"))
        out["kept"].append(tid)
        if not _open(_task(tasks, tid)):
            continue                           # a done item's contract is never rewritten
        have = contracts.get(tid) if isinstance(tid, str) else None
        try:
            then, now = fields_of(have), fields_of(taskcontract.check(new))
        except ValueError as exc:
            raise ValueError("%s: %s" % (tid, exc)) from exc
        diff = _differs(then, now)
        if not diff:
            continue                           # a line shift alone writes nothing
        said = [[tid, f, _said(then[_key(f)]), _said(now[_key(f)])] for f in diff]
        # the seal rewrites a contract only while its own is the newest: then nothing a person set is lost
        if have is None or (mine is not None and have.get("event_id") == mine):
            try:
                ev = taskcontract.record(conn, tid, new, session=session, actor="agent", root=root)
            except ValueError as exc:
                raise ValueError("%s: %s" % (tid, exc)) from exc
            item["contract_event"] = ev["id"]
            out["updated"] += said
        else:
            out["differs"] += said
    here = {i["key"] for i in items}
    out["gone"] = [f["task"] for key, f in before.items() if key not in here and _open(_task(tasks, f.get("task")))]
    return out


def seal_lines(res):
    """The lines `aar seal` prints after its GATE line, one per thing it filed, kept or noted."""
    lines = []
    if res.get("new"):
        lines.append("filed %d action item(s) into %s: %s" % (len(res["new"]), res["into"],
                                                              ", ".join(res["new"])))
    if res.get("opened"):
        lines.append('opened %s "%s", the standing op for action items' % (res["opened"], STANDING_TITLE))
    by_task = {i.get("task"): i for i in res.get("items") or []}
    if res.get("kept"):
        lines.append("kept %d action item(s) filed by an earlier seal: %s" % (
            len(res["kept"]), ", ".join("%s (%s)" % (t, (by_task.get(t) or {}).get("op")) for t in res["kept"])))
    for tid, field, old, new in res.get("updated") or []:
        lines.append("updated the contract of %s: %s %s is now %s" % (tid, field.replace("_", "-"), old, new))
    quoted = lambda v: v if v == NONE else '"%s"' % v
    for tid, field, have, want in res.get("differs") or []:
        lines.append("note: %s %s is %s on the task and %s in the report; a seal writes a task's "
                     "contract only while its own is the newest, so it left this one as it is; "
                     "change it with alpaca task contract %s" % (
                         tid, field.replace("_", "-"), quoted(have), quoted(want), tid))
    for tid in res.get("gone") or []:
        lines.append("note: %s was filed by an earlier seal and its row is gone from the report; it "
                     "stays open until moved" % tid)
    return lines


# --------------------------------------------------------------------------- overdue
def overdue(conn, today=None):
    """The filed items past due and not done, oldest due first, then those whose due line cannot be
    read: [{"id", "op", "source", "line", "owner", "due", "due_by", "days_late", "status"}]; the
    last three are None for an unreadable due. `today` is the record clock's date."""
    today = today or (sessions_view.record_now(conn) or "")[:10]
    if not _DAY.fullmatch(today):
        return []
    try:
        now = datetime.date.fromisoformat(today)
    except ValueError:
        return []
    tasks = _tasks(conn)
    contracts = taskcontract.latest(conn)
    out = []
    for item in filed(conn):
        task = _task(tasks, item.get("task"))
        if not _open(task):
            continue
        f = fields_of(contracts.get(task["id"]))
        due = token(f["due"] or "")            # the form aar._due reads: stars and underscores off
        if not due:
            continue                           # no due line: never late
        d = due_by(due)
        line = item.get("line")
        row = {"id": task["id"], "op": task["op"], "source": item["source"],
               "line": line if isinstance(line, int) and not isinstance(line, bool) else None,
               "owner": _owner_shown(f["owner"], None), "status": task["status"]}
        if d is None:                          # a due line that names no day
            out.append(dict(row, due=None, due_by=None, days_late=None))
            continue
        if d >= today:
            continue
        try:
            late = (now - datetime.date.fromisoformat(d)).days
        except (ValueError, TypeError):
            continue
        out.append(dict(row, due=due[:40], due_by=d, days_late=late))
    out.sort(key=lambda r: (r["due_by"] is None, r["due_by"] or "", r["id"]))
    return out


# --------------------------------------------------------------------------- carried over
def carried(conn, op):
    """The filed items of other ops' reports whose task is not done, by task id. Each row adds
    "task", "event" (the task-add event of its task) and the contract's owner, due and size."""
    tasks = _tasks(conn)
    contracts = taskcontract.latest(conn)
    adds = dict(conn.execute("SELECT ref, MIN(id) FROM events WHERE kind='task-add' GROUP BY ref").fetchall())
    out = []
    for item in filed(conn):
        tid = item.get("task")
        if item["source"] == op or not _open(_task(tasks, tid)) or tid not in adds:
            continue
        f = fields_of(contracts.get(tid))
        out.append({"task": tid, "event": adds[tid], "source": item["source"],
                    "due": f["due"] or NONE, "size": f["size"] or NONE,
                    "owner": _owner_shown(f["owner"], SEE), "action": _text(item.get("action"))})
    return sorted(out, key=lambda r: r["task"])


def cited(conn, op):
    """{task-add event id: task id} of every item filed from another op's report, whatever its
    status: a sealed report is a snapshot, so finishing a task must not break it."""
    adds = dict(conn.execute("SELECT MIN(id), ref FROM events WHERE kind='task-add' GROUP BY ref").fetchall())
    by_task = {t: e for e, t in adds.items()}
    return {by_task[i["task"]]: i["task"] for i in filed(conn)
            if i["source"] != op and isinstance(i.get("task"), str) and i["task"] in by_task}
