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
import sys
import threading
import time

TIMEOUT = 10
MAX_COMMITS = 400
MAX_FILES = 300
MAX_REFLOG = 60
LIVE_S = 2 * 3600          # a session that beat within this long counts as live for the worktree match
SEP = "\x1f"
TASK = re.compile(r"\bt-\d{3,}\b")
OP = re.compile(r"\bop-\d{3,}\b")
STATUS_MAX_S = 300         # a worktree's cached status is re-read after this long even when git's own files are unchanged
SWEEP_TICK_S = 8           # the sweeper reads statuses this often while the page is open (t-592)
SWEEP_IDLE_S = 120         # and stops this long after the last request for them
SWEEP_COLD_S = 70          # the cold worktrees turn over once in about this long
SWEEP_HOT_S = 1800         # a worktree whose status, HEAD or gitdir moved within this long is hot
SWEEP_HOT_MAX = 8          # hot statuses read per tick, oldest read first: bounds the CPU a busy hour can cost
SWEEP_RETRY_S = 60         # a worktree whose status read failed is tried again after this long, doubling to SWEEP_RETRY_MAX_S
SWEEP_RETRY_MAX_S = 600
SWEEP_READ_TIMEOUT_S = 4   # a hot or cold read of the sweeper that takes longer counts as failed: a hung tree must not hold the tick
SWEEP_RETRY_TIMEOUT_S = SWEEP_READ_TIMEOUT_S  # a retry may use the tick this long at most, one per tick; not shorter than a read, or a tree that reads in 3-4 s never recovers (t-592 review r4)
SWEEP_RANK_S = 900         # an unwatched worktree is read again for the ranking after this long, one per tick
WATCH_RECENT = 5           # the page keeps live the most recently active worktrees, this many
WATCH_LARGEST = 5          # and the largest by last known uncommitted count, this many more
CACHE_MAX = 4000           # entries kept per cache; a full cache is cleared whole and the next build refills it

# Results that stay true until git's own files move (t-585). Commit pairs and per-commit facts never change; a
# worktree's status and reflog are reused while the stat of HEAD, the index, the refs and the gitdir are the same.
_CACHE_LOCK = threading.Lock()
_PAIRS = {}      # (base sha, head sha) -> (ahead, behind)
_TIMES = {}      # sha -> commit time
_LAST = {}       # (main HEAD sha, top, path) -> last commit time of one file
_STATUS = {}     # worktree path -> (signature, read at (monotonic), files, oldest change, read at (wall clock))
_REFLOG = {}     # (path, ref, label) -> (signature, entries)
_FAILED = {}     # worktree path -> (error type, when) of a status read that failed and has not succeeded since (t-592)
_MOVED = {}      # worktree path -> wall time a read found its files different from the read before


_LIMIT = threading.local()   # a thread may lower git()'s timeout for the calls it makes (the sweeper's retry lane)


class GitError(Exception):
    pass


def reset_caches():
    with _CACHE_LOCK:
        for cache in (_PAIRS, _TIMES, _LAST, _STATUS, _REFLOG, _FAILED, _MOVED):
            cache.clear()


def _remember(cache, key, value):
    with _CACHE_LOCK:
        if len(cache) >= CACHE_MAX:
            cache.clear()
        cache[key] = value
    return value


def _recall(cache, key):
    """One locked read: a cache another window's build clears can not drop the key between a test and a fetch.
    Every cached value is handed out as a copy (or is immutable), so no caller can grow what is stored."""
    with _CACHE_LOCK:
        return cache.get(key)


