"""Tamper fingerprints of an attempt and the strict editable-path allowlist check (t-092).

Design: design/research-engine/architecture.md section 3.2 (the API), section 9.3 (what the
fingerprints protect), section 7.2 (the lifecycle rows that take the points) and section 8.3 (the
research-tamper event the lifecycle records from a TamperVerdict).

A fingerprint point is a digest per source key, saved as attempt_dir/fingerprints/<point>.json.
The sources (FR-016, section 9.3):

- `contract:<path>`: the contract paths as the tree holds them. The tree is the attempt worktree
  once it exists, before that the round's champion checkout, which is the commit the worktree is
  cut at. The paths are each frozen contract file (every `files/<path>` entry of the frozen
  manifest contract/files.json, section 9.1) and every other path of the tree that a protected,
  test or hidden rule hits (barrier.path_is_protected), found by a walk of the whole tree that
  never passes through a symlink. The walk leaves out the tree's own `.git` entry, Python caches
  (`__pycache__`, `.pytest_cache`, `*.pyc`, `*.pyo`) and the paths the pack's keep or eval-output
  rules admit: those are ignored outputs, which the pre-eval purge and the post-eval check own
  (section 5.3). mission_model.observe's walk records no mode and skips more names, so the walk
  here is its own (gap map F10, recorded as a deviation in the t-092 proof). These keys carry no
  inode or ctime: the champion checkout and the worktree are different files for the same key,
  and a resume cuts the tree again. A worktree edit undone between two points is left to the gate
  jail, which overlays every protected path with its frozen copy (architecture.md section 9.1),
  and that frozen copy is stamped (next item);
- `frozen:<path>`: every file of the frozen contract folder (contract.json, files.json, files/,
  holdout/, hidden/), each with its inode and ctime, and one `frozen:<folder>/` entry per folder
  (`frozen:.` for the folder itself) with its inode and ctime, so a write undone before the next
  point and a file planted and removed again inside the folder are changes. contract.json and
  files.json must be there, files.json must hash to the contract's files_sha256, and every file
  files.json lists must be there with the sha256 it lists, or the source is an error. The folders
  above contract/ (the run folder, .alpaca/research, .alpaca, the project root) get no stamp, so
  a run folder renamed away, replaced and put back between two points is not caught here: those
  folders gain entries in normal work (the launcher makes <run>/homes and procs makes <run>/pids
  at the first launch, while the first slot's window is open; every alpaca command adds and
  removes the record's WAL files in .alpaca), so their ctime would mark every attempt broken. No
  agent or gate jail binds any of them (architecture.md section 9.2, item 1), and a process
  outside the jails that can rename them can as well rewrite the engine code and the saved point
  files, which lie in the same run folder; that is outside what a fingerprint can catch;
- `clone:<path>`: the clone's HEAD, config, packed-refs, refs/, hooks/, info/ and worktrees/, each
  file with its inode and ctime and each folder with an entry of its own (`clone:.git/<part>/.`
  and `clone:.git/<part>/<folder>/`), as the frozen folder is stamped: a config section added and
  removed again between two points is a change. A resume opens the clone again and never cuts it
  again, so the stamps hold across the `reopen` compare;
- `champion:<path>` and `champion-admin:<path>`: the round's champion checkout and its admin
  folder, content and mode only, no inode or ctime: a resume cuts the champion checkout again
  (section 10), so stamps there would break the `reopen` compare. Every agent jail mounts it
  read-only (architecture.md section 9.1) and no gate reads it;
- `main:<path>`: the target repository's paths under the pack scopes in the main checkout: every
  path tracked at HEAD, every untracked path its .gitignore files do not ignore, and every
  untracked `.gitignore` file even when a rule ignores it (outside the Python cache folders),
  each hashed from the working file. So a planted `.gitignore` that ignores itself and the files
  beside it is a change, since nothing purges the main checkout. The checkout's own index, stat
  cache and .git/info/exclude are never read, so an assume-unchanged or skip-worktree mark, a
  stat setting or an exclude line cannot hide an edit. These keys carry no inode or ctime, so an
  edit undone before the next point is left out on purpose: no gate reads the main checkout (the
  gates run the pack's commands from the frozen copy, architecture.md section 2, the gates.py
  row), and when the target is the live project other sessions write there all the time. The
  project's own .alpaca/ folder is not part of this source when it lies inside the checkout
  (_engine_folders): it holds the engine's run folders (.alpaca/research/, where another slot's
  gates step cuts and removes its base tree while this slot's window is open) and attempt
  worktrees (.alpaca/worktrees/research/), which the other sources cover;
- `shared:<path>` and `read:<path>`: the declared shared checkouts and the frozen read paths,
  each resolved with os.path.realpath at every point and read file by file, stamped as the
  frozen folder is (a folder entry is `<prefix><resolved path>/.` or `<prefix><resolved
  path>/<folder>/`). A git checkout leaves out only its own `.git` entry: no .gitignore rule
  hides a file, since no pack scope bounds a shared checkout and no purge cleans it. The content
  keys name the resolved path, and a location that is reached through a link also has the key of
  its declared path, whose value names the target. The base entry (`<prefix><resolved path>/.`,
  or the file's own key) also holds the ancestor chain of the resolved path (_ancestry), so a
  parent folder renamed away, replaced and put back between two points is a change;
- `tool:<name>`: every declared tool resolved on the gate PATH, its realpath hashed with its mode,
  inode and ctime, and the ancestor chain of that realpath; `tool-path:<folder>`: each folder of
  the gate PATH, by its realpath, inode and ctime and the ancestor chain of that realpath, so a
  shadow added and removed between two points, and a folder above the binary or a PATH folder
  renamed away, replaced and put back, still change a digest. The inode, ctime, chain and folder
  keys go beyond the realpath and mode that architecture.md section 3.2 names; a package install
  into a gate PATH folder during a window then marks the attempt broken, which fails closed (a
  deviation, recorded in the t-092 proof).

The ancestor chain of a resolved path (_ancestry) lists every folder above it up to /, each by
its device and inode, and by its ctime too where a process that is not root can rename it: a
rename moves the ctime of the folder renamed, and one that is put back keeps its inode. Two kinds
of folder get no ctime. A folder whose parent is owned by root and has no group or other write
bit (/usr, /opt, /tmp itself) only root can rename, and a root process is outside what any stamp
can catch. A folder right inside a sticky shared folder (/tmp/pytest-of-<user>, a tool's own
per-user temp root) gains and loses entries whenever that tool starts, so its ctime would mark
attempts broken for nothing; a swap of that one folder that is undone between two points is left
out (recorded in the t-092 proof).

A source that cannot be read is an error entry (its key, value "error: ...") and is also listed in
FingerprintPoint.errors; compare() reads any error in either point as BROKEN (EC-007), so a check
never passes on a missing source. Nothing here follows a symlink or opens a special file: a link is
recorded by its target text, a FIFO or socket by its type.

A file name that is not valid UTF-8 is kept in its key as Python's os functions give it (each bad
byte a lone surrogate, the surrogateescape form), so it is read back and compared like any other
name. The point file is written with every non-ASCII character escaped (`\\udcff`), which json
reads back as the same key. The paths and keys a TamperVerdict lists are shown with each bad byte
as a `\\xff` escape (_shown), so the lifecycle can record them in an event (sqlite refuses a lone
surrogate); the verdict's ok is decided on the keys themselves, before that.

Pure of the record, as isolation.py: no event is written here. take_attempt() and affected() read
the record; everything else reads the disk only."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile

from alpaca import barrier, runbook, util  # noqa: F401  (allowlist_match looks up runbook.path_admitted)
from alpaca.research import events, isolation, paths, procs
from alpaca.research.types import (CloneInfo, Contract, DiffCapture, FingerprintPoint,
                                   TamperVerdict, ToolPin, WorktreeInfo)


POINTS = ("open", "after-proposer", "after-implementer", "after-eval", "after-audit",
          "after-notes", "close", "reopen")   # repair adds "after-reproducer-writer"


AUDIT_POINTS = ("audit-before", "audit-after")   # audit.run's own pair (verifier.md section 9.6)

_REPAIR_POINTS = ("after-reproducer-writer",)    # the repair task's writing agent (section 3.2)

# The clone's .git parts a point reads (section 3.2: "the clone's refs, config and hooks folder").
# objects/ and logs/ are left out: no engine step inside a window writes them, and the attempt's
# own objects go to its private and host stores, never to the clone (section 3.1).
_CLONE_PARTS = ("HEAD", "config", "packed-refs", "refs", "hooks", "info", "worktrees")

# A champion checkout's admin folder file the point leaves out: git keeps stat data in an index, so a
# checkout cut again at the same commit on resume writes other index bytes for the same tree. The
# index cannot change what the tree holds, and the tree itself is hashed file by file.
_ADMIN_SKIP = ("index",)

# What the walk of the contract paths leaves out (see the module text): caches an agent's or a
# gate's Python run writes beside a protected test. They are ignored outputs, which the pre-eval
# purge removes unless the keep list admits them (section 5.3), so they never reach the eval; a
# walk that hashed them would mark every attempt that runs its tests broken.
_CACHE_DIRS = ("__pycache__", ".pytest_cache")
_CACHE_SUFFIXES = (".pyc", ".pyo")

_CHUNK = 1 << 20
_GIT_TIMEOUT_S = 600


class _SourceError(Exception):
    """A fingerprint source that could not be read; becomes an error entry."""


# ------------------------------------------------------------------ file digests (never follow)
def _file_value(path: str, *, stamp: bool = False) -> str:
    """The digest text of one path, read without following a symlink: "file sha256=<hex> mode=<oct>"
    for a regular file, "link <target>" for a symlink, "special <kind> mode=<oct>" for a FIFO,
    socket or device (never opened), "dir" for a folder, "absent" when nothing is there. With
    stamp, a file, link or special file also gets " ino=<inode> ctime=<ns>": a write that is undone
    before the next point (the same bytes, mode and mtime again) still moves the ctime, which an
    unprivileged process cannot set back, and a copy put in its place has another inode. Raises
    _SourceError when the file changes while it is read or cannot be read."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        raise _SourceError("%s: %s" % (path, exc.strerror or exc))
    mode = stat.S_IMODE(st.st_mode)
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    if stat.S_ISLNK(st.st_mode):
        try:
            value = "link %s" % os.readlink(path)
        except OSError as exc:
            raise _SourceError("%s: %s" % (path, exc.strerror or exc))
    elif not stat.S_ISREG(st.st_mode):
        kind = ("fifo" if stat.S_ISFIFO(st.st_mode) else "socket" if stat.S_ISSOCK(st.st_mode)
                else "device")
        value = "special %s mode=%04o" % (kind, mode)
    else:
        sha, mode, st = _read_regular(path, st)
        value = "file sha256=%s mode=%04o" % (sha, mode)
    return value + (_stamp(st) if stamp else "")


