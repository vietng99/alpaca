"""Post-check (design/watchdog/design.md section 6): verify the record, compare refs, post the result.

Reads git through `alpaca.gitview.git` and the record through `db.connect_readonly`. The only writes are
the report files, the `watchdog-postcheck` event and the result message, all through alpaca's own writer.
"""
from __future__ import annotations

import os
import time

from alpaca import gitview
from alpaca.watchdog import runs


def _heads(cwd):
    out = {}
    for line in gitview.git(cwd, "for-each-ref", "refs/heads", "--format=%(refname:short) %(objectname)", check=False).splitlines():
        name, _, sha = line.rpartition(" ")
        if name:
            out[name] = sha
    return out


def _real(p):
    return os.path.realpath(p or "")


def run(root, run_id, to=None, session="cli"):
    from alpaca import db
    root = os.path.realpath(root)
    pre = runs.load(root, run_id, "preflight.json")
    cwd = pre.get("cwd") or root
    mismatches = []

    conn = db.connect_readonly(root)
    try:
        ok, reason = db.verify_chain(conn)
        events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()
    if not ok:
        mismatches.append("alpaca verify failed: %s" % reason)

    now = _heads(cwd)
    before = pre.get("refs", {})
    intent = pre.get("intent", {})
    deleted = set(intent.get("deleted_branches", []))
    moved = intent.get("moved", {})
    for name, sha in sorted(before.items()):
        cur = now.get(name)
        if name in deleted:
            if cur is not None:
                mismatches.append("branch %s should be gone but is at %s" % (name, cur[:12]))
        elif name in moved:
            want = moved[name]
            if cur is None:
                mismatches.append("branch %s is gone, it should have moved" % name)
            elif want and cur != want:
                mismatches.append("branch %s is at %s, expected %s" % (name, cur[:12], want[:12]))
            elif want is None and cur == sha:
                mismatches.append("branch %s did not move" % name)
        elif cur is None:
            mismatches.append("branch %s disappeared (the command did not name it)" % name)
        elif cur != sha:
            mismatches.append("branch %s moved from %s to %s (the command did not name it)" % (name, sha[:12], cur[:12]))
    for name in sorted(deleted - set(before)):
        if name in now:
            mismatches.append("branch %s was not in the preflight but exists" % name)

    listed = {_real(w["path"]) for w in gitview.worktrees(cwd)}
    removed = {_real(p) for p in intent.get("removed_worktrees", [])}
    for path in sorted(removed):
        if path in listed:
            mismatches.append("worktree %s is still listed" % path)
        elif os.path.exists(path):
            mismatches.append("worktree folder %s is still on disk" % path)
    for w in pre.get("worktrees", []):
        p = _real(w["path"])
        if p not in removed and p not in listed:
            mismatches.append("worktree %s disappeared (the command did not name it)" % w["path"])

    undo_note = "no undo point was made"
    try:
        undo = runs.load(root, run_id, "undo.json")
    except runs.RunError:
        undo = None
    if undo:
        lost = [r["ref"] for r in undo["refs"]
                if not gitview.git(cwd, "rev-parse", "--verify", "--quiet", r["ref"], check=False).strip()]
        for ref in lost:
            mismatches.append("undo ref %s no longer resolves" % ref)
        undo_note = "%d undo ref(s)%s" % (len(undo["refs"]), ", %d lost" % len(lost) if lost else " all resolve")

    result = "ok" if not mismatches else "drift"
    to = to or pre.get("for")
    doc = {"id": run_id, "checked": time.time(), "result": result, "mismatches": mismatches,
           "verify": {"ok": ok, "events": events, "reason": reason}, "undo": undo_note, "to": to}
    runs.save(root, run_id, "postcheck.json", doc)
    lines = ["# Watchdog postcheck %s" % run_id, "", "Result: **%s**" % result.upper(), "",
             "- alpaca verify: %s (%d events)" % ("ok" if ok else "FAILED: %s" % reason, events),
             "- undo point: %s" % undo_note, "- command: `%s`" % " ".join(pre.get("command", [])), "",
             "## Mismatches", ""] + (["- " + m for m in mismatches] or ["- none"]) + [""]
    report = runs.write_text(root, run_id, "postcheck.md", "\n".join(lines))
    doc["report"] = report
    body = "watchdog postcheck %s: %s. %s Report: %s" % (
        run_id, result, ("Mismatches: " + "; ".join(mismatches[:5]) + ".") if mismatches else "Refs and worktrees match the preflight.",
        os.path.relpath(report, root))
    doc["message"] = "skipped (no addressee)"
    if to:
        from alpaca import messages
        conn = db.connect(root)
        try:
            row = messages.post(conn, runs.ACTOR, to, "result", body, session=session)
            doc["message"] = "posted to %s (id %s)" % (to, row.get("id") if isinstance(row, dict) else row)
        except Exception as exc:
            doc["message"] = "not posted: %s" % exc
        finally:
            conn.close()
    runs.save(root, run_id, "postcheck.json", {k: v for k, v in doc.items() if k != "report"})
    runs.record(root, "watchdog-postcheck", {"id": run_id, "result": result, "mismatches": mismatches,
                                              "report": os.path.relpath(report, root)}, session)
    return {"id": run_id, "result": result, "mismatches": mismatches, "report": report, "message": doc["message"]}
