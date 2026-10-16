"""The one git runner of alpaca/attach/ (build plan P4, P5; pre-mortem X1)."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from alpaca.attach import AttachError, gitops, repo
from alpaca.tests.attachkit import attached, git, studio, upstream  # noqa: F401

ATTACH_DIR = Path(__file__).resolve().parent.parent / "attach"


@pytest.mark.parametrize("var", ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR"])
def test_inherited_git_variables_cannot_redirect_a_call_to_the_studio(attached, monkeypatch, var):
    studio_git = str(attached.studio / ".git")
    # the studio needs a commit of its own so that "the studio head" differs from the app head
    (attached.studio / "note.txt").write_text("x\n")
    git(attached.studio, "add", "note.txt")
    git(attached.studio, "commit", "-q", "-m", "studio commit")
    studio_head = git(attached.studio, "rev-parse", "HEAD")
    app_head = git(attached.app, "rev-parse", "HEAD")
    assert studio_head != app_head
    value = {"GIT_INDEX_FILE": studio_git + "/index", "GIT_OBJECT_DIRECTORY": studio_git + "/objects",
             "GIT_WORK_TREE": str(attached.studio)}.get(var, studio_git)
    monkeypatch.setenv(var, value)
    assert gitops.git(str(attached.app), "rev-parse", "HEAD") == app_head
    assert gitops.is_toplevel(str(attached.app))
    assert gitops.git(str(attached.app), "log", "-1", "--format=%s") == "second commit"
    assert gitops.git(str(attached.app), "status", "--porcelain") == ""


def test_inherited_config_variables_do_not_reach_git(attached, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Intruder")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "/tmp/evil")
    assert gitops.git(str(attached.app), "config", "--local", "user.name") == "Alpaca agent"
    assert gitops.git(str(attached.app), "config", "--local", "--get", "core.hooksPath", check=False) == ""
    assert gitops.git(str(attached.app), "var", "GIT_COMMITTER_IDENT", identity=True).startswith("Alpaca agent <alpaca-agent@localhost>")
    env = gitops.clean_env()
    assert not [k for k in env if k.startswith("GIT_CONFIG_KEY") or k.startswith("GIT_CONFIG_VALUE")] and "GIT_CONFIG_COUNT" not in env


def test_the_token_is_never_in_the_environment_of_a_plain_call(monkeypatch):
    for name in ("ALPACA_ATTACH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "PUBLIC_REPO_TOKEN", "GIT_ASKPASS", "SSH_ASKPASS"):
        monkeypatch.setenv(name, "s3cret")
    env = gitops.clean_env()
    assert "s3cret" not in " ".join(env.values())
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert gitops.clean_env({"X": "1"})["X"] == "1"


def test_process_env_scrubbed_removes_and_restores(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/x/.git")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "a.b")
    monkeypatch.setenv("KEEP", "yes")
    with gitops.process_env_scrubbed():
        assert "GIT_DIR" not in os.environ and "GIT_CONFIG_COUNT" not in os.environ and "GIT_CONFIG_KEY_0" not in os.environ
        assert os.environ["KEEP"] == "yes"
    assert os.environ["GIT_DIR"] == "/x/.git" and os.environ["GIT_CONFIG_KEY_0"] == "a.b"
    with pytest.raises(RuntimeError):
        with gitops.process_env_scrubbed():
            raise RuntimeError("boom")
    assert os.environ["GIT_DIR"] == "/x/.git"


def test_the_runner_turns_off_hooks_and_prompts(attached, tmp_path):
    hook = attached.app / ".git" / "hooks" / "post-checkout"
    marker = tmp_path / "hook-ran"
    hook.write_text("#!/bin/sh\ntouch %s\n" % marker)
    hook.chmod(0o755)
    gitops.git(str(attached.app), "checkout", "-q", "-B", "main", "origin/main")
    assert not marker.exists()
    git(attached.app, "checkout", "-q", "-B", "main", "origin/main")
    assert marker.exists(), "the control run (plain git) does run the hook, so the test can see it"


def test_a_failing_git_raises_one_scrubbed_line(tmp_path):
    with pytest.raises(AttachError) as exc:
        gitops.git(str(tmp_path), "rev-parse", "--verify", "nope")
    assert "\n" not in str(exc.value) and str(exc.value).startswith("git rev-parse failed")
    assert gitops.git(str(tmp_path), "rev-parse", "--verify", "nope", check=False) == ""
    assert gitops.run(str(tmp_path), "rev-parse", "--verify", "nope").rc != 0


def test_git_returns_bytes_for_binary_and_keeps_leading_spaces(attached):
    (attached.app / "README.md").write_text("changed\n")
    assert gitops.git(str(attached.app), "status", "--porcelain").startswith(" M README.md")
    assert isinstance(gitops.git(str(attached.app), "diff", binary=True), bytes)
    git(attached.app, "checkout", "--", "README.md")


def test_identity_is_applied_only_when_asked(attached):
    (attached.app / "new.txt").write_text("n\n")
    gitops.git(str(attached.app), "add", "new.txt")
    gitops.git(str(attached.app), "commit", "-q", "-m", "by agent", identity=True)
    assert gitops.git(str(attached.app), "log", "-1", "--format=%an <%ae>") == "Alpaca agent <alpaca-agent@localhost>"


@pytest.mark.parametrize("secret_url", ["https://github.com/o/r", "https://example.org/o/r"])
def test_remote_carries_the_token_only_to_github(monkeypatch, tmp_path, secret_url):
    seen = {}

    def fake_run(repo_path, *args, timeout=0, env=None, **kw):
        seen["env"], seen["args"] = env, args
        return gitops.Result(0, "ok\n", "")
    monkeypatch.setattr(gitops, "run", fake_run)
    monkeypatch.setattr(gitops, "token", lambda: "tok123")
    assert gitops.remote(str(tmp_path), "ls-remote", secret_url, url=secret_url) == "ok"
    carried = "GIT_CONFIG_VALUE_0" in seen["env"]
    assert carried == secret_url.startswith("https://github.com/")
    assert "tok123" not in " ".join(map(str, seen["args"]))
    if carried:
        import base64
        header = seen["env"]["GIT_CONFIG_VALUE_0"].split()[-1]
        assert base64.b64decode(header).decode() == "x-access-token:tok123"
        assert seen["env"]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"


def test_a_remote_failure_never_prints_the_token_or_userinfo(monkeypatch, tmp_path):
    monkeypatch.setattr(gitops, "token", lambda: "tok123")
    monkeypatch.setattr(gitops, "run", lambda *a, **k: gitops.Result(128, "", "fatal: unable to access 'https://u:tok123\x40github.com/o/r/'"))
    with pytest.raises(AttachError) as exc:
        gitops.remote(str(tmp_path), "fetch", url="https://github.com/o/r")
    assert "tok123" not in str(exc.value)


def test_token_order_env_then_gh_then_none(monkeypatch):
    monkeypatch.setenv("ALPACA_ATTACH_TOKEN", "  envtok ")
    assert gitops.token() == "envtok"
    monkeypatch.delenv("ALPACA_ATTACH_TOKEN")

    class Done:
        returncode, stdout = 0, "ghtok\n"
    monkeypatch.setattr(gitops.subprocess, "run", lambda *a, **k: Done())
    assert gitops.token() == "ghtok"

    def missing(*a, **k):
        raise FileNotFoundError("gh")
    monkeypatch.setattr(gitops.subprocess, "run", missing)
    assert gitops.token() is None


def test_scrub_and_split_and_repository_of():
    assert gitops.scrub("fatal: https://user:pw@host/x and ssh://git@h/y") == "fatal: https://host/x and ssh://h/y"
    assert gitops.split_source("https://h/x")[0] == "url"
    assert gitops.split_source("git\x40github.com:o/r.git")[0] == "scp"
    assert gitops.split_source("/srv/a:b")[0] == "path" and gitops.split_source("rel/dir")[0] == "path"
    assert gitops.repository_of("https://github.com/example-org/QTRM64") == "example-org/QTRM64"
    assert gitops.repository_of("https://github.com/o/r.git") == "o/r"
    assert gitops.repository_of("git\x40github.com:o/r.git") == "o/r"
    assert gitops.repository_of("https://example.org/o/r") is None and gitops.repository_of("/srv/x.git") is None


def test_validate_source(studio, tmp_path):
    root = str(studio)
    for bad in ("", "  ", "-x", "--upload-pack=x", "a\x07b", "ext::sh -c x", "https://u:p@h/x", "ftp://h/x", "ssh://git@h/x",
                "file://u:p@/srv/x", str(studio), str(studio / "src"), str(tmp_path / "missing")):
        with pytest.raises(AttachError):
            gitops.validate_source(bad, root)
    assert gitops.validate_source("https://github.com/o/r", root) == "https://github.com/o/r"
    assert gitops.validate_source("git\x40github.com:o/r.git", root) == "git\x40github.com:o/r.git"
    assert gitops.validate_source(str(tmp_path), root) == os.path.realpath(str(tmp_path))
    assert gitops.validate_source(tmp_path.as_uri(), root) == os.path.realpath(str(tmp_path))


def test_is_toplevel(attached, tmp_path):
    assert gitops.is_toplevel(str(attached.app)) and gitops.is_toplevel(str(attached.studio))
    assert not gitops.is_toplevel(str(attached.app / "src"))
    plain = attached.studio / "plain"
    plain.mkdir()
    assert not gitops.is_toplevel(str(plain))                        # inside the studio repo, no .git of its own
    link = tmp_path / "link"
    os.symlink(attached.app, link)
    assert not gitops.is_toplevel(str(link))
    gitfile = tmp_path / "gf"
    gitfile.mkdir()
    (gitfile / ".git").write_text("gitdir: %s\n" % (attached.app / ".git"))
    assert not gitops.is_toplevel(str(gitfile))


def test_config_digest_sees_what_steers_a_push_and_ignores_the_rest(attached):
    app = str(attached.app)
    base = gitops.config_digest(app)
    assert gitops.config_digest(app) == base
    git(attached.app, "config", "--local", "alias.zz", "status")
    git(attached.app, "config", "--local", "branch.main.note", "x")
    assert gitops.config_digest(app) == base
    for key, value in (("remote.origin.pushurl", "https://example.org/x"), ("core.hooksPath", ".githooks"),
                       ("merge.ours.driver", "true"), ("filter.lfs.clean", "x"), ("credential.helper", "store"),
                       ("url.https://h/.insteadOf", "gh:"), ("user.email", "evil@example.org")):
        was = git(attached.app, "config", "--local", "--get", key, check=False)
        git(attached.app, "config", "--local", key, value)
        assert gitops.config_digest(app) != base, key
        if was:
            git(attached.app, "config", "--local", key, was)
        else:
            git(attached.app, "config", "--local", "--unset", key)
    assert gitops.config_digest(app) == base


@pytest.mark.parametrize("key,value", [
    ("push.followTags", "true"), ("push.recurseSubmodules", "on-demand"), ("push.pushOption", "x"),
    ("push.gpgSign", "true"), ("push.default", "matching"), ("gpg.program", "/bin/true"), ("submodule.recurse", "true"),
    ("core.gitProxy", "/bin/true"), ("core.askPass", "/bin/true"), ("core.fsmonitor", "/bin/true"),
    ("transfer.fsckObjects", "true"), ("pack.threads", "1"), ("sendpack.sideband", "false"), ("ssh.variant", "simple"),
    ("fetch.recurseSubmodules", "yes"), ("receive.denyDeletes", "true"), ("diff.external", "/bin/true"),
    ("some.newkey", "1")])
def test_config_digest_watches_every_key_but_a_short_housekeeping_list(attached, key, value):
    """t-686 adversarial B1: a list of watched keys missed push.followTags and its siblings. The digest now covers every
    local key except the core housekeeping keys git writes itself, alias.* and branch.*."""
    app = str(attached.app)
    base = gitops.config_digest(app)
    git(attached.app, "config", "--local", key, value)
    assert gitops.config_digest(app) != base, key
    git(attached.app, "config", "--local", "--unset", key)
    assert gitops.config_digest(app) == base


def test_config_digest_ignores_the_housekeeping_keys_and_tells_an_unreadable_config_apart(attached, tmp_path):
    app = str(attached.app)
    base = gitops.config_digest(app)
    for key, value in (("core.filemode", "false"), ("core.logallrefupdates", "false"), ("core.ignorecase", "true"),
                       ("alias.zz", "status"), ("branch.main.remote", "elsewhere"), ("branch.main.merge", "refs/heads/x")):
        git(attached.app, "config", "--local", key, value)
    assert gitops.config_digest(app) == base
    # a folder that is no repository has no readable local config: its digest is the one fixed value, never an attach digest
    assert gitops.config_digest(str(tmp_path)) != base


def test_no_raw_subprocess_in_attach():
    """Every process attach starts goes through gitops (or release.checks.run_command, which is not
    `subprocess` in this package). A new module that calls subprocess itself fails here."""
    offenders = []
    for path in sorted(ATTACH_DIR.glob("*.py")):
        if path.name == "gitops.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\bsubprocess\b|\bos\.system\b|\bos\.popen\b|\bPopen\b", line.split("#", 1)[0]):
                offenders.append("%s:%d: %s" % (path.name, number, line.strip()))
    assert offenders == []


# ---- round 1 of the adversarial check ---------------------------------------------------------------------------------

AUTHOR_VARS = {"GIT_AUTHOR_NAME": "Leaky Human", "GIT_AUTHOR_EMAIL": "leaky@example.org",
               "GIT_COMMITTER_NAME": "Leaky Human", "GIT_COMMITTER_EMAIL": "leaky@example.org"}


def test_the_clean_environment_drops_author_committer_and_template_variables(monkeypatch):
    for k, v in {**AUTHOR_VARS, "GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z",
                 "GIT_TEMPLATE_DIR": "/somewhere/tpl"}.items():
        monkeypatch.setenv(k, v)
    env = gitops.clean_env()
    assert not [k for k in env if k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_")) or k == "GIT_TEMPLATE_DIR"]


def test_a_commit_made_with_identity_carries_the_attach_identity_whatever_the_caller_exports(attached, monkeypatch):
    for k, v in AUTHOR_VARS.items():
        monkeypatch.setenv(k, v)
    gitops.git(str(attached.app), "commit", "-q", "--allow-empty", "-m", "probe", identity=True)
    ident = "Alpaca agent <alpaca-agent@localhost>"
    assert git(attached.app, "log", "-1", "--format=%an <%ae> / %cn <%ce>") == ident + " / " + ident


def test_a_call_without_identity_does_not_inherit_the_callers_author(attached, monkeypatch):
    """The control: with identity off the runner passes no author, so the repository's own config answers."""
    for k, v in AUTHOR_VARS.items():
        monkeypatch.setenv(k, v)
    gitops.git(str(attached.app), "commit", "-q", "--allow-empty", "-m", "probe")
    assert git(attached.app, "log", "-1", "--format=%an") == "Alpaca agent"     # attach set user.name in the clone


