"""The watchdog watcher: one line per new git or record event, nothing when idle (design section 3).

No LLM. Each tick reads git through `gitview.git` and the record through `db.connect_readonly`,
compares with the cursor in .alpaca/watchdog/state/cursor.json and returns the new events. An event
is {kind, key, summary, data}; `format_line` prints it as `WATCHDOG <kind> <key> :: <summary>` or JSON.

The cursor holds the last message id seen, the worktree paths listed last tick and, per kind, the
keys that are firing now. A key fires once; when it clears and comes back it fires again. A missing
cursor means "baseline now": the tick records the current state, prints one `baseline` line and no
`request`, `shared-dirty`, `worktree-*` line.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import time

from alpaca import db, gitview
from alpaca.watchdog import runs

CURSOR = "cursor.json"
RECHECK_S = 3600           # how often a quiet candidate orphan is looked up in the transcripts again
TOUCHED_S = 24 * 3600      # a tool call in a worktree within this long keeps it from being an orphan
MAX_ERRORS = 100
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def _event(kind, key, summary, data=None):
    return {"kind": kind, "key": str(key), "summary": " ".join(str(summary).split()), "data": data or {}}


# ---- cursor --------------------------------------------------------------------------------

def _cursor_path(root):
    return os.path.join(root, runs.STATE, CURSOR)


def load_state(root):
    """The saved cursor, or None when there is none (or it is unreadable): the next tick baselines."""
    try:
        with open(_cursor_path(root), encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def save_state(root, state):
    path = _cursor_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _fresh():
    return {"msg": 0, "worktrees": [], "dirty_hash": None, "fired": {}, "verify": {}, "recheck": {}, "errors": []}


def _fire(st, kind, keys):
    """Keys of one kind that are firing now; returns the ones that were not firing last tick."""
    old = set(st["fired"].get(kind, []))
    st["fired"][kind] = sorted(keys)
    return [k for k in sorted(keys) if k not in old]


# ---- transcripts (hooks the tests replace) --------------------------------------------------

def _scan(root):
    """(session rows, tool calls) from the record and the transcripts the board reads.
    A call is (sid, ts, tool name, target path)."""
    from alpaca.analytics import build_index, burn, children, ratecards
    conn = db.connect_readonly(root)
    try:
        sources = build_index._transcripts(root, conn)
        families = children.discover(root, conn)
        rows = conn.execute("SELECT sid, cwd, last_beat, ended FROM sessions").fetchall()
        cards, rev = ratecards.load(conn), ratecards.revision(root, conn=conn)
    finally:
        conn.close()
    calls = []
    for sid, _cwd, _beat, _ended in rows:
        paths = [sources.get(sid)] + [e["path"] for e in (families.get(sid) or {}).values() if e.get("path")]
        for path in paths:
            if not path or not os.path.isfile(path):
                continue
            try:
                st = burn.read(path, cards, rev)
            except OSError:
                continue
            calls += [(sid, c["ts"], c["name"], c.get("path") or "") for c in st["calls"] if c["ts"]]
    return rows, calls


def _dirty_sessions(root, files):
    """Sorted session ids that edited main's uncommitted `files` (gitview.attribute over the transcripts)."""
    _rows, calls = _scan(root)
    edits = [(sid, ts, target) for sid, ts, name, target in calls if name in EDIT_TOOLS and target]
    groups = gitview.attribute(root, files, edits)
    return sorted({g["session"] for g in groups if g.get("session")})


def _sessions(root, now):
    """Session dicts for gitview.match_session from the record alone (no transcripts, no `touched`)."""
    from alpaca.analytics import burn
    conn = db.connect_readonly(root)
    try:
        rows = conn.execute("SELECT sid, cwd, last_beat, ended FROM sessions").fetchall()
    finally:
        conn.close()
    return [{"sid": sid, "cwd": cwd, "last_beat": burn._epoch(beat), "ended": ended, "touched": {}}
            for sid, cwd, beat, ended in rows]


def _touched(root, trees, now):
    """{sid: {worktree: epoch}} of tool calls made in the last 24 h inside any of `trees`."""
    _rows, calls = _scan(root)
    out = {}
    for sid, ts, _name, target in calls:
        tree = gitview.owner(target, trees) if target else None
        if tree and now - ts < TOUCHED_S and ts > out.setdefault(sid, {}).get(tree, 0):
            out[sid][tree] = ts
    return out


