"""Version control tab (t-158): a read-only view of the project's git repository.

Every git call runs with GIT_OPTIONAL_LOCKS=0 and `git --no-optional-locks`, never fetches and never
writes the index, a ref, a reflog or a working tree: only `status --porcelain=v2`, `worktree list
--porcelain`, `for-each-ref`, `rev-list --count`, `log` (including `log -g` over reflogs),
`stash list`, `remote -v`, `rev-parse` and `symbolic-ref` with one argument (it reads HEAD). Each call has a timeout and its output is bounded.

Sections: the worktrees and who works in them; a commit graph of main and the local branches over a
window; main's uncommitted files grouped by the session whose Edit or Write touched them since the
file's last commit; a timeline of git activity from the reflogs, with the stash list; remotes,
release branches and tags against their upstream as of the last fetch; and git guard denials found
in the session transcripts.
"""
from __future__ import annotations

import os
import re
import subprocess
import time

TIMEOUT = 10
MAX_COMMITS = 400
MAX_FILES = 300
MAX_REFLOG = 60
LIVE_S = 2 * 3600          # a session that beat within this long counts as live for the worktree match
SEP = "\x1f"
TASK = re.compile(r"\bt-\d{3,}\b")
OP = re.compile(r"\bop-\d{3,}\b")


class GitError(Exception):
    pass


