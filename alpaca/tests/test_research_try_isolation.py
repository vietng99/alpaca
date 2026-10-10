"""Straggler isolation per try (op-009 t-357, FU-4; design/research-engine/in-session-runtime.md
section 28).

A try dies by an event (an expiry, a refused claim, the run's stop) that stops no process, so a
call of the dead try that is still in flight, or a process it left, keeps writing by the paths its
job named. Since t-357 no output name a reader trusts is given to a second try: every implementer
tree cut has its own folder (`<task>`, then `<task>-c<n>`) and git folders, the reclaim of section
10 cuts the next tree and leaves the old one in place (its processes reaped as a best effort), and
teardown retires every dead cut before it destroys the live one.

Each test fails when the behaviour it guards is taken out:

  * every cut has its own folder and admin and host folders, the object store is shared, and each
    `research-worktree` event records its cut (paths.worktree_dir, cut_git_dir, cut_of);
  * a dead try's late write or delete, from a process inside its tree or one that left it,
    lands in the dead cut and never in the tree the attempt captures, on the in-step retry and
    on the resume reclaim (t-357 attacks a03, a04, a05);
  * the next try starts while a dead try floods its tree, and the flood is reaped (a11);
  * every try's prompt and spec name the tree it runs in (a13);
  * teardown retires each dead cut, records a cut it could not remove, and still passes;
  * the sweep takes every cut of a sealed attempt (a12);
  * the reap spares a process older than the dead cut, never leaves its base, and kills a
    process group only when the pid leads it;
  * `isolate` and `ensure_tree` cut the next name too when a resume finds the live folder gone,
    and leave the dead try's cut alone;
  * an attempt recorded before t-357 resumes into cut 2, and a relaunch with the same try number
    gets a new cut.

The record is a real one in the synthetic target repo of test_research_isolation.py, the trees
are real, and the launch is a stub; a straggler is a real child process. Nothing here starts an
agent runtime or touches the network.
"""
import dataclasses
import os
import shutil
import subprocess
import sys
import time
import types

import pytest

from alpaca import db, util
from alpaca.gates import verdict as vc
from alpaca.research import events as rev, isolation, jobs, lifecycle, paths, procs
from alpaca.research.errors import IsolationError
from alpaca.research.types import WorktreeInfo
from alpaca.tests.test_research_isolation import RUN, clone, matcher, repo  # noqa: F401

TASK = "t-344"
#: every research-worktree event of these tests is older than any process a test starts, so the
#: reap's cutoff (the dead cut's own event time) is never within the process clock's slack
OLD_TS = "2026-10-07T00:00:00+00:00"


@pytest.fixture
def old_clock():
    util.set_clock(lambda: OLD_TS)
    try:
        yield
    finally:
        util.set_clock(None)