def _stat(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _git_dirs(path):
    """(gitdir, common dir) of a working tree read from its .git file or folder, without running git."""
    dot = os.path.join(path, ".git")
    try:
        if os.path.isdir(dot):
            return dot, dot
        with open(dot, encoding="utf-8", errors="replace") as f:
            first = f.readline().strip()
    except OSError:
        return None, None
    if not first.startswith("gitdir:"):
        return None, None
    gitdir = os.path.normpath(os.path.join(path, first[7:].strip()))
    try:
        with open(os.path.join(gitdir, "commondir"), encoding="utf-8", errors="replace") as f:
            common = os.path.normpath(os.path.join(gitdir, f.readline().strip()))
    except OSError:
        common = gitdir
    return gitdir, common


def _signature(w):
    """Stat of what decides a worktree's status: HEAD, index, packed-refs, its branch ref and the gitdir."""
    gitdir, common = _git_dirs(w["path"])
    if gitdir is None:
        return None
    ref = os.path.join(common, "refs", "heads", w["branch"]) if w.get("branch") else None
    return (w.get("head"), w.get("branch"), _stat(os.path.join(gitdir, "HEAD")), _stat(os.path.join(gitdir, "index")),
            _stat(os.path.join(common, "packed-refs")), _stat(ref) if ref else None, _stat(gitdir))


def git(cwd, *args, check=True):
    """Run one read-only git command and return its stdout as text."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(name, None)
    try:
        done = subprocess.run(["git", "--no-pager", "--no-optional-locks", "-C", cwd, *args], capture_output=True, text=True,
                              timeout=getattr(_LIMIT, "timeout", None) or TIMEOUT, env=env, errors="replace")
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


def ahead_behind(root, base, ref, base_sha=None):
    """(ahead, behind) of ref against base. Given the base's commit id and a commit id for ref the answer
    is remembered: two commits always have the same distance."""
    key = (base_sha, ref) if base_sha and re.fullmatch(r"[0-9a-f]{40,64}", ref) else None
    hit = _recall(_PAIRS, key) if key else None
    if hit is not None:
        return hit                                         # a tuple of ints: immutable
    # run against the commit id the key names, so a branch that moves mid-build is not stored under the old id
    out = git(root, "rev-list", "--left-right", "--count", "%s...%s" % (base_sha if key else base, ref), check=False).split()
    found = (int(out[1]), int(out[0])) if len(out) == 2 else (None, None)
    return _remember(_PAIRS, key, found) if key and found[0] is not None else found


def _base_sha(root, base):
    return git(root, "rev-parse", "--verify", "--quiet", "refs/heads/" + base, check=False).strip() or None


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


def status(path, cheap=False):
    """[(code, path)] from `git --no-optional-locks status --porcelain=v2` (never writes the index).
    cheap=True turns off git's index-preload threads: the same answer for about 20% less CPU over 200 trees (t-592)."""
    cmd = ("-c", "core.preloadindex=false", "status") if cheap else ("status",)
    out = []
    for line in git(path, *cmd, "--porcelain=v2", "--untracked-files=normal", check=False).splitlines():
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
    hit = _recall(_TIMES, sha)                             # an int: immutable
    if hit is not None:
        return hit
    out = git(root, "log", "-1", "--format=%ct", sha, check=False).strip()
    return _remember(_TIMES, sha, int(out)) if out.isdigit() else None


def _read_tree(w, cheap=False):
    """Read one worktree's status now and remember it: (files, oldest change, read at). A failed read is noted in
    _FAILED (one stderr line the first time) and raised, so one tree never decides what another shows (t-592)."""
    sig, now, path = _signature(w), time.monotonic(), w["path"]
    try:
        files = status(path, cheap)
    except Exception as exc:
        if _recall(_FAILED, path) is None:
            print("gitview: status of worktree %s could not be read (%s)" % (os.path.basename(path), type(exc).__name__),
                  file=sys.stderr)
        _remember(_FAILED, path, (type(exc).__name__, time.time()))
        raise
    oldest, wall = _oldest_change(path, files), time.time()
    before = _recall(_STATUS, path)
    if before is not None and before[2] != files:
        _remember(_MOVED, path, wall)                      # whoever read it, the sweeper hears that it moved
    with _CACHE_LOCK:
        _FAILED.pop(path, None)
    if sig is not None:
        _remember(_STATUS, path, (sig, now, list(files), oldest, wall))
    return files, oldest, wall


def _tree_read(w):
    """(files, oldest change, read at) of one worktree, reused while git's own files for it are unchanged.
    When the read fails the last good one is returned with its own time, and _FAILED says so."""
    if not os.path.isdir(w["path"]):
        return [], None, None
    sig, now = _signature(w), time.monotonic()
    hit = _recall(_STATUS, w["path"])
    if sig is not None and hit and hit[0] == sig and now - hit[1] < STATUS_MAX_S:
        return list(hit[2]), hit[3], hit[4]                # (code, path) tuples inside: a copy of the list is enough
    try:
        return _read_tree(w)
    except Exception:
        return (list(hit[2]), hit[3], hit[4]) if hit else ([], None, None)


def _tree_status(w):
    """(files, oldest change) of one worktree, reused while git's own files for it are unchanged."""
    return _tree_read(w)[:2]


def _newest_mtime(w):
    """Wall time of the newest change to a worktree's index, HEAD, gitdir or top-level directory, else None."""
    gitdir, _ = _git_dirs(w["path"])
    times = []
    for path in (os.path.join(gitdir, "index") if gitdir else None, os.path.join(gitdir, "HEAD") if gitdir else None,
                 gitdir, w["path"]):
        try:
            times.append(os.stat(path).st_mtime) if path else None
        except OSError:
            pass
    return max(times) if times else None


def watched(trees, opened=(), recent=None, largest=None):
    """{path: reason} of the worktrees the Version control page keeps live, in this order: the main tree ("main"),
    the `recent` other trees most recently active ("recent"), the `largest` others by last known uncommitted count
    ("largest", filled from the next largest when a tree is already recent), then each tree in `opened` that is not
    yet in ("opened"). Every other tree loads on demand, so the cost stays bounded however many worktrees exist.

    Each tree is a dict with path, is_main, live (a live session works in it), active (wall time of its newest
    activity, or None) and uncommitted (its last known count, or None). None for recent or largest means
    WATCH_RECENT or WATCH_LARGEST. Pure: no git, no disk."""
    recent = WATCH_RECENT if recent is None else recent
    largest = WATCH_LARGEST if largest is None else largest
    trees = list(trees)
    out = {}
    main = next((t for t in trees if t.get("is_main")), trees[0] if trees else None)
    if main is not None:
        out[main["path"]] = "main"
    rest = [t for t in trees if t["path"] not in out]
    for t in sorted(rest, key=lambda t: (not t.get("live"), -(t.get("active") or 0), t["path"]))[:max(0, recent)]:
        out[t["path"]] = "recent"
    rest = [t for t in rest if t["path"] not in out]
    size = lambda t: t["uncommitted"] if t.get("uncommitted") is not None else -1     # never read: after a clean tree
    for t in sorted(rest, key=lambda t: (-size(t), -(t.get("active") or 0), t["path"]))[:max(0, largest)]:
        out[t["path"]] = "largest"
    known = {t["path"] for t in trees}
    for path in opened:
        if path in known and path not in out:
            out[path] = "opened"
    return out


def _base_or_none(root):
    try:
        return main_branch(root)
    except GitError:
        return None


def rank_inputs(root, trees, live=(), base=None):
    """What `watched` ranks, for each worktree of `worktrees(root)`, from stats and caches only: no status is read.
    active is the newest of the time a read found its files moved, `_newest_mtime` and its HEAD commit time; a tree
    with a live session in it (`live`: paths or real paths) ranks first."""
    live = set(live or ())
    out = []
    for w in trees:
        path = w["path"]
        hit = _recall(_STATUS, path)
        head = _commit_time(root, w["head"]) if w.get("head") else None
        times = [t for t in (_recall(_MOVED, path), _newest_mtime(w), head) if t]
        out.append({"path": path, "is_main": base is not None and w.get("branch") == base,
                    "live": path in live or os.path.realpath(path) in live,
                    "active": max(times) if times else None, "uncommitted": len(hit[2]) if hit else None})
    return out


def watch_set(root, trees, live=(), opened=(), base=None, recent=None, largest=None):
    """`watched` over the repository's worktrees, ranked by `rank_inputs`."""
    return watched(rank_inputs(root, trees, live, base), opened, recent, largest)


def overall(rows, watch):
    """The repository-level line of the worktree card from {path: status row or None} over every worktree: how
    many there are, the uncommitted files across those whose status is known, how many are not loaded (no count
    yet), how many failed their last read, how many are watched, and the main tree's path."""
    rows = {p: r or {} for p, r in rows.items()}
    known = [r for r in rows.values() if r.get("uncommitted") is not None]
    return {"worktrees": len(rows), "uncommitted": sum(r["uncommitted"] for r in known), "loaded": len(known),
            "not_loaded": len(rows) - len(known), "dirty": sum(1 for r in known if r["uncommitted"]),
            "failed": sum(1 for r in rows.values() if r.get("status_error")),
            "watched": sum(1 for p in rows if p in watch),
            "main": next((p for p, why in watch.items() if why == "main"), None)}


class Sweeper:
    """Keeps the worktree statuses fresh while someone has the Version control page open (t-592).

    A file edit changes none of the files `_signature` stats, and a full pass is about 9 CPU s over 200 trees
    (git stats every tracked file), so no pass can run every 15 s. Instead each tick of SWEEP_TICK_S reads
    the hot worktrees (a live session, or a status/HEAD/gitdir/top-directory change, seen or found on disk when
    first met, within SWEEP_HOT_S) and a slice of the cold ones that costs a fixed share of the time every cold
    tree needs, so they all turn over in about SWEEP_COLD_S whatever each costs. A tree whose read fails is left
    out for SWEEP_RETRY_S, doubling to SWEEP_RETRY_MAX_S, then takes its place by age of its last good read. One thread runs it; it starts on touch()
    and ends SWEEP_IDLE_S after the last one, so an unwatched hub pays nothing.

    Only the watched set (`watched`: the main tree, the most recent and the largest worktrees, and those a viewer
    opened in the last SWEEP_IDLE_S) goes through the hot and cold lanes. A ranking lane reads at most one other
    worktree per tick, one whose last good read is older than SWEEP_RANK_S (never read first), so the largest stay
    roughly known without full passes: a tick's cost no longer grows with the number of worktrees."""

    def __init__(self, root, tick=SWEEP_TICK_S, idle=SWEEP_IDLE_S, cold=SWEEP_COLD_S, hot_max=SWEEP_HOT_MAX,
                 clock=time.monotonic, wall=time.time, recent=None, largest=None):
        self.root, self.tick_s, self.idle_s, self.cold_s, self.hot_max = root, tick, idle, cold, hot_max
        self.clock, self.wall = clock, wall
        self.recent, self.largest = recent, largest
        self.lock = threading.Lock()
        self.thread = None
        self.wanted = None
        self.live = set()
        self.opened = {}             # path -> clock time a viewer loaded it on demand
        self.watch = {}              # path -> reason, of the last tick
        self.ranked = None           # the worktree the last tick's ranking lane read, else None
        self.seen = {}               # path -> [top-level dir stat, wall time it last moved, status signature]
        self.cost = {}               # path -> moving average of the seconds one status read took
        self.fail = {}               # path -> [failures in a row, retry not before (clock), last attempt (clock)]
        self.backlog = 0             # hot worktrees left unread by the last tick's cap
        self.stop = threading.Event()
        self.ticks = 0

    def touch(self, live=None):
        """A request for statuses: keep (or start) the thread. `live` replaces the worktrees with a live session."""
        with self.lock:
            self.wanted = self.clock()
            if live is not None:
                self.live = set(live)
            if self.thread is None:
                self.stop.clear()
                self.thread = threading.Thread(target=self._run, daemon=True, name="vcs-sweep")
                self.thread.start()

    def _run(self):
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)       # git children inherit it
        except (OSError, AttributeError):
            pass
        while True:
            with self.lock:
                if self.stop.is_set() or self.clock() - self.wanted >= self.idle_s:
                    self.thread = None
                    return
            began = self.clock()
            try:
                self.sweep(pace=True)
            except Exception:
                pass                                       # the worktree list itself failed: tried again next tick
            self.stop.wait(max(0.0, self.tick_s - (self.clock() - began)))

    def open(self, path):
        """A viewer loaded this worktree on demand: it is watched for the next SWEEP_IDLE_S (the sweeper's idle)."""
        with self.lock:
            self.opened[path] = self.clock()

    def opened_paths(self):
        with self.lock:
            now = self.clock()
            for gone in [p for p, at in self.opened.items() if now - at >= self.idle_s]:
                del self.opened[gone]
            return list(self.opened)

    def select(self, trees, base=None):
        """{path: reason} of the worktrees kept live now (see `watched`)."""
        base = _base_or_none(self.root) if base is None else base
        return watch_set(self.root, trees, self.live, self.opened_paths(), base, self.recent, self.largest)

    def rank(self, trees, watch):
        """The one unwatched worktree the ranking lane reads this tick: never read first, then the longest since
        its last good read, and only once that read is older than SWEEP_RANK_S. A failing tree keeps its retry lane."""
        wall, best = self.wall(), None
        for w in trees:
            path = w["path"]
            if path in watch or path in self.fail or not os.path.isdir(path):
                continue
            hit = _recall(_STATUS, path)
            if hit is not None and wall - hit[4] < SWEEP_RANK_S:
                continue
            key = hit[4] if hit else float("-inf")
            if best is None or key < best[0]:
                best = (key, w)
        return best[1] if best else None

    def close(self):
        self.stop.set()
        thread = self.thread
        if thread is not None:
            thread.join(10)

    def pick(self, trees):
        """(hot, cold) worktrees to read this tick; also notes which ones moved."""
        now, wall, hot, cold = self.clock(), self.wall(), [], []
        for w in trees:
            path = w["path"]
            if not os.path.isdir(path):
                continue
            top, sig, hit = _stat(path), _signature(w), _recall(_STATUS, path)
            mark = self.seen.get(path)
            if mark is None:                               # first met: on-disk evidence says if it moved lately
                newest = _newest_mtime(w)
                mark = self.seen[path] = [top, newest if newest and wall - newest < SWEEP_HOT_S else None, sig]
            elif mark[0] != top or mark[2] != sig:
                mark[0], mark[1], mark[2] = top, wall, sig
            moved = _recall(_MOVED, path)
            if moved and (mark[1] is None or moved > mark[1]):
                mark[1] = moved
            if path in self.fail:
                continue                                   # a failing tree has its own lane (sweep) and never takes a cold slot
            key = hit[4] if hit else 0                     # longest unread first
            if os.path.realpath(path) in self.live or path in self.live or (mark[1] is not None and wall - mark[1] < SWEEP_HOT_S):
                hot.append((key, w))
            else:
                cold.append((key, w))
        hot.sort(key=lambda x: x[0])
        cold.sort(key=lambda x: x[0])
        self.backlog = max(0, len(hot) - self.hot_max)
        known = [self.cost[w["path"]] for _, w in cold if w["path"] in self.cost]
        guess = sum(known) / len(known) if known else 0.05
        costs = [self.cost.get(w["path"], guess) for _, w in cold]
        budget, spent, taken = sum(costs) * self.tick_s / self.cold_s, 0.0, []
        for (_, w), c in zip(cold, costs):
            if spent >= budget:
                break
            taken.append(w)
            spent += c
        return [w for _, w in hot[:self.hot_max]], taken

    def read(self, w, limit=SWEEP_READ_TIMEOUT_S):
        path, began = w["path"], time.monotonic()
        _LIMIT.timeout = limit
        try:
            _read_tree(w, cheap=True)
        except Exception:
            n, now = self.fail.get(path, [0])[0] + 1, self.clock()
            self.fail[path] = [n, now + min(SWEEP_RETRY_S * 2 ** (n - 1), SWEEP_RETRY_MAX_S), now]
            return
        finally:
            _LIMIT.timeout = None
        self.fail.pop(path, None)
        took = time.monotonic() - began
        old = self.cost.get(path)
        self.cost[path] = took if old is None else (old + took) / 2

    def sweep(self, pace=False):
        """One tick. With pace=True the reads are spread over most of the tick, so no 10 s window holds two bursts of
        them (a tick's reads together cost 1 to 3 CPU s; evenly spread they cost the same per second)."""
        trees = worktrees(self.root)
        watch = self.watch = self.select(trees)
        for gone in [p for p in self.seen if p not in watch]:
            del self.seen[gone]                            # a tree that comes back is first met again, on disk evidence
        hot, cold = self.pick([w for w in trees if w["path"] in watch])
        ranked = self.ranked = self.rank(trees, watch)
        work = hot + cold + ([ranked] if ranked else [])
        now = self.clock()
        due = [w for w in trees if w["path"] in self.fail and self.fail[w["path"]][1] <= now and os.path.isdir(w["path"])]
        retry = min(due, key=lambda w: self.fail[w["path"]][2], default=None)    # the longest since it was tried
        span = self.tick_s * 0.85 - (SWEEP_RETRY_TIMEOUT_S if retry else 0)      # room kept for the retry
        known = [self.cost[w["path"]] for w in work if w["path"] in self.cost]
        guess = sum(known) / len(known) if known else 0.05
        expect = sum(self.cost.get(w["path"], guess) for w in work)
        rest = max(0.0, span / expect - 1) if expect and span > 0 else 0.0     # idle time per second of reading
        start = self.clock()
        for i, w in enumerate(work):
            began = self.clock()
            if pace and i >= len(hot) and began - start >= span:
                break                                      # the cold slice cost more than its estimate: the rest waits
            self.read(w)
            now = self.clock()
            if pace and self.stop.wait(max(0.0, min((now - began) * rest, start + span - now))):
                break
        if retry is not None and not (pace and self.stop.is_set()):
            self.read(retry, SWEEP_RETRY_TIMEOUT_S)       # one failed tree per tick, after the healthy ones
        self.ticks += 1
        alive = {w["path"] for w in trees}
        for table in (self.seen, self.cost, self.fail):
            for gone in [p for p in table if p not in alive]:
                del table[gone]


