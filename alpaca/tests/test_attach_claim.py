"""`task claim` in an attach studio cuts the work branch (AC-04) and the pre-mortem attacks of t-648 (build plan 6.2)."""
import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from alpaca import claims, cli, db
from alpaca.attach import AttachError, baseline, config, gitops, worktree
from alpaca.tests.attachkit import (add_tasks, attached, break_remote, counting_gates, git, make_studio,  # noqa: F401
                                    set_gates, studio, task_row, upstream, write_attach)


def claim(task, by="w1"):
    return cli.main(["task", "claim", task, "--by", by])


def events(root, kind):
    conn = db.connect(str(root))
    try:
        return [e for e in db.events(conn, limit=10 ** 9) if e["kind"] == kind]
    finally:
        conn.close()


def live(root, task):
    conn = db.connect(str(root))
    try:
        return claims.live(conn, task)
    finally:
        conn.close()


def reopen_row(att, task):
    """Reproduce the race: a second `task claim` read the row as open before the first one took the lease."""
    conn = db.connect(str(att.root))
    conn.execute("UPDATE tasks SET status='open' WHERE id=?", (task,))
    conn.close()


def branches(att):
    return git(att.app, "for-each-ref", "--format=%(refname:short)", "refs/heads/alpaca/").split()


def branch_file(att, task):
    return json.loads((att.root / ".alpaca/attach/branches" / ("%s.json" % task)).read_text())


def worktree_count(att):
    return len([line for line in git(att.app, "worktree", "list").splitlines() if line.strip()]) - 1


def wt_git(att, task, *args, **kw):
    """git in a task's work folder the way attach runs it: the repository named on the command line
    (`gitops.git` refuses a work folder, whose own .git file a worker can rewrite)."""
    folder = os.path.realpath(str(att.root / ".alpaca/attach/worktrees" / task))
    return gitops.in_worktree(str(att.app), task, folder, *args, **kw)


def app_state(att):
    return (git(att.app, "rev-parse", "HEAD"), git(att.app, "symbolic-ref", "HEAD"),
            git(att.app, "status", "--porcelain", "--ignored"), git(att.app, "config", "--local", "--list"))


# ---- AC-04 ------------------------------------------------------------------------------------------------------------

def test_claim_cuts_the_work_branch_from_the_freshly_fetched_base(attached, capsys):
    """AC-04: upstream advanced after attach; the claim fetches, cuts alpaca/<task> at the new tip, stores the baseline."""
    (task,) = add_tasks(attached.root, 1)
    new = attached.upstream.commit({"fresh.txt": "new work from someone else\n"}, "upstream moved on")
    assert git(attached.app, "rev-parse", "refs/remotes/origin/main") != new            # not fetched yet
    capsys.readouterr()
    assert claim(task) == cli.PASS
    out = capsys.readouterr().out
    assert "%s claimed by w1 until" % task in out and "alpaca attach: %s work branch alpaca/%s" % (task, task) in out
    wt = attached.root / ".alpaca/attach/worktrees" / task
    assert git(wt, "rev-parse", "HEAD") == new and git(wt, "symbolic-ref", "--short", "HEAD") == "alpaca/%s" % task
    assert (wt / "fresh.txt").is_file()
    assert git(attached.app, "rev-parse", "refs/remotes/origin/main") == new           # the fetch moved the tracking ref
    info = branch_file(attached, task)
    assert info["schema"] == 1 and info["task"] == task and info["branch"] == "alpaca/%s" % task
    assert info["worktree"] == os.path.realpath(str(wt)) and info["base_sha"] == new and info["worker"] == "w1"
    assert info["fetch_url"] == attached.upstream.url and info["cut_at"]
    base = baseline.load(str(attached.root), new)
    assert base is not None and info["baseline_key"] == base["key"]               # the baseline of the new base is stored
    (ev,) = events(attached.root, "attach-branch")
    assert ev["ref"] == task and ev["data"]["base_sha"] == new and ev["data"]["baseline_key"] == base["key"]
    assert config.task_checkout(str(attached.root), task) == Path(info["worktree"])


def test_the_claim_changes_nothing_in_the_app_checkout(attached):
    """The contract's fail case: app/ keeps its HEAD, branch, files and config; no checkout, reset or merge ran there."""
    (task,) = add_tasks(attached.root, 1)
    attached.upstream.commit({"fresh.txt": "x\n"}, "advance")
    before = app_state(attached)
    old_head = before[0]
    assert claim(task) == cli.PASS
    after = app_state(attached)
    assert after == before and after[0] == old_head
    assert git(attached.app, "symbolic-ref", "--short", "HEAD") == "main"
    assert not (attached.app / "fresh.txt").exists()


def test_a_claim_with_no_new_upstream_commit_cuts_at_the_attach_base(attached):
    (task,) = add_tasks(attached.root, 1)
    assert claim(task) == cli.PASS
    assert git(attached.root / ".alpaca/attach/worktrees" / task, "rev-parse", "HEAD") == attached.upstream.sha()
    assert len(list((attached.root / ".alpaca/attach/baseline").glob("*.json"))) == 1       # the baseline is reused


def test_two_tasks_get_two_worktrees_from_one_base(attached):
    t1, t2 = add_tasks(attached.root, 2)
    assert claim(t1) == cli.PASS and claim(t2, "w2") == cli.PASS
    assert worktree_count(attached) == 2
    assert branches(attached) == ["alpaca/%s" % t1, "alpaca/%s" % t2]


def test_the_branch_prefix_of_the_block_names_the_branch(attached):
    block = dict(config.raw(str(attached.root)))
    block["branch_prefix"] = "work/"
    write_attach(attached.root, **block)
    (task,) = add_tasks(attached.root, 1)
    assert claim(task) == cli.PASS
    assert git(attached.app, "rev-parse", "--verify", "refs/heads/work/%s" % task)


# ---- t-648.2: the claim race and a failed cut --------------------------------------------------------------------------

def test_a_losing_claim_neither_cuts_nor_releases(attached, capsys):
    t1, t2 = add_tasks(attached.root, 2)
    assert claim(t1, "w1") == cli.PASS
    reopen_row(attached, t1)
    capsys.readouterr()
    assert claim(t1, "w2") == cli.FAIL
    out = capsys.readouterr().out
    assert "GATE alpaca-task-claim: FAIL (w1 holds %s)" % t1 in out and "claimed by w2" not in out
    assert live(attached.root, t1)["worker"] == "w1"                                  # the winner's lease is intact
    assert worktree_count(attached) == 1 and not (attached.root / ".alpaca/attach/branches" / ("%s.json" % t2)).exists()
    assert len(events(attached.root, "attach-branch")) == 1


def test_a_failed_cut_returns_only_its_own_task_to_open(attached, monkeypatch, capsys):
    t1, t2 = add_tasks(attached.root, 2)
    assert claim(t2, "w2") == cli.PASS                                                # an unrelated claim

    def boom(*a, **k):
        raise AttachError("cannot cut today")
    monkeypatch.setattr(worktree, "cut", boom)
    capsys.readouterr()
    assert claim(t1, "w1") == cli.FAIL
    assert "GATE alpaca-task-claim: FAIL (attach: cannot cut today)" in capsys.readouterr().out
    assert task_row(attached.root, t1)["status"] == "open" and live(attached.root, t1) is None
    assert task_row(attached.root, t2)["status"] == "doing" and live(attached.root, t2)["worker"] == "w2"


def test_the_failed_claim_can_be_made_again_once_the_cause_is_gone(attached, monkeypatch):
    (t1,) = add_tasks(attached.root, 1)
    restore = break_remote(attached.upstream)
    assert claim(t1) == cli.FAIL and task_row(attached.root, t1)["status"] == "open"
    restore()
    assert claim(t1) == cli.PASS and task_row(attached.root, t1)["status"] == "doing"