class World:
    """An `_Attempt` of `TASK` over a real record and real trees of the synthetic clone; the
    prompt, the spec, the fingerprint points and the launch are stubs. The first tree is cut and
    recorded as the `isolate` step does."""

    def __init__(self, root, clone_info, launch=None, *, create=True):
        self.root = root
        self.conn = db.connect(root)
        self.specs = []
        self.live_at_launch = []       # the tree the record held when each try launched
        self.relaunch = False          # True: a started key with no end is launched again
        self.launch_fn = launch or (lambda conn, spec: {"outcome": "ok"})
        a = lifecycle._Attempt.__new__(lifecycle._Attempt)
        a.ctx = types.SimpleNamespace(root=root, run=RUN, clone=clone_info, stop=None, op=None,
                                      session="s-t357", runtime="claude",
                                      contract=types.SimpleNamespace(infra_retries=3))
        a.task = TASK
        a._research = {}
        a.verdict_set = lambda conn: False
        a.take_point = lambda conn, point, **kw: {}
        a.open_point = lambda: {}
        a.record_point = lambda conn, point, fp, **kw: None
        a.prompt = lambda conn, role, cwd, untrusted: "Work only in the tree %s.\n" % cwd
        a.repair_texts = lambda *x, **k: {}
        a.spec = lambda conn, role, try_n, **kw: dict(
            kw, key=paths.launch_key(TASK, role, try_n), role=role, try_n=try_n, attempt=TASK)
        a.launch = self._launch
        self.a = a
        if create:
            wt = isolation.create_worktree(root, RUN, TASK, clone_info,
                                           commit=clone_info.base_commit, cut=a.next_cut(self.conn))
            a.record_tree(self.conn, "create", wt)

    def emit(self, kind, data):
        return rev.emit(self.conn, kind, run=RUN, ref=TASK, op=None, session="s-t357", data=data)

    def _launch(self, conn, spec):
        started = [ev for ev in lifecycle._events(conn, rev.LAUNCH, TASK)
                   if ev["data"].get("key") == spec["key"]]
        if started and not self.relaunch:
            self.emit(rev.INFRA, {"key": spec["key"], "class": "lost", "detail": "found on resume"})
            return {"outcome": "lost", "infra": "lost"}
        self.specs.append(spec)
        self.live_at_launch.append(self.a.worktree(conn))
        self.emit(rev.LAUNCH, {"role": spec["role"], "key": spec["key"], "cwd": spec["cwd"]})
        res = dict(self.launch_fn(conn, spec) or {"outcome": "ok"})
        if res.get("infra") == "lost":
            self.emit(rev.INFRA, {"key": spec["key"], "class": "lost",
                                  "job": "j-" + spec["key"].replace("/", "-")})
        return res

    def implement(self):
        return self.a.implement(self.conn)

    def trees(self):
        return [ev["data"] for ev in lifecycle._events(self.conn, rev.WORKTREE, TASK)]

    def close(self):
        self.conn.close()


@pytest.fixture
def world(repo, clone, old_clock):
    worlds = []

    def make(launch=None, **kw):
        w = World(repo["root"], clone, launch, **kw)
        worlds.append(w)
        return w
    yield make
    for w in worlds:
        w.close()


def lost(conn, spec):
    return {"outcome": "infra", "infra": "lost"}


# ---------------------------------------------------------------- a straggler process
_STRAGGLER = r"""
import os, sys, time
go, ready, target, mode, action = sys.argv[1:6]
if mode == "away":
    os.chdir("/")              # holds nothing under the tree: no cwd, no open file
open(ready, "w").close()
end = time.time() + 20
while not os.path.exists(go) and time.time() < end:
    time.sleep(0.01)
if action == "write":
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w") as fh:
        fh.write("late write of a dead try\n")
elif action == "delete":
    try:
        os.remove(target)
    except OSError:
        pass
"""


class Straggler:
    """A child process standing in for a dead try's in-flight call: it starts in the dead try's
    tree (or moves to / with mode "away"), waits for `go`, then writes or deletes `target`, the
    absolute path its own job named."""

    def __init__(self, scratch, tree, target, *, mode="in", action="write"):
        os.makedirs(scratch, exist_ok=True)
        self.go = os.path.join(scratch, "go")
        self.ready = os.path.join(scratch, "ready")
        self.proc = subprocess.Popen([sys.executable, "-c", _STRAGGLER, self.go, self.ready,
                                      target, mode, action], cwd=tree)
        end = time.time() + 10
        while not os.path.exists(self.ready) and time.time() < end:
            time.sleep(0.01)

    def release(self, wait=5.0):
        open(self.go, "w").close()
        try:
            self.proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            pass

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()


_FLOOD = r"""
import os, sys, time
base = sys.argv[1]
end = time.time() + float(sys.argv[2])
i = 0
while time.time() < end:
    d = os.path.join(base, "build", "d%02d" % (i % 20))
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "f%d" % i), "w") as fh:
            fh.write("x")
    except OSError:
        pass
    i += 1
"""


def _sleeper(cwd, *, seconds=30, new_session=False):
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(%d)" % seconds],
                            cwd=cwd, start_new_session=new_session)


