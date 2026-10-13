"""The AAR state of each op, worded for the hub (op-057 t-625).

The hub copies what alpaca/aar.py and alpaca/aaritems.py already decide and never re-derives it:
`aar.owed` says which triggers are still owed, `aar.answered` says what the latest seal and the
report on disk say, `aaritems.overdue` says which filed action items are late. This module only
turns those facts into one label and the lines under it, so the page holds no status rule and no
date math. aar.py and aaritems.py never import it.
"""
from collections import Counter

from alpaca import aar, aaritems

LABELS = {"owed": "AAR owed", "changed": "AAR changed since its seal", "sealed": "AAR sealed",
          "draft": "AAR draft"}
OPEN_LINE = "Op close is blocked until the report is sealed (alpaca aar seal %s) or the owner waives it."
UNTIED_LINE = ("A NO-GO or release check FAIL no op is tied to also blocks this op: sealing this op's report "
               "answers it, alpaca aar owe <op> --event <id> moves it to the op it belongs to, or the owner "
               "waives it.")
CLOSED_LINE = "This op is closed; the owed report is listed and blocks nothing."
DRAFT_LINE = "A draft report is on disk."
LATER_LINE = "The report sealed at event:%d answers the earlier triggers; the ones listed came after it."
SEALED_LINE = "Sealed at event:%d."


def failed(what, exc):
    """One line saying a read failed, so a broken reader never looks like an empty one."""
    text = " ".join(str(exc).split())
    return "%s could not be read: %s: %s" % (what, type(exc).__name__, text[:200])


def overdue_mark(row):
    """The mark on the roadmap card of one late action item (words from aaritems.overdue's row)."""
    n = row.get("days_late")
    if isinstance(n, int) and not isinstance(n, bool):
        label = "Overdue %d day%s, due %s" % (n, "" if n == 1 else "s", row.get("due_by"))
    else:
        label = "Due not readable (see its contract)"
    return {"source": row.get("source"), "due_by": row.get("due_by"), "days_late": n, "label": label}


def _sentence(note):
    return note[:1].upper() + note[1:] + "." if note else ""


def state(op_row, due, ans, overdue=0):
    """The one label rule: `due` is this op's owed triggers, `ans` is aar.answered. A dict, or None
    when there is nothing to show."""
    out = {}
    lines = []
    seal, report, note = ans.get("seal"), ans.get("report"), ans.get("note") or ""
    if due:
        badge = "owed"
        lines.append(CLOSED_LINE if op_row.get("status") == "closed" else OPEN_LINE % op_row["id"])
        if op_row.get("status") != "closed" and any(t.get("held") for t in due):
            lines.append(UNTIED_LINE)
        if report and seal is None:
            lines.append(DRAFT_LINE)
        if report and ans.get("holds"):
            lines.append(LATER_LINE % seal)
        if note:
            lines.append(_sentence(note))
        if op_row.get("status") != "closed":
            out["blocks_close"] = True
        out["triggers"] = [{"id": t["id"], "text": aar.describe(t)} for t in due]
    elif seal is not None and ans.get("holds"):
        badge = "sealed"
        lines.append(SEALED_LINE % seal)
    elif seal is not None:
        badge = "changed"
        lines.append(_sentence(note))
    elif report:
        badge = "draft"
    else:
        badge = None
    if badge:
        out.update(badge=badge, label=LABELS[badge])
        if lines:
            out["lines"] = lines
        if report:
            out["report"] = report
        if seal is not None:
            out["seal"] = seal
    if overdue:
        out["overdue"] = overdue
    return out or None


def project(conn, root, ops):
    """(states {op id: dict}, marks {task id: dict}) for the op rows `ops`. Ops with no fact are
    absent; a reader that fails is named on the op rows instead of reading as empty."""
    late_error = None
    try:
        late = aaritems.overdue(conn)
    except Exception as exc:
        late, late_error = [], failed("Overdue AAR items", exc)
    marks = {r["id"]: overdue_mark(r) for r in late}
    by_source = Counter(r["source"] for r in late)
    states = {}
    try:
        owed = aar.owed(conn, str(root))[0]
        got = {}
        for row in ops:
            s = state(row, owed.get(row["id"], []), aar.answered(conn, str(root), row["id"]),
                      by_source.get(row["id"], 0))
            if s:
                got[row["id"]] = s
        states = got
    except Exception as exc:
        err = failed("AAR state", exc)
        states = {row["id"]: {"error": err} for row in ops}
    if late_error:
        for row in ops:
            states[row["id"]] = dict(states.get(row["id"]) or {}, overdue_error=late_error)
    return states, marks