def test_a_cut_that_dies_midway_undoes_the_worktree_the_branch_and_the_record(attached, monkeypatch):
    (task,) = add_tasks(attached.root, 1)

    def boom(*a, **k):
        raise AttachError("record is locked")
    real = worktree._commit_event
    monkeypatch.setattr(worktree, "_commit_event", boom)
    assert claim(task) == cli.FAIL
    assert worktree_count(attached) == 0 and branches(attached) == []
    assert not (attached.root / ".alpaca/attach/worktrees" / task).exists()
    assert not (attached.root / ".alpaca/attach/branches" / ("%s.json" % task)).exists()
    assert git(attached.app, "worktree", "list").count("\n") == 0
    monkeypatch.setattr(worktree, "_commit_event", real)
    assert claim(task) == cli.PASS                                                    # and the next try is clean


# ---- t-648.5: fresh means fresh ----------------------------------------------------------------------------------------

def test_an_offline_fetch_fails_the_claim_with_no_worktree(attached, capsys):
    t1, t2 = add_tasks(attached.root, 2)
    attached.upstream.commit({"x": "1\n"}, "advance")
    assert claim(t1) == cli.PASS
    assert git(attached.root / ".alpaca/attach/worktrees" / t1, "rev-parse", "HEAD") == attached.upstream.sha()
    break_remote(attached.upstream)
    capsys.readouterr()
    assert claim(t2, "w1") == cli.FAIL
    assert "GATE alpaca-task-claim: FAIL (attach: git fetch failed" in capsys.readouterr().out
    assert not (attached.root / ".alpaca/attach/worktrees" / t2).exists() and worktree_count(attached) == 1
    assert "alpaca/%s" % t2 not in branches(attached)
    assert task_row(attached.root, t2)["status"] == "open"


def test_a_failed_fetch_never_falls_back_to_the_old_remote_ref(attached):
    (task,) = add_tasks(attached.root, 1)
    old_ref = git(attached.app, "rev-parse", "refs/remotes/origin/main")
    break_remote(attached.upstream)
    with pytest.raises(AttachError, match="fetch failed"):
        worktree.cut(str(attached.root), task, worker="w1", session="cli")
    assert git(attached.app, "rev-parse", "refs/remotes/origin/main") == old_ref
    assert worktree_count(attached) == 0


def test_the_fetch_follows_a_force_pushed_base(attached):
    (task,) = add_tasks(attached.root, 1)
    git(attached.upstream.work, "reset", "-q", "--hard", "HEAD~1")
    git(attached.upstream.work, "push", "-q", "--force", "origin", "main")
    rewritten = attached.upstream.sha()
    assert claim(task) == cli.PASS
    assert git(attached.root / ".alpaca/attach/worktrees" / task, "rev-parse", "HEAD") == rewritten


def test_a_changed_clone_config_stops_the_claim_before_any_fetch(attached):
    """P8: a rewritten URL or helper would send the fetch, and its token, elsewhere."""
    (task,) = add_tasks(attached.root, 1)
    git(attached.app, "config", "--local", "url.https://evil.example/.insteadOf", attached.upstream.url)
    assert claim(task) == cli.FAIL
    with pytest.raises(AttachError, match="app git config changed since attach"):
        worktree.fetch_base(str(attached.root), config.load(str(attached.root)))
    assert worktree_count(attached) == 0 and task_row(attached.root, task)["status"] == "open"


# ---- t-648.7 and X4a --------------------------------------------------------------------------------------------------

def test_a_commit_in_the_worktree_carries_the_attach_identity(attached):
    (task,) = add_tasks(attached.root, 1)
    assert claim(task) == cli.PASS
    wt = attached.root / ".alpaca/attach/worktrees" / task
    (wt / "change.txt").write_text("work\n")
    wt_git(attached, task, "add", "change.txt")
    wt_git(attached, task, "commit", "-q", "-m", "work on the task")
    assert wt_git(attached, task, "log", "-1", "--format=%an <%ae> / %cn <%ce>") == \
        "Alpaca agent <alpaca-agent@localhost> / Alpaca agent <alpaca-agent@localhost>"
    assert git(attached.app, "rev-parse", "HEAD") == attached.upstream.sha()           # app did not move


def test_git_add_all_in_the_studio_stages_no_gitlink(attached):
    """X4a: the worktree has a .git file and would be staged as mode 160000 if it were not ignored."""
    (task,) = add_tasks(attached.root, 1)
    assert claim(task) == cli.PASS
    git(attached.root, "add", "-A")
    staged = git(attached.root, "ls-files", "-s")
    assert "160000" not in staged
    assert ".alpaca/attach" not in staged and not re.search(r"\sapp(/|$)", staged)
    assert subprocess.run(["git", "-C", str(attached.root), "check-ignore", "-q",
                           ".alpaca/attach/worktrees/%s/README.md" % task]).returncode == 0


def test_the_worktree_runs_no_hook_of_the_clone(attached):
    """P7: hooks are off for attach's own git calls, so a hook in the clone never runs at the cut."""
    marker = attached.root / "hook-ran"
    hook = attached.app / ".git/hooks/post-checkout"
    hook.write_text("#!/bin/sh\ntouch %s\n" % marker)
    hook.chmod(0o755)
    (task,) = add_tasks(attached.root, 1)
    assert claim(task) == cli.PASS and not marker.exists()


# ---- a renewal, a re-claim after the lease ran out -----------------------------------------------------------------------

def test_a_renewal_needs_the_worktree_and_cuts_nothing_new(attached, capsys):
    """A second `task claim` by the lease holder renews (claims.take); on_claim then only checks the worktree."""
    (task,) = add_tasks(attached.root, 1)
    assert claim(task) == cli.PASS
    wt = attached.root / ".alpaca/attach/worktrees" / task
    head = git(wt, "rev-parse", "HEAD")
    attached.upstream.commit({"x": "1\n"}, "advance")
    reopen_row(attached, task)
    capsys.readouterr()
    assert claim(task) == cli.PASS
    assert "work branch alpaca/%s" % task in capsys.readouterr().out
    assert worktree_count(attached) == 1 and len(events(attached.root, "attach-branch")) == 1
    assert git(wt, "rev-parse", "HEAD") == head and live(attached.root, task)["worker"] == "w1"


def test_a_renewal_without_a_worktree_fails_and_keeps_the_lease(attached):
    (task,) = add_tasks(attached.root, 1)
    conn = db.connect(str(attached.root))
    claims.take(conn, task, "w1", minutes=30, session="cli")                           # claimed before the branch existed
    conn.close()
    reopen_row(attached, task)
    assert claim(task) == cli.FAIL
    assert live(attached.root, task)["worker"] == "w1"                                 # a renewal failure releases nothing
    assert worktree_count(attached) == 0
    with pytest.raises(AttachError, match="no work branch yet"):
        worktree.on_claim(str(attached.root), task, "w1", {"status": "claimed"}, session="cli")


def test_a_released_task_claimed_again_resumes_its_work_branch(attached):
    (task,) = add_tasks(attached.root, 1)
    assert claim(task) == cli.PASS
    wt = attached.root / ".alpaca/attach/worktrees" / task
    (wt / "kept.txt").write_text("keep me\n")
    wt_git(attached, task, "add", "kept.txt")
    wt_git(attached, task, "commit", "-q", "-m", "work")
    work_head = git(wt, "rev-parse", "HEAD")
    conn = db.connect(str(attached.root))
    claims.release(conn, task, "w1", session="cli")
    conn.close()
    assert claim(task, "w2") == cli.PASS
    assert git(wt, "rev-parse", "HEAD") == work_head and (wt / "kept.txt").is_file() and worktree_count(attached) == 1
    first, second = events(attached.root, "attach-branch")
    assert "resumed" not in first["data"] and second["data"]["resumed"] is True and second["data"]["behind"] == 0