def _alive(pid):
    try:
        with open("/proc/%d/stat" % pid) as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


# ---------------------------------------------------------------- 1. a folder per cut
def test_each_cut_gets_its_own_folder_and_git_folders(world):
    w = world()
    first = w.a.worktree(w.conn)
    second = w.a._reclaim_tree(w.conn, first, 1)
    third = w.a._reclaim_tree(w.conn, second, 2)
    wdir = paths.worktrees_dir(w.root, RUN)
    git = os.path.join(paths.attempt_dir(w.root, RUN, TASK), "git")
    assert [t.path for t in (first, second, third)] == [
        os.path.join(wdir, TASK), os.path.join(wdir, TASK + "-c2"), os.path.join(wdir, TASK + "-c3")]
    assert [t.admin_dir for t in (first, second, third)] == [
        os.path.join(git, "admin"), os.path.join(git, "c2", "admin"),
        os.path.join(git, "c3", "admin")]
    assert [t.host_git for t in (first, second, third)] == [
        os.path.join(git, "host"), os.path.join(git, "c2", "host"), os.path.join(git, "c3", "host")]
    assert {t.private_objects for t in (first, second, third)} == {os.path.join(git, "objects")}
    # no reclaim removed the tree before it: every cut is still on disk until teardown
    assert all(os.path.isdir(t.path) for t in (first, second, third))
    trees = w.trees()
    assert [(d["action"], d["cut"]) for d in trees] == [
        ("create", 1), ("reclaim", 2), ("reclaim", 3)]
    assert trees[1]["retired"] == os.path.relpath(first.path, w.root)
    assert w.a.worktree(w.conn).path == third.path and w.a.next_cut(w.conn) == 4


def test_the_cut_name_form_is_owned_by_one_reader(tmp_path):
    root = str(tmp_path)
    assert paths.cut_of("t-7") == ("t-7", 1) and paths.cut_of("t-7-c2") == ("t-7", 2)
    assert paths.cut_of("t-7-c12") == ("t-7", 12)
    for name in ("t-7-c1", "t-7-c02", "t-7-c", "t-7-r2-champion", "r2-champion", "t-7.t2", "x"):
        assert paths.cut_of(name) is None, name
    assert paths.worktree_dir(root, RUN, "t-7") == paths.worktree_dir(root, RUN, "t-7", 1)
    assert os.path.basename(paths.worktree_dir(root, RUN, "t-7", 3)) == "t-7-c3"
    assert paths.cut_git_dir(root, RUN, "t-7", 1).endswith(os.path.join("t-7", "git"))
    assert paths.cut_git_dir(root, RUN, "t-7", 3).endswith(os.path.join("t-7", "git", "c3"))
    for bad in (0, -1, True, "2", 1.0):
        with pytest.raises(ValueError):
            paths.worktree_dir(root, RUN, "t-7", bad)


# ---------------------------------------------------------------- 2-4. late writes and deletes
@pytest.mark.parametrize("mode", ["in", "away"])
def test_a_dead_trys_late_write_lands_outside_the_live_tree(world, tmp_path, mode):
    box = {}

    def launch(conn, spec):
        if spec["try_n"] == 1:
            box["dead"] = spec["cwd"]
            box["s"] = Straggler(str(tmp_path / "sync"), spec["cwd"],
                                 os.path.join(spec["cwd"], "src", "late.py"), mode=mode)
            return lost(conn, spec)
        box["s"].release()                       # try 1's call lands while try 2 runs
        return {"outcome": "ok"}

    w = world(launch)
    try:
        w.implement()
    finally:
        if "s" in box:
            box["s"].close()
    assert [s["try_n"] for s in w.specs] == [1, 2]
    live = w.a.worktree(w.conn).path
    assert os.path.realpath(live) == os.path.realpath(w.specs[1]["cwd"]) != box["dead"]
    assert not os.path.exists(os.path.join(live, "src", "late.py"))
    if mode == "away":
        # nothing under the tree held it, so it was not reaped: its write is in its own dead cut
        assert os.path.isfile(os.path.join(box["dead"], "src", "late.py"))


