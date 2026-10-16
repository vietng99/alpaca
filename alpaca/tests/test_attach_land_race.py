"""`alpaca attach land` commits what it checked, and only one stage or land of a task runs at a time (op-062 t-653,
pre-mortem item 4 and the adversarial finding G1 of round 2).

`land` used to check a patch on a scratch index and then commit the whole live index, so a file a stage staged
meanwhile (the research pack's measure.sh, copied and `git add`ed) rode along in the commit. It now commits the
tree the scratch index holds, moves the branch only if it still stands at the head it read first, works from one
copy of the patch bytes, and runs under a per-task lock that `run_stage` takes too.
"""
import fcntl
import os
import subprocess
import threading
from pathlib import Path

import pytest
import yaml

from alpaca import cli, db
from alpaca.attach import AttachError, STATE_DIR, gitops, runbooks, safefs
from alpaca.tests.attachkit import add_tasks, attached, git, studio, upstream  # noqa: F401

PACK_FILE = "#!/bin/sh\necho pack only\n"


def claimed(att):
    (task,) = add_tasks(att.root, 1)
    assert cli.main(["task", "claim", task, "--by", "w1"]) == cli.PASS
    return task, os.path.realpath(str(att.root / STATE_DIR / "worktrees" / task))


def wt(att, task, folder, *args):
    return gitops.in_worktree(str(att.app), task, folder, *args)


def diff_of(tmp_path, files, name="p.patch"):
    """A patch that adds each `{rel: text}` file, made in a scratch repository."""
    scratch = tmp_path / ("scratch-" + name)
    scratch.mkdir()
    git(scratch, "init", "-q", "-b", "main")
    (scratch / "keep").write_text("k\n")
    git(scratch, "add", "-A")
    git(scratch, "commit", "-q", "-m", "b")
    for rel, text in files.items():
        target = scratch / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git(scratch, "add", "-A")
    patch = tmp_path / name
    patch.write_bytes(subprocess.run(["git", "-C", str(scratch), "diff", "--cached", "--binary"],
                                     check=True, capture_output=True).stdout)
    return str(patch)


def write_pack(att):
    Path(att.root, "research").mkdir(exist_ok=True)
    Path(att.root, "research", "measure.sh").write_text(PACK_FILE)


def stage_runbook(att, run, name="rb"):
    folder = Path(att.root, "runbooks", name)
    folder.mkdir(parents=True, exist_ok=True)
    body = {"id": "s1", "title": "s", "run": run, "workdir": "${ATTACH_WORKTREE}", "timeout": 60,
            "checks": [{"id": "c1", "type": "exit-code"}]}
    path = folder / "runbook.yaml"
    path.write_text(yaml.safe_dump({"runbook": 1, "id": "rb", "title": "t", "stages": [body]}, sort_keys=False))
    return str(path)


def landed_files(att, task, folder, before, head):
    return wt(att, task, folder, "diff", "--name-only", before, head).split()


@pytest.mark.parametrize("stage_adds", [False, True])
def test_the_commit_holds_the_checked_tree_when_a_stage_writes_after_the_check(attached, tmp_path, monkeypatch, stage_adds):
    """A file written (and, with stage_adds, `git add`ed) after the scratch check is not in the commit. It stays in
    the work folder, where `status` shows it."""
    write_pack(attached)
    task, folder = claimed(attached)
    real = runbooks._staged_blobs

    def racing(*args, **kw):
        got = real(*args, **kw)
        Path(folder, "copied-measure.sh").write_text(PACK_FILE)
        if stage_adds:
            wt(attached, task, folder, "add", "copied-measure.sh")
        return got

    monkeypatch.setattr(runbooks, "_staged_blobs", racing)
    before = wt(attached, task, folder, "rev-parse", "HEAD")
    head = runbooks.land(str(attached.root), task, diff_of(tmp_path, {"src/landed.py": "y = 1\n"}), message="m")
    assert landed_files(attached, task, folder, before, head) == ["src/landed.py"]
    assert wt(attached, task, folder, "rev-parse", "HEAD") == head
    assert wt(attached, task, folder, "rev-parse", "HEAD^") == before
    shown = wt(attached, task, folder, "status", "--porcelain", "--untracked-files=all").split("\n")
    assert shown == (["A  copied-measure.sh"] if stage_adds else ["?? copied-measure.sh"])
    conn = db.connect(str(attached.root))
    try:
        events = [e for e in db.events(conn, limit=10 ** 9) if e["kind"] == "attach-land"]
    finally:
        conn.close()
    assert len(events) == 1 and events[0]["data"]["files"] == ["src/landed.py"]
    assert events[0]["data"]["tree"] == wt(attached, task, folder, "rev-parse", "HEAD^{tree}")


