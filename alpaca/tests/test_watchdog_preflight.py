"""Watchdog preflight, undo point, rollback and post-check (op-021 t-213), in throwaway repositories."""
import os
import subprocess
import time

import pytest

from alpaca.watchdog import postcheck, preflight, runs, undo


def git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args], check=check, capture_output=True, text=True).stdout


@pytest.fixture
def repo(project, tmp_path):
    git(project, "init", "-q", "-b", "main")
    for k, v in [("user.name", "T"), ("user.email", "t@localhost"), ("commit.gpgsign", "false")]:
        git(project, "config", k, v)
    (tmp_path / "proj" / ".gitignore").write_text(".alpaca/\n", encoding="utf-8")
    (tmp_path / "proj" / "f.txt").write_text("base\n", encoding="utf-8")
    git(project, "add", ".gitignore", "ALPACA-MANIFEST", "f.txt")
    git(project, "commit", "-qm", "init")
    return project


def _wt(repo, tmp_path, name, branch=True, commit=False):
    path = str(tmp_path / name)
    if branch:
        git(repo, "worktree", "add", "-q", "-b", name, path)
    else:
        git(repo, "worktree", "add", "-q", "--detach", path)
    if commit:
        with open(os.path.join(path, "new.txt"), "w") as fh:
            fh.write("work\n")
        git(path, "add", "new.txt")
        git(path, "commit", "-qm", "unmerged work")
    return path


def _pf(repo, *cmd, sessions=(), **kw):
    return preflight.run(repo, ["git", "-C", repo, *cmd], sessions=list(sessions), claims=kw.pop("claims", {}), **kw)


def _levels(res):
    return [r["level"] for r in res["reasons"]]


def test_safe_merged_clean_linked_worktree(repo, tmp_path):
    wt = _wt(repo, tmp_path, "done")
    res = _pf(repo, "worktree", "remove", wt)
    assert res["verdict"] == "safe", res["reasons"]
    text = open(res["report"], encoding="utf-8").read()
    for section in ("## Command", "## Verdict", "## Targets", "## Sessions", "## Dry run", "alpaca watchdog undo " + res["id"]):
        assert section in text
    assert runs.load(repo, res["id"], "preflight.json")["refs"]["main"]


def test_risky_unmerged_commits_on_a_detached_worktree(repo, tmp_path):
    wt = _wt(repo, tmp_path, "scratch", branch=False, commit=True)
    res = _pf(repo, "worktree", "remove", "--force", wt)
    assert res["verdict"] == "risky"
    assert any("unreachable" in r["text"] and r["pointer"] for r in res["reasons"])


def test_risky_unknown_command(repo):
    assert _pf(repo, "gc")["verdict"] == "risky"


def test_stop_live_session_with_uncommitted_work(repo, tmp_path):
    wt = _wt(repo, tmp_path, "busy")
    with open(os.path.join(wt, "wip.txt"), "w") as fh:
        fh.write("x\n")
    live = {"sid": "s-live-1", "cwd": wt, "last_beat": time.time(), "ended": None}
    res = _pf(repo, "worktree", "remove", "--force", wt, sessions=[live], claims={"s-live-1": ["t-999"]})
    assert res["verdict"] == "stop"
    assert any("s-live-1" in r["text"] and "t-999" in r["text"] for r in res["reasons"])
    ended = dict(live, ended="2026-01-01T00:00:00Z", last_beat=time.time() - 5 * 86400)
    assert _pf(repo, "worktree", "remove", "--force", wt, sessions=[ended])["verdict"] == "risky"


def test_stop_stash_in_the_main_tree(repo):
    assert _pf(repo, "stash")["verdict"] == "stop"


def test_stop_force_push(repo):
    assert _pf(repo, "push", "--force", "origin", "main")["verdict"] == "stop"


def _conflict(repo):
    git(repo, "checkout", "-q", "-b", "other")
    open(os.path.join(repo, "f.txt"), "w").write("other\n")
    git(repo, "commit", "-qam", "other")
    git(repo, "checkout", "-q", "main")
    open(os.path.join(repo, "f.txt"), "w").write("mine\n")
    git(repo, "commit", "-qam", "mine")


def test_stop_merge_conflict_and_no_scratch_left(repo):
    _conflict(repo)
    before = git(repo, "status", "--porcelain")
    res = _pf(repo, "merge", "other")
    assert res["verdict"] == "stop"
    assert any("conflicts" in r["text"] for r in res["reasons"])
    scratch = os.path.join(repo, runs.SCRATCH)
    assert not os.path.isdir(scratch) or os.listdir(scratch) == []
    assert "scratch" not in git(repo, "worktree", "list")
    assert git(repo, "status", "--porcelain") == before
    assert open(os.path.join(repo, "f.txt")).read() == "mine\n"


