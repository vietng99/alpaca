"""Doctor in attach mode (AC-02) and its silence for a plain project (R11, PM X5)."""
import os
import shutil
import sys

import pytest

from alpaca import doctor, project as project_mod
from alpaca.attach import config, gitops, repo
from alpaca.tests.attachkit import REMOVE, attached, git, make_upstream, set_attach, studio, upstream, write_attach  # noqa: F401
from alpaca.tests.test_doctor import _full

ATTACH_ROWS = ("attach-config", "attach-repo", "attach-upstream", "attach-git-config", "attach-worktrees", "attach-push-checks")


def rows(root):
    return {c["name"]: c for c in doctor.checks(str(root))}


def good_block(up, **over):
    base = {"repo": "app", "url": up.url, "upstream": "origin", "base": "main", "branch_prefix": "alpaca/",
            "gates": [{"name": "test", "command": ["make", "test"]}]}
    base.update(over)
    return base


def test_doctor_names_the_mode_and_every_attach_row_is_ok(attached):
    r = rows(attached.root)
    for name in ATTACH_ROWS:
        assert r[name]["level"] == "ok", r[name]
    assert r["attach-config"]["detail"].startswith("attach mode on: repo app/, upstream origin/main")
    assert "pre-push hook absent" in r["attach-push-checks"]["detail"]


def test_a_bad_attach_block_is_a_doctor_error_not_a_silent_mode(studio, upstream):
    for value in (None, {}, [], "app"):
        set_attach(studio, value)
        r = rows(studio)
        assert r["attach-config"]["level"] == "error", value
        assert config.mode(str(studio)) is None


BAD_REPOS = [".", "..", "/tmp/x", ".alpaca/app", "app/../app", "link-out", "gitfile-out", "alternates-out", "no-git", "missing",
             "shallow", "core-worktree", "alpaca-project", "gitlink", "commondir-out", "objects-link-out", "refs-link-out",
             "not-ignored"]


@pytest.mark.parametrize("case", BAD_REPOS)
def test_doctor_refuses_a_bad_attach_repo(studio, upstream, tmp_path, case):
    outside = tmp_path / "outside"
    outside.mkdir()
    if case in (".", "..", "/tmp/x", ".alpaca/app", "app/../app"):
        write_attach(studio, **good_block(upstream, repo=case))
    elif case == "missing":
        write_attach(studio, **good_block(upstream))
    elif case == "link-out":
        os.symlink(outside, studio / "app")
        write_attach(studio, **good_block(upstream))
    elif case == "no-git":
        (studio / "app").mkdir()
        (studio / "app" / "Makefile").write_text("test:\n\techo\n")
        write_attach(studio, **good_block(upstream))
    elif case == "gitfile-out":
        (studio / "app").mkdir()
        (studio / "app" / ".git").write_text("gitdir: %s\n" % outside)
        write_attach(studio, **good_block(upstream))
    elif case == "shallow":
        git(tmp_path, "clone", "-q", "--depth", "1", "file://" + upstream.url, str(studio / "app"))
        write_attach(studio, **good_block(upstream))
    else:
        repo.attach(str(studio), upstream.url)
        app = studio / "app"
        if case == "alternates-out":
            (app / ".git" / "objects" / "info").mkdir(exist_ok=True)
            (app / ".git" / "objects" / "info" / "alternates").write_text(str(outside) + "\n")
        elif case == "core-worktree":
            git(app, "config", "--local", "core.worktree", str(outside))
        elif case == "alpaca-project":
            (app / "ALPACA-MANIFEST").write_text("x\n")
        elif case == "gitlink":
            git(studio, "update-index", "--add", "--cacheinfo", "160000,%s,app" % upstream.sha())
        elif case == "commondir-out":
            store = tmp_path / "outside.git"
            shutil.copytree(app / ".git", store)
            for name in ("objects", "refs", "packed-refs", "config"):
                path = app / ".git" / name
                shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)
            (app / ".git" / "commondir").write_text(str(store) + "\n")
            assert git(app, "rev-parse", "--git-common-dir", check=False).startswith(str(store))
        elif case in ("objects-link-out", "refs-link-out"):
            name = case.split("-")[0]
            shutil.move(str(app / ".git" / name), str(outside / name))
            (app / ".git" / name).symlink_to(outside / name)
        elif case == "not-ignored":
            (studio / ".git" / "info" / "exclude").write_text("")
    r = rows(studio)
    assert r["attach-repo"]["level"] == "error", r["attach-repo"]
    # a git row never runs against a folder that is not the clone: git -C app would answer for the studio repository
    assert "attach-upstream" not in r and "attach-git-config" not in r and "attach-worktrees" not in r


