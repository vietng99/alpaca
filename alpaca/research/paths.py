"""Every on-disk location of the research engine (architecture.md section 8.4).

Design: design/research-engine/architecture.md sections 8.4 and 12.6. Each helper joins its parts
under the root it is given and touches nothing on disk. egress_dir is the one exception: it also
makes its two folders with mode 0700, as section 12.6 says. Every path component made from a
value (a run id, task id, launch id, stage slug, stamp, repair line or verification id) goes
through check_name, so no bug or fix text reaches a path or a branch name (FR-014). Fixed file
names such as runner.lock and attempt.diff are literals and do not.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import time

RUN_ID = re.compile(r"^rr-[0-9]{3,6}$")
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")     # every path component made from a value
STAMP_FORMAT = "%Y%m%dt%H%M%Sz"                     # UTC, for example 20260930t061500z

_SLUG_MAX = 40          # stage_slug keeps at most this many characters before its hash suffix
_SLUG_HASH = 8          # hex digits of the id's sha256 added when the slug changed the id
_SOCKET_MAX = 108       # sizeof(sun_path) on Linux, the terminating NUL included


def check_name(value: str) -> str:
    """Return value when it matches NAME; raise ValueError otherwise. Every path component the
    engine makes from a value (a run id, task id, launch id, stage slug, stamp, repair line or
    verification id) goes through it, so no bug or fix text reaches a path or a branch (FR-014).
    Fixed file names such as runner.lock and attempt.diff are literals and do not."""
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError("not a safe path component: %r" % (value,))
    return value


def research_dir(root: str) -> str:
    """<root>/.alpaca/research"""
    return os.path.join(root, ".alpaca", "research")


def run_dir(root: str, run: str) -> str:
    """<root>/.alpaca/research/<run>"""
    return os.path.join(research_dir(root), check_name(run))


def contract_dir(root: str, run: str) -> str:
    """<run_dir>/contract: contract.json, files.json, files/, holdout/, hidden/."""
    return os.path.join(run_dir(root, run), "contract")


def clone_dir(root: str, run: str, line: str = "champion") -> str:
    """<run_dir>/clones/<line>: the improve clone ("champion") or one repair task's repository."""
    return os.path.join(run_dir(root, run), "clones", check_name(line))


def attempt_dir(root: str, run: str, task: str) -> str:
    """<run_dir>/attempts/<task>"""
    return os.path.join(run_dir(root, run), "attempts", check_name(task))


def jobs_dir(root: str, scope: str) -> str:
    """<run_dir>/jobs for a run id, else <root>/.alpaca/research/jobs for a verification outside a
    run (the scope is then the verification id): the job folders of the session runtime
    (in-session-runtime.md section 4)."""
    base = run_dir(root, scope) if RUN_ID.fullmatch(str(scope)) else research_dir(root)
    return os.path.join(base, "jobs")


def job_dir(root: str, scope: str, job: str) -> str:
    """<jobs_dir>/<job>: job.json, prompt.txt, claims/ and replies/."""
    return os.path.join(jobs_dir(root, scope), check_name(job))


def job_scratch(root: str, scope: str, job: str) -> str:
    """<job folder>/scratch: the one folder a job may use for scratch work (prototype scripts,
    drafts, temporary files). It is private to the job: no other job's folder holds it, unlike the
    session scratchpad that Claude Code advertises to every subagent of a driving session
    (in-session-runtime.md section 7.3, t-191 F29)."""
    return os.path.join(job_dir(root, scope, job), "scratch")


def launch_home(root: str, run: str | None, launch: str) -> str:
    """<run_dir>/homes/<launch>, or <root>/.alpaca/research/homes/<launch> for a launch outside a
    run."""
    base = run_dir(root, run) if run is not None else research_dir(root)
    return os.path.join(base, "homes", check_name(launch))


def pids_dir(root: str, run: str | None) -> str:
    """<run_dir>/pids, or <root>/.alpaca/research/pids for a launch outside a run."""
    base = run_dir(root, run) if run is not None else research_dir(root)
    return os.path.join(base, "pids")


def env_dir(root: str, run: str) -> str:
    """<run_dir>/env: one environment snapshot per runner start."""
    return os.path.join(run_dir(root, run), "env")


def forensics_dir(root: str, run: str, stamp: str) -> str:
    """<run_dir>/forensics/<stamp>: copies kept on an early exit."""
    return os.path.join(run_dir(root, run), "forensics", check_name(stamp))


def runner_lock(root: str, run: str) -> str:
    """<run_dir>/runner.lock"""
    return os.path.join(run_dir(root, run), "runner.lock")


def runner_pid(root: str, run: str) -> str:
    """<run_dir>/runner.pid"""
    return os.path.join(run_dir(root, run), "runner.pid")


def runner_log(root: str, run: str) -> str:
    """<run_dir>/runner.log"""
    return os.path.join(run_dir(root, run), "runner.log")


def worktrees_dir(root: str, run: str) -> str:
    """<root>/.alpaca/worktrees/research/<run>"""
    return os.path.join(root, ".alpaca", "worktrees", "research", check_name(run))