# ---- the checks ----------------------------------------------------------------------------

def _requests(root, st, conn):
    out = []
    # MAX(id) first, then the rows up to it: a message committed between the two reads is seen next tick
    top = conn.execute("SELECT MAX(id) FROM messages").fetchone()[0] or 0
    rows = conn.execute("SELECT id, sender, kind, body FROM messages WHERE to_=? AND id>? AND id<=? ORDER BY id",
                        ("watchdog", st["msg"], top)).fetchall()
    for mid, sender, kind, body in rows:
        out.append(_request_event(mid, sender, kind, body))
    st["msg"] = max(st["msg"], top)
    return out


def _request_event(mid, sender, kind, body):
    return _event("request", mid, "%s from %s: %s" % (kind, sender, (body or "")[:200]),
                  {"id": mid, "sender": sender, "kind": kind})


RECOVER_LAST = 20


def recover_requests(root):
    """Requests to watchdog newer than the newest watchdog-preflight event (the last RECOVER_LAST when
    there is none): printed when the cursor was unreadable, so a crash does not drop a pending request."""
    conn = db.connect_readonly(root)
    try:
        row = conn.execute("SELECT MAX(ts) FROM events WHERE kind=?", ("watchdog-preflight",)).fetchone()
        since = row[0] if row else None
        if since:
            rows = conn.execute("SELECT id, sender, kind, body FROM messages WHERE to_=? AND ts>? ORDER BY id",
                                ("watchdog", since)).fetchall()
        else:
            rows = conn.execute("SELECT id, sender, kind, body FROM (SELECT id, sender, kind, body FROM messages "
                                "WHERE to_=? ORDER BY id DESC LIMIT ?) ORDER BY id",
                                ("watchdog", RECOVER_LAST)).fetchall()
    finally:
        conn.close()
    return [_request_event(*r) for r in rows]


def _verify(root, st, conn):
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    prev = st["verify"]
    if prev.get("events") == count:
        return []
    ok, reason = db.verify_chain(conn)
    st["verify"] = {"events": count, "ok": ok, "reason": reason}
    if not ok:
        return [_event("verify-fail", k, "record chain does not verify: %s" % k, {"reason": k})
                for k in _fire(st, "verify-fail", [reason])]
    _fire(st, "verify-fail", [])
    if prev and not prev.get("ok", True):
        return [_event("verify-ok", "ok", "record chain verifies again: %s" % reason, {"reason": reason})]
    return []


def _own(path):
    """True for the watchdog's own scratch and restore worktrees, which no worktree event covers."""
    p = os.path.realpath(path) + os.sep
    return any(os.sep.join(["", ".alpaca", "watchdog", d, ""]) in p for d in ("scratch", "restore"))


def _worktrees(root, st, now):
    out = []
    listed = [w for i, w in enumerate(gitview.worktrees(root)) if i == 0 or not _own(w["path"])]
    paths = [w["path"] for w in listed]
    for path in paths[1:]:
        if path not in st["worktrees"]:
            out.append(_event("worktree-new", path, "new worktree %s" % path, {"path": path}))
    st["worktrees"] = paths
    return out, listed


def _orphans(root, st, listed, now):
    linked = [w for w in listed[1:] if not w["bare"]]
    trees = [os.path.realpath(w["path"]) for w in listed]
    base = gitview.main_branch(root)
    sessions, touched, found = _sessions(root, now), None, {}
    for w in linked:
        if gitview.match_session(w["path"], sessions, now, trees):
            continue
        ahead = gitview.ahead_behind(root, base, w["head"])[0] if w["head"] else None
        if not (w["detached"] or ahead == 0):
            continue
        key = "%s@%s" % (w["path"], (w["head"] or "")[:8])
        if key not in st["fired"].get("worktree-orphan", []):
            if now - st["recheck"].get(key, 0) < RECHECK_S:
                continue
            touched = _touched(root, trees, now) if touched is None else touched
            seen = [dict(s, touched=touched.get(s["sid"], {})) for s in sessions]
            if gitview.match_session(w["path"], seen, now, trees):
                st["recheck"][key] = now      # quiet but touched lately: look again in an hour
                continue
        found[key] = {"path": w["path"], "head": w["head"], "branch": w["branch"], "detached": w["detached"],
                      "ahead": ahead}
    st["recheck"] = {k: v for k, v in st["recheck"].items() if now - v < RECHECK_S}
    return [_event("worktree-orphan", k, "no live session in %s (%s)" % (
        found[k]["path"], "detached HEAD" if found[k]["detached"] else "merged into %s" % base), found[k])
        for k in _fire(st, "worktree-orphan", found)]