def test_studio_kind_tells_own_borrowed_and_none_apart(tmp_path, monkeypatch):
    own = tmp_path / "own"
    own.mkdir()
    git(own, "init", "-q", "-b", "main")
    assert gitops.studio_kind(str(own)) == "own"
    nested = own / "sub"
    nested.mkdir()
    assert gitops.studio_kind(str(nested)) == "borrowed"
    plain = tmp_path / "plain"
    plain.mkdir()
    assert gitops.studio_kind(str(plain)) == "none"
    from alpaca.tests.attachkit import make_linked_worktree_studio
    assert gitops.studio_kind(str(make_linked_worktree_studio(tmp_path, monkeypatch))) == "borrowed"
    gitfile = tmp_path / "gitfile"
    gitfile.mkdir()
    (gitfile / ".git").write_text("gitdir: %s\n" % (own / ".git"))
    assert gitops.studio_kind(str(gitfile)) == "borrowed"


def test_clone_problems_is_empty_for_a_sound_clone_and_names_each_defect(attached, tmp_path):
    assert gitops.clone_problems(str(attached.app), "app/") == []
    (attached.app / ".git" / "commondir").write_text("../elsewhere\n")
    assert any("commondir" in p for p in gitops.clone_problems(str(attached.app), "app/"))


def test_a_link_in_a_ref_folder_that_leaves_git_is_found(attached, tmp_path):
    outside = tmp_path / "outside-refs"
    outside.mkdir()
    (attached.app / ".git" / "refs" / "heads" / "stray").symlink_to(outside)
    assert any("refs/heads/stray" in p for p in gitops.clone_problems(str(attached.app), "app/"))


