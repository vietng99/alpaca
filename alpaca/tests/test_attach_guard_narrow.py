"""Attach guard false positives (op-062 t-676).

t-651 made the text guard fail closed for an attached target, and its proof report (t-651.md, "Deviations and
issues") lists ordinary commands that closed with it: `pytest -k git`, `make git-hooks`, `watch git log
--oneline`, a `for` loop with `git add $f`, `export PATH=$PATH:/x; git status`. This file pins the narrowing
that opens them again and, next to every opened form, the near forms that must stay denied (a push, a write to
the attached repository, the options that write a file or run a program).

Rows for the pre-mortem attacks of t-651 live in t-651.logs/attacks and are unchanged.
"""
from __future__ import annotations

import pytest

from alpaca.attach import guard
from alpaca.hooks import git_guard
from alpaca.tests.attachkit import add_worktree, attached, hook_payload, studio, upstream  # noqa: F401


@pytest.fixture
def world(attached):
    """The attached studio, a task worktree on alpaca/t-001, and a way to name the folder a command runs in."""
    wt = add_worktree(attached, "t-001")

    class World:
        studio = attached.root
        app = attached.app
        att = attached

        def at(self, where):
            return {"app": self.app, "wt": self.wt, ".": self.studio}[where]

    w = World()
    w.wt = wt
    return w


# ---- commands that only mention git, or read it, and must go through --------------------------------------------

ALLOWED = [
    # the done bar: reads in the attached clone
    ("git log", "app"), ("git status", "app"), ("git diff", "app"), ("git log --oneline -5", "app"),
    ("git diff --stat HEAD~1", "app"), ("git log -p -- README.md", "app"), ("git status --short", "app"),
    ("git log | head -5", "app"), ("git status && git diff", "app"), ("git log", "wt"), ("git diff", "wt"),
    # a test runner whose words are patterns and names
    ("pytest -k git", "wt"), ("pytest -k git", "app"), ("pytest -k \"git and not push\" -q", "wt"),
    ("pytest tests/test_x.py -k 'git or gh'", "wt"), ("pytest -k git -p no:cacheprovider --basetemp=/x -n 4", "wt"),
    ("py.test -k git", "wt"), ("python -m pytest -k git", "wt"), ("pytest -k git | tail -3", "wt"),
    ("pytest -k git; git status", "wt"), ("nice -n 10 pytest -k git", "wt"), ("pytest -k git", "."),
    # make with flags and target names only
    ("make git-hooks", "wt"), ("make -j4 git-hooks", "wt"), ("make -C app git-hooks", "."),
    ("make -k -s git-hooks git-status", "wt"), ("make --keep-going git-hooks", "wt"), ("make -n git-push", "wt"),
    # programs that run the words they are given and add none, with plain words
    ("watch git log --oneline", "wt"), ("watch -n 2 git log --stat", "wt"), ("watch git diff --stat HEAD~1", "wt"),
    ("flock /tmp/lock git log --oneline", "wt"), ("strace -f git log --oneline", "wt"),
    ("watch -n1 git status --short", "wt"),
    # a loop variable among the words of a command no option of which writes a file or runs a program
    ("for f in a b; do git add $f; done", "wt"), ("for f in a b; do git status $f; done", "wt"),
    ("for f in a b; do git rm --cached $f; done", "wt"), ("for f in a b; do git restore $f; done", "wt"),
    ("for f in a b; do git ls-files $f; done", "wt"), ("for f in a b; do git diff -- $f; done", "wt"),
    # a PATH that only gets folders added behind the ones searched now
    ("export PATH=$PATH:/x; git status", "wt"), ("PATH=$PATH:/x git status", "wt"),
    ("PATH=${PATH}:/opt/tools/bin:~/bin git log -1", "wt"), ("export PATH=\"$PATH:/x\"; git diff", "wt"),
] + [
    # the literal forms the deny message points to for a revision a `$(...)` word would have named
    (cmd, where) for cmd in (
        "git diff --merge-base origin/main", "git diff origin/main...HEAD", "git diff --stat origin/main...HEAD",
        "git log origin/main..HEAD", "git log --oneline HEAD~3..HEAD", "git merge-base HEAD origin/main",
        "git rev-parse --short=8 HEAD",
    ) for where in ("app", "wt")
]