def git(cwd, *args, check=True):
    """Run one read-only git command and return its stdout as text."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(name, None)
    try:
        done = subprocess.run(["git", "--no-pager", "--no-optional-locks", "-C", cwd, *args], capture_output=True, text=True,
                              timeout=TIMEOUT, env=env, errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(str(exc)) from exc
    if check and done.returncode != 0:
        raise GitError(done.stderr.strip()[:300] or "git %s failed" % args[0])
    return done.stdout


def _rows(text):
    return [line.split(SEP) for line in text.splitlines() if line.strip()]


def toplevel(root):
    return git(root, "rev-parse", "--show-toplevel").strip()


def common_dir(root):
    path = git(root, "rev-parse", "--git-common-dir").strip()
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(root, path))


def main_branch(root):
    for name in ("main", "master", "trunk"):
        if git(root, "rev-parse", "--verify", "--quiet", "refs/heads/" + name, check=False).strip():
            return name
    # symbolic-ref also names the branch of a repository with no commit yet (after `git init`)
    return (git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip()
            or git(root, "rev-parse", "--abbrev-ref", "HEAD").strip())


def ahead_behind(root, base, ref):
    """(ahead, behind) of ref against base."""
    out = git(root, "rev-list", "--left-right", "--count", "%s...%s" % (base, ref), check=False).split()
    return (int(out[1]), int(out[0])) if len(out) == 2 else (None, None)


# ---- worktrees -----------------------------------------------------------------------------

def worktrees(root):
    """`git worktree list --porcelain` as dicts."""
    out, cur = [], None
    for line in git(root, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if cur:
                out.append(cur)
            cur = None
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            cur = {"path": value, "head": None, "branch": None, "detached": False, "locked": None,
                   "prunable": None, "bare": False}
        elif cur is None:
            continue
        elif key == "HEAD":
            cur["head"] = None if set(value) <= {"0"} else value     # all zeros: no commit yet
        elif key == "branch":
            cur["branch"] = value.replace("refs/heads/", "", 1)
        elif key == "detached":
            cur["detached"] = True
        elif key == "locked":
            cur["locked"] = value or "locked"
        elif key == "prunable":
            cur["prunable"] = value or "prunable"
        elif key == "bare":
            cur["bare"] = True
    return out


def status(path):
    """[(code, path)] from `git --no-optional-locks status --porcelain=v2` (never writes the index)."""
    out = []
    for line in git(path, "status", "--porcelain=v2", "--untracked-files=normal", check=False).splitlines():
        if not line or line.startswith("#"):
            continue
        kind = line[0]
        if kind == "1":
            parts = line.split(" ", 8)
            out.append((parts[1], parts[8] if len(parts) > 8 else ""))
        elif kind == "2":
            parts = line.split(" ", 9)
            out.append((parts[1], (parts[9] if len(parts) > 9 else "").split("\t")[0]))
        elif kind == "u":
            parts = line.split(" ", 10)
            out.append(("UU", parts[10] if len(parts) > 10 else ""))
        elif kind == "?":
            out.append(("??", line[2:]))
    return out


def _oldest_change(path, files):
    ages = []
    for _, rel in files[:MAX_FILES]:
        try:
            ages.append(os.stat(os.path.join(path, rel)).st_mtime)
        except OSError:
            continue
    return min(ages) if ages else None


def _commit_time(root, sha):
    out = git(root, "log", "-1", "--format=%ct", sha, check=False).strip()
    return int(out) if out.isdigit() else None


def worktree_view(root, sessions=(), now=None):
    """Every worktree with its branch, HEAD age, ahead and behind main, uncommitted files and session."""
    now = now or time.time()
    base = main_branch(root)
    out = []
    listed = worktrees(root)
    trees = [os.path.realpath(w["path"]) for w in listed]
    for w in listed:
        files = status(w["path"]) if os.path.isdir(w["path"]) else []
        ahead, behind = ahead_behind(root, base, w["head"]) if w["head"] else (None, None)
        w.update(head_short=(w["head"] or "")[:8], head_time=_commit_time(root, w["head"]) if w["head"] else None,
                 ahead=ahead, behind=behind, uncommitted=len(files), oldest_change=_oldest_change(w["path"], files),
                 is_main=w["branch"] == base, session=match_session(w["path"], sessions, now, trees))
        out.append(w)
    return out


def owner(path, trees):
    """The deepest worktree containing a path (worktrees nest inside the main checkout), or None."""
    real = os.path.realpath(path or "")
    hits = [t for t in trees if real == t or real.startswith(t + os.sep)]
    return max(hits, key=len) if hits else None


def match_session(path, sessions, now, trees=None):
    """The session working in a worktree: a live session whose working directory lies in it (and not in
    a worktree nested deeper), else the session whose latest tool call touched a file in it.
    `sessions` are dicts with sid, cwd, last_beat (epoch), ended and touched ({worktree: epoch})."""
    real = os.path.realpath(path)
    trees = trees or [real]
    live = [s for s in sessions if not s.get("ended") and s.get("last_beat") and now - s["last_beat"] < LIVE_S
            and owner(s.get("cwd"), trees) == real]
    if live:
        best = max(live, key=lambda s: s["last_beat"])
        return {"sid": best["sid"], "basis": "live session working directory"}
    touched = [(s["touched"].get(real), s["sid"]) for s in sessions if s.get("touched", {}).get(real)]
    if touched:
        ts, sid = max(touched)
        return {"sid": sid, "basis": "last tool call in this worktree", "at": ts}
    return None


# ---- commit graph --------------------------------------------------------------------------

def branches(root):
    """Local branches with tip, upstream and whether main already contains them."""
    base = main_branch(root)
    out = []
    for name, sha, upstream, track, when in _rows(git(root, "for-each-ref", "refs/heads",
            "--format=%(refname:short)%1f%(objectname)%1f%(upstream:short)%1f%(upstream:track)%1f%(committerdate:unix)")):
        ahead, behind = ahead_behind(root, base, sha)
        out.append({"name": name, "head": sha, "upstream": upstream or None, "track": track or "",
                    "time": int(when) if when.isdigit() else None, "main": name == base,
                    "merged": name != base and ahead == 0, "ahead": ahead, "behind": behind})
    return out


def lanes(commits, trunk=()):
    """Assign each commit (newest first, each with parents) a lane so first-parent chains stay put.

    Lane 0 belongs to `trunk`, the first-parent chain of the main branch: a main commit always sits
    in lane 0 and no other commit takes it."""
    trunk = set(trunk)
    active, out = ([None] if trunk else []), {}

    def free():
        start = 1 if trunk else 0
        for i in range(start, len(active)):
            if active[i] is None:
                return i
        active.append(None)
        return len(active) - 1

    for c in commits:
        sha = c["sha"]
        if sha in trunk:
            lane = 0
        elif sha in active[1 if trunk else 0:]:
            lane = active.index(sha, 1 if trunk else 0)
        else:
            lane = free()
        for i, other in enumerate(active):
            if other == sha and i != lane:
                active[i] = None
        out[sha] = lane
        parents = c["parents"]
        first = parents[0] if parents else None
        if not trunk:
            active[lane] = first
        elif lane == 0:
            active[0] = first if first in trunk else None
        else:
            active[lane] = None if first in trunk else first   # a branch rejoining the trunk ends here
        for p in parents[1:]:
            if p not in active and p not in trunk:
                active[free()] = p
        while len(active) > (1 if trunk else 0) and active[-1] is None:
            active.pop()
    return out


def graph(root, days=7, now=None):
    """Commits of every local branch over the window, newest first, with lanes, parents, refs, task ids."""
    now = now or time.time()
    refs = ["refs/heads/" + b["name"] for b in branches(root)] or ["HEAD"]
    out = git(root, "log", "--topo-order", "--format=%H%x1f%P%x1f%ct%x1f%an%x1f%s%x1f%D",
              "--since=%d" % int(now - days * 86400), "--max-count=%d" % (MAX_COMMITS + 1), *refs, check=False)
    commits = []
    for sha, parents, when, author, subject, refs in _rows(out):
        commits.append({"sha": sha, "short": sha[:8], "parents": parents.split() if parents else [],
                        "time": int(when), "author": author[:60], "subject": subject[:200],
                        "refs": [r.strip().replace("HEAD -> ", "") for r in refs.split(",") if r.strip() and not r.strip().startswith("tag:")],
                        "tags": [r.strip()[5:] for r in refs.split(",") if r.strip().startswith("tag:")],
                        "tasks": sorted(set(TASK.findall(subject))), "ops": sorted(set(OP.findall(subject))),
                        "merge": len(parents.split()) > 1})
    truncated = len(commits) > MAX_COMMITS
    commits = commits[:MAX_COMMITS]
    base = main_branch(root)
    trunk = git(root, "rev-list", "--first-parent", "--max-count=%d" % (MAX_COMMITS * 4), "refs/heads/" + base, check=False).split()
    lane = lanes(commits, trunk)
    for c in commits:
        c["lane"] = lane[c["sha"]]
    shown = {c["sha"] for c in commits}
    for c in commits:
        c["edges"] = [{"to": p, "lane": lane.get(p), "inside": p in shown} for p in c["parents"]]
    return {"days": days, "commits": commits, "lanes": (max(lane.values()) + 1) if lane else 0,
            "truncated": truncated}


# ---- main's uncommitted files and who touched them ---------------------------------------

def attribute(root, files, edits):
    """Group main's uncommitted files by the session whose Edit or Write touched each one after the
    file's last commit. `edits` are (sid, ts, absolute path); files nobody touched are unattributed."""
    top = toplevel(root)
    by_path = {}
    for sid, ts, path in edits:
        by_path.setdefault(os.path.normpath(path), []).append((ts, sid))
    groups = {}
    if len(files) > MAX_FILES:
        groups["__capped__"] = {"session": "__capped__", "files": [], "count": len(files) - MAX_FILES}
    for code, rel in files[:MAX_FILES]:
        absolute = os.path.normpath(os.path.join(top, rel))
        last = git(top, "log", "-1", "--format=%ct", "--", rel, check=False).strip()
        last = int(last) if last.isdigit() else 0
        touched = sorted((ts, sid) for ts, sid in by_path.get(absolute, []) if ts and ts >= last)
        sid = touched[-1][1] if touched else None
        g = groups.setdefault(sid, {"session": sid, "files": []})
        g["files"].append({"path": rel, "code": code, "last_commit": last or None,
                           "touched": touched[-1][0] if touched else None,
                           "sessions": sorted({s for _, s in touched})})
    capped = groups.pop("__capped__", None)
    out = sorted(groups.values(), key=lambda g: (g["session"] is None, -len(g["files"])))
    return out + ([{"session": None, "capped": capped["count"], "files": []}] if capped else [])


