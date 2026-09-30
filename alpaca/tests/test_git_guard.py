"""The git guard denies working-tree-rewriting git commands in the shared main tree, and only there."""
import json
import os
import subprocess
import sys

import pytest

from alpaca.hooks import git_guard
from alpaca.tests.conftest import REPO


def _git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True)


@pytest.fixture
def repo(project):
    _git("init", "-q", "-b", "main", project)
    for key, value in [("user.name", "T"), ("user.email", "t@localhost"), ("commit.gpgsign", "false")]:
        _git("-C", project, "config", key, value)
    _git("-C", project, "add", "ALPACA-MANIFEST")
    _git("-C", project, "commit", "-qm", "init")
    return project


def _bash(command, cwd):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


DENIED = [
    "git stash",
    "git stash -q",
    "git stash push -m x",
    "git stash pop",
    "git reset --hard HEAD",
    "git checkout -- README.md",
    "git checkout .",
    "git checkout -f main",
    "git restore alpaca/cli.py",
    "git restore --worktree --staged x",
    "git clean -fd",
    "git clean -xdf",
    "git switch --discard-changes main",
    "ls; git stash -q 2>/dev/null; echo done",
    "true && GIT_TRACE=0 git stash",
]

ALLOWED = [
    "git stash list | head -3",
    "git stash show --stat stash@{0}",
    "git status --short",
    "git diff --stat",
    "git reset -q",
    "git reset --soft HEAD~1",
    "git checkout -b feature",
    "git restore --staged x",
    "git clean -n",
    "git commit -m 'git stash is not run here'",
    "echo git stash",
    "grep -n 'git reset --hard' notes.txt",
]


@pytest.mark.parametrize("command", DENIED)
def test_denies_rewrites_in_the_main_tree(repo, command):
    decision = git_guard.decide(_bash(command, repo))
    assert decision and decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "git guard" in decision["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("command", ALLOWED)
def test_allows_reads_and_index_only_commands(repo, command):
    assert git_guard.decide(_bash(command, repo)) is None


def test_cd_and_dash_c_resolve_the_target(repo, tmp_path):
    other = tmp_path / "elsewhere"
    _git("init", "-q", str(other))
    assert git_guard.decide(_bash("cd %s && git stash" % other, repo)) is None
    assert git_guard.decide(_bash("git -C %s reset --hard" % other, repo)) is None
    assert git_guard.decide(_bash("cd %s; git -C %s stash" % (other, repo), repo))


def test_linked_worktree_is_allowed(repo, tmp_path):
    wt = os.path.join(repo, ".claude", "worktrees", "w1")
    _git("-C", repo, "worktree", "add", "-q", "-b", "w1", wt)
    assert git_guard.decide(_bash("git stash", wt)) is None
    assert git_guard.decide(_bash("git -C .claude/worktrees/w1 reset --hard", repo)) is None


def test_other_tools_and_non_git_commands_pass(repo):
    assert git_guard.decide({"tool_name": "Read", "tool_input": {"file_path": "x"}, "cwd": repo}) is None
    assert git_guard.decide(_bash("ls -la", repo)) is None


def test_hook_prints_deny_json_and_exits_zero(repo):
    env = dict(os.environ, PYTHONPATH=REPO)
    p = subprocess.run([sys.executable, "-m", "alpaca.hooks.git_guard"], input=json.dumps(_bash("git stash", repo)),
                       capture_output=True, text=True, cwd=repo, env=env, timeout=30)
    assert p.returncode == 0
    assert json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    p = subprocess.run([sys.executable, "-m", "alpaca.hooks.git_guard"], input=json.dumps(_bash("git status", repo)),
                       capture_output=True, text=True, cwd=repo, env=env, timeout=30)
    assert p.returncode == 0 and p.stdout == ""


def test_bringing_in_a_branch_that_changed_the_live_record_is_denied(repo):
    db = os.path.join(repo, ".alpaca", "alpaca.db")
    open(db, "w").write("one")
    _git("-C", repo, "add", "-f", ".alpaca/alpaca.db")
    _git("-C", repo, "commit", "-qm", "tracked record")
    _git("-C", repo, "branch", "old")
    _git("-C", repo, "checkout", "-q", "old")
    open(db, "w").write("two")
    _git("-C", repo, "commit", "-qam", "branch changed record")
    _git("-C", repo, "checkout", "-q", "main")
    _git("-C", repo, "rm", "-q", "--cached", ".alpaca/alpaca.db")
    _git("-C", repo, "commit", "-qm", "untrack record")
    _git("-C", repo, "branch", "clean")
    for command in ("git merge old", "git cherry-pick old", "git checkout old", "git switch old", "git pull"):
        assert git_guard.decide(_bash(command, repo)), command
    assert git_guard.decide(_bash("git merge clean", repo)) is None
    assert git_guard.decide(_bash("git checkout clean", repo)) is None