def test_an_unrelated_branch_of_the_same_name_is_not_taken_over(attached):
    (task,) = add_tasks(attached.root, 1)
    git(attached.app, "branch", "alpaca/%s" % task)
    assert claim(task) == cli.FAIL
    assert task_row(attached.root, task)["status"] == "open" and worktree_count(attached) == 0
    assert git(attached.app, "rev-parse", "alpaca/%s" % task) == attached.upstream.sha()   # the branch is as it was


def test_a_leftover_folder_without_its_branch_is_refused_and_not_deleted(attached):
    (task,) = add_tasks(attached.root, 1)
    left = attached.root / ".alpaca/attach/worktrees" / task
    left.mkdir(parents=True)
    (left / "precious.txt").write_text("not ours to delete\n")
    assert claim(task) == cli.FAIL
    assert (left / "precious.txt").read_text() == "not ours to delete\n"


# ---- the task id ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("task", ["", "t-", "t-1234567", "T-1", "t-1; rm -rf /", "../t-1", "t-1/../../x", "x-1", None, "t-01 "])
def test_only_a_task_id_can_name_a_branch(attached, task):
    with pytest.raises(AttachError, match="cannot name a work branch"):
        worktree.cut(str(attached.root), task, worker="w1", session="cli")
    with pytest.raises(AttachError, match="cannot name a work branch"):
        worktree.on_claim(str(attached.root), task, "w1", {"claim": {}}, session="cli")
    assert worktree_count(attached) == 0


# ---- parallel claims (t-648.3) -------------------------------------------------------------------------------------------

def test_parallel_claims_share_one_baseline_and_each_get_a_worktree(attached, tmp_path):
    tasks = add_tasks(attached.root, 4)
    counter = tmp_path / "gate-runs.txt"
    set_gates(attached.root, counting_gates(counter))                                  # a new gate set: no baseline yet
    with ThreadPoolExecutor(4) as pool:
        codes = list(pool.map(lambda t: claim(t, "w-" + t), tasks))
    assert codes == [cli.PASS] * 4 and worktree_count(attached) == 4
    key = baseline.key(config.load(str(attached.root)), attached.upstream.sha())
    assert (attached.root / ".alpaca/attach/baseline" / ("%s.json" % key)).is_file()
    assert sorted(counter.read_text().split()) == ["one", "two"]                       # the gates ran once, not four times
    assert {branch_file(attached, t)["baseline_key"] for t in tasks} == {key}
    assert not list((attached.root / ".alpaca/attach/baseline").glob(".tmp-*"))


# ---- R11: a plain project --------------------------------------------------------------------------------------------

def test_a_plain_project_claim_prints_exactly_what_it_printed_before(studio, capsys):
    (task,) = add_tasks(studio, 1)
    capsys.readouterr()
    assert claim(task) == cli.PASS
    out = capsys.readouterr().out
    assert re.fullmatch(r"%s claimed by w1 until \S+\n" % task, out), out
    assert not (studio / ".alpaca/attach").exists()


def test_a_project_with_a_bad_attach_block_claims_like_a_plain_one(attached, capsys):
    (task,) = add_tasks(attached.root, 1)
    write_attach(attached.root, repo="app", url="")                                    # invalid: no mode (P1)
    capsys.readouterr()
    assert claim(task) == cli.PASS
    assert capsys.readouterr().out.count("\n") == 1 and worktree_count(attached) == 0


# ---- fix round 1, B1: the undo of a failed cut deletes only what the cut wrote, through no link -----------------------

def test_a_link_at_the_branches_folder_stops_the_claim_and_deletes_nothing_outside(attached, tmp_path):
    (task,) = add_tasks(attached.root, 1)
    outside = tmp_path / "outside-branches"
    outside.mkdir()
    precious = outside / ("%s.json" % task)
    precious.write_text('{"owner": "keep me"}\n')
    (attached.root / ".alpaca/attach/branches").symlink_to(outside)
    assert claim(task) == cli.FAIL
    assert precious.read_text() == '{"owner": "keep me"}\n' and sorted(p.name for p in outside.iterdir()) == [precious.name]
    assert worktree_count(attached) == 0 and branches(attached) == [] and task_row(attached.root, task)["status"] == "open"


def test_the_undo_removes_the_record_it_wrote_and_not_a_record_it_did_not(attached, monkeypatch):
    (task,) = add_tasks(attached.root, 1)
    app = str(attached.app)
    rec = attached.root / ".alpaca/attach/branches" / ("%s.json" % task)
    rec.parent.mkdir(parents=True)
    rec.write_text("{}\n")
    worktree._undo(str(attached.root), app, task, "alpaca/%s" % task, str(attached.root / ".alpaca/attach/worktrees" / task),
                   False, False, False)
    assert rec.read_text() == "{}\n"                                                # made_record False: kept
    worktree._undo(str(attached.root), app, task, "alpaca/%s" % task, str(attached.root / ".alpaca/attach/worktrees" / task),
                   False, False, True)
    assert not rec.exists()


# ---- fix round 1, B3: a failed claim releases only a lease it still holds ---------------------------------------------

def test_a_failed_cut_after_the_lease_moved_on_does_not_release_the_new_holder(attached, monkeypatch):
    (t1,) = add_tasks(attached.root, 1)
    real = baseline.ensure

    def lease_moves_on(root, sha, **kw):
        conn = db.connect(str(attached.root))
        try:
            claims.release(conn, t1, "w1", session="owner")                           # what `task move deferred` does
            assert claims.take(conn, t1, "w2", session="w2")["status"] == claims.CLAIMED
        finally:
            conn.close()
        real(root, sha, **kw)
        raise AttachError("the baseline died after the lease moved on")
    monkeypatch.setattr(baseline, "ensure", lease_moves_on)
    assert claim(t1, "w1") == cli.FAIL
    assert live(attached.root, t1)["worker"] == "w2"
    assert task_row(attached.root, t1)["status"] == "doing"


def test_a_cut_whose_lease_moved_on_makes_no_worktree_and_the_new_holder_cuts_its_own(attached, monkeypatch, capsys):
    (t1,) = add_tasks(attached.root, 1)
    real = baseline.ensure
    won = {}

    def lease_moves_on(root, sha, **kw):
        out = real(root, sha, **kw)
        conn = db.connect(str(attached.root))
        try:
            claims.release(conn, t1, "w1", session="owner")
            won.update(claims.take(conn, t1, "w2", session="w2"))
        finally:
            conn.close()
        return out
    monkeypatch.setattr(baseline, "ensure", lease_moves_on)
    capsys.readouterr()
    assert claim(t1, "w1") == cli.FAIL
    assert "moved on during the cut" in capsys.readouterr().out
    assert worktree_count(attached) == 0 and branches(attached) == []                 # w1 made nothing
    assert live(attached.root, t1)["worker"] == "w2"                                   # and ended nothing of w2's
    monkeypatch.setattr(baseline, "ensure", real)
    worktree.on_claim(str(attached.root), t1, "w2", won, session="w2")                 # w2's own cut goes through
    assert worktree_count(attached) == 1 and branch_file(attached, t1)["worker"] == "w2"