def _stamp(st: os.stat_result) -> str:
    return " ino=%d ctime=%d" % (st.st_ino, st.st_ctime_ns)


def _read_regular(path: str, st: os.stat_result) -> tuple[str, int, os.stat_result]:
    """The sha256, the permission bits and the closing fstat of the regular file path, whose lstat
    was st: opened with O_NOFOLLOW and O_NONBLOCK (a FIFO swapped in never blocks), checked to be
    the same inode, and refused when its size, mtime or ctime moved during the read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ELOOP:          # swapped for a link after the lstat
            raise _SourceError("%s: changed during the read" % path)
        raise _SourceError("%s: %s" % (path, exc.strerror or exc))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_ino != st.st_ino:
            raise _SourceError("%s: changed during the read" % path)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
    except OSError as exc:
        raise _SourceError("%s: %s" % (path, exc.strerror or exc))
    finally:
        os.close(fd)
    if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise _SourceError("%s: changed during the read" % path)
    return digest.hexdigest(), stat.S_IMODE(after.st_mode), after


def _tree_value(base: str, rel: str) -> str:
    """_file_value of base/rel, where rel is a "/" path inside the tree base, without passing
    through a symlink on the way: a link in a parent folder gives "link-in-path <part> -> <target>"
    and a parent that is missing or not a folder gives "absent", so a folder swapped for a link to
    a copy elsewhere is a change, never a read of the copy."""
    parts = rel.split("/")
    cursor = base
    for part in parts[:-1]:
        cursor = os.path.join(cursor, part)
        try:
            st = os.lstat(cursor)
        except FileNotFoundError:
            return "absent"
        except OSError as exc:
            raise _SourceError("%s: %s" % (cursor, exc.strerror or exc))
        if stat.S_ISLNK(st.st_mode):
            try:
                return "link-in-path %s -> %s" % (os.path.relpath(cursor, base),
                                                  os.readlink(cursor))
            except OSError as exc:
                raise _SourceError("%s: %s" % (cursor, exc.strerror or exc))
        if not stat.S_ISDIR(st.st_mode):
            return "absent"
    return _file_value(os.path.join(base, *parts))


def _walk(base: str, *, prune: tuple = (), folders: list | None = None) -> list[str]:
    """Every entry below the folder base that is not a folder (files, links, special files), as
    paths relative to base with "/" separators, sorted. Never descends into a symlink. An entry of
    base itself whose name is in prune is left out whole. When folders is a list, the relative
    path of every folder below base is appended to it. A folder that cannot be listed raises
    _SourceError."""
    found = []
    pending = [""]
    while pending:
        rel = pending.pop()
        folder = os.path.join(base, rel) if rel else base
        try:
            with os.scandir(folder) as listing:
                items = list(listing)
        except OSError as exc:
            raise _SourceError("%s: %s" % (folder, exc.strerror or exc))
        for item in items:
            if not rel and item.name in prune:
                continue
            child = item.name if not rel else rel + "/" + item.name
            try:
                is_dir = item.is_dir(follow_symlinks=False)
            except OSError as exc:
                raise _SourceError("%s: %s" % (item.path, exc.strerror or exc))
            if is_dir:
                pending.append(child)
                if folders is not None:
                    folders.append(child)
            else:
                found.append(child)
    return sorted(found)


def _add_tree(entries: dict, prefix: str, base: str, *, skip=(), stamp: bool = False,
              prune: tuple = ()) -> None:
    """One entry per file below base, keyed prefix + relative path. base must be a folder that is
    not a symlink. With stamp, each file value carries its inode and ctime (_file_value), and each
    folder has an entry of its own, keyed prefix + "." for base and prefix + <relative path> + "/"
    below it, holding its inode and ctime: a file planted and removed again between two points
    still moves its folder's ctime. prune names entries of base itself that are left out whole."""
    try:
        st = os.lstat(base)
    except OSError as exc:
        raise _SourceError("%s: %s" % (base, exc.strerror or exc))
    if not stat.S_ISDIR(st.st_mode):
        raise _SourceError("%s: not a folder" % base)
    folders: list = []
    for rel in _walk(base, prune=prune, folders=folders if stamp else None):
        if rel in skip:
            continue
        entries[prefix + rel] = _file_value(os.path.join(base, rel), stamp=stamp)
    if stamp:
        entries[prefix + "."] = "dir" + _stamp(st)
        for rel in folders:
            entries[prefix + rel + "/"] = _folder_value(os.path.join(base, rel))