def test_a_plain_folder_is_not_mistaken_for_the_studio_repository(studio, upstream):
    (studio / "app").mkdir()
    write_attach(studio, **good_block(upstream))
    detail = rows(studio)["attach-repo"]["detail"]
    assert "is not a git repository of its own" in detail


def test_missing_upstream_remote_and_base_are_errors(attached):
    git(attached.app, "update-ref", "-d", "refs/remotes/origin/main")
    r = rows(attached.root)
    assert r["attach-upstream"]["level"] == "error" and "never fetched" in r["attach-upstream"]["detail"]
    git(attached.app, "remote", "remove", "origin")
    assert "does not exist" in rows(attached.root)["attach-upstream"]["detail"]


@pytest.mark.parametrize("key,value", [("remote.origin.pushurl", "https://example.org/x.git"), ("core.hooksPath", ".githooks"),
                                       ("merge.ours.driver", "true"), ("credential.helper", "store"),
                                       ("url.https://h/.insteadOf", "gh:"), ("filter.x.clean", "cat")])
def test_a_changed_git_config_is_reported(attached, key, value):
    git(attached.app, "config", "--local", key, value)
    r = rows(attached.root)["attach-git-config"]
    assert r["level"] == "error" and "changed since attach" in r["detail"]


def test_removing_the_push_trap_is_a_drift_too(attached):
    git(attached.app, "config", "--local", "--unset", "remote.origin.pushurl")
    assert rows(attached.root)["attach-git-config"]["level"] == "error"


def test_doctor_reports_the_pushinsteadof_entries_next_to_the_pushurl_trap(attached):
    r = rows(attached.root)["attach-git-config"]
    assert r["level"] == "ok" and "pushurl" in r["detail"] and "pushInsteadOf" in r["detail"]
    assert "%d of its spellings" % len(repo.push_rewrites(str(attached.app))) in r["detail"]


def test_the_push_checks_row_names_pushinsteadof_only_when_the_entries_are_present(attached, monkeypatch):
    from alpaca.attach import proven
    assert "pushurl and pushInsteadOf block" in rows(attached.root)["attach-push-checks"]["detail"]
    app = str(attached.app)
    git(attached.app, "config", "--local", "--unset-all", repo.PUSH_REWRITE_KEY)
    monkeypatch.setattr(proven, "init_facts", lambda root, cfg: {"config_digest": gitops.config_digest(app)})
    r = rows(attached.root)
    detail = r["attach-push-checks"]["detail"]
    assert "pushurl block" in detail and "pushInsteadOf" not in detail and r["attach-git-config"]["level"] == "warn"


def test_removing_the_pushinsteadof_entries_is_a_drift(attached):
    git(attached.app, "config", "--local", "--unset-all", repo.PUSH_REWRITE_KEY)
    r = rows(attached.root)["attach-git-config"]
    assert r["level"] == "error" and "changed since attach" in r["detail"]


def test_an_attach_made_without_the_entries_is_a_warning_that_names_them(attached, monkeypatch):
    """An attach made before the entries existed has a digest taken without them, so the digest still matches."""
    from alpaca.attach import proven
    app = str(attached.app)
    git(attached.app, "config", "--local", "--unset-all", repo.PUSH_REWRITE_KEY)
    monkeypatch.setattr(proven, "init_facts", lambda root, cfg: {"config_digest": gitops.config_digest(app)})
    r = rows(attached.root)["attach-git-config"]
    assert r["level"] == "warn" and repo.PUSH_REWRITE_KEY in r["detail"] and "has no" in r["detail"]
    assert "attached before t-697" in r["detail"] and "remote.origin.pushurl trap still holds" in r["detail"]
    assert "A new studio gets the entries" in r["detail"] and "Attach the repository again" not in r["detail"]


def test_a_missing_state_file_is_a_warning(attached):
    os.unlink(config.state_dir(str(attached.root)) / "state.json")
    assert rows(attached.root)["attach-git-config"]["level"] == "warn"


def add_task_worktree(attached, task="t-001"):
    wt = config.state_dir(str(attached.root)) / "worktrees" / task
    gitops.git(str(attached.app), "worktree", "add", "-q", "-b", "alpaca/%s" % task, str(wt), "origin/main")
    config.write_json_atomic(config.state_dir(str(attached.root)) / "branches" / ("%s.json" % task),
                             {"schema": 1, "task": task, "branch": "alpaca/%s" % task, "worktree": str(wt)})
    return wt


