"""The credential layer under the text guard (op-062 t-651; pre-mortem X3, second half).

The text guard (alpaca/attach/guard.py) judges the git and gh calls the command text fixes and denies the
ones it shows but cannot fully read. It cannot see a path with no readable `git` or `gh` word in the Bash
text: a script on disk, `make push`, a word built at run time such as `$(printf gi)t`. What holds then is
`remote.<upstream>.pushurl`, set at attach time in the shared git config of the clone and all its task
worktrees. These tests run the bypass list for real, with the guard out of the way and an environment that
would let a push through, and require that the bare upstream does not change.

Known limits, stated here so no one reads more into the trap. It is config the agent can write: a path the text
layer cannot see (a script on disk, a run-time word) can rewrite it and then push by name; a `url.<x>.insteadOf`
rule in the global config rewrites it too, and config_digest hashes only local keys (t-651.logs/think/pick-a0.md,
"Confirmed in this step" 4); a Write or Edit of the clone's git config or hooks is not a Bash command; and when the
hook cannot find the studio it fails open. A push that spells the upstream's address out is held by the
`url.<trap>.pushInsteadOf` entries attach writes for the listed spellings of the configured address (t-697; before it, a
strict xfail pinned that such a push landed). Any other spelling of the same repository still reaches it, because git
compares the rewrite prefix as written: user information added, `//` or `/./` in the path, `file://localhost`, a relative
path, a port (`:443`, `ssh.github.com:443`), a leading slash after the colon, a host in another case, and a GitHub
owner/repo in another case than the configured one or its lower-cased form. Only a credential the clone and the session do not hold removes the
rest of the residue (t-654 or a follow-up). One strict xfail pins it,
`test_a_script_on_disk_that_rewrites_the_trap_is_a_known_gap`; the day the credential seam lands it turns red and
this note must change. The chain tests ask the hook before every step, as a session would, and run only
the steps it allows.
"""
import os
import subprocess

import pytest

from alpaca.attach import repo
from alpaca.hooks import git_guard
from alpaca.tests.attachkit import (add_worktree, attached, commit_in, git, hook_payload, studio,  # noqa: F401
                                     upstream)

BYPASS = [
    "python3 -c \"import subprocess as s; s.run(['g'+'it','push','origin','HEAD'])\"",
    "sh -c 'git push origin HEAD'", "bash -c 'git push'", "git -c alias.p=push p origin HEAD",
    "GIT_DIR=.git git push", "git --git-dir=.git push", "env git push", "command git push",
    "git push --no-verify origin HEAD", "git -c core.hooksPath=/dev/null push",
    "git push origin HEAD:refs/heads/elsewhere", "git push --force origin HEAD:main", "git push origin :main",
    "git push --mirror origin", "git push --tags origin", "git send-pack origin HEAD",
    "printf 'git push origin HEAD' | sh", "git push upstream HEAD",
    # fix round 1: forms built or hidden at run time, still pushing by the remote's name
    "G=git; $G push origin HEAD", "$(printf gi)t push origin HEAD:refs/heads/x",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.p GIT_CONFIG_VALUE_0=push git p origin HEAD",
    "git rebase -x 'git push origin HEAD:refs/heads/x' HEAD~1", "find . -maxdepth 0 -exec git push origin HEAD \\;",
]


def refs(bare):
    return git(bare, "for-each-ref", "--format=%(refname) %(objectname)")