def test_release_if_held_releases_the_holder_only_with_its_fence(attached):
    (t1,) = add_tasks(attached.root, 1)
    conn = db.connect(str(attached.root))
    try:
        got = claims.take(conn, t1, "w1", session="cli")
        assert worktree.release_if_held(conn, t1, "w2", got["fence"], session="cli") is False       # another worker
        assert worktree.release_if_held(conn, t1, "w1", got["fence"] + 1, session="cli") is False   # a stale fence
        assert live_in(conn, t1)["worker"] == "w1"
        assert worktree.release_if_held(conn, t1, "w1", got["fence"], session="cli") is True
        assert live_in(conn, t1) is None
        assert worktree.release_if_held(conn, t1, "w1", got["fence"], session="cli") is False       # already ended
    finally:
        conn.close()


def live_in(conn, task):
    return claims.live(conn, task)


# ---- fix round 1, B4: every claim fetches; a resumed branch is judged against the fresh base --------------------------

def release(att, task, by="w1"):
    conn = db.connect(str(att.root))
    try:
        claims.release(conn, task, by, session="cli")
    finally:
        conn.close()


def test_a_reclaim_fetches_the_base_before_it_resumes(attached):
    (task,) = add_tasks(attached.root, 1)
    assert claim(task) == cli.PASS
    release(attached, task)
    new = attached.upstream.commit({"z": "1\n"}, "upstream moved on")
    assert git(attached.app, "rev-parse", "refs/remotes/origin/main") != new
    assert claim(task, "w2") == cli.PASS
    assert git(attached.app, "rev-parse", "refs/remotes/origin/main") == new


def test_a_reclaim_of_a_branch_with_no_commit_moves_it_to_the_fresh_base(attached, capsys):
    (task,) = add_tasks(attached.root, 1)
    assert claim(task) == cli.PASS
    wt = attached.root / ".alpaca/attach/worktrees" / task
    old = git(wt, "rev-parse", "HEAD")
    release(attached, task)
    new = attached.upstream.commit({"z.txt": "1\n"}, "upstream moved on")
    app_before = app_state(attached)
    capsys.readouterr()
    assert claim(task, "w2") == cli.PASS
    out = capsys.readouterr().out
    assert git(wt, "rev-parse", "HEAD") == new and (wt / "z.txt").is_file()
    assert "resumed work branch alpaca/%s" % task in out and "base %s, upstream now %s, 0 behind" % (new[:12], new[:12]) in out
    info = branch_file(attached, task)
    assert info["base_sha"] == new and info["upstream_sha"] == new and info["behind"] == 0 and info["worker"] == "w2"
    assert info["baseline_key"] == baseline.key(config.load(str(attached.root)), new)
    assert baseline.load(str(attached.root), new) is not None and old != new          # the baseline of the new base exists
    assert git(attached.app, "rev-parse", "--verify", "refs/heads/alpaca/%s" % task) == new
    assert app_state(attached) == app_before                                            # app/ did not change
    assert worktree_count(attached) == 1


def test_a_reclaim_of_a_branch_with_commits_keeps_them_and_reports_how_far_behind(attached, capsys):
    (task,) = add_tasks(attached.root, 1)
    assert claim(task) == cli.PASS
    wt = attached.root / ".alpaca/attach/worktrees" / task
    base = git(wt, "rev-parse", "HEAD")
    (wt / "kept.txt").write_text("keep me\n")
    wt_git(attached, task, "add", "kept.txt")
    wt_git(attached, task, "commit", "-q", "-m", "work")
    work_head = git(wt, "rev-parse", "HEAD")
    release(attached, task)
    attached.upstream.commit({"a.txt": "1\n"}, "up 1")
    new = attached.upstream.commit({"b.txt": "1\n"}, "up 2")
    capsys.readouterr()
    assert claim(task, "w2") == cli.PASS
    out = capsys.readouterr().out
    assert git(wt, "rev-parse", "HEAD") == work_head and (wt / "kept.txt").is_file() and not (wt / "a.txt").exists()
    assert "(base %s, upstream now %s, 2 behind)" % (base[:12], new[:12]) in out
    info = branch_file(attached, task)
    assert info["base_sha"] == base and info["upstream_sha"] == new and info["behind"] == 2 and info["worker"] == "w2"
    ev = events(attached.root, "attach-branch")[-1]["data"]
    assert ev["resumed"] is True and ev["upstream_sha"] == new and ev["behind"] == 2 and ev["base_sha"] == base


def test_a_reclaim_with_uncommitted_edits_does_not_move_the_branch(attached):
    (task,) = add_tasks(attached.root, 1)
    assert claim(task) == cli.PASS
    wt = attached.root / ".alpaca/attach/worktrees" / task
    base = git(wt, "rev-parse", "HEAD")
    (wt / "scratch.txt").write_text("not committed\n")
    release(attached, task)
    attached.upstream.commit({"z.txt": "1\n"}, "upstream moved on")
    assert claim(task, "w2") == cli.PASS
    assert git(wt, "rev-parse", "HEAD") == base and (wt / "scratch.txt").read_text() == "not committed\n"
    assert branch_file(attached, task)["behind"] == 1


def test_an_offline_reclaim_fails_and_keeps_the_branch_and_the_work(attached):
    (task,) = add_tasks(attached.root, 1)
    assert claim(task) == cli.PASS
    wt = attached.root / ".alpaca/attach/worktrees" / task
    (wt / "kept.txt").write_text("keep me\n")
    wt_git(attached, task, "add", "kept.txt")
    wt_git(attached, task, "commit", "-q", "-m", "work")
    work_head = git(wt, "rev-parse", "HEAD")
    release(attached, task)
    restore = break_remote(attached.upstream)
    assert claim(task, "w2") == cli.FAIL
    assert task_row(attached.root, task)["status"] == "open"
    assert git(wt, "rev-parse", "HEAD") == work_head and (wt / "kept.txt").is_file()
    assert git(attached.app, "rev-parse", "--verify", "refs/heads/alpaca/%s" % task) == work_head
    restore()
    assert claim(task, "w2") == cli.PASS


def test_a_resume_that_cannot_write_its_record_leaves_the_branch_as_it_was(attached, monkeypatch):
    (task,) = add_tasks(attached.root, 1)
    assert claim(task) == cli.PASS
    wt = attached.root / ".alpaca/attach/worktrees" / task
    (wt / "kept.txt").write_text("keep me\n")
    wt_git(attached, task, "add", "kept.txt")
    wt_git(attached, task, "commit", "-q", "-m", "work")
    work_head = git(wt, "rev-parse", "HEAD")
    release(attached, task)

    def boom(*a, **k):
        raise AttachError("record is locked")
    monkeypatch.setattr(worktree, "_commit_event", boom)
    assert claim(task, "w2") == cli.FAIL
    assert git(wt, "rev-parse", "HEAD") == work_head and (wt / "kept.txt").is_file() and worktree_count(attached) == 1
    assert (attached.root / ".alpaca/attach/branches" / ("%s.json" % task)).is_file()


# ---- fix round 1, B5: the fetch URL is the attach.url of project.yaml, nothing else ----------------------------------------

def test_a_clean_attach_has_the_same_url_in_state_and_in_project_yaml(attached):
    state = json.loads((attached.root / ".alpaca/attach/state.json").read_text())
    assert state["fetch_url"] == config.load(str(attached.root)).url == attached.upstream.url


