"""One guarded operation's folder under .alpaca/watchdog/runs/<id>/ (design section 1)."""
from __future__ import annotations

import datetime
import json
import os
import re
import secrets

RUNS = os.path.join(".alpaca", "watchdog", "runs")
STATE = os.path.join(".alpaca", "watchdog", "state")
SCRATCH = os.path.join(".alpaca", "watchdog", "scratch")
UNDO = os.path.join(".alpaca", "watchdog", "undo")
ID = re.compile(r"^wd-\d{8}-\d{6}-[0-9a-f]{6}$")
ACTOR = "watchdog"


class RunError(Exception):
    pass


def new_id(now=None):
    now = now or datetime.datetime.now()
    return "wd-%s-%s" % (now.strftime("%Y%m%d-%H%M%S"), secrets.token_hex(3))


def run_dir(root, run_id, create=False):
    if not ID.match(run_id or ""):
        raise RunError("not a watchdog run id: %r" % run_id)
    path = os.path.join(root, RUNS, run_id)
    if create:
        os.makedirs(path, exist_ok=True)
    elif not os.path.isdir(path):
        raise RunError("no watchdog run %s under %s" % (run_id, os.path.join(root, RUNS)))
    return path


def save(root, run_id, name, data):
    path = os.path.join(run_dir(root, run_id, create=True), name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def load(root, run_id, name):
    path = os.path.join(run_dir(root, run_id), name)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise RunError("%s has no %s yet" % (run_id, name))


def write_text(root, run_id, name, text):
    path = os.path.join(run_dir(root, run_id, create=True), name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def record(root, kind, data, session="cli", ref=None):
    """Append one watchdog event to the record through alpaca's own writer."""
    from alpaca import db
    conn = db.connect(root)
    try:
        return db.append_event(conn, session=session, actor=ACTOR, kind=kind, ref=ref, data=data)
    finally:
        conn.close()
