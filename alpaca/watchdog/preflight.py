"""Preflight (design/watchdog/design.md section 4): what a git command would touch, and a verdict.

Reads only. Every git call goes through `alpaca.gitview.git` (no optional locks, never writes an
index, ref, reflog or working tree) and the record is read through `db.connect_readonly`. The one
exception is the merge dry run, which runs in a scratch worktree under
`.alpaca/watchdog/scratch/<id>` that is removed again; the target tree is never touched.
"""
from __future__ import annotations

import datetime
import os
import re
import shlex
import shutil
import subprocess
import time

from alpaca import gitview
from alpaca.watchdog import gitw, runs

DRY_LINES = 40
RECENT_S = 24 * 3600
VALUE_FLAGS = {"-m", "-F", "-s", "-X", "--message", "--file", "--strategy", "--strategy-option"}


# ---- parsing -------------------------------------------------------------------------------

GLOBAL_VALUE = ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix", "--config-env")
REDIRECT = ("--git-dir", "--work-tree")          # these (and GIT_DIR=/GIT_WORK_TREE=) change the tree git works on
OPERATORS = ("&&", "||", ";", "|", "&")
LONG = {
    "reset": ("--soft", "--mixed", "--hard", "--merge", "--keep", "--quiet", "--patch", "--refresh", "--no-refresh",
              "--recurse-submodules", "--no-recurse-submodules", "--pathspec-from-file", "--pathspec-file-nul"),
    "clean": ("--force", "--dry-run", "--exclude", "--quiet", "--interactive"),
    "branch": ("--delete", "--force", "--move", "--copy", "--list", "--merged", "--no-merged", "--all", "--remotes",
               "--set-upstream-to", "--unset-upstream", "--track", "--no-track", "--create-reflog"),
    "merge": ("--abort", "--quit", "--continue", "--no-commit", "--commit", "--no-ff", "--ff", "--ff-only", "--squash",
              "--strategy", "--strategy-option", "--message", "--file", "--no-verify", "--verbose", "--quiet"),
    "worktree": ("--force", "--detach", "--lock", "--no-checkout", "--quiet", "--reason"),
    "stash": ("--include-untracked", "--all", "--keep-index", "--no-keep-index", "--message", "--patch", "--quiet"),
    "checkout": ("--force", "--detach", "--merge", "--theirs", "--ours", "--patch", "--quiet", "--orphan", "--overlay",
                 "--no-overlay"),
    "restore": ("--source", "--staged", "--worktree", "--patch", "--quiet", "--ours", "--theirs", "--overlay", "--no-overlay"),
    "switch": ("--create", "--force-create", "--detach", "--force", "--discard-changes", "--merge", "--quiet"),
    "push": ("--force", "--force-with-lease", "--force-if-includes", "--delete", "--mirror", "--all", "--tags",
             "--dry-run", "--set-upstream", "--follow-tags", "--prune", "--quiet"),
}


def _expand(sub, args):
    """git accepts any unique prefix of a long option (`--har` for `--hard`); spell them out before classifying."""
    table = LONG.get(sub)
    if not table:
        return list(args)
    out = []
    for n, a in enumerate(args):
        if a == "--":
            out += list(args[n:])
            break
        if a.startswith("--") and len(a) > 2:
            name, eq, val = a.partition("=")
            hits = [o for o in table if o.startswith(name)]
            if name not in table and len(hits) == 1:
                a = hits[0] + eq + val
        out.append(a)
    return out


def _split_chain(argv):
    """(first command, [later commands]) when argv holds shell operators (the list was built from a shell line)."""
    for i, a in enumerate(argv):
        if a in OPERATORS or (a.endswith(";") and len(a) > 1):
            head = list(argv[:i]) + ([a[:-1]] if a not in OPERATORS else [])
            rest = [a_ for a_ in argv[i + 1:]]
            more = []
            cur = []
            for b in rest:
                if b in OPERATORS or (b.endswith(";") and len(b) > 1):
                    if b not in OPERATORS:
                        cur.append(b[:-1])
                    more.append(cur)
                    cur = []
                else:
                    cur.append(b)
            more.append(cur)
            return head, [m for m in more if m]
    return list(argv), []