def agent_env():
    """An environment in which a push would succeed if nothing stopped it: no prompt, no host config, and an
    ssh command that fails at once so the trap's address is never looked up on the network."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_SSH_COMMAND="false",
               GIT_AUTHOR_NAME="Agent", GIT_AUTHOR_EMAIL="agent@example.org", GIT_COMMITTER_NAME="Agent",
               GIT_COMMITTER_EMAIL="agent@example.org")
    return env


@pytest.fixture
def work(attached):
    wt = add_worktree(attached, "t-001")
    commit_in(wt, {"src/new.py": "x = 1\n"}, "a change")
    git(attached.app, "remote", "add", "upstream", attached.upstream.url)   # a second remote name, no trap on it
    git(attached.app, "config", "--local", "remote.upstream.pushurl", repo.PUSH_TRAP)
    return attached, wt


def test_the_control_push_lands_when_the_trap_is_removed(work):
    """Without the trap the same environment pushes: the tests below can fail."""
    att, wt = work
    before = refs(att.upstream.bare)
    git(att.app, "config", "--local", "--unset", "remote.origin.pushurl")
    git(att.app, "config", "--local", "--unset-all", repo.PUSH_REWRITE_KEY)         # the rewrites hold a push by name too
    done = subprocess.run("git push origin HEAD:refs/heads/control", shell=True, cwd=wt, env=agent_env(),
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert refs(att.upstream.bare) != before


@pytest.mark.parametrize("cmd", BYPASS)
def test_raw_push_from_a_task_worktree_cannot_reach_the_upstream(work, cmd):
    att, wt = work
    before = refs(att.upstream.bare)
    subprocess.run(cmd, shell=True, cwd=wt, env=agent_env(), capture_output=True, text=True, timeout=60)
    assert refs(att.upstream.bare) == before


@pytest.mark.parametrize("cmd", ["git push origin HEAD", "git push origin HEAD:refs/heads/elsewhere",
                                 "git push --delete origin main"])
def test_raw_push_from_the_clone_cannot_reach_the_upstream(work, cmd):
    att, _wt = work
    before = refs(att.upstream.bare)
    subprocess.run(cmd, shell=True, cwd=att.app, env=agent_env(), capture_output=True, text=True, timeout=60)
    assert refs(att.upstream.bare) == before


def test_the_trap_is_in_the_shared_config_so_a_worktree_sees_it(work):
    att, wt = work
    assert git(wt, "config", "--get", "remote.origin.pushurl").startswith("alpaca-handoff-only:")
    assert git(att.app, "config", "--get", "remote.origin.pushurl") == repo.PUSH_TRAP


def test_fetch_still_works_with_the_trap_in_place(work):
    att, wt = work
    att.upstream.commit({"docs/new.md": "x\n"}, "more")
    done = subprocess.run("git fetch origin", shell=True, cwd=wt, env=agent_env(), capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr
    assert git(att.app, "rev-parse", "origin/main") == att.upstream.sha()


@pytest.mark.parametrize("form", [
    "git -c remote.origin.pushurl={url} push origin HEAD:refs/heads/x",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.pushurl GIT_CONFIG_VALUE_0={url} git push origin "
    "HEAD:refs/heads/x",
])
def test_an_added_pushurl_on_the_trapped_remote_does_not_reach_the_upstream(work, form):
    """A pushurl added at run time joins the trap's; git tries the trap first and stops (ledger item 2)."""
    att, wt = work
    before = refs(att.upstream.bare)
    subprocess.run(form.format(url=att.upstream.url), shell=True, cwd=wt, env=agent_env(), capture_output=True,
                   text=True, timeout=60)
    assert refs(att.upstream.bare) == before


def test_a_run_time_push_to_the_spelled_out_url_cannot_reach_the_upstream(work):
    """t-697: the `url.<trap>.pushInsteadOf` entries rewrite a push that spells the upstream's address out (a word built
    at run time, so the text guard cannot read it) into the same refusing address as the trap on the remote's name."""
    att, wt = work
    before = refs(att.upstream.bare)
    subprocess.run("G=git; $G push %s HEAD:refs/heads/gap" % att.upstream.url, shell=True, cwd=wt, env=agent_env(),
                   capture_output=True, text=True, timeout=60)
    assert refs(att.upstream.bare) == before


@pytest.mark.parametrize("form", ["{url}", "{url}/", "file://{url}", "{stem}", "{stem}.git"])
def test_every_spelling_of_the_upstream_folder_cannot_reach_the_upstream(work, form):
    att, wt = work
    before = refs(att.upstream.bare)
    stem = att.upstream.url[:-4] if att.upstream.url.endswith(".git") else att.upstream.url
    spelled = form.format(url=att.upstream.url.rstrip("/"), stem=stem)
    subprocess.run("G=git; $G push %s HEAD:refs/heads/gap" % spelled, shell=True, cwd=wt, env=agent_env(),
                   capture_output=True, text=True, timeout=60)
    assert refs(att.upstream.bare) == before


