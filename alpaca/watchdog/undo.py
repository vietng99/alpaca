"""Undo point and rollback (design/watchdog/design.md section 5).

`create` writes refs under refs/watchdog/<id>/, a wip snapshot commit per dirty target (built with a
temporary index, so the worktree's own index and files are untouched) and a record snapshot through
the sqlite3 backup API. `rollback` recreates what is gone and never moves a ref that exists, and
never restores the record.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import shutil
import tempfile
import time

from alpaca import gitview
from alpaca.watchdog import gitw, runs

KEEP_SNAPSHOTS = 5
IGNORED_NOTE = "ignored files are not captured"


class UndoError(runs.RunError):
    pass


def _git(cwd, *args, env=None, check=True):
    """A git WRITE through the watchdog's writer runner (gitw); errors become UndoError."""
    try:
        return gitw.run(cwd, *args, env=env, check=check)
    except gitw.GitWriteError as exc:
        raise UndoError(str(exc)) from exc


def _read(cwd, *args):
    """A read-only git call ('' when it fails)."""
    try:
        return gitview.git(cwd, *args, check=False)
    except gitview.GitError:
        return ""


def _safe(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "x"


def _real(p):
    return os.path.realpath(p or "")


def _short(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]


def _ref_name(cwd, prefix, name, used, path):
    """prefix + name, checked with `git check-ref-format`; a failing or colliding name gets a short hash suffix."""
    ref = prefix + _safe(name)
    if ref in used or gitw.run(cwd, "check-ref-format", ref, check=False).returncode != 0:
        ref = "%s%s-%s" % (prefix, _safe(name), _short(path or name))
    used.add(ref)
    return ref


def _is_main_tree(path):
    try:
        gitw.assert_toplevel(path, linked=True)
        return False
    except gitw.GitWriteError:
        return True


def _gitlinks(path, env, rev=None):
    """Paths with mode 160000 (a nested repository) in the temp index, or in the tree of `rev`."""
    if rev:
        out = _git(path, "ls-tree", "-r", "-z", rev, env=env).stdout
        rows = [r.split(" ", 1)[0] == "160000" and r.split("\t", 1)[1] for r in out.split("\0") if "\t" in r]
    else:
        out = _git(path, "ls-files", "-s", "-z", env=env).stdout
        rows = [r.split(" ", 1)[0] == "160000" and r.split("\t", 1)[1] for r in out.split("\0") if "\t" in r]
    return {r for r in rows if r}


def _wip_commit(path, run_id, tmp_base, gitlinks=None):
    """The sha of a commit holding the tree's uncommitted and untracked files (no ref is written), or None
    when it is clean. `gitlinks`, when given, collects the nested repositories `add -A` staged as bare
    gitlinks (mode 160000 the HEAD tree lacks): their files are not in the commit."""
    gitw.assert_toplevel(path, linked=False)       # the main tree's read-only snapshot is allowed
    if not _read(path, "status", "--porcelain").strip():
        return None
    head = _git(path, "rev-parse", "--verify", "HEAD").stdout.strip()
    real_index = _git(path, "rev-parse", "--git-path", "index").stdout.strip()
    real_index = real_index if os.path.isabs(real_index) else os.path.join(path, real_index)
    os.makedirs(tmp_base, exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="wd-index-", dir=tmp_base)
    index = os.path.join(tmpdir, "index")
    try:
        env = {"GIT_INDEX_FILE": index}
        if os.path.isfile(real_index):
            shutil.copyfile(real_index, index)
        else:
            _git(path, "read-tree", head, env=env)
        unmerged = _git(path, "ls-files", "-u", env=env).stdout.strip()
        if unmerged:
            raise UndoError("unmerged index (mid-merge): " + ", ".join(sorted({l.split("\t")[-1] for l in unmerged.splitlines()})[:8]))
        skipped = []
        for _ in range(8):       # git refuses a nested repository with no commit: leave it out and say so
            res = _git(path, "add", "-A", "--", ".", ":(exclude).alpaca/watchdog", *[":(exclude)" + x for x in skipped],
                       env=env, check=False)
            bad = re.findall(r"'([^']+?)/?' does not have a commit checked out", res.stderr)
            if res.returncode == 0 or not bad or set(bad) <= set(skipped):
                break
            skipped += [x for x in bad if x not in skipped]
        if res.returncode != 0:
            raise UndoError("git add -A failed in %s: %s" % (path, res.stderr.strip()[:300]))
        if gitlinks is not None:
            gitlinks.extend(sorted((_gitlinks(path, env) - _gitlinks(path, None, rev=head)) | set(skipped)))
        tree = _git(path, "write-tree", env=env).stdout.strip()
        who = {"GIT_AUTHOR_NAME": "watchdog", "GIT_AUTHOR_EMAIL": "watchdog@localhost",
               "GIT_COMMITTER_NAME": "watchdog", "GIT_COMMITTER_EMAIL": "watchdog@localhost"}
        return _git(path, "commit-tree", tree, "-p", head, "-m", "watchdog %s wip %s" % (run_id, os.path.basename(path)),
                    env=who).stdout.strip()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def wip_snapshot(path, ref, run_id, tmp_base, gitlinks=None):
    """Commit the tree's uncommitted and untracked files to `ref`; (ref, sha) or None when clean.
    The temp index is a copy of the tree's own index, so a tracked file that matches .gitignore stays
    in the snapshot and the real index and files are never touched. Raises UndoError when the tree
    cannot be captured (an unmerged index). Nested repositories found are appended to `gitlinks`."""
    commit = _wip_commit(path, run_id, tmp_base, gitlinks)
    if not commit:
        return None
    _git(path, "update-ref", ref, commit)
    return ref, commit


def _fetch_sha(cwd, source, sha, ref):
    """Copy `sha` (and its history) from the repository at `source` into cwd's repository under `ref`.
    Nothing is written in `source`; the upload side is told to serve an unreferenced commit."""
    res = gitw.run(cwd, "-c", "uploadpack.allowAnySHA1InWant=true", "fetch", "-q", "--no-tags",
                   "--no-write-fetch-head", source, "%s:%s" % (sha, ref), check=False)
    if res.returncode != 0:
        raise UndoError("could not copy %s from %s: %s" % (sha[:12], source, res.stderr.strip()[:200]))


def capture_nested(cwd, path, rel, run_id, tmp_base, used, refs):
    """Capture the nested repository `rel` of the tree at `path`: its HEAD and a wip snapshot of its own
    uncommitted files are copied into the outer repository under refs/watchdog/<id>/nested/. Returns the
    record the rollback restores from. Raises UndoError when it cannot be captured."""
    npath = os.path.join(path, rel)
    if not os.path.exists(os.path.join(npath, ".git")):
        raise UndoError("nested path %s has no repository to capture" % rel)
    head = _read(npath, "rev-parse", "--verify", "--quiet", "HEAD").strip()
    if not head:
        raise UndoError("nested repository %s has no commit yet; its files are not captured" % rel)
    branch = _read(npath, "symbolic-ref", "-q", "--short", "HEAD").strip() or None
    inner = []
    wip = _wip_commit(npath, run_id, tmp_base, inner)
    if inner:
        raise UndoError("nested repository %s holds further nested repositories (%s); not captured"
                        % (rel, ", ".join(inner[:4])))
    prefix = "refs/watchdog/%s/nested/" % run_id
    head_ref = _ref_name(cwd, prefix, rel + "-head", used, npath)
    _fetch_sha(cwd, npath, head, head_ref)
    refs.append({"kind": "nested-head", "ref": head_ref, "sha": head, "path": npath})
    rec = {"rel": rel, "branch": branch, "head": head, "head_ref": head_ref, "wip": None, "wip_ref": None}
    if wip:
        wip_ref = _ref_name(cwd, prefix, rel + "-wip", used, npath)
        _fetch_sha(cwd, npath, wip, wip_ref)
        refs.append({"kind": "nested-wip", "ref": wip_ref, "sha": wip, "path": npath})
        rec.update(wip=wip, wip_ref=wip_ref)
    return rec


def _snapshot_record(root, run_id):
    from alpaca import db
    dest_dir = os.path.join(root, runs.UNDO, run_id)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "alpaca.db")
    if os.path.exists(dest):
        os.unlink(dest)
    src = db.connect_readonly(root)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out, pages=-1)
            check = out.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            out.close()
    finally:
        src.close()
    if check != "ok":
        raise UndoError("record snapshot failed its integrity check: %s" % check)
    h = hashlib.sha256()
    with open(dest, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    _prune(root)
    return {"path": dest, "sha256": h.hexdigest(), "bytes": os.path.getsize(dest), "integrity": check}


def _prune(root):
    base = os.path.join(root, runs.UNDO)
    found = []
    for d in os.listdir(base) if os.path.isdir(base) else []:
        f = os.path.join(base, d, "alpaca.db")
        if os.path.isfile(f):
            found.append((os.path.getmtime(f), f))
    for _, f in sorted(found, reverse=True)[KEEP_SNAPSHOTS:]:
        os.unlink(f)




def create(root, run_id, session="cli"):
    root = os.path.realpath(root)
    pre = runs.load(root, run_id, "preflight.json")
    cwd = pre.get("cwd") or root
    refs, trees, used = [], [], set()
    tmp_base = os.path.join(root, runs.UNDO, run_id)

    def put(kind, ref, sha, **extra):
        _git(cwd, "update-ref", ref, sha)
        refs.append(dict(kind=kind, ref=ref, sha=sha, **extra))

    for t in pre.get("targets", []):
        if t.get("branch") and t.get("head"):
            put("heads", "refs/watchdog/%s/heads/%s" % (run_id, t["branch"]), t["head"], branch=t["branch"])
        elif t.get("detached") and t.get("head"):
            ref = _ref_name(cwd, "refs/watchdog/%s/detached/" % run_id, t["name"], used, t.get("path"))
            put("detached", ref, t["head"], name=t["name"])
        if t.get("path"):
            trees.append({"path": t["path"], "branch": t.get("branch"), "head": t.get("head"),
                          "detached": bool(t.get("detached")), "name": t["name"], "wip": None, "not_captured": None})
            if os.path.isdir(t["path"]):
                ref = _ref_name(cwd, "refs/watchdog/%s/wip/" % run_id, t["name"], used, t["path"])
                links = []
                try:
                    snap = wip_snapshot(t["path"], ref, run_id, tmp_base, links)
                except UndoError as exc:
                    trees[-1]["not_captured"] = str(exc)
                    continue
                if snap:
                    refs.append({"kind": "wip", "ref": snap[0], "sha": snap[1], "path": t["path"]})
                    trees[-1]["wip"] = snap[0]
                trees[-1]["nested"], failed = [], []
                for rel in links:         # nested repositories the snapshot holds only as bare gitlinks
                    try:
                        trees[-1]["nested"].append(capture_nested(cwd, t["path"], rel, run_id, tmp_base, used, refs))
                    except UndoError as exc:
                        failed.append(str(exc))
                if failed:
                    trees[-1]["not_captured"] = "; ".join(failed)
    snapshot = _snapshot_record(root, run_id)
    command = "alpaca watchdog rollback %s" % run_id
    not_captured = [{"path": t["path"], "error": t["not_captured"]} for t in trees if t["not_captured"]]
    doc = {"id": run_id, "created": time.time(), "cwd": cwd, "refs": refs, "worktrees": trees,
           "snapshot": snapshot, "rollback": command, "note": IGNORED_NOTE, "not_captured": not_captured}
    runs.save(root, run_id, "undo.json", doc)
    runs.record(root, "watchdog-undo", {"id": run_id, "refs": [r["ref"] for r in refs],
                                        "snapshot": snapshot["path"], "sha256": snapshot["sha256"],
                                        "not_captured": not_captured}, session)
    return {"id": run_id, "refs": refs, "snapshot": snapshot, "rollback": command, "not_captured": not_captured}


def _lay_back(path, wip_ref):
    """Put the wip snapshot into a linked worktree as uncommitted changes (path check first)."""
    gitw.assert_toplevel(path, linked=True)
    _git(path, "checkout", "--no-overlay", wip_ref, "--", ".")
    _git(path, "reset", "-q")


def _restore_nested(cwd, path, nested, run_id):
    """Recreate each captured nested repository inside the restored tree at `path`: its HEAD (on its
    branch when it had one) and its uncommitted files. Its remotes and config are not kept. Returns a list
    of failure texts."""
    failed = []
    for n in nested or []:
        dest = os.path.join(path, n["rel"])
        if os.path.exists(dest) and (not os.path.isdir(dest) or os.listdir(dest)):
            failed.append("%s exists and is not empty; left alone" % dest)
            continue
        os.makedirs(dest, exist_ok=True)
        try:
            _git(dest, "init", "-q")
            _git(dest, "fetch", "-q", "--no-tags", "--no-write-fetch-head", cwd, "%s:refs/watchdog-restore/head" % n["head_ref"])
            if n.get("branch"):
                _git(dest, "update-ref", "refs/heads/" + n["branch"], n["head"])
                _git(dest, "symbolic-ref", "HEAD", "refs/heads/" + n["branch"])
            else:
                _git(dest, "update-ref", "--no-deref", "HEAD", n["head"])
            _git(dest, "reset", "-q", "--hard", "HEAD")
            if n.get("wip_ref"):
                _git(dest, "fetch", "-q", "--no-tags", "--no-write-fetch-head", cwd, "%s:refs/watchdog-restore/wip" % n["wip_ref"])
                _git(dest, "checkout", "--no-overlay", n["wip"], "--", ".")
                _git(dest, "reset", "-q")
            for ref in ("refs/watchdog-restore/head", "refs/watchdog-restore/wip"):
                _git(dest, "update-ref", "-d", ref, check=False)
        except UndoError as exc:
            failed.append("%s: %s" % (dest, exc))
    return failed


def rollback(root, run_id, dry_run=False, session="cli"):
    root = os.path.realpath(root)
    undo = runs.load(root, run_id, "undo.json")
    try:
        kind = runs.load(root, run_id, "preflight.json").get("kind")
    except runs.RunError:
        kind = None
    cwd = undo.get("cwd") or root
    steps, ok = [], True

    def step(action, detail):
        steps.append({"action": action, "detail": detail})

    def exists(ref):
        return _read(cwd, "rev-parse", "--verify", "--quiet", ref).strip()

    def listing():
        return gitview.worktrees(cwd)

    wip_by_path = {r["path"]: r for r in undo["refs"] if r["kind"] == "wip"}
    heads = [r for r in undo["refs"] if r["kind"] == "heads"]
    state = {}                     # branch -> "ok" (usable for a worktree) or "moved"

    if kind in ("worktree-remove", "branch-delete"):
        for r in heads:
            name, sha = r["branch"], r["sha"]
            now = exists("refs/heads/" + name)
            if not now:
                step("recreate-branch", "%s at %s" % (name, sha[:12]))
                if not dry_run:
                    _git(cwd, "branch", name, sha)
                state[name] = "ok"
            elif now == sha:
                step("keep-branch", "%s already at %s" % (name, sha[:12]))
                state[name] = "ok"
            else:
                state[name] = "moved"
                if kind == "branch-delete":
                    ok = False
                step("refuse-branch", "%s exists at %s, the undo point has %s; left alone. To move it yourself: "
                     "git branch -f %s %s" % (name, now[:12], sha[:12], name, sha))

    if kind == "worktree-remove":
        for t in sorted(undo.get("worktrees", []), key=lambda w: len(w["path"])):   # a parent before its nested worktree
            path = t["path"]
            if not t.get("head"):
                continue
            listed = {_real(w["path"]): w for w in listing()}
            elsewhere = {w["branch"] for w in listed.values() if w.get("branch") and _real(w["path"]) != _real(path)}
            registered = _real(path) in listed
            if registered and os.path.isdir(path):
                step("keep-worktree", "%s still exists" % path)
                continue
            if not registered and os.path.exists(path) and os.listdir(path):
                ok = False
                step("refuse-worktree", "%s exists but is not a registered worktree; left alone" % path)
                continue
            on_branch = bool(t.get("branch")) and state.get(t["branch"]) == "ok" and t["branch"] not in elsewhere \
                and (t["branch"] in {r["branch"] for r in heads})
            flags = ["-f"] if registered else []
            if on_branch:
                spec = "git worktree add %s%s %s" % ("-f " if registered else "", path, t["branch"])
                args = ["worktree", "add", *flags, path, t["branch"]]
            else:
                why = "" if not t.get("branch") else " (its branch moved or is checked out elsewhere)"
                spec = "git worktree add %s--detach %s %s%s" % ("-f " if registered else "", path, t["head"][:12], why)
                args = ["worktree", "add", *flags, "--detach", path, t["head"]]
            recreate = len(steps)
            step("recreate-worktree", spec)
            wip = t.get("wip") and wip_by_path.get(path)
            if wip:
                step("restore-wip", "lay %s back as uncommitted files in %s" % (wip["ref"], path))
            for n in t.get("nested") or []:
                step("restore-nested", "recreate the nested repository %s in %s at %s" % (n["rel"], path, n["head"][:12]))
            if dry_run:
                continue
            res = _git(cwd, *args, check=False)
            if res.returncode != 0:
                ok = False
                steps[recreate]["detail"] += " FAILED: " + res.stderr.strip()[:200]
                continue
            if wip:
                _lay_back(path, wip["ref"])
            for failure in _restore_nested(cwd, path, t.get("nested"), run_id):
                ok = False
                step("refuse-nested", failure)
    elif kind in ("reset-hard", "merge", "clean", "stash"):
        for t in undo.get("worktrees", []):
            path, name = t["path"], t["name"]
            wip = wip_by_path.get(path)
            moved = False
            if t.get("branch"):
                now = exists("refs/heads/" + t["branch"])
                moved = bool(now) and now != t.get("head")
            if kind in ("clean", "stash"):
                needed = bool(wip)
            else:
                needed = moved or bool(wip) or (os.path.isdir(path) and _read(path, "rev-parse", "HEAD").strip() != t.get("head"))
            if not needed or not t.get("head"):
                step("keep-worktree", "%s: nothing to restore" % path)
                continue
            dest = os.path.join(root, ".alpaca", "watchdog", "restore", run_id, _safe(name))
            if os.path.exists(dest):
                step("keep-restore", "%s already exists; left alone" % dest)
                continue
            step("restore-worktree", "git worktree add --detach %s %s%s" % (dest, t["head"][:12],
                 ", then lay %s back" % wip["ref"] if wip else ""))
            for n in t.get("nested") or []:
                step("restore-nested", "recreate the nested repository %s in %s at %s" % (n["rel"], dest, n["head"][:12]))
            if not dry_run:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                res = _git(cwd, "worktree", "add", "--detach", dest, t["head"], check=False)
                if res.returncode != 0:
                    ok = False
                    steps[-1]["detail"] += " FAILED: " + res.stderr.strip()[:200]
                    continue
                if wip:
                    _lay_back(dest, wip["ref"])
                for failure in _restore_nested(cwd, dest, t.get("nested"), run_id):
                    ok = False
                    step("refuse-nested", failure)
            if kind in ("reset-hard", "merge") and t.get("branch"):
                step("owner-command", "to move %s back: git -C %s reset --hard refs/watchdog/%s/heads/%s"
                     % (t["branch"], dest, run_id, t["branch"]))
            if wip:
                step("owner-command", "to lay the wip back in place: git -C %s checkout --no-overlay %s -- . && git -C %s reset -q"
                     % (path, wip["ref"], path))
    step("manual-record", "the record is never restored automatically. Only after every session is stopped and the owner "
         "decides: cp %s %s (then run alpaca verify). Overwriting the live record under running sessions is the "
         "2026-09-30 incident." % (undo["snapshot"]["path"], os.path.join(root, ".alpaca", "alpaca.db")))
    if not dry_run:
        runs.record(root, "watchdog-rollback", {"id": run_id, "ok": ok, "steps": steps}, session)
    return {"id": run_id, "ok": ok, "dry_run": dry_run, "steps": steps}
