"""Disk hygiene: plan and apply the declared cleanup of scratch, /tmp leftovers and heavy run trees.

Agents write large scratch (tree copies, suite temp, run trees) and rarely delete it. On
2026-10-04 one session's scratchpad held 30G, three forgotten pytest basetemps held 13G of the
RAM-backed /tmp, and `.alpaca/` held 40G of run trees next to 57M of code. The owner ruled out
quotas and caps (a long session must never stall on one), so this module removes what is dead
instead. Every rule is DATA in project.yaml under `disk:`; with no `disk:` block nothing is ever
removed and only the doctor line reports use. See docs/temp-and-scratch.md.

Three sources of candidates:

  * scratch  - `<tmp>/claude-<uid>/<escaped project dir>/<session id>/` folders Claude Code makes
               per session (and the same under this project's linked worktrees). A folder goes
               when the record shows its session ended at least `ended_grace_hours` ago; a
               session the record shows open (no end) or does not know goes only after
               `open_session_days` with nothing written anywhere in the folder. No record, no
               scratch sweep.
  * tmp      - top-level entries of `tmp_root` that match `tmp_globs`, are owned by this user and
               have been quiet for `grace_hours`.
  * prune    - `prune` rules: a glob under the project root, optionally gated on the task named
               by one path part being done, on a minimum age, and on keeping the newest N.
  * worktree - with a `worktrees:` block, linked worktrees under `.alpaca/worktrees/` named
               `op<nnn>-...` whose op is closed in the record, whose HEAD is merged into the main
               branch, that have no changes and have been quiet `min_age_days`. They go by
               `git worktree remove` (never --force) and their branch by `git branch -d`.

Nothing goes that a live process holds (its cwd, its executable, a mapped or open file inside),
that holds a `.keep` file at its top, that a sealed proof's evidence pointer names, that a proof
report's text names (prune candidates), that a prune rule's `named_in` files name, or that a session the record shows live owns. Removals are recorded
as one `disk-gc` event.
"""
from __future__ import annotations

import fnmatch
import glob as _glob
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time

from alpaca import paths, project

KEEP_FILE = ".keep"
LOCK_NAME = "disk-sweep.lock"
STAMP_NAME = "disk-sweep.stamp"
GIB = 1024 ** 3
#: escaped forms of `<root>/.alpaca/worktrees/` and `<root>/.claude/worktrees/`
WORKTREE_SLUG_PREFIXES = ("--alpaca-worktrees-", "--claude-worktrees-")

#: the keys a `disk:` block may carry; anything else is reported by `policy_errors`.
POLICY_KEYS = {"grace_hours", "ended_grace_hours", "open_session_days", "sweep_every_hours",
               "scratch_base", "tmp_root", "tmp_globs", "prune", "worktrees", "warn_gib"}
PRUNE_KEYS = {"glob", "task_part", "min_age_days", "keep_newest", "named_in", "name_part",
              "dirs_only", "why"}


# ------------------------------------------------------------------------------------- policy
def load_policy(root) -> dict | None:
    """The `disk:` block of project.yaml, or None when the project declares none (then nothing is
    ever removed). Defaults fill only the timing knobs, never what may be removed."""
    block = (project.load(root) or {}).get("disk")
    if not isinstance(block, dict):
        return None
    return {
        "grace_hours": float(block.get("grace_hours", 48)),
        "ended_grace_hours": float(block.get("ended_grace_hours", 12)),
        "open_session_days": float(block.get("open_session_days", 14)),
        "sweep_every_hours": float(block.get("sweep_every_hours", 6)),
        "scratch_base": block.get("scratch_base"),
        "tmp_root": block.get("tmp_root"),
        "tmp_globs": [str(g) for g in (block.get("tmp_globs") or [])],
        "prune": [r for r in (block.get("prune") or []) if isinstance(r, dict) and r.get("glob")],
        "warn_gib": dict(block.get("warn_gib") or {}),
        "worktrees": dict(block["worktrees"]) if isinstance(block.get("worktrees"), dict) else None,
    }