def test_task_worktrees_inside_the_studio_are_ok(attached):
    add_task_worktree(attached)
    r = rows(attached.root)["attach-worktrees"]
    assert r["level"] == "ok" and "1 task worktree" in r["detail"]


def test_a_deleted_task_worktree_is_an_error(attached):
    wt = add_task_worktree(attached)
    shutil.rmtree(wt)
    r = rows(attached.root)["attach-worktrees"]
    assert r["level"] == "error" and "t-001" in r["detail"] and "worktree repair" in r["detail"]


def test_a_worktree_of_another_repository_is_an_error(attached, tmp_path):
    wt = add_task_worktree(attached)
    other = make_upstream(tmp_path, "other")
    foreign = tmp_path / "foreign-wt"
    git(other.work, "worktree", "add", "-q", "-b", "x", str(foreign))
    shutil.rmtree(wt)
    shutil.copytree(foreign, wt, symlinks=True)
    assert rows(attached.root)["attach-worktrees"]["level"] == "error"


def test_a_moved_studio_names_the_repair_command(attached, tmp_path):
    add_task_worktree(attached)
    new = tmp_path / "moved-studio"
    shutil.move(str(attached.root), str(new))
    r = rows(new)["attach-worktrees"]
    assert r["level"] == "error" and "git -C app worktree repair" in r["detail"] and "moved from" in r["detail"]


def test_a_gitfile_that_names_the_clone_itself_is_an_error_not_a_pass(attached):
    """t-648 class review r2, S1: the old row asked `git -C <worktree>` for the common dir, and a gitfile rewritten
    to `gitdir: app/.git` answers with the clone's own, so the row said ok."""
    wt = add_task_worktree(attached)
    (wt / ".git").write_text("gitdir: %s\n" % (attached.app / ".git"))
    r = rows(attached.root)["attach-worktrees"]
    assert r["level"] == "error" and "t-001" in r["detail"] and "its .git file names" in r["detail"]


def test_the_digest_row_reads_the_record_not_the_state_file(attached):
    """The digest of the clone's config is the one the attach-init event holds; a forged state.json changes nothing."""
    import json
    state_path = config.state_dir(str(attached.root)) / "state.json"
    state = json.loads(state_path.read_text())
    state["config_digest"] = "0" * 64
    state_path.write_text(json.dumps(state))
    assert rows(attached.root)["attach-git-config"]["level"] == "ok"
    git(attached.app, "config", "--local", "extensions.worktreeConfig", "true")
    assert rows(attached.root)["attach-git-config"]["level"] == "error"


def test_a_worktree_outside_the_studio_is_listed(attached, tmp_path):
    git(attached.app, "worktree", "add", "-q", "-b", "stray", str(tmp_path / "stray-wt"), "origin/main")
    r = rows(attached.root)["attach-worktrees"]
    assert r["level"] == "error" and "outside the studio" in r["detail"]


def test_a_repository_pre_push_hook_is_named_not_replaced(attached):
    hook = attached.app / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    assert "pre-push hook present" in rows(attached.root)["attach-push-checks"]["detail"]
    assert hook.read_text() == "#!/bin/sh\nexit 0\n"


# ---- R11: a project without attach: ------------------------------------------------------------------

def _plain_rows(project):
    _full(project)
    from alpaca import cli
    cli.main(["init"])
    cli.main(["onboard", "--name", "d", "--who", "a:owner", "--what", "w"])
    os.makedirs(doctor.paths.transcript_dir(project), exist_ok=True)
    return doctor.checks(project)


def test_doctor_output_is_identical_for_a_plain_project_whatever_the_attach_package_does(project, monkeypatch):
    first = _plain_rows(project)
    assert not [c for c in first if c["name"].startswith("attach")]
    with_module_missing = None
    monkeypatch.setitem(sys.modules, "alpaca.attach.health", None)             # `import` of it raises ImportError
    with_module_missing = doctor.checks(project)
    assert with_module_missing == first

    def boom(root, add):
        raise RuntimeError("must not be called")
    monkeypatch.undo()
    import alpaca.attach.health as health
    monkeypatch.setattr(health, "doctor_check", boom)
    assert doctor.checks(project) == first


def test_a_plain_project_cannot_become_attached_by_an_empty_key(project):
    _plain_rows(project)
    cfg = project_mod.load(project)
    assert "attach" not in cfg
    assert config.mode(project) is None and config.config_errors(project) == []