def test_the_patch_file_changing_after_the_check_changes_nothing(attached, tmp_path, monkeypatch):
    """The patch is read once. A writer that swaps the file for a patch that brings the pack file, after the
    check, gets neither the check nor the commit."""
    write_pack(attached)
    task, folder = claimed(attached)
    good = diff_of(tmp_path, {"src/landed.py": "y = 1\n"}, name="good.patch")
    evil = Path(diff_of(tmp_path, {"copied-measure.sh": PACK_FILE, "src/evil.py": "e = 1\n"}, name="evil.patch")).read_bytes()
    real = runbooks._staged_blobs

    def swapping(*args, **kw):
        got = real(*args, **kw)
        Path(good).write_bytes(evil)
        return got

    monkeypatch.setattr(runbooks, "_staged_blobs", swapping)
    before = wt(attached, task, folder, "rev-parse", "HEAD")
    head = runbooks.land(str(attached.root), task, good, message="m")
    assert landed_files(attached, task, folder, before, head) == ["src/landed.py"]
    assert wt(attached, task, folder, "status", "--porcelain", "--untracked-files=all") == ""
    assert not Path(folder, "src", "evil.py").exists() and not Path(folder, "copied-measure.sh").exists()


def test_a_branch_that_moves_between_the_check_and_the_commit_is_refused_and_the_folder_is_put_back(attached, tmp_path, monkeypatch):
    task, folder = claimed(attached)
    real = runbooks._staged_blobs

    def moving(*args, **kw):
        got = real(*args, **kw)
        gitops.in_worktree(str(attached.app), task, folder, "commit", "-q", "--allow-empty", "-m", "someone else", identity=True)
        return got

    monkeypatch.setattr(runbooks, "_staged_blobs", moving)
    before = wt(attached, task, folder, "rev-parse", "HEAD")
    with pytest.raises(AttachError, match="moved while the patch was landing"):
        runbooks.land(str(attached.root), task, diff_of(tmp_path, {"src/landed.py": "y = 1\n"}), message="m")
    after = wt(attached, task, folder, "rev-parse", "HEAD")
    assert after != before and wt(attached, task, folder, "log", "-1", "--format=%s") == "someone else"
    assert wt(attached, task, folder, "status", "--porcelain", "--untracked-files=all") == ""
    assert not Path(folder, "src", "landed.py").exists()
    assert not Path(attached.root, STATE_DIR, "runs", task, "land").exists()


EDIT_MAIN = """diff --git a/src/main.py b/src/main.py
--- a/src/main.py
+++ b/src/main.py
@@ -1 +1 @@
-print('hello')
+print('landed')
"""


@pytest.mark.parametrize("stage_adds", [False, True])
def test_a_file_the_patch_changes_being_edited_after_the_check_commits_nothing(attached, tmp_path, monkeypatch, stage_adds):
    """The patch edits src/main.py; a writer edits the same file (and stages it) after the check. The live apply
    cannot take the patch on top of that, so nothing is committed and the writer's edit is left as it was."""
    task, folder = claimed(attached)
    patch = tmp_path / "edit.patch"
    patch.write_text(EDIT_MAIN)
    real = runbooks._staged_blobs

    def racing(*args, **kw):
        got = real(*args, **kw)
        Path(folder, "src", "main.py").write_text("print('other')\n")
        if stage_adds:
            wt(attached, task, folder, "add", "src/main.py")
        return got

    monkeypatch.setattr(runbooks, "_staged_blobs", racing)
    before = wt(attached, task, folder, "rev-parse", "HEAD")
    with pytest.raises(AttachError, match="git apply failed"):
        runbooks.land(str(attached.root), task, str(patch), message="m")
    assert wt(attached, task, folder, "rev-parse", "HEAD") == before
    assert Path(folder, "src", "main.py").read_text() == "print('other')\n"
    assert wt(attached, task, folder, "status", "--porcelain") == ("M  src/main.py" if stage_adds else " M src/main.py")


