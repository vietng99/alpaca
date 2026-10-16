"""`alpaca attach study`: the codebase map of the attached repository (op-062 t-652, R13, AC-14).

The map is a markdown file in the studio, `design/attach/codebase-map.md`, written by scout
subagents the main session runs. This module does the parts that need no agent and that decide
whether the map can be trusted:

  prepare   fetch the base branch, export a read-only snapshot of the base tree at one commit into
            `.alpaca/attach/study/<sha12>/`, store a few command outputs (`git log`, `shortlog`, the
            file list) under `.alpaca/attach/study/outputs/<sha12>/`, write a map skeleton when there
            is none and flag the areas an older map has gone stale for.
  check     every bullet under an `## Area:` heading carries a pointer, and every pointer resolves.
            Only blank lines, comment-only lines and bullets (with their indented continuation lines)
            are allowed inside an area; any other line is a problem, so no line is skipped.
  ingest    one wiki document per area (`wiki/attach/map/<area>/<text id>.md`, volatile) through the
            wiki's own write door. The map is read once, under the study lock, parsed once, and each
            document is built from the bullets that parse checked and from nothing else; after the
            wiki stored a document its vault copy is compared with the text that was checked. A
            replaced version is retired.
  stale     the areas whose paths changed between the map's commit and the newest base `prepare` recorded;
            the flag is kept per map path.

Map format (the build plan, section 6.6):

    ---
    attach_sha: "<40 hex>"
    areas:
      - id: firmware-core
        paths: ["Core/", "QTRM-App/"]
    ---
    ## Area: firmware-core
    - The entry point is main(). [ptr: app@Core/Src/main.c:12-30]
    - 57 commits on main. [ptr: out:.alpaca/attach/study/outputs/<sha12>/git-log.txt:1]

A pointer is one of two forms and nothing else (a `cmd:` pointer or a bare path is refused, because a
command cannot be run again later and a bare path says nothing about which version it meant):

  app@<path>:<n>  or  app@<path>:<a>-<b>   a line (range) of a file of the attached repository at the
                                            map's `attach_sha`, read with git from the object store,
                                            never from the `app/` checkout, which moves on (P21)
  out:<outputs>/<sha12>/<name>:<n>          a line of the output `<name>` (git-log.txt, shortlog.txt or
                                            files.txt) of the map's commit, where <outputs> is
                                            `.alpaca/attach/study/outputs` and <sha12> is the first 12
                                            characters of `attach_sha`. The checker recomputes the output
                                            from the object store at `attach_sha`; the file on disk is
                                            for the scouts to read and plays no part in the check

An `app@` pointer is refused when the path is absolute, holds `..`, `.` or an empty part, is a symlink
or a submodule in the tree, is not a file, is binary, names a private runtime file (`.env`, keys,
databases), when the line is 0 or past the end, or when the path lies outside the paths of the area
that cites it (otherwise the stale flag could not see a change to it).

Which commit a pointer may name, and why its content can be trusted. The clone and the studio's state files
are writable by the agents that run in the studio, so nothing they say about themselves is taken as given:

  * `attach_sha` must equal a base that `prepare` recorded (an `attach-study` event). `prepare` takes the base
    id from the upstream (`git ls-remote attach.url`, run with no repository so the clone's config is not read),
    fetches, and refuses a ref that names another id. `--no-fetch` uses the newest recorded base and reads no
    ref. A commit made in the clone, a local branch, an older commit nobody studied: refused.
  * Every object read for a pointer, a snapshot, an output or a stale diff goes through `git cat-file --batch`
    and is hashed in Python (`sha1("<type> <size>\\0" + bytes)` must equal its id). Trees and commits are parsed
    here, and `git-log.txt`, `shortlog.txt`, `files.txt` and the stale diff are computed from them. No `git log`,
    `ls-tree`, `diff` or `merge-base` is run (a test pins the subcommands), so the clone's commit-graph, index
    and grafts are not read, and an object that does not hash to its id is refused whether it is loose or packed.
    SHA-1 collisions are not detected by `hashlib`.
  * The anchors are `attach.url` in project.yaml (human-owned) and the record. A process that writes
    `.alpaca/alpaca.db` directly is outside what this module sees.

`--map` is a `.md` file under `design/`. The stale flag is kept per map path (`stale.json`).

Limits, stated plainly: a pointer proves that the cited line exists in the recorded commit, not that
the claim written beside it is true; that stays with the scouts and the reviewers of the map. The
snapshot is read-only by file mode, not by mount. The wiki keeps retired documents as history; they
leave the current indexes.

Every file this module writes goes through `safefs`; every git call goes through `gitops`.
"""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import fnmatch
import hashlib
import heapq
import json
import os
import re
import stat
from dataclasses import dataclass, field

from alpaca.attach import AttachError, STATE_DIR, config, gitops, health, proven, safefs

MAP_PATH = "design/attach/codebase-map.md"
STUDY_DIR = STATE_DIR + "/study"
OUTPUT_DIR = STUDY_DIR + "/outputs"
WIKI_SRC_DIR = STUDY_DIR + "/wiki"
STALE_PATH = STUDY_DIR + "/stale.json"
SERVED_PATH = STUDY_DIR + "/served.json"     # written by ingest only: what the wiki serves
WIKI_PREFIX = "wiki/attach/map/"

MAX_MAP_BYTES = 2 << 20
MAX_POINTER_BLOB = 16 << 20
MAX_SNAPSHOT_BYTES = 1 << 30
MAX_SNAPSHOT_FILES = 250000
MAX_AREAS = 40
MAX_PROBLEMS = 200
LOG_LIMIT = 500
FILES_LIMIT = 20000
BINARY_PROBE = 8000
BATCH_BYTES = 64 << 20
#: bounds on a hostile map: a line, the number of lines, the front matter, one pointer, the log scan
MAX_LINE = 4000
MAX_LINES = 20000
MAX_FRONT_BYTES = 32 << 10
MAX_POINTER_TEXT = 1000
MAX_AREA_ID = 64
SHORTLOG_LIMIT = 200000
HISTORY_EVENTS = 2000
#: bounds on a tree: distinct folders read, paths listed (one subtree can be named many times), paths a diff may touch
MAX_TREES = 500000
MAX_TREE_ENTRIES = 1000000
MAX_CHANGED = 1000000
#: the command outputs prepare stores and the checker recomputes, by name
OUTPUT_NAMES = ("git-log.txt", "shortlog.txt", "files.txt")

_SHA = re.compile(r"[0-9a-f]{40}")
_SHA_LINE = re.compile(r"^attach_sha:[ \t]*[\"']?([0-9a-f]{40})[\"']?[ \t]*$", re.M)
_AREA_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# Every pattern below is anchored and has no nested or overlapping repetition, so it is linear in the
# length of the line it is run on; the line is at most MAX_LINE characters when it is matched.
_AREA_HEAD = re.compile(r"^##[ \t]+Area:(.*)$")
_H2 = re.compile(r"^#{1,2}\s")
_SUBHEAD = re.compile(r"^#{3,6}\s")
_BULLET = re.compile(r"^(\s*)(?:[-*+]|\d{1,3}[.)])\s+(.*)$")
_POINTER = re.compile(r"^(?P<kind>app@|out:)(?P<path>.+):(?P<a>[0-9]{1,7})(?:-(?P<b>[0-9]{1,7}))?$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f\\]")
#: characters a line splitter other than ours would read as the end of a line (NUL and the C0 controls but tab)
_LINE_BREAKS = re.compile("[\x00-\x08\x0a-\x1f\x7f\x85\u2028\u2029]")
#: file names (last part of a pointer path) that hold credentials even when `paths.private_runtime` is silent
_PRIVATE_NAMES = (".npmrc", ".pypirc", ".netrc", ".htpasswd", "*.p12", "*.pfx", "*.jks", "*.keystore", "secrets.*",
                  "secret.*", "credentials.json", "credentials.yml", "credentials.yaml")
_PRIVATE_DIRS = (".ssh", ".aws", ".gnupg", ".kube")


@dataclass
class MapCheck:
    ok: bool
    problems: list
    claims: int
    sha: str


@dataclass
class PointerCheck:
    ok: bool
    reason: str = ""


class _Bad(Exception):
    """A pointer or a map rule that does not hold; str(exc) is the one-line reason."""


# ------------------------------------------------------------------------------------ the objects

class _Missing(AttachError):
    """An object the clone does not hold."""


class _Corrupt(AttachError):
    """An object whose bytes do not hash to its id, or whose shape is not git's."""


def _git(app, *args, **kw):
    """`gitops.run` that reads the clone as its recorded objects say, not as the clone has been bent:
    replacement objects are off (`--no-replace-objects`, a global option, so it goes ahead of the subcommand)
    and the graft file is switched off (`GIT_GRAFT_FILE=/dev/null`), and a missing object is not fetched from a
    promisor remote the clone's config names (`GIT_NO_LAZY_FETCH`). Every object a pointer, a snapshot, an
    output or a stale diff depends on is read through `_Objects`, which hashes what git returns."""
    kw["env"] = gitops.clean_env({"GIT_GRAFT_FILE": os.devnull, "GIT_NO_LAZY_FETCH": "1"})
    return gitops.run(app, "--no-replace-objects", *args, **kw)


def _recorded_bases(root, *, readonly=False):
    """The commits `prepare` recorded as the base it studied, newest first (an `attach-study` event whose action
    is `prepare`, written by the verb). Only prepare events count against the window, so later `stale` and
    `ingest` events never push an old base out of it. `readonly` opens the record without migrations (the
    status read, which writes nothing)."""
    from alpaca import db
    conn = db.connect_readonly(str(root)) if readonly else db.connect(str(root))
    try:
        try:
            rows = [json.loads(r[0]) for r in conn.execute(
                "SELECT data FROM events WHERE kind='attach-study' AND json_extract(data, '$.action')='prepare' "
                "ORDER BY id DESC LIMIT ?", (HISTORY_EVENTS,))]
        except Exception:                                    # noqa: BLE001 - an sqlite without json_extract
            rows = [e.get("data") for e in reversed(db.events(conn, kind="attach-study", limit=HISTORY_EVENTS))]
    finally:
        conn.close()
    out = []
    for data in rows:
        if isinstance(data, dict) and data.get("action") == "prepare":
            sha = data.get("sha")
            if isinstance(sha, str) and _SHA.fullmatch(sha) and sha not in out:
                out.append(sha)
    return out