# ---- activity: reflogs and stash ---------------------------------------------------------

ACTIONS = (("commit (merge)", "merge"), ("merge", "merge"), ("commit (amend)", "amend"), ("commit (initial)", "commit"),
           ("commit", "commit"), ("reset", "reset"), ("checkout", "checkout"), ("rebase", "rebase"),
           ("cherry-pick", "cherry-pick"), ("pull", "pull"), ("branch", "branch"), ("revert", "revert"))


def _action(message):
    for prefix, kind in ACTIONS:
        if message.startswith(prefix):
            return kind
    return message.split(":")[0][:20] or "update"


def reflog(cwd, ref, label):
    out = []
    for sha, selector, message in _rows(git(cwd, "log", "-g", "--date=unix", "--max-count=%d" % MAX_REFLOG,
                                            "--format=%H%x1f%gd%x1f%gs", ref, check=False)):
        when = re.search(r"@\{(\d+)\}", selector)
        out.append({"ts": int(when.group(1)) if when else None, "sha": sha[:8], "where": label,
                    "action": _action(message), "message": message[:200]})
    return out


def activity(root, trees):
    """Reflog entries of HEAD, main and each worktree's HEAD, newest first, and the stash list."""
    base = main_branch(root)
    items = reflog(root, "refs/heads/" + base, base)
    for w in trees:
        if os.path.isdir(w["path"]):
            label = w["branch"] or ("detached at " + (w["head"] or "")[:8])
            items += [dict(e, where="%s (%s)" % (label, os.path.basename(w["path"]))) for e in reflog(w["path"], "HEAD", label)]
    seen, unique = set(), []
    for e in sorted(items, key=lambda e: -(e["ts"] or 0)):
        key = (e["ts"], e["sha"], e["message"])
        if key not in seen:
            seen.add(key)
            unique.append(e)
    stash = [{"ref": ref, "ts": int(when) if when.isdigit() else None, "message": message[:200]}
             for ref, when, message in _rows(git(root, "stash", "list", "--format=%gd%x1f%ct%x1f%gs", check=False))]
    return {"items": unique[:150], "stash": stash}


# ---- remotes, release branches, tags ----------------------------------------------------