def policy_errors(root) -> list:
    block = (project.load(root) or {}).get("disk")
    if block is None:
        return []
    if not isinstance(block, dict):
        return ["disk: is not a mapping"]
    errs = ["disk.%s is not a known key" % k for k in sorted(set(block) - POLICY_KEYS)]
    wt = block.get("worktrees")
    if wt is not None and not isinstance(wt, dict):
        errs.append("disk.worktrees is not a mapping")
    elif isinstance(wt, dict):
        errs += ["disk.worktrees.%s is not a known key" % k
                 for k in sorted(set(wt) - {"min_age_days", "main_branch"})]
    for i, rule in enumerate(block.get("prune") or []):
        if not isinstance(rule, dict) or not rule.get("glob"):
            errs.append("disk.prune[%d] has no glob" % i)
            continue
        errs += ["disk.prune[%d].%s is not a known key" % (i, k) for k in sorted(set(rule) - PRUNE_KEYS)]
        g = str(rule["glob"])
        if os.path.isabs(g) or ".." in g.split("/"):
            errs.append("disk.prune[%d].glob must stay under the project root" % i)
        if ("named_in" in rule) != ("name_part" in rule):
            errs.append("disk.prune[%d] needs named_in and name_part together" % i)
    return errs


# ------------------------------------------------------------------------------------ helpers
def tree_bytes(path) -> int:
    """Bytes held under path, symlinks counted as links and never followed."""
    total = 0
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    if not stat.S_ISDIR(st.st_mode):
        return st.st_blocks * 512 if st.st_blocks else st.st_size
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        s = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += s.st_blocks * 512 if s.st_blocks else s.st_size
                    if stat.S_ISDIR(s.st_mode):
                        stack.append(e.path)
        except OSError:
            continue
    return total


def quiet_seconds(path, now=None) -> float:
    """Seconds since anything at the top of path changed: the newest mtime of path itself and its
    direct children. One level only, so it stays cheap on a 30G tree."""
    now = time.time() if now is None else now
    try:
        newest = os.lstat(path).st_mtime
    except OSError:
        return 0.0
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
                except OSError:
                    pass
    except (NotADirectoryError, OSError):
        pass
    return max(0.0, now - newest)


def deep_quiet_seconds(path, now=None) -> float:
    """Seconds since anything anywhere under path changed (newest mtime of every entry, symlinks
    not followed). Used where the record cannot vouch for a folder, so a deep write keeps it."""
    now = time.time() if now is None else now
    try:
        newest = os.lstat(path).st_mtime
    except OSError:
        return 0.0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    newest = max(newest, st.st_mtime)
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(e.path)
        except OSError:
            continue
    return max(0.0, now - newest)


