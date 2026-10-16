"""The proof code binds an attach task to the record and refuses when it cannot (op-062 t-650, fix round 1).

Round 1 found one class: the seal decided what to seal from inputs an agent can change (the branch file, the
branch the worktree is on, a link in the path, the git config the host or a submodule brings) and, when they
were missing or wrong, sealed the task as a plain one. Every test here is a way to change one of those inputs;
each must end in a refusal, or in a seal that still carries the app diff, never in a plain seal.
"""
import json
import os
import shutil

import pytest

from alpaca import db, proof, util
from alpaca.attach import proofhook
from alpaca.research import verify_gate
from alpaca.tests.attachkit import attached, git, studio, upstream  # noqa: F401  (fixtures)
from alpaca.tests.attachproofkit import commit_in, cut, latest_seal, prepare, record_branch
from alpaca.tests.test_attach_proof import refusal, scaffold_text, seal, task  # noqa: F401


def branch_file(root, name="t-001"):
    return os.path.join(root, ".alpaca", "attach", "branches", "%s.json" % name)


def app_patch(root, payload):
    return open(os.path.join(root, payload["tree"]["app"]["patch"]), encoding="utf-8").read()


def sub_with_filter(wt, marker):
    """A populated submodule in the worktree whose OWN config has a clean filter that makes `marker`."""
    sub = wt / "sub"
    sub.mkdir()
    git(sub, "init", "-q")
    (sub / "f.txt").write_text("a\n")
    (sub / ".gitattributes").write_text("* filter=x\n")
    git(sub, "add", "-A")
    git(sub, "commit", "-q", "-m", "s")
    git(sub, "config", "filter.x.clean", "touch %s; cat" % marker)
    git(wt, "add", "sub")
    git(wt, "commit", "-q", "-m", "add sub")
    with open(sub / "f.txt", "a") as fh:
        fh.write("b\n")


# ------------------------------------------------------------------------- the binding to the record
def test_a_task_with_no_record_event_is_refused_never_sealed_plain(attached):
    root = prepare(attached)
    cut(attached, record=False)
    for verify in (False, True):
        assert "no attach-branch record" in refusal(root, verify=verify)
    assert proofhook.header_line(root, "t-001").startswith("not read (ATTACH-WORKTREE")
    assert latest_seal(root) is None


def test_a_deleted_branch_file_does_not_make_the_seal_plain(task):
    att, root, wt = task
    commit_in(wt, {"src/feature.py": "feature = 1\n"})
    os.remove(branch_file(root))
    payload = seal(root, verify=True)
    assert payload["attach"]["branch"] == "alpaca/t-001" and "src/feature.py" in app_patch(root, payload)


def test_a_branch_file_that_is_a_link_is_refused(task, tmp_path):
    att, root, wt = task
    copy = tmp_path / "t-001.json"
    shutil.copy(branch_file(root), copy)
    os.remove(branch_file(root))
    os.symlink(str(copy), branch_file(root))
    assert "link or cannot be read" in refusal(root, verify=True)
    assert proofhook.header_line(root, "t-001").startswith("not read (")


def test_a_corrupt_branch_file_is_refused(task):
    att, root, wt = task
    open(branch_file(root), "w").write("{not json")
    assert "link or cannot be read" in refusal(root)


@pytest.mark.parametrize("key", ["base_sha", "branch"])
def test_a_branch_file_that_differs_from_the_record_is_refused(task, key):
    att, root, wt = task
    head = commit_in(wt, {"src/feature.py": "feature = 1\n"})
    data = json.load(open(branch_file(root)))
    data[key] = head if key == "base_sha" else "alpaca/other"
    open(branch_file(root), "w").write(json.dumps(data))
    assert "does not match the record" in refusal(root, verify=True)


def test_the_base_of_the_seal_is_the_recorded_one(task):
    att, root, wt = task
    commit_in(wt, {"src/feature.py": "feature = 1\n"})
    payload = seal(root, verify=True)
    base = [e for e in db.events(db.connect(root), kind="attach-branch")][-1]["data"]["base_sha"]
    assert payload["attach"]["base"] == payload["tree"]["app"]["base"] == base