def _latest_base(root, *, readonly=False):
    """The base the newest `prepare` recorded, or None. Nothing in the clone (a ref, a config key) names a base."""
    bases = _recorded_bases(root, readonly=readonly)
    return bases[0] if bases else None


_FORGED = ("the object %s in the clone does not hash to its own id; the clone was changed by hand, "
           "fetch it again")


def _parse_batch(data, oids):
    """{oid: (type, bytes) or None} from the output of `git cat-file --batch` for `oids`, in order. Every
    object is hashed (`sha1("<type> <size>\\0" + bytes)`) and must equal the id it was asked for: git does not
    do this when it reads a loose or packed object, so an object file rewritten under a real id would be
    served as that id (and when a loose and a packed copy disagree, git prints the size of one and the bytes of
    the other). A mismatch, a size that disagrees with the bytes, a record in the wrong place or bytes left
    over raise _Corrupt."""
    out, pos = {}, 0
    for oid in oids:
        nl = data.find(b"\n", pos)
        if nl < 0:
            raise _Corrupt("git cat-file ended before the object %s" % oid[:12])
        head = data[pos:nl].split(b" ")
        if len(head) == 2 and head[1] == b"missing" and head[0] == oid.encode("ascii"):
            out[oid] = None
            pos = nl + 1
            continue
        if len(head) != 3 or head[0] != oid.encode("ascii") or not head[2].isdigit():
            raise _Corrupt(_FORGED % oid[:12])
        size = int(head[2])
        body = data[nl + 1:nl + 1 + size]
        if len(body) != size or data[nl + 1 + size:nl + 2 + size] != b"\n":
            raise _Corrupt(_FORGED % oid[:12])
        if hashlib.sha1(head[1] + b" %d\0" % size + body).hexdigest() != oid:
            raise _Corrupt(_FORGED % oid[:12])
        out[oid] = (head[1].decode("ascii", "replace"), body)
        pos = nl + 2 + size
    if pos != len(data):
        raise _Corrupt("git cat-file answered with more than was asked for")
    return out


_MODE = re.compile(rb"[0-7]{5,6}")


def _parse_tree(body):
    """[(mode, kind, oid, name)] of a tree object in stored order. mode is six digits ("040000"), kind is
    tree, commit (a submodule) or blob (a file or a link)."""
    out, pos, n = [], 0, len(body)
    while pos < n:
        sp = body.find(b" ", pos)
        nul = body.find(b"\0", sp + 1) if sp >= 0 else -1
        if sp < 0 or nul < 0 or nul + 21 > n or not _MODE.fullmatch(body[pos:sp]):
            raise _Corrupt("a tree object of the clone is malformed")
        name = body[sp + 1:nul]
        if not name or b"/" in name:
            raise _Corrupt("a tree object of the clone holds the entry name %r" % name[:40])
        mode = "%06o" % int(body[pos:sp], 8)
        kind = "tree" if mode == "040000" else "commit" if mode == "160000" else "blob"
        out.append((mode, kind, body[nul + 1:nul + 21].hex(), name.decode("utf-8", "surrogateescape")))
        pos = nul + 21
    return out


_CLEAN_FIELD = re.compile("[\x00-\x1f\x7f\x85\u2028\u2029]")


def _field(text, limit):
    """`text` as one line of an output: control characters and line separators become spaces, cut at `limit`."""
    return _CLEAN_FIELD.sub(" ", text)[:limit]


@dataclass
class _Commit:
    tree: str
    parents: list
    author: str
    adate: str
    ctime: int
    subject: str


def _ident(raw):
    """(name, unix seconds, utc offset in minutes) from the text after `author ` or `committer `. Linear;
    a line that does not parse gets a name of the whole text and the epoch."""
    text = raw.decode("utf-8", "replace")
    parts = text.rsplit(" ", 2)
    if len(parts) != 3:
        return text.strip(), 0, 0
    who, stamp, zone = parts
    lt = who.rfind("<")
    name = (who[:lt] if lt >= 0 else who).strip()
    try:
        ts = int(stamp)
    except ValueError:
        ts = 0
    offset = 0
    if re.fullmatch(r"[+-][0-9]{4}", zone):
        offset = (int(zone[1:3]) * 60 + int(zone[3:5])) * (-1 if zone[0] == "-" else 1)
    return name, ts, offset


def _short_date(ts, offset):
    try:
        zone = datetime.timezone(datetime.timedelta(minutes=offset))
        return datetime.datetime.fromtimestamp(ts, zone).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "0000-00-00"


def _parse_commit(oid, body):
    """The fields the outputs need, from the raw commit object (its headers up to the first blank line)."""
    head, _, message = body.partition(b"\n\n")
    lines = head.split(b"\n")
    if len(lines) < 2 or not lines[0].startswith(b"tree "):
        raise _Corrupt("the commit %s has no tree line" % oid[:12])
    tree = lines[0][5:].decode("ascii", "replace")
    if not _SHA.fullmatch(tree):
        raise _Corrupt("the commit %s names no tree" % oid[:12])
    parents, i = [], 1
    while i < len(lines) and lines[i].startswith(b"parent "):
        parent = lines[i][7:].decode("ascii", "replace")
        if not _SHA.fullmatch(parent):
            raise _Corrupt("the commit %s names a parent that is not an id" % oid[:12])
        if parent not in parents:
            parents.append(parent)
        i += 1
    author = committer = None
    for line in lines[i:]:
        if line.startswith(b" "):
            continue
        if author is None and line.startswith(b"author "):
            author = line[7:]
        elif committer is None and line.startswith(b"committer "):
            committer = line[10:]
    name, ts, offset = _ident(author if author is not None else b"")
    ctime = _ident(committer if committer is not None else b"")[1]
    subject = " ".join(part.strip() for part in message.lstrip(b"\n").split(b"\n\n", 1)[0].decode("utf-8", "replace").split("\n"))
    return _Commit(tree, parents, _field(name, 200), _short_date(ts, offset), ctime, _field(subject.strip(), 500))


class _Objects:
    """The objects of the clone, read through `git cat-file --batch` and proved by their hash. Trees and
    commits are parsed here in Python, so no output, listing or diff depends on what `git log`, `ls-tree` or
    `diff` would say about the clone's own commit-graph, replace refs, grafts or index."""

    def __init__(self, app):
        self.app = app
        self._format_ok = False
        self.trees = {}
        self._dicts = {}
        self._commits = {}
        self._history = {}

    def _batch(self, oids):
        if not oids:
            return {}
        if not self._format_ok:
            res = _git(self.app, "rev-parse", "--show-object-format")
            if res.rc == 0 and res.out.strip() not in ("", "sha1"):
                raise AttachError("the clone uses the %s object format; attach study reads sha1 repositories" % res.out.strip()[:20])
            self._format_ok = True
        res = _git(self.app, "cat-file", "--batch", input="\n".join(oids) + "\n", binary=True, timeout=600)
        if res.rc != 0:
            raise AttachError("git cat-file failed while reading the clone")
        return _parse_batch(res.out, oids)

    def read_many(self, oids, kind):
        """{oid: bytes} of objects of type `kind`. _Missing when the clone lacks one, _Corrupt when one does
        not hash to its id or is of another type."""
        out = {}
        for oid, item in self._batch(list(dict.fromkeys(oids))).items():
            if item is None:
                raise _Missing("the object %s is not in the clone" % oid[:12])
            if item[0] != kind:
                raise _Corrupt("the object %s is a %s, not a %s" % (oid[:12], item[0], kind))
            out[oid] = item[1]
        return out

    def read(self, oid, kind):
        return self.read_many([oid], kind)[oid]

    def sizes(self, oids):
        """{oid: size or None} of blobs, from `cat-file --batch-check`. Only for sizing a read: the read itself
        hashes the bytes, which fixes the size."""
        oids = list(dict.fromkeys(oids))
        res = _git(self.app, "cat-file", "--batch-check", input="\n".join(oids) + "\n", binary=True, timeout=600)
        if res.rc != 0:
            raise AttachError("git cat-file failed while sizing the clone's objects")
        lines = res.out.split(b"\n")
        out = {}
        for i, oid in enumerate(oids):
            parts = lines[i].split(b" ") if i < len(lines) else []
            if len(parts) == 3 and parts[0] == oid.encode("ascii") and parts[1] == b"blob" and parts[2].isdigit():
                out[oid] = int(parts[2])
            else:
                out[oid] = None
        return out

    def load_trees(self, oids):
        need = [o for o in dict.fromkeys(oids) if o not in self.trees]
        if need:
            for oid, body in self.read_many(need, "tree").items():
                self.trees[oid] = _parse_tree(body)

    def entries(self, oid):
        """{name: (mode, kind, oid)} of one tree."""
        if oid not in self._dicts:
            self.load_trees([oid])
            self._dicts[oid] = {name: (mode, kind, o) for mode, kind, o, name in self.trees[oid]}
        return self._dicts[oid]

    def commit(self, oid):
        if oid not in self._commits:
            self._commits[oid] = _parse_commit(oid, self.read(oid, "commit"))
        return self._commits[oid]

    def all_entries(self, tree_oid):
        """[(mode, kind, oid, path)] of every file, link and submodule below `tree_oid`, in git's order. The
        trees are read a level at a time; the number of entries is bounded (a tree can name one subtree many
        times, so a few objects can stand for far more paths)."""
        pending, seen = [tree_oid], set()
        while pending:
            self.load_trees(pending)
            seen.update(pending)
            if len(seen) > MAX_TREES:
                raise AttachError("the tree holds more than %d folders" % MAX_TREES)
            nxt = []
            for oid in pending:
                nxt += [e[2] for e in self.trees[oid] if e[1] == "tree" and e[2] not in seen]
            pending = list(dict.fromkeys(nxt))
        out, stack = [], [(iter(self.trees[tree_oid]), "")]
        while stack:
            it, prefix = stack[-1]
            for mode, kind, oid, name in it:
                if kind == "tree":
                    stack.append((iter(self.trees[oid]), prefix + name + "/"))
                    break
                out.append((mode, kind, oid, prefix + name))
                if len(out) > MAX_TREE_ENTRIES:
                    raise AttachError("the tree names more than %d paths" % MAX_TREE_ENTRIES)
            else:
                stack.pop()
        return out

    def history(self, tip):
        """[(oid, _Commit)] of the commits reachable from `tip`, newest first and every child before its parents
        (`git log --date-order` in Python, ties broken by id), at most SHORTLOG_LIMIT of them. One pure function
        of the verified commits; no `git log`, so the clone's commit-graph and grafts play no part."""
        if tip in self._history:
            return self._history[tip]
        seen, frontier = {}, [tip]
        while frontier and len(seen) < SHORTLOG_LIMIT:
            wave = [o for o in dict.fromkeys(sorted(frontier)) if o not in seen][:SHORTLOG_LIMIT - len(seen)]
            if not wave:
                break
            frontier = []
            for oid, body in self.read_many(wave, "commit").items():
                seen[oid] = _parse_commit(oid, body)
                frontier += seen[oid].parents
        waiting = {oid: 0 for oid in seen}
        for commit in seen.values():
            for parent in commit.parents:
                if parent in waiting:
                    waiting[parent] += 1
        heap = [(-seen[o].ctime, o) for o, n in waiting.items() if n == 0]
        heapq.heapify(heap)
        order = []
        while heap:
            _, oid = heapq.heappop(heap)
            order.append((oid, seen[oid]))
            for parent in seen[oid].parents:
                if parent in waiting:
                    waiting[parent] -= 1
                    if waiting[parent] == 0:
                        heapq.heappush(heap, (-seen[parent].ctime, parent))
        if len(order) != len(seen):
            raise _Corrupt("the history of %s is not acyclic" % tip[:12])
        self._history[tip] = order
        return order