def held_paths() -> list:
    """Real paths this user's processes hold: every cwd, executable, mapped file and open file. A
    candidate that contains one of these is in use and stays. A process whose /proc entries this
    user cannot read (a bwrap sandbox, sshd) is skipped; the session rules and the quiet times
    are the guard there."""
    out, uid = [], os.getuid()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return out
    for pid in pids:
        base = "/proc/" + pid
        try:
            if os.stat(base).st_uid != uid:
                continue
        except OSError:
            continue
        for link in ("cwd", "root", "exe"):
            try:
                t = os.readlink(os.path.join(base, link))
                if link != "root" or t != "/":
                    out.append(t[:-10] if t.endswith(" (deleted)") else t)
            except OSError:
                pass
        try:
            with open(base + "/maps", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    i = line.find(" /")
                    if i != -1:
                        t = line[i + 1:].rstrip("\n")
                        out.append(t[:-10] if t.endswith(" (deleted)") else t)
        except OSError:
            pass
        try:
            for fd in os.listdir(base + "/fd"):
                try:
                    t = os.readlink("%s/fd/%s" % (base, fd))
                except OSError:
                    continue
                if t.startswith("/"):
                    out.append(t[:-10] if t.endswith(" (deleted)") else t)
        except OSError:
            pass
    return out


def _mtime(path) -> float:
    try:
        return os.lstat(path).st_mtime
    except OSError:
        return 0.0


def _inside(path, parent) -> bool:
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def cited_paths(root) -> set:
    """Real paths that sealed proofs name as evidence pointers. They are never removed."""
    out = set()
    for seal in _glob.glob(os.path.join(paths.runtime_dir(root), "proofs", "**", "*.seal.json"),
                           recursive=True):
        try:
            with open(seal, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        for item in data.get("evidence") or []:
            ptr = str(item.get("pointer") or "")
            if ptr.startswith("local:"):
                p = ptr[len("local:"):]
                out.add(os.path.realpath(p if os.path.isabs(p) else os.path.join(root, p)))
    return out


def report_text(root) -> str:
    """The text of every proof report (`.alpaca/proofs/<op>/<id>.md`). A prune candidate whose
    path a report names stays, even when the report never sealed it as evidence."""
    out = []
    for f in sorted(_glob.glob(os.path.join(paths.runtime_dir(root), "proofs", "*", "*.md"))):
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                out.append(fh.read())
        except OSError:
            pass
    return "\n".join(out)


def scratch_bases(root, policy) -> list:
    """The folders holding this project's per-session Claude Code scratch, newest naming first.
    Claude Code puts them at `<CLAUDE_CODE_TMPDIR or /tmp>/claude-<uid>/<escaped cwd>/<sid>/`."""
    if policy and policy.get("scratch_base"):
        tops = [os.path.expanduser(str(policy["scratch_base"]))]
    else:
        tops = []
        for t in (os.environ.get("CLAUDE_CODE_TMPDIR"), "/var/tmp", "/tmp"):
            if t:
                d = os.path.join(t, "claude-%d" % os.getuid())
                if d not in tops:
                    tops.append(d)
    slug = paths.escape_cwd(os.path.realpath(root))
    out = []
    for top in tops:
        try:
            names = os.listdir(top)
        except OSError:
            continue
        for n in sorted(names):
            # the project itself and its linked worktrees; a plain `<slug>-` prefix would also
            # take a sibling project such as `.../Alpaca-old`
            if n == slug or n.startswith(tuple(slug + p for p in WORKTREE_SLUG_PREFIXES)):
                d = os.path.join(top, n)
                if os.path.isdir(d) and not os.path.islink(d):
                    out.append(d)
    return out


def _sessions(root):
    """sid -> {live, ended, last} from the record, read-only, or None when the record cannot be
    read (then no scratch folder is judged at all)."""
    if not os.path.isfile(paths.db_path(root)):
        return None
    try:
        from alpaca import db, sessions_view
        conn = db.connect_readonly(root)
    except Exception:
        return None
    try:
        view = sessions_view.classify(conn)
    except Exception:
        return None
    finally:
        conn.close()
    out = {}
    for sid, info in view.items():
        last = None
        for key in ("last_beat", "ended", "started"):
            last = sessions_view.later_of(last, info.get(key)) if last else info.get(key)
        out[sid] = {"live": sessions_view.is_active(info), "ended": info.get("ended"), "last": last}
    return out


def _age_of_stamp(ts, now) -> float | None:
    from alpaca import sessions_view
    import datetime
    dt = sessions_view.parse_ts(ts) if ts else None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return now - dt.timestamp()


def _task_status(root):
    try:
        from alpaca import db
        conn = db.connect_readonly(root)
    except Exception:
        return {}
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT id, status FROM tasks")}
    except Exception:
        return {}
    finally:
        conn.close()


# ------------------------------------------------------------------------------------ planning
def _git(args, cwd, timeout=30, err=False):
    """(returncode, stdout) of one git command, stdout plus stderr when err; (1, "") when git
    cannot run."""
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e) if err else ""
    return p.returncode, (p.stdout + p.stderr) if err else p.stdout


#: ignored entries a worktree may hold and still be removed: caches and the shared-venv link
WORKTREE_JUNK = re.compile(r"^(?:.*/)?(?:__pycache__/|\.pytest_cache/|[^/]*\.pyc|\.venv)$")
IN_PROGRESS = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
               "BISECT_LOG")


def worktree_hold(path) -> str | None:
    """Why a worktree must stay even though its op is done, or None. git worktree remove deletes
    ignored files without asking (a worktree's own .alpaca/alpaca.db is ignored), so anything
    beyond caches keeps it; so do skip-worktree or assume-unchanged files and an operation that
    is half done (rebase, merge, cherry-pick, revert, bisect)."""
    rc, out = _git(["status", "--porcelain", "--ignored", "--untracked-files=all"], path)
    if rc != 0:
        return "git status failed"
    for line in out.splitlines():
        if line.startswith("!! "):
            if not WORKTREE_JUNK.match(line[3:].strip().strip('"')):
                return "it holds ignored files (%s)" % line[3:].strip()
        elif line.strip():
            return "it has uncommitted or untracked changes"
    rc, out = _git(["ls-files", "-v"], path)
    if rc != 0 or any(l[:1] == "S" or l[:1].islower() for l in out.splitlines()):
        return "it has skip-worktree or assume-unchanged files"
    rc, gitdir = _git(["rev-parse", "--absolute-git-dir"], path)
    gitdir = gitdir.strip()
    if rc != 0 or not gitdir:
        return "git rev-parse failed"
    for name in IN_PROGRESS:
        if os.path.exists(os.path.join(gitdir, name)):
            return "a %s is in progress" % name
    return None


