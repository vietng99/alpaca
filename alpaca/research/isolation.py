"""Clone, trees and worktrees of a research run: the stripped clone, the champion checkout, attempt
worktrees with a private object store, the ignored-output purge and post-eval check, diff capture,
rebuild, the champion merge, reclaim and sweep. Pure of the record: every function returns a
dataclass of types.py (or plain values) and the lifecycle step that called it records the event.

Design: design/research-engine/architecture.md section 3.1 (the API and each function's contract),
section 5.3 (the clone, the champion checkout and the attempt worktrees) and section 10.5 (worktree
reclamation); FR-004, FR-013, FR-014, FR-017, FR-018 and FR-021 of design/research-engine/spec.md.

Three rules hold for every git call this module makes:

  * No engine tree is a registered git worktree. The engine never runs `git worktree`: it cuts
    every tree with `git read-tree -u --reset` from its own host git folder, and a tree an agent
    runs git in gets an admin folder the engine makes outside the clone's `.git`. So the clone's
    `.git/worktrees/` stays an empty folder, and no attempt's admin folder can make the cut of
    another tree fail.
  * Host git against a tree reads no file an agent can write. git_env pins GIT_DIR to the
    engine's host git folder, GIT_COMMON_DIR to the clone's `.git`, GIT_WORK_TREE to the recorded
    tree path, the index to the engine's own and the object store to the engine's own; every call
    also passes GIT_HARDEN. The clone's common config is the engine's alone and never sets
    `extensions.worktreeConfig` or an include, which build_clone and open_clone check.
  * Host git never waits on a file an agent planted. Before any git call against a tree an agent
    could write, a walk that runs no git (_hazards) finds every `.git` below the root and every
    `.gitignore`, `.gitattributes` or `.gitmodules` that is not a regular file (a FIFO would
    block git's open for good), or any entry whose path in the tree is longer than _MAX_REL
    (git cannot open it and skips it with a warning, so it would stay outside the tracked diff);
    the caller refuses, removes or leaves it out. Every git call also runs under a time limit
    (_GIT_TIMEOUT_S) that kills it and raises IsolationError.

Every walk of a tree an agent could write is a loop over an explicit stack, never Python
recursion, so a folder chain of any depth ends in a result or an IsolationError, never a
RecursionError; capture, commit_prepared, purge_ignored and post_eval turn any OSError or
RecursionError into IsolationError(ISOLATION-CONTAINMENT) (check-adversarial-resume-r1 B2).
"""

from __future__ import annotations

import datetime
import functools
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time

from alpaca import barrier
from alpaca.gates import git_containment, leak_audit
from alpaca.gates import verdict as vc
from alpaca.research import paths
from alpaca.research.errors import IsolationError
from alpaca.research.types import (CloneInfo, Contract, DiffCapture, PurgeResult, WorktreeInfo)

GIT_HARDEN = ("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
              "-c", "core.fsmonitor=false", "-c", "core.pager=cat", "-c", "core.editor=false",
              "-c", "core.sshCommand=false", "-c", "core.alternateRefsCommand=",
              "-c", "core.fsync=none", "-c", "merge.tool=",
              "-c", "uploadpack.packObjectsHook=", "-c", "protocol.ext.allow=never",
              "-c", "core.excludesFile=/dev/null", "-c", "core.attributesFile=/dev/null",
              "-c", "diff.ignoreSubmodules=all")
# A last layer only, and not a complete list (architecture.md section 3.1): the guarantees are
# git_env's engine-owned GIT_DIR and a common config with no extensions.worktreeConfig or include
# key. No `diff.external=`: capture uses `git diff-tree ... --no-ext-diff --no-textconv`.

# Pins this module adds after GIT_HARDEN on its own calls: no automatic gc or maintenance runs a
# child process in the middle of an engine call, and tag signing is off as commit signing is.
_EXTRA = ("-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "tag.gpgsign=false",
          "-c", "tag.forceSignAnnotated=false")

_AUTHOR = ("Alpaca Research", "research@localhost")
_TASK_ID = re.compile(r"^t-[0-9]+$")
_HOME_TASK = re.compile(r"^(?:vf-)?(t-[0-9]+)-")
_GITLINK = "160000"
# keys of a common config that would let git read a config file an agent can write
_CONFIG_REFUSED = ("extensions.worktreeconfig", "include.", "includeif.")
# the config the engine writes into every clone it builds: signing off for every git, the
# engine's and an agent's alike (FR-013, gap map F03), and a plain byte-for-byte checkout
_CLONE_CONFIG = (("commit.gpgsign", "false"), ("tag.gpgsign", "false"),
                 ("tag.forceSignAnnotated", "false"),
                 ("core.autocrlf", "false"), ("core.fileMode", "true"))
_GIT_FAILED = "ISOLATION-CONTAINMENT"   # a git call that failed: the tree could not be contained
# the longest one engine git call may run, in seconds; past it the call's whole process group is
# killed and IsolationError(ISOLATION-CONTAINMENT) raised, so a git blocked on a file an agent
# planted (a FIFO it opens) ends the step instead of the run (check-adversarial-r3 B1)
_GIT_TIMEOUT_S = 600
# the longest path, in bytes and relative to the tree root, an entry of an agent-written tree may
# have. git opens a folder by its path relative to the tree, so past PATH_MAX (4096) it cannot
# open it and lists nothing below it; a file planted there is outside the tracked diff. The walk
# before git (_hazards) treats a longer path as a hazard well before that limit, which also keeps
# every absolute path the walks build under PATH_MAX (check-adversarial-resume-r1 B2).
_MAX_REL = 2048


# ---------------------------------------------------------------- environment and git calls

def _base_env() -> dict:
    """The runner's environment with every GIT_* variable removed, then no system or global git
    config and no terminal prompt."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def git_env(clone: CloneInfo, *, worktree: WorktreeInfo | None = None,
            index: str | None = None) -> dict:
    """The environment every engine git call uses (architecture.md section 3.1).

    With a worktree: GIT_DIR is the engine's host git folder (worktree.host_git), GIT_COMMON_DIR
    the clone's `.git`, GIT_WORK_TREE the recorded tree path, GIT_INDEX_FILE `index` or
    worktree.index (IsolationError when both are None), GIT_OBJECT_DIRECTORY the host store
    `<host_git>/objects` and the clone's objects its alternate. So neither the tree's `.git` file,
    nor the admin folder's HEAD, index, `commondir` or config, nor the agent's private store is
    ever read. With no worktree (a clone call), GIT_DIR is the clone's `.git`, and GIT_INDEX_FILE
    is `index` when given. Every call also passes GIT_HARDEN on its command line."""
    env = _base_env()
    common = os.path.join(clone.path, ".git")
    if worktree is None:
        env["GIT_DIR"] = common
        if index is not None:
            env["GIT_INDEX_FILE"] = index
        return env
    idx = index if index is not None else worktree.index
    if idx is None:
        raise IsolationError(_GIT_FAILED, "no engine index for %s" % worktree.label)
    if not worktree.host_git:
        raise IsolationError(_GIT_FAILED, "no host git folder for %s" % worktree.label)
    env.update({
        "GIT_DIR": worktree.host_git,
        "GIT_COMMON_DIR": common,
        "GIT_WORK_TREE": worktree.path,
        "GIT_INDEX_FILE": idx,
        "GIT_OBJECT_DIRECTORY": os.path.join(worktree.host_git, "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": os.path.join(common, "objects"),
    })
    return env


def _run(argv, *, env: dict, cwd: str | None = None, stdin: bytes | None = None,
         stdout=None, timeout: float | None = None, name: str = "") -> subprocess.CompletedProcess:
    """Run argv in its own process group with a time limit (_GIT_TIMEOUT_S when None); past it,
    kill the whole group (git and any child it started) and raise IsolationError
    ISOLATION-CONTAINMENT. The pipes are closed, not drained, after a kill, so a child that left
    the group and still holds them cannot block the engine either."""
    limit = _GIT_TIMEOUT_S if timeout is None else timeout
    proc = subprocess.Popen(argv, env=env, cwd=cwd,
                            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                            stdout=stdout if stdout is not None else subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, err = proc.communicate(input=stdin, timeout=limit)
    except BaseException as exc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        if isinstance(exc, subprocess.TimeoutExpired):
            raise IsolationError(_GIT_FAILED, "git %s timed out after %s s" % (name, limit))
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _git(args, *, env: dict, cwd: str | None = None, stdin: bytes | None = None,
         stdout=None, code: str = _GIT_FAILED, ok=(0,), timeout: float | None = None) -> bytes:
    """Run one hardened git call; raise IsolationError(code) on an exit code outside `ok`, and
    IsolationError(ISOLATION-CONTAINMENT) when it runs past _GIT_TIMEOUT_S (or `timeout`).

    The default code is ISOLATION-CONTAINMENT for every failed call, an infrastructure failure
    (a full disk, a missing git) included: the defined set of section 3.1 has no separate git
    code, so the research-worktree event records such a failure as a containment refusal, and
    the message names the git verb and its last error line to tell the two apart."""
    argv = ["git", *GIT_HARDEN, *_EXTRA, *args]
    res = _run(argv, env=env, cwd=cwd, stdin=stdin, stdout=stdout, timeout=timeout,
               name=args[0] if args else "")
    if res.returncode not in ok:
        err = (res.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise IsolationError(code, "git %s exited %d: %s"
                             % (args[0] if args else "", res.returncode, (err[-1] if err else "")[:300]))
    return res.stdout if res.stdout is not None else b""


def _contained(fn):
    """Wrap a call that walks or runs git on a tree an agent could write: any OSError (a folder
    the agent made unremovable, a path the kernel refuses) or RecursionError becomes
    IsolationError(ISOLATION-CONTAINMENT), the one error section 3.1 defines for this module, so the
    lifecycle always gets a defined refusal (check-adversarial-resume-r1 B2 and B3)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (OSError, RecursionError) as exc:
            raise IsolationError(_GIT_FAILED, "%s: %s: %s" % (fn.__name__, type(exc).__name__,
                                                              str(exc)[:300])) from exc
    return wrapper


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape").strip()