# ------------------------------------------------------------------------------------ the source

class _Source:
    """The attached repository at one commit, read from verified objects only. Never touches the checkout.

    A commit is accepted when it is a base that `prepare` recorded, exactly: the id came from the upstream
    (`_upstream_sha`, `git ls-remote` with no repository) and the commit, its tree and every blob below it are
    proved by their hash, so what a pointer reads is what the upstream holds at that id. A commit made inside
    the clone, a local branch, an older commit nobody studied: refused. `trusted=True` is for `prepare` itself,
    which reads the commit it is about to record."""

    def __init__(self, root, sha, *, trusted=False):
        self.root = str(root)
        self.cfg = config.load(root)
        problems = health.repo_problems(root, self.cfg.repo)
        if problems:
            raise AttachError(problems[0])
        self.app = str(self.cfg.repo_path(root))
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise AttachError("the map's attach_sha must be 40 lowercase hex characters")
        self.sha = sha
        self.objs = _Objects(self.app)
        try:
            self.tree = self.objs.commit(sha).tree
        except _Missing:
            raise AttachError("commit %s is not in %s/; run alpaca attach study to fetch it" % (sha[:12], self.cfg.repo)) from None
        if not trusted and sha not in _recorded_bases(root):
            raise AttachError("commit %s is not a base that alpaca attach study recorded for %s/; "
                              "a map names a commit that prepare studied" % (sha[:12], self.cfg.repo))
        self._dirs = {}
        self._blobs = {}
        self._outputs = {}

    def listing(self, parent):
        """{name: (mode, type, object sha)} of the folder `parent` ("" is the top) at the commit."""
        if parent in self._dirs:
            return self._dirs[parent]
        gone = _Bad("%s does not exist at commit %s (or a part of it is a file or a link)" % (parent, self.sha[:12]))
        try:
            oid = self.tree
            for part in parent.split("/") if parent else []:
                found = self.objs.entries(oid).get(part)
                if found is None or found[1] != "tree":
                    raise gone
                oid = found[2]
            out = dict(self.objs.entries(oid))
        except AttachError as exc:
            raise _Bad(str(exc)) from None
        self._dirs[parent] = out
        return out

    def lines(self, path):
        """Number of lines of the regular text file `path` at the commit, or _Bad."""
        parent, _, name = path.rpartition("/")
        found = self.listing(parent).get(name)
        if found is None:
            raise _Bad("%s does not exist at commit %s" % (path, self.sha[:12]))
        mode, kind, obj = found
        if mode == "120000":
            raise _Bad("%s is a symbolic link in the tree; point at the file it names" % path)
        if mode == "160000":
            raise _Bad("%s is a submodule, not a file of this repository" % path)
        if kind != "blob" or mode not in ("100644", "100755"):
            raise _Bad("%s is not a regular file" % path)
        if obj not in self._blobs:
            self._blobs[obj] = self._count(path, obj)
        count = self._blobs[obj]
        if isinstance(count, _Bad):
            raise count
        return count

    def _count(self, path, obj):
        try:
            size = self.objs.sizes([obj])[obj]
            if size is None:
                return _Bad("%s cannot be read from the object store" % path)
            if size > MAX_POINTER_BLOB:
                return _Bad("%s is larger than %d bytes; point at a smaller file" % (path, MAX_POINTER_BLOB))
            data = self.objs.read(obj, "blob")
        except AttachError as exc:
            return _Bad("%s cannot be read from the object store: %s" % (path, exc))
        if b"\0" in data[:BINARY_PROBE]:
            return _Bad("%s is a binary file; a pointer needs text lines" % path)
        return data.count(b"\n") + (0 if not data or data.endswith(b"\n") else 1)

    def output(self, name):
        """The bytes of the command output `name` for this commit, recomputed from the verified objects."""
        if name not in self._outputs:
            try:
                self._outputs[name] = _output_bytes(self.app, self.sha, name, objs=self.objs)
            except AttachError as exc:
                raise _Bad(str(exc)) from None
        return self._outputs[name]


