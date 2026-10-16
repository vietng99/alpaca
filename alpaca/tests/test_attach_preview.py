"""The merge preview (AC-15, R14) and the pre-mortem attacks of t-649 that concern it (build plan 6.3, P12)."""
import os
import sys
from pathlib import Path

import pytest

from alpaca.attach import AttachError, baseline, check, config, safefs, worktree
from alpaca.tests.attachkit import (AttachedStudio, add_worktree, attached, commit_in, git, set_gates, studio,  # noqa: F401
                                    upstream)

TASK = "t-001"
AGENT = {"GIT_AUTHOR_NAME": "Alpaca agent", "GIT_AUTHOR_EMAIL": "alpaca-agent@localhost",
         "GIT_COMMITTER_NAME": "Alpaca agent", "GIT_COMMITTER_EMAIL": "alpaca-agent@localhost"}


def py_gate(name, code, timeout=60):
    return {"name": name, "command": [sys.executable, "-B", "-c", code], "timeout": timeout}


#: imports every module of the tree: red when one module cannot import another
IMPORT_ALL = ("import glob, importlib, sys; sys.path.insert(0, '.'); "
              "[importlib.import_module(f[:-3]) for f in sorted(glob.glob('*.py'))]")
GATES = [py_gate("build", "import sys; sys.exit(0)"), py_gate("test", "import os, sys; sys.exit(1 if os.path.exists('broken') else 0)")]


def up_commit_bytes(up, files, message="binary change"):
    for rel, data in files.items():
        target = up.work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    git(up.work, "add", "-A")
    git(up.work, "commit", "-q", "-m", message)
    git(up.work, "push", "-q", "origin", up.branch)


def up_remove(up, rel, message="upstream removes a file"):
    git(up.work, "rm", "-q", rel)
    git(up.work, "commit", "-q", "-m", message)
    git(up.work, "push", "-q", "origin", up.branch)


def attach_with(studio, upstream, files=None, binary=None, gates=GATES):
    """Upstream gets `files` (text) and `binary` ({path: bytes}) first, then the studio attaches it."""
    from alpaca.attach import repo
    if files:
        upstream.commit(files, "base files")
    if binary:
        up_commit_bytes(upstream, binary, "base binary")
    cfg = repo.attach(str(studio), upstream.url)
    att = AttachedStudio(studio, upstream, cfg)
    set_gates(att.root, list(gates))
    return att


def snapshot(att):
    """Everything a merge preview must leave alone in the attached clone."""
    app = att.app
    admin = app / ".git"
    return {"head": git(app, "rev-parse", "HEAD"), "branch": git(app, "symbolic-ref", "HEAD"),
            "worktrees": git(app, "worktree", "list", "--porcelain"),
            "refs": git(app, "for-each-ref", "--format=%(refname) %(objectname)"),
            "merge_head": (admin / "MERGE_HEAD").exists(), "rr_cache": (admin / "rr-cache").exists(),
            "admin": sorted(os.listdir(str(admin / "worktrees"))), "status": git(app, "status", "--porcelain", "--ignored"),
            "config": git(app, "config", "--local", "--list"), "orig_head": (admin / "ORIG_HEAD").exists()}


def residue(att):
    base = att.root / ".alpaca/attach"
    return [p.name for p in base.iterdir() if p.name.startswith(("preview", "seed"))] + \
        (list((base / "runs" / TASK).iterdir()) if (base / "runs" / TASK).exists() else [])


# ---- AC-15: conflicts fail and name their files ------------------------------------------------------------------------