def test_a_fetch_url_in_project_yaml_that_the_record_does_not_carry_is_not_followed(attached, tmp_path):
    """State.json is output now, so the tie that matters is project.yaml against the attach-init event."""
    from alpaca.tests.attachkit import make_upstream
    (task,) = add_tasks(attached.root, 1)
    (tmp_path / "evil").mkdir()
    other = make_upstream(tmp_path / "evil", name="evil", files={"EVIL.txt": "another repository\n"})
    block = dict(config.raw(str(attached.root)))
    block["url"] = other.url
    write_attach(attached.root, **block)
    assert claim(task) == cli.FAIL
    assert worktree_count(attached) == 0 and branches(attached) == []
    assert not (attached.root / ".alpaca/attach/worktrees" / task).exists()
    with pytest.raises(AttachError, match="differs from the URL recorded when the project was attached"):
        worktree.fetch_base(str(attached.root), config.load(str(attached.root)))


def test_a_fetch_url_forged_in_state_json_changes_nothing(attached, tmp_path):
    """The old B5 attack, restated: state.json is output, no decision reads it, so a forged `fetch_url` neither
    redirects the fetch nor stops a claim that project.yaml and the record agree on."""
    from alpaca.tests.attachkit import make_upstream
    (task,) = add_tasks(attached.root, 1)
    (tmp_path / "evil").mkdir()
    other = make_upstream(tmp_path / "evil", name="evil", files={"EVIL.txt": "another repository\n"})
    state_path = attached.root / ".alpaca/attach/state.json"
    state = json.loads(state_path.read_text())
    state["fetch_url"] = other.url
    state_path.write_text(json.dumps(state))
    assert claim(task) == cli.PASS
    wt = attached.root / ".alpaca/attach/worktrees" / task
    assert not (wt / "EVIL.txt").exists() and git(wt, "rev-parse", "HEAD") == attached.upstream.sha()


def test_a_source_with_a_transport_helper_is_refused_at_the_fetch(attached, tmp_path):
    (task,) = add_tasks(attached.root, 1)
    marker = tmp_path / "pwned"
    block = dict(config.raw(str(attached.root)))
    block["url"] = "ext::sh -c 'touch %s'" % marker
    write_attach(attached.root, **block)
    state_path = attached.root / ".alpaca/attach/state.json"
    state = json.loads(state_path.read_text())
    state["fetch_url"] = block["url"]
    state_path.write_text(json.dumps(state))
    with pytest.raises(AttachError, match="transport helper"):
        worktree.fetch_base(str(attached.root), config.load(str(attached.root)))
    assert not marker.exists()


# ---- fix round 1: a renewal over an incomplete record fails with a reason, not a traceback ----------------------------------

def test_a_damaged_record_file_changes_nothing_about_a_renewal(attached, capsys):
    """The old "incomplete record" refusal, restated: `branches/<task>.json` is output, so a renewal reads
    the record's event and the derived folder, and prints the folder it derived."""
    (task,) = add_tasks(attached.root, 1)
    assert claim(task) == cli.PASS
    rec = attached.root / ".alpaca/attach/branches" / ("%s.json" % task)
    info = json.loads(rec.read_text())
    del info["base_sha"]
    info["worktree"] = "/somewhere/else"
    rec.write_text(json.dumps(info))
    reopen_row(attached, task)
    capsys.readouterr()
    assert claim(task) == cli.PASS
    out = capsys.readouterr().out
    assert "/somewhere/else" not in out and os.path.realpath(str(attached.root / ".alpaca/attach/worktrees" / task)) in out
    assert live(attached.root, task)["worker"] == "w1"


# ---- fix round 2 (class review r2): plain studio files are output, git in a work folder is proved ------------------------

def derived(att, task):
    return os.path.realpath(str(att.root / ".alpaca/attach/worktrees" / task))


def record_file(att, task):
    return att.root / ".alpaca/attach/branches" / ("%s.json" % task)


def _evil_url(tmp_path):
    from alpaca.tests.attachkit import make_upstream
    (tmp_path / "evil").mkdir(exist_ok=True)
    return make_upstream(tmp_path / "evil", name="evil", files={"EVIL.txt": "another repository\n"}).url


STATE_FORGERIES = ["deleted", "empty-object", "garbage", "fetch-url-evil", "digest-removed", "digest-wrong", "root-and-repo-hostile"]
RECORD_FORGERIES = ["deleted", "empty-object", "garbage", "worktree-app", "worktree-outside", "worktree-nul", "base-stranger",
                    "baseline-key-bogus", "branch-other", "fetch-url-evil"]


def _forge_file(path, how, att, tmp_path, is_state):
    if how == "deleted":
        os.unlink(path)
        return
    if how == "empty-object":
        path.write_text("{}")
        return
    if how == "garbage":
        path.write_bytes(b"\xff\x00not json at all")
        return
    data = json.loads(path.read_text())
    outside = tmp_path / "outside-folder"
    outside.mkdir(exist_ok=True)
    if how == "fetch-url-evil":
        data["fetch_url"] = _evil_url(tmp_path)
        data["url"] = data["fetch_url"]
    elif how == "digest-removed":
        data.pop("config_digest", None)
    elif how == "digest-wrong":
        data["config_digest"] = "0" * 64
    elif how == "root-and-repo-hostile":
        data.update(root="/", repo="../etc", base="x", upstream="y", base_sha_at_attach="f" * 40)
    elif how == "worktree-app":
        data["worktree"] = str(att.app)
    elif how == "worktree-outside":
        data["worktree"] = str(outside)
    elif how == "worktree-nul":
        data["worktree"] = "/x/\u0000/y"
    elif how == "base-stranger":
        data["base_sha"] = "f" * 40
    elif how == "baseline-key-bogus":
        data["baseline_key"] = "../../etc"
    elif how == "branch-other":
        data["branch"] = "main"
    else:
        raise AssertionError(how)
    path.write_text(json.dumps(data))


def _assert_the_outcome_of_an_unforged_claim(att, task, folder, new, out, app_before, rc):
    assert rc == cli.PASS, out
    assert folder in out and "claimed by w2" in out, out
    assert git(folder, "rev-parse", "HEAD") == new and not (Path(folder) / "EVIL.txt").exists()
    assert git(att.app, "rev-parse", "--verify", "refs/heads/alpaca/%s" % task) == new
    assert app_state(att) == app_before                                       # app/ did not change
    assert live(att.root, task)["worker"] == "w2"


@pytest.mark.parametrize("how", STATE_FORGERIES)
def test_forged_state_json_changes_no_decision(attached, tmp_path, capsys, how):
    """Every forgery of state.json (deleted, empty, garbage, hostile values) gives the outcome of an unforged
    studio: the same return codes, the same printed folder, the same branch tips, app/ unchanged."""
    (task,) = add_tasks(attached.root, 1)
    _forge_file(attached.root / ".alpaca/attach/state.json", how, attached, tmp_path, True)
    capsys.readouterr()
    assert claim(task, "w1") == cli.PASS
    assert derived(attached, task) in capsys.readouterr().out
    release(attached, task, "w1")
    new = attached.upstream.commit({"later.txt": "x\n"}, "upstream moved")
    app_before = app_state(attached)
    capsys.readouterr()
    rc = claim(task, "w2")
    _assert_the_outcome_of_an_unforged_claim(attached, task, derived(attached, task), new, capsys.readouterr().out, app_before, rc)


@pytest.mark.parametrize("how", RECORD_FORGERIES)
def test_forged_branch_record_changes_no_decision(attached, tmp_path, capsys, how):
    (task,) = add_tasks(attached.root, 1)
    assert claim(task, "w1") == cli.PASS
    release(attached, task, "w1")
    _forge_file(record_file(attached, task), how, attached, tmp_path, False)
    new = attached.upstream.commit({"later.txt": "x\n"}, "upstream moved")
    app_before = app_state(attached)
    capsys.readouterr()
    rc = claim(task, "w2")
    _assert_the_outcome_of_an_unforged_claim(attached, task, derived(attached, task), new, capsys.readouterr().out, app_before, rc)
    reopen_row(attached, task)                         # and a renewal prints the derived folder, never a recorded one
    capsys.readouterr()
    assert claim(task, "w2") == cli.PASS and derived(attached, task) in capsys.readouterr().out