def test_attach_sets_a_pushinsteadof_entry_for_each_spelling_of_the_upstream(work):
    att, wt = work
    have = git(att.app, "config", "--local", "--get-all", repo.PUSH_REWRITE_KEY).splitlines()
    assert have and att.upstream.url in have
    assert have == repo.push_url_forms(att.upstream.url)
    assert git(wt, "config", "--get-all", repo.PUSH_REWRITE_KEY).splitlines() == have      # a task worktree shares it


HOST_FORMS = [
    "https://github.com/acme/widget", "https://github.com/acme/widget.git", "https://github.com/acme/widget/",
    "http://github.com/acme/widget.git", "git\x40github.com:acme/widget.git", "git\x40github.com:acme/widget",
    "github.com:acme/widget.git", "ssh://git\x40github.com/acme/widget.git", "ssh://git\x40github.com/acme/widget",
    "ssh://github.com/acme/widget.git", "git://github.com/acme/widget.git",
]


def _host_clone(tmp_path, configured):
    clone = tmp_path / "hostclone"
    git(tmp_path, "init", "-q", str(clone))
    git(clone, "commit", "-q", "--allow-empty", "-m", "x")
    git(clone, "remote", "add", "origin", configured)
    git(clone, "config", "--local", "remote.origin.pushurl", repo.PUSH_TRAP)
    repo.set_push_rewrites(str(clone), "origin")
    return clone


def _push_target(clone, spelled):
    """The address git would push to: `git push -v` names it on its first line, before any transport runs. The ssh
    command fails at once and a dead proxy stops an unrewritten https push, so no run reaches a network."""
    env = agent_env()
    env.update(https_proxy="http://127.0.0.1:1", http_proxy="http://127.0.0.1:1")
    done = subprocess.run(["git", "push", "-v", "--dry-run", spelled, "HEAD:refs/heads/x"], cwd=clone, env=env,
                          capture_output=True, text=True, timeout=60)
    return [x for x in done.stderr.splitlines() if x.startswith("Pushing to ")][:1]


@pytest.mark.parametrize("configured", ["https://github.com/acme/widget.git", "git\x40github.com:acme/widget.git"])
@pytest.mark.parametrize("spelled", HOST_FORMS)
def test_a_host_address_in_any_listed_form_is_rewritten_to_the_trap(tmp_path, configured, spelled):
    clone = _host_clone(tmp_path, configured)
    target = _push_target(clone, spelled)
    assert target and target[0].startswith("Pushing to " + repo.PUSH_TRAP), (spelled, target)


def test_another_repository_on_the_same_host_is_not_rewritten(tmp_path):
    clone = _host_clone(tmp_path, "https://github.com/acme/widget.git")
    assert _push_target(clone, "https://github.com/acme/other.git") == ["Pushing to https://github.com/acme/other.git"]


def test_a_longer_name_that_starts_with_the_upstream_name_is_refused_too_a_pinned_over_block(tmp_path):
    """git has no exact match for a rewrite prefix, and the stem with no `.git` must be held (a push to `.../widget`), so a
    neighbour whose name starts with it is rewritten to the refusing address as well. Safe side; docs/attach.md says so."""
    clone = _host_clone(tmp_path, "https://github.com/acme/widget.git")
    for neighbour in ("https://github.com/acme/widgetx.git", "https://github.com/acme/widget-fork.git"):
        target = _push_target(clone, neighbour)
        assert target and target[0].startswith("Pushing to " + repo.PUSH_TRAP), neighbour
    assert _push_target(clone, "https://github.com/acme/other.git") == ["Pushing to https://github.com/acme/other.git"]


def test_a_folder_that_extends_the_upstream_folder_name_is_refused_too(work):
    att, wt = work
    sibling = att.upstream.bare.parent / (att.upstream.bare.name + "-work")
    subprocess.run(["git", "init", "-q", "--bare", str(sibling)], check=True, env=agent_env())
    before = refs(sibling)
    subprocess.run("G=git; $G push %s HEAD:refs/heads/x" % sibling, shell=True, cwd=wt, env=agent_env(),
                   capture_output=True, text=True, timeout=60)
    assert refs(sibling) == before