def test_clean_merge_dry_run_is_not_a_conflict(repo):
    git(repo, "branch", "side")
    res = _pf(repo, "merge", "side")
    assert "stop" not in _levels(res)


def _dirty_worktree(repo, tmp_path):
    wt = _wt(repo, tmp_path, "keep", commit=True)
    with open(os.path.join(wt, "f.txt"), "w") as fh:
        fh.write("modified\n")
    with open(os.path.join(wt, "loose.txt"), "w") as fh:
        fh.write("untracked\n")
    return wt


def test_undo_leaves_the_target_untouched(repo, tmp_path):
    wt = _dirty_worktree(repo, tmp_path)
    res = _pf(repo, "worktree", "remove", "--force", wt)
    before, index_before = git(wt, "status", "--porcelain"), open(os.path.join(repo, ".git", "worktrees", "keep", "index"), "rb").read()
    out = undo.create(repo, res["id"])
    assert git(wt, "status", "--porcelain") == before
    assert open(os.path.join(repo, ".git", "worktrees", "keep", "index"), "rb").read() == index_before
    kinds = {r["kind"] for r in out["refs"]}
    assert kinds == {"heads", "wip"}
    for r in out["refs"]:
        assert git(repo, "rev-parse", r["ref"]).strip() == r["sha"]
    assert len(out["snapshot"]["sha256"]) == 64 and os.path.isfile(out["snapshot"]["path"])
    assert out["rollback"] == "alpaca watchdog rollback " + res["id"]


def test_rollback_restores_worktree_branch_and_files(repo, tmp_path):
    wt = _dirty_worktree(repo, tmp_path)
    head = git(wt, "rev-parse", "HEAD").strip()
    res = _pf(repo, "worktree", "remove", "--force", wt)
    undo.create(repo, res["id"])
    git(repo, "worktree", "remove", "--force", wt)
    git(repo, "branch", "-D", "keep")
    assert not os.path.exists(wt)
    plan = undo.rollback(repo, res["id"], dry_run=True)
    assert [s["action"] for s in plan["steps"]][:3] == ["recreate-branch", "recreate-worktree", "restore-wip"]
    assert not os.path.exists(wt)
    done = undo.rollback(repo, res["id"])
    assert done["ok"], done["steps"]
    assert git(repo, "rev-parse", "keep").strip() == head
    assert open(os.path.join(wt, "f.txt")).read() == "modified\n"
    assert open(os.path.join(wt, "loose.txt")).read() == "untracked\n"
    status = git(wt, "status", "--porcelain").splitlines()
    assert " M f.txt" in status and "?? loose.txt" in status
    assert any(s["action"] == "manual-record" for s in done["steps"])


def test_rollback_never_moves_an_existing_branch(repo, tmp_path):
    wt = _wt(repo, tmp_path, "mover", commit=True)
    res = _pf(repo, "branch", "-D", "mover")
    undo.create(repo, res["id"])
    git(repo, "worktree", "remove", "--force", wt)
    git(repo, "branch", "-D", "mover")
    git(repo, "branch", "mover", "main")
    elsewhere = git(repo, "rev-parse", "mover").strip()
    out = undo.rollback(repo, res["id"])
    assert not out["ok"]
    assert git(repo, "rev-parse", "mover").strip() == elsewhere
    assert any(s["action"] == "refuse-branch" for s in out["steps"])


def test_postcheck_ok_after_the_intended_removal(repo, tmp_path):
    wt = _wt(repo, tmp_path, "gone")
    res = _pf(repo, "worktree", "remove", wt, for_="s-asker")
    undo.create(repo, res["id"])
    git(repo, "worktree", "remove", wt)
    out = postcheck.run(repo, res["id"])
    assert out["result"] == "ok", out["mismatches"]
    assert os.path.isfile(out["report"])
    from alpaca import db
    conn = db.connect_readonly(repo)
    try:
        msg = conn.execute("SELECT sender, to_, kind FROM messages").fetchall()
        kinds = [r[0] for r in conn.execute("SELECT kind FROM events WHERE kind LIKE 'watchdog-%'")]
    finally:
        conn.close()
    assert [tuple(m) for m in msg] == [("watchdog", "s-asker", "result")]
    assert sorted(kinds) == ["watchdog-postcheck", "watchdog-preflight", "watchdog-undo"]