def _zlist(raw: bytes) -> list:
    """The entries of a NUL-terminated git listing, as str (undecodable bytes kept)."""
    return [p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p]


def _git_date(stamp: str) -> str:
    """A git date for `stamp`: paths.STAMP_FORMAT (UTC) or an ISO 8601 time with an offset, as
    `<epoch seconds> <+hhmm>`, so the same stamp always gives the same commit."""
    try:
        when = datetime.datetime.strptime(stamp, paths.STAMP_FORMAT).replace(
            tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        try:
            when = datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("not a stamp: %r" % (stamp,))
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
    offset = int(when.utcoffset().total_seconds()) // 60
    sign = "+" if offset >= 0 else "-"
    return "%d %s%02d%02d" % (int(when.timestamp()), sign, abs(offset) // 60, abs(offset) % 60)


def _commit_env(env: dict, stamp: str) -> dict:
    date = _git_date(stamp)
    out = dict(env)
    out.update({"GIT_AUTHOR_NAME": _AUTHOR[0], "GIT_AUTHOR_EMAIL": _AUTHOR[1],
                "GIT_COMMITTER_NAME": _AUTHOR[0], "GIT_COMMITTER_EMAIL": _AUTHOR[1],
                "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    return out


def _sha256_file(path: str) -> tuple:
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


# ---------------------------------------------------------------- no-follow removal

def _rmtree(path: str) -> bool:
    """Remove path without following a symlink: a link or a file is unlinked, a folder removed
    with shutil.rmtree (which never descends into a link), after making each folder writable
    again when an agent took the write bit away. True when something was there."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(path)
        return True

    # an agent may have taken the read or write bit off a folder it made: give every real folder
    # back to its owner first (top down, links never followed), then remove the tree
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    for folder, dirs, _files in os.walk(path, topdown=True, followlinks=False):
        for name in dirs:
            full = os.path.join(folder, name)
            try:
                if stat.S_ISDIR(os.lstat(full).st_mode):
                    os.chmod(full, 0o700)
            except OSError:
                pass
    shutil.rmtree(path)
    return True


def _inside_no_link(root: str, rel: str, create: bool = False) -> str | None:
    """The absolute path of rel under root when no part of it but the last is a symlink and it
    stays under root; else None. With create, a missing folder on the way is made (never through a
    link), for a tree that does not hold the folder of a file laid into it."""
    parts = [p for p in rel.replace(os.sep, "/").split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        return None
    here = root
    for part in parts[:-1]:
        here = os.path.join(here, part)
        try:
            mode = os.lstat(here).st_mode
        except FileNotFoundError:
            if not create:
                return None
            try:
                os.mkdir(here)
            except OSError:
                return None
            continue
        except OSError:
            return None
        if not stat.S_ISDIR(mode):
            return None
    return os.path.join(here, parts[-1])


# ---------------------------------------------------------------- the clone's config checks

def _check_clone(path: str) -> None:
    """The common config holds no key that makes git read a config file an agent can write
    (ISOLATION-CONFIG), and `.git/worktrees/` is an empty real folder (ISOLATION-REGISTRY)."""
    dotgit = os.path.join(path, ".git")
    cfg = os.path.join(dotgit, "config")
    try:
        if not stat.S_ISREG(os.lstat(cfg).st_mode):
            raise IsolationError("ISOLATION-CONFIG", "the clone config is not a plain file")
    except FileNotFoundError:
        raise IsolationError("ISOLATION-CONFIG", "the clone has no config")
    names = _git(["config", "--file", cfg, "--list", "--name-only", "--no-includes"],
                 env=_base_env(), code="ISOLATION-CONFIG")
    for name in _text(names).splitlines():
        low = name.strip().lower()
        if any(low == k or (k.endswith(".") and low.startswith(k)) for k in _CONFIG_REFUSED):
            raise IsolationError("ISOLATION-CONFIG", "the clone config sets %s" % low)
    reg = os.path.join(dotgit, "worktrees")
    try:
        st = os.lstat(reg)
    except FileNotFoundError:
        raise IsolationError("ISOLATION-REGISTRY", "the clone has no .git/worktrees folder")
    if not stat.S_ISDIR(st.st_mode):
        raise IsolationError("ISOLATION-REGISTRY", ".git/worktrees is not a folder")
    if os.listdir(reg):
        raise IsolationError("ISOLATION-REGISTRY", ".git/worktrees holds a registered worktree")


# ---------------------------------------------------------------- building a clone

def _manifest_memory(src: str, commit: str, env: dict) -> list:
    """The [memory] entries of the target's ALPACA-MANIFEST at commit, or [] when it has none.
    It reads the section with the rules of alpaca/manifest.py `classes` (a `[name]` header line,
    `#` comments, blank lines skipped), but from the blob at commit: `classes` reads the file on
    disk, and the clone must follow the manifest of the base commit, not of the working tree."""
    res = _run(["git", *GIT_HARDEN, "-C", src, "cat-file", "blob", "%s:ALPACA-MANIFEST" % commit],
               env=env, name="cat-file")
    if res.returncode != 0:
        return []
    out, section = [], None
    for line in res.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section == "memory":
            out.append(line)
    return out


def _anchored(path: str, entries) -> bool:
    """True when path is one of entries, or under one, read from the root (case kept)."""
    for entry in entries:
        e = entry.strip().strip("/")
        if e and (path == e or path.startswith(e + "/")):
            return True
    return False


def _term_list(names, terms) -> leak_audit.TermList:
    """The explicit term list of a clone's proof: each held-out file name as a regex that matches
    that path name whole (so the runbook, which names the held-out rules and is in the clone,
    does not trip it), each fix term as a substring (a `re:` line when shorter than the barrier's
    minimum). Every shape rule is cleared: only the protected-path and term lanes decide."""
    tl = leak_audit.TermList()
    seen = set()
    for term in terms:
        term = str(term).strip()
        if not term:
            continue
        key = leak_audit.defang(term).casefold()
        if key in seen:
            continue
        seen.add(key)
        if len(term) >= leak_audit.MIN_TERM_LEN:
            tl.terms.append((term, key))
        else:
            pat = re.escape(term)
            tl.regexes.append(("fix term", re.compile(pat, re.I), re.compile(pat.encode(), re.I)))
    for name in names:
        pat = "^" + re.escape(name) + "$"
        tl.regexes.append(("held-out name", re.compile(pat, re.I),
                           re.compile(pat.encode("utf-8", "surrogateescape"), re.I)))
    tl.shape_allow = [(re.compile(".*", re.S), "a research clone: the target's own content", 1)]
    tl.digest = hashlib.sha256(json.dumps([sorted(names), sorted(seen)]).encode()).hexdigest()
    return tl


def _policy_path(clone_path: str) -> str:
    """Where a clone's barrier policy lives: beside the clone, outside its `.git`, which the
    proposer, implementer and reproducer writer jails mount; fix terms must never be there."""
    return clone_path.rstrip("/") + ".barrier.json"


def _prove(clone_path: str, commit: str, policy: dict) -> None:
    """barrier.scan over commit with an explicit cfg and term_list; ISOLATION-BARRIER on a hit."""
    env = _base_env()
    blobs = set()
    listing = _git(["ls-tree", "-r", "-z", "--full-tree", commit],
                   env=dict(env, GIT_DIR=os.path.join(clone_path, ".git")))
    for entry in listing.split(b"\0"):
        if entry:
            meta = entry.split(b"\t", 1)[0].split()
            if len(meta) == 3 and meta[1] == b"blob":
                blobs.add(meta[2].decode("ascii"))
    cfg = {"tier": "private", "barrier": {
        "protected_paths": list(policy["protected"]),
        "allow_blobs": ["%s  # a research clone: the target's own file" % b for b in sorted(blobs)]}}
    tl = _term_list(policy["names"], policy["terms"])
    report = barrier.scan(clone_path, [commit], protected=list(policy["protected"]),
                          term_list=tl, cfg=cfg)
    if report.verdict != vc.PASS:
        raise IsolationError("ISOLATION-BARRIER", ",".join(report.reasons) or "barrier refused")


def _source_entries(src: str, commit: str, env: dict) -> list:
    """(mode, sha, path) of every tracked entry of commit, gitlinks included."""
    raw = _git(["-C", src, "ls-tree", "-r", "-z", "--full-tree", commit], env=env)
    out = []
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        mode, _typ, sha = meta.decode("ascii").split()
        out.append((mode, sha, path.decode("utf-8", "surrogateescape")))
    return out


def _build(src: str, commit: str, dest: str, *, keep_entry, stamp: str, branches: tuple,
           message: str) -> str:
    """Build a repository at dest holding one root commit: the entries of src at commit that
    keep_entry admits, gitlinks dropped, dated stamp by Alpaca Research, with signing off, no
    tags, no remotes and no hooks path; then point each branch at it. The objects are copied from
    src with pack-objects, so no file of the target is written to disk on the way and no
    `.gitattributes` export rule changes a file. Built in a sibling folder and renamed into place,
    so a kill leaves no half-built clone at dest. Returns the root commit."""
    if os.path.lexists(dest):
        raise IsolationError("ISOLATION-PATH-TAKEN", "a clone already exists at its path")
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    src_env = dict(_base_env(), GIT_NO_REPLACE_OBJECTS="1")
    entries = [e for e in _source_entries(src, commit, src_env)
               if e[0] != _GITLINK and keep_entry(e[2])]
    stage = tempfile.mkdtemp(prefix=".build-", dir=parent)
    try:
        _git(["init", "-q", "--template=", "-b", "main", stage], env=_base_env())
        dotgit = os.path.join(stage, ".git")
        for key, value in _CLONE_CONFIG:
            _git(["config", "--file", os.path.join(dotgit, "config"), key, value], env=_base_env())
        os.makedirs(os.path.join(dotgit, "worktrees"), exist_ok=True)
        env = dict(_base_env(), GIT_DIR=dotgit)
        shas = sorted({sha for _m, sha, _p in entries})
        if shas:
            pack = subprocess.Popen(["git", *GIT_HARDEN, "-C", src, "pack-objects", "--stdout", "-q"],
                                    env=src_env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
            unpack = subprocess.Popen(["git", *GIT_HARDEN, *_EXTRA, "index-pack", "--stdin"],
                                      env=env, stdin=pack.stdout, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE)
            pack.stdout.close()
            pack.stdin.write(("\n".join(shas) + "\n").encode("ascii"))
            pack.stdin.close()
            try:
                err = unpack.communicate(timeout=_GIT_TIMEOUT_S)[1] or b""
                pack.wait(timeout=_GIT_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                for proc in (pack, unpack):
                    proc.kill()
                    proc.wait()
                raise IsolationError(_GIT_FAILED, "copying the base objects timed out after %s s"
                                     % _GIT_TIMEOUT_S)
            if pack.returncode != 0 or unpack.returncode != 0:
                raise IsolationError(_GIT_FAILED, "copying the base objects failed: %s"
                                     % err.decode("utf-8", "replace").strip()[:300])
        index = os.path.join(dotgit, "build-index")
        info = b"".join(b"%s %s\t%s\0" % (m.encode(), s.encode(), p.encode("utf-8", "surrogateescape"))
                        for m, s, p in entries)
        _git(["update-index", "-z", "--index-info"], env=dict(env, GIT_INDEX_FILE=index), stdin=info)
        if not entries:
            _git(["read-tree", "--empty"], env=dict(env, GIT_INDEX_FILE=index))
        tree = _text(_git(["write-tree"], env=dict(env, GIT_INDEX_FILE=index)))
        if os.path.exists(index):
            os.unlink(index)
        root_commit = _text(_git(["commit-tree", tree, "-m", message], env=_commit_env(env, stamp)))
        for branch in branches:
            _git(["update-ref", "refs/heads/%s" % branch, root_commit], env=env)
        os.rename(stage, dest)
    except BaseException:
        _rmtree(stage)
        raise
    return root_commit


def _resolve(src: str, ref: str) -> str:
    return _text(_git(["-C", src, "rev-parse", "--verify", "--end-of-options", "%s^{commit}" % ref],
                      env=dict(_base_env(), GIT_NO_REPLACE_OBJECTS="1")))


def _clone_policy(root: str, contract: Contract, src: str, commit: str, fix_terms=()) -> tuple:
    """(keep_entry, policy) for a clone of src at commit: what leaves (the hidden and held-out
    paths as barrier.path_is_protected matches them, `.alpaca/`, the reproducer store and the
    target's memory-class paths from the root) and what the proof checks."""
    rules = [str(r) for r in tuple(contract.hidden or ()) + tuple(contract.holdout_paths or ())]
    protected = list(rules) + [".alpaca/"]
    anchored = list(_manifest_memory(src, commit, _base_env()))
    store = os.path.relpath(paths.reproducer_store(root), src)
    if store != "." and not store.startswith(".." + os.sep) and store != "..":
        store = store.replace(os.sep, "/")
        anchored.append(store + "/")
        protected.append(store + "/")
    names = []

    def keep_entry(path: str) -> bool:
        if barrier.path_is_protected(path, rules):
            names.append(path)
            return False
        if barrier.path_is_protected(path, [".alpaca/"]) or _anchored(path, anchored):
            return False
        return True

    policy = {"protected": protected, "names": names, "terms": [str(t) for t in fix_terms]}
    return keep_entry, policy


def _write_policy(clone_path: str, policy: dict) -> None:
    body = {"protected": list(policy["protected"]), "names": sorted(set(policy["names"])),
            "terms": list(policy["terms"])}
    tmp = _policy_path(clone_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(body, fh, sort_keys=True)
    os.replace(tmp, _policy_path(clone_path))


def _read_policy(clone_path: str) -> dict:
    try:
        with open(_policy_path(clone_path), encoding="utf-8") as fh:
            body = json.load(fh)
    except (OSError, ValueError):
        raise IsolationError("ISOLATION-BARRIER", "the clone's barrier policy is missing")
    return {"protected": list(body.get("protected") or ()), "names": list(body.get("names") or ()),
            "terms": list(body.get("terms") or ())}


def _repo(root: str, contract: Contract) -> str:
    rp = contract.repo_path or "."
    return os.path.normpath(rp if os.path.isabs(rp) else os.path.join(root, rp))


def build_clone(root: str, run: str, contract: Contract, *, line: str = "champion",
                base_ref: str | None = None, stamp: str) -> CloneInfo:
    """Stripped clone of contract.repo_path at base_ref (default contract.repo_commit): the tracked
    files minus contract.hidden, contract.holdout_paths, the reproducer store, `.alpaca/` and the
    target's memory-class paths; one root commit dated stamp by "Alpaca Research
    <research@localhost>"; signing off, no tags, no remotes, no hooks path; `main` and
    research_branch(run, line) at that commit; `.git/worktrees/` an empty folder. Proved with
    barrier.scan over its commit with an explicit cfg and term_list (held-out names as terms).
    Deterministic: the same inputs give the same base commit. Raises IsolationError."""
    src = _repo(root, contract)
    commit = _resolve(src, base_ref or contract.repo_commit)
    return _make_clone(root, run, contract, src, commit, line=line, stamp=stamp, fix_terms=())


def _make_clone(root, run, contract, src, commit, *, line, stamp, fix_terms) -> CloneInfo:
    dest = paths.clone_dir(root, run, line)
    branch = paths.research_branch(run, line)
    keep_entry, policy = _clone_policy(root, contract, src, commit, fix_terms)
    base = _build(src, commit, dest, keep_entry=keep_entry, stamp=stamp,
                  branches=("main", branch), message="research base")
    try:
        _check_clone(dest)
        _write_policy(dest, policy)
        _prove(dest, base, policy)
    except BaseException:
        _rmtree(dest)
        _rmtree(_policy_path(dest))
        raise
    return CloneInfo(path=dest, line=line, branch=branch, base_commit=base, head=base)


def build_head_clone(root: str, *, head: str, path: str, stamp: str) -> CloneInfo:
    """For a verification outside a run: the tracked files of head in the project at root minus
    `.alpaca`, one root commit dated stamp at path, with the config and registry checks of
    build_clone; line "verify", branch `main` (no research branch)."""
    commit = _resolve(root, head)
    dest = os.path.abspath(path)

    def keep_entry(p: str) -> bool:
        return not (p == ".alpaca" or p.startswith(".alpaca/"))

    base = _build(root, commit, dest, keep_entry=keep_entry, stamp=stamp, branches=("main",),
                  message="verification base")
    try:
        _check_clone(dest)
    except BaseException:
        _rmtree(dest)
        raise
    return CloneInfo(path=dest, line="verify", branch="main", base_commit=base, head=base)


def open_clone(root: str, run: str, *, line: str = "champion") -> CloneInfo | None:
    """The CloneInfo of paths.clone_dir(root, run, line) as it is on disk (its research branch head
    and root commit, read with git_env(clone)), after the config and registry checks of
    build_clone; None when the clone is missing."""
    dest = paths.clone_dir(root, run, line)
    if not os.path.lexists(dest):
        return None
    if os.path.islink(dest) or not os.path.isdir(os.path.join(dest, ".git")):
        raise IsolationError("ISOLATION-CONFIG", "the clone path is not a clone")
    _check_clone(dest)
    branch = paths.research_branch(run, line)
    probe = CloneInfo(path=dest, line=line, branch=branch, base_commit="", head="")
    env = git_env(probe)
    head = _text(_git(["rev-parse", "--verify", "refs/heads/%s" % branch], env=env))
    roots = _text(_git(["rev-list", "--max-parents=0", head], env=env)).split()
    if len(roots) != 1:
        raise IsolationError("ISOLATION-BARRIER", "the research branch has %d root commits"
                             % len(roots))
    return CloneInfo(path=dest, line=line, branch=branch, base_commit=roots[0], head=head)


def build_repair_repo(root: str, run: str, contract: Contract, *, repair_task: str,
                      base_commit: str, fix_terms: tuple[str, ...], stamp: str) -> CloneInfo:
    """As build_clone for one repair task: the base commit's tree only, so no commit after the base
    exists (FR-014); fix_terms (fix SHAs, fix-only paths) join the barrier term list; line =
    repair_task, which must be a task id, so the repository's path and its branch
    research/<run>/<repair task> carry no bug or fix identifier."""
    if not isinstance(repair_task, str) or not _TASK_ID.fullmatch(repair_task):
        raise ValueError("a repair line is a task id: %r" % (repair_task,))
    src = _repo(root, contract)
    commit = _resolve(src, base_commit)
    return _make_clone(root, run, contract, src, commit, line=repair_task, stamp=stamp,
                       fix_terms=tuple(fix_terms or ()))


# ---------------------------------------------------------------- trees

def tree_info(path: str, *, commit: str, label: str, admin: bool,
              read_only: bool = False) -> WorktreeInfo:
    """Pure: the WorktreeInfo fresh_tree returns for a tree it cut at path, from its naming rule
    alone (host_git `<path>.git-host`, index `<host_git>/index`, admin_dir `<path>.git-admin` when
    admin, else None; no private object store)."""
    host = path.rstrip("/") + ".git-host"
    return WorktreeInfo(path=path, label=label, commit=commit,
                        admin_dir=(path.rstrip("/") + ".git-admin") if admin else None,
                        private_objects=None, index=os.path.join(host, "index"), host_git=host,
                        read_only=read_only)


def _cut(clone: CloneInfo, wt: WorktreeInfo, commit: str) -> None:
    """Make the engine's host git folder (a HEAD holding commit, refs/, objects/ with the clone's
    objects as its one alternate) and fill wt.path and the engine index with
    `git read-tree -u --reset <commit>`."""
    host = wt.host_git
    os.makedirs(os.path.join(host, "objects", "info"), exist_ok=True)
    os.makedirs(os.path.join(host, "refs"), exist_ok=True)
    with open(os.path.join(host, "objects", "info", "alternates"), "w", encoding="utf-8") as fh:
        fh.write(os.path.join(clone.path, ".git", "objects") + "\n")
    with open(os.path.join(host, "HEAD"), "w", encoding="ascii") as fh:
        fh.write(commit + "\n")
    os.makedirs(wt.path, exist_ok=True)
    _git(["read-tree", "-u", "--reset", commit], env=git_env(clone, worktree=wt), cwd=wt.path)


def _make_admin(clone: CloneInfo, wt: WorktreeInfo, commit: str) -> None:
    """The tree's admin folder (HEAD, `commondir`, an index read from commit) and the tree's
    `.git` file naming it, so git run inside the tree finds it. The engine records admin_dir now
    and never reads it back from the `.git` file."""
    admin = wt.admin_dir
    os.makedirs(admin, exist_ok=True)
    with open(os.path.join(admin, "HEAD"), "w", encoding="ascii") as fh:
        fh.write(commit + "\n")
    with open(os.path.join(admin, "commondir"), "w", encoding="utf-8") as fh:
        fh.write(os.path.join(clone.path, ".git") + "\n")
    index = os.path.join(admin, "index")
    env = git_env(clone, worktree=wt, index=index)
    _git(["read-tree", commit], env=env, cwd=wt.path)
    _git(["update-index", "-q", "--refresh"], env=env, cwd=wt.path, ok=(0, 1))
    with open(os.path.join(wt.path, ".git"), "w", encoding="utf-8") as fh:
        fh.write("gitdir: %s\n" % admin)


def _overlay(tree: str, overlays, make_parents: bool = False) -> None:
    """Copy each (source, path in the tree) of overlays into tree, never through a link. A folder
    on the way that the tree lacks is made only with make_parents (holdout.run_holdout lays the
    held-out files, which the clone strips with their folders, into its own tree this way)."""
    for source, rel in overlays or ():
        dest = _inside_no_link(tree, str(rel), create=make_parents)
        if dest is None:
            raise IsolationError("ISOLATION-CONTAINMENT", "an overlay leaves the tree")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.lexists(dest):
            _rmtree(dest)
        if os.path.isdir(source) and not os.path.islink(source):
            shutil.copytree(source, dest, symlinks=True)
        else:
            shutil.copy2(source, dest, follow_symlinks=False)


def fresh_tree(root: str, clone: CloneInfo, *, commit: str, path: str,
               overlays: tuple = (), admin: bool = False) -> WorktreeInfo:
    """A clean tree of clone at commit under path, cut without `git worktree add` from the
    engine's host git folder `<path>.git-host`; each (frozen copy, path inside the tree) of
    overlays is copied over it. With admin True it also gets its admin folder
    `<path>.git-admin` and a `.git` file naming it. IsolationError ISOLATION-PATH-TAKEN when any of
    its folders exists. Destroyed with destroy_worktree."""
    path = os.path.abspath(path)
    wt = tree_info(path, commit=commit, label=os.path.basename(path), admin=admin)
    for p in (wt.path, wt.host_git, wt.admin_dir):
        if p and os.path.lexists(p):
            raise IsolationError("ISOLATION-PATH-TAKEN", "a tree folder already exists")
    try:
        _cut(clone, wt, commit)
        _overlay(wt.path, overlays)
        if admin:
            _make_admin(clone, wt, commit)
    except BaseException:
        for p in (wt.path, wt.host_git, wt.admin_dir):
            if p:
                _rmtree(p)
        raise
    return wt


def _attempt_info(root: str, run: str, task: str, commit: str, cut: int = 1) -> WorktreeInfo:
    git_dir = paths.cut_git_dir(root, run, task, cut)
    host = os.path.join(git_dir, "host")
    return WorktreeInfo(path=paths.worktree_dir(root, run, task, cut), label=task, commit=commit,
                        admin_dir=os.path.join(git_dir, "admin"),
                        private_objects=os.path.join(paths.attempt_dir(root, run, task), "git",
                                                     "objects"),
                        index=os.path.join(host, "index"), host_git=host)


def create_worktree(root: str, run: str, task: str, clone: CloneInfo, *,
                    commit: str, cut: int = 1) -> WorktreeInfo:
    """Cut `cut` of the attempt worktree at commit under paths.worktree_dir(root, run, task, cut),
    cut as fresh_tree cuts it, with its git folders under the attempt dir: the host git folder
    `host/` (no jail mounts it) and the admin folder `admin/` in paths.cut_git_dir(root, run, task,
    cut), and the agent's private object store `git/objects/`, shared by every cut.
    git_containment.scan must find nothing (ISOLATION-CONTAINMENT). Two attempts never share a
    path, and two cuts of one attempt never do either: the path is the attempt task id and the cut
    number, both from the record (H14: no caller text; in-session-runtime.md section 28.1). A real
    folder left at this cut's own path by a killed create is this cut's and is cut again; anything
    else there (a link, a file) raises ISOLATION-PATH-TAKEN. task must be a record task id
    (t-<n>), so no caller can give a repair attempt a path that names its bug or fix (gap map
    H14); ValueError otherwise, and for a cut that is not an int >= 1."""
    if not isinstance(task, str) or not _TASK_ID.fullmatch(task):
        raise ValueError("an attempt worktree is named by its task id: %r" % (task,))
    wt = _attempt_info(root, run, task, commit, cut)
    try:
        st = os.lstat(wt.path)
    except FileNotFoundError:
        st = None
    if st is not None and not stat.S_ISDIR(st.st_mode):
        raise IsolationError("ISOLATION-PATH-TAKEN", "the worktree path is not a folder")
    for p in (wt.admin_dir, wt.host_git):
        if os.path.islink(p):
            raise IsolationError("ISOLATION-PATH-TAKEN", "an attempt git folder is a link")
    for p in (wt.path, wt.admin_dir, wt.host_git):
        _rmtree(p)
    os.makedirs(os.path.dirname(wt.path), exist_ok=True)
    os.makedirs(wt.private_objects, exist_ok=True)
    try:
        _cut(clone, wt, commit)
        _make_admin(clone, wt, commit)
        findings = git_containment.scan(wt.path)
        if findings:
            raise IsolationError("ISOLATION-CONTAINMENT",
                                 ",".join(sorted({f[0] for f in findings})))
    except BaseException:
        for p in (wt.path, wt.admin_dir, wt.host_git):
            _rmtree(p)
        raise
    return wt


def champion_checkout(root: str, run: str, round_n: int, clone: CloneInfo, *,
                      commit: str) -> WorktreeInfo:
    """The round's champion of clone.line at paths.champion_dir(root, run, round_n,
    line=clone.line), cut with fresh_tree(..., admin=True), read-only to every proposer.
    Idempotent: an existing checkout at the same commit (its engine HEAD) is returned only when
    it is clean: no nested repository (nested_git), no entry git would block on (_hazards), no
    changed or untracked path against commit (a root `.git` that is not the engine's among them),
    no ignored leftover and no entry git never lists (_unlisted); otherwise it is destroyed and
    cut again. The design's clean rule
    names only the first two; a stale ignored output, folder or FIFO would otherwise reach the
    proposers' view."""
    path = paths.champion_dir(root, run, round_n, line=clone.line)
    label = "r%d-champion" % int(round_n)
    wt = tree_info(path, commit=commit, label=label, admin=True, read_only=True)
    if os.path.lexists(path):
        try:
            with open(os.path.join(wt.host_git, "HEAD"), encoding="ascii") as fh:
                at = fh.read().strip()
        except OSError:
            at = ""
        if at == commit and os.path.isdir(path) and not os.path.islink(path):
            if not any(_compare(clone, wt, commit)):
                return wt
        destroy_worktree(clone, wt)
    else:
        for p in (wt.host_git, wt.admin_dir):
            _rmtree(p)
    made = fresh_tree(root, clone, commit=commit, path=path, admin=True)
    return tree_info(made.path, commit=commit, label=label, admin=True, read_only=True)


def _engine_dotgit(wt: WorktreeInfo) -> bytes | None:
    """The bytes the engine writes to the tree's root `.git`: `gitdir: <admin_dir>` and a newline
    for a tree with an admin folder, else None (no root `.git` at all)."""
    if not wt.admin_dir:
        return None
    return ("gitdir: %s\n" % wt.admin_dir).encode("utf-8", "surrogateescape")


def _root_dotgit_ok(wt: WorktreeInfo) -> bool:
    """Runs no git. True when the tree's root `.git` is what the engine wrote: a regular file
    holding exactly _engine_dotgit(wt) (one byte more is read, so a longer file fails), or
    nothing when that is None. A `.git` folder, a symlink or a file with any other bytes is a
    place the agent could plant data that git never lists, so the eval could read it outside the
    tracked diff."""
    want = _engine_dotgit(wt)
    path = os.path.join(wt.path, ".git")
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return want is None
    except OSError:
        return False
    if want is None or not stat.S_ISREG(st.st_mode):
        return False
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        got = os.read(fd, len(want) + 1)
    finally:
        os.close(fd)
    return got == want


def _restore_root_dotgit(wt: WorktreeInfo) -> None:
    """Runs no git. Remove whatever is at the tree's root `.git` without following a link, then
    write the engine's bytes (_engine_dotgit) as a new file, or leave nothing when there is no
    admin folder."""
    path = os.path.join(wt.path, ".git")
    _rmtree(path)
    want = _engine_dotgit(wt)
    if want is None:
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        os.write(fd, want)
    finally:
        os.close(fd)


def nested_git(wt: WorktreeInfo) -> tuple[str, ...]:
    """Runs no git. The relative path of every entry named `.git` below wt.path (a folder, a file
    or a symlink, at any depth but the root, whose `.git` is the engine's `gitdir:` file), found
    by an os.scandir walk that never follows or descends into a symlink, sorted. The root `.git`
    is never listed here (architecture.md section 3.1): purge_ignored puts it back before the
    eval, and post_eval lists a root `.git` that is not the engine's in PurgeResult.changed."""
    hits = []
    stack = [(wt.path, "")]
    while stack:                               # an explicit stack: no depth limit, no recursion
        folder, rel = stack.pop()
        try:
            with os.scandir(folder) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            name = rel + entry.name
            if entry.name == ".git":
                if rel:
                    hits.append(name)
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append((entry.path, name + "/"))
    return tuple(sorted(hits))


# ---------------------------------------------------------------- purge and post-eval

# the files host git opens by name in every folder it walks or compares (ignore rules, attributes,
# submodule config); each must be a regular file, since opening a FIFO blocks until a writer
# comes, and once the agent's jail has exited none ever does (check-adversarial-r3 B1)
_GIT_READS = (".gitignore", ".gitattributes", ".gitmodules")


def _hazards(wt: WorktreeInfo) -> tuple:
    """Runs no git. (nested, special) of the tree at wt.path, the entries host git would read that
    an agent could turn into a wait with no end. nested: every `.git` entry below the root, as
    nested_git lists them (git reads a nested `.git`'s gitdir and HEAD, files the agent wrote, and
    a FIFO HEAD blocks it). special: every `.gitignore`, `.gitattributes` or `.gitmodules` at any
    depth that is not a regular file (a FIFO, a socket, a device, a folder or a symlink), any
    entry whose path relative to the root is longer than _MAX_REL bytes (git cannot open what lies
    there, so a file planted below it is outside the tracked diff; the walk does not go below it),
    and any folder the walk cannot read. A folder that lacks an owner bit gets its owner's read,
    write and execute bits back first, as _scan and _rmtree do. One os.scandir walk over an
    explicit stack that never follows a link; the root `.git` is skipped (the pinned GIT_DIR never
    reads it, and git skips a `.git` entry by name). capture, purge_ignored, post_eval and the
    champion's clean check call it before any host git call (check-adversarial-r3 B1 and m1,
    check-adversarial-resume-r1 B2 and B3)."""
    nested, special = [], []
    stack = [(wt.path, "")]
    while stack:                               # an explicit stack: no depth limit, no recursion
        folder, rel = stack.pop()
        try:
            mode = os.lstat(folder).st_mode
            if mode & 0o700 != 0o700:
                os.chmod(folder, stat.S_IMODE(mode) | 0o700)
            with os.scandir(folder) as it:
                entries = list(it)
        except OSError:
            special.append(rel.rstrip("/") or ".")
            continue
        for entry in entries:
            name = rel + entry.name
            if len(os.fsencode(name)) > _MAX_REL:
                special.append(name)
                continue
            if entry.name == ".git":
                if rel:
                    nested.append(name)
                continue
            if entry.name in _GIT_READS:
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    special.append(name)
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append((entry.path, name + "/"))
    return tuple(sorted(nested)), tuple(sorted(special))


def _short(rel: str) -> str:
    """rel cut to 200 characters for an error message (a hazard path may be 2 KB long)."""
    return rel if len(rel) <= 200 else rel[:200] + "..."


def _top_most(folders) -> tuple:
    """The folders of the set that lie below no other folder of it, sorted."""
    out = []
    for folder in sorted(set(folders)):
        if not any(folder.startswith(top + "/") for top in out):
            out.append(folder)
    return tuple(out)



def _admitted(path: str, rules) -> str | None:
    """runbook.path_admitted(path, rules), looked up when called: the strict matcher of
    runbook-format-3.md section 14.1 (case kept, every rule read from the root), which reads the
    keep and outputs lists (t-096 adds it to alpaca/runbook.py)."""
    if not rules:
        return None
    from alpaca import runbook
    return runbook.path_admitted(path, tuple(rules))


def _fresh_index(clone: CloneInfo, wt: WorktreeInfo, commit: str) -> dict:
    """Remove the engine index and read it fresh from commit; return the git environment."""
    env = git_env(clone, worktree=wt)
    try:
        os.unlink(env["GIT_INDEX_FILE"])
    except FileNotFoundError:
        pass
    _git(["read-tree", commit], env=env, cwd=wt.path)
    return env


def _ignored(env: dict, cwd: str) -> list:
    return _zlist(_git(["ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
                       env=env, cwd=cwd))


def _tracked(env: dict, cwd: str, head: str) -> tuple:
    """(entries, folders, gitlinks) of head: entries the (mode, path) of every tracked entry,
    folders every folder that holds a tracked path (the parents of every entry), gitlinks the
    paths of mode 160000 entries."""
    raw = _git(["ls-tree", "-r", "-z", "--full-tree", head], env=env, cwd=cwd)
    entries, folders, links = [], set(), set()
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        mode = meta.split()[0].decode("ascii")
        rel = path.decode("utf-8", "surrogateescape")
        entries.append((mode, rel))
        if mode == _GITLINK:
            links.add(rel)
        parts = rel.split("/")
        for i in range(1, len(parts)):
            folders.add("/".join(parts[:i]))
    return entries, folders, links


# the extended attributes the reset drops besides user.*: a POSIX ACL the file owner may set,
# whose named entries chmod leaves in place (check-adversarial-r2 m1)
_ACL_XATTRS = ("system.posix_acl_access", "system.posix_acl_default")


def _drop_xattrs(path: str) -> None:
    """Remove every user.* extended attribute and the POSIX ACLs of path, never following a link;
    a name the file system does not hold or refuses is skipped."""
    try:
        names = [n for n in os.listxattr(path, follow_symlinks=False) if n.startswith("user.")]
    except OSError:
        names = []
    for name in (*names, *_ACL_XATTRS):
        try:
            os.removexattr(path, name, follow_symlinks=False)
        except OSError:
            pass


def _reset_metadata(root: str, entries, folders, when: float) -> None:
    """Give the tree root and every tracked file and folder of head the metadata of a fresh cut,
    never following a link: no user.* extended attribute and no POSIX ACL, then mode 0755 for a
    folder and for a regular file head records executable (100755), 0644 for any other regular
    file, and the one mtime `when` the engine chose. A mode, an xattr, an ACL or an mtime the agent
    set is a channel outside the tracked diff that the eval could read (check-adversarial-r1 M1,
    check-adversarial-r2 m1 and m2)."""
    def reset(target: str, mode: int) -> None:
        _drop_xattrs(target)
        try:
            os.chmod(target, mode)
            os.utime(target, (when, when), follow_symlinks=False)
        except OSError:
            pass

    try:
        if stat.S_ISDIR(os.lstat(root).st_mode):
            reset(root, 0o755)
    except OSError:
        pass
    for folder in sorted(folders):
        target = _inside_no_link(root, folder)
        try:
            if target is None or not stat.S_ISDIR(os.lstat(target).st_mode):
                continue
        except OSError:
            continue
        reset(target, 0o755)
    for mode, rel in entries:
        if mode not in ("100644", "100755"):
            continue
        target = _inside_no_link(root, rel)
        try:
            if target is None or not stat.S_ISREG(os.lstat(target).st_mode):
                continue
        except OSError:
            continue
        reset(target, 0o755 if mode == "100755" else 0o644)


def _scan(folder: str) -> list | None:
    """The entries of folder, never following a link; a folder the agent made unreadable is
    given back to its owner once; None when it still cannot be read."""
    for attempt in (0, 1):
        try:
            with os.scandir(folder) as it:
                return list(it)
        except PermissionError:
            if attempt:
                return None
            try:
                if stat.S_ISDIR(os.lstat(folder).st_mode):
                    os.chmod(folder, 0o700)
            except OSError:
                return None
        except OSError:
            return None
    return None


def _is_regular(path: str) -> bool:
    """True when path is a regular file, never following a link."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _plain(entry) -> bool:
    """A regular file or a symlink, the only entries other than folders that git lists."""
    return entry.is_symlink() or entry.is_file(follow_symlinks=False)


def _purge_folders(wt: WorktreeInfo, tracked, folders, links, keep) -> list:
    """After the file purge, remove, without following a link, every entry that git never lists
    and keep does not admit, with everything below it that keep does not admit: a folder that
    holds no tracked path of head (an empty `answers/42/`, or the `build/` an ignored file left),
    a non-folder entry of a tracked folder that is not a regular file or symlink at a path head
    tracks (a FIFO or a unix socket, whose name the eval could list; check-adversarial-r2 B1),
    and whatever is inside a gitlink folder of head (git lists nothing below a gitlink). `tracked`
    holds the paths of head's entries. The root `.git` is skipped (purge_ignored restored it).
    A folder keep admits stays with its folders and regular files; any other entry in it (a
    symlink, which could point the eval outside the tree, a FIFO, a socket) is removed, and so is
    a kept non-folder entry that is not a regular file (check-adversarial-resume-r1 m2). The walk
    is post-order over an explicit stack, never recursion. Returns the paths removed."""
    removed = []
    # a frame: [folder, rel, kind, entries, next index, stays]; kind is "tracked" (a folder that
    # holds a tracked path, or the root), "link" (a gitlink folder), "kept" (keep admits it) or
    # "free" (anything else: removed on the way back up when nothing in it stays)

    def frame(folder: str, rel: str, kind: str) -> list:
        entries = _scan(folder)
        if entries is None and kind == "tracked":
            raise IsolationError("ISOLATION-CONTAINMENT", "a tracked folder cannot be read")
        return [folder, rel, kind, entries or [], 0, False]

    stack = [frame(wt.path, "", "tracked")]
    while stack:
        top = stack[-1]
        folder, rel, kind, entries, i, _stays = top
        if i == len(entries):
            stack.pop()
            if stack:
                parent = stack[-1]
                if kind != "free" or top[5]:
                    parent[5] = True
                else:
                    _rmtree(folder)
                    removed.append(rel.rstrip("/"))
            continue
        top[4] = i + 1
        entry = entries[i]
        name = rel + entry.name
        if not rel and entry.name == ".git":
            top[5] = True
            continue
        if entry.is_dir(follow_symlinks=False):
            if kind == "kept":
                child = "kept"
            elif name in links:
                child = "link"
            elif name in folders:
                child = "tracked"
            elif _admitted(name, keep):
                child = "kept"
            else:
                child = "free"
            stack.append(frame(entry.path, name + "/", child))
            continue
        if kind == "tracked" and name in tracked and _plain(entry):
            top[5] = True
        elif (kind == "kept" or _admitted(name, keep)) and not entry.is_symlink() \
                and entry.is_file(follow_symlinks=False):
            top[5] = True
        else:
            _rmtree(entry.path)
            removed.append(name)
    return removed


def _unlisted(wt: WorktreeInfo, folders, links) -> tuple:
    """Runs no git. The entries below wt.path that git never lists, so post_eval names them itself:
    the top-most folders that hold no tracked path of head, are no gitlink of head and hold no
    entry but folders at any depth (or cannot be read); every entry that is neither a folder, a
    regular file nor a symlink (a FIFO, a socket, a device); and every entry inside a gitlink
    folder of head. Links are never followed; the root `.git` is skipped (_compare checks it).
    One post-order walk over an explicit stack, never recursion: a folder's findings are known
    when the walk leaves it, and an untracked folder that holds no entry but folders at any depth
    is reported alone, in place of what lies below it."""
    # a frame: [rel, tracked, entries, next index, solid, found]; solid is True once any entry
    # but a folder was seen at any depth below, found the findings below it
    stack = [["", True, _scan(wt.path) or [], 0, False, []]]
    while True:
        top = stack[-1]
        rel, tracked, entries, i, solid, found = top
        if i == len(entries):
            stack.pop()
            if not stack:
                return tuple(sorted(found))
            parent = stack[-1]
            if not tracked and not solid:
                parent[5].append(rel.rstrip("/"))
            else:
                parent[5].extend(found)
            parent[4] = parent[4] or solid
            continue
        top[3] = i + 1
        entry = entries[i]
        name = rel + entry.name
        if not rel and entry.name == ".git":
            continue
        if not entry.is_dir(follow_symlinks=False):
            top[4] = True
            if not _plain(entry):
                found.append(name)
            continue
        if name in links:
            top[4] = True
            found.extend(name + "/" + e.name for e in _scan(entry.path) or ())
            continue
        stack.append([name + "/", name in folders, _scan(entry.path) or [], 0, False, []])


def _compare(clone: CloneInfo, wt: WorktreeInfo, head: str) -> tuple:
    """(nested, special, changed, ignored, unlisted) of wt against head. On a `.git` below the root
    or an entry git would block on (_hazards) no git runs, and only nested and special are
    filled. A root `.git` that is not the engine's (_root_dotgit_ok) goes into changed and the
    compare still runs, since the pinned GIT_DIR never reads that file."""
    nested, special = _hazards(wt)
    if nested or special:
        return nested, special, (), (), ()
    env = _fresh_index(clone, wt, head)
    diff = _zlist(_git(["diff", "--ignore-submodules=all", "--name-only", "-z", "--no-renames",
                        "--no-ext-diff", "--no-textconv", head], env=env, cwd=wt.path))
    untracked = _zlist(_git(["ls-files", "--others", "--exclude-standard", "-z"], env=env,
                            cwd=wt.path))
    ignored = _ignored(env, wt.path)
    _entries, folders, links = _tracked(env, wt.path, head)
    changed = set(diff) | set(untracked)
    if not _root_dotgit_ok(wt):
        changed.add(".git")
    return ((), (), tuple(sorted(changed)), tuple(sorted(ignored)),
            _unlisted(wt, folders, links))


@_contained
def purge_ignored(clone: CloneInfo, wt: WorktreeInfo, *, head: str,
                  keep: tuple[str, ...]) -> PurgeResult:
    """Make the tree the eval reads hold head's tracked bytes and the kept paths, and nothing else.
    head is the captured commit of this tree (or forked_from when the capture was empty), so
    every path the attempt made is tracked in it. Before any git call, put back the tree's root
    `.git` (_restore_root_dotgit: whatever is there is removed without following a link and the
    engine's `gitdir:` bytes are written again, or nothing when the tree has no admin folder),
    then remove every entry host git would block on or cannot reach (_hazards: a `.git` below the
    root, a `.gitignore`, `.gitattributes` or `.gitmodules` that is not a regular file, a path
    longer than _MAX_REL), which a tracked path of head gets back from checkout-index below; one
    that keep admits (or a folder that cannot be read) raises ISOLATION-CONTAINMENT, since the
    listing would still open it.
    Then, with the engine index read fresh from head: give the tracked folders back their mode so
    the purge can write them; delete every path `git ls-files --others --ignored
    --exclude-standard -z` lists, except the regular files keep admits (a kept symlink is removed,
    since the eval could read through it outside the tree), never following a symlink; remove
    every entry git never lists that keep does not admit (_purge_folders: a folder that holds no
    tracked path, a FIFO or socket, anything below a gitlink, and in a kept folder anything but
    folders and regular files); rewrite every tracked file from
    head with `git checkout-index -f -a`, so the tracked bytes the eval reads depend on head alone
    and not on a clean conversion the tree's `.gitattributes` applied at capture (`ident`, `text`
    end-of-line normalization, `working-tree-encoding`; check-contract-r2 finding 1,
    check-adversarial-r2 B2); last, reset the metadata of the root and every tracked file and
    folder (_reset_metadata). With the engine index and GIT_DIR, an ignored output the agent
    force-added to its own admin index is still listed and removed. Returns what was removed
    (files, links, folders and other entries, the ones git never lists included) and the ignored
    set left. Any OSError or RecursionError is raised as ISOLATION-CONTAINMENT (_contained)."""
    _restore_root_dotgit(wt)
    removed, kept = [], []
    nested, special = _hazards(wt)
    for rel in (*nested, *special):
        if rel == "." or _admitted(rel, keep):
            raise IsolationError("ISOLATION-CONTAINMENT", "host git would read %s, which the "
                                 "purge may not remove" % _short(rel))
        target = _inside_no_link(wt.path, rel)
        if target is None:
            raise IsolationError("ISOLATION-CONTAINMENT", "%s runs through a link" % _short(rel))
        if _rmtree(target):
            removed.append(rel)
    if any(_hazards(wt)):
        raise IsolationError("ISOLATION-CONTAINMENT", "an entry host git would read is still there")
    env = _fresh_index(clone, wt, head)
    entries, folders, links = _tracked(env, wt.path, head)
    when = time.time()
    _reset_metadata(wt.path, entries, folders, when)
    for rel in _ignored(env, wt.path):
        clean = rel.rstrip("/")
        target = _inside_no_link(wt.path, clean)
        if target is None:
            raise IsolationError("ISOLATION-CONTAINMENT", "an ignored path runs through a link")
        if _admitted(clean, keep) and _is_regular(target):
            kept.append(clean)
            continue
        if _rmtree(target):
            removed.append(clean)
    removed.extend(_purge_folders(wt, {rel for _mode, rel in entries}, folders, links, keep))
    _git(["checkout-index", "-f", "-a"], env=env, cwd=wt.path)
    _reset_metadata(wt.path, entries, folders, when)
    return PurgeResult(phase="pre-eval", removed=tuple(sorted(set(removed))),
                      kept=tuple(sorted(kept)), ok=True)


@_contained
def post_eval(clone: CloneInfo, wt: WorktreeInfo, *, head: str, keep: tuple[str, ...],
              eval_outputs: tuple[str, ...], before: tuple[str, ...]) -> PurgeResult:
    """After the eval, first the no-git walk (_hazards): on any `.git` entry below the root,
    PurgeResult.nested lists them, on any `.gitignore`, `.gitattributes` or `.gitmodules` that is
    not a regular file (a FIFO would block the listing), or on a path longer than _MAX_REL (git
    cannot reach it), PurgeResult.new lists it whatever keep and eval_outputs admit, ok is False
    and no git runs. Otherwise, with the engine index
    read fresh from head: `new` lists ignored paths not in `before` that neither keep nor
    eval_outputs admits, and, on the same rule, the entries git never lists (_unlisted: a folder
    that holds no tracked path and no file at any depth, a FIFO, socket or device, an entry inside
    a gitlink folder); `changed` the paths whose content differs from head, the untracked paths
    git does not ignore (an explicit compare with head, never `git status`), and ".git" when the
    root `.git` is not the engine's. ok is False when any list is not empty. Any OSError or
    RecursionError is raised as ISOLATION-CONTAINMENT (_contained)."""
    nested, special, changed, ignored, unlisted = _compare(clone, wt, head)
    if nested or special:
        return PurgeResult(phase="post-eval", nested=nested, new=special, ok=False)
    was = {p.rstrip("/") for p in before or ()}
    new, kept = [], []
    for rel in ignored:
        clean = rel.rstrip("/")
        if _admitted(clean, keep):
            kept.append(clean)
        elif clean in was or _admitted(clean, eval_outputs):
            continue
        else:
            new.append(clean)
    for rel in unlisted:
        if not (rel in was or _admitted(rel, keep) or _admitted(rel, eval_outputs)):
            new.append(rel)
    return PurgeResult(phase="post-eval", kept=tuple(sorted(kept)), new=tuple(sorted(new)),
                      changed=tuple(changed), ok=not new and not changed)


# ---------------------------------------------------------------- capture, rebuild, merge

def _stage(clone: CloneInfo, wt: WorktreeInfo, forked_from: str) -> tuple:
    """Stage the tree into the engine index read fresh from forked_from and the engine host store:
    (env, tree, gitlinks). Before any git call, the no-git walk (_hazards): a `.gitignore`,
    `.gitattributes` or `.gitmodules` that is not a regular file raises ISOLATION-CONTAINMENT
    (`git add -A` would open it and, on a FIFO, wait for a writer that never comes), and so does
    a path longer than _MAX_REL (git would skip it and what lies below it); each nested
    repository (the top-most folders that hold a `.git` below the root) is left out of
    `git add -A` with a literal exclude pathspec, so git never reads its gitdir or HEAD, files
    the agent wrote, and no HEAD SHA of another repository reaches the diff; it is listed in
    gitlinks, with any mode 160000 entry of the index (check-adversarial-r3 B1 and m1)."""
    nested, special = _hazards(wt)
    if special:
        raise IsolationError(_GIT_FAILED, "host git would open %s, which is not a regular file"
                             " or lies past the path limit"
                             % ", ".join(_short(r) for r in special[:5]))
    repos = _top_most(n.rsplit("/", 1)[0] for n in nested)
    env = _fresh_index(clone, wt, forked_from)
    spec = ["--", "."] + [":(exclude,literal)%s" % r for r in repos] if repos else []
    _git(["add", "-A", *spec], env=env, cwd=wt.path)
    links = set(repos)
    for entry in _git(["ls-files", "-s", "-z"], env=env, cwd=wt.path).split(b"\0"):
        if entry:
            meta, path = entry.split(b"\t", 1)
            if meta.split()[0].decode("ascii") == _GITLINK:
                links.add(path.decode("utf-8", "surrogateescape"))
    tree = _text(_git(["write-tree"], env=env, cwd=wt.path))
    return env, tree, tuple(sorted(links))


def _record(env: dict, cwd: str, *, forked_from: str, tree: str, message: str, stamp: str,
            diff_path: str | None) -> tuple:
    """(head, paths): the commit of tree on forked_from dated stamp, or forked_from when the tree
    is forked_from's; the binary diff between them written to diff_path when given."""
    base_tree = _text(_git(["rev-parse", "--verify", "%s^{tree}" % forked_from], env=env, cwd=cwd))
    if tree == base_tree:
        if diff_path:
            open(diff_path, "wb").close()
        return forked_from, ()
    head = _text(_git(["commit-tree", tree, "-p", forked_from, "-m", message],
                      env=_commit_env(env, stamp), cwd=cwd))
    if diff_path:
        with open(diff_path, "wb") as fh:
            _git(["diff-tree", "-r", "-p", "--binary", "--no-renames", "--no-ext-diff",
                  "--no-textconv", forked_from, head], env=env, cwd=cwd, stdout=fh)
    names = _zlist(_git(["diff-tree", "-r", "--name-only", "--no-renames", "-z", forked_from, head],
                        env=env, cwd=cwd))
    return head, tuple(names)


def _capture_to(clone, wt, *, forked_from, message, stamp, diff_path) -> DiffCapture:
    env, tree, links = _stage(clone, wt, forked_from)
    os.makedirs(os.path.dirname(diff_path), exist_ok=True)
    head, names = _record(env, wt.path, forked_from=forked_from, tree=tree, message=message,
                          stamp=stamp, diff_path=diff_path)
    sha, size = _sha256_file(diff_path)
    return DiffCapture(forked_from=forked_from, head=head, tree=tree, path=diff_path, sha256=sha,
                       bytes=size, paths=names, stamp=stamp, message=message,
                       empty=head == forked_from, gitlinks=links)


@_contained
def capture(clone: CloneInfo, wt: WorktreeInfo, *, forked_from: str, message: str, stamp: str,
            out_dir: str) -> DiffCapture:
    """Stage the worktree into the engine index (read fresh from forked_from) and the engine host
    store, never the agent's private store, after the no-git walk of _stage (ISOLATION-CONTAINMENT
    on a `.gitignore`, `.gitattributes` or `.gitmodules` that is not a regular file or on a path
    longer than _MAX_REL, and on any OSError or RecursionError (_contained); a nested
    repository is left out of the staging and listed in gitlinks); commit the tree on
    forked_from dated stamp; write `git diff-tree -r -p --binary --no-renames --no-ext-diff
    --no-textconv` to out_dir/attempt.diff. An unchanged tree gives an empty capture: no commit, a
    zero-byte attempt.diff, head = forked_from and the sha256 of no bytes."""
    return _capture_to(clone, wt, forked_from=forked_from, message=message, stamp=stamp,
                       diff_path=os.path.join(out_dir, "attempt.diff"))


@_contained
def commit_prepared(clone: CloneInfo, wt: WorktreeInfo, *, parent: str, message: str,
                    stamp: str, out_dir: str) -> DiffCapture:
    """After the repair `prepare` stage ran on wt (a fresh_tree of the repair clone at parent, its
    root commit): the steps of capture into out_dir/prepare.diff, `git add -A` leaving the
    stage's ignored outputs out; barrier.scan over the new commit with the clone's policy
    (ISOLATION-BARRIER on a hit); then `git update-ref refs/heads/<clone.branch> <head> <parent>`.
    An unchanged tree gives an empty DiffCapture (head = parent) and no commit."""
    cap = _capture_to(clone, wt, forked_from=parent, message=message, stamp=stamp,
                      diff_path=os.path.join(out_dir, "prepare.diff"))
    if cap.empty:
        return cap
    if cap.gitlinks:
        raise IsolationError("ISOLATION-BARRIER", "the prepare stage left a nested repository")
    # proved first in a scratch repository that borrows the clone's objects, so a refused commit
    # never reaches the clone's store; then made again in the clone and the branch moves
    policy = _read_policy(clone.path)
    scratch = tempfile.mkdtemp(prefix=".prove-", dir=os.path.dirname(clone.path))
    try:
        _git(["init", "-q", "--template=", "-b", "main", scratch], env=_base_env())
        info = os.path.join(scratch, ".git", "objects", "info")
        os.makedirs(info, exist_ok=True)
        with open(os.path.join(info, "alternates"), "w", encoding="utf-8") as fh:
            fh.write(os.path.join(clone.path, ".git", "objects") + "\n")
        probe = CloneInfo(path=scratch, line=clone.line, branch="main", base_commit=parent,
                          head=parent)
        _reproduce(probe, parent=parent, capture=cap, message=message, stamp=stamp)
        _prove(scratch, cap.head, policy)
    finally:
        _rmtree(scratch)
    _reproduce(clone, parent=parent, capture=cap, message=message, stamp=stamp)
    _git(["update-ref", "refs/heads/%s" % clone.branch, cap.head, parent], env=git_env(clone))
    return cap


def rebuild(root: str, run: str, task: str, clone: CloneInfo, *, recorded: DiffCapture,
            message: str, stamp: str, cut: int = 1) -> WorktreeInfo:
    """Recreate a destroyed or reclaimed attempt worktree, as cut `cut`, from
    recorded.forked_from plus the recorded diff: create_worktree, `git apply --binary` of the diff
    to the tree, then the steps
    of capture; ISOLATION-REBUILD-MISMATCH when the diff file no longer has its recorded sha256
    or the rebuilt head differs from recorded.head. Last, every tracked file is written again from
    head (`git checkout-index -f -a`), so the tree holds head's bytes as a cut gives them."""
    sha, _size = _sha256_file(recorded.path)
    if sha != recorded.sha256:
        raise IsolationError("ISOLATION-REBUILD-MISMATCH", "the recorded diff changed")
    wt = create_worktree(root, run, task, clone, commit=recorded.forked_from, cut=cut)
    if not recorded.empty:
        env = git_env(clone, worktree=wt)
        _git(["apply", "--binary", "--whitespace=nowarn", "--allow-empty", recorded.path],
             env=env, cwd=wt.path, code="ISOLATION-REBUILD-MISMATCH")
    env, tree, _links = _stage(clone, wt, recorded.forked_from)
    head, _names = _record(env, wt.path, forked_from=recorded.forked_from, tree=tree,
                           message=message, stamp=stamp, diff_path=None)
    if head != recorded.head:
        raise IsolationError("ISOLATION-REBUILD-MISMATCH",
                             "rebuilt head %s, recorded %s" % (head[:12], recorded.head[:12]))
    # `git apply` writes the diff's bytes with no smudge (an `ident` stays `$Id$`): write every
    # tracked file again from head, so the rebuilt tree holds what a cut of head holds; the index
    # is read fresh from head first, so it holds no stat data that marks a file up to date
    env = _fresh_index(clone, wt, head)
    _git(["checkout-index", "-f", "-a"], env=env, cwd=wt.path)
    return wt


def merge_champion(clone: CloneInfo, *, branch: str, parent: str, capture: DiffCapture,
                   message: str, stamp: str) -> str:
    """Reproduce the attempt commit in the clone's own store from the recorded diff (same tree,
    parent, author, dates and message give the same hash) with a temporary index outside every
    worktree, check it equals capture.head, then `git update-ref refs/heads/<branch> <head>
    <parent>`. Never checks out. Idempotent: returns head when the branch already points at it."""
    env = git_env(clone)
    ref = "refs/heads/%s" % branch
    now = _git(["rev-parse", "--verify", "-q", ref], env=env, ok=(0, 1))
    if _text(now) == capture.head:
        return capture.head
    head = _reproduce(clone, parent=parent, capture=capture, message=message, stamp=stamp)
    if head != parent:
        _git(["update-ref", ref, head, parent], env=env)
    return head


def _reproduce(clone: CloneInfo, *, parent: str, capture: DiffCapture, message: str,
               stamp: str) -> str:
    """The commit of capture made again in the clone's own store from its diff file, with a
    temporary index outside every worktree; ISOLATION-REBUILD-MISMATCH when the diff file lost its
    recorded sha256 or the commit is not capture.head."""
    sha, _size = _sha256_file(capture.path)
    if sha != capture.sha256:
        raise IsolationError("ISOLATION-REBUILD-MISMATCH", "the recorded diff changed")
    scratch = tempfile.mkdtemp(prefix=".merge-", dir=os.path.dirname(clone.path))
    try:
        ienv = git_env(clone, index=os.path.join(scratch, "index"))
        _git(["read-tree", parent], env=ienv, cwd=scratch)
        if not capture.empty:
            _git(["apply", "--cached", "--binary", "--whitespace=nowarn", "--allow-empty",
                  capture.path], env=ienv, cwd=scratch, code="ISOLATION-REBUILD-MISMATCH")
        tree = _text(_git(["write-tree"], env=ienv, cwd=scratch))
        head, _names = _record(ienv, scratch, forked_from=parent, tree=tree, message=message,
                               stamp=stamp, diff_path=None)
    finally:
        _rmtree(scratch)
    if head != capture.head:
        raise IsolationError("ISOLATION-REBUILD-MISMATCH",
                             "made head %s, recorded %s" % (head[:12], capture.head[:12]))
    return head


# ---------------------------------------------------------------- teardown

def _made_admin(wt: WorktreeInfo) -> tuple:
    """The admin and host folders the engine makes for the tree at wt.path: the cut's
    `admin` and `host` in paths.cut_git_dir for an attempt worktree
    (<root>/.alpaca/worktrees/research/<run>/<task> or <task>-c<n>, read by paths.cut_of), and
    `<path>.git-admin` and `<path>.git-host` for any other tree."""
    path = os.path.abspath(wt.path)
    admins = {path + ".git-admin"}
    hosts = {path + ".git-host"}
    name, run_dir = os.path.basename(path), os.path.dirname(path)
    run = os.path.basename(run_dir)
    research = os.path.dirname(run_dir)
    dot = os.path.dirname(os.path.dirname(research))
    cut = paths.cut_of(name)
    if (cut is not None and os.path.basename(research) == "research"
            and os.path.basename(os.path.dirname(research)) == "worktrees"
            and os.path.basename(dot) == ".alpaca"):
        try:
            att = paths.cut_git_dir(os.path.dirname(dot), run, cut[0], cut[1])
        except ValueError:
            att = None
        if att:
            admins.add(os.path.join(att, "admin"))
            hosts.add(os.path.join(att, "host"))
    return admins, hosts


def destroy_worktree(clone: CloneInfo, wt: WorktreeInfo) -> None:
    """Runs no git. Checks that wt.admin_dir and wt.host_git, when set, are folders the engine
    makes for that tree (ISOLATION-PATH-TAKEN otherwise), then removes wt.path, wt.admin_dir and
    wt.host_git, the paths recorded at create time, each without following a symlink. The agent's
    private store stays with the attempt evidence."""
    admins, hosts = _made_admin(wt)
    if wt.admin_dir and os.path.abspath(wt.admin_dir) not in admins:
        raise IsolationError("ISOLATION-PATH-TAKEN", "not an admin folder the engine made")
    if wt.host_git and os.path.abspath(wt.host_git) not in hosts:
        raise IsolationError("ISOLATION-PATH-TAKEN", "not a host git folder the engine made")
    for p in (wt.path, wt.admin_dir, wt.host_git):
        if p:
            _rmtree(p)
    # a cut n > 1 of an attempt worktree: its `git/c<n>` folder goes once it is empty
    holder = os.path.dirname(os.path.abspath(wt.admin_dir or wt.host_git or ""))
    if re.fullmatch(r"c[0-9]+", os.path.basename(holder)) \
            and os.path.basename(os.path.dirname(holder)) == "git":
        _drop_empty(holder)


def _this_attempts_cut(root: str, run: str, task: str, recorded: WorktreeInfo) -> None:
    """ISOLATION-PATH-TAKEN unless recorded.path is paths.worktree_dir(root, run, task, c) for the
    cut c that paths.cut_of reads from its name, with the same task."""
    cut = paths.cut_of(os.path.basename(os.path.abspath(recorded.path or "")))
    if cut is None or cut[0] != task or os.path.abspath(recorded.path) != os.path.abspath(
            paths.worktree_dir(root, run, task, cut[1])):
        raise IsolationError("ISOLATION-PATH-TAKEN", "the recorded path is not this attempt's")


def reclaim(root: str, run: str, task: str, clone: CloneInfo, *,
            recorded: WorktreeInfo) -> bool:
    """Destroy a cut of this attempt's worktree with destroy_worktree(clone, recorded), where
    recorded comes from the task's `research-worktree` event of that cut, never from the
    worktree's `.git` file; the path must be a cut of this task (section 28.1). Reads no record.
    True when one existed."""
    _this_attempts_cut(root, run, task, recorded)
    existed = any(p and os.path.lexists(p)
                  for p in (recorded.path, recorded.admin_dir, recorded.host_git))
    destroy_worktree(clone, recorded)
    return existed


def remove_quiet(paths_: list) -> list:
    """_rmtree each path and return the ones that raised OSError (a folder a live writer keeps
    filling raises ENOTEMPTY, p3 of t-357): a dead tree that cannot be removed now is left to the
    sweep instead of failing the step."""
    left = []
    for p in paths_:
        if not p:
            continue
        try:
            _rmtree(p)
        except OSError:
            left.append(p)
    return left


def retire(root: str, run: str, task: str, clone: CloneInfo, *,
           recorded: WorktreeInfo) -> tuple:
    """(existed, left): remove a dead cut of this attempt's worktree and its admin and host
    folders with remove_quiet, after the checks of reclaim (a cut of this task, folders the
    engine makes for it). `left` lists the paths that could not be removed. Runs no git and reads
    no record (teardown, in-session-runtime.md section 28.3)."""
    _this_attempts_cut(root, run, task, recorded)
    admins, hosts = _made_admin(recorded)
    if recorded.admin_dir and os.path.abspath(recorded.admin_dir) not in admins:
        raise IsolationError("ISOLATION-PATH-TAKEN", "not an admin folder the engine made")
    if recorded.host_git and os.path.abspath(recorded.host_git) not in hosts:
        raise IsolationError("ISOLATION-PATH-TAKEN", "not a host git folder the engine made")
    folders = [recorded.path, recorded.admin_dir, recorded.host_git]
    existed = any(p and os.path.lexists(p) for p in folders)
    left = remove_quiet(folders)
    cut = paths.cut_of(os.path.basename(os.path.abspath(recorded.path)))
    if cut and cut[1] > 1:
        _drop_empty(paths.cut_git_dir(root, run, task, cut[1]))
    return existed, left


def _drop_empty(folder: str) -> None:
    """Remove the `git/c<n>` folder of a cut once its admin and host folders are gone; a folder
    that still holds anything (or a link) stays."""
    try:
        if stat.S_ISDIR(os.lstat(folder).st_mode):
            os.rmdir(folder)
    except OSError:
        pass


def sweep(root: str, run: str | None = None, *, sealed, dry_run: bool = True,
          grace_s: int = 3600, alive=None) -> list[str]:
    """Worktrees and launch homes whose attempt is done with sealed evidence
    (sealed(root, run, task) -> bool), older than grace_s, whose run has no live runner
    (alive(root, run) -> bool, default runner.is_alive). Dry run by default; returns the paths
    removed or to remove. Every cut of an attempt worktree (`<task>` and `<task>-c<n>`, read by
    paths.cut_of) is taken with that cut's `admin` and `host` folders (paths.cut_git_dir); the
    private object store stays. Reads no record. An attempt's age is the latest mtime of its
    attempt dir `attempts/<task>/` and the cut's host git folder, two folders no jail mounts,
    so attempt code cannot keep a sealed tree of a dead run from being swept by setting the mtime
    of the tree or of a launch home (check-adversarial-resume-r1 m3); only when neither exists does
    the entry's own mtime count."""
    if alive is None:
        from alpaca.research import runner
        alive = runner.is_alive
    if run is not None:
        runs = [run]
    else:
        found = set()
        # the folder that holds every run's worktrees folder, from paths.py (section 8.4)
        worktrees_root = os.path.dirname(paths.worktrees_dir(root, "rr-000"))
        for base in (worktrees_root, paths.research_dir(root)):
            try:
                found.update(n for n in os.listdir(base) if paths.RUN_ID.fullmatch(n))
            except OSError:
                pass
        runs = sorted(found)
    cutoff = time.time() - grace_s
    out = []

    def old(r: str, task: str, p: str, cut: int = 1) -> bool:
        att = paths.attempt_dir(root, r, task)
        times = []
        for q in (att, os.path.join(paths.cut_git_dir(root, r, task, cut), "host")):
            try:
                times.append(os.lstat(q).st_mtime)
            except OSError:
                pass
        if not times:
            try:
                times.append(os.lstat(p).st_mtime)
            except OSError:
                return False
        return max(times) < cutoff

    for r in runs:
        if alive(root, r):
            continue
        wdir = paths.worktrees_dir(root, r)
        try:
            names = sorted(os.listdir(wdir))
        except OSError:
            names = []
        for name in names:
            cut = paths.cut_of(name)
            if cut is None or not sealed(root, r, cut[0]):
                continue
            tree = os.path.join(wdir, name)
            if not old(r, cut[0], tree, cut[1]):
                continue
            git_dir = paths.cut_git_dir(root, r, cut[0], cut[1])
            for p in (tree, os.path.join(git_dir, "admin"), os.path.join(git_dir, "host")):
                if os.path.lexists(p):
                    out.append(p)
                    if not dry_run:
                        _rmtree(p)
            if cut[1] > 1 and not dry_run:
                _drop_empty(git_dir)
        hdir = os.path.dirname(paths.launch_home(root, r, "x"))
        try:
            homes = sorted(os.listdir(hdir))
        except OSError:
            homes = []
        for name in homes:
            m = _HOME_TASK.match(name)
            home = os.path.join(hdir, name)
            if m and sealed(root, r, m.group(1)) and old(r, m.group(1), home):
                out.append(home)
                if not dry_run:
                    _rmtree(home)
    return out