def conflict(att, kind):
    """Cut the work branch, commit a change on it, commit a clashing change upstream; returns the paths git must name."""
    wt = add_worktree(att, TASK)
    if kind == "content":
        commit_in(wt, {"src/main.py": "print('branch')\n"}, "branch edit")
        att.upstream.commit({"src/main.py": "print('upstream')\n"}, "upstream edit")
        return ["src/main.py"]
    if kind == "add-add":
        commit_in(wt, {"docs/new.md": "from the branch\n"}, "branch adds")
        att.upstream.commit({"docs/new.md": "from upstream\n"}, "upstream adds")
        return ["docs/new.md"]
    if kind == "binary":
        commit_in(wt, {"logo.bin": b"\x00\x01branch"}, "branch changes the image")
        up_commit_bytes(att.upstream, {"logo.bin": b"\x00\x02upstream"})
        return ["logo.bin"]
    if kind == "rename-delete":
        git(wt, "mv", "src/main.py", "src/app.py")
        git(wt, "commit", "-q", "-m", "branch renames", env=AGENT)
        up_remove(att.upstream, "src/main.py")
        return ["src/app.py"]
    if kind == "modify-delete":
        commit_in(wt, {"src/main.py": "print('branch')\n"}, "branch edit")
        up_remove(att.upstream, "src/main.py")
        return ["src/main.py"]
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["content", "rename-delete", "binary", "add-add", "modify-delete"])
def test_a_conflict_fails_the_check_and_names_the_files(studio, upstream, kind):
    """AC-15."""
    att = attach_with(studio, upstream, binary={"logo.bin": b"\x00\x01base"} if kind == "binary" else None)
    files = conflict(att, kind)
    res = check.run(str(att.root), TASK)
    assert res.verdict == "FAIL"
    assert set(files) <= set(res.conflicts), (res.conflicts, files)
    assert res.preview_results == [] and res.regressions == []                       # the preview gates are not run
    assert [g["passed"] for g in res.head_results] == [True, True]                  # the branch alone is green
    assert res.reason.startswith("merge conflict in %d file" % len(res.conflicts))
    assert check.latest(str(att.root), TASK).conflicts == res.conflicts


def test_the_verb_names_the_conflicted_files(studio, upstream, capsys):
    from alpaca import cli
    att = attach_with(studio, upstream)
    conflict(att, "content")
    capsys.readouterr()
    assert cli.main(["attach", "check", TASK]) == cli.FAIL
    out = capsys.readouterr().out
    assert "conflict: src/main.py" in out and "preview not run" in out and "preview base" in out
    assert out.rstrip().endswith("GATE alpaca-attach-check: FAIL (merge conflict in 1 file)")


def test_the_merge_takes_the_verified_shas_not_the_seed_refs(studio, upstream, monkeypatch):
    """merge-tree merges the two commits the check verified, not whatever `refs/preview/*` names when git reads
    them (adversarial round 3, P3a). The spy moves `refs/preview/head` to the base commit just before merge-tree
    runs: merged by name that is base into base, which is clean; merged by sha the conflict stays named."""
    att = attach_with(studio, upstream)
    conflict(att, "content")
    real = check.gitops.run
    moved = []

    def swap_then_run(repo, *args, **kw):
        if "merge-tree" in args:
            base_sha = check.gitops.git(repo, "rev-parse", "--verify", "%s^{commit}" % baseline.BASE_REF)
            check.gitops.git(repo, "update-ref", check.HEAD_REF, base_sha)
            moved.append(check.gitops.git(repo, "rev-parse", "--verify", check.HEAD_REF) == base_sha)
        return real(repo, *args, **kw)

    monkeypatch.setattr(check.gitops, "run", swap_then_run)
    res = check.run(str(att.root), TASK)
    assert moved == [True]                                         # the swap happened, once, before the merge
    assert res.verdict == "FAIL", (res.verdict, res.reason)        # a refusal (ERROR) is not this result
    assert "src/main.py" in res.conflicts and res.reason.startswith("merge conflict")


def test_a_clean_merge_of_other_files_is_not_a_conflict(attached):
    set_gates(attached.root, list(GATES))
    wt = add_worktree(attached, TASK)
    commit_in(wt, {"src/branch.py": "x = 1\n"}, "branch adds a file")
    attached.upstream.commit({"src/other.py": "y = 2\n"}, "upstream adds another")
    res = check.run(str(attached.root), TASK)
    assert res.verdict == "PASS" and res.conflicts == [] and res.preview_base == attached.upstream.sha()
    assert res.preview_base != res.base_sha


# ---- pre-mortem t-649.1: the preview gates test the merged tree --------------------------------------------------------