@pytest.mark.parametrize("mode", ["in", "away"])
def test_a_dead_trys_late_delete_leaves_the_live_edit(world, tmp_path, mode):
    edit = "def f():\n    return 2  # try 2's own edit\n"
    box = {}

    def launch(conn, spec):
        if spec["try_n"] == 1:
            box["s"] = Straggler(str(tmp_path / "sync"), spec["cwd"],
                                 os.path.join(spec["cwd"], "src", "app.py"), mode=mode,
                                 action="delete")
            return lost(conn, spec)
        with open(os.path.join(spec["cwd"], "src", "app.py"), "w") as fh:
            fh.write(edit)
        box["s"].release()
        return {"outcome": "ok"}

    w = world(launch)
    try:
        w.implement()
    finally:
        if "s" in box:
            box["s"].close()
    with open(os.path.join(w.a.worktree(w.conn).path, "src", "app.py")) as fh:
        assert fh.read() == edit


def test_the_resume_reclaim_cuts_a_new_tree_too(world, tmp_path):
    box = {}

    def launch(conn, spec):
        box["s"].release()                       # try 1's call lands while the next try runs
        return {"outcome": "ok"}

    w = world(launch)
    first = w.a.worktree(w.conn).path
    # try 1 launched, then the runner died: no end and no infra row
    w.emit(rev.LAUNCH, {"role": "implementer", "key": paths.launch_key(TASK, "implementer", 1),
                        "cwd": first})
    w.relaunch = True
    box["s"] = Straggler(str(tmp_path / "sync"), first, os.path.join(first, "src", "late.py"),
                         mode="away")
    try:
        w.implement()
    finally:
        box["s"].close()
    live = w.a.worktree(w.conn).path
    assert live != first and w.specs[-1]["cwd"] == live
    assert not os.path.exists(os.path.join(live, "src", "late.py"))
    assert [d["action"] for d in w.trees()] == ["create", "reclaim"]


# ---------------------------------------------------------------- 5. a busy dead tree
def test_the_next_try_starts_while_a_dead_try_floods_its_tree(world):
    box = {}

    def launch(conn, spec):
        if spec["try_n"] == 1:
            box["p"] = subprocess.Popen([sys.executable, "-c", _FLOOD, spec["cwd"], "20"],
                                        cwd=spec["cwd"])
            time.sleep(0.5)                       # thousands of files exist before the next cut
            return lost(conn, spec)
        return {"outcome": "ok"}

    w = world(launch)
    try:
        w.implement()                             # no exception out of the reclaim
        assert [s["try_n"] for s in w.specs] == [1, 2]
        reclaim = [d for d in w.trees() if d["action"] == "reclaim"]
        assert len(reclaim) == 1 and box["p"].pid in reclaim[0]["reaped"], reclaim
        box["p"].wait(timeout=5)
        assert box["p"].returncode is not None
    finally:
        if "p" in box and box["p"].poll() is None:
            box["p"].kill()
            box["p"].wait()


# ---------------------------------------------------------------- 6. each try is told its tree
def test_every_try_is_told_its_own_tree(world):
    w = world(lambda conn, spec: lost(conn, spec) if spec["try_n"] < 3 else {"outcome": "ok"})
    w.implement()
    assert [s["try_n"] for s in w.specs] == [1, 2, 3]
    assert len({s["cwd"] for s in w.specs}) == 3
    for spec, live in zip(w.specs, w.live_at_launch):
        assert spec["cwd"] == spec["workdir"] == live.path
        assert spec["cwd"] in spec["prompt"]
        assert tuple(spec["write_paths"]) == (spec["cwd"],)
        assert spec["git_admin"] == live.admin_dir and spec["git_objects"] == live.private_objects


