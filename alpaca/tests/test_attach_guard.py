"""AC-08: the git guard in an attached studio (op-062 t-651; pre-mortem X3 text layer, t-651.1 to t-651.3).

An agent's plain `git push` from app/ or from a task worktree is denied, whatever wraps it. Reads and a
commit in a task worktree stay open. The studio is chosen from the target of each call, not from the
session's folder. The old DENIED and ALLOWED lists of test_git_guard.py are pinned by digest.
"""
import ast
import hashlib
import json
import os
import subprocess
import sys
import time

import pytest

from alpaca.attach import guard
from alpaca.hooks import git_guard
from alpaca.tests.attachkit import (add_worktree, attached, git, hook_payload, studio, upstream)  # noqa: F401
from alpaca.tests.conftest import REPO

# sha256 of the source text of the DENIED and ALLOWED assignments of test_git_guard.py when this task began
PINNED_REPLAY_DIGEST = "d853536f7d20347d2555e785cbb133de27231f310271bbbf1284467c31e085b3"


@pytest.fixture
def world(attached):
    """The attached studio, a task worktree on alpaca/t-001, and a way to name the folder a command runs in."""
    wt = add_worktree(attached, "t-001")

    class World:
        studio = attached.root
        app = attached.app
        wt = None
        att = attached

        def at(self, where):
            return {"app": self.app, "wt": self.wt, ".": self.studio, "src": self.app / "src",
                    "wtsrc": self.wt / "src"}[where]

    w = World()
    w.wt = wt
    return w


def decision(command, cwd):
    return git_guard.decide(hook_payload(command, cwd))


def denied(command, cwd):
    out = decision(command, cwd)
    return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"