def _folder_value(path: str) -> str:
    """"dir ino=<inode> ctime=<ns>" of a folder, read without following a symlink; a folder that
    was swapped for anything else between the walk and this read is an error."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise _SourceError("%s: %s" % (path, exc.strerror or exc))
    if not stat.S_ISDIR(st.st_mode):
        raise _SourceError("%s: changed during the read" % path)
    return "dir" + _stamp(st)


def _real_folder(path: str) -> None:
    """Raise _SourceError unless path is a folder that is not a symlink."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise _SourceError("%s: missing or not a folder (%s)" % (path, exc.strerror or exc))
    if not stat.S_ISDIR(st.st_mode):
        raise _SourceError("%s: missing or not a folder" % path)


def _ancestry(path: str) -> str:
    """" up=" and the ancestor chain of the absolute path: every folder above it up to /, nearest
    first, joined by ";", each "<folder> dev=<device> ino=<inode>", plus " ctime=<ns>" when a
    process that is not root can rename the folder, "<folder> absent" when nothing is there.
    A rename moves the ctime of the folder renamed, so a folder renamed away, replaced and put
    back between two points is a change, although its inode is back. No ctime for "/", for a
    folder whose parent is owned by root with no group or other write bit (only root can rename
    it), or for a folder right inside a sticky folder such as /tmp (a per-user temp root that
    gains entries whenever its tool starts; see the module text). An ancestor that is not a
    folder, or cannot be read, is an error."""
    chain = []
    cursor = os.path.dirname(path)
    while True:
        try:
            st = os.lstat(cursor)
        except FileNotFoundError:
            st = None
        except OSError as exc:
            raise _SourceError("%s: %s" % (cursor, exc.strerror or exc))
        if st is not None and not stat.S_ISDIR(st.st_mode):
            raise _SourceError("%s: an ancestor that is not a folder" % cursor)
        chain.append((cursor, st))
        parent = os.path.dirname(cursor)
        if parent == cursor:
            break
        cursor = parent
    parts = []
    for i, (folder, st) in enumerate(chain):
        if st is None:
            parts.append("%s absent" % folder)
            continue
        text = "%s dev=%d ino=%d" % (folder, st.st_dev, st.st_ino)
        above = chain[i + 1][1] if i + 1 < len(chain) else None
        if above is not None and not (
                (above.st_uid == 0 and not above.st_mode & 0o022)
                or above.st_mode & stat.S_ISVTX):
            text += " ctime=%d" % st.st_ctime_ns
        parts.append(text)
    return " up=" + ";".join(parts)


def _add_location(entries: dict, prefix: str, location: str) -> None:
    """A read path or a shared location, resolved with os.path.realpath first, so a location
    declared through a link is read where the link points. A resolved location other than the
    declared one adds the key prefix + location, whose value names the target, so a retargeted
    link is a change. Then a file is one entry and a folder one entry per file and per folder
    (_add_tree with stamp), all keyed by the resolved path, so a write undone before the next
    point and a file planted and removed again are changes. The base entry (prefix + resolved
    path + "/." for a folder, the file's own key for a file) also holds the ancestor chain of the
    resolved path (_ancestry), so a parent folder swapped and put back is a change. A folder is
    read file by file even when it is a git checkout: only its own `.git` entry is left out, and
    nothing its .gitignore files ignore is skipped (a shared checkout has no pack scope and no
    purge cleans it). A resolved path that is missing is an error (fail closed), a dangling link
    included."""
    real = os.path.realpath(location)
    if real != location:
        entries[prefix + location] = "resolves-to " + real
    value = _file_value(real, stamp=True)
    if value == "absent":
        raise _SourceError("%s: missing" % (location if real == location
                                            else "%s (-> %s)" % (location, real)))
    up = _ancestry(real)
    if value == "dir":
        stem = prefix + real.rstrip("/") + "/"
        _add_tree(entries, stem, real, stamp=True, prune=(".git",))
        entries[stem + "."] += up
    else:
        entries[prefix + real] = value + up