def worktree_ops(path, branch) -> list:
    """Every op a worktree names: by its folder (`op020-...`) and by its branch (`op022/...`)."""
    found = []
    m = re.match(r"^op(\d+)-", os.path.basename(os.path.realpath(path)))
    if m:
        found.append("op-" + m.group(1))
    m = re.match(r"^refs/heads/op(\d+)/", branch or "")
    if m and "op-" + m.group(1) not in found:
        found.append("op-" + m.group(1))
    return found


def linked_worktrees(root) -> list:
    """[{path, head, branch}] for every linked worktree of the repository at root."""
    rc, out = _git(["worktree", "list", "--porcelain"], root)
    if rc != 0:
        return []
    items, cur = [], {}
    for line in out.splitlines() + [""]:
        if not line:
            if cur.get("path"):
                items.append(cur)
            cur = {}
        elif line.startswith("worktree "):
            cur = {"path": line[len("worktree "):], "head": None, "branch": None}
        elif line.startswith("HEAD "):
            cur["head"] = line[5:]
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch "):]
    return [w for w in items if os.path.realpath(w["path"]) != os.path.realpath(root)]


def _op_status(root):
    if not os.path.isfile(paths.db_path(root)):
        return None
    try:
        from alpaca import db
        conn = db.connect_readonly(root)
    except Exception:
        return None
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT id, status FROM ops")}
    except Exception:
        return None
    finally:
        conn.close()


def _candidate(kind, path, reason, **extra):
    c = {"kind": kind, "path": path, "reason": reason}
    c.update(extra)
    return c


_UNSET = object()