ATTACH_DENIED = [
    ("git push origin HEAD", "app"), ("git push", "wt"), ("git -C app push", "."),
    ("git --git-dir=app/.git push", "."), ("GIT_DIR=app/.git git push", "."), ("bash -c 'git push'", "wt"),
    ("git push origin HEAD; echo \"it's", "wt"), ("git push --force-with-lease origin alpaca/t-1", "wt"),
    ("git push origin +alpaca/t-1", "wt"), ("git push --mirror", "app"), ("git push --delete origin main", "app"),
    ("git commit -am x", "app"), ("git reset --hard", "app"), ("git merge origin/main", "app"),
    ("git rebase origin/main", "app"), ("git tag v1", "wt"), ("git worktree add ../x", "app"),
    ("git branch -D alpaca/t-1", "app"), ("git remote set-url origin https://evil", "app"),
    ("git config remote.origin.pushurl x", "wt"), ("git -c core.hooksPath=/dev/null commit -m x", "wt"),
    ("git commit --no-verify -m x", "wt"),
    # the wrappers of the build plan and the pre-mortem list
    ("gh pr create --title x --body y", "wt"), ("gh api -X POST repos/o/n/git/refs -f ref=x", "wt"),
    ("git -c alias.p=push p origin HEAD", "wt"), ("env git push", "wt"), ("command git push", "wt"),
    ("sh -c 'git push origin HEAD'", "wt"), ("bash -c \"sh -c 'git push'\"", "app"),
    ("git --git-dir app/.git --work-tree . push", "."), ("GIT_DIR=app/.git GIT_WORK_TREE=app git push", "."),
    ("cd app && git push", "."), ("(cd app; git push)", "."), ("git -C app/src push", "."), ("git push", "src"),
    ("env GIT_DIR=app/.git git push origin HEAD", "."), ("export GIT_DIR=app/.git; git push", "."),
    ("nohup git push &", "wt"), ("timeout 20 git push", "wt"), ("sudo git push", "wt"),
    ("echo origin | xargs git push", "wt"), ("eval 'git push'", "wt"), ("echo `git push`", "wt"),
    ("echo $(git push)", "wt"), ("/usr/bin/git push", "wt"), ("if true; then git push; fi", "wt"),
    ("git send-pack ../upstream.git HEAD", "wt"), ("git -C ../../../../../app push", "wtsrc"),
    ("python3 -c \"import subprocess as s; s.run(['git','push'])\"", "wt"),
    ("git commit -nm x", "wt"), ("git commit -n -m x", "wt"), ("git -c credential.helper=x fetch", "app"),
    ("git -c url.http://x/.insteadOf=https://y/ fetch", "wt"), ("git -c remote.origin.url=x fetch", "wt"),
    ("git checkout -b other", "wt"), ("git switch main", "wt"), ("git checkout main", "app"),
    ("git update-ref refs/heads/x HEAD", "wt"), ("git symbolic-ref HEAD refs/heads/x", "wt"),
    ("git filter-branch --all", "wt"), ("git reflog expire --all", "wt"), ("git remote add x https://x", "wt"),
    ("git worktree remove ../t-001", "app"), ("git branch -m x", "wt"), ("git branch --delete x", "app"),
    ("git cherry-pick abc", "app"), ("git clean -fd", "app"), ("git restore x", "app"), ("git stash", "app"),
    ("git add -A", "app"), ("git pull", "app"),
    ("gh pr merge 3", "wt"), ("gh release create v1", "wt"), ("gh repo delete o/n --yes", "wt"),
    ("gh workflow run ci.yml", "wt"), ("gh api repos/o/n/pulls --input body.json", "wt"),
    ("gh api repos/o/n/issues -F title=x", "wt"), ("gh api --method PATCH repos/o/n", "wt"),
    ("gh -R o/n pr comment 1 --body x", "wt"), ("gh alias set p 'pr create'", "wt"),
    # fix round 1 (t-651.logs/fix-r1.md): config through the environment, the same as a bad -c
    ("GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.p GIT_CONFIG_VALUE_0=push git p origin HEAD", "wt"),
    ("GIT_CONFIG_PARAMETERS=\"'alias.p'='push'\" git p", "wt"), ("export GIT_CONFIG_COUNT=1; git status", "wt"),
    ("GIT_CONFIG_GLOBAL=./x.cfg git p", "wt"), ("GIT_EXEC_PATH=/tmp/x git status", "wt"),
    ("GIT_SSH_COMMAND=./x git fetch", "wt"),
    # -c keys that name a program git runs
    ("git -c diff.external=./x diff", "wt"), ("git -c filter.a.smudge=./x checkout -- x", "wt"),
    ("git -c gpg.program=./x commit -S -m x", "wt"), ("git -c sequence.editor=./x rebase -i origin/main", "wt"),
    ("git -c Alias.P=push P", "wt"), ("git -c submodule.x.update='!./x' submodule update", "wt"),
    # git subcommands that run a command text of their own
    ("git rebase -x 'git push' origin/main", "wt"), ("git rebase --exec='git push origin HEAD' origin/main", "wt"),
    ("git submodule foreach git push", "wt"), ("git submodule foreach 'git push origin HEAD'", "wt"),
    ("git bisect run sh -c 'git push'", "wt"), ("git difftool -x 'git push' HEAD~1", "wt"),
    ("git difftool --extcmd='sh -c \"git push\"'", "wt"), ("git ls-remote --upload-pack='x' origin", "wt"),
    # programs that run their words, and a shell reading a pipe
    ("watch git push", "wt"), ("find . -maxdepth 0 -exec git push \\;", "wt"), ("parallel git ::: push", "wt"),
    ("printf 'git push origin HEAD' | sh", "wt"), ("echo 'git push' | bash", "wt"), ("bash -lc 'git push'", "wt"),
    ("bash --login -c 'git push'", "wt"), ("zsh -o x -c 'git push'", "wt"),
    ("bash <<'EOF'\ngit push\nEOF", "wt"), ("git push 2>&1 | tee log", "wt"),
    # the word git spelled with quotes or a backslash, which the shell joins back
    ("g\\it push", "wt"), ("g''it push", "wt"), ("\"g\"it push origin HEAD", "app"), ("git pu\\sh", "wt"),
    ("\\git push", "wt"), ("'git' push", "wt"), ("git 'push'", "wt"), ("exec git push", "wt"),
    # a folder the text does not give: cd -, popd; $PWD is the folder the command reached
    ("cd app; cd ..; cd -; git push", "."), ("cd app; popd; git push", "."), ("GIT_DIR=$PWD/app/.git git push", "."),
    ("git -C $PWD push", "wt"), ("git -C app -C src push", "."), ("git --work-tree=app push", "."),
    # other ways out: external push subcommands, mail, a notes ref, index writes in the clone
    ("git lfs push origin HEAD", "wt"), ("git subtree push --prefix=x origin main", "wt"),
    ("git send-email HEAD~1", "wt"), ("git imap-send", "wt"), ("git notes add -m x", "app"),
    ("git checkout-index -a -f", "app"), ("git read-tree HEAD", "app"),
    # gh: only read verbs are open (an allow-list), so unlisted write verbs are denied too
    ("gh pr checkout 3", "app"), ("gh cache delete --all", "wt"), ("gh project create --title x", "wt"),
    ("gh auth setup-git", "wt"), ("gh codespace ssh", "wt"), ("gh ruleset create", "wt"),
    ("gh api graphql -f query='mutation{}'", "wt"), ("gh api -XPOST repos/o/n/forks", "wt"),
    ("gh issue create -t x -b y", "wt"), ("gh release list", "wt"), ("gh pr", "wt"),
]
ATTACH_ALLOWED = [
    ("git commit -am x", "wt"), ("git status", "app"), ("git diff origin/main", "wt"),
    ("git log --oneline", "app"), ("git fetch", "app"), ("git fetch origin", "wt"),
    ("git status --short", "wt"), ("git show HEAD", "app"), ("git rev-parse HEAD", "wt"), ("git ls-files", "app"),
    ("git worktree list", "app"), ("git remote -v", "app"), ("git remote show origin", "app"),
    ("git config --get remote.origin.url", "wt"), ("git config --list", "app"), ("git branch -a", "app"),
    ("git branch --show-current", "wt"), ("git stash list", "app"), ("git tag -l", "app"), ("git tag", "app"),
    ("git commit -m 'git push is not run here'", "wt"), ("echo git push", "wt"), ("grep -n 'git push' notes.txt", "wt"),
    ("git add -A && git commit -m x", "wt"), ("git diff --stat; git log -3", "app"), ("git checkout -- src/main.py", "wt"),
    ("git log -n 5", "wt"), ("git commit -m '-n fix'", "wt"), ("git blame src/main.py", "app"),
    ("git clean -n", "app"), ("git cat-file -p HEAD", "wt"), ("git -C app status", "."), ("git -C app log -1", "."),
    ("git --no-pager diff", "wt"), ("git commit -a -m x", "wt"), ("git reset -q", "wt"),
    ("gh pr view 3", "wt"), ("gh pr list", "wt"), ("gh api repos/o/n/pulls", "wt"), ("gh auth status", "wt"),
    ("gh api -X GET repos/o/n", "wt"), ("ls -la", "wt"), ("git push", "."),
    # fix round 1: the new rules leave these open
    ("git rebase -x 'make test' origin/main", "wt"), ("git rebase --exec='make test' origin/main", "wt"),
    ("git submodule status", "wt"), ("git -c submodule.recurse=true status", "wt"),
    ("git -c merge.conflictstyle=diff3 merge origin/main", "wt"), ("PAGER=cat git log -1", "wt"),
    ("gh run view 1", "wt"), ("gh pr diff 3", "wt"), ("gh pr checks 3", "wt"), ("gh repo view", "wt"),
    ("gh search repos x", "wt"), ("gh issue list", "wt"), ("gh --version", "wt"), ("git lfs ls-files", "wt"),
    ("cd app && git status", "."), ("git -c color.ui=never log -1", "wt"), ("git bisect start", "wt"),
    ("gh variable get X", "wt"), ("git commit-tree HEAD^{tree} -m x", "app"), ("echo hi | bash", "wt"),
    ("git notes list", "app"), ("watch git status", "wt"), ("git update-index --refresh", "wt"),
]