def live_paths(view):
    """The worktree paths of a worktree_view with a live session in them."""
    return [w["path"] for w in view if (w.get("session") or {}).get("basis") == "live session working directory"]


def status_rows(root, trees=None):
    """{path: {uncommitted, oldest_change, checked_at, status_error}} from what is cached; nothing is read here (t-592).
    After a failed read the row keeps the last good count and its checked_at, and status_error names the failure.
    `trees` is the worktree list when the caller already has it; a tree never read and never failed maps to None."""
    out = {}
    for w in (worktrees(root) if trees is None else trees):
        hit = _recall(_STATUS, w["path"])
        failed = (_recall(_FAILED, w["path"]) or (None,))[0]
        out[w["path"]] = ({"uncommitted": len(hit[2]), "oldest_change": hit[3], "checked_at": hit[4], "status_error": failed}
                          if hit else ({"uncommitted": None, "oldest_change": None, "checked_at": None, "status_error": failed}
                                       if failed else None))
    return out


def _cached(path):
    """(files count or None, oldest change, read at) of a worktree from the status cache alone: no git runs."""
    hit = _recall(_STATUS, path)
    return (len(hit[2]), hit[3], hit[4]) if hit else (None, None, None)


def worktree_view(root, sessions=(), now=None, opened=()):
    """Every worktree with its branch, HEAD age, ahead and behind main, uncommitted files and session.

    Only the watched set (`watched`: main, the most recent, the largest, and the `opened` paths) has its status
    read here, when git's own files moved; any other row shows its cached count, or uncommitted None and loaded
    False when it was never read. Each row says why it is watched (`watch`: main, recent, largest, opened or None)."""
    now = now or time.time()
    base = main_branch(root)
    base_sha = _base_sha(root, base)
    out = []
    listed = worktrees(root)
    trees = [os.path.realpath(w["path"]) for w in listed]
    for w in listed:
        w["session"] = match_session(w["path"], sessions, now, trees)
    live = live_paths(listed)
    watch = watch_set(root, listed, live, opened, base)
    for w in listed:
        reason = watch.get(w["path"])
        failed = None
        if reason:
            files, oldest, checked = _tree_read(w)
            failed = (_recall(_FAILED, w["path"]) or (None,))[0]
            count = None if checked is None and failed else len(files)
        else:
            count, oldest, checked = _cached(w["path"])
            failed = (_recall(_FAILED, w["path"]) or (None,))[0]
        ahead, behind = ahead_behind(root, base, w["head"], base_sha) if w["head"] else (None, None)
        w.update(head_short=(w["head"] or "")[:8], head_time=_commit_time(root, w["head"]) if w["head"] else None,
                 ahead=ahead, behind=behind, uncommitted=count, oldest_change=oldest, checked_at=checked,
                 status_error=failed, is_main=w["branch"] == base, watch=reason,
                 loaded=count is not None)
        out.append(w)
    return out