# ---------------------------------------------------------------- 7. teardown
def test_teardown_retires_every_dead_cut_then_destroys_the_live_one(world, monkeypatch):
    w = world()
    first = w.a.worktree(w.conn)
    second = w.a._reclaim_tree(w.conn, first, 1)
    third = w.a._reclaim_tree(w.conn, second, 2)
    real = isolation._rmtree

    def busy(path):
        if os.path.abspath(path) == os.path.abspath(second.path):
            raise OSError(39, "Directory not empty")      # a writer still holds cut 2
        return real(path)
    monkeypatch.setattr(isolation, "_rmtree", busy)
    assert w.a.teardown(w.conn) == vc.PASS
    tail = [d for d in w.trees() if d["action"] in ("retire", "destroy")]
    assert [(d["action"], d["cut"]) for d in tail] == [("retire", 1), ("retire", 2), ("destroy", 3)]
    assert tail[0]["left"] == [] and tail[0]["existed"] is True and tail[0]["refused"] is None
    assert tail[1]["left"] == [os.path.relpath(second.path, w.root)]
    assert not any("error" in d for d in tail[:2])   # a retire never reads as a broken verdict
    assert w.a.worktree(w.conn) is None
    assert not os.path.lexists(first.path) and not os.path.lexists(third.path)
    assert os.path.isdir(second.path)                     # left to the sweep
    assert not os.path.lexists(os.path.dirname(third.admin_dir))   # git/c3 is gone too
    # a second teardown changes nothing
    n = len(w.trees())
    assert w.a.teardown(w.conn) == vc.PASS and len(w.trees()) == n


# ---------------------------------------------------------------- 8. the sweep
def test_sweep_takes_every_cut_of_a_sealed_attempt(world):
    w = world()
    first = w.a.worktree(w.conn)
    w.a._reclaim_tree(w.conn, w.a._reclaim_tree(w.conn, first, 1), 2)
    got = isolation.sweep(w.root, RUN, sealed=lambda *x: True, dry_run=False, grace_s=-60,
                          alive=lambda *x: False)
    assert len(got) == 9, got                 # three trees, each with its admin and host
    wdir = paths.worktrees_dir(w.root, RUN)
    assert os.listdir(wdir) == []
    git = os.path.join(paths.attempt_dir(w.root, RUN, TASK), "git")
    assert sorted(os.listdir(git)) == ["objects"]


def test_the_sweep_leaves_an_unsealed_attempts_cuts(world):
    w = world()
    w.a._reclaim_tree(w.conn, w.a.worktree(w.conn), 1)
    got = isolation.sweep(w.root, RUN, sealed=lambda *x: False, dry_run=False, grace_s=-60,
                          alive=lambda *x: False)
    assert got == [] and sorted(os.listdir(paths.worktrees_dir(w.root, RUN))) == [
        TASK, TASK + "-c2"]


# ---------------------------------------------------------------- 9. git folders of a cut
def test_made_admin_maps_a_cut_to_its_git_folders_and_refuses_another_tasks(repo, clone):
    root = repo["root"]
    mine = isolation.create_worktree(root, RUN, "t-5", clone, commit=clone.base_commit, cut=2)
    other = isolation.create_worktree(root, RUN, "t-6", clone, commit=clone.base_commit, cut=2)
    admins, hosts = isolation._made_admin(mine)
    assert mine.admin_dir in admins and mine.host_git in hosts
    assert other.admin_dir not in admins
    forged = dataclasses.replace(mine, admin_dir=other.admin_dir)
    for fn in (isolation.reclaim, isolation.retire):
        with pytest.raises(IsolationError):
            fn(root, RUN, "t-5", clone, recorded=forged)
        with pytest.raises(IsolationError):
            fn(root, RUN, "t-5", clone, recorded=other)          # another task's cut
    assert os.path.isdir(other.path) and os.path.isdir(other.admin_dir)
    assert isolation.reclaim(root, RUN, "t-5", clone, recorded=mine) is True
    assert not any(os.path.lexists(p) for p in (mine.path, mine.admin_dir, mine.host_git))