# ------------------------------------------------------------------ git helpers
def _plain_git_env() -> dict:
    """The environment for git in a checkout the engine does not own (the main checkout, a shared
    checkout): the runner's environment with every inherited GIT_* variable removed and system
    and global config off, as isolation.git_env does for a clone."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    return env


def _git(args: list, *, cwd: str, env: dict) -> bytes:
    """git with GIT_HARDEN on the command line; raises _SourceError on a non-zero exit."""
    try:
        proc = subprocess.run(["git", *isolation.GIT_HARDEN, *args], cwd=cwd, env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=_GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        raise _SourceError("git %s: %s" % (args[0], exc))
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise _SourceError("git %s: exit %d%s" % (args[0], proc.returncode,
                                                   (": " + detail[-1]) if detail else ""))
    return proc.stdout


def _nul_list(raw: bytes) -> list[str]:
    return [p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p]


def _shown(text: str) -> str:
    """text as a TamperVerdict lists it: unchanged when it is valid UTF-8, else each byte a
    surrogateescape name holds as a `\\xff` escape (any other lone surrogate as `\\udcff`), so the
    verdict can be recorded. Used only for what a verdict shows, never for a decision."""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        pass
    try:
        return text.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")
    except UnicodeError:
        return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _shown_all(items) -> tuple:
    return tuple(sorted({_shown(str(i)) for i in items}))


def _checkout(entries: dict, prefix: str, repo: str, *, scope=None, drop=()) -> None:
    """A git checkout by HEAD plus dirty paths, read without trusting the checkout's own state:
    the paths are every path tracked at HEAD (`git ls-tree`) and every untracked path the
    checkout's .gitignore files do not ignore (`git ls-files --others
    --exclude-per-directory=.gitignore` against an empty private index, so .git/info/exclude and
    the checkout's index are never read; GIT_HARDEN sets core.excludesFile to /dev/null), plus
    every untracked `.gitignore` file whatever ignores it (the same listing with no exclude rule,
    limited to that name), outside the Python cache folders, which write their own self-ignoring
    one: a planted `.gitignore` that ignores itself and a file beside it is then a key. Each
    path is hashed from its working file with _tree_value, never through a symlink, so no
    assume-unchanged or skip-worktree mark, no stat setting (core.checkStat, core.trustctime) and
    no racy-clean timing can hide an edit; a gitlink keeps its commit id from HEAD. scope, when
    given, is a tuple of root rules: only paths barrier.path_is_protected hits are kept (a wider
    match, so the safe side). drop names folders, relative to the top of the checkout, whose
    paths are left out of both listings and never hashed (the project's own .alpaca/ folder,
    _engine_folders)."""
    env = _plain_git_env()
    top = _git(["rev-parse", "--show-toplevel"], cwd=repo, env=env).decode(
        "utf-8", "surrogateescape").strip()
    if os.path.realpath(top) != os.path.realpath(repo):
        raise _SourceError("%s: not the top of a git checkout (%s is)" % (repo, top))
    head = _git(["rev-parse", "--verify", "HEAD^{commit}"], cwd=repo, env=env).strip()
    if not head:
        raise _SourceError("%s: no HEAD commit" % repo)
    drop = tuple(d.strip("/") for d in drop if d.strip("/"))

    def dropped(rel: str) -> bool:
        return any(rel == d or rel.startswith(d + "/") for d in drop)
    # the dropped folders never reach git's walk either (a large .alpaca/ costs time)
    skip = [":(top,exclude,literal)%s" % d for d in drop]
    gitlinks = {}
    tracked = set()
    for line in _git(["ls-tree", "-r", "-z", "--full-tree", "HEAD"], cwd=repo,
                     env=env).split(b"\0"):
        if not line:
            continue
        meta, _, name = line.partition(b"\t")
        fields = meta.split()
        rel = name.decode("utf-8", "surrogateescape")
        tracked.add(rel)
        if fields[0] == b"160000":
            gitlinks[rel] = "commit %s mode=160000" % fields[2].decode()
    with tempfile.TemporaryDirectory(prefix="alpaca-fp-") as scratch:
        # a path in an empty folder: git reads a missing index as empty and never writes it
        private = dict(env, GIT_INDEX_FILE=os.path.join(scratch, "index"))
        others = set(_nul_list(_git(["ls-files", "--others", "--exclude-per-directory=.gitignore",
                                     "-z", "--full-name", "--", ":/", *skip], cwd=repo,
                                    env=private)))
        # a .gitignore that ignores itself hides itself and every file it names from the listing
        # above; list every untracked one with no exclude rule at all
        for rel in _nul_list(_git(["ls-files", "--others", "-z", "--full-name", "--",
                                   ":(top,glob)**/.gitignore", *skip], cwd=repo, env=private)):
            if not any(part in _CACHE_DIRS for part in rel.split("/")[:-1]):
                others.add(rel)
    for rel in sorted(tracked | others):
        if dropped(rel):
            continue
        if scope is not None and barrier.path_is_protected(rel, scope) is None:
            continue
        key = prefix + rel
        entries[key] = gitlinks[rel] if rel in gitlinks else _tree_value(repo, rel.rstrip("/"))


# ------------------------------------------------------------------ tools
def resolve_tools(names: tuple[str, ...], path_env: str) -> dict[str, ToolPin]:
    """Each name resolved on path_env (the PATH the gates get), its realpath hashed with its mode.
    A name that does not resolve is a ToolPin with error set, which fails closed."""
    pins = {}
    for name in names:
        name = str(name)
        if not name or "/" in name or name in (".", ".."):
            pins[name] = ToolPin(name=name, resolved=None, sha256=None, mode=None,
                                 error="not a bare command name")
            continue
        found = shutil.which(name, path=path_env)
        if not found:
            pins[name] = ToolPin(name=name, resolved=None, sha256=None, mode=None,
                                 error="not found on the gate PATH")
            continue
        real = os.path.realpath(found)
        try:
            st = os.lstat(real)
            if not stat.S_ISREG(st.st_mode):
                raise _SourceError("%s: not a regular file" % real)
            sha, mode, _st = _read_regular(real, st)
        except (_SourceError, OSError) as exc:
            pins[name] = ToolPin(name=name, resolved=found, sha256=None, mode=None, error=str(exc))
            continue
        pins[name] = ToolPin(name=name, resolved=found, sha256=sha, mode=mode)
    return pins


def _tool_value(pin: ToolPin) -> str:
    """The pin, plus the inode and ctime of its realpath (a write that is undone before the next
    point, same bytes and mtime again, still moves the ctime, which an unprivileged process
    cannot set back) and the ancestor chain of the realpath (_ancestry: a folder above the binary
    renamed away, replaced and put back still moves that folder's ctime)."""
    real = os.path.realpath(pin.resolved)
    try:
        st = os.lstat(real)
    except OSError as exc:
        raise _SourceError("%s: %s" % (real, exc.strerror or exc))
    return "sha256=%s mode=%04o path=%s real=%s ino=%d ctime=%d%s" % (
        pin.sha256, pin.mode, pin.resolved, real, st.st_ino, st.st_ctime_ns, _ancestry(real))


def _path_folder_value(folder: str) -> str:
    """One gate PATH folder: its realpath, inode and ctime (a file added to it and removed again
    before the next point still moves the ctime), "absent" when nothing is there, and in both
    cases the ancestor chain of the realpath (_ancestry), so a folder above it swapped and put
    back, or a missing PATH folder made and removed again, is a change."""
    real = os.path.realpath(folder)
    try:
        st = os.stat(real)
    except FileNotFoundError:
        return "absent" + _ancestry(real)
    except OSError as exc:
        raise _SourceError("%s: %s" % (folder, exc.strerror or exc))
    kind = "dir" if stat.S_ISDIR(st.st_mode) else "not-a-folder"
    return "%s real=%s ino=%d ctime=%d%s" % (kind, real, st.st_ino, st.st_ctime_ns,
                                            _ancestry(real))


# ------------------------------------------------------------------ points
def _point_path(root: str, run: str, task: str, point: str) -> str:
    """attempt_dir/fingerprints/<point>.json (section 8.4)."""
    return os.path.join(paths.attempt_dir(root, run, task), "fingerprints", "%s.json" % point)


def _check_point(point: str) -> str:
    if point not in POINTS + AUDIT_POINTS + _REPAIR_POINTS:
        raise ValueError("unknown fingerprint point %r" % (point,))
    return point


def _abs(root: str, location: str) -> str:
    return os.path.normpath(os.path.join(root, location))


def _guard(entries: dict, errors: list, key: str, fn) -> None:
    """Run one source; an error becomes the error entry `key` (fail closed, EC-007)."""
    try:
        fn()
    except (_SourceError, OSError, ValueError) as exc:
        message = str(exc) or exc.__class__.__name__
        entries[key] = "error: " + message
        errors.append("%s: %s" % (key, message))


def _main_scope(root: str, contract: Contract, repo: str) -> tuple:
    """The pack scopes in the target repository: the editable, protected, test, hidden and holdout
    rules and the runbook folder, as root rules of that repository."""
    scope = (tuple(contract.editable) + tuple(contract.protected) + tuple(contract.tests)
             + tuple(contract.hidden) + tuple(contract.holdout_paths))
    folder = os.path.relpath(_abs(root, os.path.dirname(contract.runbook) or "."), repo)
    if folder != "." and not folder.startswith(".."):
        scope += (folder.replace(os.sep, "/").rstrip("/") + "/",)
    return scope


def _engine_folders(root: str, repo: str) -> tuple:
    """The folders of the main checkout that the `main:` source leaves out: the project's own
    .alpaca/ folder, relative to the top of the checkout, when it lies inside it (the target is the
    live project, or a folder that holds it). That folder is Alpaca's state, never target content,
    and the engine cuts and removes trees there while other slots' windows are open: another
    slot's base, reproducer and holdout trees under .alpaca/research/<run>/ (they have no .git, so
    git lists their files) and the attempt worktrees under .alpaca/worktrees/research/. The
    engine's own copies are the `frozen:`, `contract:`, `clone:` and `champion:` sources. The
    folder is named as git lists it: the real top of the checkout plus `.alpaca`, not resolved
    further, so a link named .alpaca is left out as itself."""
    top = os.path.realpath(repo)
    state = os.path.join(os.path.realpath(root), ".alpaca")
    rel = os.path.relpath(state, top)
    if rel == "." or rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
        return ()
    return (rel.replace(os.sep, "/"),)


def _safe_rel(rel: str) -> bool:
    return bool(rel) and not rel.startswith("/") and ".." not in rel.split("/") and "\0" not in rel


def _read_manifest(frozen: str, files_sha256: str) -> dict:
    """The frozen manifest contract/files.json (section 9.1: each path of files/, holdout/ and
    hidden/ -> {sha256, mode, size}), parsed: contract.json and files.json must be regular files,
    files.json a json object of safe relative paths under the three folders, and its sha256 in
    the normalized form contract.freeze writes it (util.canonical_json, t-096's _canon) must be
    files_sha256, the contract's own hash of the manifest, which read_frozen and load_frozen tie
    to the record. An empty files_sha256 is an error too. Raises _SourceError otherwise, so an
    emptied or rewritten manifest is an error at every later point, not a quiet pair of equal
    points; _verify_manifest then checks the files it lists."""
    for name in ("contract.json", "files.json"):
        if not _file_value(os.path.join(frozen, name)).startswith("file "):
            raise _SourceError("%s: missing or not a regular file" % os.path.join(frozen, name))
    target = os.path.join(frozen, "files.json")
    try:
        st = os.lstat(target)
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if os.fstat(fd).st_ino != st.st_ino:
                raise _SourceError("%s: changed during the read" % target)
            chunks = []
            while True:
                chunk = os.read(fd, _CHUNK)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(fd)
        manifest = json.loads(b"".join(chunks).decode("utf-8"))
    except OSError as exc:
        raise _SourceError("%s: %s" % (target, exc.strerror or exc))
    except ValueError as exc:
        raise _SourceError("%s: does not parse: %s" % (target, exc))
    if not isinstance(manifest, dict):
        raise _SourceError("%s: not a json object" % target)
    if not files_sha256:
        raise _SourceError("the contract names no files_sha256 to check %s against" % target)
    if util.sha256_hex(util.canonical_json(manifest)) != files_sha256:
        raise _SourceError("%s: does not hash to the contract's files_sha256" % target)
    for key in manifest:
        if not isinstance(key, str) or not _safe_rel(key) or key.split("/")[0] not in (
                "files", "holdout", "hidden") or len(key.split("/")) < 2:
            raise _SourceError("%s: an entry outside files/, holdout/ and hidden/: %r"
                               % (target, key))
    return manifest


def _verify_manifest(frozen: str, manifest: dict) -> None:
    """Every file the manifest lists is there, a regular file with the sha256 it lists; raises
    _SourceError otherwise."""
    for key, meta in sorted(manifest.items()):
        value = _tree_value(frozen, key)
        want = meta.get("sha256") if isinstance(meta, dict) else None
        if not value.startswith("file "):
            raise _SourceError("%s: listed in files.json, and %s" % (
                os.path.join(frozen, key), "missing" if value == "absent" else value))
        if value.split()[1] != "sha256=%s" % want:
            raise _SourceError("%s: does not match the sha256 files.json lists"
                               % os.path.join(frozen, key))


def _skipped(rel: str, skip_rules: tuple) -> bool:
    """A path the walk of the contract paths leaves out: a Python cache, or a path the keep or
    eval-output rules admit (runbook.path_admitted through allowlist_match)."""
    parts = rel.split("/")
    if any(p in _CACHE_DIRS for p in parts[:-1]) or parts[-1].endswith(_CACHE_SUFFIXES):
        return True
    if not skip_rules:
        return False
    try:
        return allowlist_match(rel, skip_rules) is not None
    except AttributeError as exc:           # no strict matcher to read the rules with: fail closed
        raise _SourceError("the keep and eval-output rules cannot be matched: %s" % exc)


def _protected_walk(base: str, rules: tuple, skip_rules: tuple) -> list[str]:
    """Every path below the tree base (never through a symlink; the tree's own `.git` entry and
    the cache folders pruned) that barrier.path_is_protected(rel, rules) hits and _skipped leaves
    in. The whole tree is walked: a one-folder or name rule hits at any depth, and the match
    ignores case, so no literal root prefix would bound it."""
    found = []
    pending = [""]
    while pending:
        rel = pending.pop()
        folder = os.path.join(base, rel) if rel else base
        try:
            with os.scandir(folder) as listing:
                items = list(listing)
        except OSError as exc:
            raise _SourceError("%s: %s" % (folder, exc.strerror or exc))
        for item in items:
            if not rel and item.name == ".git":
                continue
            child = item.name if not rel else rel + "/" + item.name
            try:
                is_dir = item.is_dir(follow_symlinks=False)
            except OSError as exc:
                raise _SourceError("%s: %s" % (item.path, exc.strerror or exc))
            if is_dir:
                if item.name not in _CACHE_DIRS:
                    pending.append(child)
            elif barrier.path_is_protected(child, rules) is not None and not _skipped(
                    child, skip_rules):
                found.append(child)
    return sorted(found)


def _collect(root: str, run: str, *, contract: Contract | None, worktree: WorktreeInfo | None,
             clone: CloneInfo | None, champion: WorktreeInfo | None, path_env: str | None,
             missing: dict) -> tuple[dict, list]:
    entries: dict = {}
    errors: list = []
    for key, message in missing.items():
        entries[key] = "error: " + message
        errors.append("%s: %s" % (key, message))
    if contract is None:
        return entries, errors
    frozen = paths.contract_dir(root, run)

    # the frozen contract folder: every file, and the manifest checked against it
    box: dict = {}

    def frozen_folder():
        _add_tree(entries, "frozen:", frozen, stamp=True)
        box["manifest"] = _read_manifest(frozen, contract.files_sha256)
        _verify_manifest(frozen, box["manifest"])
    _guard(entries, errors, "frozen", frozen_folder)

    # the contract paths as the tree holds them: the frozen files, then every other path a
    # protected, test or hidden rule hits
    tree = worktree if worktree is not None else champion
    if "contract" not in missing:
        if tree is None:
            entries["contract"] = "error: no worktree and no champion checkout to read"
            errors.append("contract: no worktree and no champion checkout to read")
        else:
            def contract_paths():
                _real_folder(tree.path)
                manifest = box.get("manifest")
                if manifest is None:
                    raise _SourceError("the frozen manifest %s cannot be read" % os.path.join(
                        frozen, "files.json"))
                for key in manifest:
                    if key.startswith("files/"):
                        rel = key[len("files/"):]
                        entries["contract:" + rel] = _tree_value(tree.path, rel)
                rules = tuple(contract.protected) + tuple(contract.tests) + tuple(contract.hidden)
                skip_rules = tuple(contract.keep) + tuple(contract.outputs)
                for rel in _protected_walk(tree.path, rules, skip_rules):
                    entries["contract:" + rel] = _tree_value(tree.path, rel)
            _guard(entries, errors, "contract", contract_paths)

    # the clone's refs, config and hooks
    if clone is None:
        if "clone" not in missing:
            entries["clone"] = "error: no clone"
            errors.append("clone: no clone")
    else:
        def clone_parts():
            git_dir = os.path.join(clone.path, ".git")
            for part in _CLONE_PARTS:
                where = os.path.join(git_dir, part)
                value = _file_value(where, stamp=True)
                if value.startswith("dir"):
                    _add_tree(entries, "clone:.git/%s/" % part, where, stamp=True)
                else:
                    if part in ("HEAD", "config") and value == "absent":
                        raise _SourceError("%s: missing" % where)
                    entries["clone:.git/" + part] = value
        _guard(entries, errors, "clone", clone_parts)

    # the round's champion checkout, read-only to every proposer of the round
    if champion is None:
        if "champion" not in missing:
            entries["champion"] = "error: no champion checkout"
            errors.append("champion: no champion checkout")
    else:
        def champion_tree():
            _add_tree(entries, "champion:", champion.path)
            if champion.admin_dir:
                _add_tree(entries, "champion-admin:", champion.admin_dir, skip=_ADMIN_SKIP)
        _guard(entries, errors, "champion", champion_tree)

    # the target repository in the main checkout, under the pack scopes
    repo = _abs(root, contract.repo_path or ".")

    def main_checkout():
        if not os.path.isdir(repo):          # the operator's own path: a link to it is fine
            raise _SourceError("%s: missing" % repo)
        _checkout(entries, "main:", repo, scope=_main_scope(root, contract, repo),
                  drop=_engine_folders(root, repo))
    _guard(entries, errors, "main", main_checkout)

    # the declared shared checkouts and the frozen read paths, each resolved at this point
    for location in contract.shared:
        where = _abs(root, str(location))
        _guard(entries, errors, "shared:" + where,
               lambda where=where: _add_location(entries, "shared:", where))
    for location in (contract.read_real or contract.read_paths):
        where = _abs(root, str(location))
        _guard(entries, errors, "read:" + where,
               lambda where=where: _add_location(entries, "read:", where))

    # every tool the gates call, resolved on the gate PATH, and the PATH folders themselves
    if path_env is None:
        path_env = procs.gate_path(tuple(contract.tools_real))
    for name, pin in resolve_tools(tuple(contract.tools), path_env).items():
        key = "tool:" + name
        if pin.error:
            entries[key] = "error: " + pin.error
            errors.append("%s: %s" % (key, pin.error))
        else:
            _guard(entries, errors, key,
                   lambda key=key, pin=pin: entries.__setitem__(key, _tool_value(pin)))
    if contract.tools:
        for folder in dict.fromkeys(f for f in path_env.split(os.pathsep) if f):
            key = "tool-path:" + folder
            _guard(entries, errors, key,
                   lambda key=key, folder=folder: entries.__setitem__(
                       key, _path_folder_value(folder)))
    return entries, errors


def _save(root: str, run: str, task: str, point: str, entries: dict,
          errors: list) -> FingerprintPoint:
    """Write the point as canonical json with every non-ASCII character escaped (ensure_ascii), not
    util.canonical_json: a key that holds a lone surrogate (a file name that is not valid UTF-8)
    cannot be encoded as UTF-8, and its `\\udcff` escape reads back as the same key."""
    target = _point_path(root, run, task, point)
    fp = FingerprintPoint(run=run, task=task, point=point, taken_at=util.now_iso(),
                          entries=dict(sorted(entries.items())), errors=tuple(errors), path=target)
    util.write_text(target, json.dumps({"run": fp.run, "task": fp.task, "point": fp.point,
                                        "taken_at": fp.taken_at, "entries": fp.entries,
                                        "errors": list(fp.errors)},
                                       sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=True) + "\n")
    return fp


def take(root: str, run: str, task: str, point: str, *, contract: Contract,
         worktree: WorktreeInfo | None, clone: CloneInfo, champion: WorktreeInfo | None,
         path_env: str) -> FingerprintPoint:
    """One point: the contract paths in the worktree (mission_model.observe style, never through
    a symlink: the frozen files and every other path a protected, test or hidden rule hits), the
    frozen contract files, the clone's refs, config and hooks folder, the round's
    champion checkout, the target repo's paths under the pack scopes in the main checkout (HEAD
    plus dirty paths), the declared shared checkouts, and the resolved tools. Saved as
    attempt_dir/fingerprints/<point>.json; a missing source is an error entry.

    worktree is None before the attempt worktree is cut (the open and after-proposer points); the
    contract paths are then read in the champion checkout, the commit the worktree is cut at, so
    the `contract:` keys run on unchanged across the cut. clone or champion None, a tree folder
    that is missing or a link, or a frozen folder whose contract.json, files.json or listed files
    cannot be read, is an error entry (fail closed). point is one of POINTS,
    AUDIT_POINTS or "after-reproducer-writer" (ValueError otherwise)."""
    _check_point(point)
    entries, errors = _collect(root, run, contract=contract, worktree=worktree, clone=clone,
                               champion=champion, path_env=path_env, missing={})
    return _save(root, run, task, point, entries, errors)


def _load_point(path: str) -> FingerprintPoint:
    """A saved point read back (the lifecycle's reopen compare after a restart, and affected())."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return FingerprintPoint(run=data["run"], task=data["task"], point=data["point"],
                            taken_at=data["taken_at"], entries=dict(data["entries"]),
                            errors=tuple(data.get("errors") or ()), path=path)


# ------------------------------------------------------------------ take_attempt (the audit's pair)
def _task_events(conn, kind: str, ref: str) -> list[dict]:
    rows = conn.execute("SELECT id, data FROM events WHERE kind=? AND ref=? ORDER BY id",
                        (kind, ref)).fetchall()
    return [{"id": r[0], "data": json.loads(r[1] or "{}")} for r in rows]


def _recorded_worktree(conn, attempt: str, root: str | None = None) -> WorktreeInfo:
    """The worktree of the attempt's newest research-worktree create, reclaim or rebuild event with
    no error, unless a later destroy follows it (section 3.2). The event records its paths
    relative to the project root (isolation records them so); with `root` each is made absolute
    the way the lifecycle's own worktree() makes it, so a caller whose cwd is elsewhere reads
    the same folders."""
    current = None
    for event in _task_events(conn, "research-worktree", attempt):
        data = event["data"]
        action = data.get("action")
        if action in ("create", "reclaim", "rebuild") and not data.get("error"):
            current = data
        elif action == "destroy":
            current = None
    if current is None:
        raise _SourceError("no live research-worktree event for %s" % attempt)

    def where(location):
        return _abs(root, location) if root and location else location

    return WorktreeInfo(path=where(current["path"]), label=attempt,
                        commit=current.get("commit") or "",
                        admin_dir=where(current.get("admin_dir")),
                        private_objects=where(current.get("private_objects")),
                        index=where(current.get("index")), host_git=where(current.get("host_git")))


def take_attempt(conn, root: str, attempt: str, point: str) -> FingerprintPoint:
    """take() for a caller that holds only the attempt task id: audit.run (t-095) calls it with
    "audit-before" right before the auditor's launch and "audit-after" right after it, then
    compare(before, after); a BROKEN verdict voids the audit (verifier.md section 9.6). point
    must be one of AUDIT_POINTS (ValueError otherwise), so it never writes a lifecycle point's
    file. It reads the record and the disk and writes only the point's json file; it records no
    event. take()'s arguments are built as the lifecycle builds them: run from the attempt's
    task-add `research` object; contract.read_frozen; the worktree of the newest live
    research-worktree event; isolation.open_clone for the line of the research-round-open event
    whose `tasks` hold the attempt; isolation.tree_info for that round's champion checkout; and
    procs.gate_path(contract.tools_real). A part it cannot build is an error entry for that
    source, and compare() reads it as BROKEN (EC-007): the audit is then voided, never passed.
    A task whose task-add has no research run (not an attempt task) raises ValueError, as a point
    outside AUDIT_POINTS does."""
    if point not in AUDIT_POINTS:
        raise ValueError("take_attempt takes only %s, not %r" % (" or ".join(AUDIT_POINTS), point))
    adds = _task_events(conn, "task-add", attempt)
    research = (adds[0]["data"].get("research") or {}) if adds else {}
    run = research.get("run")
    if not run:
        raise ValueError("%s is not an attempt task of a research run" % attempt)
    missing: dict = {}
    contract = worktree = clone = champion = None
    try:
        from alpaca.research import contract as contract_mod
        contract = contract_mod.read_frozen(root, run)
    except Exception as exc:  # noqa: BLE001  (any failure is a missing source: fail closed)
        missing["frozen"] = "the frozen contract cannot be read: %s" % (exc or type(exc).__name__)
    try:
        worktree = _recorded_worktree(conn, attempt, root)
        _real_folder(worktree.path)       # a recorded worktree whose folder is gone or a link
    except _SourceError as exc:
        missing["contract"] = str(exc)
        worktree = None
    opened = None
    for row in conn.execute("SELECT data FROM events WHERE kind=? ORDER BY id",
                            ("research-round-open",)).fetchall():
        data = json.loads(row[0] or "{}")
        if data.get("run") == run and attempt in (data.get("tasks") or ()):
            opened = data
    if opened is None:
        missing["clone"] = "no research-round-open event holds %s" % attempt
        missing["champion"] = missing["clone"]
    else:
        line = opened.get("repair_task") or "champion"
        try:
            clone = isolation.open_clone(root, run, line=line)
            if clone is None:
                missing["clone"] = "the clone of line %s is missing" % line
        except Exception as exc:  # noqa: BLE001
            missing["clone"] = "the clone cannot be opened: %s" % (exc or type(exc).__name__)
        try:
            round_n = int(opened.get("round"))
            champion = isolation.tree_info(
                paths.champion_dir(root, run, round_n, line), commit=opened.get("champion_commit"),
                label="r%d-champion" % round_n, admin=True, read_only=True)
            try:
                _real_folder(champion.path)
            except _SourceError as exc:
                missing["champion"] = "the champion checkout: %s" % exc
                champion = None
        except Exception as exc:  # noqa: BLE001
            missing["champion"] = "the champion checkout cannot be named: %s" % (
                exc or type(exc).__name__)
            champion = None
    path_env = procs.gate_path(tuple(contract.tools_real)) if contract is not None else None
    entries, errors = _collect(root, run, contract=contract, worktree=worktree, clone=clone,
                               champion=champion, path_env=path_env, missing=missing)
    return _save(root, run, attempt, point, entries, errors)


# ------------------------------------------------------------------ compare
def _point_errors(fp: FingerprintPoint) -> tuple:
    """fp.errors, plus each entry whose value is an error ("error: ...") that the list does not
    already name, so a point file whose list was emptied still reads as BROKEN; each prefixed
    with the point."""
    listed = list(fp.errors)
    for key, value in fp.entries.items():
        if isinstance(value, str) and value.startswith("error: "):
            item = "%s: %s" % (key, value[len("error: "):])
            if item not in listed:
                listed.append(item)
    return tuple(_shown("%s %s" % (fp.point, e)) for e in listed)


def compare(before: FingerprintPoint, after: FingerprintPoint) -> TamperVerdict:
    """BROKEN on any changed, added or missing entry, or on an error entry in either point (an
    item of its errors list, or an entry whose value starts with "error: ").
    `changed` lists the keys whose digest differs and the keys only `after` has; `missing` the
    keys only `before` has; `errors` each point's errors, prefixed with its point. ok is decided on
    the keys as saved; the lists show each key with _shown, so a verdict can be recorded. The two
    points must belong to the same run and task (ValueError otherwise)."""
    if (before.run, before.task) != (after.run, after.task):
        raise ValueError("compare needs two points of one attempt: %s/%s against %s/%s"
                         % (before.run, before.task, after.run, after.task))
    old, new = before.entries, after.entries
    changed = [k for k in new if k not in old or old[k] != new[k]]
    missing = [k for k in old if k not in new]
    errors = _point_errors(before) + _point_errors(after)
    return TamperVerdict(point=after.point, ok=not (changed or missing or errors),
                         changed=_shown_all(changed), missing=_shown_all(missing), errors=errors)


# ------------------------------------------------------------------ allowlist
def allowlist_match(path: str, rules: tuple[str, ...]) -> str | None:
    """Returns runbook.path_admitted(path, rules), looked up when called: the editable rule that
    admits path, else None. Not a second matcher: t-088's strict matcher (runbook-format-3.md
    section 14.1), which keeps case, reads every rule from the worktree root and lets a folder
    rule match only that folder at the root. t-096 adds path_admitted to alpaca/runbook.py; the
    name is read here at each call, so importing this module never needs it."""
    return getattr(runbook, "path_admitted")(path, tuple(rules))


def _judge(changed, editable, protected) -> tuple[tuple, tuple]:
    """The changed paths outside the allowlist and those on the protected list, each judged as
    it is and then shown with _shown (a name that is not valid UTF-8 keeps a `\\xff` escape)."""
    outside = _shown_all(p for p in set(changed) if allowlist_match(p, tuple(editable)) is None)
    hit = _shown_all(p for p in set(changed)
                     if barrier.path_is_protected(p, tuple(protected)) is not None)
    return outside, hit


def check_paths(clone: CloneInfo, wt: WorktreeInfo, *, forked_from: str,
                editable: tuple[str, ...], protected: tuple[str, ...]) -> TamperVerdict:
    """First isolation.nested_git(wt): on any entry the verdict is BROKEN with
    TamperVerdict.nested listing them, and no git runs. Otherwise, the paths the worktree changed
    against forked_from: the engine index is read fresh from forked_from (remove wt.index, then
    `git read-tree <forked_from>`); the changed, added or deleted tracked paths are
    `git diff --ignore-submodules=all --name-only -z --no-renames <forked_from>`, and the new
    paths `git ls-files --others --exclude-standard -z`, plus every new `.gitignore` and
    `.gitattributes` file whatever ignores it (the same listing with no exclude rule, limited to
    those names, outside the Python cache folders). Every call reads its attributes from
    forked_from (GIT_ATTR_SOURCE), never from the worktree, so a planted `.gitattributes` (a
    `text`, eol or working-tree-encoding rule) cannot make a rewritten tracked file read as clean,
    and a planted self-ignoring `.gitignore` is itself a changed path. Each path outside the
    allowlist, or on the protected list (barrier.path_is_protected), makes the verdict BROKEN with
    the paths listed. No `git status`: it reads the admin HEAD and index, which the agent writes.
    Every git call runs with isolation.git_env(clone, worktree=wt) and GIT_HARDEN, with the
    worktree root as its cwd. A walk, git_env or git call that fails is an error: BROKEN (fail
    closed). The verdict's point is "after-implementer" (section 7.2, rows 18 and 19), a default
    only: a caller that runs the check after another writing agent (the repair task's reproducer
    writer) records the verdict under its own point, dataclasses.replace(verdict, point=...)."""
    point = "after-implementer"
    try:
        nested = tuple(isolation.nested_git(wt))
    except Exception as exc:  # noqa: BLE001  (fail closed)
        return TamperVerdict(point=point, ok=False,
                             errors=(_shown("nested-git walk: %s" % (exc or type(exc).__name__)),))
    if nested:
        return TamperVerdict(point=point, ok=False, nested=_shown_all(nested))
    try:
        env = dict(isolation.git_env(clone, worktree=wt))
        # attributes from forked_from, never from the worktree: a planted .gitattributes (`text`,
        # an eol or a working-tree-encoding rule) would make git read a rewritten file as clean
        env["GIT_ATTR_SOURCE"] = forked_from
        index = env.get("GIT_INDEX_FILE") or wt.index
        if index and os.path.lexists(index):
            os.unlink(index)
        _git(["read-tree", forked_from], cwd=wt.path, env=env)
        changed = set(_nul_list(_git(["diff", "--ignore-submodules=all", "--name-only", "-z",
                                      "--no-renames", forked_from, "--"], cwd=wt.path, env=env)))
        changed.update(_nul_list(_git(["ls-files", "--others", "--exclude-standard", "-z"],
                                      cwd=wt.path, env=env)))
        # a planted .gitignore that ignores itself hides itself and the files beside it from the
        # listing above; list every new ignore and attribute file with no exclude rule at all,
        # outside the Python cache folders (pytest writes a self-ignoring .pytest_cache/.gitignore)
        for rel in _nul_list(_git(["ls-files", "--others", "-z", "--",
                                   ":(top,glob)**/.gitignore", ":(top,glob)**/.gitattributes"],
                                  cwd=wt.path, env=env)):
            if not any(part in _CACHE_DIRS for part in rel.split("/")[:-1]):
                changed.add(rel)
    except Exception as exc:  # noqa: BLE001  (fail closed)
        return TamperVerdict(point=point, ok=False,
                             errors=(_shown("host git: %s" % (exc or type(exc).__name__)),))
    outside, hit = _judge(changed, editable, protected)
    return TamperVerdict(point=point, ok=not (outside or hit), outside=outside, protected=hit,
                         changed=_shown_all(sorted(changed)))


def check_diff(capture: DiffCapture, *, editable: tuple[str, ...],
               protected: tuple[str, ...]) -> TamperVerdict:
    """The same allowlist and protected check over capture.paths, the paths `git diff-tree` names
    between forked_from and head, plus a gitlink check: every entry of capture.gitlinks (a nested
    repository `git add -A` recorded as mode 160000) makes the verdict BROKEN with
    TamperVerdict.nested listing it, whatever the allowlist says. Pure: no git call and no read of
    the worktree. The verdict's point is "capture" (section 7.2, rows 21 and 23a), which is not a
    fingerprint point: the lifecycle records this verdict in research-diff, not as a
    research-tamper, and names the point there if it needs one. capture.paths is only as good as
    capture's own git calls: its `git add -A` reads the worktree's .gitattributes unless it runs
    with GIT_ATTR_SOURCE set to forked_from, as check_paths does, and a planted `text` or
    working-tree-encoding rule would then stage a rewritten file as its old blob (t-091's
    capture needs that pin)."""
    outside, hit = _judge(tuple(capture.paths), editable, protected)
    nested = _shown_all(capture.gitlinks)
    return TamperVerdict(point="capture", ok=not (outside or hit or nested), outside=outside,
                         protected=hit, nested=nested)


# ------------------------------------------------------------------ affected slots
# Sources that are one thing for every slot of the run: each maps its key prefix to the error
# entry _collect writes when it cannot be read. A key of one of them that a point does not hold (a
# file planted after the point) still names a source the point read.
_WHOLE_SOURCES = {"frozen:": "frozen", "clone:": "clone", "champion:": "champion",
                  "champion-admin:": "champion", "main:": "main"}
_LOCATION_SOURCES = ("shared:", "read:")


def _location_root(prefix: str, key: str, value: str) -> str | None:
    """The key stem of the declared location an entry of a point shows was read, else None: its
    base folder entry (prefix + <resolved path> + "/.", _add_tree with stamp), its link entry
    (value "resolves-to <resolved path>") or its error entry (prefix + <declared path>)."""
    if key.endswith("/.") and value.startswith("dir"):
        return key[:-1]
    if value.startswith("resolves-to "):
        return prefix + value[len("resolves-to "):].rstrip("/") + "/"
    if value.startswith("error: "):
        return key.rstrip("/") + "/"
    return None


def _key_in(entries: dict, source_key: str) -> bool:
    """True when the saved point entries read the source that source_key names: the point holds
    the key itself, or keys under it as a folder stem, or else the source the key belongs to. A
    frozen:, clone:, champion:, champion-admin: or main: key belongs to that one source, which the
    point read when it holds any key with the prefix or the source's error entry. A shared: or
    read: key belongs to the declared location it lies under (_location_root). So a file a slot
    planted, whose key no earlier point holds, still flags every slot that reads its source. A
    contract:, tool: or tool-path: key matches only itself or its stem: every point that reads a
    tool or a PATH folder holds its key. contract: keys are each attempt's own tree, so affected()
    never passes one here."""
    if source_key in entries:
        return True
    stem = source_key.rstrip("/") + "/"
    if any(k.startswith(stem) for k in entries):
        return True
    for prefix, error_key in _WHOLE_SOURCES.items():
        if source_key.startswith(prefix):
            return any(k == error_key or k.startswith(prefix) for k in entries)
    for prefix in _LOCATION_SOURCES:
        if source_key.startswith(prefix):
            for key, value in entries.items():
                root = _location_root(prefix, key, str(value)) if key.startswith(prefix) else None
                if root is not None and (source_key.startswith(root)
                                         or source_key.rstrip("/") + "/" == root):
                    return True
            return False
    return False


def affected(conn, run: str, source_key: str, *, since_event: int) -> list[str]:
    """Attempt tasks of the run whose open fingerprint window covers a change of one shared
    source, so every affected slot is flagged (FR-016, EC-047).

    The shared sources are the key prefixes frozen:, clone:, champion:, champion-admin:, main:,
    shared:, read:, tool: and tool-path:. A contract: key names a path of one attempt's own tree
    (its worktree, or before the cut the champion checkout, whose change also shows under a
    champion: key), which no other slot reads, so a contract: key returns [] (the slot's own
    compare already marks it broken). Each recorded research-tamper event's `path` must be the
    saved point file as take() returns it (FingerprintPoint.path, an absolute path): affected()
    opens it as recorded and takes no root, and a path it cannot open keeps the slot (fail
    closed). When the row also carries a `sha256`, the file must hash to it, or the slot is kept
    (fail closed): a point file rewritten after its row was recorded cannot drop a slot.

    Only attempt tasks are slots: a task whose task-add `research` object names the run with role
    `attempt` (section 8.2, row 1 of section 7.2; a round 0 baseline task has that role too). A
    repair task (role `repair`) and a report task (role `report`) are never returned, and a
    research-tamper or research-attempt row whose task has no such task-add is left out.

    The change lies after the event since_event (the last point that still saw the old digest)
    and before now. A slot's window opens at its task-add, which the record holds before any
    point is taken, so a slot whose point is saved but whose research-tamper row is not recorded
    yet is still found. The window shuts at the task's `close` point or its research-attempt
    event, whichever comes first; it covers the change unless it shut at or before since_event.
    A slot is kept only when its newest recorded point read the source of source_key (_key_in:
    the key, a folder stem whose keys start with it, or the source the key belongs to, so a file
    one slot planted flags every slot that reads that source although no earlier point holds its
    key), so a slot of another line or round that never read the source is not flagged; a slot
    with no recorded point yet, or whose point file cannot be read or does not match its row, is
    kept (fail closed). Returns the task ids in the order their windows opened, the caller's own
    task included. Reads the record and the saved points; writes nothing."""
    if source_key.startswith("contract:"):
        return []
    opened: dict = {}
    shut: dict = {}
    newest: dict = {}
    rows = conn.execute("SELECT id, ref, kind, data FROM events WHERE kind IN (?, ?, ?) "
                        "ORDER BY id", ("task-add", events.TAMPER, events.ATTEMPT)).fetchall()
    for event_id, ref, kind, raw in rows:
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            continue
        if not ref or not isinstance(data, dict):
            continue
        if kind == "task-add":
            research = data.get("research")
            if (isinstance(research, dict) and research.get("run") == run
                    and research.get("role") == "attempt"):
                opened.setdefault(ref, event_id)
            continue
        if data.get("run") != run or ref not in opened:
            continue
        if kind == events.TAMPER:
            newest[ref] = data
            if data.get("point") == "close":
                shut.setdefault(ref, event_id)
        else:
            shut.setdefault(ref, event_id)
    found = []
    for task in sorted(opened, key=opened.get):
        end = shut.get(task)
        if end is not None and end <= since_event:
            continue
        try:
            if task not in newest:
                raise OSError("no point recorded yet")
            saved = newest[task].get("path")
            if not saved:
                raise OSError("no point file recorded")
            want = newest[task].get("sha256")
            if want:
                with open(saved, "rb") as fh:
                    if hashlib.sha256(fh.read()).hexdigest() != want:
                        raise ValueError("the point file does not match its row")
            keep = _key_in(_load_point(saved).entries, source_key)
        except (OSError, ValueError, KeyError, TypeError):
            keep = True
        if keep:
            found.append(task)
    return found
