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