def test_the_preview_gates_see_the_merged_tree(attached):
    """Green on the branch alone, red only after the merge: the gate clone must hold both sides' files."""
    attached.upstream.commit({"a.py": "def helper():\n    return 1\n"}, "base helper")
    set_gates(attached.root, [py_gate("test", IMPORT_ALL)])
    wt = add_worktree(attached, TASK)
    commit_in(wt, {"a.py": "def helper2():\n    return 1\n"}, "rename helper to helper2")
    attached.upstream.commit({"b.py": "from a import helper\n"}, "upstream starts to use helper")
    res = check.run(str(attached.root), TASK)
    assert res.conflicts == []
    assert res.head_results[0]["passed"] is True                                    # the branch alone: no b.py
    assert res.preview_results[0]["passed"] is False and "ImportError" in res.preview_results[0]["log_tail"]
    assert res.verdict == "FAIL" and [(r["gate"], r["at"]) for r in res.regressions] == [("test", "preview")]


def warm_the_target_baseline(att):
    """Take the baseline of the newest upstream commit now, so a spy gate counts only the check's own runs."""
    cfg = config.load(str(att.root))
    baseline.ensure(str(att.root), worktree.fetch_base(str(att.root), cfg))


def test_the_preview_gate_clone_holds_the_files_of_both_sides(attached, tmp_path):
    seen = tmp_path / "seen"
    spy = py_gate("test", "import os; open(%r, 'a').write(','.join(sorted(f for f in os.listdir('.') if f.endswith('.py'))) + '\\n')"
                  % str(seen))
    set_gates(attached.root, [spy])
    wt = add_worktree(attached, TASK)
    commit_in(wt, {"branch_only.py": "x = 1\n"}, "branch file")
    attached.upstream.commit({"upstream_only.py": "y = 2\n"}, "upstream file")
    warm_the_target_baseline(attached)                      # the baseline at the moved upstream also runs the gate
    base_runs = len(seen.read_text().split()) if seen.exists() else 0
    check.run(str(attached.root), TASK)
    head_run, preview_run = seen.read_text().split()[base_runs:]
    assert head_run == "branch_only.py" and preview_run == "branch_only.py,upstream_only.py"


def test_the_preview_is_a_committed_merge_with_two_parents(attached):
    set_gates(attached.root, list(GATES))
    wt = add_worktree(attached, TASK)
    head = commit_in(wt, {"src/branch.py": "x = 1\n"}, "branch file")
    tip = attached.upstream.commit({"src/other.py": "y = 2\n"}, "upstream file")
    base = worktree.fetch_base(str(attached.root), config.load(str(attached.root)))
    assert base == tip
    seed = safefs.make_dir(str(attached.root), ".alpaca/attach/runs/probe/seed")
    got, conflicts = check.merge_preview(str(attached.app), seed, branch="alpaca/%s" % TASK, base_ref="refs/remotes/origin/main")
    try:
        assert conflicts == [] and got == Path(seed)
        merged = git(seed, "rev-parse", "HEAD")
        assert git(seed, "rev-list", "--parents", "-n1", merged).split() == [merged, tip, head]
        assert git(seed, "log", "-1", "--format=%an <%ae>", merged) == "Alpaca agent <alpaca-agent@localhost>"
        assert (Path(seed) / "src/branch.py").is_file() and (Path(seed) / "src/other.py").is_file()
        assert git(seed, "status", "--porcelain") == ""
    finally:
        safefs.remove_tree(str(attached.root), ".alpaca/attach/runs/probe")


# ---- pre-mortem t-649.2: merge drivers, attributes and rerere do not hide a conflict -----------------------------------