# ---- the near forms that stay denied: each would let a push, a write or a program through ----------------------

DENIED = [
    # the narrowing never covers a call the text shows
    ("pytest -k git; git push origin HEAD", "wt"), ("pytest -k git && git push", "wt"),
    ("pytest -k git | git push", "wt"), ("pytest -k git; git commit -m x", "app"),
    # make: any option or word that is not a flag or a target
    ("make -f - git-hooks", "wt"), ("make -j4 -f Makefile git-hooks", "wt"), ("make --eval='$(shell git push)' x", "wt"),
    ("make --eval=\"x:;git push\" x", "wt"), ("make SHELL=git git-hooks", "wt"),
    ("MAKEFLAGS=--eval=x make git-hooks", "wt"), ("env MAKEFLAGS=-f- make git-hooks", "wt"),
    ("echo -f- | xargs make git-hooks", "wt"), ("echo push | xargs make git", "wt"), ("make git-hooks; git push", "wt"),
    ("make 'git push'", "wt"), ("make -E 'x:=$(shell git push)' x", "wt"),
    # a program that adds words (or reads its words again) keeps the call open
    ("watch git log \"--format=%h --output=x\"", "wt"), ("watch git log '--out'put=x", "wt"),
    ("watch git log $X", "wt"), ("watch git push", "wt"), ("echo push | xargs watch git", "wt"),
    ("echo push | xargs watch -n 1 git", "wt"), ("watch GIT_DIR=app/.git git commit -m x", "."),
    ("watch FOO=1 git push", "wt"), ("watch -n 1 'git push'", "wt"), ("flock /tmp/lock git push", "wt"),
    ("strace -o out git push", "wt"), ("watch git log --output=x", "wt"), ("watch git log --outp=x", "wt"),
    ("watch git -C app commit -m x", "."), ("watch git commit -m x", "app"), ("flock x git log \"a b\" --output=y", "wt"),
    ("echo push | parallel git", "wt"), ("echo log | parallel git", "wt"), ("parallel git ::: push", "wt"),
    ("find . -maxdepth 0 -exec git log {} \\;", "wt"),
    # a loop variable among the words of a command that has options which write a file or run a program
    ("for f in a b; do git commit $f; done", "wt"), ("for f in a b; do git log $f; done", "wt"),
    ("for f in a b; do git diff $f; done", "wt"), ("for f in a b; do git fetch $f; done", "wt"),
    ("for f in a b; do git push $f; done", "wt"), ("for f in a b; do git show $f; done", "wt"),
    ("for f in a b; do git add $f; done", "app"), ("for f in a b; do git rm $f; done", "app"),
    ("for f in a b; do git branch $f; done", "wt"), ("for f in a b; do git config $f; done", "wt"),
    ("for f in a b; do git remote $f; done", "wt"), ("git commit $X", "wt"), ("git add $X --chmod=+x; git push", "wt"),
    ("git add $F; git push origin HEAD", "wt"), ("git add --pathspec-from-file=$F", "app"),
    # PATH: anything but literal folders behind $PATH
    ("PATH=/x:$PATH git status", "wt"), ("export PATH=/x:$PATH; git status", "wt"), ("PATH=$PATH:$X git status", "wt"),
    ("PATH=$PATH:/x:$PATH git status", "wt"), ("PATH=$PATH: git status", "wt"), ("PATH=$PATH:: git status", "wt"),
    ("PATH=.:$PATH git status", "wt"), ("PATH=/x git status", "wt"), ("PATH=/x:/y git status", "wt"), ("PATH=$PATH git status", "wt"),
    ("PATH=$PATH:/x:/y:$(echo /z) git status", "wt"), ("PATH=$PATH:/x git push", "wt"),
    ("PATH=\"$PATH:/x y\" git status", "wt"),
    ("PATH=$PATH:/x git -c core.pager=less status", "wt"), ("HOME=/x git status", "wt"),
    # a word built at run time from git's own output: the guard cannot know it is not an option, so every
    # such word stays denied (git describe and rev-parse --abbrev-ref print ref names, and a ref name may
    # start with a dash; rev-parse prints back option words it does not know)
    ("git diff $(git merge-base HEAD origin/main)", "app"), ("git diff $(git merge-base HEAD origin/main)", "wt"),
    ("git log --oneline $(git rev-parse HEAD~3)..HEAD", "wt"), ("git log `git rev-parse HEAD~3`..HEAD", "wt"),
    ("git diff --stat $(git merge-base HEAD origin/main) HEAD", "app"),
    ("git log $(git describe --tags --abbrev=0)..HEAD", "wt"), ("git show $(git rev-list -n 1 HEAD)", "wt"),
    ("git diff $(git rev-parse --abbrev-ref HEAD)", "wt"), ("git log -1 $(git rev-parse --short=8 HEAD)", "wt"),
    ("git diff $(git rev-parse --verify HEAD^{commit}) --stat", "wt"),
    ("git reset --soft $(git merge-base HEAD origin/main)", "wt"), ("git rebase $(git merge-base HEAD origin/main)", "wt"),
    ("git diff $(echo --output=x)", "wt"), ("git diff $(git rev-parse --output=x HEAD)", "wt"),
    ("git diff $(git rev-parse -- --output=x)", "wt"), ("git diff $(git rev-parse HEAD; echo --output=x)", "wt"),
    ("git diff $(git rev-parse HEAD && echo --output=x)", "wt"), ("git diff $(git rev-parse HEAD | cat)", "wt"),
    ("git diff $(cat f)", "wt"), ("git diff $(git rev-parse HEAD) $X", "wt"),
    ("git diff $(git rev-parse HEAD) $(echo --output=x)", "wt"), ("git diff $(git -C /x rev-parse HEAD)", "wt"),
    ("git log $(git rev-parse --git-dir)", "wt"), ("git log $(git rev-parse --show-prefix)", "wt"),
    ("git log $(git rev-parse HEAD > x)", "wt"), ("git log $(git rev-parse $X)", "wt"),
    ("git log $(git rev-parse -x)", "wt"), ("git log $(git rev-parse --opt)", "wt"),
    ("git log $(git log -1 --format=%s)", "wt"),
    ("git log $(git show HEAD)", "wt"), ("git log $(git diff HEAD)", "wt"), ("git log $(git config user.name)", "wt"),
    ("git log $(git for-each-ref --format=%(refname))", "wt"), ("git log $(git branch --show-current)", "wt"),
    ("git log --output=$(git rev-parse HEAD)", "wt"), ("git log $(git rev-parse HEAD) --output=x", "wt"),
    ("git reflog $(git rev-parse expire) --expire=now --all", "wt"), ("git reflog $(git rev-parse delete) x", "wt"),
    ("git submodule $(git rev-parse update)", "wt"), ("git stash $(git rev-parse drop)", "wt"),
    ("git remote $(git rev-parse add) x /y", "wt"), ("git worktree $(git rev-parse add) ../x", "wt"),
    ("git notes $(git rev-parse add) -m x", "wt"), ("git lfs $(git rev-parse push) origin", "wt"),
    ("git reset --soft $(git merge-base HEAD origin/main)", "app"), ("git merge $(git rev-parse origin/main)", "app"),
    ("git reset --hard $(git rev-parse HEAD) --output=x; git push", "wt"),
    ("git push $(git rev-parse HEAD)", "wt"), ("git commit $(git rev-parse HEAD) -m x", "app"),
    ("git reset --hard $(git rev-parse HEAD)", "app"), ("git fetch $(git rev-parse HEAD)", "wt"),
    ("git $(git rev-parse HEAD)", "wt"), ("$(git rev-parse HEAD) log", "wt"),
   
    ("git -c core.pager=less diff $(git rev-parse HEAD)", "wt"),
]