def hold(att, task):
    fd = safefs.open_lock(att.root, "%s/locks/%s.lock" % (STATE_DIR, task))
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_a_held_lock_makes_land_and_stage_refuse_and_a_released_one_lets_them_run(attached, tmp_path):
    task, folder = claimed(attached)
    rb = stage_runbook(attached, "true")
    patch = diff_of(tmp_path, {"src/landed.py": "y = 1\n"})
    fd = hold(attached, task)
    try:
        with pytest.raises(AttachError, match="another stage or land of %s is running" % task):
            runbooks.land(str(attached.root), task, patch, message="m")
        with pytest.raises(AttachError, match="another stage or land of %s is running" % task):
            runbooks.run_stage(str(attached.root), task, rb, "s1")
        assert wt(attached, task, folder, "status", "--porcelain", "--untracked-files=all") == ""
    finally:
        os.close(fd)
    assert runbooks.run_stage(str(attached.root), task, rb, "s1")["verdict"] == "PASS"
    assert runbooks.land(str(attached.root), task, patch, message="m")


def test_the_lock_of_one_task_does_not_hold_up_another_task(attached, tmp_path):
    first, second = add_tasks(attached.root, 2)
    for task in (first, second):
        assert cli.main(["task", "claim", task, "--by", "w1"]) == cli.PASS
    fd = hold(attached, first)
    try:
        assert runbooks.land(str(attached.root), second, diff_of(tmp_path, {"src/b.py": "b = 1\n"}), message="m")
    finally:
        os.close(fd)


def test_a_land_during_a_running_stage_is_refused_and_the_stage_is_left_alone(attached, tmp_path):
    """The stage copies the pack file into its tree and runs `git add` while land is attempted. Land is refused at
    once; the stage finishes; no commit holds the pack file."""
    write_pack(attached)
    task, folder = claimed(attached)
    gate = tmp_path / "gate"
    started = tmp_path / "started"
    run = ("touch %s; i=0; while [ ! -e %s ] && [ $i -lt 1500 ]; do i=$((i+1)); sleep 0.02; done; "
           "cp %s copied-measure.sh; git add copied-measure.sh; true" % (started, gate, Path(attached.root, "research", "measure.sh")))
    rb = stage_runbook(attached, run)
    before = wt(attached, task, folder, "rev-parse", "HEAD")
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.update(result=runbooks.run_stage(str(attached.root), task, rb, "s1")))
    thread.start()
    try:
        for _ in range(500):
            if started.exists():
                break
            threading.Event().wait(0.02)
        assert started.exists()
        with pytest.raises(AttachError, match="another stage or land"):
            runbooks.land(str(attached.root), task, diff_of(tmp_path, {"src/r.py": "z = 1\n"}), message="race")
    finally:
        gate.write_text("go\n")
        thread.join()
    assert outcome["result"]["verdict"] == "PASS" and outcome["result"]["files_written"] == ["copied-measure.sh"]
    assert wt(attached, task, folder, "rev-parse", "HEAD") == before


def test_two_lands_at_once_never_share_a_commit(attached, tmp_path):
    """Each of six rounds starts two lands of one task together. A land is refused or it lands alone: no commit holds
    two files, no file of a refused land is in the folder, and the folder ends clean."""
    task, folder = claimed(attached)
    landed = 0
    for n in range(6):
        before = wt(attached, task, folder, "rev-parse", "HEAD")
        outcome = {}
        patches = {k: diff_of(tmp_path, {"src/%s%d.py" % (k, n): "%s = %d\n" % (k, n)}, name="%s%d.patch" % (k, n)) for k in "ab"}

        def go(key):
            try:
                outcome[key] = runbooks.land(str(attached.root), task, patches[key], message=key)
            except AttachError as exc:
                outcome[key] = exc

        threads = [threading.Thread(target=go, args=(k,)) for k in "ab"]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert set(outcome) == {"a", "b"}
        commits = wt(attached, task, folder, "rev-list", "%s..HEAD" % before).split()
        for sha in commits:
            names = wt(attached, task, folder, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).split()
            assert len(names) == 1, (names, outcome)
        assert len(commits) == sum(1 for v in outcome.values() if isinstance(v, str)), outcome
        assert wt(attached, task, folder, "status", "--porcelain", "--untracked-files=all") == "", outcome
        landed += len(commits)
    assert landed >= 6


# ------------------------------------------------------------------ round 3: nothing git re-reads is trusted