def plan(root, policy=None, *, current_sid=None, now=None, held=None, sessions=_UNSET,
         tasks=None, cited=None, reports=None, ops=_UNSET) -> dict:
    """What a sweep would remove and what it keeps, with a reason for each. Pure read.
    `current_sid` is the session running the sweep; its folder always stays. `sessions` None
    means the record could not be read: no scratch folder is judged."""
    root = os.path.realpath(root)
    policy = load_policy(root) if policy is None else policy
    if not policy:
        return {"policy": False, "remove": [], "keep": []}
    now = time.time() if now is None else now
    grace = policy["grace_hours"] * 3600
    held = [os.path.realpath(h) for h in (held_paths() if held is None else held)]
    sessions = _sessions(root) if sessions is _UNSET else sessions
    cited = cited_paths(root) if cited is None else cited
    remove, keep = [], []

    def blocked(path):
        real = os.path.realpath(path)
        if os.path.exists(os.path.join(path, KEEP_FILE)) or \
                os.path.exists(os.path.join(path, "scratchpad", KEEP_FILE)):
            return "has a .keep file"
        if any(_inside(h, real) for h in held):
            return "a running process holds it"
        if any(_inside(c, real) for c in cited):
            return "a sealed proof cites a path inside it"
        return None

    # scratch: one folder per Claude Code session
    ended_grace = policy["ended_grace_hours"] * 3600
    open_limit = policy["open_session_days"] * 86400
    for base in (scratch_bases(root, policy) if sessions is not None else []):
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for sid in names:
            d = os.path.join(base, sid)
            if not os.path.isdir(d) or os.path.islink(d):
                continue
            if sid == current_sid:
                keep.append(_candidate("scratch", d, "the session running this sweep"))
                continue
            info = sessions.get(sid)
            if info and info["live"]:
                keep.append(_candidate("scratch", d, "its session is live in the record"))
                continue
            if info and info["ended"]:
                age = _age_of_stamp(info["last"], now)
                quiet = deep_quiet_seconds(d, now)
                age = quiet if age is None else min(age, quiet)
                if age < ended_grace:
                    keep.append(_candidate("scratch", d, "session ended %.0f h ago, under %.0f h"
                                           % (age / 3600, policy["ended_grace_hours"])))
                    continue
                why = "its session ended and nothing changed for %.0f h" % (age / 3600)
            else:
                # open in the record (it may be idle, not dead) or unknown to it (a worktree's
                # own record): only a long, deep quiet lets it go
                age = deep_quiet_seconds(d, now)
                if info:
                    rec = _age_of_stamp(info["last"], now)
                    age = age if rec is None else min(age, rec)
                if age < open_limit:
                    keep.append(_candidate("scratch", d, "%s, quiet %.1f d, under %.0f d" % (
                        "session not ended in the record" if info else "session unknown to the record",
                        age / 86400, policy["open_session_days"])))
                    continue
                why = "%s and nothing written in it for %.0f d" % (
                    "its session never ended" if info else "its session is unknown to the record",
                    age / 86400)
            b = blocked(d)
            (keep if b else remove).append(_candidate("scratch", d, b or why))

    # sandbox temp roots: `/tmp/sbx<sid8>` of a session the record shows ended (alpaca/sandbox.py). SessionEnd
    # removes it; this is the backstop for a session that died. The folder names its session in a marker
    # file with the full session id, never by the 8-character name: a folder with no marker, one that is
    # not ours or closed to others, a link, and one whose session is live, not ended or unknown stay.
    from alpaca import sandbox
    sbx_base = os.path.realpath(sandbox.TEMP_BASE)
    for n in (sorted(os.listdir(sbx_base)) if sessions is not None and os.path.isdir(sbx_base) else []):
        d = os.path.join(sbx_base, n)
        if not n.startswith(sandbox.TEMP_PREFIX) or os.path.islink(d) or not os.path.isdir(d):
            continue
        sid = sandbox._marker_of(d)
        if not sid:
            keep.append(_candidate("sandbox-tmp", d, "no marker of ours (not made by a session of this user)"))
            continue
        if sid == current_sid:
            keep.append(_candidate("sandbox-tmp", d, "the session running this sweep"))
            continue
        info = sessions.get(sid)
        if not info:
            keep.append(_candidate("sandbox-tmp", d, "its session is unknown to the record"))
            continue
        if info["live"] or not info["ended"]:
            keep.append(_candidate("sandbox-tmp", d, "its session is live or not ended in the record"))
            continue
        age = _age_of_stamp(info["last"], now) or 0
        age = min(age, deep_quiet_seconds(d, now))
        if age < ended_grace:
            keep.append(_candidate("sandbox-tmp", d, "session ended %.0f h ago, under %.0f h"
                                   % (age / 3600, policy["ended_grace_hours"])))
            continue
        b = blocked(d)
        (keep if b else remove).append(_candidate(
            "sandbox-tmp", d, b or "its session ended and nothing changed for %.0f h" % (age / 3600)))

    # tmp: declared globs at the top of tmp_root, owned by this user
    troot = policy.get("tmp_root")
    if troot and policy["tmp_globs"]:
        try:
            names = sorted(os.listdir(troot))
        except OSError:
            names = []
        uid = os.getuid()
        for n in names:
            if not any(fnmatch.fnmatchcase(n, g) for g in policy["tmp_globs"]):
                continue
            p = os.path.join(troot, n)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if st.st_uid != uid or stat.S_ISLNK(st.st_mode):
                continue
            quiet = quiet_seconds(p, now)
            if quiet < grace:
                keep.append(_candidate("tmp", p, "quiet %.0f h, under the %.0f h grace"
                                       % (quiet / 3600, policy["grace_hours"])))
                continue
            b = blocked(p)
            (keep if b else remove).append(
                _candidate("tmp", p, b or "matches a declared /tmp glob and has been quiet %.0f h"
                           % (quiet / 3600)))

    # prune: declared run-tree rules under the project root
    tasks = _task_status(root) if tasks is None and any(
        r.get("task_part") is not None for r in policy["prune"]) else (tasks or {})
    for rule in policy["prune"]:
        g = str(rule["glob"])
        if os.path.isabs(g) or ".." in g.split("/"):
            continue
        matches = [m for m in sorted(_glob.glob(os.path.join(root, g)))
                   if _inside(os.path.realpath(m), root) and not os.path.islink(m)
                   and (not rule.get("dirs_only") or os.path.isdir(m))]
        keep_newest = int(rule.get("keep_newest") or 0)
        newest = set()
        if keep_newest:
            by_age = sorted(matches, key=_mtime, reverse=True)
            newest = set(by_age[:keep_newest])
        min_age = float(rule.get("min_age_days") or 0) * 86400
        named = ""
        if rule.get("named_in"):
            sources = sorted(_glob.glob(os.path.join(root, str(rule["named_in"]))))
            if not sources:
                # the files that say what must stay are missing: keep everything this rule matches
                keep.extend(_candidate("prune", m, "no file matches named_in %s" % rule["named_in"],
                                       rule=g) for m in matches)
                continue
            for f in sources:
                try:
                    with open(f, encoding="utf-8", errors="replace") as fh:
                        named += fh.read() + "\n"
                except OSError:
                    pass
        for m in matches:
            rel = os.path.relpath(m, root)
            if m in newest:
                keep.append(_candidate("prune", m, "one of the newest %d" % keep_newest, rule=g))
                continue
            part = rule.get("task_part")
            if part is not None:
                parts = rel.split(os.sep)
                tid = parts[int(part)] if int(part) < len(parts) else None
                if tasks.get(tid) != "done":
                    keep.append(_candidate("prune", m, "task %s is not done" % tid, rule=g))
                    continue
            if rule.get("named_in") is not None and rule.get("name_part") is not None:
                parts = rel.split(os.sep)
                name = parts[int(rule["name_part"])] if int(rule["name_part"]) < len(parts) else None
                if name and name in named:
                    keep.append(_candidate("prune", m, "%s names %s" % (rule["named_in"], name),
                                           rule=g))
                    continue
            if reports is None:
                reports = report_text(root)
            if rel in reports or m in reports:
                keep.append(_candidate("prune", m, "a proof report names it", rule=g))
                continue
            quiet = quiet_seconds(m, now)
            if quiet < min_age:
                keep.append(_candidate("prune", m, "quiet %.1f d, under %.1f d"
                                       % (quiet / 86400, min_age / 86400), rule=g))
                continue
            b = blocked(m)
            (keep if b else remove).append(_candidate(
                "prune", m, b or str(rule.get("why") or "matches a declared prune rule"), rule=g))
    # worktree: linked worktrees of closed ops, merged, clean and quiet
    wt_policy = policy.get("worktrees")
    if wt_policy is not None:
        ops = _op_status(root) if ops is _UNSET else ops
        base = os.path.realpath(os.path.join(paths.runtime_dir(root), "worktrees"))
        main = str(wt_policy.get("main_branch") or "main")
        min_age = float(wt_policy.get("min_age_days", 3)) * 86400
        for w in (linked_worktrees(root) if ops is not None else []):
            p = w["path"]
            real = os.path.realpath(p)
            if os.path.dirname(real) != base or not os.path.isdir(p) or os.path.islink(p):
                continue
            def keep_it(why):
                keep.append(_candidate("worktree", p, why, branch=w["branch"]))
            names = worktree_ops(p, w["branch"])
            if not names:
                keep_it("no op in its name or branch")
                continue
            not_closed = [o for o in names if ops.get(o) != "closed"]
            if not_closed:
                keep_it(", ".join("%s is %s" % (o, ops.get(o) or "not in the record")
                                  for o in not_closed))
                continue
            hold = worktree_hold(p)
            if hold:
                keep_it(hold)
                continue
            if not w["head"] or _git(["merge-base", "--is-ancestor", w["head"], main], root)[0] != 0:
                keep_it("its HEAD is not merged into %s" % main)
                continue
            quiet = quiet_seconds(p, now)
            if quiet < min_age:
                keep_it("quiet %.1f d, under %.1f d" % (quiet / 86400, min_age / 86400))
                continue
            b = blocked(p)
            if b:
                keep_it(b)
            else:
                remove.append(_candidate("worktree", p, "%s closed, merged into %s and clean"
                                         % (", ".join(names), main), branch=w["branch"],
                                         head=w["head"], ops=names, main=main))
    return {"policy": True, "remove": remove, "keep": keep}