def parse(argv, cwd=None):
    """{cwd, sub, args, kind, gitopts, start, redirect, chain} from a git argv. A leading `GIT_DIR=...`
    and `git`, the global options (`-C dir`, `-c k=v`, `--git-dir`, `--work-tree`, both `--opt val` and
    `--opt=val`) are handled; shell operators split off later commands into `chain`."""
    argv, chain = _split_chain(argv)
    start = cwd or os.getcwd()
    here = start
    opts, redirect = [], False
    while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
        key, _, val = argv.pop(0).partition("=")
        if key in ("GIT_DIR", "GIT_WORK_TREE"):
            opts.append("--%s=%s" % ("git-dir" if key == "GIT_DIR" else "work-tree", val))
            redirect = True
    if argv and os.path.basename(argv[0]) == "git":
        argv = argv[1:]
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        opt = argv[i]
        name, eq, _ = opt.partition("=")
        if eq and name in GLOBAL_VALUE and name not in ("-C", "-c"):
            opts.append(opt)
            redirect = redirect or name in REDIRECT
            i += 1
        elif opt in GLOBAL_VALUE and i + 1 < len(argv):
            opts += [opt, argv[i + 1]]
            if opt == "-C":
                here = os.path.normpath(os.path.join(here, os.path.expanduser(argv[i + 1])))
            redirect = redirect or opt in REDIRECT
            i += 2
        else:
            i += 1
    sub = argv[i] if i < len(argv) else ""
    args = _expand(sub, argv[i + 1:])
    return {"cwd": here, "sub": sub, "args": args, "kind": _kind(sub, args), "gitopts": opts, "start": start,
            "redirect": redirect, "chain": chain}