# ---------------------------------------------------------------- 10. an attempt from before t-357
def test_an_old_layout_attempt_resumes_into_cut_2(world):
    w = world(create=False)
    wt = isolation.create_worktree(w.root, RUN, TASK, w.a.ctx.clone,
                                   commit=w.a.ctx.clone.base_commit)
    # the event an engine before t-357 wrote: no `cut` field
    w.emit(rev.WORKTREE, {"action": "create", "path": os.path.relpath(wt.path, w.root),
                          "admin_dir": os.path.relpath(wt.admin_dir, w.root),
                          "host_git": os.path.relpath(wt.host_git, w.root),
                          "index": os.path.relpath(wt.index, w.root),
                          "private_objects": os.path.relpath(wt.private_objects, w.root),
                          "commit": wt.commit, "head": wt.commit, "error": None})
    assert w.a.next_cut(w.conn) == 2
    new = w.a._reclaim_tree(w.conn, w.a.worktree(w.conn), 1)
    assert os.path.basename(new.path) == TASK + "-c2" and os.path.isdir(wt.path)


# ---------------------------------------------------------------- 11-12. the reap
def test_the_reap_spares_a_process_older_than_the_dead_cut(tmp_path):
    import datetime
    base = tmp_path / "trees"
    tree = base / "t-1-c2"
    tree.mkdir(parents=True)
    old = _sleeper(str(tree))
    try:
        time.sleep(0.3)
        since = datetime.datetime.now(datetime.timezone.utc)
        time.sleep(0.3)
        new = _sleeper(str(tree))
        try:
            got = jobs.reap([str(tree)], since, bases=[str(base)])
            assert got["killed"] == [new.pid] and got["left"] == [], got
            assert [s["pid"] for s in got["spared"]] == [old.pid]
            assert got["spared"][0]["why"] == "started before the job was handed"
            new.wait(timeout=5)
            assert _alive(old.pid)
        finally:
            if new.poll() is None:
                new.kill()
            new.wait()
    finally:
        old.kill()
        old.wait()


def test_the_reap_never_leaves_its_base(tmp_path):
    import datetime
    since = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    base = tmp_path / "trees"
    base.mkdir()
    p = _sleeper(str(outside))
    try:
        for folders, bases in (([str(outside)], [str(base)]), ([str(base)], [str(base)]),
                               ([str(base / ".." / "elsewhere")], [str(base)])):
            got = jobs.reap(folders, since, bases=bases)
            assert got["killed"] == [], (folders, got)
        assert _alive(p.pid)
        assert jobs.reap([str(outside)], None, bases=[str(tmp_path)])["killed"] == []
        assert _alive(p.pid)
    finally:
        p.kill()
        p.wait()


_LEADER = r"""
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
with open(sys.argv[1], "w") as fh:
    fh.write(str(child.pid))
time.sleep(30)
"""


def test_the_reap_kills_a_group_only_when_the_pid_leads_it(tmp_path):
    note = str(tmp_path / "child.pid")
    leader = subprocess.Popen([sys.executable, "-c", _LEADER, note], start_new_session=True)
    try:
        end = time.time() + 10
        while not (os.path.exists(note) and open(note).read()) and time.time() < end:
            time.sleep(0.02)
        member = int(open(note).read())
        assert os.getpgid(member) == leader.pid != member
        # the member does not lead its group: only it is killed, the leader lives
        assert member in procs.kill_tree(member, lead_only=True)
        time.sleep(0.2)
        assert not _alive(member) and _alive(leader.pid)
        # the leader leads its group: the group goes with it
        assert leader.pid in procs.kill_tree(leader.pid, lead_only=True)
        leader.wait(timeout=5)
    finally:
        if leader.poll() is None:
            leader.kill()
            leader.wait()