def driver_environment(monkeypatch, tmp_path):
    """A user-level git config (HOME) and process-level config that resolve every conflict silently."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[merge \"ours\"]\n\tdriver = true\n[rerere]\n\tenabled = true\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "merge.ours.driver")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "rerere.enabled")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "true")


@pytest.mark.parametrize("attribute", ["* merge=ours", "* merge=union", "*.py merge=ours\n* merge=union"])
def test_the_preview_ignores_merge_drivers_attributes_and_rerere(studio, upstream, attribute, monkeypatch, tmp_path):
    """A `.gitattributes` in the base tree (or on the branch) that says `merge=ours` or `merge=union`, with a driver
    configured at user, process and repository level, must not turn a content conflict into a clean merge."""
    att = attach_with(studio, upstream, files={".gitattributes": attribute + "\n"})
    driver_environment(monkeypatch, tmp_path)
    wt = add_worktree(att, TASK)
    commit_in(wt, {"src/main.py": "print('branch')\n", ".gitattributes": "* merge=union\n"}, "branch edit")
    att.upstream.commit({"src/main.py": "print('upstream')\n"}, "upstream edit")
    res = check.run(str(att.root), TASK)
    assert res.verdict == "FAIL" and "src/main.py" in res.conflicts


def test_the_merge_preview_ignores_the_clones_own_driver_and_rerere_config(attached):
    """Called on its own (the clone's git config is otherwise guarded by the P8 digest): drivers and rerere
    configured in app/.git/config do not reach the scratch seed."""
    set_gates(attached.root, list(GATES))
    wt = add_worktree(attached, TASK)
    commit_in(wt, {"src/main.py": "print('branch')\n", ".gitattributes": "* merge=ours\n"}, "branch edit")
    attached.upstream.commit({"src/main.py": "print('upstream')\n"}, "upstream edit")
    git(attached.app, "config", "merge.ours.driver", "true")
    git(attached.app, "config", "rerere.enabled", "true")
    git(attached.app, "config", "rerere.autoupdate", "true")
    git(attached.app, "fetch", "-q", "--no-tags", attached.upstream.url, "+refs/heads/main:refs/remotes/origin/main")
    seed = safefs.make_dir(str(attached.root), ".alpaca/attach/runs/probe/seed")
    try:
        got, conflicts = check.merge_preview(str(attached.app), seed, branch="alpaca/%s" % TASK,
                                             base_ref="refs/remotes/origin/main")
        assert got is None and conflicts == ["src/main.py"]
        assert not (attached.app / ".git" / "rr-cache").exists() and not (Path(seed) / ".git" / "rr-cache").exists()
    finally:
        safefs.remove_tree(str(attached.root), ".alpaca/attach/runs/probe")


def test_a_rewritten_clone_config_stops_the_check_before_the_preview(attached):
    """P8: the same edit through the front door is refused outright."""
    set_gates(attached.root, list(GATES))
    add_worktree(attached, TASK)
    git(attached.app, "config", "merge.ours.driver", "true")
    with pytest.raises(AttachError, match="app git config changed"):
        check.run(str(attached.root), TASK)


# ---- pre-mortem t-649.3: no residue, whatever the outcome --------------------------------------------------------------

def arrange(att, outcome):
    """A branch whose check ends as `outcome`; returns nothing."""
    if outcome == "pass":
        wt = add_worktree(att, TASK)
        commit_in(wt, {"src/branch.py": "x = 1\n"}, "branch file")
    elif outcome == "gate-fail":
        wt = add_worktree(att, TASK)
        commit_in(wt, {"broken": "x\n"}, "break the test")
    elif outcome == "conflict":
        conflict(att, "content")
    elif outcome == "timeout":
        set_gates(att.root, [py_gate("test", "import os, time; os.path.exists('slow') and time.sleep(60)", timeout=2)])
        wt = add_worktree(att, TASK)
        commit_in(wt, {"slow": "x\n"}, "make the test slow")
    else:
        raise AssertionError(outcome)


@pytest.mark.parametrize("outcome", ["pass", "gate-fail", "conflict", "timeout"])
def test_a_check_leaves_no_residue_in_the_clone_or_the_studio(attached, outcome):
    set_gates(attached.root, list(GATES))
    arrange(attached, outcome)
    worktree.fetch_base(str(attached.root), config.load(str(attached.root)))         # the one ref a check moves
    before = snapshot(attached)
    res = check.run(str(attached.root), TASK)
    assert res.verdict == ("PASS" if outcome == "pass" else "FAIL")
    assert snapshot(attached) == before
    assert not (attached.root / ".alpaca/attach/runs" / TASK).exists() and residue(attached) == []
    assert not (attached.app / ".git" / "MERGE_HEAD").exists()
    assert git(attached.app, "worktree", "list", "--porcelain").count("worktree ") == 2     # app and the task folder


def test_a_check_recovers_after_a_killed_previous_run(attached):
    set_gates(attached.root, list(GATES))
    wt = add_worktree(attached, TASK)
    commit_in(wt, {"src/branch.py": "x = 1\n"}, "branch file")
    stale = attached.root / ".alpaca/attach/runs" / TASK / "20260101T000000Z-deadbeef"
    (stale / "seed-preview").mkdir(parents=True)
    git(stale / "seed-preview", "init", "-q")
    (stale / "work-head").mkdir()
    (stale / "work-head" / "gate-0.log").write_text("left by a killed run\n", encoding="utf-8")
    res = check.run(str(attached.root), TASK)
    assert res.verdict == "PASS"
    assert not (attached.root / ".alpaca/attach/runs" / TASK).exists()


def test_an_interrupted_check_cleans_its_run_folder(attached, monkeypatch):
    set_gates(attached.root, list(GATES))
    wt = add_worktree(attached, TASK)
    commit_in(wt, {"src/branch.py": "x = 1\n"}, "branch file")
    real = baseline.run_gate_set_each
    calls = []

    def interrupted(cfg, seed, work, progress):
        calls.append(seed)
        if len(calls) == 2:                                   # the preview run
            raise KeyboardInterrupt
        return real(cfg, seed, work, progress)
    monkeypatch.setattr(baseline, "run_gate_set_each", interrupted)
    with pytest.raises(KeyboardInterrupt):
        check.run(str(attached.root), TASK)
    assert not (attached.root / ".alpaca/attach/runs" / TASK).exists()
    from alpaca import db
    conn = db.connect(str(attached.root))
    try:
        assert [e for e in db.events(conn, limit=10 ** 9) if e["kind"] == "attach-check"] == []
    finally:
        conn.close()


def test_the_preview_registers_nothing_in_the_clone_while_it_runs(attached, tmp_path):
    """During the preview gates the clone lists the task folder only (no preview worktree), and has no MERGE_HEAD."""
    out = tmp_path / "during"
    spy = py_gate("test", "import subprocess, sys; open(%r, 'a').write(subprocess.run(['git', '-C', %r, 'worktree', 'list'], "
                  "capture_output=True, text=True).stdout + '--\\n')" % (str(out), str(attached.app)))
    set_gates(attached.root, [spy])
    wt = add_worktree(attached, TASK)
    commit_in(wt, {"src/branch.py": "x = 1\n"}, "branch file")
    attached.upstream.commit({"src/other.py": "y = 2\n"}, "upstream file")
    warm_the_target_baseline(attached)                      # the baseline at the moved upstream also runs the gate
    runs_before = out.read_text().count("--") if out.exists() else 0
    check.run(str(attached.root), TASK)
    listings = out.read_text().split("--\n")[runs_before:-1]
    assert len(listings) == 2
    for text in listings:
        assert len([line for line in text.splitlines() if line.strip()]) == 2
        assert ".alpaca/attach/runs" not in text
    assert not (attached.app / ".git" / "MERGE_HEAD").exists()


# ---- the function itself -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("branch,base_ref", [("../x", "refs/remotes/origin/main"), ("a b", "refs/remotes/origin/main"),
                                               ("alpaca/t-001", "main"), ("alpaca/t-001", "refs/../x"), ("x.lock", "refs/x"),
                                               ("-x", "refs/x"), ("alpaca/t-001/", "refs/x")])
def test_merge_preview_refuses_a_bad_name(attached, branch, base_ref):
    seed = safefs.make_dir(str(attached.root), ".alpaca/attach/runs/probe/seed")
    try:
        with pytest.raises(AttachError):
            check.merge_preview(str(attached.app), seed, branch=branch, base_ref=base_ref)
    finally:
        safefs.remove_tree(str(attached.root), ".alpaca/attach/runs/probe")


def test_merge_preview_refuses_a_sha_that_is_not_the_ref(attached):
    set_gates(attached.root, list(GATES))
    wt = add_worktree(attached, TASK)
    commit_in(wt, {"src/branch.py": "x = 1\n"}, "branch file")
    seed = safefs.make_dir(str(attached.root), ".alpaca/attach/runs/probe/seed")
    try:
        with pytest.raises(AttachError, match="not the expected"):
            check.merge_preview(str(attached.app), seed, branch="alpaca/%s" % TASK, base_ref="refs/remotes/origin/main",
                                head="0" * 40)
    finally:
        safefs.remove_tree(str(attached.root), ".alpaca/attach/runs/probe")


def test_judge_marks_a_gate_without_a_baseline_entry_as_a_regression():
    base = [{"name": "a", "passed": True}]
    head = [{"name": "a", "passed": True, "returncode": 0, "timed_out": False},
            {"name": "new", "passed": False, "returncode": 2, "timed_out": False}]
    verdict, regressions, red, reason = check.judge(base, head, [], [])
    assert verdict == "FAIL" and [r["gate"] for r in regressions] == ["new"] and red == []