@pytest.mark.parametrize("cmd,cwd", ATTACH_DENIED)
def test_attach_guard_denies(world, cmd, cwd):
    out = decision(cmd, world.at(cwd))
    assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny", cmd
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "attach guard" in reason and "alpaca attach handoff" in reason and "worktrees" in reason


@pytest.mark.parametrize("cmd,cwd", ATTACH_ALLOWED)
def test_attach_guard_allows(world, cmd, cwd):
    assert decision(cmd, world.at(cwd)) is None, cmd


def test_a_push_in_the_studios_own_repository_is_not_attach_business(world):
    """The studio repo is the human's; attach only guards the attached clone. `git push` from the studio root
    was in ATTACH_ALLOWED above and the guard says so by returning no attach reason."""
    assert guard.decide("git push", world.studio) is None
    assert guard.decide("git -C %s push" % world.studio, world.app) is None


def test_commit_is_denied_on_a_branch_that_is_not_a_work_branch(world):
    git(world.wt, "checkout", "-q", "--detach")
    assert denied("git commit -am x", world.wt)
    git(world.wt, "checkout", "-q", "-b", "scratch")
    assert denied("git commit -am x", world.wt)
    # integration of t-648 and t-651 (op-062): the work branch of a folder is the one its `attach-branch` event
    # names, `<prefix><task>`, proved by alpaca.attach.proven; another name with the prefix is not it
    git(world.wt, "checkout", "-q", "-b", "alpaca/t-002")
    assert denied("git commit -am x", world.wt)
    git(world.wt, "checkout", "-q", "alpaca/t-001")
    assert not denied("git commit -am x", world.wt)


def test_a_commit_in_the_clone_is_denied_even_on_a_work_branch_name(world):
    git(world.app, "checkout", "-q", "-b", "alpaca/t-009")
    assert denied("git commit -am x", world.app)


def test_the_denial_is_recorded_for_the_studio(world):
    path = world.studio / git_guard.DENIALS
    assert not path.exists()
    assert denied("git push", world.wt)
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert len(lines) == 1 and "git push" in lines[0]["reason"]


# ---- pre-mortem t-651.2: the studio comes from the target, not from the session folder --------------------------

@pytest.fixture
def other(tmp_path):
    """Another Alpaca project: a folder with an ALPACA-MANIFEST and no attach block."""
    folder = tmp_path / "other-project"
    folder.mkdir()
    (folder / "ALPACA-MANIFEST").write_text("# manifest\n")
    return folder