def test_the_lower_cased_github_spelling_is_held_when_the_configured_one_has_capitals(tmp_path):
    """GitHub ignores the case of owner and repo; a prefix rule cannot, so only the lower-cased spelling is added."""
    clone = _host_clone(tmp_path, "https://github.com/Acme/Widget.git")
    for spelled in ("https://github.com/Acme/Widget.git", "https://github.com/acme/widget.git", "git\x40github.com:acme/widget",
                    "ssh://git\x40github.com/acme/widget.git"):
        target = _push_target(clone, spelled)
        assert target and target[0].startswith("Pushing to " + repo.PUSH_TRAP), spelled
    # a mixed spelling (only the repo capitalised) is not one of the two, and is not held
    assert _push_target(clone, "https://github.com/acme/Widget.git") == ["Pushing to https://github.com/acme/Widget.git"]


def test_the_push_url_forms_cover_the_listed_spellings():
    forms = repo.push_url_forms("https://github.com/acme/widget")
    for want in ("https://github.com/acme/widget", "https://github.com/acme/widget.git", "https://github.com/acme/widget/",
                 "git\x40github.com:acme/widget", "git\x40github.com:acme/widget.git", "ssh://git\x40github.com/acme/widget",
                 "ssh://git\x40github.com/acme/widget.git", "ssh://github.com/acme/widget.git"):
        assert want in forms, want
    assert len(forms) == len(set(forms)) and repo.push_url_forms("") == []


# ---- fix round 3: the hook in the loop, then the residue a Bash-text layer cannot see -----------------------------

def _hook_allows(cmd, cwd):
    out = git_guard.decide(hook_payload(cmd, cwd))
    return not (out and out["hookSpecificOutput"]["permissionDecision"] == "deny")


@pytest.mark.parametrize("chain,where", [
    # investigate-r2.md: the trap is removed through xargs, then a push by name through xargs
    (["echo set-url --push origin {url} | xargs git remote", "echo push origin HEAD:refs/heads/chain-a | xargs git"],
     "wt"),
    # investigate-r2-probe: a run-time git word removes the trap and pushes by name
    (["G=git; $G remote set-url --push origin {url}; $G push origin HEAD:refs/heads/chain-b"], "wt"),
    (["G=git; $G remote set-url --push origin {url}", "G=git; $G push origin HEAD:refs/heads/chain-c"], "wt"),
    # approach-0 F3: env -C moves the target from the studio root into the clone
    (["env -C app git remote set-url --push origin {url}", "env -C app git push origin HEAD:refs/heads/chain-d"],
     "root"),
])
def test_a_chain_of_steps_the_hook_is_asked_about_cannot_reach_the_upstream(work, chain, where):
    att, wt = work
    cwd = wt if where == "wt" else att.root
    before = refs(att.upstream.bare)
    ran = []
    for step in chain:
        cmd = step.format(url=att.upstream.url)
        if not _hook_allows(cmd, cwd):
            continue
        ran.append(cmd)
        subprocess.run(cmd, shell=True, cwd=cwd, env=agent_env(), capture_output=True, text=True, timeout=60)
    assert refs(att.upstream.bare) == before, "steps the hook allowed: %r" % ran
    assert git(att.app, "config", "--get", "remote.origin.pushurl") == repo.PUSH_TRAP


def _script(wt, url):
    path = wt / "s.sh"
    path.write_text("git remote set-url --push origin %s\ngit push origin HEAD:refs/heads/script\n" % url)
    return path


def test_the_text_layer_does_not_see_a_script_on_disk(work):
    """The residue, stated as a fact: the hook has no git word to judge in `sh ./s.sh`."""
    att, wt = work
    _script(wt, att.upstream.url)
    assert _hook_allows("sh ./s.sh", wt)


@pytest.mark.xfail(strict=True, reason="known gap: a script on disk rewrites the agent-writable trap and pushes by "
                   "name; only a credential the clone does not hold closes it (t-651.logs/fix-r3.md)")
def test_a_script_on_disk_that_rewrites_the_trap_is_a_known_gap(work):
    att, wt = work
    _script(wt, att.upstream.url)
    before = refs(att.upstream.bare)
    subprocess.run("sh ./s.sh", shell=True, cwd=wt, env=agent_env(), capture_output=True, text=True, timeout=60)
    assert refs(att.upstream.bare) == before