def test_an_invalid_attach_block_refuses_an_attach_task_and_leaves_a_studio_task_alone(task):
    att, root, wt = task
    from alpaca import cli, project as project_mod
    assert cli.main(["task", "add", "--title", "studio", "op-001", "a studio only task", "--phase", "build"]) == 0
    commit_in(wt, {"src/feature.py": "feature = 1\n"})
    path = project_mod.path(root)
    util.write_text(path, util.read_text(path).replace("attach:", "attach:\n  bogus_key_xyz: 1", 1))
    assert "ATTACH-CONFIG" in refusal(root, verify=True)
    assert proofhook.header_line(root, "t-001").startswith("not read (ATTACH-CONFIG")
    assert proofhook.seal_block(root, "t-002") is None and proofhook.header_line(root, "t-002") is None


def test_a_rewritten_record_cannot_be_forged_by_the_branch_file_alone(task):
    """The record holds the base; an agent that rewrites only the file to hide committed work is refused."""
    att, root, wt = task
    head = commit_in(wt, {"src/feature.py": "feature = 1\n"})
    data = json.load(open(branch_file(root)))
    data["base_sha"] = head
    open(branch_file(root), "w").write(json.dumps(data))
    with pytest.raises(proof.Refusal):
        seal(root, verify=True)
    assert latest_seal(root) is None


def test_the_latest_record_event_wins_after_a_resume(task):
    att, root, wt = task
    base = json.load(open(branch_file(root)))["base_sha"]
    record_branch(root, "t-001", branch="alpaca/t-001", worktree=wt, base_sha=base)
    assert seal(root)["attach"]["base"] == base


# -------------------------------------------------------------- the worktree is the task's and on its branch
def test_a_decoy_branch_in_the_worktree_is_refused(task):
    att, root, wt = task
    commit_in(wt, {"src/real.py": "real = 1\n"})
    base = json.load(open(branch_file(root)))["base_sha"]
    git(wt, "switch", "-q", "-c", "decoy", base)
    assert "not on its branch alpaca/t-001" in refusal(root, verify=True)
    assert "on decoy" in proofhook.header_line(root, "t-001")


def test_a_detached_head_is_refused(task):
    att, root, wt = task
    base = json.load(open(branch_file(root)))["base_sha"]
    git(wt, "checkout", "-q", "--detach", base)
    assert "detached HEAD" in refusal(root)


def test_a_link_in_place_of_the_worktree_is_refused(task):
    att, root, wt = task
    other = wt.parent / "t-999"
    base = json.load(open(branch_file(root)))["base_sha"]
    git(att.app, "worktree", "add", "-q", "-b", "alpaca/t-999", str(other), base)
    commit_in(other, {"src/other.py": "other = 1\n"})
    shutil.move(str(wt), str(wt) + ".away")
    os.symlink(str(other), str(wt))
    assert "is a link" in refusal(root, verify=True)
    assert proofhook.header_line(root, "t-001").startswith("not read (")


def test_a_broken_link_in_place_of_the_worktree_is_not_called_gone(task, tmp_path):
    att, root, wt = task
    shutil.move(str(wt), str(wt) + ".away")
    os.symlink(str(tmp_path / "nowhere"), str(wt))
    assert "is a link" in refusal(root)


def test_a_git_file_copied_from_another_worktree_is_refused(task):
    att, root, wt = task
    other = wt.parent / "t-999"
    base = json.load(open(branch_file(root)))["base_sha"]
    git(att.app, "worktree", "add", "-q", "-b", "alpaca/t-999", str(other), base)
    (wt / ".git").write_text((other / ".git").read_text())
    assert "not point back" in refusal(root)


def test_the_worktrees_folder_as_a_link_is_refused(task, tmp_path):
    att, root, wt = task
    folder = wt.parent
    shutil.move(str(folder), str(tmp_path / "away"))
    os.symlink(str(tmp_path / "away"), str(folder))
    assert "is a link" in refusal(root)