def test_guard_classifies_by_target_path(world, other):
    for cmd in ("git -C %s push origin HEAD" % world.app, "git -C %s push" % world.wt,
                "cd %s && git push" % world.app, "git --git-dir=%s/.git push" % world.app,
                "GIT_DIR=%s/.git git push" % world.app):
        assert denied(cmd, other), cmd
    assert not denied("git -C %s status" % world.app, other)
    assert not denied("git push", other)


def test_a_session_outside_every_project_is_judged_by_the_target_too(world, tmp_path):
    bare_cwd = tmp_path / "nowhere"
    bare_cwd.mkdir()
    assert denied("git -C %s push" % world.app, bare_cwd)
    assert guard.decide("git -C %s log -1" % world.app, bare_cwd) is None


def test_a_path_in_a_variable_is_followed_or_the_push_is_denied(world, other, monkeypatch):
    monkeypatch.setenv("APP_DIR", str(world.app))
    assert denied("git -C $APP_DIR push", other)
    assert denied("APP=%s; git -C \"$APP\" push" % world.app, other)
    assert denied("git -C $UNKNOWN_DIR push", world.wt)          # cannot be read: push is refused while involved
    assert not denied("git -C $UNKNOWN_DIR status", world.wt)


def test_classify(world, tmp_path):
    s = str(world.studio)
    assert guard.classify(world.app, s) == "attached-main"
    assert guard.classify(world.app / "src", s) == "attached-main"
    assert guard.classify(world.app / ".git", s) == "attached-main"
    assert guard.classify(world.wt, s) == "attached-worktree"
    assert guard.classify(world.wt / "src", s) == "attached-worktree"
    assert guard.classify(world.app / ".git" / "worktrees" / "t-001", s) == "attached-worktree"
    assert guard.classify(world.studio, s) == "studio"
    assert guard.classify(world.studio / "design", s) == "studio"
    assert guard.classify(tmp_path, s) == "other"
    assert guard.classify(world.studio / ".alpaca" / "attach" / "worktrees" / "t-404", s) == "attached-worktree"


def test_a_nested_repository_inside_the_clone_is_not_the_clone(world):
    nested = world.app / "vendor" / "lib"
    nested.mkdir(parents=True)
    git(nested, "init", "-q")
    assert guard.classify(nested, str(world.studio)) == "studio"
    assert not denied("git push", nested)


def test_studios_are_found_by_state_file_only(world, tmp_path):
    assert guard.studios_of(world.wt / "src") == [os.path.realpath(world.studio)]
    assert guard.studios_of(tmp_path) == []


def test_a_plain_project_is_never_touched(project):
    """No attach state anywhere: every command, push included, passes the attach layer with no answer."""
    for cmd in ("git push", "git push origin HEAD", "gh pr create", "git commit --no-verify -m x"):
        assert guard.check(cmd, str(project)) is None


# ---- unreadable commands ----------------------------------------------------------------------------------------

def test_an_unreadable_command_is_denied_while_a_studio_is_involved(world, tmp_path):
    # the older rules cannot read these lines either and fail open in the hook; the attach rules decide first.
    # Fix round 3 (t-651.logs/fix-r3.md, pick-a0 A3/A4): the deny no longer needs the word push or gh, since
    # the part the lexer cannot read could hold any git call.
    assert guard.check("git commit -m \"it's", world.wt)
    assert guard.check("git push origin HEAD; echo \"it's", world.wt)
    assert guard.check("gh pr create --title \"x", world.wt)
    assert guard.check("git -C %s push; echo 'x" % world.app, world.studio)
    assert guard.check("git status; echo 'x", world.app)
    assert guard.check("git status; echo 'x", tmp_path) is None            # no studio involved


# ---- the old replay lists are untouched --------------------------------------------------------------------------

def test_guard_replay_lists_untouched():
    path = os.path.join(REPO, "alpaca", "tests", "test_git_guard.py")
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    parts = [ast.get_source_segment(source, node) for node in ast.parse(source).body
             if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in ("DENIED", "ALLOWED")]
    assert len(parts) == 2
    assert hashlib.sha256("\n".join(parts).encode()).hexdigest() == PINNED_REPLAY_DIGEST


def test_the_old_replay_lists_keep_their_outcome_in_an_attached_studio(world):
    """The studio's own tree is still guarded by the old rules, and the old DENIED commands still deny."""
    from alpaca.tests import test_git_guard as old
    for cmd in old.DENIED:
        assert guard.check(cmd, world.studio) is None            # the studio itself is not an attach target
        assert denied(cmd, world.studio), cmd                    # and the older rule still denies it there
    for cmd in old.ALLOWED:
        assert guard.check(cmd, world.studio) is None


# ---- the hook as a process, under the wall ceiling -------------------------------------------------------------