#: an attempt worktree's folder name: the task id for cut 1, `<task>-c<n>` for cut n >= 2
#: (in-session-runtime.md section 28.1); the only place that reads the form
_CUT_NAME = re.compile(r"^(t-[0-9]+)(?:-c([2-9]|[1-9][0-9]+))?$")


def _cut_number(cut) -> int:
    if isinstance(cut, bool) or not isinstance(cut, int) or cut < 1:
        raise ValueError("a worktree cut is an int >= 1: %r" % (cut,))
    return cut


def worktree_dir(root: str, run: str, task: str, cut: int = 1) -> str:
    """<worktrees_dir>/<task> for cut 1 and <worktrees_dir>/<task>-c<n> for cut n > 1: one cut of
    an attempt worktree. Every cut of an attempt has its own folder, never given to another try
    (section 28.1)."""
    n = _cut_number(cut)
    leaf = check_name(task) if n == 1 else check_name("%s-c%d" % (check_name(task), n))
    return os.path.join(worktrees_dir(root, run), leaf)


def cut_git_dir(root: str, run: str, task: str, cut: int = 1) -> str:
    """<attempt_dir>/git for cut 1 and <attempt_dir>/git/c<n> for cut n > 1: the folder that holds
    the cut's `admin` and `host` git folders. The object store `objects` stays at
    <attempt_dir>/git/objects for every cut (content-addressed)."""
    n = _cut_number(cut)
    base = os.path.join(attempt_dir(root, run, task), "git")
    return base if n == 1 else os.path.join(base, "c%d" % n)


def cut_of(name: str) -> tuple | None:
    """(task, cut) for a worktree folder name `t-<k>` (cut 1) or `t-<k>-c<n>` (n >= 2), else
    None: a champion name, a cut 1 spelled `-c1` or anything else is no cut."""
    m = _CUT_NAME.fullmatch(name) if isinstance(name, str) else None
    if not m:
        return None
    return m.group(1), int(m.group(2) or 1)


def champion_dir(root: str, run: str, round_n: int, line: str = "champion") -> str:
    """<worktrees_dir>/r<N>-champion for the improve line, <worktrees_dir>/<repair task>-r<N>-champion
    for a repair line."""
    if line == "champion":
        leaf = "r%d-champion" % int(round_n)
    else:
        leaf = "%s-r%d-champion" % (check_name(line), int(round_n))
    return os.path.join(worktrees_dir(root, run), check_name(leaf))


def reports_dir(root: str, run: str) -> str:
    """<root>/.alpaca/reports/research/<run>"""
    return os.path.join(root, ".alpaca", "reports", "research", check_name(run))


def reproducer_store(root: str, runbook_id: str | None = None,
                     case_id: str | None = None) -> str:
    """<root>/contracts/research[/<runbook id>[/<case id>]], keyed as t-088 keys the store
    (runbook-format-3.md section 16.1): by the ids as the runbook writes them, since the owner
    fills the store before any run exists. Each id must be a runbook slug (runbook.SLUG,
    alpaca/runbook.py:111), which cannot hold "/" or start with "."; ValueError otherwise. A case
    id needs a runbook id."""
    from alpaca.runbook import SLUG
    parts = [root, "contracts", "research"]
    for value in (runbook_id, case_id):
        if value is None:
            continue
        if not isinstance(value, str) or not SLUG.fullmatch(value):
            raise ValueError("not a runbook slug: %r" % (value,))
    if runbook_id is not None:
        parts.append(runbook_id)
        if case_id is not None:
            parts.append(case_id)
    elif case_id is not None:
        raise ValueError("a case id needs a runbook id")
    return os.path.join(*parts)


def hidden_dir(root: str, run: str, case_id: str) -> str:
    """<contract_dir>/hidden/<stage_slug(case id)>: an origin store reproducer frozen at run
    start."""
    return os.path.join(contract_dir(root, run), "hidden", stage_slug(case_id))


def reproducer_dir(root: str, run: str, case_id: str) -> str:
    """<run_dir>/reproducers/<stage_slug(case id)>: an origin generate reproducer frozen with its
    proof."""
    return os.path.join(run_dir(root, run), "reproducers", stage_slug(case_id))


def holdout_dir(root: str, run: str, stage: str) -> str:
    """<run_dir>/holdout/<stage_slug(stage)>"""
    return os.path.join(run_dir(root, run), "holdout", stage_slug(stage))


def stage_home(stage_dir: str, side: str, repeat: int) -> str:
    """<stage_dir>/home/<side>-<repeat>, the HOME of one gate or holdout jail, where stage_dir is
    the folder that holds the jail's evidence/ (an attempt's or a verification's
    gates/<nn>-<slug>/, or holdout_dir(...)) and side is the research-gate side (`attempt`,
    `base`, `verify`, `prepare`) or `holdout`. It sits beside evidence/, never inside it, so no
    check reads it. The caller makes it fresh (removes any leftover with a no-follow rmtree, then
    makes it empty with mode 0700) before each jail and removes it after judged_copy, so no jail
    starts with files an earlier jail wrote."""
    return os.path.join(stage_dir, "home", check_name("%s-%d" % (side, int(repeat))))