def test_each_unsound_clone_names_what_is_wrong(attached, tmp_path):
    outside = tmp_path / "elsewhere"
    shutil.move(str(attached.app / ".git" / "objects"), str(outside))
    (attached.app / ".git" / "objects").symlink_to(outside)
    detail = rows(attached.root)["attach-repo"]["detail"]
    assert "link outside" in detail and "objects" in detail


def test_a_link_inside_git_that_stays_inside_it_is_not_a_problem(attached):
    (attached.app / ".git" / "refs-alias").symlink_to(attached.app / ".git" / "refs")
    assert rows(attached.root)["attach-repo"]["level"] == "ok"


def test_a_studio_that_is_a_linked_worktree_is_an_error_and_a_staged_gitlink_is_named(tmp_path, monkeypatch, upstream):
    from alpaca.tests.attachkit import make_linked_worktree_studio
    root = make_linked_worktree_studio(tmp_path, monkeypatch)
    (root / "app").mkdir()
    git(tmp_path, "clone", "-q", upstream.url, str(root / "app"))
    write_attach(root, **good_block(upstream))
    git(root, "add", "-A")
    detail = rows(root)["attach-repo"]
    assert detail["level"] == "error"
    assert "linked git worktree" in detail["detail"] and "embedded repository" in detail["detail"]


def test_doctor_reports_a_clone_the_studio_no_longer_ignores(attached):
    (attached.studio / ".git" / "info" / "exclude").write_text("")
    r = rows(attached.root)["attach-repo"]
    assert r["level"] == "error" and "does not ignore app/" in r["detail"]


def test_doctor_reports_a_nested_alpaca_manifest_in_the_clone(attached):
    sub = attached.app / "tools" / "kit"
    sub.mkdir(parents=True)
    (sub / "ALPACA-MANIFEST").write_text("# vendored\n")
    git(attached.app, "add", "-A")
    git(attached.app, "commit", "-q", "-m", "vendor a kit")
    row = rows(attached.root)["attach-repo"]
    assert row["level"] == "error" and "ALPACA-MANIFEST at tools/kit/" in row["detail"], row


# ----------------------------------------------------------------------------- t-675 (t-647 follow-ups 2 and 4)

def test_a_moved_studio_with_no_task_worktrees_gets_a_moved_row(attached, tmp_path):
    new = tmp_path / "moved-studio"
    shutil.move(str(attached.root), str(new))
    r = rows(new)["attach-worktrees"]
    assert r["level"] == "warn" and "moved from" in r["detail"] and str(new) in r["detail"], r
    assert "no task worktree exists yet" in r["detail"]


def test_the_moved_row_does_not_echo_control_characters_from_state_json(attached):
    """A recorded root with an escape sequence and a newline is shown as one safe line (t-675 round 2, M1)."""
    import json
    sf = attached.root / ".alpaca" / "attach" / "state.json"
    st = json.loads(sf.read_text())
    st["root"] = "/old\x1b[2J\nattach-worktrees ok forged"
    sf.write_text(json.dumps(st))
    r = rows(attached.root)["attach-worktrees"]
    assert "\x1b" not in r["detail"] and "\n" not in r["detail"], repr(r["detail"])
    assert "/old?[2J?attach-worktrees ok forged" in r["detail"]


def test_a_studio_that_did_not_move_still_reads_ok(attached):
    r = rows(attached.root)["attach-worktrees"]
    assert r["level"] == "ok" and "0 task worktree" in r["detail"]


def test_doctor_in_a_studio_reached_through_a_link_says_the_path_is_a_link(attached, tmp_path):
    link = tmp_path / "studio-link"
    link.symlink_to(attached.root)
    r = rows(link)["attach-repo"]
    assert r["level"] == "error" and "studio path is a link" in r["detail"], r
    assert "linked git worktree" not in r["detail"]


def test_a_link_to_a_studio_without_git_gets_the_link_row_and_no_git_rows(project, upstream, tmp_path):
    """A05: the git rows ran for any kind but "none", so a link to a git-less studio was told to edit a
    .git/info/exclude that does not exist."""
    from alpaca.attach import health
    from alpaca.tests.attachkit import make_studio
    root = make_studio(project, git_init=False)
    repo.attach(str(root), upstream.url)
    link = tmp_path / "studio-link"
    link.symlink_to(root)
    problems = health.repo_problems(str(link), "app")
    assert any("studio path is a link" in p for p in problems), problems
    assert not any("does not ignore" in p or "gitlink" in p for p in problems), problems