@pytest.mark.parametrize("cmd,cwd", ALLOWED)
def test_ordinary_work_that_only_mentions_git_goes_through(world, cmd, cwd):
    assert guard.check(cmd, world.at(cwd)) is None, cmd


@pytest.mark.parametrize("cmd,cwd", DENIED)
def test_the_near_forms_stay_denied(world, cmd, cwd):
    found = guard.check(cmd, world.at(cwd))
    assert found is not None, cmd
    out = git_guard.decide(hook_payload(cmd, world.at(cwd)))
    assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny", cmd


@pytest.mark.parametrize("cmd,cwd", [
    ("pytest -k git", "wt"), ("git log", "app"), ("git status", "app"), ("git diff", "app"),
    ("make git-hooks", "wt"), ("watch git log --oneline", "wt"), ("export PATH=$PATH:/x; git status", "wt"),
    ("for f in a b; do git add $f; done", "wt"),
])
def test_the_hook_as_a_whole_lets_the_named_forms_through(world, cmd, cwd):
    """The done bar through the real hook entry (the older main-tree rules run after the attach rules)."""
    assert git_guard.decide(hook_payload(cmd, world.at(cwd))) is None, cmd


def test_a_run_time_revision_word_is_denied_with_the_literal_forms_named(world):
    """A `$(...)` word is denied like any word built at run time, and the message shows `$(...)` and the
    literal forms; a loop variable gets the plain message."""
    found = guard.check("git diff $(git merge-base HEAD origin/main)", world.at("wt"))
    assert found is not None, "the run-time revision word passed"
    assert "for a revision" in found.reason and "$(...)" in found.reason, found.reason
    assert guard._MARK not in found.reason, found.reason
    plain = guard.check("for f in a b; do git log $f; done", world.at("wt"))
    assert plain is not None, "the loop variable passed"
    assert "for a revision" not in plain.reason and "built at run time" in plain.reason, plain.reason