# ---------------------------------------------------------------- 13. a relaunch of the same try
def test_a_relaunch_with_the_same_try_number_gets_a_new_cut(world):
    w = world()
    w.relaunch = True
    first = w.a.worktree(w.conn).path
    key = paths.launch_key(TASK, "implementer", 1)
    # the runner died while try 1 ran: a launch with no end and no infra row
    w.emit(rev.LAUNCH, {"role": "implementer", "key": key, "cwd": first})

    def killed(conn, spec):
        raise KeyboardInterrupt("the runner dies again before try 1's end is written")
    w.launch_fn = killed
    with pytest.raises(KeyboardInterrupt):
        w.implement()
    second = w.specs[-1]["cwd"]
    assert w.specs[-1]["key"] == key and second != first
    # the next resume relaunches try 1 once more, on a third cut
    w.launch_fn = lambda conn, spec: {"outcome": "ok"}
    w.implement()
    third = w.specs[-1]["cwd"]
    assert w.specs[-1]["key"] == key and len({first, second, third}) == 3
    assert [d["cut"] for d in w.trees()] == [1, 2, 3]


# ---------------------------------------------------------------- 14. the two other cut sites
# Every step that cuts a tree takes the next cut (section 28.1): the reclaim (tests above), and
# also `isolate` and `ensure_tree` when a resume finds the live folder gone. Cut 1 here is a dead
# try's tree that is still on disk: a cut at its name would remove it and cut it again, so its late
# writes would land in the tree the attempt captures.
def _dead_first_cut(w):
    """Cut 1 (the dead try's tree, with a marker file) and cut 2, the live one, reclaimed from it."""
    first = w.a.worktree(w.conn)
    second = w.a._reclaim_tree(w.conn, first, 1)
    marker = os.path.join(first.path, "dead-try-marker")
    with open(marker, "w") as fh:
        fh.write("the dead try's tree\n")
    return first, second, marker


def test_isolate_cuts_the_next_name_when_the_live_folder_is_gone(world, clone):
    w = world()
    first, second, marker = _dead_first_cut(w)
    shutil.rmtree(second.path)
    w.a.opened = lambda conn: {"champion_commit": clone.base_commit}
    assert w.a.isolate(w.conn) == vc.PASS
    live = w.a.worktree(w.conn)
    assert os.path.basename(live.path) == TASK + "-c3", live.path
    assert os.path.isdir(live.path)
    last = w.trees()[-1]
    assert (last["action"], last["cut"]) == ("reclaim", 3)
    assert last["retired"] == os.path.relpath(second.path, w.root)
    assert os.path.isfile(marker)                    # the dead try's cut 1 is not touched


def test_ensure_tree_rebuilds_at_the_next_cut(world, clone):
    w = world()
    first, second, marker = _dead_first_cut(w)
    with open(os.path.join(second.path, "src", "app.py"), "w") as fh:
        fh.write("def f():\n    return 2\n")
    out_dir = paths.attempt_dir(w.root, RUN, TASK)
    cap = isolation.capture(clone, second, forked_from=second.commit, message="attempt t-344",
                            stamp="20261007t000000z", out_dir=out_dir)
    w.emit(rev.DIFF, {"forked_from": cap.forked_from, "head": cap.head, "tree": cap.tree,
                      "path": os.path.relpath(cap.path, w.root), "sha256": cap.sha256,
                      "bytes": cap.bytes, "paths": list(cap.paths), "gitlinks": list(cap.gitlinks),
                      "empty": cap.empty, "error": None, "stamp": cap.stamp,
                      "message": cap.message})
    shutil.rmtree(second.path)
    before = max(d["cut"] for d in w.trees())
    got = w.a.ensure_tree(w.conn)
    assert got is not None
    assert os.path.basename(got.path) == TASK + "-c3", got.path
    with open(os.path.join(got.path, "src", "app.py")) as fh:
        assert fh.read() == "def f():\n    return 2\n"
    last = w.trees()[-1]
    assert last["action"] == "rebuild" and last["cut"] > before
    assert w.a.worktree(w.conn).path == got.path
    assert os.path.isfile(marker)                    # the dead try's cut 1 is not touched