def _mtime(tree, path):
    try:
        return os.stat(os.path.join(tree, path)).st_mtime_ns
    except OSError:
        return 0


def _shared_dirty(root, st, listed):
    main = listed[0]["path"] if listed else root
    files = gitview.status(main)
    digest = hashlib.sha1(json.dumps(sorted((code, path, _mtime(main, path)) for code, path in files)).encode("utf-8")).hexdigest()
    if digest == st["dirty_hash"]:
        return []
    sids = _dirty_sessions(main, files) if files else []
    st["dirty_hash"] = digest
    keys = [",".join(sids)] if len(sids) >= 2 else []
    return [_event("shared-dirty", k, "main has uncommitted files from %d sessions: %s" % (len(sids), k),
                   {"sessions": sids, "files": len(files)}) for k in _fire(st, "shared-dirty", keys)]


def _error(st, where, exc):
    text = "%s :: %s" % (where, str(exc) or type(exc).__name__)
    if text in st["errors"]:
        return []
    st["errors"] = (st["errors"] + [text])[-MAX_ERRORS:]
    return [_event("error", where, str(exc) or type(exc).__name__, {"where": where})]


def tick(root, state=None, now=None):
    """One scan. Returns (events, new state). `state` None means no cursor: baseline."""
    now = time.time() if now is None else now
    root = os.path.realpath(root)
    baseline = state is None
    st = dict(_fresh(), **copy.deepcopy(state or {}))
    events, listed = [], []

    def step(where, fn):
        try:
            return fn()
        except Exception as exc:  # a failed read must not stop the loop
            events.extend(_error(st, where, exc))
            return None

    def record():
        conn = db.connect_readonly(root)
        try:
            events.extend(_requests(root, st, conn))
            events.extend(_verify(root, st, conn))
        finally:
            conn.close()

    def trees():
        new, found = _worktrees(root, st, now)
        events.extend(new)
        return found

    step("record", record)
    listed = step("git worktree list", trees) or []
    if listed:
        step("worktree orphans", lambda: events.extend(_orphans(root, st, listed, now)))
        step("git status", lambda: events.extend(_shared_dirty(root, st, listed)))
    if baseline:
        kept = [e for e in events if e["kind"] in ("verify-fail", "error")]
        fired = st["fired"]
        line = _event("baseline", "start", "watching %d worktree(s), %d orphan, record %s" % (
            len(listed), len(fired.get("worktree-orphan", [])),
            "verifies" if st["verify"].get("ok") else "does not verify" if st["verify"] else "unread"),
            {"worktrees": len(listed), "orphans": len(fired.get("worktree-orphan", []))})
        events = [line] + kept
    return events, st


# ---- output and loop -----------------------------------------------------------------------

def format_line(event, as_json=False):
    if as_json:
        return json.dumps(event, sort_keys=True)
    return "WATCHDOG %s %s :: %s" % (event["kind"], event["key"], event["summary"])


def run(root, interval=30.0, once=False, as_json=False, out=sys.stdout, sleep=time.sleep):
    """Tick until interrupted (or once), print each new event on its own flushed line. Returns 0."""
    state = load_state(root)
    unreadable = state is None and os.path.exists(_cursor_path(root))
    try:
        while True:
            events, new = tick(root, state)
            if unreadable:
                unreadable = False
                try:
                    events = events + recover_requests(root)
                except Exception as exc:
                    events = events + _error(new, "cursor recovery", exc)
            for event in events:
                print(format_line(event, as_json), file=out, flush=True)
            if new != state:
                save_state(root, new)
            state = new
            if once:
                return 0
            sleep(interval)
    except (KeyboardInterrupt, BrokenPipeError):
        return 0