def test_a_plain_project_pays_nothing_for_the_narrowing(project):
    assert guard.check("pytest -k git", str(project)) is None
    assert guard.check("git push origin HEAD", str(project)) is None


def test_the_tables_name_what_the_narrowing_reads():
    """The tables and patterns the narrowing stands on, pinned so a widening shows up as a failing test."""
    assert guard._ARG_ONLY == {"pytest", "py.test"}
    assert guard._NO_ADDED_WORDS == {"watch", "flock", "strace", "ltrace", "chronic"}
    assert not hasattr(guard, "_revision_query") and not hasattr(guard, "_REV_SUBS")
    for flag in ("-f", "--eval=x", "-E", "SHELL=git", "-o", "--output-sync", "--file=x", "-W", "-p"):
        assert not guard._make_targets_only([flag, "t"], {}), flag
    for flag in ("-j4", "-k", "-s", "-n", "-B", "--keep-going", "--jobs=4", "t", "git-hooks", "sub/dir.o"):
        assert guard._make_targets_only([flag], {}), flag
    assert not guard._make_targets_only(["t"], {"MAKEFLAGS": "-f-"})
    assert not guard._make_targets_only(["-C"], {})


# ---- a part built at run time anywhere in a word (op-062 t-676, after the shield cap) -------------------------------
#
# The shell cuts the result of an unquoted `$(...)`, backtick or `$NAME` into words wherever it sits in a word, so
# `origin$(printf ' --upload-pack=x')` reaches git as `origin --upload-pack=x`. The guard used to read a word as
# run time only when it began with `$`. Each row below ran through t-651's guard and t-676's first rounds.

_UP = "$(printf ' --upload-pack=x')"
_PUSH = "$(printf '. push origin HEAD:refs/heads/demo --repo')"