def tree_row(root, path, trees=None):
    """One worktree's row read now, on demand (the page's "Load status"), with the sweeper's read timeout.
    None unless `path` is exactly one of the repository's worktree paths: no git runs anywhere else."""
    trees = worktrees(root) if trees is None else trees
    w = next((dict(t) for t in trees if t["path"] == path), None)
    if w is None:
        return None
    files = oldest = checked = None
    if os.path.isdir(path):
        _LIMIT.timeout = SWEEP_READ_TIMEOUT_S
        try:
            files, oldest, checked = _read_tree(w, cheap=True)
        except Exception:
            pass                                           # _FAILED names it; the last good read, if any, is shown
        finally:
            _LIMIT.timeout = None
    count = len(files) if files is not None else None
    if files is None:
        count, oldest, checked = _cached(path)
    base = main_branch(root)
    ahead, behind = ahead_behind(root, base, w["head"], _base_sha(root, base)) if w["head"] else (None, None)
    w.update(head_short=(w["head"] or "")[:8], head_time=_commit_time(root, w["head"]) if w["head"] else None,
             ahead=ahead, behind=behind, uncommitted=count, oldest_change=oldest, checked_at=checked,
             status_error=(_recall(_FAILED, path) or (None,))[0], is_main=w["branch"] == base, watch="opened",
             loaded=count is not None, generated=time.time())
    return w


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
    base_sha = _base_sha(root, base)
    out = []
    for name, sha, upstream, track, when in _rows(git(root, "for-each-ref", "refs/heads",
            "--format=%(refname:short)%1f%(objectname)%1f%(upstream:short)%1f%(upstream:track)%1f%(committerdate:unix)")):
        ahead, behind = ahead_behind(root, base, sha, base_sha)
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
    head = git(top, "rev-parse", "--verify", "--quiet", "HEAD", check=False).strip()
    by_path = {}
    for sid, ts, path in edits:
        by_path.setdefault(os.path.normpath(path), []).append((ts, sid))
    groups = {}
    if len(files) > MAX_FILES:
        groups["__capped__"] = {"session": "__capped__", "files": [], "count": len(files) - MAX_FILES}
    for code, rel in files[:MAX_FILES]:
        absolute = os.path.normpath(os.path.join(top, rel))
        cached = _recall(_LAST, (head, top, rel)) if head else None      # an int: immutable
        if cached is not None:
            last = cached
        else:
            last = git(top, "log", "-1", "--format=%ct", head or "HEAD", "--", rel, check=False).strip()
            last = int(last) if last.isdigit() else 0
            if head:
                _remember(_LAST, (head, top, rel), last)
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