def test_a_ceiling_variable_does_not_hide_the_work_tree_around_the_studio(project, monkeypatch):
    """GIT_CEILING_DIRECTORIES in the caller would make a folder of another repository look like it has no git
    around it; the runner drops it, so the studio is still seen as borrowed."""
    from alpaca.tests.attachkit import make_studio
    root = make_studio(project, git_init=False)
    git(Path(root).parent, "init", "-q", "-b", "main")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(Path(root).parent))
    assert gitops.studio_kind(str(root)) == "borrowed"


# ---- t-648 fix round 2: history rewriting off, the work-folder runner and its proof ------------------------------------------

def _cut(att, task="t-001", worker="w1"):
    """A real work folder of `task`, cut the way a claim cuts it; returns its real path."""
    from alpaca.attach import worktree
    worktree.cut(str(att.root), task, worker=worker, session="cli")
    return os.path.realpath(str(att.root / ".alpaca/attach/worktrees" / task))


def test_the_clean_environment_switches_history_rewriting_off_and_does_not_set_a_shallow_file(monkeypatch):
    monkeypatch.setenv("GIT_SHALLOW_FILE", "/somewhere")
    env = gitops.clean_env()
    assert env["GIT_NO_REPLACE_OBJECTS"] == "1" and env["GIT_GRAFT_FILE"] == os.devnull
    assert "advice.graftFileDeprecated=false" in gitops._QUIET_HOOKS
    # GIT_SHALLOW_FILE=/dev/null makes git read every repository as shallow, so clone_problems would refuse every clone
    monkeypatch.delenv("GIT_SHALLOW_FILE")
    assert "GIT_SHALLOW_FILE" not in gitops.clean_env()