# -------------------------------------------------------------------------------------- apply
def _rm_onexc(func, path, exc):
    """rmtree's error handler. A folder this user cannot write or read (bwrap and module caches
    leave 0500, 0300 or 0000 dirs behind) gets u+rwx and is removed again; any other failure is
    left for the caller's check that the path is gone."""
    try:
        if func in (os.unlink, os.rmdir, os.remove):
            parent = os.path.dirname(path)
            os.chmod(parent, os.lstat(parent).st_mode | stat.S_IRWXU)
            func(path)
        elif os.path.isdir(path) and not os.path.islink(path):
            os.chmod(path, os.lstat(path).st_mode | stat.S_IRWXU)
            remove(path)
    except (OSError, TypeError, ValueError):
        pass


def remove(path) -> None:
    if os.path.islink(path) or not os.path.isdir(path):
        os.unlink(path)
    else:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_rm_onexc)
        else:
            shutil.rmtree(path, onerror=lambda f, p, _info: _rm_onexc(f, p, None))


def remove_worktree(root, c):
    """Re-check the worktree right before removing it (HEAD, holds, op status: anything may have
    changed since the plan), then `git worktree remove` (no --force) and, for an `op<nnn>/...`
    branch still merged into main, `git branch -d`. Returns a note when the branch stays."""
    p = c["path"]
    rc, head = _git(["rev-parse", "HEAD"], p)
    if rc != 0 or head.strip() != c.get("head"):
        raise OSError("HEAD moved since the plan")
    hold = worktree_hold(p)
    if hold:
        raise OSError("held since the plan: " + hold)
    ops = _op_status(root) or {}
    if any(ops.get(o) != "closed" for o in c.get("ops") or ["?"]):
        raise OSError("an op of this worktree is no longer closed")
    rc, out = _git(["worktree", "remove", p], root, timeout=300, err=True)
    if rc != 0:
        raise OSError("git worktree remove refused: " + out.strip()[:300])
    br = c.get("branch") or ""
    main = c.get("main") or "main"
    if re.match(r"^refs/heads/op\d+/", br):
        name = br[len("refs/heads/"):]
        if _git(["merge-base", "--is-ancestor", br, main], root)[0] != 0:
            return "branch %s kept (not merged into %s)" % (name, main)
        rc, out = _git(["branch", "-d", name], root, err=True)
        if rc != 0:
            return "branch %s kept (%s)" % (name, out.strip()[:200])
    elif br:
        return "branch %s kept (not an op branch)" % br[len("refs/heads/"):]
    return None