# ---------------------------------------------------------------------- one config view for check and run
def test_a_submodule_clean_filter_does_not_run_on_header_plain_seal_or_verify_seal(task, tmp_path):
    att, root, wt = task
    marker = tmp_path / "PWNED"
    sub_with_filter(wt, marker)
    scaffold_text(root)
    assert not marker.exists(), "the header ran the submodule's filter"
    seal(root)
    assert not marker.exists(), "a plain seal ran the submodule's filter"
    # the work adds a submodule entry, which a verification seal refuses (fix round 2, B2); the filter must
    # not have run on the way to that refusal either
    assert "ATTACH-SUBMODULE" in refusal(root, verify=True)
    assert not marker.exists(), "a verification seal ran the submodule's filter"


def test_the_inherited_global_config_filter_does_not_run(task, monkeypatch, tmp_path):
    att, root, wt = task
    marker = tmp_path / "PWNED"
    cfg = tmp_path / "global.cfg"
    cfg.write_text("[filter \"x\"]\n\tclean = touch %s; cat\n" % marker)
    (wt / ".gitattributes").write_text("* filter=x\n")
    (wt / "a.txt").write_text("a\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    seal(root, verify=True)
    scaffold_text(root)
    assert not marker.exists()


def test_a_global_attributes_file_and_a_home_config_do_not_apply_to_the_app_patch(task, monkeypatch, tmp_path):
    """The studio's own tree patch still reads the host's config (that is the host's, not foreign); the
    attached repo's patch must not. The filter notes the folder it ran in."""
    att, root, wt = task
    marker = tmp_path / "RAN"
    home = tmp_path / "home"
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".gitconfig").write_text("[filter \"x\"]\n\tclean = pwd >> %s; cat\n" % marker)
    (home / ".config" / "git" / "attributes").write_text("* filter=x\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    (wt / "a.txt").write_text("a\n")
    seal(root, verify=True)
    scaffold_text(root)
    ran_in = marker.read_text() if marker.exists() else ""
    assert os.path.join("worktrees", "t-001") not in ran_in, ran_in


def test_take_tree_keeps_its_argv_when_not_asked(project, monkeypatch):
    """A project without attach: the new keywords default to what take_tree always did."""
    git(project, "init", "-q")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "x")
    seen = []
    real = verify_gate.subprocess.run

    def spy(argv, **kw):
        seen.append(tuple(argv))
        return real(argv, **kw)
    monkeypatch.setattr(verify_gate.subprocess, "run", spy)
    verify_gate.take_tree(str(project))
    head = ("git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")
    assert seen and all(a[:5] == head and a[5] not in ("-c",) for a in seen), seen
    add = [a for a in seen if "add" in a][0]
    assert add[5:] == ("add", "-A", "--", ".", ":(exclude).alpaca")


# ----------------------------------------------------------------------- no write through a planted link
def test_a_link_at_the_seal_index_copy_is_not_written_through(task, tmp_path):
    att, root, wt = task
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n")
    gitdir = git(wt, "rev-parse", "--absolute-git-dir")
    os.symlink(str(victim), os.path.join(gitdir, "alpaca-seal-index.%d" % os.getpid()))
    (wt / "a.txt").write_text("a\n")
    with pytest.raises(proof.Refusal) as exc:
        seal(root, verify=True)
    assert "index copy" in str(exc.value.problems)
    assert victim.read_bytes() == b"precious\n"
    assert os.path.islink(os.path.join(gitdir, "alpaca-seal-index.%d" % os.getpid()))   # not even removed


def test_a_stale_index_copy_is_refused_and_left_alone(task):
    att, root, wt = task
    gitdir = git(wt, "rev-parse", "--absolute-git-dir")
    stale = os.path.join(gitdir, "alpaca-seal-index.%d" % os.getpid())
    open(stale, "wb").write(b"old")
    with pytest.raises(proof.Refusal):
        seal(root, verify=True)
    assert open(stale, "rb").read() == b"old"