def test_land_reads_the_committed_tree_not_the_scratch_index_when_that_file_changes_after_the_apply(attached, tmp_path, monkeypatch):
    """Adversarial R3-1: a writer changes the scratch index (a studio file) after the patch was applied to it and
    before `write-tree`. The checks read the rows of the tree that is committed, so the pack blob is seen."""
    write_pack(attached)
    task, folder = claimed(attached)
    index = os.path.join(str(attached.root), STATE_DIR, "runs", task, "land", "index")
    real = runbooks._run_in

    def racing(app, t, f, *args, **kw):
        if args and args[0] == "write-tree":
            env = gitops.clean_env({"GIT_INDEX_FILE": index})
            blob = gitops.run_in_worktree(app, t, f, "-C", f, "hash-object", "-w", "--stdin", input=PACK_FILE).out.strip()
            gitops.run_in_worktree(app, t, f, "-C", f, "update-index", "--add", "--cacheinfo",
                                   "100644,%s,copied-measure.sh" % blob, env=env)
        return real(app, t, f, *args, **kw)

    monkeypatch.setattr(runbooks, "_run_in", racing)
    before = wt(attached, task, folder, "rev-parse", "HEAD")
    with pytest.raises(AttachError, match="byte for byte"):
        runbooks.land(str(attached.root), task, diff_of(tmp_path, {"src/landed.py": "y\n"}), message="m")
    assert wt(attached, task, folder, "rev-parse", "HEAD") == before


def test_land_refuses_a_protected_path_that_only_the_committed_tree_holds(attached, tmp_path, monkeypatch):
    """Adversarial R3-2: the same writer adds a protected path (not a pack file) to the scratch index. The
    protected-path rule is asked of the committed tree's rows too."""
    task, folder = claimed(attached)
    index = os.path.join(str(attached.root), STATE_DIR, "runs", task, "land", "index")
    real = runbooks._run_in

    def racing(app, t, f, *args, **kw):
        if args and args[0] == "write-tree":
            env = gitops.clean_env({"GIT_INDEX_FILE": index})
            blob = gitops.run_in_worktree(app, t, f, "-C", f, "hash-object", "-w", "--stdin", input="evil\n").out.strip()
            gitops.run_in_worktree(app, t, f, "-C", f, "update-index", "--add", "--cacheinfo",
                                   "100644,%s,RESUME.md" % blob, env=env)
        return real(app, t, f, *args, **kw)

    monkeypatch.setattr(runbooks, "_run_in", racing)
    before = wt(attached, task, folder, "rev-parse", "HEAD")
    with pytest.raises(AttachError, match="never receives"):
        runbooks.land(str(attached.root), task, diff_of(tmp_path, {"src/landed.py": "y\n"}), message="m")
    assert wt(attached, task, folder, "rev-parse", "HEAD") == before


def test_land_leaves_no_copy_of_the_patch_in_the_studio_for_git_to_read_again(attached, tmp_path, monkeypatch):
    """Every git step gets the patch bytes on standard input, so there is no studio file to swap."""
    task, folder = claimed(attached)
    land_dir = Path(attached.root, STATE_DIR, "runs", task, "land")
    seen = []
    real = runbooks._staged_blobs

    def looking(*a, **kw):
        seen.extend(sorted(os.listdir(str(land_dir))))
        return real(*a, **kw)

    monkeypatch.setattr(runbooks, "_staged_blobs", looking)
    runbooks.land(str(attached.root), task, diff_of(tmp_path, {"src/landed.py": "y\n"}), message="m")
    assert "patch.diff" not in seen, seen


@pytest.mark.parametrize("verb", ["land", "stage"])
def test_a_plain_project_called_through_the_api_leaves_no_attach_state(studio, tmp_path, verb):
    """Adversarial R3-3: the lock is taken after the attach check, so a refusal makes no `.alpaca/attach/` folder."""
    root = str(studio)
    assert not os.path.lexists(os.path.join(root, STATE_DIR))
    with pytest.raises(AttachError):
        if verb == "land":
            runbooks.land(root, "t-001", diff_of(tmp_path, {"a.py": "a\n"}), message="m")
        else:
            rb = tmp_path / "rb.yaml"
            rb.write_text("runbook: 1\nid: rb\ntitle: t\nstages: [{id: s1, title: s, run: 'true', checks: [{id: c, type: exit-code}]}]\n")
            runbooks.run_stage(root, "t-001", str(rb), "s1")
    assert not os.path.lexists(os.path.join(root, STATE_DIR))