def test_postcheck_drift_when_another_branch_moved_and_no_addressee(repo, tmp_path):
    wt = _wt(repo, tmp_path, "gone2")
    git(repo, "branch", "bystander")
    res = _pf(repo, "worktree", "remove", wt)
    git(repo, "worktree", "remove", wt)
    git(repo, "checkout", "-q", "bystander")
    open(os.path.join(repo, "f.txt"), "w").write("moved\n")
    git(repo, "commit", "-qam", "bystander moves")
    out = postcheck.run(repo, res["id"])
    assert out["result"] == "drift"
    assert any("bystander" in m for m in out["mismatches"])
    assert "skipped" in out["message"]


def test_postcheck_drift_when_the_worktree_is_still_there(repo, tmp_path):
    wt = _wt(repo, tmp_path, "stays")
    res = _pf(repo, "worktree", "remove", wt)
    out = postcheck.run(repo, res["id"], to="s-x")
    assert out["result"] == "drift" and any("still listed" in m for m in out["mismatches"])


def test_snapshots_keep_the_newest_five(repo, tmp_path):
    wt = _wt(repo, tmp_path, "many")
    ids = []
    for i in range(7):
        res = _pf(repo, "worktree", "remove", wt)
        undo.create(repo, res["id"])
        ids.append(res["id"])
        time.sleep(0.02)
    left = [i for i in ids if os.path.isfile(os.path.join(repo, runs.UNDO, i, "alpaca.db"))]
    assert left == ids[2:]
    assert os.path.isfile(os.path.join(repo, runs.RUNS, ids[0], "undo.json"))


def _refs_and_indexes(repo):
    refs = git(repo, "for-each-ref")
    idx = {}
    for dirpath, _dirs, files in os.walk(os.path.join(repo, ".git")):
        if "index" in files and "scratch" not in dirpath:
            idx[dirpath] = open(os.path.join(dirpath, "index"), "rb").read()
    return refs, idx


def test_plain_push_is_a_stop_with_the_owners_reason(repo):
    res = _pf(repo, "push", "origin", "main")
    assert res["verdict"] == "stop"
    assert any("push is the owner's (brief 4)" in r["text"] for r in res["reasons"])


def test_preflight_writes_no_ref_and_no_index_outside_the_scratch(repo, tmp_path):
    _conflict(repo)
    wt = _dirty_worktree(repo, tmp_path)
    before = _refs_and_indexes(repo)
    _pf(repo, "merge", "other")
    _pf(repo, "worktree", "remove", "--force", wt)
    _pf(repo, "reset", "--hard", "HEAD~1")
    assert _refs_and_indexes(repo) == before
    assert git(repo, "worktree", "list").count("\n") == 2


def test_rollback_restores_a_tracked_but_ignored_file(repo, tmp_path):
    wt = _wt(repo, tmp_path, "ign")
    with open(os.path.join(repo, ".gitignore"), "a") as fh:
        fh.write("*.log\n")
    git(repo, "commit", "-qam", "ignore logs")
    git(wt, "merge", "-q", "main")
    with open(os.path.join(wt, "kept.log"), "w") as fh:
        fh.write("tracked\n")
    git(wt, "add", "-f", "kept.log")
    git(wt, "commit", "-qm", "track an ignored file")
    with open(os.path.join(wt, "kept.log"), "w") as fh:
        fh.write("tracked and edited\n")
    with open(os.path.join(wt, "loose.txt"), "w") as fh:
        fh.write("u\n")
    res = _pf(repo, "worktree", "remove", "--force", wt)
    undo.create(repo, res["id"])
    git(repo, "worktree", "remove", "--force", wt)
    git(repo, "branch", "-D", "ign")
    assert undo.rollback(repo, res["id"])["ok"]
    assert open(os.path.join(wt, "kept.log")).read() == "tracked and edited\n"
    assert os.path.exists(os.path.join(wt, "loose.txt"))
    assert "D kept.log" not in git(wt, "status", "--porcelain")


def test_undo_leaves_the_real_index_byte_identical_main_and_linked(repo, tmp_path):
    wt = _dirty_worktree(repo, tmp_path)
    git(wt, "add", "loose.txt")
    open(os.path.join(repo, "f.txt"), "w").write("main dirty\n")
    res = _pf(repo, "worktree", "remove", "--force", wt)
    before = _refs_and_indexes(repo)[1]
    undo.create(repo, res["id"])
    assert _refs_and_indexes(repo)[1] == before


def test_rollback_of_a_removed_worktree_leaves_a_moved_branch_alone(repo, tmp_path):
    wt = _wt(repo, tmp_path, "mv", commit=True)
    saved = git(wt, "rev-parse", "HEAD").strip()
    res = _pf(repo, "worktree", "remove", wt)
    undo.create(repo, res["id"])
    git(repo, "worktree", "remove", wt)
    git(repo, "branch", "-f", "mv", "main")
    moved = git(repo, "rev-parse", "mv").strip()
    out = undo.rollback(repo, res["id"])
    assert git(repo, "rev-parse", "mv").strip() == moved != saved
    assert git(wt, "rev-parse", "HEAD").strip() == saved
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"
    assert any(s["action"] == "refuse-branch" for s in out["steps"])