def apply(root, planned, *, session="alpaca", measure=True) -> dict:
    """Remove every planned candidate and record one `disk-gc` event naming each path, its kind,
    its reason and the bytes freed."""
    done, failed, freed = [], [], 0
    for c in planned["remove"]:
        size = c["bytes"] if "bytes" in c else (tree_bytes(c["path"]) if measure else 0)
        try:
            if c["kind"] == "worktree":
                note = remove_worktree(root, c)
                if note:
                    c = dict(c, note=note)
            else:
                remove(c["path"])
        except Exception as e:
            failed.append(dict(c, error="%s: %s" % (type(e).__name__, e)))
            continue
        if os.path.lexists(c["path"]):
            failed.append(dict(c, error="still present after removal"))
            continue
        done.append(dict(c, bytes=size))
        freed += size
    if done or failed:
        try:
            from alpaca import db
            conn = db.connect(root)
            try:
                db.append_event(conn, session=session, actor="alpaca", kind="disk-gc",
                                data={"removed": done, "failed": failed, "bytes": freed})
            finally:
                conn.close()
        except Exception:
            pass
    return {"removed": done, "failed": failed, "bytes": freed}


# ------------------------------------------------------------------------- background sweeps
def _lock(root):
    import fcntl
    from alpaca import _nofollow
    try:     # no link followed below .alpaca (t-424): a linked config folder or lock file is refused
        fh = os.fdopen(_nofollow.open_file_below(root, ("config",), LOCK_NAME,
                                                 os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o666), "a")
    except OSError:
        return None
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def due(root, policy, now=None) -> bool:
    """True when the last full sweep is older than `sweep_every_hours`."""
    now = time.time() if now is None else now
    try:
        last = os.path.getmtime(os.path.join(paths.config_dir(root), STAMP_NAME))
    except OSError:
        return True
    return now - last >= policy["sweep_every_hours"] * 3600