def _plant_replace_ref(att, up):
    git(att.app, "fetch", "-q", "--no-tags", att.upstream.url, "+refs/heads/main:refs/pm/up")
    blob = git(att.app, "hash-object", "-w", "--stdin", input="planted\n")
    tree = git(att.app, "mktree", input="100644 blob %s\tPLANTED\n" % blob)
    git(att.app, "replace", up, git(att.app, "commit-tree", tree, "-m", "planted"))


GIT_FORGERIES = ["gitfile-to-app", "gitfile-deleted", "gitfile-to-sibling", "commondir", "replace-ref", "shallow-file",
                 "worktree-config", "head-switched"]


@pytest.mark.parametrize("how", GIT_FORGERIES)
def test_forged_git_files_change_no_decision(attached, tmp_path, capsys, how):
    """Each forgery of what git reads either leaves the outcome of the unforged claim or fails the claim with the
    lease freed; app/ and the studio's own repository never move."""
    t1, t2 = add_tasks(attached.root, 2)
    assert claim(t1, "w1") == cli.PASS
    assert claim(t2, "w3") == cli.PASS
    release(attached, t1, "w1")
    folder = derived(attached, t1)
    admin = attached.app / ".git/worktrees" / t1
    new = attached.upstream.commit({"later.txt": "x\n"}, "upstream moved")
    tip2 = git(attached.app, "rev-parse", "refs/heads/alpaca/%s" % t2)
    if how == "gitfile-to-app":
        (Path(folder) / ".git").write_text("gitdir: %s\n" % (attached.app / ".git"))
    elif how == "gitfile-deleted":
        os.unlink(os.path.join(folder, ".git"))
    elif how == "gitfile-to-sibling":
        (Path(folder) / ".git").write_text("gitdir: %s\n" % (attached.app / ".git/worktrees" / t2))
    elif how == "commondir":
        (admin / "commondir").write_text("/tmp/elsewhere\n")
    elif how == "replace-ref":
        _plant_replace_ref(attached, new)
    elif how == "shallow-file":
        (attached.app / ".git/shallow").write_text("%s\n" % git(attached.app, "rev-parse", "HEAD"))
    elif how == "worktree-config":
        git(attached.app, "config", "extensions.worktreeConfig", "true")
    elif how == "head-switched":
        (admin / "HEAD").write_text("ref: refs/heads/main\n")
    app_before = app_state(attached)
    studio_before = (git(attached.root, "rev-parse", "-q", "--verify", "HEAD", check=False), git(attached.root, "branch", "--list"))
    capsys.readouterr()
    rc = claim(t1, "w2")
    out = capsys.readouterr().out
    print("forged git file %s: rc %s | %s" % (how, rc, out.strip().splitlines()[-1][:200] if out.strip() else ""))
    assert app_state(attached) == app_before and git(attached.app, "rev-parse", "refs/heads/alpaca/%s" % t2) == tip2
    assert (git(attached.root, "rev-parse", "-q", "--verify", "HEAD", check=False), git(attached.root, "branch", "--list")) == studio_before
    if rc == cli.PASS:
        assert how == "replace-ref", "%s: the claim passed over a forged git file:\n%s" % (how, out)
        truth = sorted(git(attached.upstream.bare, "ls-tree", "--name-only", new).split("\n"))
        assert sorted(p.name for p in Path(folder).iterdir() if p.name != ".git") == truth
        assert git(attached.app, "rev-parse", "--verify", "refs/heads/alpaca/%s" % t1) == new
    else:
        assert rc == cli.FAIL and live(attached.root, t1) is None and task_row(attached.root, t1)["status"] == "open", out
        assert git(attached.app, "rev-parse", "--verify", "refs/heads/alpaca/%s" % t1) != new


# ---- U1 and U2: the undo deletes through no link and prunes nothing global ------------------------------------------------

def test_t7_a_failed_post_add_proof_deletes_nothing_through_a_link(attached, tmp_path, monkeypatch):
    """The folder is swapped for a link to an outside folder; git then writes a checkout (with a gitfile back to the
    admin folder) there, and the outside folder holds a file of its own by the time the proof fails. The old undo ran
    `git worktree remove --force` through the link and deleted that file (outside the studio)."""
    (task,) = add_tasks(attached.root, 1)
    outside = tmp_path / "outside-swap"
    outside.mkdir()
    keep = outside / "keep.txt"
    real_git = gitops.git

    def swap(repo, *args, **kw):
        if args[:2] == ("worktree", "add"):
            folder = Path(args[-2])
            os.rmdir(folder)
            folder.symlink_to(outside)
            out = real_git(repo, *args, **kw)
            keep.write_text("keep\n")
            return out
        return real_git(repo, *args, **kw)
    monkeypatch.setattr(gitops, "git", swap)
    assert claim(task, "w1") == cli.FAIL
    monkeypatch.setattr(gitops, "git", real_git)
    assert keep.read_text() == "keep\n" and (outside / ".git").is_file()               # the checkout stays where git put it
    assert not os.path.lexists(attached.root / ".alpaca/attach/worktrees" / task)      # the link is gone
    assert live(attached.root, task) is None and branches(attached) == [] and worktree_count(attached) == 0
    assert not (attached.app / ".git/worktrees" / task).exists()
    assert claim(task, "w1") == cli.PASS                                               # and the task can be claimed again


def test_t8_a_failed_cut_of_one_task_leaves_the_admin_folder_of_another(attached, monkeypatch):
    """A global `git worktree prune` in the undo dropped the admin folder of any task whose folder was out of
    sight at that moment (a folder renamed by hand, a disk not mounted)."""
    t1, t2 = add_tasks(attached.root, 2)
    assert claim(t1, "w1") == cli.PASS
    away = Path(derived(attached, t1) + ".away")
    os.rename(derived(attached, t1), away)                                              # t-001's folder is out of sight
    assert git(attached.app, "worktree", "list", "--porcelain").count("prunable") == 1

    def boom(*a, **k):
        raise AttachError("the event step died")
    real = worktree._commit_event
    monkeypatch.setattr(worktree, "_commit_event", boom)
    assert claim(t2, "w2") == cli.FAIL
    monkeypatch.setattr(worktree, "_commit_event", real)
    assert (attached.app / ".git/worktrees" / t1).is_dir(), "the undo of t-002 removed the admin folder of t-001"
    assert not (attached.app / ".git/worktrees" / t2).exists() and branches(attached) == ["alpaca/%s" % t1]
    os.rename(away, derived(attached, t1))
    gitops.prove_worktree(str(attached.app), t1, derived(attached, t1), "alpaca/%s" % t1)


def test_a_failed_add_leaves_neither_a_branch_nor_an_admin_folder(attached, monkeypatch):
    (task,) = add_tasks(attached.root, 1)
    real_git = gitops.git

    def add_then_die(repo, *args, **kw):
        out = real_git(repo, *args, **kw)
        if args[:2] == ("worktree", "add"):
            raise AttachError("git died after the add")
        return out
    monkeypatch.setattr(gitops, "git", add_then_die)
    assert claim(task, "w1") == cli.FAIL
    monkeypatch.setattr(gitops, "git", real_git)
    assert branches(attached) == [] and worktree_count(attached) == 0 and not (attached.app / ".git/worktrees" / task).exists()
    assert not os.path.lexists(attached.root / ".alpaca/attach/worktrees" / task)
    assert claim(task, "w1") == cli.PASS