def reflog(cwd, ref, label, sig=None):
    """Reflog entries of ref; with a signature of the reflog's files they are reused while it is unchanged."""
    key = (cwd, ref, label)
    hit = _recall(_REFLOG, key) if sig is not None else None
    if hit is not None and hit[0] == sig:
        return [dict(e) for e in hit[1]]                   # a copy: activity() extends and sorts what it gets
    out = _reflog(cwd, ref, label)
    if sig is not None:
        _remember(_REFLOG, key, (sig, [dict(e) for e in out]))
    return out


def _reflog(cwd, ref, label):
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
    _, common = _git_dirs(root)
    items = reflog(root, "refs/heads/" + base, base,
                   (_stat(os.path.join(common, "logs", "refs", "heads", base)), _stat(os.path.join(common, "packed-refs"))) if common else None)
    for w in trees:
        if os.path.isdir(w["path"]):
            label = w["branch"] or ("detached at " + (w["head"] or "")[:8])
            gitdir, _ = _git_dirs(w["path"])
            sig = (_stat(os.path.join(gitdir, "logs", "HEAD")), w.get("head")) if gitdir else None
            items += [dict(e, where="%s (%s)" % (label, os.path.basename(w["path"]))) for e in reflog(w["path"], "HEAD", label, sig)]
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

def snapshot(root, days=7, now=None, opened=()):
    """Everything the tab shows, from read-only git calls and the transcripts the board already reads.
    `opened`: worktree paths a viewer loaded on demand lately; they are read like the watched set."""
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
    view = worktree_view(root, sessions, now, opened)
    main = next((w for w in view if w["is_main"]), view[0] if view else None)
    dirty = status(main["path"]) if main else []
    # The repository is shown from its main working tree: worktrees nest inside it.
    watch = {w["path"]: w["watch"] for w in view if w["watch"]}
    return {"generated": now, "root": trees[0]["path"] if trees else toplevel(root), "main": main_branch(root), "worktrees": view,
            "overall": overall({w["path"]: w for w in view}, watch),
            "branches": branches(root), "graph": graph(root, days, now),
            "uncommitted": attribute(main["path"], dirty, edits) if main else [],
            "activity": activity(root, trees), "remotes": remotes(root),
            "denials": sorted(denials, key=lambda d: -d["ts"])[:30]}
