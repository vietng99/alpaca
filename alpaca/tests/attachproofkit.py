"""Test support for t-650 (proof and verify see the attached repo): a task with a work branch.

t-648 cuts the real worktree and writes `.alpaca/attach/branches/<task>.json` when a task is claimed. These
tests do not depend on that verb: `cut` makes the same two things by hand, in the format of the build plan
section 5, so the proof code is tested against the files it will read.

    from alpaca.tests.attachkit import attached, studio, upstream, git          # noqa: F401  (fixtures)
    from alpaca.tests.attachproofkit import cut, prepare, commit_in

A separate module from attachkit.py on purpose: attachkit is appended to by several tasks at once.
"""
import json
import os
from pathlib import Path

from alpaca import cli, db
from alpaca.tests.attachkit import git


def prepare(attached, task="t-001"):
    """A studio that can seal a proof for `task`: one commit in the studio's own repository (so a seal in
    verification scope has a head to cut from), an op and the task on the record. Returns the root string."""
    root = str(attached.root)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "studio base")
    assert cli.main(["op", "new", "add the shout verb", "--done-when", "the verb prints upper case"]) == 0
    assert cli.main(["task", "add", "--title", "task", "op-001", "write the shout verb", "--phase", "build"]) == 0
    return root


def record_branch(root, task, *, branch, worktree, base_sha, worker="builder"):
    """The `attach-branch` event t-648 appends at every cut (data keys branch, worktree, base_sha, baseline_key,
    worker). The proof code takes the branch and the base from this event, not from the branch file."""
    conn = db.connect(str(root))
    try:
        row = conn.execute("SELECT op FROM tasks WHERE id=?", (task,)).fetchone()
        db.append_event(conn, session="builder", actor=worker, kind="attach-branch",
                        op=row["op"] if row is not None else None, ref=task,
                        data={"branch": branch, "worktree": str(worktree), "base_sha": base_sha,
                              "baseline_key": "0" * 16, "worker": worker})
    finally:
        conn.close()


def cut(attached, task="t-001", *, record=True):
    """The worktree, the branch file and the `attach-branch` event t-648 would make for `task`.
    Returns the worktree Path. `record=False` leaves the event out."""
    root = Path(attached.root)
    app = root / attached.cfg.repo
    base = git(app, "rev-parse", "refs/remotes/origin/main")
    branch = "%s%s" % (attached.cfg.branch_prefix, task)
    wt = Path(os.path.realpath(root)) / ".alpaca" / "attach" / "worktrees" / task
    wt.parent.mkdir(parents=True, exist_ok=True)
    git(app, "worktree", "add", "-q", "-b", branch, str(wt), base)
    branches = root / ".alpaca" / "attach" / "branches"
    branches.mkdir(parents=True, exist_ok=True)
    (branches / ("%s.json" % task)).write_text(json.dumps({
        "schema": 1, "task": task, "branch": branch, "worktree": str(wt), "base_sha": base,
        "baseline_key": "0" * 16, "fetch_url": attached.cfg.url, "cut_at": "2026-10-14T00:00:00+07:00",
        "worker": "builder"}, indent=2) + "\n", encoding="utf-8")
    if record:
        record_branch(root, task, branch=branch, worktree=wt, base_sha=base)
    return wt


def commit_in(wt, files, message="work"):
    """Write `files` ({path: text}) in the worktree and commit them; returns the new head sha."""
    for rel, text in files.items():
        target = Path(wt) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", message)
    return git(wt, "rev-parse", "HEAD")


def latest_seal(root, task="t-001"):
    from alpaca import proof
    conn = db.connect(root)
    try:
        return proof.latest_seal(conn, task)
    finally:
        conn.close()