def _output_bytes(app, sha, name, objs=None):
    """The command output `name` as a pure function of the commit `sha`: computed in Python from the commit
    and tree objects, each proved by its hash (no `git log`, no mailmap, no abbreviation, no signature or
    notes lines, no decoration, no wall clock), so the same commit gives the same bytes on every run.
    `prepare` stores these bytes and the checker recomputes them.

      git-log.txt   `<id> <author date> <author name> <subject>` per commit, newest first, children before
                    parents, the first LOG_LIMIT of them
      shortlog.txt  `<count>\\t<author name>` per author, by count then name
      files.txt     every path of the tree, in git's order, the first FILES_LIMIT of them"""
    if name not in OUTPUT_NAMES:
        raise AttachError("%s is not an output alpaca attach study makes (%s)" % (name, ", ".join(OUTPUT_NAMES)))
    objs = objs or _Objects(app)
    try:
        if name == "git-log.txt":
            text = "\n".join("%s %s %s %s" % (oid, c.adate, c.author, c.subject)
                             for oid, c in objs.history(sha)[:LOG_LIMIT])
        elif name == "shortlog.txt":
            counts = {}
            for _, c in objs.history(sha):
                counts[c.author] = counts.get(c.author, 0) + 1
            text = "\n".join("%6d\t%s" % (n, who) for who, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
        else:
            text = "\n".join(e[3] for e in objs.all_entries(objs.commit(sha).tree)[:FILES_LIMIT])
    except _Missing as exc:
        raise AttachError("the history of %s is not in the clone (%s); run alpaca attach study" % (sha[:12], exc)) from None
    return (text + "\n").encode("utf-8", "surrogateescape")


# ----------------------------------------------------------------------------------- the pointer

def _clean_path(path):
    """The path of a pointer, or _Bad: relative, canonical, no link-like parts."""
    if not path or path.startswith("/") or _CONTROL.search(path):
        raise _Bad("the path %r is not a relative path of the repository" % path[:80])
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise _Bad("the path %r holds '..', '.' or an empty part" % path[:80])
    if any(p.lower() == ".git" for p in parts):
        raise _Bad("the path %r is inside .git" % path[:80])
    return path


def _parse(pointer):
    text = pointer.strip()
    if len(text) > MAX_POINTER_TEXT:
        raise _Bad("a pointer is at most %d characters" % MAX_POINTER_TEXT)
    if text.startswith("[ptr:") and text.endswith("]"):
        text = text[5:-1].strip()
    if text.startswith("cmd:"):
        raise _Bad("a cmd: pointer cannot be checked later; cite a stored output as out:%s/<sha12>/<name>:<line>"
                   % OUTPUT_DIR)
    m = _POINTER.match(text)
    if not m:
        raise _Bad("a pointer is app@<path>:<line> or app@<path>:<a>-<b> or out:<path>:<line>; got %r" % text[:80])
    a = int(m.group("a"))
    b = int(m.group("b")) if m.group("b") else a
    if a < 1:
        raise _Bad("line numbers start at 1")
    if b < a:
        raise _Bad("the range %d-%d runs backwards" % (a, b))
    return ("app" if m.group("kind") == "app@" else "out"), _clean_path(m.group("path")), a, b


def _private_name(path):
    parts = path.split("/")
    if any(p.lower() in _PRIVATE_DIRS for p in parts[:-1]):
        return True
    low = parts[-1].lower()
    return any(fnmatch.fnmatchcase(low, pattern) for pattern in _PRIVATE_NAMES)


def _check_app(root, source, path, a, b):
    from alpaca import paths
    cfg = source.cfg
    if _private_name(path) or paths.private_runtime(str(root), os.path.join(str(root), cfg.repo, *path.split("/"))):
        raise _Bad("%s is a private or secret file by name; a map never cites it" % path)
    total = source.lines(path)
    if b > total:
        raise _Bad("%s has %d lines at commit %s; the pointer names line %d" % (path, total, source.sha[:12], b))


def _check_out(source, path, a, b):
    """An `out:` pointer names one of the outputs of the map's own commit. The line count comes from the
    output recomputed at that commit; the file in the outputs folder is never read, so a later prepare,
    a file dropped into the folder or an edited copy changes nothing here."""
    prefix = OUTPUT_DIR + "/"
    here = prefix + source.sha[:12] + "/"
    if not path.startswith(prefix) or len(path) == len(prefix):
        raise _Bad("an out: pointer names a file under %s/<sha12>/ (the outputs of the map's commit)" % OUTPUT_DIR)
    if not path.startswith(here):
        raise _Bad("an out: pointer names a file under %s (the map is at %s); got %s"
                   % (here, source.sha[:12], path[len(prefix):][:60]))
    name = path[len(here):]
    if name not in OUTPUT_NAMES:
        raise _Bad("%s is not an output alpaca attach study makes (%s)" % (name[:60], ", ".join(OUTPUT_NAMES)))
    total = source.output(name).count(b"\n")
    if b > total:
        raise _Bad("%s has %d lines at commit %s; the pointer names line %d" % (name, total, source.sha[:12], b))


def _check_one(root, source_for, pointer):
    kind, path, a, b = _parse(pointer)
    if kind == "out":
        _check_out(source_for(), path, a, b)
    else:
        _check_app(root, source_for(), path, a, b)
    return kind, path


def _source_or_bad(root, sha, *, trusted=False):
    try:
        return _Source(root, sha, trusted=trusted)
    except AttachError as exc:
        raise _Bad(str(exc)) from None


def check_map_pointer(root, pointer, sha) -> PointerCheck:
    """Does `pointer` resolve? Both forms are read at `sha`, from the object store of the attached
    repository: `app@` pointers from the tree, `out:` pointers from the output recomputed at `sha`.
    Never raises: a pointer that does not resolve, a commit the clone does not hold or that is not
    upstream history, and a studio that is not attached all answer `ok=False` with a reason."""
    cache = []

    def source_for():
        if not cache:
            cache.append(_source_or_bad(root, sha))
        return cache[0]
    try:
        _check_one(root, source_for, str(pointer))
    except _Bad as exc:
        return PointerCheck(False, str(exc))
    return PointerCheck(True, "")


# ------------------------------------------------------------------------------------- the map

def _check_map_arg(root, map_path):
    """The map is a `.md` file under `design/` of the studio and nowhere else: not a file of the attached
    repository, not on the human-owned authority surface (`contracts/`, `doctrine/`, `project.yaml`), not
    a state file. Raises AttachError. Every reader and the one writer of a map go through this."""
    cfg = config.load(root)
    try:
        clean = _clean_path(map_path) if isinstance(map_path, str) else _clean_path("")
    except _Bad:
        raise AttachError("--map %r is not a relative path of the studio" % str(map_path)[:80]) from None
    parts = clean.split("/")
    if (len(parts) < 2 or parts[0] != "design" or not clean.endswith(".md") or len(parts[-1]) <= 3
            or clean == cfg.repo or clean.startswith(cfg.repo + "/")):
        raise AttachError("the map is a .md file under design/ of the studio (default %s); %r is not"
                          % (MAP_PATH, clean[:80]))
    return clean


def _read_map(root, map_path):
    _check_map_arg(root, map_path)
    try:
        data = safefs.read_existing(root, map_path, limit=MAX_MAP_BYTES)
    except AttachError as exc:
        raise AttachError("the map %s cannot be read: %s" % (map_path, exc)) from None
    if data is None:
        raise AttachError("there is no map at %s; run alpaca attach study and have the scouts write it" % map_path)
    try:
        return re.sub(r"\r\n", "\n", data.decode("utf-8"))
    except UnicodeDecodeError:
        raise AttachError("the map %s is not UTF-8 text" % map_path) from None


def _split_front(text):
    """(front matter text, index of the first body line) or None."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), i + 1
    return None


def _clean_area_path(value):
    if not isinstance(value, str):
        raise _Bad("an area path must be a string")
    text = value.rstrip("/")
    try:
        return _clean_path(text)
    except _Bad:
        raise _Bad("the area path %r is not a relative file or folder of the repository" % value[:80]) from None


def _front(text):
    """(sha or None, [(id, [paths])], problems, body start index)."""
    problems, areas, sha = [], [], None
    split = _split_front(text)
    if split is None:
        return None, [], ["the map starts with no front matter (--- attach_sha and areas ---)"], 0
    front, body = split
    if len(front) > MAX_FRONT_BYTES or any(len(line) > MAX_LINE for line in front.split("\n")):
        return None, [], ["the front matter is longer than %d characters or has a line longer than %d"
                          % (MAX_FRONT_BYTES, MAX_LINE)], body
    m = _SHA_LINE.search(front)
    if m:
        sha = m.group(1)
    else:
        problems.append("the front matter has no attach_sha of 40 lowercase hex characters")
    import yaml
    try:
        if any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(front, Loader=yaml.SafeLoader)):
            return sha, [], problems + ["the front matter uses a YAML alias (*name), which is not allowed"], body
        data = yaml.safe_load(front) or {}
    except (yaml.YAMLError, RecursionError, ValueError):
        return sha, [], problems + ["the front matter is not valid YAML"], body
    if not isinstance(data, dict):
        return sha, [], problems + ["the front matter must be a mapping"], body
    listed = data.get("areas")
    if not isinstance(listed, list) or not listed:
        return sha, [], problems + ["the front matter lists no areas"], body
    seen = set()
    for i, item in enumerate(listed[:MAX_AREAS * 2]):
        label = "areas[%d]" % i
        if not isinstance(item, dict):
            problems.append("%s is not a mapping with an id and paths" % label)
            continue
        ident, paths = item.get("id"), item.get("paths")
        if not isinstance(ident, str) or len(ident) > MAX_AREA_ID or not _AREA_ID.fullmatch(ident):
            problems.append("%s.id must be lowercase letters, digits and - (got %r)" % (label, str(ident)[:40]))
            continue
        if ident in seen:
            problems.append("area %s is listed twice" % ident)
            continue
        seen.add(ident)
        if not isinstance(paths, list) or not paths:
            problems.append("area %s needs a non-empty paths list" % ident)
            continue
        clean = []
        for value in paths[:1000]:
            try:
                clean.append(_clean_area_path(value))
            except _Bad as exc:
                problems.append("area %s: %s" % (ident, exc))
        areas.append((ident, clean))
    if len(listed) > MAX_AREAS * 2:
        problems.append("the front matter lists more than %d areas" % (MAX_AREAS * 2))
    return sha, areas, problems, body


def _under(path, prefixes):
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def _strip_comments(line):
    """`line` with every `<!-- ... -->` replaced by one space, or None when a comment does not close on
    the line. One pass with `str.find`: linear in the length of the line."""
    out, pos = [], 0
    while True:
        i = line.find("<!--", pos)
        if i < 0:
            out.append(line[pos:])
            return "".join(out)
        j = line.find("-->", i + 4)
        if j < 0:
            return None
        out.append(line[pos:i])
        out.append(" ")
        pos = j + 3


def _split_pointers(text):
    """([pointer text], the text with every `[ptr: ...]` replaced by one space, an unterminated `[ptr:`
    was seen). A pointer is `[ptr:` up to the next `]` on the same line. One pass with `str.find`."""
    pointers, rest, open_ = [], [], False
    for line in text.split("\n"):
        pos = 0
        while True:
            i = line.find("[ptr:", pos)
            if i < 0:
                rest.append(line[pos:])
                break
            j = line.find("]", i + 5)
            if j < 0:
                rest.append(line[pos:])
                open_ = True
                break
            rest.append(line[pos:i])
            rest.append(" ")
            pointers.append(line[i + 5:j].strip())
            pos = j + 1
        rest.append("\n")
    return pointers, "".join(rest), open_


@dataclass
class _Analysis:
    check: MapCheck
    docs: list = field(default_factory=list)       # [(area id, document text)], only for an ok check
    areas: list = field(default_factory=list)      # [(area id, [paths])] of this parse, only for an ok check


def _analyze(root, text) -> _Analysis:
    """Parse the map text once and check it. The one place that decides what a map says: the result holds
    the verdict and, when it is ok, the area documents built from the bullets this parse checked (comment
    text removed) and from nothing else. A line that is not blank, not comment-only and not a bullet or the
    continuation of a bullet is a problem inside an area, so no line is skipped and then copied."""
    problems = []
    lines = text.split("\n")
    if len(lines) > MAX_LINES:
        return _Analysis(MapCheck(False, ["the map has %d lines; the limit is %d" % (len(lines), MAX_LINES)], 0, ""))
    sha, areas, found, body = _front(text)
    problems += found
    for idx, line in enumerate(lines):
        if len(line) > MAX_LINE:
            problems.append("line %d is longer than %d characters; keep a map line short" % (idx + 1, MAX_LINE))
            lines[idx] = ""
        elif idx >= body and _LINE_BREAKS.search(line):
            problems.append("line %d holds a control character or a line separator" % (idx + 1))
            lines[idx] = ""
    known = {a for a, _ in areas}
    sections, current, in_bullet = {}, None, False
    for idx in range(body, len(lines)):
        if len(problems) > MAX_PROBLEMS:
            break
        line, n = lines[idx], idx + 1
        head = _AREA_HEAD.match(line)
        if head:
            ident = head.group(1).strip()
            if not _AREA_ID.fullmatch(ident) or len(ident) > MAX_AREA_ID:
                problems.append("line %d: %r is not an area id (lowercase letters, digits and -)" % (n, ident[:40]))
                current = None
            elif ident not in known:
                problems.append("line %d: area %s is not in the front matter" % (n, ident))
                current = None
            elif ident in sections:
                problems.append("line %d: area %s has two sections" % (n, ident))
                current = None
            else:
                current = ident
                sections[ident] = []
            in_bullet = False
            continue
        if _H2.match(line):
            current, in_bullet = None, False
            continue
        clean = _strip_comments(line)
        if clean is None:
            problems.append("line %d: an HTML comment must open and close on the same line" % n)
            in_bullet = False
            continue
        if not clean.strip():
            if not line.strip():
                in_bullet = False
            continue
        if _SUBHEAD.match(clean):
            if current is not None:
                problems.append("line %d: a sub-heading inside an area is neither checked nor ingested; "
                                "write the claim as a bullet with a pointer, or delete it" % n)
                in_bullet = False
            continue
        if current is None:
            problems.append("line %d: text outside an area section is neither ingested nor checked; "
                            "put it in an area as a bullet with a pointer, or delete it" % n)
            continue
        if _BULLET.match(clean):
            in_bullet = True
            sections[current].append([n, [clean.rstrip()]])
        elif in_bullet and clean[:1].isspace():
            sections[current][-1][1].append(clean.rstrip())
        else:
            problems.append("line %d: a claim must be a bullet with a pointer; this line is plain text" % n)
            in_bullet = False
    shown = "\n".join(lines)
    if "[[" in shown:
        first = shown[:shown.index("[[")].count("\n") + 1
        problems.append("line %d: a double-bracket link is not allowed in the map (it would mint wiki edges)" % first)
    from alpaca.wiki.ingest.drain import screen
    if str(screen(shown)) != shown:
        problems.append("the map holds text the secret screen would redact (a key, token or password); remove it")
    claims = 0
    source = []

    def source_for():
        if not source:
            try:
                source.append(_source_or_bad(root, sha))
            except _Bad as exc:
                source.append(exc)
        if isinstance(source[0], _Bad):
            raise source[0]
        return source[0]
    paths_of = dict(areas)
    for ident, _ in areas:
        if ident not in sections:
            problems.append("area %s is in the front matter but has no '## Area: %s' section" % (ident, ident))
    seen = {}
    for ident, bullets in sections.items():
        if not bullets:
            problems.append("area %s has no claims" % ident)
        claims += len(bullets)
        for n, rows in bullets:
            if len(problems) > MAX_PROBLEMS:
                break
            pointers, claim, unterminated = _split_pointers("\n".join(rows))
            if unterminated:
                problems.append("line %d: a [ptr: ... has no closing ]" % n)
            if not pointers:
                problems.append("line %d: the claim has no [ptr: ...] pointer" % n)
                continue
            if sum(1 for ch in claim if ch.isalnum() or ch == "_") < 3:
                problems.append("line %d: the bullet has a pointer but no claim text" % n)
            for pointer in pointers:
                if sha is None:
                    problems.append("line %d: pointer %s cannot be checked without an attach_sha" % (n, pointer[:80]))
                    continue
                if pointer not in seen:
                    try:
                        seen[pointer] = _check_one(root, source_for, pointer)
                    except _Bad as exc:
                        seen[pointer] = exc
                got = seen[pointer]
                if isinstance(got, _Bad):
                    problems.append("line %d: pointer %s: %s" % (n, pointer[:120], got))
                elif got[0] == "app" and not _under(got[1], paths_of.get(ident, [])):
                    problems.append("line %d: pointer %s: %s is outside the paths of area %s; add it to the area's "
                                    "paths so a change to it marks the area stale" % (n, pointer[:120], got[1], ident))
    if claims == 0 and not any("has no claims" in p for p in problems):
        problems.append("the map has no claims")
    docs = []
    if not problems:
        for ident, _ in areas:
            content = "\n".join(row for _, rows in sections[ident] for row in rows)
            doc = "---\nvolatile: true\nattach_sha: %s\narea: %s\n---\n## Area: %s\n\n%s\n" % (sha, ident, ident, content)
            if str(screen(doc)) != doc:
                problems.append("the document of area %s holds text the secret screen would redact" % ident)
            docs.append((ident, doc))
    if len(problems) > MAX_PROBLEMS:
        problems = problems[:MAX_PROBLEMS] + ["... and more"]
    return _Analysis(MapCheck(ok=not problems, problems=problems, claims=claims, sha=sha or ""),
                     docs if not problems else [], [(i, list(p)) for i, p in (areas or [])] if not problems else [])


def check_map(root, map_path=MAP_PATH) -> MapCheck:
    """The AC-14 check. ok when the front matter is sound, every bullet under an `## Area:` heading
    has a pointer, every pointer resolves (at the map's `attach_sha`), and no text breaks a rule.
    A missing or unreadable map raises AttachError; everything else is a problem in the result."""
    config.load(root)
    return _analyze(root, _read_map(root, map_path)).check


# ----------------------------------------------------------------------------- stale areas

def _map_front(root, map_path):
    sha, areas, problems, _ = _front(_read_map(root, map_path))
    if sha is None or not areas:
        raise AttachError("the map front matter is unusable: %s" % (problems[0] if problems else "no areas"))
    return sha, areas


def _changed_paths(objs, old_sha, new_sha):
    """Every path whose content, mode or kind differs between two commits, a rename counted at its old and its
    new path, a deleted or replaced folder counted at each file below it. Two verified trees are compared
    level by level and a subtree with the same id on both sides is skipped, so only what changed is read."""
    pairs = [("", objs.commit(old_sha).tree, objs.commit(new_sha).tree)]
    touched, seen_pairs = [], 0
    while pairs:
        objs.load_trees([oid for _, a, b in pairs for oid in (a, b) if oid])
        nxt = []
        for prefix, a, b in pairs:
            seen_pairs += 1
            ea = objs.entries(a) if a else {}
            eb = objs.entries(b) if b else {}
            for name in sorted(set(ea) | set(eb)):
                x, y = ea.get(name), eb.get(name)
                if x == y:
                    continue
                path = prefix + name
                x_tree, y_tree = x is not None and x[1] == "tree", y is not None and y[1] == "tree"
                if x_tree and y_tree:
                    nxt.append((path + "/", x[2], y[2]))
                    continue
                if x is not None and not x_tree:
                    touched.append(path)
                if y is not None and not y_tree:
                    touched.append(path)
                if x_tree:
                    nxt.append((path + "/", x[2], None))
                if y_tree:
                    nxt.append((path + "/", None, y[2]))
            if len(touched) > MAX_CHANGED or seen_pairs > MAX_TREES:
                raise AttachError("the two commits differ in more than %d paths" % MAX_CHANGED)
        pairs = nxt
    return touched


def stale_areas(root, old_sha, new_sha, map_path=MAP_PATH) -> list:
    """The ids (in map order) of the areas whose paths changed between `old_sha` and `new_sha`. A
    rename or a copy counts at both its old and its new path, a delete at its old path (PM t-652.4).
    The two trees are read from hash-proved objects and compared in Python (no `git diff`, which reads the
    clone's own index and commit-graph). When the difference cannot be computed (a commit the clone no
    longer holds, an object that does not hash to its id, a difference past MAX_CHANGED paths) every area
    is stale."""
    _, areas = _map_front(root, map_path)
    return _stale_of(root, areas, old_sha, new_sha)


def _stale_of(root, areas, old_sha, new_sha) -> list:
    """stale_areas for a list of (area id, paths) already in hand; the map file is not read."""
    cfg = config.load(root)
    problems = health.repo_problems(root, cfg.repo)
    if problems:
        raise AttachError(problems[0])
    if old_sha == new_sha:
        return []
    for sha in (old_sha, new_sha):
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise AttachError("a commit id must be 40 lowercase hex characters")
    try:
        touched = _changed_paths(_Objects(str(cfg.repo_path(root))), old_sha, new_sha)
    except AttachError:
        return [a for a, _ in areas]
    return [ident for ident, paths in areas if any(_under(p, paths) for p in touched)]


MAX_STALE_MAPS = 50


def _load_stale(root):
    """The stale file as a dict with its `maps` entry a dict. One file, one flag per map: the top level
    (`areas`, `since_sha`, `to_sha`, `at`) is the flag of the default map `MAP_PATH`, which health and sync
    read; any other map's flag is under `maps[<path>]`, so a run on one map never rewrites another's flag
    (t-652 round 2, R2-06). `served_map` names the map the last ingest put into the wiki."""
    data = safefs.read_bytes(root, STALE_PATH)
    doc = {}
    if data is not None:
        try:
            value = json.loads(data.decode("utf-8"))
        except ValueError:
            value = None
        if isinstance(value, dict):
            doc = value
    maps = doc.get("maps")
    doc["maps"] = {k: v for k, v in maps.items() if isinstance(k, str) and isinstance(v, dict)} if isinstance(maps, dict) else {}
    if not isinstance(doc.get("areas"), list):
        doc.update(areas=[], since_sha=None, to_sha=None, at=None)
    return doc


def _write_stale(root, map_path, areas, since, to, *, served=None):
    """Record the stale areas of `map_path` (None leaves its flag as it is) under the study lock the caller
    holds, and the map the wiki now serves when `served` is given."""
    from alpaca import util
    doc = _load_stale(root)
    if areas is not None:
        entry = {"areas": list(areas), "since_sha": since, "to_sha": to, "at": util.now_iso()}
        if map_path == MAP_PATH:
            doc.update(entry)
        else:
            doc["maps"][map_path] = entry
            if len(doc["maps"]) > MAX_STALE_MAPS:
                oldest = sorted(doc["maps"], key=lambda k: str(doc["maps"][k].get("at")))[:len(doc["maps"]) - MAX_STALE_MAPS]
                for key in oldest:
                    del doc["maps"][key]
    if served is not None:
        doc["served_map"] = served
    doc.update(schema=2, map=MAP_PATH)
    config.write_json_atomic(os.path.join(str(root), STALE_PATH), doc, root=root)


def _served_record(root):
    """{map, sha, areas: [[id, [paths]]]} that the last complete ingest wrote, or None."""
    data = safefs.read_bytes(root, SERVED_PATH)
    try:
        rec = json.loads(data.decode("utf-8")) if data is not None else None
    except ValueError:
        return None
    if not (isinstance(rec, dict) and isinstance(rec.get("map"), str) and isinstance(rec.get("sha"), str)
            and isinstance(rec.get("areas"), list)):
        return None
    areas = [(a[0], [p for p in a[1] if isinstance(p, str)]) for a in rec["areas"]
             if isinstance(a, list) and len(a) == 2 and isinstance(a[0], str) and isinstance(a[1], list)]
    return {"map": rec["map"], "sha": rec["sha"], "areas": areas}


def _refresh_served(root, base) -> str:
    """The flag of the map the wiki serves, against `base`, from the area paths it was ingested with (the map
    file is not read again). Unknown is stale: a diff that cannot be made flags every served area. It never
    raises: it returns "" or a note saying the flag was not written (status then reads every served area as
    stale, see health._stale_shown). The caller holds the study lock."""
    from alpaca import util
    try:
        rec = _served_record(root)
        if rec is None or base is None:
            return ""
        try:
            stale = _stale_of(root, rec["areas"], rec["sha"], base)
        except AttachError:
            stale = [a for a, _ in rec["areas"]]
        doc = _load_stale(root)
        doc["served"] = {"map": rec["map"], "sha": rec["sha"], "to_sha": base, "areas": stale, "at": util.now_iso()}
        doc["served_map"] = rec["map"]
        config.write_json_atomic(os.path.join(str(root), STALE_PATH), doc, root=root)
    except AttachError as exc:
        return "the flag of the served map was not written: %s" % exc
    return ""


def read_stale(root, map_path=None):
    """The stale file as a dict (the flag of the default map at the top level, others under `maps`), or None.
    With `map_path`, the flag of that map as a dict of its own, or None when it was never flagged."""
    data = safefs.read_bytes(root, STALE_PATH)
    if data is None:
        return None
    try:
        value = json.loads(data.decode("utf-8"))
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    if map_path is None:
        return value
    if map_path == MAP_PATH:
        return {k: value.get(k) for k in ("areas", "since_sha", "to_sha", "at")}
    maps = value.get("maps")
    entry = maps.get(map_path) if isinstance(maps, dict) else None
    return entry if isinstance(entry, dict) else None


@contextlib.contextmanager
def _locked(root):
    """The study lock (`.alpaca/attach/study.lock`), held by `prepare`, `ingest` and `flag_stale` for their
    whole run. It is a plain `flock`, so a caller that already holds it in the same process (it opens its
    own descriptor) would wait for itself: call these three one after the other, never one inside another."""
    fd = safefs.open_lock(root, STATE_DIR + "/study.lock")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def flag_stale(root, map_path=MAP_PATH, *, session="cli") -> list:
    """Compare the map's commit with the newest base `prepare` recorded, write the map's flag in
    `.alpaca/attach/study/stale.json` and return the stale area ids. A map at that base clears the flag.
    There is no fetch here and the clone's refs are not read: the base is the one the record names, so a
    caller that fetched (`attach sync`, t-654) calls `prepare` first, which anchors the fetch to the upstream
    and records the base. This call takes the study lock, so it must not be called while the caller holds it."""
    config.load(root)
    with _locked(root):
        old, _ = _map_front(root, map_path)
        new = _latest_base(root)
        if new is None:
            raise AttachError("no base was recorded yet; run alpaca attach study first")
        areas = stale_areas(root, old, new, map_path)
        _write_stale(root, map_path, areas, old, new)
        note = _refresh_served(root, new)
    _event(root, session, {"action": "stale", "map": map_path, "map_sha": old, "base_sha": new, "areas": areas,
                           "note": note})
    return areas


# ------------------------------------------------------------------------------------ the record

def _event(root, session, data):
    from alpaca import db
    conn = db.connect(str(root))
    try:
        db.append_event(conn, session=session or "cli", actor="alpaca", kind="attach-study", op="op-0", data=data)
    finally:
        conn.close()


# ----------------------------------------------------------------------------------- prepare

def _upstream_sha(root, cfg, url):
    """The id the upstream itself gives for the base branch: `git ls-remote <url> refs/heads/<base>` run with
    no repository (`GIT_DIR=/dev/null`), so nothing in the clone's config (`url.*.insteadOf`, `remote.*`, a
    proxy) can change where it asks. This id, not a ref of the clone, is what `prepare` records."""
    app = str(cfg.repo_path(root))
    ref = "refs/heads/%s" % cfg.base
    out = gitops.remote(app, "ls-remote", "--", url, ref, url=url, no_repo=True)
    found = [line.split("\t") for line in out.splitlines() if line.count("\t") == 1]
    shas = [sha for sha, name in found if name == ref and _SHA.fullmatch(sha)]
    if len(shas) != 1:
        raise AttachError("the upstream names no single branch %s" % cfg.base)
    return shas[0]


def _check_fetch_config(root, cfg):
    """Refuse to run a network command in the clone when its config keys that steer a transfer or run a
    command (every local key but the core housekeeping, alias and branch keys: `gitops.config_digest`) are not
    the ones recorded at attach: the same comparison the `attach-git-config` doctor row makes. The recorded digest is
    `proven.init_facts`, the `attach-init` event of the record, never `config_digest` of state.json (a studio
    file an agent can write; op-062 integration of t-652, after t-648's class review r2). That digest hashes
    `git config --local --list` only, so config kept in `.git/config.worktree` (`extensions.worktreeConfig`) is
    not seen and this check passes when a rewrite sits there. It is an early, loud refusal for the keys it
    sees, not the defence. The defence is the upstream anchor (`_upstream_sha`): the base id comes from the
    upstream and the fetched ref must equal it. Probe P3-04 shows the anchor refusing a fetch redirected
    through the worktree config (t-652.logs/probe-adversarial-r3.log:18-19)."""
    want = proven.init_facts(root, cfg)["config_digest"]          # refuses when the record holds no digest
    if gitops.config_digest(str(cfg.repo_path(root))) != want:
        raise AttachError("%s/.git/config changed since attach (any key but the core housekeeping, alias and branch keys counts); "
                          "review it before a fetch" % cfg.repo)


def _fetch_base(root, cfg):
    """Fetch the base branch into `refs/remotes/<upstream>/<base>` from the recorded URL under the attach
    fetch lock (the same lock file t-648's `fetch_base` uses) and return the base commit id.

    The id comes from the upstream (`_upstream_sha`), not from the ref: the fetch itself runs in the clone and
    reads its config, so a rewrite there could bring another repository's commit. The id is asked of `attach.url`
    of project.yaml, a file of the authority surface, and the fetch uses `proven.fetch_url`: that same URL, only
    while it equals the URL the `attach-init` event recorded. `fetch_url` of `state.json` is not read (op-062
    integration of t-652, after t-648's class review r2), so a rewrite there sends no fetch anywhere. After the fetch the ref must name the upstream's id (asked after the fetch, up to
    three tries, because the upstream may move between the two calls); a ref that names anything else is
    refused, and the commit is read and hashed by `_Objects` before it is recorded."""
    url = proven.fetch_url(root, cfg)
    app = str(cfg.repo_path(root))
    ref = "refs/remotes/%s/%s" % (cfg.upstream, cfg.base)
    _check_fetch_config(root, cfg)
    fd = safefs.open_lock(root, STATE_DIR + "/fetch.lock")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        got = want = ""
        for _ in range(3):
            gitops.remote(app, "fetch", "--no-tags", "--no-recurse-submodules", "--", url,
                          "+refs/heads/%s:%s" % (cfg.base, ref), url=url)
            want = _upstream_sha(root, cfg, cfg.url)
            res = _git(app, "rev-parse", "--verify", "-q", ref)
            got = res.out.strip() if res.rc == 0 else ""
            if got == want:
                return want
    finally:
        os.close(fd)
    raise AttachError("the fetch left %s at %s but the upstream says %s; check %s/.git/config for url.*.insteadOf "
                      "or remote.* keys, and attach.url in project.yaml"
                      % (ref, got[:12] or "nothing", want[:12], cfg.repo))


def _is_real_folder(root, rel):
    """True when `rel` is a folder of the studio itself: `lstat` says directory, so a link to a folder
    (to the moving checkout, say) is not one. A link in the chain above makes `safefs.chain` raise."""
    try:
        parent, name = safefs.chain(root, rel)
        return stat.S_ISDIR(os.lstat(os.path.join(parent, name)).st_mode)
    except (AttachError, OSError):
        return False


def _blob_batches(objs, wanted):
    """Yield {object id: bytes} for the objects in `wanted` (id -> size), in groups of at most BATCH_BYTES.
    The bytes are raw blobs (no attributes, no filters) and each one is proved by its hash."""
    group, size = [], 0
    for obj, n in wanted.items():
        if group and size + n > BATCH_BYTES:
            yield objs.read_many(group, "blob")
            group, size = [], 0
        group.append(obj)
        size += n
    if group:
        yield objs.read_many(group, "blob")


def _snapshot(root, cfg, sha):
    """Export the tree at `sha` into `.alpaca/attach/study/<sha12>/` (read-only files). Idempotent: a
    finished snapshot (its done file names the commit) is kept. Returns the done record. Every blob is read
    and hashed once before anything is removed or written, and again as it is written, so an object that does
    not hash to its id stops `prepare` with the earlier snapshot and outputs untouched."""
    sha12 = sha[:12]
    folder = "%s/%s" % (STUDY_DIR, sha12)
    done_path = "%s/%s.json" % (STUDY_DIR, sha12)
    prior = safefs.read_bytes(root, done_path)
    if prior is not None:
        try:
            record = json.loads(prior.decode("utf-8"))
            if isinstance(record, dict) and record.get("sha") == sha and _is_real_folder(root, folder):
                record["reused"] = True
                return record
        except ValueError:
            pass
    app = str(cfg.repo_path(root))
    objs = _Objects(app)
    entries = objs.all_entries(objs.commit(sha).tree)
    blobs = [e for e in entries if e[0] in ("100644", "100755")]
    sizes = objs.sizes([e[2] for e in blobs]) if blobs else {}
    if any(n is None for n in sizes.values()):
        raise AttachError("the tree at %s names a file the clone does not hold" % sha12)
    if len(blobs) > MAX_SNAPSHOT_FILES or sum(sizes[e[2]] for e in blobs) > MAX_SNAPSHOT_BYTES:
        raise AttachError("the tree at %s holds %d files and %d bytes; the snapshot limit is %d files and %d bytes"
                          % (sha12, len(blobs), sum(sizes[e[2]] for e in blobs), MAX_SNAPSHOT_FILES, MAX_SNAPSHOT_BYTES))
    skipped = [e[3] for e in entries if e[0] not in ("100644", "100755")]
    wanted, by_obj = {}, {}
    for mode, _, obj, path in blobs:
        parts = path.split("/")
        if any(p in ("", ".", "..") or p.lower() == ".git" for p in parts):
            skipped.append(path)
            continue
        wanted[obj] = sizes[obj]
        by_obj.setdefault(obj, []).append((mode, path))
    for _ in _blob_batches(objs, wanted):                    # pass 1: prove every blob; nothing is written yet
        pass
    safefs.make_dir(root, STUDY_DIR)
    safefs.clear_path(root, folder)
    safefs.make_dir(root, folder)
    written, total = 0, 0
    for batch in _blob_batches(objs, wanted):                # pass 2: write, proving each blob again
        for obj, data in batch.items():
            for mode, path in by_obj[obj]:
                safefs.write_bytes(root, "%s/%s" % (folder, path), data, mode=0o555 if mode == "100755" else 0o444)
                written += 1
                total += len(data)
    record = {"schema": 1, "sha": sha, "files": written, "bytes": total, "skipped": sorted(skipped)[:200],
              "skipped_count": len(skipped)}
    config.write_json_atomic(os.path.join(str(root), done_path), record, root=root)
    record["reused"] = False
    return record


def _prune(root, keep):
    """Remove snapshots and stored outputs of other commits than those in `keep` (the new one and the map's).
    The folders are listed and the entries deleted through `safefs` (descriptor walks that refuse a link), so
    a study folder swapped for a link deletes nothing outside the studio; a link in the chain raises."""
    gone = []
    for folder, with_json in ((STUDY_DIR, True), (OUTPUT_DIR, False)):
        for name in safefs.list_names(root, folder):
            m = re.fullmatch(r"([0-9a-f]{12})(\.json)?", name)
            if not m or m.group(1) in keep or (m.group(2) and not with_json):
                continue
            if m.group(2):
                safefs.remove_file(root, "%s/%s" % (folder, name))
            elif safefs.clear_path(root, "%s/%s" % (folder, name)) and folder == STUDY_DIR:
                gone.append(name)
    return gone


def _store_outputs(root, cfg, sha):
    """Write the command outputs of `sha` to `outputs/<sha12>/` for the scouts to read. The bytes are the
    ones the checker recomputes (`_output_bytes`); a file there that was edited or added by hand is
    overwritten (or ignored) and never counts as evidence."""
    app = str(cfg.repo_path(root))
    objs = _Objects(app)
    made = []
    for name in OUTPUT_NAMES:
        rel = "%s/%s/%s" % (OUTPUT_DIR, sha[:12], name)
        safefs.write_bytes(root, rel, _output_bytes(app, sha, name, objs=objs), mode=0o644)
        made.append(rel)
    return made


def _slug(name, taken):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "area"
    ident, n = base, 2
    while ident in taken:
        ident, n = "%s-%d" % (base, n), n + 1
    return ident


def _skeleton(source, sha):
    top = source.listing("")
    folders = sorted(n for n, (mode, kind, _) in top.items() if kind == "tree" and not n.startswith("."))
    files = sorted(n for n, (mode, kind, _) in top.items() if kind == "blob")
    areas, taken = [], set()
    for name in folders[:MAX_AREAS]:
        ident = _slug(name, taken)
        taken.add(ident)
        areas.append((ident, [name + "/"]))
    if files and len(areas) < MAX_AREAS:
        areas.append((_slug("root-files", taken), files[:50]))
    head = ["---", 'attach_sha: "%s"' % sha, "areas:"]
    for ident, paths in areas:
        head += ["  - id: %s" % ident, "    paths: [%s]" % ", ".join(json.dumps(p) for p in paths)]
    head.append("---")
    body = ["# Codebase map", "",
            "<!-- Scouts: replace each placeholder with bullets. One bullet is one claim and ends with one or more pointers. -->",
            "<!-- Pointer: [ptr: app@<path>:<line>] or [ptr: app@<path>:<a>-<b>] reads the file at attach_sha; "
            "[ptr: out:%s/%s/<file>:<line>] reads a stored output (git-log.txt, shortlog.txt or files.txt). -->"
            % (OUTPUT_DIR, sha[:12]),
            "<!-- A pointer path must lie under the area's paths. No double-bracket links. "
            "Check with: alpaca attach study check -->", ""]
    for ident, _ in areas:
        body += ["## Area: %s" % ident, "", "<!-- bullets for this area -->", ""]
    if len(folders) > MAX_AREAS:
        body.append("<!-- %d more top-level folders were left out of the skeleton: %s -->"
                    % (len(folders) - MAX_AREAS, ", ".join(folders[MAX_AREAS:MAX_AREAS + 10])))
    return "\n".join(head + [""] + body) + "\n"


def prepare(root, *, fetch=True, map_path=MAP_PATH, session="cli") -> dict:
    """Fetch (unless `fetch` is False), export the snapshot and the stored outputs at the base commit,
    write a map skeleton when there is no map, and flag the stale areas of the map named by `map_path`.

    With a fetch the base is the id the upstream gives for the branch (`_upstream_sha`), checked against the
    fetched ref. Without one (`--no-fetch`, no network) the base is the newest one an earlier prepare
    recorded: the refs of the clone name no base, because an agent can move them. The commit is read from
    the object store and proved by its hash before anything is written."""
    cfg = config.load(root)
    _check_map_arg(root, map_path)
    problems = health.repo_problems(root, cfg.repo)
    if problems:
        raise AttachError(problems[0])
    with _locked(root):
        if fetch:
            sha = _fetch_base(root, cfg)
        else:
            sha = _latest_base(root)
            if sha is None:
                raise AttachError("no base was recorded yet; run alpaca attach study without --no-fetch once")
        try:
            _Objects(str(cfg.repo_path(root))).commit(sha)
        except _Missing:
            raise AttachError("commit %s is not in %s/; run alpaca attach study to fetch it" % (sha[:12], cfg.repo)) from None
        snap = _snapshot(root, cfg, sha)
        outputs = _store_outputs(root, cfg, sha)
        info = {"sha": sha, "sha12": sha[:12], "base": cfg.base, "upstream": cfg.upstream,
                "snapshot": "%s/%s" % (STUDY_DIR, sha[:12]), "files": snap["files"], "bytes": snap["bytes"],
                "skipped": snap["skipped_count"], "reused": snap["reused"], "outputs": outputs,
                "map": _map_state(root, cfg, sha, map_path, session)}
        served_note = _refresh_served(root, sha)    # the base of this prepare: its event is written below
        if served_note:
            info["served_note"] = served_note
        keep = {sha[:12]}
        if info["map"].get("attach_sha"):
            keep.add(info["map"]["attach_sha"][:12])
        info["pruned"] = _prune(root, keep)
        # recorded inside the lock: an ingest that waited for this prepare finds the base it wrote the skeleton at
        _event(root, session, {"action": "prepare", "sha": sha, "files": info["files"], "bytes": info["bytes"],
                               "map": {k: v for k, v in info["map"].items() if k != "problems"}})
    return info


def _map_state(root, cfg, sha, map_path, session):
    state = {"path": map_path, "exists": False, "created": False, "attach_sha": None, "stale_areas": [], "ok": None}
    try:
        text = _read_map(root, map_path)
    except AttachError as exc:
        if "there is no map" not in str(exc):
            raise
        source = _Source(root, sha, trusted=True)
        safefs.write_bytes(root, map_path, _skeleton(source, sha).encode("utf-8"), mode=0o644, create_only=True)
        state.update(exists=True, created=True, attach_sha=sha)
        return state
    state["exists"] = True
    old, areas, found, _ = _front(text)
    state["attach_sha"] = old
    if old is None or not areas:
        state.update(ok=False, problems=found)
        return state
    stale = stale_areas(root, old, sha, map_path)
    _write_stale(root, map_path, stale, old, sha)
    state["stale_areas"] = stale
    return state


# ------------------------------------------------------------------------------------- ingest

def _doc_id(ident, doc):
    """`wiki/attach/map/<area>/<12 hex of the text's sha256>.md`: one id per distinct text of an area."""
    return "%s%s/%s.md" % (WIKI_PREFIX, ident, hashlib.sha256(doc.encode("utf-8")).hexdigest()[:12])


def _vault_bytes(root, doc_id):
    """The bytes of the wiki's vault copy of `doc_id` (what the write door stored), or None."""
    from alpaca.wiki.config import Config
    path = Config.for_project(str(root)).vault_dir / doc_id
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_bytes()
    except OSError:
        return None


def _retire_replaced(root, fresh, areas):
    """Retire the wiki documents of the map that this ingest replaced or left behind: every active
    document under `wiki/attach/map/` that is not one of `fresh` ({area: doc id}). A document of an
    area that is still in the map is superseded by the area's new document; a document of an area that
    left the map is retired with no successor. This is the wiki's own pattern for a replaced projection
    (`drain` retires the older versions of a source the same way), and it keeps the body-shrink gate
    out of the way: that gate guards one document id against a truncated paste, and a refreshed area
    is a new document, not an edit of an old one."""
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    absorber = Absorber(Config.for_project(str(root)))
    try:
        rows = absorber.db.conn.execute(
            "SELECT DISTINCT doc_id FROM blocks WHERE status='active' AND substr(doc_id, 1, ?) = ?",
            (len(WIKI_PREFIX), WIKI_PREFIX)).fetchall()
        keep = set(fresh.values())
        old = {}
        for row in rows:
            doc_id = row[0]
            m = re.fullmatch(re.escape(WIKI_PREFIX) + r"([a-z0-9-]+)/[0-9a-f]{12}\.md", doc_id)
            if m is None or doc_id in keep:
                continue
            old.setdefault(m.group(1), []).append(doc_id)
        retired = []
        for ident, ids in sorted(old.items()):
            if ident in areas:
                absorber.retire_documents(ids, reason="attach-map-refreshed", superseded_by=fresh[ident])
            else:
                absorber.retire_documents(ids, reason="attach-map-area-removed")
            retired += ids
        return retired
    finally:
        absorber.close()


def _retire_ids(root, ids, reason):
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    absorber = Absorber(Config.for_project(str(root)))
    try:
        absorber.retire_documents(list(ids), reason=reason)
    finally:
        absorber.close()


def _active_map_ids(root) -> set:
    """The ids of the active wiki documents under `wiki/attach/map/` (read under the study lock). A wiki that
    does not exist yet (no ingest ever ran) holds none."""
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    cfg = Config.for_project(str(root))
    if not os.path.lexists(str(cfg.vault_dir)):
        return set()
    absorber = Absorber(cfg)
    try:
        return {r[0] for r in absorber.db.conn.execute(
            "SELECT DISTINCT doc_id FROM blocks WHERE status='active' AND substr(doc_id, 1, ?) = ?",
            (len(WIKI_PREFIX), WIKI_PREFIX))}
    finally:
        absorber.close()


def _roll_back(root, before) -> str:
    """After a failed ingest, retire every active map document that was not active before the run, so a
    refused ingest does not leave two versions of an area. Returns a sentence for the refusal text. A document
    that the run retired before it failed (a replaced one) is not brought back: run the ingest again."""
    try:
        extra = sorted(_active_map_ids(root) - before)
        if extra:
            _retire_ids(root, extra, "attach-ingest-rolled-back")
    except Exception as exc:                                        # noqa: BLE001 - the refusal is the answer
        return "; the documents this run stored could not be retired again (%s: %s)" % (type(exc).__name__, gitops.scrub(exc))
    return "; the documents this run stored were retired again" if extra else ""


def ingest(root, map_path=MAP_PATH, *, session="cli") -> list:
    """Check the map, then ingest one wiki document per area, `wiki/attach/map/<area>/<text id>.md`
    (volatile), through `wiki.service.ingest`. Returns the doc ids. A map that fails the check ingests
    nothing. A text the wiki already holds is skipped by the wiki itself. A changed area becomes a new
    document and the old one is retired with the new one as its successor, so an area refreshed with
    fewer lines ingests (PM t-652.5) and the wiki never serves two versions of an area.

    The run holds the study lock, reads the map once and parses it once: the documents are built from the
    bullets that this parse checked. The wiki reads its input from a file, so after each document the wiki
    stored (its vault copy) is compared with the checked text; a difference retires the document and fails.
    A run that fails after it stored a document retires every document it stored (`_roll_back`), and it
    writes neither `study/served.json` nor the stale flag: only a complete ingest changes what status reports."""
    config.load(root)
    with _locked(root):
        analysis = _analyze(root, _read_map(root, map_path))
        result = analysis.check
        if not result.ok:
            raise AttachError("the map does not pass the check (%d problems); first: %s"
                              % (len(result.problems), result.problems[0]))
        from alpaca.wiki import service
        try:
            before = _active_map_ids(root)
        except Exception as exc:                                    # noqa: BLE001 - store errors
            raise AttachError("the wiki could not be read: %s: %s" % (type(exc).__name__, gitops.scrub(exc))) from None
        fresh = {}
        try:
            for ident, doc in analysis.docs:
                doc_id = _doc_id(ident, doc)
                rel = "%s/%s.md" % (WIKI_SRC_DIR, ident)
                safefs.write_bytes(root, rel, doc.encode("utf-8"), mode=0o644)
                try:
                    service.ingest(str(root), os.path.join(os.path.realpath(str(root)), rel), doc_id=doc_id, session=session)
                except ValueError as exc:
                    raise AttachError("the wiki refused %s: %s" % (doc_id, exc)) from None
                except Exception as exc:                            # noqa: BLE001 - store errors
                    raise AttachError("the wiki refused %s: %s: %s" % (doc_id, type(exc).__name__, gitops.scrub(exc))) from None
                if _vault_bytes(root, doc_id) != doc.encode("utf-8"):
                    try:
                        _retire_ids(root, [doc_id], "attach-map-text-changed-in-flight")
                    except Exception:                               # noqa: BLE001 - the refusal below is the answer
                        pass
                    raise AttachError("the wiki stored %s with text other than the text that passed the check; "
                                      "the document was retired, run alpaca attach study ingest again" % doc_id)
                fresh[ident] = doc_id
            try:
                retired = _retire_replaced(root, fresh, set(fresh))
            except Exception as exc:                                # noqa: BLE001 - store errors
                raise AttachError("the replaced documents could not be retired: %s: %s"
                                  % (type(exc).__name__, gitops.scrub(exc))) from None
        except AttachError as exc:
            raise AttachError(str(exc) + _roll_back(root, before)) from None
        ids = list(fresh.values())
        # a map at the newest recorded base is not stale; the flag of any other map is left as it is
        at_base = result.sha == _latest_base(root)
        flag_note = ""
        try:
            config.write_json_atomic(os.path.join(str(root), SERVED_PATH),
                                     {"map": map_path, "sha": result.sha, "areas": analysis.areas}, root=root)
            _write_stale(root, map_path, [] if at_base else None, result.sha, result.sha, served=map_path)
            flag_note = _refresh_served(root, _latest_base(root))
        except AttachError as exc:                                  # the documents are in; say the flag is not
            flag_note = "the stale flag was not written: %s" % exc
    _event(root, session, {"action": "ingest", "map": map_path, "map_sha": result.sha, "docs": ids,
                           "retired": retired, "claims": result.claims, "note": flag_note})
    return ids


# ---------------------------------------------------------------------------------------- verb

def add_parser(verbs):
    p = verbs.add_parser("study", help="prepare the study material and check the codebase map (prepare, check, ingest, stale)")
    p.add_argument("action", nargs="?", default="prepare", choices=("prepare", "check", "ingest", "stale"),
                   help="prepare (default): snapshot and skeleton; check: pointers resolve; ingest: into the wiki; "
                        "stale: flag the areas that changed")
    p.add_argument("--map", default=MAP_PATH, help="the map file in the studio (default %s)" % MAP_PATH)
    p.add_argument("--no-fetch", action="store_true",
                   help="prepare from the newest base an earlier prepare recorded (no network, no ref of the clone is read)")
    p.add_argument("--json", action="store_true", help="print one JSON object")


def run(args) -> int:
    from alpaca import cli as alpaca_cli
    from alpaca.attach.cli import finish
    root = alpaca_cli._root()
    action = getattr(args, "action", None) or "prepare"
    session = getattr(args, "session", None) or "cli"
    map_path = getattr(args, "map", None) or MAP_PATH
    if config.mode(root) is None:
        return finish(args, "study", "BLOCKED", "this project is not attached (or its attach: block is invalid)")
    try:
        if action == "prepare":
            info = prepare(root, fetch=not args.no_fetch, map_path=map_path, session=session)
            lines = ["alpaca attach study: base %s/%s at %s" % (info["upstream"], info["base"], info["sha12"]),
                     "  snapshot %s (%d files, %d bytes%s)" % (info["snapshot"], info["files"], info["bytes"],
                                                              ", kept from an earlier run" if info["reused"] else ""),
                     "  outputs: %s" % ", ".join(os.path.basename(o) for o in info["outputs"])]
            m = info["map"]
            if m["created"]:
                lines.append("  map skeleton written to %s; scouts fill it, then alpaca attach study check" % m["path"])
            else:
                lines.append("  map %s at %s; stale areas: %s" % (m["path"], (m["attach_sha"] or "?")[:12],
                                                                  ", ".join(m["stale_areas"]) or "none"))
            if m.get("ok") is False:
                lines.append("  the map %s cannot be used: %s" % (m["path"], (m.get("problems") or ["no usable front matter"])[0]))
                return finish(args, "study", "FAIL", "the map %s has no usable front matter" % m["path"],
                              lines=lines, receipt=info)
            return finish(args, "study", "PASS", lines=lines, receipt=info)
        if action == "check":
            res = check_map(root, map_path)
            lines = ["alpaca attach study check: %s at %s, %d claims" % (map_path, res.sha[:12], res.claims)]
            lines += ["  PROBLEM %s" % p for p in res.problems]
            return finish(args, "study", "PASS" if res.ok else "FAIL",
                          "" if res.ok else "%d problems; first: %s" % (len(res.problems), res.problems[0]),
                          lines=lines, receipt={"ok": res.ok, "claims": res.claims, "sha": res.sha,
                                                "problems": res.problems})
        if action == "ingest":
            ids = ingest(root, map_path, session=session)
            return finish(args, "study", "PASS", lines=["alpaca attach study: ingested %d area documents" % len(ids)]
                          + ["  %s" % i for i in ids], receipt={"docs": ids})
        areas = flag_stale(root, map_path, session=session)
        return finish(args, "study", "PASS", lines=["alpaca attach study: stale areas: %s" % (", ".join(areas) or "none")],
                      receipt={"stale_areas": areas})
    except AttachError as exc:
        return finish(args, "study", "FAIL", gitops.scrub(exc))


SUBVERBS = {"study": (add_parser, run)}