def machine_lock_dir() -> str:
    """The folder of workspace.registry_path() (alpaca/workspace.py:117), plus locks/: the
    machine-wide locks of section 4.7, shared by every project on the host."""
    from alpaca import workspace
    return os.path.join(os.path.dirname(workspace.registry_path()), "locks")


def research_branch(run: str, line: str = "champion") -> str:
    """research/<run> for the improve line, research/<run>/<repair task> for a repair line."""
    if line == "champion":
        return "research/%s" % check_name(run)
    return "research/%s/%s" % (check_name(run), check_name(line))


def attempt_key(run: str, round_n: int, slot: int, repair_index: int | None = None) -> str:
    """The key an attempt task carries in its task-add event (section 8.2):
    <run>:r<N>:s<S>, or <run>:repair:<i>:r<N>:s<S> for a fix attempt of repair task i."""
    if repair_index is None:
        return "%s:r%d:s%d" % (run, int(round_n), int(slot))
    return "%s:repair:%d:r%d:s%d" % (run, int(repair_index), int(round_n), int(slot))


def launch_key(task: str, role: str, try_n: int) -> str:
    """<task>/<role>/t<try>: the resume key of one launch (section 6.1)."""
    return "%s/%s/t%d" % (task, role, int(try_n))


def launch_id(key: str) -> str:
    """The key with every "/" turned into "-", through check_name: t-140/implementer/t1 gives
    t-140-implementer-t1, and t-089's vf-t-140-15890/v1/r0/t1 gives vf-t-140-15890-v1-r0-t1."""
    return check_name(key.replace("/", "-"))


def launch_session(run: str, launch: str) -> str:
    """<run>-<launch id>: the record session of one launch."""
    return "%s-%s" % (check_name(run), check_name(launch))


def stage_slug(stage: str) -> str:
    """A runbook stage id (alpaca/runbook.py:111 allows "." and "_") as a path and lock component:
    lower case, every run of characters outside [a-z0-9-] turned into "-", trimmed to 40
    characters, and, when that changed the id, "-" plus the first 8 hex digits of the id's sha256,
    so two ids never share a slug. Used for gates/<nn>-<slug>/ and the lock stage-<slug>."""
    if not isinstance(stage, str) or not stage:
        raise ValueError("a stage id is a non-empty string: %r" % (stage,))
    slug = re.sub(r"[^a-z0-9-]+", "-", stage.lower())[:_SLUG_MAX]
    if slug != stage:
        digest = hashlib.sha256(stage.encode("utf-8")).hexdigest()[:_SLUG_HASH]
        slug = "%s-%s" % (slug, digest)
    return check_name(slug)


def stamp(when: float | None = None) -> str:
    """time.strftime(STAMP_FORMAT) in UTC, of `when` (seconds since the epoch) or now."""
    return time.strftime(STAMP_FORMAT, time.gmtime(when))


def egress_dir(root: str, run: str | None, launch: str) -> str:
    """<base>/alpaca-egress-<uid>/<first 16 hex of sha256(root, run, launch)>, where base is
    $XDG_RUNTIME_DIR when it is set and exists, else /tmp; folders made with mode 0700. Short on
    purpose: a Unix socket path must stay under 108 bytes.

    Both folders are made here when missing, each with mode 0700, and each is then checked with
    os.lstat: a folder (not a link), owned by this user, mode 0700. Anything else raises
    ValueError, so a folder another user planted under a shared /tmp is never used. The hash input
    is the three values joined by newlines, run written as the empty string when None."""
    base = os.environ.get("XDG_RUNTIME_DIR") or ""
    if not base or not os.path.isdir(base):
        base = "/tmp"
    key = "\n".join((root, run or "", launch)).encode("utf-8")
    leaf = hashlib.sha256(key).hexdigest()[:16]
    parent = os.path.join(base, "alpaca-egress-%d" % os.getuid())
    path = os.path.join(parent, leaf)
    if len(os.path.join(path, "egress.sock").encode("utf-8")) >= _SOCKET_MAX:
        raise ValueError("egress socket path too long for a Unix socket: %s" % path)
    for folder in (parent, path):
        _private_dir(folder)
    return path


def _private_dir(folder: str) -> None:
    """Make folder with mode 0700 when it is missing, then refuse it (ValueError) unless it is a
    folder, not a link, owned by this user, with mode 0700."""
    try:
        os.mkdir(folder, 0o700)
    except FileExistsError:
        pass
    else:
        os.chmod(folder, 0o700)          # the umask may have cleared bits of 0700
    st = os.lstat(folder)
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError("egress folder is not a folder: %s" % folder)
    if st.st_uid != os.getuid():
        raise ValueError("egress folder is owned by uid %d: %s" % (st.st_uid, folder))
    if stat.S_IMODE(st.st_mode) != 0o700:
        raise ValueError("egress folder has mode %o, not 700: %s"
                         % (stat.S_IMODE(st.st_mode), folder))
