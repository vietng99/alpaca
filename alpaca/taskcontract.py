"""The contract of one task: its input, expected output, done bar and fail cases.

A task statement often carries all four in one sentence ("cva6 first physical run: 6_final.gds
non-empty, route DRC 0 ... at most 3 single-knob attempts"). An engineer checking the task
needs them apart. `alpaca task contract <id>` appends one `task-contract` event with the four
lists; the newest event per task is its current contract, and earlier ones stay in the record.
A task may name the profile stage it runs (alpaca/profile.py `stages`), so its page can show that
stage's latest run contract. A project without a profile has no stages, so it names none.

A contract restates what the task asks. It never grades the task: a task closes only with a
sealed proof report.
"""
from alpaca import db

KIND = "task-contract"
PARTS = ("input", "expected", "done_bar", "fail_cases")
MAX_ITEMS, MAX_TEXT = 20, 600


def _items(value, name):
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise ValueError("%s must be a list of at most %d lines" % (name, MAX_ITEMS))
    out = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > MAX_TEXT:
            raise ValueError("each %s line must be text of 1 to %d characters" % (name, MAX_TEXT))
        out.append(item.strip())
    return out


def check(data, stages=()):
    """The contract as it will be recorded, or ValueError. `stages` is the profile's stage list
    (alpaca/profile.py); a stage outside it is refused, and with no profile every stage is."""
    if not isinstance(data, dict):
        raise ValueError("contract must be an object")
    out = {part: _items(data.get(part), part) for part in PARTS}
    if not out["done_bar"]:
        raise ValueError("a contract needs at least one done bar line")
    stage = data.get("stage") or None
    if stage is not None and stage not in stages:
        if not stages:
            raise ValueError("no profile declares stages, so a contract cannot name one")
        raise ValueError("stage must be one of the profile stages: " + ", ".join(stages))
    out["stage"] = stage
    source = data.get("source")
    out["source"] = source.strip()[:MAX_TEXT] if isinstance(source, str) and source.strip() else None
    return out


def record(conn, task, data, *, session, actor):
    rows = db.rows(conn, "tasks", "id=?", (task,))
    if not rows:
        raise ValueError("no task %s" % task)
    from alpaca import profile
    stages = profile.for_conn(conn).stages()
    kept = check(data, tuple(str(s) for s in stages) if isinstance(stages, (list, tuple)) else ())
    return db.append_event(conn, session=session, actor=actor, kind=KIND, op=rows[0]["op"], ref=task, data=kept)


def latest(conn):
    """{task id: the newest contract with its event id, time and author}."""
    out = {}
    import json
    for row in conn.execute("SELECT id,ts,actor,ref,data FROM events WHERE kind=? ORDER BY id", (KIND,)):
        try:
            data = json.loads(row["data"])
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            out[row["ref"]] = dict(data, event_id=row["id"], at=row["ts"], by=row["actor"])
    return out