def test_a_full_clone_has_no_problems_under_the_clean_environment_and_the_graft_hint_is_silent(attached):
    assert gitops.clone_problems(str(attached.app), "app/") == []
    assert gitops.git(str(attached.app), "rev-parse", "--is-shallow-repository") == "false"
    grafts = attached.app / ".git" / "info" / "grafts"
    grafts.parent.mkdir(exist_ok=True)
    grafts.write_text("%s\n" % git(attached.app, "rev-parse", "HEAD"))                  # makes HEAD a root for plain git
    res = gitops.run(str(attached.app), "rev-list", "--count", "HEAD")
    assert res.rc == 0 and res.err == "" and res.out.strip() == "2"                     # the grafts file is masked, no hint
    assert git(attached.app, "rev-list", "--count", "HEAD") == "1"                      # (plain git does read it)


def test_a_replace_ref_does_not_change_what_a_sha_names_for_attach(attached):
    up = attached.upstream.sha()
    git(attached.app, "fetch", "-q", "--no-tags", attached.upstream.url, "+refs/heads/main:refs/pm/up")
    blob = git(attached.app, "hash-object", "-w", "--stdin", input="planted\n")
    tree = git(attached.app, "mktree", input="100644 blob %s\tPLANTED\n" % blob)
    fake = git(attached.app, "commit-tree", tree, "-m", "planted")
    git(attached.app, "replace", up, fake)
    assert git(attached.app, "ls-tree", "--name-only", up) == "PLANTED"                 # plain git follows it
    assert "PLANTED" not in gitops.git(str(attached.app), "ls-tree", "--name-only", up)