def test_hook_process_denies_a_push_in_an_attached_studio_within_the_ceiling(world):
    env = dict(os.environ, PYTHONPATH=REPO)
    started = time.monotonic()
    p = subprocess.run([sys.executable, "-m", "alpaca.hooks.git_guard"],
                       input=json.dumps(hook_payload("git push origin HEAD", world.wt)), capture_output=True,
                       text=True, cwd=str(world.studio), env=env, timeout=30)
    assert time.monotonic() - started < 20.0
    assert p.returncode == 0
    assert json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    p = subprocess.run([sys.executable, "-m", "alpaca.hooks.git_guard"],
                       input=json.dumps(hook_payload("git status", world.wt)), capture_output=True, text=True,
                       cwd=str(world.studio), env=env, timeout=30)
    assert p.returncode == 0 and p.stdout == ""


def test_a_failure_inside_the_attach_rules_lets_the_call_go_ahead(world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("broken")
    monkeypatch.setattr(guard, "check", boom)
    assert decision("git push", world.wt) is None


def test_a_hook_run_with_no_git_or_gh_in_the_command_never_loads_the_attach_rules(world, monkeypatch):
    monkeypatch.setattr(guard, "check", lambda *a, **k: (_ for _ in ()).throw(AssertionError("loaded")))
    assert decision("ls -la", world.wt) is None
    assert decision("echo hello", world.wt) is None


def test_a_task_worktree_outside_the_default_place_is_still_the_clones_by_git(world, tmp_path):
    """A linked worktree of the clone made somewhere else keeps the clone's common git folder, which the
    rule follows when the path is inside the studio; outside the studio it is not guarded by path alone."""
    inside = add_worktree(world.att, "t-002")            # t-648's cut: the branch is recorded and proved
    assert denied("git push", inside)
    assert not denied("git commit --allow-empty -m x", inside)


def test_a_folder_made_by_plain_git_worktree_add_is_not_a_work_folder(world):
    """Integration of t-648 and t-651 (op-062): git listing a folder on an `alpaca/` branch is not proof. With no
    `attach-branch` event for the task, or a `.git` file that names another admin folder, the guard keeps
    writes closed; reads stay open."""
    made = world.studio / ".alpaca" / "attach" / "worktrees" / "t-003"
    git(world.app, "worktree", "add", "-q", "-b", "alpaca/t-003", str(made), "origin/main")
    assert denied("git commit --allow-empty -m x", made)
    assert denied("git add -A", made)
    assert denied("git push", made)
    assert not denied("git status", made)
    # a proved folder whose .git file is rewritten to the clone's own git folder: a commit would land on the
    # clone's checked-out branch, so it is denied
    (world.wt / ".git").write_text("gitdir: %s\n" % (world.app / ".git"))
    assert denied("git commit --allow-empty -m x", world.wt)


# ---- a git alias from the user's own config -----------------------------------------------------------------------

def test_an_alias_that_stands_for_a_push_is_followed(world, tmp_path, monkeypatch):
    config = tmp_path / "global-gitconfig"
    config.write_text("[alias]\n\tpp = push\n\tst = status\n\tsh = !git push origin HEAD\n\twho = !echo hi\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    assert denied("git pp origin HEAD", world.wt)
    assert denied("git sh", world.wt)
    assert not denied("git st", world.wt)
    assert not denied("git who", world.wt)
    assert not denied("git pp origin HEAD", world.studio)          # the studio's own repo is not attach's business


def test_an_alias_that_runs_a_shell_is_followed_and_an_unreadable_one_is_denied(world, tmp_path, monkeypatch):
    """Fix round 1: a `!` alias whose text starts a nested shell used to raise inside the rules, and the hook
    let the call go ahead (fail open). Now the nested text is read, and text that cannot be read but mentions
    push is denied, as at the top level."""
    config = tmp_path / "global-gitconfig"
    config.write_text("[alias]\n\tsp = !bash -c 'git push'\n\tsq = !sh -c 'git push\n\tok = !bash -c 'git status'\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    assert denied("git sp", world.wt)
    assert denied("git sq", world.wt)
    assert not denied("git ok", world.wt)


# ---- fix round 3: the rules fail closed for an attached target (t-651.logs/think/pick-a0.md, fix-r3.md) ----------
# Rows are taken from the frozen probe files of rounds 2 and 3 (attacks/test_r2_guard_decisions.py,
# investigate-r2-probe/test_inv_r2_probe.py, think/approach-0-probe, think/pick-a0-probe) and the pick-a0 step lists.

FAIL_CLOSED_DENIED = [
    # words that xargs, parallel or find add (open calls)
    ("echo push | xargs git", "wt"), ("printf 'push\\n' | xargs -n1 git", "wt"), ("echo push | xargs -I{} git {}", "wt"),
    ("parallel git {} ::: push", "wt"), ("echo push | parallel git", "wt"),
    ("echo set-url --push origin /x | xargs git remote", "wt"), ("echo remote.origin.pushurl /x | xargs git config", "wt"),
    ("echo --no-verify | xargs git commit -m x", "wt"), ("echo -n | xargs git commit -m x", "wt"),
    ("echo -D alpaca/t-001 | xargs git branch", "wt"), ("echo add ../x | xargs git worktree", "wt"),
    ("echo v1 | xargs git tag", "wt"), ("echo 'git push origin HEAD' | xargs -I{} sh -c '{}'", "wt"),
    ("echo 'git push origin HEAD' | xargs -0 sh -c", "wt"), ("xargs -a cmds.txt git", "wt"),
    ("parallel -a cmds.txt git", "wt"), ("find . -maxdepth 0 -exec git {} origin HEAD \\;", "wt"),
    ("echo -O./x | xargs git grep x", "wt"),
    # a subcommand or program word built at run time
    ("S=push; git $S origin HEAD", "wt"), ("git $(echo push) origin HEAD", "wt"), ("git ${SUB:-push} origin HEAD", "wt"),
    ("git $'push' origin HEAD", "wt"), ("git -C . $(echo push)", "wt"), ("G=git; $G push origin HEAD", "wt"),
    ("$(echo git) push origin HEAD", "wt"), ("X='git push origin HEAD'; $X", "wt"),
    ("X='git push origin HEAD'; eval $X", "wt"), ("git pu{sh,} origin HEAD", "wt"), ("git pus? origin", "wt"),
    ("git-lfs push", "wt"), ("git -c \"$(echo core.hooksPath)=/x\" status", "wt"),
    ("git commit $(echo --no-verify) -m x", "wt"),
    # wrapper options that hide the call or move the target
    ("env -S 'git push origin HEAD'", "wt"), ("env -C app git push origin HEAD", "."),
    ("env --chdir=app git push origin HEAD", "."), ("env --chdir app git push", "."), ("sudo -D app git push", "."),
    ("timeout 5 env -S 'git push'", "wt"), ("sudo git push", "wt"),
    # a program git runs, named by a variable, a -c key or an option
    ("GIT_EXTERNAL_DIFF='git push origin HEAD' git diff HEAD~1", "wt"),
    ("GIT_SEQUENCE_EDITOR='git push origin HEAD' git rebase -i HEAD~1", "wt"),
    ("GIT_EDITOR='git push origin HEAD' git commit --amend", "wt"),
    ("git grep -O'git push origin HEAD' x", "wt"), ("git grep --open-files-in-pager='git push origin HEAD' x", "wt"),
    ("git -c tar.foo.command='git push origin HEAD' archive --format=foo HEAD", "wt"),
    ("git -c trailer.x.command='git push origin HEAD' commit -m x --trailer x=1", "wt"),
    ("GIT_TRACE=/tmp/x git status", "wt"),
    # subcommands that are not on the table
    ("git credential fill", "wt"), ("git svn dcommit", "wt"), ("git foo-bar", "wt"), ("git maintenance start", "wt"),
    # programs that carry a command text: awk, sed, shells not on the old list, busybox
    ("awk 'BEGIN{system(\"git push origin HEAD\")}'", "wt"), ("echo x | sed 's/.*/git push origin HEAD/e'", "wt"),
    ("fish -c 'git push origin HEAD'", "wt"), ("busybox sh -c 'git push origin HEAD'", "wt"),
    ("tcsh -c 'git push origin HEAD'", "wt"),
    # option abbreviations and options that write a file of the caller's choosing
    ("git commit -am x --no-veri", "wt"), ("git commit --no-verif -m x", "wt"), ("git fetch --update-h origin", "wt"),
    ("git ls-remote --upload-p=./x origin", "wt"), ("git clone --upload-p=./x url", "wt"),
    ("git branch --del alpaca/t-1", "wt"), ("git branch --mo a b", "wt"), ("git tag --del v1", "wt"),
    ("git config --unset-a x.y", "wt"), ("git config --rep x.y z", "wt"), ("git log --output=.git/config", "wt"),
    ("git diff --output=hook", "wt"), ("git show --output=x HEAD", "wt"), ("git format-patch -o /tmp/x HEAD~1", "wt"),
    ("git diff-tree --output=x HEAD", "wt"), ("git shortlog --output=x HEAD", "wt"),
    ("git range-diff --output=x HEAD~1 HEAD HEAD", "wt"), ("git diff --output=.git/config", "wt"),
    ("git archive -o .git/hooks/x HEAD", "wt"), ("git archive --format=tar --outp=x HEAD", "wt"),
    # a shell or interpreter fed by a here-document, a here-string or `-`, and a substitution in an unquoted body
    ("bash <<< 'git push origin HEAD'", "wt"), ("python3 - <<'EOF'\nimport os; os.system('git status')\nEOF", "wt"),
    ("cat <<EOF\n$(git push origin HEAD)\nEOF", "wt"), ("git log $X -1", "wt"),
    ("pwsh -Co 'git push origin HEAD'", "wt"), ("python3 x.py --run 'git tag v1'", "wt"),
]
FAIL_CLOSED_ALLOWED = [
    ("git ls-files | xargs git log --oneline --", "wt"), ("echo src/new.py | xargs git diff --", "wt"),
    ("echo src/new.py | xargs git add", "wt"), ("watch git status", "wt"),
    ("cd $(git rev-parse --show-toplevel) && git status", "wt"),
    ("for f in $(git ls-files); do git log -1 -- $f; done", "wt"), ("git log --format=\"$(echo %h)\" -1", "wt"),
    ("$(git rev-parse --show-toplevel)/bin/tool status", "wt"), ("git config --get-all remote.origin.url", "wt"),
    ("git archive HEAD", "wt"), ("git ls-remote origin", "wt"), ("git remote", "wt"), ("git commit -m x", "wt"),
    ("git gc", "wt"), ("git help push", "wt"), ("git log -1 | awk '{print $2}'", "wt"),
    ("git status --short | sed 's/^ M //'", "wt"), ("grep -rn 'git push' docs", "wt"),
    ("git diff --name-only | sort | uniq", "wt"), ("git commit-tree HEAD^{tree} -m x", "wt"),
    ("env LANG=C git log -1", "wt"), ("env -C app git status", "."), ("nice -n 5 git status", "wt"),
    ("timeout 20 git fetch", "wt"), ("GIT_TRACE=1 git status", "wt"), ("git log --no-renames -1", "wt"),
    ("git diff --stat", "wt"), ("git fetch --prune", "wt"), ("git show --format=%H", "wt"),
    ("git diff -O /tmp/ord", "wt"), ("python -c \"print(1)\"", "wt"), ("git --version", "wt"),
    ("git -C $NOPE log -1", "wt"), ("git archive --format=zip --prefix=x/ HEAD", "wt"),
    # ordinary work in a task worktree, including the usual here-document commit forms
    ("git commit -m \"$(cat <<'EOF'\nFix the parser's bug (again)\nEOF\n)\"", "wt"),
    ("git commit -F - <<'EOF'\nIt's done\nEOF", "wt"), ("cat <<'EOF' > notes.md\nit's about git\nEOF", "wt"),
    ("git log --grep \"$PATTERN\" -1", "wt"), ("git status -sb", "wt"), ("git diff --cached", "wt"),
    ("git log --oneline --graph -n 10", "wt"), ("git add .", "wt"), ("git commit --amend --no-edit", "wt"),
    ("git reset --soft HEAD~1", "wt"), ("git restore --staged x", "wt"), ("git stash push -m x", "wt"),
    ("git stash pop", "wt"), ("git fetch origin main", "wt"), ("git merge --ff-only origin/main", "wt"),
    ("git rebase origin/main", "wt"), ("git rev-parse --abbrev-ref HEAD", "wt"), ("git log origin/main..HEAD", "wt"),
    ("git diff origin/main...HEAD --stat", "wt"), ("git ls-files | wc -l", "wt"), ("git grep -n foo", "wt"),
    ("git blame -L 1,10 x", "wt"), ("git rm --cached x", "wt"), ("git mv a b", "wt"), ("git clean -n -d", "wt"),
    ("git describe --tags", "wt"), ("git shortlog -sn", "wt"), ("git log --author=x --since=2.weeks", "wt"),
    ("git diff --name-status", "wt"), ("git diff --check", "wt"), ("git format-patch -1 HEAD --stdout", "wt"),
    ("git log -p -- x", "wt"), ("git log --pretty=format:'%h %s'", "wt"), ("git diff --word-diff", "wt"),
    ("git log --follow -- x", "wt"), ("git show HEAD:src/main.py", "wt"), ("git rev-list --count HEAD", "wt"),
    ("git merge-base HEAD origin/main", "wt"), ("git cherry-pick abc", "wt"), ("git am --abort", "wt"),
    ("git checkout -- x", "wt"), ("git commit -m \"$MSG\"", "wt"), ("cd src && git diff -- $FILE", "wt"),
]


@pytest.mark.parametrize("cmd,cwd", FAIL_CLOSED_DENIED)
def test_fail_closed_denies(world, cmd, cwd):
    out = decision(cmd, world.at(cwd))
    assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny", cmd
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "attach guard" in reason and "alpaca attach handoff" in reason and "worktrees" in reason


@pytest.mark.parametrize("cmd,cwd", FAIL_CLOSED_ALLOWED)
def test_fail_closed_keeps_ordinary_work_open(world, cmd, cwd):
    # the attach layer's answer; two here-document rows are text the older main-tree rules cannot lex, which
    # the real hook runs inside its fail-open wrapper (git_guard.main)
    assert guard.check(cmd, world.at(cwd)) is None, cmd


def test_git_dir_in_the_session_environment_is_the_target(world, tmp_path, monkeypatch):
    """GIT_DIR exported before the session started (not in the text) still names the attached clone."""
    monkeypatch.setenv("GIT_DIR", str(world.app / ".git"))
    assert denied("git push", tmp_path)
    assert denied("git push origin HEAD", tmp_path)
    assert guard.decide("git status", tmp_path) is None
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.setenv("GIT_WORK_TREE", str(world.app))
    assert denied("git push", tmp_path)


def test_no_studio_no_change_for_the_new_rules(project):
    for cmd in ("echo push | xargs git", "G=git; $G push", "awk 'BEGIN{system(\"git push\")}'", "git foo-bar",
                "git commit -m \"it's", "GIT_TRACE=/tmp/x git status"):
        assert guard.check(cmd, project) is None, cmd


# ---- the error paths: a rule that fails or a lookup that fails is a deny while a studio is involved ----------------

def test_a_failing_alias_lookup_is_a_deny(world, monkeypatch):
    def boom(*a, **k):
        raise OSError("git is gone")
    monkeypatch.setattr(guard.gitops, "run", boom)
    assert "alias lookup" in guard.decide("git st", world.wt)
    assert guard.decide("git status", world.wt) is None


def test_a_rule_that_raises_is_a_deny_inside_a_studio_and_nothing_outside(world, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("broken rule")
    monkeypatch.setattr(guard, "_judge_call", boom)
    assert "could not judge" in guard.decide("git status", world.wt)
    assert guard.check("git status", tmp_path) is None
    monkeypatch.undo()
    monkeypatch.setattr(guard, "_calls", boom)
    assert "could not read" in guard.decide("git status", world.wt)
    assert guard.check("git status", tmp_path) is None


# ---- the table self-check: every CLOSED and OPEN entry has an allowed row, and a denied row where one exists -----

def test_every_table_entry_has_an_allowed_and_a_denied_row(world):
    allowed = lambda cmd, where=world.wt: guard.check(cmd, where) is None         # noqa: E731
    for sub in sorted(guard._READS | guard._OBJECT_WRITERS | guard._UPKEEP):
        assert allowed("git %s" % sub), sub
    for sub in sorted(guard._OUTPUT_WRITERS):
        assert not allowed("git %s --output=x" % sub) and not allowed("git %s --outp=x" % sub), sub
    for sub in sorted(guard._FETCH_LIKE):
        assert not allowed("git %s --upload-pack=./x origin" % sub), sub
    for sub in sorted(guard._GATED):
        assert allowed("git %s" % sub), sub
        assert not allowed("git %s" % sub, world.app), sub
    for sub in sorted(guard._OPEN_ANY):
        assert allowed("echo x | xargs git %s" % sub), sub
    for sub in sorted(guard._OPEN_ANY & guard._GATED):
        assert not allowed("echo x | xargs git %s" % sub, world.app), sub
    for sub in sorted(guard._OPEN_DASHDASH):
        assert allowed("echo x | xargs git %s --" % sub), sub
        assert not allowed("echo x | xargs git %s" % sub), sub
    forms = {
        "remote": ("git remote -v", "git remote add x https://x"),
        "config": ("git config --get user.name", "git config user.name x"),
        "worktree": ("git worktree list", "git worktree add ../x"),
        "branch": ("git branch -a", "git branch -D x"),
        "tag": ("git tag -l", "git tag v1"),
        "stash": ("git stash list", "git stash"),
        "notes": ("git notes list", "git notes add -m x"),
        "symbolic-ref": ("git symbolic-ref HEAD", "git symbolic-ref HEAD refs/heads/x"),
        "reflog": ("git reflog", "git reflog expire --all"),
        "submodule": ("git submodule status", "git submodule update"),
        "lfs": ("git lfs ls-files", "git lfs push origin HEAD"),
        "bisect": ("git bisect start", "git bisect run sh -c 'git push'"),
        "clean": ("git clean -n", "git clean -fd"),
        "checkout": ("git checkout -- x", "git checkout main"),
        "maintenance": ("git maintenance run", "git maintenance start"),
    }
    assert set(forms) == guard._FORMS
    for sub, (ok, bad) in forms.items():
        where = world.app if sub in ("stash", "clean") else world.wt
        assert allowed(ok, where) and not allowed(bad, where), sub
    for sub in ("credential", "hook", "bundle", "svn", "p4", "cvs", "interpret-trailers", "difftool", "mergetool",
                "rerere", "pack-objects", "unpack-objects", "no-such-command"):
        assert not allowed("git %s" % sub), sub
    for sub in guard._ALWAYS_DENIED:
        assert not allowed("git %s" % sub), sub