def sweep(root, *, session, mode) -> dict | None:
    """One locked full sweep when one is due. `mode` "start" spares the starting session's own
    folder; "end" runs at SessionEnd, where the ending session's folder is spared by its
    `ended_grace_hours`. Returns None when another sweep holds the lock or none is due."""
    root = os.path.realpath(root)
    policy = load_policy(root)
    if not policy:
        return None
    fh = _lock(root)
    if fh is None:
        return None
    try:
        if not due(root, policy):
            return None
        from alpaca import _nofollow
        _nofollow.write_below(root, ("config",), STAMP_NAME, ("%d\n" % time.time()).encode("ascii"), 0o600)
        planned = plan(root, policy, current_sid=session if mode == "start" else None)
        return apply(root, planned, session=session)
    finally:
        fh.close()


def spawn_sweep(root, sid, mode) -> bool:
    """Start a sweep in a detached process so a hook returns at once. Spawns nothing when the
    project declares no `disk:` block, no sweep is due, or ALPACA_DISK_SWEEP=0 (the test suite)."""
    if os.environ.get("ALPACA_DISK_SWEEP") == "0":
        return False
    policy = load_policy(root)
    if not policy or sid in (None, "", "unknown") or not due(root, policy):
        return False
    # through the one launcher (t-070 round 2 C-A): `python -I`, no working folder on the path
    from alpaca import child
    argv = child.python_argv("alpaca.disk", "sweep", "--root", root, "--session", sid,
                             "--mode", mode, served_root=root)
    subprocess.Popen(argv, cwd=root, env=child.child_env(served_root=root), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                     close_fds=True)
    return True


# -------------------------------------------------------------------------------------- usage
def _du(path, timeout=15) -> int | None:
    try:
        p = subprocess.run(["du", "-sb", path], capture_output=True, text=True, timeout=timeout)
        return int(p.stdout.split()[0]) if p.stdout else None
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def usage(root, policy=None) -> dict:
    """Bytes held by the project's runtime dir, its session scratch, and this user's /tmp share."""
    root = os.path.realpath(root)
    policy = load_policy(root) if policy is None else policy
    out = {"project": _du(paths.runtime_dir(root)), "scratch": None, "tmp": None}
    bases = scratch_bases(root, policy)
    if bases:
        sizes = [_du(b) for b in bases]
        out["scratch"] = None if None in sizes else sum(sizes)
    try:
        from alpaca import sysmon
        q = sysmon.user_quota("/tmp")
        if q and q.get("used_b") is not None:
            out["tmp"] = int(q["used_b"])
    except Exception:
        pass
    return out


def doctor_check(root, add) -> None:
    """One `disk` line: warn when a share passes its `warn_gib` threshold. Never an error: the
    owner chose cleanup over caps."""
    policy = load_policy(root)
    errs = policy_errors(root)
    if errs:
        add("disk", "warn", "; ".join(errs[:3]))
        return
    u = usage(root, policy)
    warn = (policy or {}).get("warn_gib") or {}
    shown, over = [], []
    for key, label in (("project", ".alpaca"), ("scratch", "scratch"), ("tmp", "/tmp")):
        v = u.get(key)
        if v is None:
            continue
        shown.append("%s %.1fG" % (label, v / GIB))
        limit = warn.get(key)
        if limit is not None and v > float(limit) * GIB:
            over.append("%s over %sG" % (label, limit))
    if not policy:
        add("disk", "ok", ", ".join(shown) + "; no disk: block in project.yaml, nothing is swept")
    elif over:
        add("disk", "warn", ", ".join(shown) + "; " + ", ".join(over)
            + "; run `alpaca retention gc` to see what can go")
    else:
        add("disk", "ok", ", ".join(shown) or "nothing measured")


# ----------------------------------------------------------------------------------- entry
def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="python -m alpaca.disk")
    sub = ap.add_subparsers(dest="verb", required=True)
    sw = sub.add_parser("sweep")
    sw.add_argument("--root", required=True)
    sw.add_argument("--session", required=True)
    sw.add_argument("--mode", choices=("start", "end"), required=True)
    a = ap.parse_args(argv)
    try:
        sweep(a.root, session=a.session, mode=a.mode)
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(_main())