RUNTIME_DENIED = [
    # a substitution in the middle of an argument word: unquoted, in double quotes, backtick, nested, a variable
    ("git fetch origin" + _UP, "app"), ("git fetch origin" + _UP, "wt"),
    ("git fetch origin`printf ' --upload-pack=x'`", "app"), ("git fetch origin`printf ' --upload-pack=x'`", "wt"),
    ("git fetch \"origin" + _UP + "\"", "app"), ("git fetch \"origin`printf ' --upload-pack=x'`\"", "wt"),
    ("git fetch origin$(echo $(printf ' --upload-pack=x'))", "app"),
    ("git fetch origin$(echo `printf ' --upload-pack=x'`)", "wt"),
    ("git fetch origin${UP}", "app"), ("git fetch origin$UP", "wt"), ("git fetch \"origin${UP}\"", "app"),
    ("UP=" + _UP + "; git fetch origin${UP}", "app"), ("UP=" + _UP + "; git fetch origin$UP", "wt"),
    ("UP=' --upload-pack=x'; git fetch origin${UP}", "app"), ("UP=' --upload-pack=x'; git fetch origin$UP", "wt"),
    ("git fetch --up$(echo load)-pack=x origin", "app"), ("git fetch --upload-pack=$(echo x) origin", "wt"),
    ("git clone origin" + _UP + " dest", "app"), ("git ls-remote origin" + _UP, "wt"),
    ("git pull origin" + _UP, "wt"), ("git fetch origin $(printf '%s' --upload-pack=x)", "app"),
    # the options that write a file
    ("git diff HEAD$(printf ' --output=x')", "app"), ("git diff --out$(echo put)=x HEAD", "app"),
    ("git log HEAD$(printf ' --output=x')", "wt"), ("git log HEAD`printf ' --output=x'`", "app"),
    ("git show HEAD\"$(printf ' --output=x')\"", "wt"), ("git log --format=$(echo %h) -1", "app"),
    ("git log --format=$(printf '%h --output=x') -1", "wt"), ("git log --pretty=${F}x -1", "app"),
    # the run-time part is the value of an option: it can still be several words
    ("git fetch --depth $(printf '1 --upload-pack=x') origin", "app"),
    ("git fetch --depth $(printf '1 --upload-pack=x') origin", "wt"),
    ("X=$(printf '1 --upload-pack=x'); git fetch --depth $X origin", "app"),
    ("X=$(printf '1 --upload-pack=x'); git fetch --depth $X origin", "wt"),
    ("git commit -m $(printf 'x --no-verify')", "wt"), ("git commit -m x$(printf ' --no-verify')", "wt"),
    ("X=$(printf 'x --no-verify'); git commit -m $X", "wt"),
    # a run-time part in an option before the subcommand moves the work to another folder or adds a subcommand
    ("git -C " + _PUSH + " status", "app"), ("git -C " + _PUSH + " status", "wt"),
    ("git -C `printf '. push origin HEAD:refs/heads/demo --repo'` status", "app"),
    ("git -C .$(printf ' push origin HEAD:refs/heads/demo --repo') status", "wt"),
    ("git -c user.name=$(printf 'x push origin HEAD:refs/heads/demo --repo') status", "app"),
    ("git -c user.name=$(printf 'x push origin HEAD:refs/heads/demo --repo') status", "wt"),
    ("git --git-dir=.git$(printf ' push origin x --repo') status", "app"),
    ("git --work-tree $(printf '. push origin x --repo') status", "wt"),
    ("git --namespace=$(printf 'x push origin y --repo') status", "wt"),
    ("X=$(printf '. push origin HEAD:x --repo'); git -C $X status", "app"),
    ("X=$(printf '. push origin HEAD:x --repo'); git -C $X status", "wt"),
    ("X='. push origin HEAD:x --repo'; git -C $X status", "app"),
    ("X='. push origin HEAD:x --repo'; git --git-dir=$X status", "wt"),
    # a wrapper's word that the shell cuts: the program the wrapper runs is not the one in the text
    ("env FOO=$(printf 'a GIT_SSH_COMMAND=prog') git fetch", "app"),
    ("env FOO=`printf 'a GIT_SSH_COMMAND=prog'` git fetch", "wt"),
    ("timeout 5$(printf ' git push origin HEAD') git status", "wt"),
    ("nice -n $(printf '1 git push origin HEAD') git status", "app"),
    ("env -u $(printf 'A git push origin HEAD') git status", "wt"),
    ("echo x | xargs -n $(printf '1 git push origin HEAD') git status", "wt"),
    ("watch -n 1$(printf ' git push origin HEAD') git status", "wt"),
    ("flock /tmp/l$(printf ' git push origin HEAD') git status", "wt"),
    ("$(printf 'git push origin HEAD ')/x status", "wt"), ("`printf 'git push origin HEAD '`/x", "app"),
    ("bash $(printf '-c') 'git push origin HEAD'", "wt"), ("sh -$(printf c) 'git push origin HEAD'", "app"),
    # gh
    ("gh api repos/o/r $(printf '-X DELETE')", "app"), ("gh api repos/o/r$(printf ' -X DELETE')", "wt"),
    # a short option that takes a value hides a `--` that is its value
    ("git fetch -o -- origin --upload-pack=x", "app"), ("git fetch -o -- origin --upload-pack=x", "wt"),
    ("git pull -o -- origin --upload-pack=x", "wt"), ("git ls-remote -o -- origin --upload-pack=x", "app"),
    ("git fetch -o x -- origin --upload-pack=x", "wt"),
]