def test_a_branch_someone_else_makes_during_the_cut_is_not_deleted_by_the_undo(attached, monkeypatch):
    """The cut makes its branch with `git branch`, which refuses a name that exists, so a branch made by a person a
    moment after the cut checked that the name was free is not the cut's to delete."""
    (task,) = add_tasks(attached.root, 1)
    real_git = gitops.git
    first = git(attached.app, "rev-parse", "HEAD~1")

    def human_first(repo, *args, **kw):
        if args[:1] == ("branch",) and "alpaca/%s" % task in args:
            git(attached.app, "branch", "alpaca/%s" % task, first)            # the person's branch, on an older commit
        return real_git(repo, *args, **kw)
    monkeypatch.setattr(gitops, "git", human_first)
    assert claim(task, "w1") == cli.FAIL
    monkeypatch.setattr(gitops, "git", real_git)
    assert git(attached.app, "rev-parse", "alpaca/%s" % task) == first
    assert live(attached.root, task) is None and worktree_count(attached) == 0


# ---- S4b: a shallow file added after attach ------------------------------------------------------------------------------

def test_s4b_a_shallow_file_added_after_attach_refuses_the_claim(attached, capsys):
    (task,) = add_tasks(attached.root, 1)
    (attached.app / ".git/shallow").write_text("%s\n" % git(attached.app, "rev-parse", "HEAD"))
    capsys.readouterr()
    assert claim(task) == cli.FAIL
    assert "shallow" in capsys.readouterr().out
    assert live(attached.root, task) is None and worktree_count(attached) == 0 and branches(attached) == []


# ---- PM-2a and C4: a resume that keeps the branch says why -----------------------------------------------------------------

def test_a_work_branch_also_checked_out_in_app_is_kept_on_its_base_and_the_claim_passes(attached, capsys):
    (task,) = add_tasks(attached.root, 1)
    assert claim(task) == cli.PASS
    release(attached, task)
    git(attached.app, "checkout", "-q", "--ignore-other-worktrees", "alpaca/%s" % task)
    old = git(attached.app, "rev-parse", "HEAD")
    new = attached.upstream.commit({"later.txt": "x\n"}, "upstream moved")
    before = app_state(attached)
    capsys.readouterr()
    assert claim(task, "w2") == cli.PASS
    out = capsys.readouterr().out
    assert "kept on its base: the branch is also checked out in %s" % os.path.realpath(str(attached.app)) in out and "1 behind" in out
    assert app_state(attached) == before and git(derived(attached, task), "rev-parse", "HEAD") == old != new
    assert live(attached.root, task)["worker"] == "w2"


def test_an_ignored_file_in_the_way_keeps_the_branch_and_says_so(attached, capsys):
    attached.upstream.commit({".gitignore": "secrets.env\n"}, "ignore secrets")
    (task,) = add_tasks(attached.root, 1)
    assert claim(task) == cli.PASS
    folder = Path(derived(attached, task))
    (folder / "secrets.env").write_text("MINE\n")
    release(attached, task)
    attached.upstream.commit({"secrets.env": "TEMPLATE=1\n", ".gitignore": ""}, "track a template")
    capsys.readouterr()
    assert claim(task, "w2") == cli.PASS
    out = capsys.readouterr().out
    assert "kept on its base: a file in the work folder that git ignores is in the way" in out and "1 behind" in out
    assert (folder / "secrets.env").read_text() == "MINE\n"


def test_a_clean_branch_with_nothing_in_the_way_moves_and_says_nothing_about_keeping(attached, capsys):
    (task,) = add_tasks(attached.root, 1)
    assert claim(task) == cli.PASS
    release(attached, task)
    new = attached.upstream.commit({"later.txt": "x\n"}, "upstream moved")
    capsys.readouterr()
    assert claim(task, "w2") == cli.PASS
    assert "kept on its base" not in capsys.readouterr().out and git(derived(attached, task), "rev-parse", "HEAD") == new


# ---- C6: ALPACA-MANIFEST in the fetched tree ---------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["ALPACA-MANIFEST", "sub/Alpaca-Manifest"])
def test_a_base_holding_alpaca_manifest_stops_a_first_claim_with_nothing_left_behind(attached, capsys, name):
    (task,) = add_tasks(attached.root, 1)
    attached.upstream.commit({name: "x\n"}, "upstream becomes an alpaca project")
    capsys.readouterr()
    assert claim(task) == cli.FAIL
    assert "ALPACA-MANIFEST" in capsys.readouterr().out
    assert live(attached.root, task) is None and branches(attached) == [] and worktree_count(attached) == 0
    assert not os.path.lexists(derived(attached, task)) and not record_file(attached, task).exists()
    with pytest.raises(AttachError, match="ALPACA-MANIFEST"):
        worktree.fetch_base(str(attached.root), config.load(str(attached.root)))


@pytest.mark.parametrize("name", ["ALPACA-MANIFEST", "sub/Alpaca-Manifest"])
def test_a_resume_keeps_the_branch_when_the_fetched_base_holds_alpaca_manifest_and_says_so(attached, capsys, name):
    (task,) = add_tasks(attached.root, 1)
    assert claim(task) == cli.PASS
    folder = derived(attached, task)
    head = git(folder, "rev-parse", "HEAD")
    release(attached, task)
    attached.upstream.commit({name: "x\n"}, "upstream becomes an alpaca project")
    capsys.readouterr()
    assert claim(task, "w2") == cli.PASS
    assert "kept on its base: the fetched base holds ALPACA-MANIFEST" in capsys.readouterr().out
    assert git(folder, "rev-parse", "HEAD") == head
    assert json.loads(record_file(attached, task).read_text())["behind"] == 1


# ---- K2: an exception of any kind after the take frees the task ----------------------------------------------------------------

@pytest.mark.parametrize("exc", [TypeError("boom"), KeyError("k"), ValueError("v"), OSError("disk"), AttachError("a refusal"),
                                 RuntimeError("x" * 500)])
def test_k2_any_exception_out_of_the_cut_fails_the_claim_cleanly_and_frees_the_task(attached, monkeypatch, capsys, exc):
    (task,) = add_tasks(attached.root, 1)

    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(worktree, "on_claim", boom)
    capsys.readouterr()
    assert claim(task, "w1") == cli.FAIL
    out = capsys.readouterr().out
    assert "GATE alpaca-task-claim: FAIL (attach: " in out and "Traceback" not in out
    if not isinstance(exc, (OSError, AttachError)):
        assert "attach: internal %s" % type(exc).__name__ in out and len(out.splitlines()[-1]) < 400
    assert live(attached.root, task) is None and task_row(attached.root, task)["status"] == "open"


def test_k2_an_interrupt_frees_the_task_and_is_raised_again(attached, monkeypatch):
    (task,) = add_tasks(attached.root, 1)

    def interrupt(*a, **k):
        raise KeyboardInterrupt()
    monkeypatch.setattr(worktree, "on_claim", interrupt)
    with pytest.raises(KeyboardInterrupt):
        claim(task, "w1")
    assert live(attached.root, task) is None and task_row(attached.root, task)["status"] == "open"


def test_k2_a_release_that_fails_is_named_and_does_not_hide_the_first_error(attached, monkeypatch, capsys):
    (task,) = add_tasks(attached.root, 1)

    def boom(*a, **k):
        raise AttachError("the cut died")

    def release_boom(*a, **k):
        raise RuntimeError("the record is locked")
    monkeypatch.setattr(worktree, "on_claim", boom)
    monkeypatch.setattr(worktree, "release_if_held", release_boom)
    capsys.readouterr()
    assert claim(task, "w1") == cli.FAIL
    out = capsys.readouterr().out
    assert "the cut died" in out and "the lease release also failed: the record is locked" in out