def test_clean_undo_restores_into_a_new_worktree(repo, tmp_path):
    wt = _dirty_worktree(repo, tmp_path)
    res = _pf(repo, "-C", wt, "clean", "-f")
    undo.create(repo, res["id"])
    os.unlink(os.path.join(wt, "loose.txt"))        # what clean -f did
    out = undo.rollback(repo, res["id"])
    dest = os.path.join(repo, ".alpaca", "watchdog", "restore", res["id"], "keep")
    assert out["ok"], out["steps"]
    assert open(os.path.join(dest, "loose.txt")).read() == "untracked\n"
    assert open(os.path.join(dest, "f.txt")).read() == "modified\n"
    assert not os.path.exists(os.path.join(wt, "loose.txt"))        # the original is left alone
    assert any(s["action"] == "owner-command" and "checkout --no-overlay" in s["detail"] for s in out["steps"])
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip() == "keep"


def test_unmerged_index_is_reported_as_not_captured(repo, tmp_path):
    wt = _wt(repo, tmp_path, "mid", commit=True)
    open(os.path.join(wt, "new.txt"), "w").write("mine\n")
    git(wt, "commit", "-qam", "mine")
    git(repo, "checkout", "-q", "-b", "theirs", "main")
    open(os.path.join(repo, "new.txt"), "w").write("theirs\n")
    git(repo, "add", "new.txt")
    git(repo, "commit", "-qm", "theirs")
    git(repo, "checkout", "-q", "main")
    git(wt, "merge", "theirs", check=False)
    assert "AA new.txt" in git(wt, "status", "--porcelain")
    res = _pf(repo, "worktree", "remove", "--force", wt)
    out = undo.create(repo, res["id"])
    assert [n["path"] for n in out["not_captured"]] == [wt]
    assert "unmerged" in out["not_captured"][0]["error"]
    assert not any(r["kind"] == "wip" for r in out["refs"])
    assert os.path.isfile(out["snapshot"]["path"])


def test_merge_dry_run_needs_no_identity_and_up_to_date_leaves_no_scratch(repo, monkeypatch):
    git(repo, "config", "--unset", "user.name")
    git(repo, "config", "--unset", "user.email")
    for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(repo))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    git(repo, "branch", "side")
    res = _pf(repo, "merge", "side")        # side == main: already up to date
    assert "stop" not in _levels(res) and not any("could not run" in r["text"] for r in res["reasons"]), res["reasons"]
    scratch = os.path.join(repo, runs.SCRATCH)
    assert not os.path.isdir(scratch) or os.listdir(scratch) == []
    assert "scratch" not in git(repo, "worktree", "list")


@pytest.mark.parametrize("form", ["option", "env"])
def test_git_dir_alone_pointing_at_main_from_a_linked_tree_is_stop(repo, tmp_path, form):
    """Adversarial round 2: with --git-dir alone (or GIT_DIR=) git keeps the linked tree's files but writes
    main's index, HEAD and branch, so `reset --hard HEAD~1` would move main back."""
    lw = _wt(repo, tmp_path, "lw")
    (tmp_path / "proj" / "f.txt").write_text("main moves on\n", encoding="utf-8")
    git(repo, "commit", "-qam", "C3 on main")
    main_git = os.path.join(repo, ".git")
    if form == "option":
        argv = ["git", "-C", lw, "--git-dir=" + main_git, "reset", "--hard", "HEAD~1"]
    else:
        argv = ["GIT_DIR=" + main_git, "git", "-C", lw, "reset", "--hard", "HEAD~1"]
    res = preflight.run(repo, argv, sessions=[], claims={})
    assert res["verdict"] == "stop", res["reasons"]
    assert any("own git dir" in r["text"] for r in res["reasons"])


def test_reset_hard_over_a_nested_worktree_at_a_tracked_path_is_not_safe(repo, tmp_path):
    """Adversarial round 2 follow-up F1: a worktree nested at a non-ignored path inside the target."""
    nested = os.path.join(repo, "sub")
    git(repo, "worktree", "add", "-q", "--detach", nested)
    with open(os.path.join(nested, "wip.txt"), "w") as fh:
        fh.write("live work\n")
    live = [{"sid": "s-nested", "cwd": nested, "last_beat": time.time(), "ended": None}]
    res = _pf(repo, "reset", "--hard", "HEAD", sessions=live)
    assert res["verdict"] in ("risky", "stop"), res["reasons"]
    assert any("does not ignore" in r["text"] for r in res["reasons"])