def test_the_config_digest_watches_the_extensions_keys(attached):
    before = gitops.config_digest(str(attached.app))
    git(attached.app, "config", "extensions.worktreeConfig", "true")
    assert gitops.config_digest(str(attached.app)) != before


def test_tree_manifest_hits_finds_the_file_at_any_depth_and_in_any_case(attached):
    first = attached.upstream.sha()
    assert gitops.tree_manifest_hits(str(attached.app), first) == []
    new = attached.upstream.commit({"ALPACA-MANIFEST": "x\n", "deep/er/Alpaca-Manifest": "y\n", "ok/alpaca-manifest.txt": "z\n"}, "m")
    git(attached.app, "fetch", "-q", "--no-tags", attached.upstream.url, "+refs/heads/main:refs/pm/up")
    assert sorted(gitops.tree_manifest_hits(str(attached.app), new)) == ["ALPACA-MANIFEST", "deep/er/Alpaca-Manifest"]


def test_git_refuses_to_act_in_a_task_work_folder_and_in_any_folder_inside_it(attached):
    folder = _cut(attached)
    for target in (folder, os.path.join(folder, "src")):
        with pytest.raises(AttachError, match="in_worktree"):
            gitops.git(target, "status")
        with pytest.raises(AttachError, match="in_worktree"):
            gitops.run(target, "rev-parse", "HEAD")
    assert gitops.git(str(attached.app), "rev-parse", "--is-inside-work-tree") == "true"   # the clone itself is fine
    assert gitops.git(str(attached.root), "rev-parse", "--is-inside-work-tree", check=False) in ("true", "")