def test_k2_a_renewal_that_fails_keeps_its_lease(attached, monkeypatch):
    (task,) = add_tasks(attached.root, 1)
    assert claim(task, "w1") == cli.PASS
    reopen_row(attached, task)

    def boom(*a, **k):
        raise TypeError("a surprise")
    monkeypatch.setattr(worktree, "on_claim", boom)
    assert claim(task, "w1") == cli.FAIL
    assert live(attached.root, task)["worker"] == "w1"


# ---- K3 and A8: the lease is checked after the last side effect, and nothing is printed before ---------------------------------

def test_a8_nothing_is_printed_when_the_lease_moved_during_the_claim(attached, monkeypatch, capsys):
    (task,) = add_tasks(attached.root, 1)
    real = worktree.on_claim

    def lease_moves(root, t, worker, res, **kw):
        lines = real(root, t, worker, res, **kw)
        conn = db.connect(str(attached.root))
        try:
            claims.release(conn, t, "w1", session="owner")
            assert claims.take(conn, t, "w2", session="w2")["status"] == claims.CLAIMED
        finally:
            conn.close()
        return lines
    monkeypatch.setattr(worktree, "on_claim", lease_moves)
    capsys.readouterr()
    assert claim(task, "w1") == cli.FAIL
    out = capsys.readouterr().out
    assert "moved on during the claim" in out and "work branch" not in out and "claimed by w1" not in out
    assert live(attached.root, task)["worker"] == "w2"                                 # the new holder keeps its lease


def test_k3_a_lease_that_moved_during_the_add_writes_no_event_and_leaves_nothing(attached, monkeypatch):
    """The lease check and the `attach-branch` event are one transaction (the last step of the cut): a lease that
    moved on before it writes no event, and the cut is undone. The end point in after_claim alone would fail the
    claim but leave an event, a branch and a folder naming a worker that no longer holds the task."""
    (task,) = add_tasks(attached.root, 1)
    real_git = gitops.git
    moved = []

    def git_spy(repo, *args, **kw):
        if args[:2] == ("worktree", "add") and not moved:
            conn = db.connect(str(attached.root))
            try:
                claims.release(conn, task, "w1", session="owner")
                assert claims.take(conn, task, "w2", session="w2")["status"] == claims.CLAIMED
            finally:
                conn.close()
            moved.append(True)
        return real_git(repo, *args, **kw)
    monkeypatch.setattr(gitops, "git", git_spy)
    assert claim(task, "w1") == cli.FAIL
    monkeypatch.setattr(gitops, "git", real_git)
    assert moved and events(attached.root, "attach-branch") == []
    assert branches(attached) == [] and worktree_count(attached) == 0 and not os.path.lexists(derived(attached, task))
    assert not record_file(attached, task).exists() and not (attached.app / ".git/worktrees" / task).exists()
    assert live(attached.root, task)["worker"] == "w2"


# ---- self-heal: the event step fails after the branch moved ------------------------------------------------------------------------

def test_a_resume_whose_event_step_failed_after_the_branch_moved_heals_on_the_next_claim(attached, monkeypatch, capsys):
    (task,) = add_tasks(attached.root, 1)
    assert claim(task) == cli.PASS
    folder = derived(attached, task)
    release(attached, task)
    new = attached.upstream.commit({"later.txt": "x\n"}, "upstream moved")
    real = worktree._commit_event

    def boom(*a, **k):
        raise AttachError("the event step died")
    monkeypatch.setattr(worktree, "_commit_event", boom)
    assert claim(task, "w2") == cli.FAIL
    monkeypatch.setattr(worktree, "_commit_event", real)
    assert git(folder, "rev-parse", "HEAD") == new and live(attached.root, task) is None      # the branch moved, nothing recorded
    capsys.readouterr()
    assert claim(task, "w3") == cli.PASS
    assert "0 behind" in capsys.readouterr().out
    last = events(attached.root, "attach-branch")[-1]["data"]
    assert last["base_sha"] == new and last["behind"] == 0 and last["worker"] == "w3" and last["resumed"] is True


# ---- the lock order ---------------------------------------------------------------------------------------------------------------

def test_the_fetch_lock_is_never_taken_inside_a_db_transaction():
    """`_commit_event` takes the db write lock while the fetch lock is held, so no code may do it the other way."""
    import ast
    from pathlib import Path as _P
    bad = []
    for path in sorted((_P(worktree.__file__).parent).glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.With) and any(isinstance(i.context_expr, ast.Call) and getattr(i.context_expr.func, "attr", "") == "transaction"
                                                  for i in node.items):
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call) and getattr(inner.func, "id", getattr(inner.func, "attr", "")) == "_locked":
                        bad.append("%s:%d" % (path.name, inner.lineno))
    assert not bad, bad


# ---- proven: a missing proof is a refusal, never a skipped check -----------------------------------------------------------------------

def test_a_studio_with_an_attach_block_but_no_attach_init_event_is_not_fetched_or_cut(studio, upstream):
    from alpaca.attach import proven
    from alpaca.tests.attachkit import write_attach
    write_attach(studio, repo="app", url=upstream.url, gates=[{"name": "test", "command": ["make", "test"]}])
    cfg = config.load(str(studio))
    with pytest.raises(AttachError, match="no attach-init event"):
        proven.init_facts(str(studio), cfg)
    with pytest.raises(AttachError, match="no attach-init event"):
        proven.fetch_url(str(studio), cfg)
    with pytest.raises(AttachError, match="no attach-init event"):
        proven.config_unchanged(str(studio), cfg, str(studio / "app"))


def test_the_proved_view_of_a_branch_needs_its_event_and_its_derived_folder(attached):
    from alpaca.attach import proven
    (task,) = add_tasks(attached.root, 1)
    root = str(attached.root)
    cfg = config.load(root)
    assert proven.branch_view(root, cfg, task) is None and proven.task_checkout(root, task) is None
    assert claim(task) == cli.PASS
    view = proven.branch_view(root, cfg, task)
    assert view["worktree"] == derived(attached, task) and view["branch"] == "alpaca/%s" % task
    assert view["base_sha"] == attached.upstream.sha() and view["worker"] == "w1" and view["fetch_url"] == cfg.url
    assert proven.task_checkout(root, task) == Path(derived(attached, task)) == config.task_checkout(root, task)
    assert config.task_branch(root, task) == view
    shutil.rmtree(derived(attached, task))
    assert proven.branch_view(root, cfg, task) is None and proven.task_checkout(root, task) is None
    for bad in (None, "", "t-", "../t-001", "T-001", 7):
        assert proven.branch_view(root, cfg, bad) is None and proven.task_folder(root, cfg, bad) is None


def test_the_derived_folder_is_not_taken_when_it_is_a_link_or_the_clone(attached, tmp_path):
    from alpaca.attach import proven
    root = str(attached.root)
    cfg = config.load(root)
    parent = attached.root / ".alpaca/attach/worktrees"
    parent.mkdir(parents=True)
    (parent / "t-001").symlink_to(attached.app)
    assert proven.task_folder(root, cfg, "t-001") is None
    (parent / "t-001").unlink()
    (parent / "t-001").mkdir()
    assert proven.task_folder(root, cfg, "t-001") == os.path.join(os.path.realpath(root), ".alpaca/attach/worktrees/t-001")
    shutil.rmtree(parent)
    parent.symlink_to(tmp_path)
    assert proven.task_folder(root, cfg, "t-001") is None
