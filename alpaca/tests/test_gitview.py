"""t-158: the Version control tab reads a throwaway repository without writing to it."""
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from alpaca import db, gitview, transcripts

WEB = Path(__file__).resolve().parents[1] / "web"
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def sh(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def commit(cwd, name, text, message):
    Path(cwd, name).write_text(text)
    sh(cwd, "add", name)
    sh(cwd, "commit", "-q", "-m", message)


@pytest.fixture
def repo(project, tmp_path, monkeypatch):
    for key, value in (("GIT_AUTHOR_NAME", "A"), ("GIT_AUTHOR_EMAIL", "a@example.com"), ("GIT_COMMITTER_NAME", "A"),
                       ("GIT_COMMITTER_EMAIL", "a@example.com"), ("GIT_CONFIG_GLOBAL", os.devnull),
                       ("GIT_CONFIG_NOSYSTEM", "1")):
        monkeypatch.setenv(key, value)
    root = Path(project)
    sh(root, "init", "-q", "-b", "main")
    (root / ".gitignore").write_text(".alpaca/\nsrc/\nstyle/\nALPACA-MANIFEST\n")
    sh(root, "add", ".gitignore")
    commit(root, "a.txt", "one\n", "Start the project (op-001)")
    sh(root, "checkout", "-q", "-b", "feature")
    commit(root, "f.txt", "f\n", "Build the feature for t-101")
    sh(root, "checkout", "-q", "main")
    commit(root, "m.txt", "m\n", "Work on main")
    sh(root, "merge", "-q", "--no-ff", "feature", "-m", "Merge feature (op-002 t-101)")
    sh(root, "checkout", "-q", "-b", "open")
    commit(root, "o.txt", "o\n", "Unmerged work for t-102")
    sh(root, "checkout", "-q", "main")
    commit(root, "r.txt", "r\n", "A commit to undo")
    sh(root, "reset", "-q", "--hard", "HEAD~1")
    Path(root, "s.txt").write_text("stash me\n")
    sh(root, "add", "s.txt")
    sh(root, "stash", "push", "-q", "-m", "parked work")
    sh(root, "tag", "v1.0")
    sh(root, "remote", "add", "origin", "https://user:secret@example.com/team/repo.git")
    wt = tmp_path / "wt"
    sh(root, "worktree", "add", "-q", "-b", "wt-branch", str(wt))
    commit(wt, "w.txt", "w\n", "Worktree work")
    # Uncommitted: a.txt edited by session s1 after its last commit, b.txt touched by nobody.
    time.sleep(1.1)
    Path(root, "a.txt").write_text("two\n")
    Path(root, "b.txt").write_text("new\n")
    now = time.time()
    conn = db.connect(project)
    db.upsert(conn, "sessions", "sid", {"sid": "s1", "started": "2026-10-01T00:00:00Z", "cwd": str(wt),
                                         "last_beat": datetime.fromtimestamp(now, tz=timezone.utc).isoformat()})
    conn.close()
    stamp = datetime.fromtimestamp(now, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    records = [{"type": "assistant", "timestamp": stamp, "message": {"id": "r1", "model": "claude-opus-5", "content": [
                    {"type": "tool_use", "id": "e1", "name": "Edit", "input": {"file_path": str(root / "a.txt")}},
                    {"type": "tool_use", "id": "g1", "name": "Bash", "input": {"command": "git stash"}}],
                "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}},
               {"type": "user", "timestamp": stamp, "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "g1", "is_error": True,
                     "content": "Alpaca git guard: git stash moves every change in the shared main tree"}]}}]
    path = Path(transcripts.local_path(project, "s1"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))
    return root, wt


def git_state(root):
    """Modification times and sizes of the index, every ref, reflog and worktree admin file."""
    git_dir = root / ".git"
    out = {}
    for path in sorted(git_dir.rglob("*")):
        if path.is_file() and ("objects" not in path.parts):
            st = path.stat()
            out[str(path.relative_to(git_dir))] = (st.st_mtime_ns, st.st_size)
    return out


def test_every_section_and_no_write(repo):
    root, wt = repo
    os.utime(root / "m.txt")                              # a stat change status would like to refresh
    before = git_state(root)
    d = gitview.snapshot(str(root), days=7)
    assert git_state(root) == before                     # no index, ref, reflog or lock written
    trees = {Path(w["path"]).name: w for w in d["worktrees"]}
    main, side = trees[root.name], trees["wt"]
    assert main["is_main"] and main["uncommitted"] == 2 and main["oldest_change"]
    assert side["branch"] == "wt-branch" and (side["ahead"], side["behind"]) == (1, 0)
    assert side["session"] == {"sid": "s1", "basis": "live session working directory"}
    assert main["session"] == {"sid": "s1", "basis": "last tool call in this worktree", "at": main["session"]["at"]}
    branches = {b["name"]: b for b in d["branches"]}
    assert branches["feature"]["merged"] and not branches["open"]["merged"] and branches["open"]["ahead"] == 1
    commits = {c["subject"]: c for c in d["graph"]["commits"]}
    merge = commits["Merge feature (op-002 t-101)"]
    assert merge["merge"] and merge["tasks"] == ["t-101"] and merge["ops"] == ["op-002"] and len(merge["edges"]) == 2
    assert commits["Unmerged work for t-102"]["lane"] != commits["Work on main"]["lane"]
    assert "A commit to undo" not in commits                    # reset away: no branch reaches it
    groups = {g["session"]: [f["path"] for f in g["files"]] for g in d["uncommitted"]}
    assert groups["s1"] == ["a.txt"] and groups[None] == ["b.txt"]
    actions = {e["action"] for e in d["activity"]["items"]}
    assert {"commit", "merge", "reset", "checkout"} <= actions
    assert [s["message"] for s in d["activity"]["stash"]] == ["On main: parked work"]
    r = d["remotes"]
    assert r["remotes"] == [{"name": "origin", "url": "https://example.com/team/repo.git"}]   # no credential shown
    assert r["fetch_head"] is None and [t["tag"] for t in r["tags"]] == ["v1.0"]
    assert [(x["session"], x["line"]) for x in d["denials"]] == [("s1", 2)]


def test_lanes_keep_first_parent_chains_apart():
    commits = [{"sha": "m", "parents": ["a", "b"]}, {"sha": "b", "parents": ["a"]}, {"sha": "a", "parents": []}]
    assert gitview.lanes(commits) == {"m": 0, "b": 1, "a": 0}
    tips = [{"sha": "o", "parents": ["m"]}, {"sha": "m", "parents": ["a", "b"]}, {"sha": "b", "parents": ["x"]},
            {"sha": "a", "parents": ["x"]}, {"sha": "x", "parents": []}]
    got = gitview.lanes(tips, trunk=["m", "a", "x"])
    assert got["m"] == got["a"] == got["x"] == 0 and got["o"] != 0 and got["b"] != 0   # main keeps lane 0


def test_the_tab_is_wired_read_only():
    hub = (WEB / "hub.js").read_text(encoding="utf-8")
    # the nav entry comes from the page registry (pages.json); hub.js maps the id to its renderer
    page = {p["id"]: p for p in json.loads((WEB / "pages.json").read_text(encoding="utf-8"))["pages"]}["vcs"]
    assert page["label"] == "Version control" and page["renderer"] == "builtin" and not page.get("gate")
    assert "vcs:vcsView" in hub
    source = (Path(gitview.__file__)).read_text(encoding="utf-8")
    import re
    calls = re.findall(r'git\([a-z_\[\]"]+, "([a-z-]+)", "?([a-z-]*)', source)
    verbs = {verb for verb, _ in calls}
    assert verbs <= {"rev-parse", "symbolic-ref", "worktree", "status", "log", "for-each-ref", "rev-list", "stash",
                     "remote"}, verbs
    # symbolic-ref only reads: one argument, HEAD
    assert re.findall(r'git\([a-z_]+, "symbolic-ref", ([^)]*)\)', source) == ['"--quiet", "--short", "HEAD", check=False'], source
    assert {sub for verb, sub in calls if verb == "stash"} == {"list"}
    assert {sub for verb, sub in calls if verb == "worktree"} == {"list"}
    assert 'GIT_OPTIONAL_LOCKS="0"' in source and '"--no-optional-locks"' in source


def test_graph_cap_reports_only_actual_omissions(repo, monkeypatch):
    root, _ = repo
    total = len(gitview.graph(str(root))['commits'])
    monkeypatch.setattr(gitview, 'MAX_COMMITS', total)
    exact = gitview.graph(str(root))
    assert len(exact['commits']) == total
    assert exact['truncated'] is False
    monkeypatch.setattr(gitview, 'MAX_COMMITS', total - 1)
    capped = gitview.graph(str(root))
    assert len(capped['commits']) == total - 1
    assert capped['truncated'] is True
    shown = {c['sha'] for c in capped['commits']}
    assert all(e['inside'] == (e['to'] in shown)
               for c in capped['commits'] for e in c['edges'])


def test_a_repository_with_no_commit_yet(project, monkeypatch):
    """`alpaca new` runs `git init` and commits nothing: the tab shows the branch and empty sections."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    sh(project, "init", "-q", "-b", "trunk-to-be")
    Path(project, "notes.txt").write_text("new\n")
    d = gitview.snapshot(project, days=7)
    assert d["main"] == "trunk-to-be" and d["branches"] == [] and d["graph"]["commits"] == []
    [tree] = d["worktrees"]
    assert tree["is_main"] and tree["head"] is None and tree["head_short"] == "" and tree["uncommitted"] >= 1
    assert d["activity"] == {"items": [], "stash": []}


def test_a_folder_with_no_repository(project):
    """`alpaca new --no-git`: the snapshot says there is no repository instead of failing."""
    if gitview.git(project, "rev-parse", "--git-dir", check=False).strip():
        pytest.skip("the test folder sits inside a git repository")
    d = gitview.snapshot(project, days=7)
    assert d["repository"] is False and d["root"] == os.path.realpath(project)
    assert "d.repository === false" in (WEB / "vcs.js").read_text(encoding="utf-8")


# ---- t-585: cached snapshots ------------------------------------------------------------

def _count_git(monkeypatch):
    calls = []
    real = gitview.git

    def counting(cwd, *args, **kw):
        calls.append((str(cwd), args))
        return real(cwd, *args, **kw)

    monkeypatch.setattr(gitview, "git", counting)
    return calls


def _shape(d):
    """A snapshot without the clock and the per-run session fields."""
    d = json.loads(json.dumps(d))
    d.pop("generated", None)
    for w in d.get("worktrees", []):
        w.pop("checked_at", None)                          # when the status was read differs by design (t-592)
    return d


def test_cached_snapshot_equals_a_fresh_one_and_skips_unchanged_worktrees(repo, monkeypatch):
    root, wt = repo
    now = time.time()
    gitview.reset_caches()
    first = gitview.snapshot(str(root), days=7, now=now)
    calls = _count_git(monkeypatch)
    warm = gitview.snapshot(str(root), days=7, now=now)
    assert _shape(warm) == _shape(first)
    assert not [c for c in calls if c[1][0] == "status" and c[0] != str(root)]          # the side worktree: no status
    assert not [c for c in calls if c[1][0] == "rev-list" and "--count" in c[1]]        # no ahead/behind recount
    assert not [c for c in calls if c[1][0] == "log" and "-g" in c[1] and c[0] != str(root)]
    gitview.reset_caches()
    assert _shape(gitview.snapshot(str(root), days=7, now=now)) == _shape(first)       # cached equals fresh


def test_a_commit_a_new_worktree_and_a_removed_worktree_show_after_a_rebuild(repo, tmp_path):
    root, wt = repo
    gitview.reset_caches()
    d = gitview.snapshot(str(root), days=7)
    side = {Path(w["path"]).name: w for w in d["worktrees"]}["wt"]
    assert (side["ahead"], side["uncommitted"]) == (1, 0)
    commit(wt, "w2.txt", "w2\n", "More worktree work")
    Path(wt, "loose.txt").write_text("x\n")
    sh(wt, "add", "loose.txt")                                       # a staged file moves the index
    other = tmp_path / "other"
    sh(root, "worktree", "add", "-q", "-b", "other-branch", str(other))
    d = gitview.snapshot(str(root), days=7)
    trees = {Path(w["path"]).name: w for w in d["worktrees"]}
    assert (trees["wt"]["ahead"], trees["wt"]["uncommitted"]) == (2, 1)
    assert trees["other"]["branch"] == "other-branch"
    assert "More worktree work" in {c["subject"] for c in d["graph"]["commits"]}
    assert any(e["message"].startswith("commit: More worktree work") for e in d["activity"]["items"])
    sh(root, "worktree", "remove", "--force", str(other))
    d = gitview.snapshot(str(root), days=7)
    assert "other" not in {Path(w["path"]).name for w in d["worktrees"]}


def test_hub_vcs_serves_the_last_snapshot_and_refreshes_in_the_background(project, monkeypatch):
    import threading
    from alpaca import serve
    live = serve.Live(project)
    gate, built = threading.Event(), []

    def snap(root, days=7, now=None, opened=()):
        built.append(days)
        if len(built) > 1 and days == 7:
            gate.wait(5)
        return {"generated": time.time(), "n": len(built)}

    monkeypatch.setattr(gitview, "snapshot", snap)
    monkeypatch.setattr(serve, "VCS_S", 0.05)
    first = live.vcs(7)
    assert first["n"] == 1 and first["snapshot_at"]
    time.sleep(0.1)
    started = time.time()
    again = live.vcs(7)                                   # stale: answered at once, a rebuild starts
    assert again["n"] == 1 and again["refreshing"] and time.time() - started < 1
    assert live.vcs(7)["n"] == 1 and len(built) == 2     # one refresh at a time
    assert live.vcs(30)["n"] == 3                         # another window is not blocked by the running refresh
    gate.set()
    for _ in range(100):
        if live.vcs(7)["n"] >= 2:
            break
        time.sleep(0.05)
    assert live.vcs(7)["n"] >= 2


def test_a_fresh_live_warms_days_7_without_a_request_and_a_request_shares_that_build(project, monkeypatch):
    import threading
    from alpaca import serve
    live = serve.Live(project)
    gate, built = threading.Event(), []

    def snap(root, days=7, now=None, opened=()):
        built.append(days)
        gate.wait(5)
        return {"generated": time.time(), "n": len(built)}

    monkeypatch.setattr(gitview, "snapshot", snap)
    monkeypatch.setattr(serve, "VCS_WARM_S", 0)
    live.data_ready.set()
    warm = live.warm_vcs()
    for _ in range(100):
        if built:
            break
        time.sleep(0.02)
    assert built == [7]                                   # the warm started a build with no request
    got = []
    req = threading.Thread(target=lambda: got.append(live.vcs(7)))
    req.start()                                           # arrives mid-build: waits, starts no second build
    time.sleep(0.2)
    assert req.is_alive() and built == [7]
    gate.set()
    req.join(5)
    warm.join(5)
    assert got[0]["n"] == 1 and built == [7]
    assert live.vcs(30)["n"] == 2 and built == [7, 30]    # other windows are not warmed


def test_cached_values_are_never_grown_by_a_caller_and_activity_follows_a_removed_worktree(repo, tmp_path):
    root, wt = repo
    other = tmp_path / "other"
    sh(root, "worktree", "add", "-q", "-b", "other-branch", str(other))
    gitview.reset_caches()
    key = (str(root), "refs/heads/main", "main")
    sizes = []
    for _ in range(4):
        a = gitview.activity(str(root), gitview.worktrees(str(root)))
        sizes.append((len(gitview._REFLOG[key][1]), len(a["items"])))
        a["items"].append({"junk": 1})                       # a caller that mutates what it got
    assert len(set(sizes)) == 1, sizes                       # the cached reflog did not grow
    warm_status = gitview._tree_status(next(w for w in gitview.worktrees(str(root)) if w["path"] == str(wt)))
    warm_status[0].append(("junk", "x"))
    assert ("junk", "x") not in gitview._tree_status(next(w for w in gitview.worktrees(str(root)) if w["path"] == str(wt)))[0]
    sh(root, "worktree", "remove", "--force", str(other))
    trees = gitview.worktrees(str(root))
    cached = gitview.activity(str(root), trees)
    gitview.reset_caches()
    fresh = gitview.activity(str(root), trees)
    assert cached == fresh and not [e for e in cached["items"] if "(other)" in e["where"]]


def test_ahead_behind_is_keyed_and_computed_on_the_same_commit(repo, monkeypatch):
    root, _ = repo
    gitview.reset_caches()
    calls = _count_git(monkeypatch)
    sha = sh(root, "rev-parse", "main").strip()
    head = sh(root, "rev-parse", "open").strip()
    gitview.ahead_behind(str(root), "main", head, sha)
    spec = [c[1][-1] for c in calls if c[1][0] == "rev-list"][0]
    assert spec == "%s...%s" % (sha, head)


# ---- the status sweeper (t-592) ---------------------------------------------------------------

def _wait(check, seconds=8.0):
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


def _bare_trees(tmp_path, n):
    out = []
    for i in range(n):
        d = tmp_path / ("t%d" % i)
        d.mkdir()
        os.utime(d, (1e9, 1e9))                            # untouched for years: a first look does not find it hot
        out.append({"path": str(d), "head": None, "branch": None})
    return out


def test_an_in_place_edit_that_leaves_gits_files_alone_reaches_a_hot_worktree_within_a_tick(repo):
    root, wt = repo
    gitview.reset_caches()
    tree = next(w for w in gitview.worktrees(str(root)) if w["path"] == str(wt))
    assert gitview._tree_read(tree)[0] == []
    sig = gitview._signature(tree)
    with open(wt / "w.txt", "a") as f:                     # an in-place write: no rename, no new file, no git command
        f.write("more\n")
    assert gitview._signature(tree) == sig                 # the old cache key is blind to it
    assert gitview._tree_read(tree)[0] == []               # and so the cached count lags
    sw = gitview.Sweeper(str(root), tick=0.2)
    sw.touch([str(wt)])
    try:
        assert _wait(lambda: (gitview.status_rows(str(root))[str(wt)] or {}).get("uncommitted") == 1, 3)
        with open(wt / "w.txt", "a") as f:
            f.write("and more\n")
        Path(wt, "new.txt").write_text("x\n")              # a new untracked file
        assert _wait(lambda: gitview.status_rows(str(root))[str(wt)]["uncommitted"] == 2, 3)
        os.remove(wt / "w.txt")                            # a deletion
        assert _wait(lambda: gitview.status_rows(str(root))[str(wt)]["uncommitted"] == 2 and
                     ("D" in "".join(c for c, _ in gitview._recall(gitview._STATUS, str(wt))[2])
                      or ".D" in "".join(c for c, _ in gitview._recall(gitview._STATUS, str(wt))[2])), 3)
    finally:
        sw.close()


def test_a_cold_worktree_is_reached_within_one_turnover(repo):
    root, wt = repo
    gitview.reset_caches()
    for w in gitview.worktrees(str(root)):
        gitview._tree_read(w)
    with open(wt / "w.txt", "a") as f:
        f.write("more\n")
    sw = gitview.Sweeper(str(root), tick=0.2, cold=0.6)    # two trees: one read per tick, a turnover of about 0.4 s
    t0 = time.time()
    for _ in range(4):
        sw.sweep()
        if gitview.status_rows(str(root))[str(wt)]["uncommitted"] == 1:
            break
    assert gitview.status_rows(str(root))[str(wt)]["uncommitted"] == 1
    assert sw.ticks <= 3 and time.time() - t0 < 5


def test_budget_picks_hot_first_capped_and_a_cold_slice(tmp_path):
    gitview.reset_caches()
    trees = _bare_trees(tmp_path, 40)
    sw = gitview.Sweeper(str(tmp_path), tick=5, cold=60, hot_max=8)
    sw.live = {t["path"] for t in trees[:12]}              # 12 live sessions, the cap is 8
    for i, t in enumerate(trees):                          # the lower the index, the longer since it was read
        gitview._STATUS[t["path"]] = (None, 0, [], None, 1000.0 + i)
    hot, cold = sw.pick(trees)
    assert [w["path"] for w in hot] == [t["path"] for t in trees[:8]]
    assert len(cold) == 3                                  # 28 cold * 5 s / 60 s, rounded up
    assert [w["path"] for w in cold] == [t["path"] for t in trees[12:15]]


def test_a_moved_top_directory_or_head_makes_a_worktree_hot_for_a_while(tmp_path):
    gitview.reset_caches()
    trees = _bare_trees(tmp_path, 3)
    sw = gitview.Sweeper(str(tmp_path), tick=5, cold=60)
    hot, cold = sw.pick(trees)
    assert hot == [] and len(cold) == 1                    # the first look is not a change
    Path(trees[1]["path"], "new.txt").write_text("x")      # the top directory's mtime moves
    hot, _ = sw.pick(trees)
    assert [w["path"] for w in hot] == [trees[1]["path"]]
    sw.seen[trees[1]["path"]][1] -= gitview.SWEEP_HOT_S + 1
    hot, _ = sw.pick(trees)
    assert hot == []                                       # hot lasts SWEEP_HOT_S, then it is cold again


def test_one_sweeper_thread_never_overlaps_a_slow_sweep_and_stops_when_unwatched(tmp_path, monkeypatch):
    sw = gitview.Sweeper(str(tmp_path), tick=0.05, idle=0.6)
    running, peak, runs = [0], [0], [0]

    def slow(**kw):
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        runs[0] += 1
        time.sleep(0.2)                                    # longer than a tick
        running[0] -= 1

    monkeypatch.setattr(sw, "sweep", slow)
    for _ in range(5):
        sw.touch()                                         # many requests, one thread
    first = sw.thread
    assert first is not None
    time.sleep(0.4)
    sw.touch()
    assert sw.thread is first
    assert peak[0] == 1 and runs[0] >= 2
    assert _wait(lambda: sw.thread is None, 3)             # nobody asked for idle_s: it ends by itself
    seen = runs[0]
    time.sleep(0.3)
    assert runs[0] == seen                                 # and does no more work
    sw.touch()                                             # a new request starts it again
    assert _wait(lambda: runs[0] > seen, 2)
    sw.close()
    assert sw.thread is None


def test_live_runs_no_sweeper_until_a_status_request_arrives(repo):
    from alpaca import serve
    root, wt = repo
    live = serve.Live(str(root))
    try:
        assert live._vcs_sweep.thread is None              # a hub nobody watches pays nothing
        live.vcs_touch()
        assert live._vcs_sweep.thread is not None
    finally:
        live._vcs_sweep.close()


def _old(path):
    os.utime(path, (1e9, 1e9))


def test_a_worktree_changed_just_before_the_first_look_starts_hot(tmp_path):
    gitview.reset_caches()
    trees = _bare_trees(tmp_path, 4)
    os.utime(trees[2]["path"], None)                       # its top directory changed a moment ago
    sw = gitview.Sweeper(str(tmp_path))
    hot, cold = sw.pick(trees)
    assert [w["path"] for w in hot] == [trees[2]["path"]] and len(cold) >= 1
    # a read by anyone (a snapshot build) that finds the files different makes it hot for the sweeper too
    gitview._remember(gitview._MOVED, trees[0]["path"], time.time())
    hot, _ = gitview.Sweeper(str(tmp_path)).pick(trees)
    assert trees[0]["path"] in [w["path"] for w in hot]


def test_a_snapshot_read_that_finds_new_files_marks_the_worktree_moved(repo):
    root, wt = repo
    gitview.reset_caches()
    tree = next(w for w in gitview.worktrees(str(root)) if w["path"] == str(wt))
    gitview._read_tree(tree)
    assert gitview._recall(gitview._MOVED, str(wt)) is None      # the first read has nothing to differ from
    Path(wt, "fresh.txt").write_text("x\n")
    gitview._read_tree(tree)
    assert gitview._recall(gitview._MOVED, str(wt)) is not None


def _timing_out(monkeypatch, bad):
    real = gitview.git

    def fake(cwd, *args, check=True):
        if str(cwd) == str(bad) and "status" in args:
            raise gitview.GitError("Command '['git', ...]' timed out after 10 seconds")
        return real(cwd, *args, check=check)

    monkeypatch.setattr(gitview, "git", fake)


def test_one_worktree_whose_status_times_out_does_not_stop_the_others(repo, tmp_path, monkeypatch, capsys):
    root, wt = repo
    other = tmp_path / "other"
    sh(root, "worktree", "add", "-q", "-b", "other-branch", str(other))
    gitview.reset_caches()
    for w in gitview.worktrees(str(root)):
        gitview._tree_read(w)
    _timing_out(monkeypatch, wt)
    Path(other, "n.txt").write_text("x\n")
    now = [1000.0]
    sw = gitview.Sweeper(str(root), tick=8, cold=16, clock=lambda: now[0])
    sw.live = {str(wt), str(other)}
    for _ in range(6):
        sw.sweep()                                         # no exception reaches the caller
    rows = gitview.status_rows(str(root))
    assert rows[str(other)]["uncommitted"] == 1 and rows[str(other)]["status_error"] is None
    assert rows[str(wt)]["status_error"] == "GitError" and rows[str(wt)]["checked_at"] is not None   # the last good read stays visible
    assert capsys.readouterr().err.count("could not be read") == 1      # one line for the tree, not one per tick
    assert sw.fail[str(wt)] == [1, 1000.0 + gitview.SWEEP_RETRY_S, 1000.0]     # backed off: not retried on each of six ticks
    now[0] += gitview.SWEEP_RETRY_S + 1                    # past the wait: retried, and the next wait is twice as long
    sw.sweep()
    assert sw.fail[str(wt)] == [2, now[0] + 2 * gitview.SWEEP_RETRY_S, now[0]]
    view = gitview.worktree_view(str(root))                # a snapshot build survives it too and says so
    assert next(v for v in view if v["path"] == str(wt))["status_error"] == "GitError"
    monkeypatch.undo()                                     # it recovers: the error is gone after one good read
    sw.fail.clear()
    sw.sweep()
    assert gitview.status_rows(str(root))[str(wt)]["status_error"] is None


def test_cold_slice_is_sized_by_what_each_tree_costs(tmp_path):
    gitview.reset_caches()
    trees = _bare_trees(tmp_path, 20)
    sw = gitview.Sweeper(str(tmp_path), tick=8, cold=70)
    for t in trees[:5]:
        sw.cost[t["path"]] = 0.2                           # four times the others
    for t in trees[5:]:
        sw.cost[t["path"]] = 0.05
    _, cold = sw.pick(trees)
    spent = sum(sw.cost[w["path"]] for w in cold)
    budget = (5 * 0.2 + 15 * 0.05) * 8 / 70
    assert budget <= spent < budget + 0.2                  # a fixed share of the time all of them need
    assert len(cold) < 20 * 8 / 70 + 2
    assert sw.backlog == 0
    sw.live = {t["path"] for t in trees}
    sw.hot_max = 8
    sw.pick(trees)
    assert sw.backlog == 12                                # said, not hidden


def test_a_cold_tree_whose_read_failed_once_is_read_again_within_a_turnover_plus_its_wait(tmp_path, monkeypatch):
    gitview.reset_caches()
    trees = _bare_trees(tmp_path, 30)
    monkeypatch.setattr(gitview, "worktrees", lambda root: [dict(t) for t in trees])
    monkeypatch.setattr(gitview, "_signature", lambda w: ("sig",))
    bad, reads, failed = trees[7]["path"], {}, []

    def fake_status(path, cheap=False):
        if path == bad and not failed:
            failed.append(1)
            raise gitview.GitError("timed out after 10 seconds")
        reads.setdefault(path, []).append(now[0])
        return []

    monkeypatch.setattr(gitview, "status", fake_status)
    now = [10_000_000.0]
    monkeypatch.setattr(gitview.time, "time", lambda: now[0])
    # all 30 in the watched set (t-608): the cold-lane turnover is what is measured here, not the ranking lane
    sw = gitview.Sweeper(str(tmp_path), tick=8, cold=70, clock=lambda: now[0], wall=lambda: now[0], recent=len(trees))
    for i, t in enumerate(trees):                          # as after a page open: every row read once, a little apart
        gitview._remember(gitview._STATUS, t["path"], (("sig",), 0, [], None, now[0] - 300 + i))
    failed_at = None
    for _ in range(80):                                    # 640 s of ticks
        sw.sweep()
        if failed and failed_at is None:
            failed_at = now[0]
        now[0] += 8
    assert failed_at is not None and bad in reads
    assert reads[bad][0] - failed_at <= 70 + gitview.SWEEP_RETRY_S + 8
    assert bad not in sw.fail and gitview.status_rows(str(tmp_path))[bad]["status_error"] is None


def test_a_never_read_failing_tree_is_not_counted_as_clean(repo, monkeypatch):
    root, wt = repo
    gitview.reset_caches()
    _timing_out(monkeypatch, wt)
    view = gitview.worktree_view(str(root))
    row = next(v for v in view if v["path"] == str(wt))
    assert row["uncommitted"] is None and row["status_error"] == "GitError" and row["checked_at"] is None


def test_failing_trees_have_their_own_lane_one_retry_per_tick_with_a_short_timeout(tmp_path, monkeypatch):
    gitview.reset_caches()
    trees = _bare_trees(tmp_path, 40)
    monkeypatch.setattr(gitview, "worktrees", lambda root: [dict(t) for t in trees])
    monkeypatch.setattr(gitview, "_signature", lambda w: ("sig",))
    bad = {t["path"] for t in trees[:5]}
    calls = []

    def fake_status(path, cheap=False):
        calls.append((path, getattr(gitview._LIMIT, "timeout", None)))
        if path in bad:
            raise gitview.GitError("timed out after 3 seconds")
        return []

    monkeypatch.setattr(gitview, "status", fake_status)
    now = [10_000_000.0]
    sw = gitview.Sweeper(str(tmp_path), tick=8, cold=70, clock=lambda: now[0], wall=lambda: now[0])
    for i, p in enumerate(sorted(bad)):                    # all five failed, at different times, and are due now
        sw.fail[p] = [1, now[0] - 1, now[0] - 100 + i]
    _, cold = sw.pick(trees)
    assert not bad & {w["path"] for w in cold}             # a failing tree never takes a cold slot
    order = []
    for _ in range(5):
        calls.clear()
        sw.sweep()
        tried = [(p, lim) for p, lim in calls if p in bad]
        assert len(tried) == 1 and tried[0][1] == gitview.SWEEP_RETRY_TIMEOUT_S      # one per tick, short timeout
        assert calls[-1] == tried[0]                       # after the healthy reads
        assert all(lim == gitview.SWEEP_READ_TIMEOUT_S for p, lim in calls if p not in bad)
        order.append(tried[0][0])
        now[0] += 1
    assert order == sorted(bad)                            # the longest since tried first
    assert gitview._LIMIT.timeout is None


# ---- the watched set: main, the most recent, the largest, the opened (t-608) -----------------------------

def _t(path, active=None, uncommitted=None, is_main=False, live=False):
    return {"path": path, "active": active, "uncommitted": uncommitted, "is_main": is_main, "live": live}


def test_watched_takes_main_then_the_most_recent_then_the_largest_without_repeats():
    trees = [_t("/m", active=999, uncommitted=900, is_main=True)]
    trees += [_t("/r%d" % i, active=500 + i, uncommitted=i) for i in range(8)]     # r7..r3 are the most recent
    trees[-1]["uncommitted"] = 1000                                                # r7 is also the largest of all
    trees += [_t("/b%d" % i, active=10 + i, uncommitted=100 + i) for i in range(4)]
    trees += [_t("/never", active=1), _t("/old-live", active=2, live=True)]
    got = gitview.watched(trees, recent=5, largest=5)
    assert got["/m"] == "main"                                  # main is its own part, never a recent or a largest slot
    assert [p for p, why in got.items() if why == "recent"] == ["/old-live", "/r7", "/r6", "/r5", "/r4"]
    # r7 is not counted twice: the largest fill from the next largest, down to r3 (3 files)
    assert [p for p, why in got.items() if why == "largest"] == ["/b3", "/b2", "/b1", "/b0", "/r3"]
    assert "/never" not in got and len(got) == 11
    assert gitview.watched(trees) == got                        # WATCH_RECENT and WATCH_LARGEST are 5 and 5
    assert (gitview.WATCH_RECENT, gitview.WATCH_LARGEST) == (5, 5)


def test_watched_with_fewer_trees_than_slots_and_the_opened_ones():
    few = [_t("/m", is_main=True), _t("/a", active=5), _t("/b", uncommitted=3), _t("/c")]
    assert gitview.watched(few) == {"/m": "main", "/a": "recent", "/b": "recent", "/c": "recent"}
    assert gitview.watched([_t("/first"), _t("/second", active=9)]) == {"/first": "main", "/second": "recent"}
    assert gitview.watched([]) == {}
    many = [_t("/m", is_main=True)] + [_t("/x%02d" % i, active=100 - i, uncommitted=0) for i in range(20)]
    got = gitview.watched(many, opened=["/x19", "/nowhere", "/m", "/x00"])
    assert got["/x19"] == "opened" and "/nowhere" not in got  # only a listed worktree can be opened
    assert got["/m"] == "main" and got["/x00"] == "recent"     # an opened tree already in keeps its first reason
    assert len(got) == 12


def test_the_sweeper_reads_only_the_watched_set_and_one_ranking_tree_per_tick(tmp_path, monkeypatch):
    gitview.reset_caches()
    trees = _bare_trees(tmp_path, 30)
    monkeypatch.setattr(gitview, "worktrees", lambda root: [dict(t) for t in trees])
    monkeypatch.setattr(gitview, "_signature", lambda w: ("sig",))
    reads = []

    def fake_status(path, cheap=False):
        reads.append(path)
        return [("??", "f%d" % k) for k in range(int(os.path.basename(path)[1:]) % 7)]

    monkeypatch.setattr(gitview, "status", fake_status)
    now = [10_000_000.0]
    monkeypatch.setattr(gitview.time, "time", lambda: now[0])
    # cold=tick: every watched tree is read every tick, so anything else read is the ranking lane's
    sw = gitview.Sweeper(str(tmp_path), tick=8, cold=8, clock=lambda: now[0], wall=lambda: now[0])
    ranked = {}
    for _ in range(40):
        reads.clear()
        sw.sweep()
        assert len(sw.watch) == 11 and list(sw.watch.values()).count("main") == 1
        outside = [p for p in reads if p not in sw.watch]
        assert len(outside) <= 1 and outside == ([sw.ranked["path"]] if sw.ranked else [])
        for p in outside:
            assert p not in ranked or now[0] - ranked[p] >= gitview.SWEEP_RANK_S   # not again within SWEEP_RANK_S
            ranked[p] = now[0]
        now[0] += 8
    assert all(r and r["uncommitted"] is not None for r in gitview.status_rows(str(tmp_path)).values())  # all known
    assert sw.ranked is None                                    # and none is read again before SWEEP_RANK_S
    largest = sorted((p for p, why in sw.watch.items() if why == "largest"))
    counts = gitview.status_rows(str(tmp_path))
    unwatched = [counts[p]["uncommitted"] for p in counts if p not in sw.watch]
    assert min(counts[p]["uncommitted"] for p in largest) >= max(unwatched)  # the largest are the largest known
    now[0] += gitview.SWEEP_RANK_S
    reads.clear()
    sw.sweep()
    assert len([p for p in reads if p not in sw.watch]) == 1   # the ranking lane turns over again, one per tick


def test_worktree_view_never_reads_the_status_of_an_unwatched_worktree(repo, tmp_path, monkeypatch):
    root, wt = repo
    extra = []
    for i in range(3):
        extra.append(tmp_path / ("more%d" % i))
        sh(root, "worktree", "add", "-q", "-b", "more-%d" % i, str(extra[-1]))
    Path(extra[2], "dirty.txt").write_text("x\n")
    gitview.reset_caches()
    monkeypatch.setattr(gitview, "WATCH_RECENT", 1)
    monkeypatch.setattr(gitview, "WATCH_LARGEST", 1)
    real, called = gitview.status, []

    def counting(path, cheap=False):
        called.append(path)
        return real(path, cheap)

    monkeypatch.setattr(gitview, "status", counting)
    view = gitview.worktree_view(str(root))
    rows = {w["path"]: w for w in view}
    watched = {p for p, w in rows.items() if w["watch"]}
    assert len(rows) == 5 and len(watched) == 3 and rows[str(root)]["watch"] == "main"
    assert set(called) == watched                               # git status ran on the watched trees only
    for p, w in rows.items():
        assert w["head_short"] and w["ahead"] is not None       # HEAD and ahead/behind stay on every row
        if p not in watched:
            assert w["uncommitted"] is None and w["loaded"] is False and w["checked_at"] is None
    cold = next(p for p in rows if p not in watched)
    gitview._read_tree(next(w for w in gitview.worktrees(str(root)) if w["path"] == cold))   # a ranking read
    called.clear()
    rows = {w["path"]: w for w in gitview.worktree_view(str(root))}
    assert rows[cold]["loaded"] is True and rows[cold]["uncommitted"] is not None    # the cached count is shown
    assert cold not in called                                   # and reused: its git files did not move
    assert not set(called) - {p for p, w in rows.items() if w["watch"]}
    opened = next(p for p, w in rows.items() if w["watch"] is None)
    rows = {w["path"]: w for w in gitview.worktree_view(str(root), opened=[opened])}
    assert rows[opened]["watch"] == "opened" and rows[opened]["loaded"] is True
    over = gitview.snapshot(str(root))["overall"]
    assert over["worktrees"] == 5 and over["main"] == str(root) and over["loaded"] + over["not_loaded"] == 5


def test_vcs_tree_endpoint_reads_only_an_exact_worktree_path_behind_the_login(repo, monkeypatch):
    import urllib.error
    import urllib.request
    from alpaca import serve
    from alpaca.tests.test_hub_live_refresh import _start
    root, wt = repo
    gitview.reset_caches()
    real, called = gitview.status, []

    def counting(path, cheap=False):
        called.append(path)
        return real(path, cheap)

    monkeypatch.setattr(gitview, "status", counting)
    live = serve.Live(str(root))
    monkeypatch.setattr(live._vcs_sweep, "touch", lambda live=None: None)   # no sweeper thread: count the request's reads
    httpd, base = _start(live, str(root))

    def get(path, headers=None):
        from urllib.parse import quote
        req = urllib.request.Request(base + "/hub/vcs-tree.json?path=" + quote(path, safe=""), headers=headers or {})
        try:
            r = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as exc:
            r = exc
        return r.status, json.loads(r.read() or b"{}")

    try:
        for bad in ("", "/", str(root / "a.txt"), str(wt) + "/", str(wt) + "/../wt", str(root / ".git"), "/etc"):
            code, body = get(bad)
            assert code == 404 and body["error"] == "Not a worktree of this repository", bad
        assert called == [] and live._vcs_sweep.opened_paths() == []   # nothing read, nothing opened
        code, row = get(str(wt))
        assert code == 200 and row["path"] == str(wt) and row["watch"] == "opened" and row["uncommitted"] == 0
        assert row["checked_at"] and row["branch"] == "wt-branch" and row["ahead"] == 1
        assert called == [str(wt)] and live._vcs_sweep.opened_paths() == [str(wt)]
        status = json.loads(urllib.request.urlopen(base + "/hub/vcs-status.json", timeout=30).read())
        assert str(wt) in status["worktrees"] and status["overall"]["worktrees"] == 2
        monkeypatch.setenv("ALPACA_FILES_AUTH", "user:code")    # a configured login guards it like every /hub/ route
        called.clear()
        code, body = get(str(wt))
        assert code == 401 and called == []
    finally:
        httpd.shutdown()
        httpd.server_close()
        live._vcs_sweep.close()