def test_in_worktree_names_the_repository_itself_whatever_the_gitfile_says(attached):
    folder = _cut(attached)
    gitops.prove_worktree(str(attached.app), "t-001", folder, "alpaca/t-001")
    assert gitops.in_worktree(str(attached.app), "t-001", folder, "symbolic-ref", "HEAD") == "refs/heads/alpaca/t-001"
    (attached.root / ".alpaca/attach/worktrees/t-001/.git").write_text("gitdir: %s\n" % (attached.app / ".git"))
    assert gitops.in_worktree(str(attached.app), "t-001", folder, "symbolic-ref", "HEAD") == "refs/heads/alpaca/t-001"
    os.unlink(os.path.join(folder, ".git"))
    assert gitops.in_worktree(str(attached.app), "t-001", folder, "symbolic-ref", "HEAD") == "refs/heads/alpaca/t-001"
    assert git(attached.app, "symbolic-ref", "HEAD") == "refs/heads/main"


def test_in_worktree_takes_only_an_absolute_folder_and_a_task_id(attached):
    folder = _cut(attached)
    with pytest.raises(AttachError, match="not an absolute path"):
        gitops.in_worktree(str(attached.app), "t-001", "relative/folder", "status")
    for bad in ("", "t-", "../t-001", "t-1/../x", None, "app"):
        with pytest.raises(AttachError, match="cannot name a work folder"):
            gitops.in_worktree(str(attached.app), bad, folder, "status")