def remotes(root):
    names = {}
    for line in git(root, "remote", "-v", check=False).splitlines():
        parts = line.split()
        if len(parts) >= 2:
            names.setdefault(parts[0], parts[1])
    fetch = os.path.join(common_dir(root), "FETCH_HEAD")
    tracked = []
    for name, sha, upstream, track in _rows(git(root, "for-each-ref", "refs/heads",
            "--format=%(refname:short)%1f%(objectname:short)%1f%(upstream:short)%1f%(upstream:track)")):
        if upstream:
            ahead = re.search(r"ahead (\d+)", track)
            behind = re.search(r"behind (\d+)", track)
            tracked.append({"branch": name, "upstream": upstream, "ahead": int(ahead.group(1)) if ahead else 0,
                            "behind": int(behind.group(1)) if behind else 0, "gone": "gone" in track})
    release = [{"ref": ref, "sha": sha, "time": int(when) if when.isdigit() else None}
               for ref, sha, when in _rows(git(root, "for-each-ref", "refs/heads/release", "refs/remotes",
                                               "--format=%(refname:short)%1f%(objectname:short)%1f%(committerdate:unix)", check=False))
               if "release" in ref]
    tags = [{"tag": tag, "sha": sha, "time": int(when) if when.isdigit() else None}
            for tag, sha, when in _rows(git(root, "for-each-ref", "refs/tags", "--sort=-creatordate", "--count=20",
                                            "--format=%(refname:short)%1f%(objectname:short)%1f%(creatordate:unix)", check=False))]
    return {"remotes": [{"name": n, "url": _redact(u)} for n, u in sorted(names.items())],
            "fetch_head": os.stat(fetch).st_mtime if os.path.isfile(fetch) else None,
            "tracking": tracked, "release": release, "tags": tags}


def _redact(url):
    """A remote URL without any credential in it."""
    return re.sub(r"//[^/@]+@", "//", url)


# ---- the snapshot ------------------------------------------------------------------------

def snapshot(root, days=7, now=None):
    """Everything the tab shows, from read-only git calls and the transcripts the board already reads."""
    from alpaca import db
    from alpaca.analytics import build_index, burn, children
    now = now or time.time()
    root = os.path.realpath(root)
    if not git(root, "rev-parse", "--git-dir", check=False).strip():
        # a project made with `alpaca new --no-git`: the tab says so instead of failing
        return {"generated": now, "repository": False, "root": root}
    conn = db.connect_readonly(root)
    try:
        sources = build_index._transcripts(root, conn)
        families = children.discover(root, conn)
        rows = conn.execute("SELECT sid, cwd, last_beat, ended FROM sessions").fetchall()
        from alpaca.analytics import ratecards
        cards, cards_rev = ratecards.load(conn), ratecards.revision(root, conn=conn)
    finally:
        conn.close()
    trees = worktrees(root)
    tree_paths = [os.path.realpath(w["path"]) for w in trees]
    sessions, edits, denials = [], [], []
    for sid, cwd, beat, ended in rows:
        touched = {}
        paths = [sources.get(sid)] + [e["path"] for e in (families.get(sid) or {}).values() if e.get("path")]
        for i, path in enumerate(paths):
            if not path or not os.path.isfile(path):
                continue
            try:
                st = burn.read(path, cards, cards_rev)
            except OSError:
                continue
            for c in st["calls"]:
                target = c.get("path") or ""
                if c["name"] in ("Edit", "Write", "MultiEdit", "NotebookEdit") and target and c["ts"]:
                    edits.append((sid, c["ts"], target))
                if target and c["ts"]:
                    tp = owner(target, tree_paths)
                    if tp and c["ts"] > touched.get(tp, 0):
                        touched[tp] = c["ts"]
            for e in st["errors"].values():
                if "Alpaca git guard" in e["text"] and e["ts"] and now - e["ts"] < 30 * 86400:
                    denials.append({"session": sid, "thread": None if i == 0 else os.path.basename(path)[:-6].replace("agent-", ""),
                                    "ts": e["ts"], "line": e["line"], "text": e["text"][:240]})
        sessions.append({"sid": sid, "cwd": cwd, "last_beat": burn._epoch(beat), "ended": ended, "touched": touched})
    view = worktree_view(root, sessions, now)
    main = next((w for w in view if w["is_main"]), view[0] if view else None)
    dirty = status(main["path"]) if main else []
    # The repository is shown from its main working tree: worktrees nest inside it.
    return {"generated": now, "root": trees[0]["path"] if trees else toplevel(root), "main": main_branch(root), "worktrees": view,
            "branches": branches(root), "graph": graph(root, days, now),
            "uncommitted": attribute(main["path"], dirty, edits) if main else [],
            "activity": activity(root, trees), "remotes": remotes(root),
            "denials": sorted(denials, key=lambda d: -d["ts"])[:30]}