RUNTIME_ALLOWED = [
    # a double-quoted substitution is one word: a message, a folder, the value of a long option
    ("git commit -m \"$(printf 'msg')\"", "wt"), ("git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\"", "wt"),
    ("git commit -m \"msg $(date +%F)\"", "wt"), ("git commit -m \"$MSG\"", "wt"),
    ("git -C \"$(git rev-parse --show-toplevel)\" status", "app"),
    ("git -C \"$(git rev-parse --show-toplevel)\" status", "wt"),
    ("git -c user.name=\"$(printf x)\" status", "app"), ("git -c user.name=\"$(printf x)\" log -1", "wt"),
    ("git log --format=\"$(echo %h)\" -1", "app"), ("git log --format=\"$(echo %h)\" -1", "wt"),
    ("git log --format=\"`echo %h`\" -1", "wt"), ("git log --grep=\"$(echo fix)\"", "app"),
    # the folder of the repository, whole: its text is a path
    ("git -C $(git rev-parse --show-toplevel) status", "app"), ("git -C $(git rev-parse --show-toplevel) log -1", "wt"),
    ("$(git rev-parse --show-toplevel)/bin/tool status", "wt"),
    # single-quoted text is literal
    ("git log --grep='a$(b)'", "app"), ("git log --grep='a`b`' -1", "wt"),
    ("git grep '$(foo)' -- src", "wt"), ("git grep 'a`b`'", "app"),
    # a variable behind `--`, a variable on a command no option of which writes or runs, a known variable
    ("for f in a b; do git log -1 -- $f; done", "wt"), ("for f in a b; do git add $f; done", "wt"),
    ("D=src; git log -1 -- $D", "app"), ("N=3; git log -n $N", "app"), ("N=3; git log -n$N", "wt"),
    ("git status \"$(git rev-parse --show-toplevel)\"", "app"),
]


@pytest.mark.parametrize("cmd,cwd", RUNTIME_DENIED)
def test_a_run_time_part_anywhere_in_a_word_is_denied(world, cmd, cwd):
    found = guard.check(cmd, world.at(cwd))
    assert found is not None, cmd
    assert guard._MARK not in found.reason and guard._SPLIT not in found.reason, found.reason
    out = git_guard.decide(hook_payload(cmd, world.at(cwd)))
    assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny", cmd


@pytest.mark.parametrize("cmd,cwd", RUNTIME_ALLOWED)
def test_the_run_time_forms_that_cannot_add_words_go_through(world, cmd, cwd):
    assert guard.check(cmd, world.at(cwd)) is None, cmd
    assert git_guard.decide(hook_payload(cmd, world.at(cwd))) is None, cmd


@pytest.mark.parametrize("cmd", ["git fetch origin" + _UP, "git -C " + _PUSH + " status",
                                 "git log --format=$(echo %h) -1", "git fetch --depth $(echo 1 2) origin"])
def test_the_deny_for_a_run_time_part_names_it_and_hides_the_internal_mark(world, cmd):
    found = guard.check(cmd, world.at("app"))
    assert found is not None and "built at run time" in found.reason, found
    assert "$(...)" in found.reason, found.reason
    assert guard._MARK not in found.reason and guard._SPLIT not in found.reason, found.reason


def test_a_plain_project_pays_nothing_for_the_run_time_rules(project):
    for cmd, _cwd in RUNTIME_DENIED:
        assert guard.check(cmd, str(project)) is None, cmd