def _resolve(start, opts):
    """(toplevel, git dir, error) for the tree `git <opts>` works on: `git <same -C/--git-dir/--work-tree>
    rev-parse --show-toplevel --absolute-git-dir` run with every GIT_* variable stripped. The git dir holds
    the index, HEAD and branch the command writes; with `--git-dir` alone it can belong to another tree
    than the files (adversarial round 2, A1)."""
    keep = []
    i = 0
    while i < len(opts):
        o = opts[i]
        if o in ("-C",):
            keep += [o, opts[i + 1]]
            i += 2
        elif o in ("-c", "--namespace", "--exec-path", "--super-prefix", "--config-env"):
            i += 2
        elif o in REDIRECT:
            keep += [o, opts[i + 1]]
            i += 2
        elif o.partition("=")[0] in REDIRECT:
            keep.append(o)
            i += 1
        else:
            i += 1
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        done = subprocess.run(["git", "--no-pager", "--no-optional-locks", *keep, "rev-parse", "--show-toplevel",
                               "--absolute-git-dir"], cwd=start, capture_output=True, text=True, env=env,
                              timeout=gitview.TIMEOUT, errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, None, str(exc)
    lines = done.stdout.splitlines()
    if done.returncode != 0 or len(lines) < 2:
        return None, None, (done.stderr.strip()[:200] or "git rev-parse failed")
    return lines[0], lines[1], None


def _own_git_dir(top):
    """The git dir a tree uses when no --git-dir is given: `git -C <top> rev-parse --absolute-git-dir`."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        done = subprocess.run(["git", "--no-pager", "--no-optional-locks", "-C", top, "rev-parse", "--absolute-git-dir"],
                              capture_output=True, text=True, env=env, timeout=gitview.TIMEOUT, errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _rc(cwd, *args):
    """Return code of a read-only git call with every GIT_* variable stripped (None when git cannot run)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        return subprocess.run(["git", "--no-pager", "--no-optional-locks", "-C", cwd, *args], capture_output=True,
                              env=env, timeout=gitview.TIMEOUT).returncode
    except (OSError, subprocess.TimeoutExpired):
        return None


def _short(args):
    return {c for a in args if a.startswith("-") and not a.startswith("--") for c in a[1:]}


def _positional(args):
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a == "--":
            continue
        elif a in VALUE_FLAGS:
            skip = True
        elif not a.startswith("-"):
            out.append(a)
    return out


def _kind(sub, args):
    if sub == "push":
        return "push"
    if sub == "worktree" and args[:1] == ["remove"]:
        return "worktree-remove"
    if sub == "branch" and ({"--delete", "-d", "-D"} & set(args) or {"d", "D"} & _short(args)):
        return "branch-delete"
    if sub == "clean" and not ("--dry-run" in args or "n" in _short(args)):
        return "clean"
    if sub == "reset" and "--hard" in args:
        return "reset-hard"
    if sub == "merge" and not {"--abort", "--quit", "--continue"} & set(args):
        return "merge"
    if sub == "stash" and args[:1] not in (["list"], ["show"]):
        return "stash"
    return "unknown"


# ---- facts ---------------------------------------------------------------------------------

def _g(cwd, *args):
    """Read-only git; '' when it fails."""
    try:
        return gitview.git(cwd, *args, check=False)
    except gitview.GitError:
        return ""


def _heads(root):
    out = {}
    for line in _g(root, "for-each-ref", "refs/heads", "--format=%(refname:short) %(objectname)").splitlines():
        name, _, sha = line.rpartition(" ")
        if name:
            out[name] = sha
    return out


def _parse_time(value):
    if isinstance(value, (int, float)):
        return float(value) / (1000.0 if value > 1e11 else 1.0)
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def load_sessions(root):
    """(sessions, claims) from the record, read-only. Each session: sid, cwd, last_beat (epoch), ended."""
    from alpaca import db, sessions_view
    conn = db.connect_readonly(root)
    try:
        rows = conn.execute("SELECT sid, cwd, last_beat, ended FROM sessions").fetchall()
        sessions = [{"sid": r[0], "cwd": r[1], "last_beat": _parse_time(r[2]), "ended": r[3]} for r in rows]
        try:
            claims = sessions_view.claims_by_session(conn)
        except Exception:
            claims = {}
    except Exception:
        sessions, claims = [], {}
    finally:
        conn.close()
    return sessions, claims


def _real(path):
    return os.path.realpath(path or "")


def _inside(path, tree):
    p, t = _real(path), _real(tree)
    return p == t or p.startswith(t + os.sep)


def _session_facts(target_path, trees, sessions, now):
    """Sessions whose working directory is in target_path (and not in a deeper worktree)."""
    hits = []
    real = _real(target_path)
    for s in sessions:
        if not s.get("cwd"):
            continue
        if gitview.owner(s["cwd"], [_real(t) for t in trees]) != real:
            continue
        beat = s.get("last_beat")
        live = (not s.get("ended")) and bool(beat) and now - beat < gitview.LIVE_S
        recent = bool(beat) and now - beat < RECENT_S
        hits.append({"sid": s["sid"], "cwd": s["cwd"], "last_beat": beat, "ended": s.get("ended"),
                     "live": live, "recent": recent})
    return hits


def _tree_target(path, w, uncommitted, untracked):
    return {"type": "worktree", "path": path, "name": os.path.basename(path.rstrip(os.sep)),
            "branch": w.get("branch"), "head": w.get("head"), "detached": bool(w.get("detached")),
            "uncommitted": uncommitted, "untracked": untracked}


def _counts(path):
    files = gitview.status(path) if os.path.isdir(path) else []
    untracked = sum(1 for code, _ in files if code == "??")
    return len(files) - untracked, untracked


def _unreachable(root, tip, exclude_branches=()):
    """Commits reachable from tip and from no local branch outside exclude_branches."""
    others = [sha for name, sha in _heads(root).items() if name not in exclude_branches]
    out = _g(root, "rev-list", "--count", tip, *["^" + s for s in others]).strip()
    return int(out) if out.isdigit() else None


def _lines(text):
    lines = text.splitlines()
    return lines[:DRY_LINES] + (["... (%d more lines)" % (len(lines) - DRY_LINES)] if len(lines) > DRY_LINES else [])


def _admin_of(scratch):
    """The admin directory a scratch worktree's `.git` file points at, or None."""
    try:
        with open(os.path.join(scratch, ".git"), encoding="utf-8") as fh:
            line = fh.read().strip()
    except OSError:
        return None
    return os.path.realpath(line[len("gitdir:"):].strip()) if line.startswith("gitdir:") else None


def _remove_scratch(root, tree, scratch):
    """Remove the watchdog's own scratch worktree. Returns the scratch path when it is still there (it is
    then left alone and reported), else None. `git worktree prune` is never run (repo-wide): after an
    rmtree only the one admin entry whose `gitdir` file names this scratch is deleted."""
    if not os.path.isdir(scratch):
        return None
    base = os.path.realpath(os.path.join(root, runs.SCRATCH))
    if not os.path.realpath(scratch).startswith(base + os.sep):
        return scratch                       # never touch anything outside the watchdog's scratch folder
    admin = _admin_of(scratch)
    try:
        gitw.assert_toplevel(scratch, linked=True)
        gitw.run(scratch, "merge", "--abort", check=False)
        if gitw.run(tree, "worktree", "remove", scratch, check=False).returncode != 0:
            gitw.run(tree, "worktree", "remove", "--force", scratch, check=False)
    except gitw.GitWriteError:
        pass
    if not os.path.isdir(scratch):
        return None
    shutil.rmtree(scratch, ignore_errors=True)
    if os.path.isdir(scratch):
        return scratch
    try:
        common = gitw._out(tree, "rev-parse", "--git-common-dir")
        common = os.path.realpath(common if os.path.isabs(common) else os.path.join(tree, common))
        if admin and os.path.dirname(admin) == os.path.join(common, "worktrees") and os.path.isdir(admin):
            with open(os.path.join(admin, "gitdir"), encoding="utf-8") as fh:
                named = fh.read().strip()
            if os.path.realpath(named) == os.path.join(os.path.realpath(scratch), ".git"):
                shutil.rmtree(admin, ignore_errors=True)
    except (OSError, gitw.GitWriteError, TypeError):
        pass
    return None


def merge_dry_run(root, run_id, tree, rev, left=None):
    """(conflicts or None, output lines). None when the dry run could not run. The merge runs in a
    detached scratch worktree under .alpaca/watchdog/scratch/<id> (no ref is written). The scratch is
    removed in the `finally`; one that cannot be removed is left and its path appended to `left`."""
    base = os.path.join(root, runs.SCRATCH)
    os.makedirs(base, exist_ok=True)
    scratch = os.path.join(base, run_id)
    try:
        added = gitw.run(tree, "worktree", "add", "--detach", scratch, "HEAD", check=False)
        if added.returncode != 0:
            return None, ["could not create the scratch worktree: " + (added.stderr.strip()[:200])]
        gitw.assert_toplevel(scratch, linked=True)      # the merge only ever runs in the scratch itself
        who = {"GIT_AUTHOR_NAME": "watchdog", "GIT_AUTHOR_EMAIL": "watchdog@localhost",
               "GIT_COMMITTER_NAME": "watchdog", "GIT_COMMITTER_EMAIL": "watchdog@localhost"}
        merged = gitw.run(scratch, "merge", "--no-commit", "--no-ff", rev, env=who, check=False)
        text = (merged.stdout + merged.stderr).strip()
        conflicts = [l for l in _g(scratch, "diff", "--name-only", "--diff-filter=U").splitlines() if l]
        # rc 1 with unmerged paths is a conflict; rc 128 (or anything else without conflicts) is a fatal
        if merged.returncode == 128 or (merged.returncode != 0 and not conflicts):
            return None, _lines("git merge --no-commit --no-ff %s failed: %s" % (rev, text))
        return conflicts, _lines(text)
    except gitw.GitWriteError as exc:
        return None, [str(exc)]
    finally:
        leftover = _remove_scratch(root, tree, scratch)
        if leftover and left is not None:
            left.append(leftover)


def _clean_dry(cwd, args):
    """The dry run of a `git clean`: the same flags (-e/--exclude with their values), no -f/-q/-i."""
    flags, paths, past = ["-n"], [], False
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if past:
            paths.append(a)
        elif a == "--":
            past = True
        elif a.startswith("--"):
            name, eq, val = a.partition("=")
            if name == "--exclude":
                if not eq and i < len(args):
                    val, i = args[i], i + 1
                flags += ["-e", val]
            elif name not in ("--force", "--dry-run", "--quiet", "--interactive"):
                flags.append(a)
        elif a.startswith("-") and len(a) > 1:
            cluster = a[1:]
            kept = ""
            for k, ch in enumerate(cluster):
                if ch == "e":
                    val = cluster[k + 1:]
                    if not val and i < len(args):
                        val, i = args[i], i + 1
                    flags += ["-e", val]
                    break
                if ch not in "fnqi":
                    kept += ch
            if kept:
                flags.append("-" + kept)
        else:
            paths.append(a)
    return _g(cwd, "clean", *flags, *(["--"] + paths if paths else []))


# ---- the run -------------------------------------------------------------------------------

def run(root, argv, for_=None, session="cli", sessions=None, claims=None, now=None, cwd=None):
    root = os.path.realpath(root)
    now = now or time.time()
    run_id = runs.new_id()
    runs.run_dir(root, run_id, create=True)
    p = parse(argv, cwd)
    sub, args, kind, here = p["sub"], p["args"], p["kind"], p["cwd"]
    if sessions is None:
        sessions, loaded_claims = load_sessions(root)
        claims = loaded_claims if claims is None else claims
    claims = claims or {}

    reasons = []

    def add(level, text, pointer, fix):
        reasons.append({"level": level, "text": text, "pointer": pointer, "fix": fix})

    if p["chain"]:
        add("risky", "the argument list holds %d more command(s) after a shell operator; only the first is judged"
            % len(p["chain"]), "argv: %s" % " ".join(shlex.quote(a) for a in argv),
            "one command per preflight: run the preflight once for each command")
        for seg in p["chain"]:
            later = parse(seg, cwd)
            if later["kind"] == "push":
                add("stop", "push is the owner's (brief 4): it follows the first command in the same argument list",
                    "git %s" % " ".join(shlex.quote(a) for a in [later["sub"]] + later["args"]),
                    "ask the owner; the watchdog never pushes")
    if p["redirect"]:
        top_r, gitdir_r, why = _resolve(p["start"], p["gitopts"])
        own = _own_git_dir(top_r) if top_r else None
        if top_r and own and _real(own) != _real(gitdir_r):
            here = top_r
            add("stop", "the command writes the index, HEAD and branch of git dir %s while its files are %s, whose "
                "own git dir is %s; that mix rewrites another tree's state" % (gitdir_r, top_r, own),
                "git %s rev-parse --show-toplevel --absolute-git-dir" % " ".join(shlex.quote(a) for a in p["gitopts"]),
                "run the command with -C <the tree it is meant for> and no --git-dir")
        elif top_r:
            here = top_r                 # the tree the redirected command really works on
        else:
            add("risky", "cannot resolve the tree --git-dir/--work-tree (or GIT_DIR/GIT_WORK_TREE) point at (%s)" % why,
                "git %s rev-parse --show-toplevel" % " ".join(shlex.quote(a) for a in p["gitopts"]),
                "run the command with -C <worktree> instead of --git-dir/--work-tree")
    base_cwd = here if os.path.isdir(here) else root
    try:
        trees_list = gitview.worktrees(base_cwd)
        main = gitview.main_branch(base_cwd)
        top = gitview.toplevel(base_cwd)
    except gitview.GitError as exc:
        trees_list, main, top = [], "main", base_cwd
        add("risky", "not a git repository here (%s)" % exc, "git -C %s rev-parse --show-toplevel" % base_cwd,
            "run the preflight from inside the repository")
    tree_paths = [w["path"] for w in trees_list]
    by_path = {_real(w["path"]): w for w in trees_list}

    targets, dry, intent = [], [], {"removed_worktrees": [], "deleted_branches": [], "moved": {}}
    dry_ok = True
    unreachable = 0
    merged_note = None

    def tree_for(path):
        w = by_path.get(_real(path))
        if not w:
            return None
        files = _counts(w["path"])
        return _tree_target(w["path"], w, *files)

    def remove_one(t, nested_in=None):
        nonlocal unreachable
        targets.append(t)
        intent["removed_worktrees"].append(t["path"])
        if nested_in:
            t["nested_in"] = nested_in
            add("risky", "worktree %s lies inside %s and is deleted with it" % (t["path"], nested_in),
                "git -C %s worktree list" % base_cwd, "remove %s first on its own terms, or move it out of %s" % (t["path"], nested_in))
        ahead, behind = gitview.ahead_behind(base_cwd, main, t["head"]) if t["head"] else (None, None)
        t["ahead_of_main"] = ahead
        dry.append("$ git -C %s status --porcelain" % t["path"])
        dry.extend(_lines(_g(t["path"], "status", "--porcelain")))
        dry.append("%s is %s commit(s) ahead of %s" % (t["path"], ahead, main))
        if t["detached"]:
            n = _unreachable(base_cwd, t["head"])
            t["unreachable"] = n
            unreachable += n or 0
            if n:
                add("risky", "%d commit(s) of detached %s are on no branch and would become unreachable"
                    % (n, t["name"]), "git -C %s rev-list --count %s --not --branches" % (base_cwd, t["head"][:12]),
                    "branch it first (`git branch <name> %s`) or run `alpaca watchdog undo` and keep the undo ref" % t["head"][:12])
        lost = t["uncommitted"] + t["untracked"]
        if lost:
            add("risky", "%d uncommitted and %d untracked file(s) in %s would be lost"
                % (t["uncommitted"], t["untracked"], t["path"]), "git -C %s status --porcelain" % t["path"],
                "commit or move the files, or keep the wip snapshot from `alpaca watchdog undo`")

    def nested_trees(parents):
        """(registered worktree, the target it lies inside) for every worktree inside a target."""
        real = [_real(t["path"]) for t in parents]
        out = []
        for w in trees_list:
            rp = _real(w["path"])
            if w.get("bare") or rp in real:
                continue
            for t, r in zip(parents, real):
                if rp.startswith(r + os.sep):
                    out.append((w, t["path"]))
                    break
        return out

    if kind == "worktree-remove":
        paths = [os.path.normpath(os.path.join(here, a)) for a in _positional(args[1:])]
        if not paths:
            dry_ok = False
        for path in paths:
            t = tree_for(path)
            if t is None:
                dry_ok = False
                add("risky", "%s is not a registered worktree" % path, "git -C %s worktree list" % base_cwd,
                    "name a path that `git worktree list` shows")
                continue
            remove_one(t)
        for w, parent in nested_trees(list(targets)):
            remove_one(tree_for(w["path"]), nested_in=parent)
    elif kind == "branch-delete":
        names = _positional(args)
        heads = _heads(base_cwd)
        if not names:
            dry_ok = False
        merged_out = _g(base_cwd, "branch", "--merged", main)
        dry.append("$ git branch --merged %s" % main)
        dry += _lines(merged_out)
        merged = {l.strip(" *+") for l in merged_out.splitlines()}
        for name in names:
            if name not in heads:
                dry_ok = False
                add("risky", "branch %s does not exist" % name, "git -C %s for-each-ref refs/heads/%s" % (base_cwd, name),
                    "name an existing local branch")
                continue
            ahead, _ = gitview.ahead_behind(base_cwd, main, heads[name])
            targets.append({"type": "branch", "branch": name, "head": heads[name], "path": None, "name": name,
                            "ahead_of_main": ahead, "merged": name in merged})
            intent["deleted_branches"].append(name)
            dry.append("%s is %s commit(s) ahead of %s" % (name, ahead, main))
        gone = {t["branch"] for t in targets}
        for t in targets:
            n = _unreachable(base_cwd, t["head"], exclude_branches=gone)
            t["unreachable"] = n
            unreachable += n or 0
            if n:
                add("risky", "branch %s holds %d commit(s) on no other branch; they would become unreachable"
                    % (t["branch"], n), "git -C %s rev-list --count %s --not <other branches>" % (base_cwd, t["branch"]),
                    "merge the branch first or keep the undo ref from `alpaca watchdog undo`")
    else:
        w = by_path.get(_real(top))
        if w:
            t = _tree_target(w["path"], w, *_counts(w["path"]))
            targets.append(t)
        else:
            t = None
        if kind == "clean":
            dry.append("$ git clean -n ...")
            out = _clean_dry(base_cwd, args)
            dry += _lines(out)
            n = sum(1 for l in out.splitlines() if l.startswith("Would remove"))
            if not n and t:
                n = t["untracked"]       # the dry run said nothing: fall back to what git status calls untracked
                if n:
                    dry.append("(the dry run listed nothing; counting the %d untracked file(s) in the tree)" % n)
            if n:
                add("risky", "%d untracked file(s) would be deleted" % n, "git -C %s clean -n" % base_cwd,
                    "list them, keep what is needed, or keep the wip snapshot from `alpaca watchdog undo`")
            forced = sum(a.count("f") if a.startswith("-") and not a.startswith("--") else (a == "--force")
                         for a in args if a != "--")
            if t and ({"x", "X"} & _short(args)) and forced >= 2:
                for w, parent in nested_trees([t]):      # -x -ff also removes nested repositories and worktrees
                    nt = tree_for(w["path"])
                    nt["nested_in"] = parent
                    targets.append(nt)
        elif kind == "reset-hard":
            pos = _positional(args)
            rev = pos[0] if pos else "HEAD"
            dry.append("$ git diff --stat HEAD %s" % rev)
            dry += _lines(_g(base_cwd, "diff", "--stat", "HEAD", rev))
            dry += _lines(_g(base_cwd, "status", "--porcelain"))
            new = _g(base_cwd, "rev-parse", "--verify", "--quiet", rev + "^{commit}").strip()
            if not new:
                dry_ok = False
                add("risky", "%s does not resolve to a commit" % rev, "git -C %s rev-parse %s" % (base_cwd, rev),
                    "name a revision that exists")
            if t and t.get("branch") and new:
                intent["moved"][t["branch"]] = new
                n = _g(base_cwd, "rev-list", "--count", "HEAD", "^" + new,
                       *["^" + s for nm, s in _heads(base_cwd).items() if nm != t["branch"]]).strip()
                unreachable = int(n) if n.isdigit() else 0
                if unreachable:
                    add("risky", "%d commit(s) of %s would become unreachable" % (unreachable, t["branch"]),
                        "git -C %s rev-list --count HEAD ^%s" % (base_cwd, rev),
                        "keep the undo ref from `alpaca watchdog undo` or branch the tip first")
            if t and t["uncommitted"]:
                add("risky", "%d uncommitted file(s) in %s would be overwritten" % (t["uncommitted"], t["path"]),
                    "git -C %s status --porcelain" % t["path"], "commit or keep the wip snapshot from `alpaca watchdog undo`")
            if t:
                # reset --hard leaves a nested worktree in an ignored folder alone, but one at a path the
                # target revision tracks gets its files overwritten (adversarial round 2, F1)
                for w, parent in nested_trees([t]):
                    rel = os.path.relpath(w["path"], t["path"])
                    if _rc(t["path"], "check-ignore", "-q", rel) == 0:
                        continue
                    nt = tree_for(w["path"])
                    if nt is None:
                        continue
                    nt["nested_in"] = parent
                    targets.append(nt)
                    add("risky", "worktree %s lies at a path %s does not ignore; reset --hard can overwrite its files"
                        % (w["path"], parent), "git -C %s check-ignore %s" % (t["path"], rel),
                        "move the worktree out of %s, or into an ignored folder" % parent)
        elif kind == "merge":
            revs = _positional(args)
            if not revs or not t:
                dry_ok = False
            else:
                left = []
                conflicts, out = merge_dry_run(root, run_id, t["path"], revs[0], left)
                for path in left:
                    add("risky", "the scratch worktree %s could not be removed and was left in place" % path,
                        "git -C %s worktree list" % base_cwd, "remove it by hand: git worktree remove --force %s" % path)
                dry.append("$ git merge --no-commit --no-ff %s  (in a scratch worktree, removed afterwards)" % revs[0])
                dry += out
                if conflicts is None:
                    dry_ok = False
                elif conflicts:
                    add("stop", "the merge conflicts in %d file(s): %s" % (len(conflicts), ", ".join(conflicts[:8])),
                        "scratch merge of %s into %s; see Dry run" % (revs[0], t["branch"] or "HEAD"),
                        "resolve in a branch worktree, or merge a different revision")
                if t.get("branch"):
                    already = _rc(base_cwd, "merge-base", "--is-ancestor", revs[0], "HEAD") == 0
                    intent["moved"][t["branch"]] = None if not already else t["head"]
        elif kind == "stash":
            dry.append("$ git status --porcelain")
            dry += _lines(_g(base_cwd, "status", "--porcelain"))
            if args[:1] in (["clear"], ["drop"], ["pop"], ["branch"]):
                entries = _g(base_cwd, "stash", "list")
                dry.append("$ git stash list")
                dry += _lines(entries)
                add("risky", "stash %s changes the repo-wide stash list (%d entr%s now); a dropped entry becomes unreachable"
                    % (args[0], len(entries.splitlines()), "y" if len(entries.splitlines()) == 1 else "ies"),
                    "git -C %s stash list" % base_cwd,
                    "apply the entry in a branch worktree first, or `git stash show -p stash@{n}` and save it")
        elif kind == "push":
            add("stop", "push is the owner's (brief 4)", "git %s" % " ".join(shlex.quote(a) for a in [sub] + args),
                "ask the owner; the watchdog never pushes")
        else:
            add("risky", "unknown command: git %s" % (sub or "(none)"), "alpaca/watchdog/preflight.py classify table",
                "the watchdog does not know what it touches; ask the owner")

    # stop: git_guard would deny it in the shared main tree
    from alpaca.hooks import git_guard
    reason = git_guard.rewrites_worktree(sub, args) if sub in ("stash", "reset", "clean", "checkout", "restore", "switch") else None
    if reason and git_guard._main_toplevel(base_cwd) == _real(root):
        add("stop", "%s in the shared main tree at %s" % (reason, _real(root)), "alpaca/hooks/git_guard.py rewrites_worktree",
            "do it in a separate worktree (git worktree add), or the owner runs it in their own terminal")
    if sub in git_guard.BRINGS_REVISION:
        live = git_guard.writes_live_record(base_cwd, sub, args)
        if live:
            add("stop", live, "alpaca/hooks/git_guard.py writes_live_record",
                "remove the live record from that branch first, as the message says")

    # sessions and claims in the targets
    session_rows, held = [], {}
    for t in targets:
        if not t.get("path"):
            continue
        for s in _session_facts(t["path"], tree_paths, sessions, now):
            s["target"] = t["path"]
            session_rows.append(s)
            held[s["sid"]] = claims.get(s["sid"], [])
            dirty = t["uncommitted"] + t["untracked"]
            if s["live"] and dirty:
                add("stop", "live session %s works in %s with %d uncommitted file(s)%s"
                    % (s["sid"][:12], t["path"], dirty,
                       (" and holds " + ", ".join(claims[s["sid"]])) if claims.get(s["sid"]) else ""),
                    "record: sessions row %s (cwd, last_beat); git -C %s status --porcelain" % (s["sid"], t["path"]),
                    "wait for the session to commit or end, or ask it to move; the undo wip snapshot is not consent")
            elif s["recent"]:
                add("risky", "session %s was active in %s within 24 h" % (s["sid"][:12], t["path"]),
                    "record: sessions row %s last_beat" % s["sid"], "ask that session before the owner signs")

    if not dry_ok and kind != "unknown":
        add("risky", "the dry run could not run", "see Dry run", "fix the command or its target, then run the preflight again")

    level = "stop" if any(r["level"] == "stop" for r in reasons) else ("risky" if reasons else "safe")

    facts = {"unreachable_commits": unreachable, "main_branch": main,
             "uncommitted": sum(t.get("uncommitted", 0) for t in targets),
             "untracked": sum(t.get("untracked", 0) for t in targets), "dry_run_ok": dry_ok}
    doc = {"id": run_id, "created": now, "for": for_, "session": session, "root": root, "cwd": base_cwd,
           "command": ["git"] + ([sub] if sub else []) + args, "kind": kind, "verdict": level,
           "reasons": reasons, "targets": targets, "sessions": session_rows, "claims": held,
           "facts": facts, "dry_run": dry, "intent": intent,
           "refs": _heads(base_cwd) if trees_list else {},
           "worktrees": [{"path": w["path"], "head": w["head"], "branch": w["branch"], "detached": w["detached"]}
                         for w in trees_list]}
    runs.save(root, run_id, "preflight.json", doc)
    report = runs.write_text(root, run_id, "preflight.md", render(doc))
    runs.record(root, "watchdog-preflight", {"id": run_id, "command": doc["command"], "verdict": level,
                                              "for": for_, "report": os.path.relpath(report, root)}, session)
    return {"id": run_id, "verdict": level, "report": report, "reasons": reasons}


def render(doc):
    out = ["# Watchdog preflight %s" % doc["id"], "", "## Command", "", "`%s`" % " ".join(shlex.quote(a) for a in doc["command"]),
           "", "Run from `%s`. Asked by: %s. Class: %s." % (doc["cwd"], doc.get("for") or "(not given)", doc["kind"]),
           "", "## Verdict: %s" % doc["verdict"].upper(), ""]
    if doc["reasons"]:
        for r in doc["reasons"]:
            out.append("- [%s] %s (pointer: %s)" % (r["level"], r["text"], r["pointer"]))
    else:
        out.append("- nothing found that would be lost or contested (pointer: the Targets and Dry run sections below)")
    out += ["", "## Targets", ""]
    if doc["targets"]:
        for t in doc["targets"]:
            out.append("- %s `%s` branch=%s head=%s uncommitted=%s untracked=%s" % (
                t["type"], t.get("path") or t.get("branch"), t.get("branch"), (t.get("head") or "")[:12],
                t.get("uncommitted", "n/a"), t.get("untracked", "n/a")))
    else:
        out.append("- none identified")
    out += ["", "## Sessions", ""]
    if doc["sessions"]:
        for s in doc["sessions"]:
            claims = doc["claims"].get(s["sid"]) or []
            out.append("- %s in `%s`: %s%s" % (s["sid"], s["target"], "live" if s["live"] else "not live",
                                                 ("; claims " + ", ".join(claims)) if claims else ""))
    else:
        out.append("- no session works in a target")
    out += ["", "## Dry run", "", "```"] + (doc["dry_run"] or ["(none for this command)"]) + ["```", "",
            "## Undo", "", "`alpaca watchdog undo %s`" % doc["id"], "",
            "Keeps refs under refs/watchdog/%s/, a wip snapshot of uncommitted and untracked files (ignored files are not captured) "
            "and a record snapshot." % doc["id"], "", "## What would make it safe", ""]
    if doc["verdict"] == "safe":
        out.append("- It is safe as it stands. Run the undo command before the owner signs.")
    else:
        for r in doc["reasons"]:
            out.append("- %s" % r["fix"])
    out.append("")
    return "\n".join(out)