def _forge(att, how, folder):
    admin = att.app / ".git/worktrees/t-001"
    if how == "gitfile-to-app":
        (Path(folder) / ".git").write_text("gitdir: %s\n" % (att.app / ".git"))
    elif how == "gitfile-missing":
        os.unlink(os.path.join(folder, ".git"))
    elif how == "gitfile-garbage":
        (Path(folder) / ".git").write_text("hello\n")
    elif how == "gitfile-is-a-link":
        os.unlink(os.path.join(folder, ".git"))
        os.symlink(str(admin / "gitdir"), os.path.join(folder, ".git"))
    elif how == "commondir-forged":
        (admin / "commondir").write_text("/tmp/elsewhere\n")
    elif how == "commondir-link":
        os.unlink(admin / "commondir")
        os.symlink("../..", admin / "commondir")
    elif how == "gitdir-forged":
        (admin / "gitdir").write_text("/tmp/elsewhere/.git\n")
    elif how == "head-detached":
        (admin / "HEAD").write_text("%s\n" % git(att.app, "rev-parse", "HEAD"))
    elif how == "head-other-branch":
        git(att.app, "branch", "other")
        (admin / "HEAD").write_text("ref: refs/heads/other\n")
    elif how == "admin-gone":
        shutil.rmtree(admin)
    elif how == "folder-is-a-link":
        moved = Path(str(folder) + ".moved")
        os.rename(folder, moved)
        os.symlink(moved, folder)
    elif how == "folder-gone":
        shutil.rmtree(folder)
    else:
        raise AssertionError(how)


FORGERIES = ["gitfile-to-app", "gitfile-missing", "gitfile-garbage", "gitfile-is-a-link", "commondir-forged", "commondir-link",
             "gitdir-forged", "head-detached", "head-other-branch", "admin-gone", "folder-is-a-link", "folder-gone"]


def test_prove_worktree_passes_for_a_fresh_cut(attached):
    folder = _cut(attached)
    gitops.prove_worktree(str(attached.app), "t-001", folder, "alpaca/t-001")


@pytest.mark.parametrize("how", FORGERIES)
def test_prove_worktree_refuses_each_forgery(attached, how):
    folder = _cut(attached)
    _forge(attached, how, folder)
    with pytest.raises(AttachError, match="work folder of t-001|cannot name|not sound"):
        gitops.prove_worktree(str(attached.app), "t-001", folder, "alpaca/t-001")


def test_prove_worktree_refuses_the_wrong_task_the_wrong_branch_and_the_clone_itself(attached):
    folder = _cut(attached)
    with pytest.raises(AttachError, match="not the folder named after the task"):
        gitops.prove_worktree(str(attached.app), "t-002", folder, "alpaca/t-002")
    with pytest.raises(AttachError, match="HEAD is not the work branch"):
        gitops.prove_worktree(str(attached.app), "t-001", folder, "alpaca/other")
    with pytest.raises(AttachError, match="not the folder named after the task|attached clone itself"):
        gitops.prove_worktree(str(attached.app), "t-001", os.path.realpath(str(attached.app)), "alpaca/t-001")


def test_prove_worktree_names_the_repair_for_a_broken_gitfile(attached):
    folder = _cut(attached)
    os.unlink(os.path.join(folder, ".git"))
    with pytest.raises(AttachError, match=r"git -C .* worktree repair"):
        gitops.prove_worktree(str(attached.app), "t-001", folder, "alpaca/t-001")


def test_worktree_list_reports_branch_and_prunable(attached):
    folder = _cut(attached)
    listing = {w["path"]: w for w in gitops.worktree_list(str(attached.app))}
    assert listing[folder]["branch"] == "refs/heads/alpaca/t-001" and listing[folder]["prunable"] is False
    assert listing[os.path.realpath(str(attached.app))]["branch"] == "refs/heads/main"
    shutil.rmtree(folder)
    assert {w["path"]: w for w in gitops.worktree_list(str(attached.app))}[folder]["prunable"] is True