# ----------------------------------------------------------------------------- one head per seal
def test_a_commit_between_the_diff_and_the_block_refuses_the_seal(task, monkeypatch):
    att, root, wt = task
    def late(root_, task_, conn=None):
        commit_in(wt, {"src/late.py": "late = 1\n"})
        return orig(root_, task_, conn)
    orig = proofhook.seal_block
    monkeypatch.setattr(proofhook, "seal_block", late)
    with pytest.raises(proof.Refusal) as exc:
        seal(root, verify=True)
    assert "moved during the seal" in str(exc.value.problems)
    assert latest_seal(root) is None


def test_a_commit_while_the_patch_is_taken_is_refused(task, monkeypatch):
    att, root, wt = task
    orig = verify_gate.take_tree

    def late(*a, **kw):
        got = orig(*a, **kw)
        if kw.get("skip_gitlinks"):
            commit_in(wt, {"src/late.py": "late = 1\n"})
        return got
    monkeypatch.setattr(verify_gate, "take_tree", late)
    with pytest.raises(ValueError, match="moved while its diff was taken"):
        proofhook.take_app_patch(root, "t-001")


# ---------------------------------------------------------------- a git failure is a refusal, not a crash
def test_a_git_failure_inside_the_hook_is_a_refusal(task, monkeypatch):
    from alpaca.attach import AttachError, gitops
    att, root, wt = task
    real = gitops.run

    def broken(repo, *args, **kw):
        if args and args[0] == "rev-parse":
            raise AttachError("git rev-parse ran out of time after 120s")
        return real(repo, *args, **kw)
    monkeypatch.setattr(gitops, "run", broken)
    assert "ran out of time" in refusal(root)
    assert "ran out of time" in refusal(root, verify=True)
    assert proofhook.header_line(root, "t-001").startswith("not read (")


def test_an_unreadable_record_refuses_an_attach_task(task, monkeypatch):
    att, root, wt = task
    import sqlite3

    def boom(conn, task_):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(proofhook, "_recorded", boom)
    assert "record could not be read" in refusal(root)


# ------------------------------------------- op-062 integration of t-650 with t-648: git in the work folder
def test_an_admin_folder_that_no_longer_points_back_is_refused_by_prove_worktree(task):
    """The gitfile names the task's admin folder, but the admin folder's gitdir names another place:
    t-648's `prove_worktree` refuses it, and the seal says so."""
    from alpaca.attach import gitops
    att, root, wt = task
    with open(os.path.join(gitops.admin_dir(str(att.app), "t-001"), "gitdir"), "w") as fh:
        fh.write("/elsewhere/.git\n")
    msg = refusal(root, verify=True)
    assert "not sound" in msg and "admin folder names" in msg, msg
    assert latest_seal(root) is None


def test_no_git_runs_in_the_work_folder_by_its_own_git_file(task, monkeypatch):
    """Every git the hook starts in the work folder names the task's admin folder: on the command line
    (`gitops.run_in_worktree`) or, for the staging copy, in GIT_DIR and GIT_WORK_TREE."""
    from alpaca.attach import gitops
    att, root, wt = task
    commit_in(wt, {"src/x.py": "x = 1\n"})
    admin, wt_real = gitops.admin_dir(str(att.app), "t-001"), os.path.realpath(str(wt))
    argvs, envs = [], []
    real_exec, real_take = gitops._exec, verify_gate.take_tree

    def spy_exec(argv, *a, **kw):
        argvs.append(list(argv))
        return real_exec(argv, *a, **kw)

    def spy_take(where, **kw):
        envs.append((where, kw.get("env") or {}))
        return real_take(where, **kw)
    monkeypatch.setattr(gitops, "_exec", spy_exec)
    monkeypatch.setattr(verify_gate, "take_tree", spy_take)
    payload = seal(root, verify=True)
    assert payload["tree"]["app"]["bytes"] > 0
    by_gitfile = [a for a in argvs if "-C" in a and a[a.index("-C") + 1] == wt_real
                  and not any(x.startswith("--git-dir=") for x in a)]
    assert not by_gitfile, by_gitfile
    app_takes = [env for where, env in envs if os.path.realpath(str(where)) == wt_real]
    assert app_takes and all(env.get("GIT_DIR") == admin and env.get("GIT_WORK_TREE") == wt_real
                             for env in app_takes), app_takes
