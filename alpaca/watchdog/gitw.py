"""The watchdog's one writer runner for git (design/watchdog/design.md section 2).

`gitview.git` is read-only: it strips GIT_INDEX_FILE (so a temporary index would become the real
one) and its 10 s timeout can cut a write in half. Every git write the watchdog makes (the merge
dry run's scratch worktree, undo refs and snapshot commits, rollback) goes through `run` here.
"""
from __future__ import annotations

import os
import subprocess

TIMEOUT = 120


# `git rev-parse --local-env-vars`: variables that point git at another repository, index or object store
LOCAL_ENV = ("GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
             "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE",
             "GIT_INDEX_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX", "GIT_SHALLOW_FILE",
             "GIT_COMMON_DIR", "GIT_NAMESPACE")


class GitWriteError(Exception):
    pass


def _env(extra=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    for name in LOCAL_ENV:
        env.pop(name, None)
    if extra:
        env.update(extra)
    return env


def run(cwd, *args, env=None, timeout=TIMEOUT, check=True):
    """Run `git -C cwd args`; returns the CompletedProcess. Raises GitWriteError (with stderr) when
    check is set and git fails, or when it cannot run or times out."""
    try:
        done = subprocess.run(["git", "--no-pager", "-C", cwd, *args], capture_output=True, text=True,
                              env=_env(env), timeout=timeout, errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitWriteError("git %s could not run in %s: %s" % (" ".join(args[:2]), cwd, exc)) from exc
    if check and done.returncode != 0:
        raise GitWriteError("git %s failed in %s: %s" % (" ".join(args[:2]), cwd, (done.stderr or done.stdout).strip()[:300]))
    return done


def _out(cwd, *args):
    done = run(cwd, *args, check=False)
    return done.stdout.strip() if done.returncode == 0 else None


def assert_toplevel(path, linked=True):
    """Fail unless `path` is itself a working tree root (git rev-parse --show-toplevel equals it) and,
    when `linked`, a linked worktree (git dir differs from the common dir). A wrong working directory
    then stops here instead of writing into the main tree."""
    if not os.path.isdir(path):
        raise GitWriteError("%s is not a directory" % path)
    top = _out(path, "rev-parse", "--show-toplevel")
    if not top or os.path.realpath(top) != os.path.realpath(path):
        raise GitWriteError("%s is not a working tree root (git says %s)" % (path, top))
    if linked:
        gdir = _out(path, "rev-parse", "--absolute-git-dir")
        common = _out(path, "rev-parse", "--git-common-dir")
        if not gdir or not common:
            raise GitWriteError("cannot read the git dir of %s" % path)
        common = common if os.path.isabs(common) else os.path.join(path, common)
        if os.path.realpath(gdir) == os.path.realpath(common):
            raise GitWriteError("%s is the main working tree, not a linked worktree" % path)
    return os.path.realpath(path)