def test_worktree_admins_on_finds_only_the_admin_folder_of_that_branch(attached):
    _cut(attached, "t-001")
    _cut(attached, "t-002")
    found = gitops.worktree_admins_on(str(attached.app), "alpaca/t-002")
    assert [os.path.basename(p) for p in found] == ["t-002"]
    assert gitops.worktree_admins_on(str(attached.app), "alpaca/none") == []


def test_studio_kind_names_a_link_path_as_link(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    git(real, "init", "-q", "-b", "main")
    link = tmp_path / "link"
    link.symlink_to(real)
    assert gitops.studio_kind(str(real)) == "own" and gitops.studio_kind(str(link)) == "link"


def test_studio_kind_names_a_link_path_with_a_trailing_slash_or_dot_as_link(tmp_path):
    """A02: `os.path.islink` on the raw text said no for `link/` and `link/.`, which are the same link."""
    real = tmp_path / "real"
    real.mkdir()
    git(real, "init", "-q", "-b", "main")
    link = tmp_path / "link"
    link.symlink_to(real)
    assert {gitops.studio_kind(p) for p in (str(link), str(link) + "/", str(link) + "/.", str(link) + "//")} == {"link"}
    assert gitops.studio_kind(str(real) + "/") == "own"
    # only a parent folder that is a link does not make the studio itself a link
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(tmp_path)
    assert gitops.studio_kind(str(parent_link / "real")) == "own"
    assert gitops.is_toplevel(str(link) + "/") is False


# --------------------------------------------------------------------- t-652 round 2: trace switches, no repository

@pytest.mark.parametrize("var", ["GIT_TRACE", "GIT_TRACE2", "GIT_TRACE2_EVENT", "GIT_TRACE2_PERF", "GIT_TRACE2_CONFIG_PARAMS",
                                 "GIT_TRACE_PACKET", "GIT_TRACE_CURL", "GIT_TRACE_SETUP", "GIT_TRACE_REDACT", "GIT_CURL_VERBOSE"])
def test_the_clean_environment_drops_every_trace_switch(monkeypatch, var):
    monkeypatch.setenv(var, "/tmp/should-not-be-written")
    assert var not in gitops.clean_env()


def test_a_trace_switch_never_makes_git_write_a_file(attached, monkeypatch, tmp_path):
    trace = tmp_path / "trace.out"
    for var in ("GIT_TRACE", "GIT_TRACE2_EVENT", "GIT_TRACE_PACKET", "GIT_TRACE_SETUP"):
        monkeypatch.setenv(var, str(trace))
    gitops.git(str(attached.app), "status", "--porcelain")
    assert not trace.exists()


def test_remote_with_no_repo_ignores_the_clone_config(attached, tmp_path):
    """`url.<forged>.insteadOf <upstream>` in the clone redirects a plain ls-remote; the no_repo form asks the upstream."""
    forged_bare = tmp_path / "forged.git"
    git(tmp_path, "clone", "-q", "--bare", str(attached.upstream.bare), str(forged_bare))
    work = tmp_path / "forged-work"
    git(tmp_path, "clone", "-q", str(forged_bare), str(work))
    git(work, "checkout", "-q", attached.cfg.base)
    (work / "forged.txt").write_text("x\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "forged")
    git(work, "push", "-q", "origin", attached.cfg.base)
    forged = git(work, "rev-parse", "HEAD")
    real = attached.upstream.sha()
    url = attached.upstream.url
    git(attached.app, "config", "url.%s.insteadOf" % forged_bare, url)
    ref = "refs/heads/%s" % attached.cfg.base
    plain = gitops.remote(str(attached.app), "ls-remote", "--", url, ref, url=url)
    anchored = gitops.remote(str(attached.app), "ls-remote", "--", url, ref, url=url, no_repo=True)
    assert plain.split()[0] == forged and anchored.split()[0] == real
