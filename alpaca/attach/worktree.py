"""The work branch of a task: one git worktree of the attached clone, cut from a fresh base (op-062 t-648, R4).

`alpaca task claim` in an attach studio calls `after_claim` (alpaca/ops.py, behind one `config.mode`
test). A first claim fetches the upstream base, then cuts `<branch_prefix><task>` from the fetched
commit into `.alpaca/attach/worktrees/<task>`, records `branches/<task>.json` (output for people) and
the `attach-branch` event (the authority), and the baseline of that commit. The checkout in `app/` is
never touched: no checkout, reset, merge or pull runs there, so it goes stale by design and nothing here
reads it as the base (pre-mortem t-648.1).

Two rules hold for every line below (t-648 class review r2):

  1. Plain files under `.alpaca/attach/` are output. A decision reads `alpaca.attach.proven`, which
     returns only what the project.yaml `attach:` block, the append-only record, a folder derived from the
     task id, or git proved. `test_attach_read_guard.py` fails on any other read.
  2. git acts in a task's work folder only through `gitops.in_worktree`, which names the repository on the
     command line, after `gitops.prove_worktree` read the folder, its `.git` file and the admin folder byte
     by byte. Every attach git call runs with replace objects and grafts off (`gitops.clean_env`).

Failure rules, each from a pre-mortem line:

  * every claim fetches, a first claim and a claim that resumes a branch alike. The fetch uses
    `attach.url` of project.yaml, only while it equals the URL the `attach-init` event recorded, under one
    lock, and a failed fetch fails the claim. It never falls back to the old remote-tracking ref (t-648.5,
    P8). The clone's config digest and soundness are checked again at each fetch, and the fetched tree is
    listed for ALPACA-MANIFEST before anything is cut or moved;
  * a claim that did not win the lease neither cuts nor releases (t-648.2, enforced in `after_claim`);
  * a cut that fails undoes what it made (worktree, admin folder, branch, record file) through no link and
    never outside the studio. `after_claim` releases the claim only while this worker still holds the lease
    it took (`release_if_held`), so the task goes back to open and a later holder keeps its lease. The
    `attach-branch` event is the last step of a cut and is written in one transaction with a check that the
    lease is still held, so nothing can fail after it;
  * a task that is claimed again after its lease ran out resumes its branch. With no commit of its own
    and a clean worktree the branch moves to the fresh base; with commits, or when the branch is also checked
    out elsewhere, or an ignored file is in the way, or the new base holds ALPACA-MANIFEST, it is kept and
    the record and the claim line say how far upstream is ahead;
  * commits in the worktree carry the identity set at attach (`alpaca-agent@localhost`), because the
    worktree shares the clone's config (t-648.7);
  * the worktree sits under the ignored `.alpaca/attach/`, so `git add -A` in the studio stages no
    gitlink (X4a).

Lock order: `fetch.lock` first, then the db write lock (`_commit_event` takes it while the fetch lock is held).
Nothing takes `fetch.lock` while a db transaction is open (`test_attach_claim.py`, lock-order test).
"""
from __future__ import annotations

import fcntl
import os
import re
from contextlib import contextmanager

from alpaca.attach import AttachError, STATE_DIR, baseline, config, gitops, proven, safefs

TASK_ID = r"t-[0-9]{1,6}"
_TASK = re.compile(TASK_ID)
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _rel(*parts) -> str:
    return "/".join((STATE_DIR,) + parts)


def _app(root, cfg) -> str:
    app = str(cfg.repo_path(root))
    if not gitops.is_toplevel(app):
        raise AttachError("%s/ is not a git checkout of its own; run alpaca attach status" % cfg.repo)
    return app


def _need_task(task) -> str:
    if not isinstance(task, str) or not _TASK.fullmatch(task):
        raise AttachError("%r cannot name a work branch (a task id looks like t-123)" % str(task)[:40])
    return task


@contextmanager
def _locked(root):
    """The one lock for changes to the clone's git state (a fetch, a worktree add): git itself locks
    each ref, but a fetch racing a worktree add can still fail with `cannot lock ref`."""
    fd = safefs.open_lock(root, _rel("fetch.lock"))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ------------------------------------------------------------------------------------ the fetch

def _manifest_refusal(hits, sha) -> str:
    where = hits[0]
    return ("the upstream base %s holds ALPACA-MANIFEST (%s); a work folder cut from it would look like a second "
            "studio root, so attach will not cut or move a branch to it" % (sha[:12], where))


def _fetch(root, cfg, app):
    """Fetch the upstream base; returns (sha, manifest hits in its tree). The URL, the clone's config and
    the clone itself are proved first, inside the caller's lock."""
    url = proven.fetch_url(root, cfg)
    proven.config_unchanged(root, cfg, app)
    problems = gitops.clone_problems(app, cfg.repo + "/")
    if problems:
        raise AttachError("the attached clone is no longer sound: %s" % problems[0])
    ref = "refs/remotes/%s/%s" % (cfg.upstream, cfg.base)
    gitops.remote(app, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
                  "--", url, "+refs/heads/%s:%s" % (cfg.base, ref), url=url)
    sha = gitops.git(app, "rev-parse", "--verify", "%s^{commit}" % ref)
    return sha, gitops.tree_manifest_hits(app, sha)


def fetch_base(root, cfg) -> str:
    """Fetch the upstream base and return its commit sha. Raises AttachError when the fetch fails, or when
    the fetched tree holds ALPACA-MANIFEST."""
    app = _app(root, cfg)
    with _locked(root):
        sha, hits = _fetch(root, cfg, app)
    if hits:
        raise AttachError(_manifest_refusal(hits, sha))
    return sha


# ------------------------------------------------------------------------------------ the lease

def still_holds(conn, task, worker, fence) -> bool:
    """True while the record folds `(worker, fence)` as the holder of `task`. A lease whose time has
    passed with nobody else holding it still counts: it is this worker's to release."""
    from alpaca import board
    held = board.fold_leases(conn, [task]).get(task)
    return bool(held) and held.get("worker") == worker and held.get("fence") == fence


def release_if_held(conn, task, worker, fence, *, session) -> bool:
    """End `worker`'s lease on `task` only while it is the lease taken with `fence`. `claims.release`
    writes a release without asking who holds the task, so a worker whose lease ended meanwhile (a
    deferral, a takeover) would end the next holder's lease. The check and the release are one
    transaction. Returns True when a release was written (t-648 pre-mortem 2, adversarial B3)."""
    from alpaca import claims, db
    with db.transaction(conn):
        if not still_holds(conn, task, worker, fence):
            return False
        claims.release(conn, task, worker, session=session)
    return True


def _need_lease(root, task, worker, fence) -> None:
    """Refuse to go on when the lease taken at the claim has moved on (an early exit before anything is
    made; the binding check is the one inside `_commit_event`)."""
    if fence is None:
        return
    from alpaca import db
    conn = db.connect(str(root))
    try:
        ok = still_holds(conn, task, worker, fence)
    finally:
        conn.close()
    if not ok:
        raise AttachError("the lease on %s moved on during the cut; claim it again" % task)


# ------------------------------------------------------------------------------------ the cut

def _branch_exists(app, branch) -> bool:
    return gitops.run(app, "show-ref", "--verify", "--quiet", "refs/heads/%s" % branch).rc == 0


def _derived(root, task) -> str:
    return os.path.join(os.path.realpath(str(root)), _rel("worktrees", task))


def _undo(root, app, task, branch, folder, made_branch, made_tree, made_record):
    """Take away what this cut made, through no link and never outside the studio (Alpaca rule 3).

    The work folder goes through `safefs.remove_tree`, and a folder that a swap turned into a link through
    `safefs.drop_link` (the link only, never its target). When a worktree was added, the admin folder of
    this cut's branch in `app/.git/worktrees` is removed directly: `git worktree remove` would follow the
    path git registered (which can be outside the studio) and a global `worktree prune` would drop another
    task's folder. Then the branch, then the record file this call wrote. Each step is tried on its own so
    a failure in one leaves the rest done."""
    steps = [lambda: safefs.remove_tree(root, _rel("worktrees", task)),
             lambda: safefs.drop_link(root, _rel("worktrees", task))]
    if made_tree:
        steps.append(lambda: [safefs.remove_tree(root, p) for p in gitops.worktree_admins_on(app, branch)])
    if made_branch:
        steps.append(lambda: gitops.run(app, "branch", "-D", "--quiet", branch))
    if made_record:
        steps.append(lambda: safefs.remove(root, _rel("branches", "%s.json" % task)))
    for step in steps:
        try:
            step()
        except (AttachError, OSError):
            pass


def _write_record(root, task, payload) -> None:
    """`branches/<task>.json`: the same facts as the `attach-branch` event, for people and `attach status`."""
    config.write_json_atomic(config.state_dir(root) / "branches" / ("%s.json" % task), payload, root=root)


def _commit_event(root, task, worker, fence, session, payload, *, resumed=False) -> None:
    """The `attach-branch` event, the last step of a cut or a resume. One transaction holds the check that
    `worker` still holds the lease taken with `fence` and the event itself, so a lease that moved on before
    this point writes nothing, and nothing after this point can fail."""
    from alpaca import db
    conn = db.connect(str(root))
    try:
        data = {k: payload.get(k) for k in ("branch", "worktree", "base_sha", "baseline_key", "worker")}
        if resumed:
            data.update(resumed=True, upstream_sha=payload.get("upstream_sha"), behind=payload.get("behind"))
        if payload.get("kept"):
            data["kept"] = payload["kept"]
        with db.transaction(conn):
            if fence is not None and not still_holds(conn, task, worker, fence):
                raise AttachError("the lease on %s moved on during the cut; claim it again" % task)
            db.append_event(conn, session=session, actor=worker, kind="attach-branch", op=baseline._op_of(conn, task),
                            ref=task, data=data, conn_in_txn=True)
    finally:
        conn.close()


def _known(root, cfg, app, task, branch):
    """The proved view of the task's work branch when it already has one, None when it has no record. A
    record whose folder fails the git proof is a refusal that names the repair, not a silent fresh cut."""
    view = proven.branch_view(root, cfg, task)
    if view is None:
        return None
    try:
        gitops.prove_worktree(app, task, view["worktree"], branch)
    except AttachError as exc:
        raise AttachError("%s cannot be resumed: %s" % (task, exc)) from None
    return view


def cut(root, task, *, worker, session, fence=None) -> dict:
    """Fetch the upstream base, then cut `<branch_prefix><task>` from it into its own worktree and return
    the `branches/<task>.json` payload. A task that already has a proved work branch gets that one back,
    after the same fetch (see `_resume`). `fence` is the fence of the lease just taken: the cut stops when
    that lease moved on before the worktree is made, and the event that ends the cut is written only while
    it is still held."""
    _need_task(task)
    cfg = config.load(root)
    app = _app(root, cfg)
    branch = cfg.branch_prefix + task
    with _locked(root):
        known = _known(root, cfg, app, task, branch)
        if known is None:
            if _branch_exists(app, branch):
                raise AttachError("branch %s already exists in %s/ and has no work folder recorded for %s; to keep its commits "
                                  "bring the folder back (git -C %s worktree add .alpaca/attach/worktrees/%s %s), to start "
                                  "over delete the branch (git -C %s branch -D %s)"
                                  % (branch, cfg.repo, task, cfg.repo, task, branch, cfg.repo, branch))
            if os.path.lexists(_derived(root, task)) or os.path.lexists(gitops.admin_dir(app, task)):
                raise AttachError("the work folder of %s is left over without a recorded branch; remove it with git -C %s "
                                  "worktree remove --force .alpaca/attach/worktrees/%s, or delete the folder and run "
                                  "git -C %s worktree prune" % (task, cfg.repo, task, cfg.repo))
        sha, hits = _fetch(root, cfg, app)         # every claim fetches: no work branch rests on a stale base
        if known is None and hits:
            raise AttachError(_manifest_refusal(hits, sha))
    if known is not None:
        return _resume(root, cfg, app, task, branch, known, sha, hits, worker=worker, session=session, fence=fence)
    from alpaca import util
    # the baseline (it may run for minutes) is made before anything exists to undo
    base = baseline.ensure(root, sha, task=task, session=session)
    with _locked(root):
        proven.config_unchanged(root, cfg, app)
        if _branch_exists(app, branch) or os.path.lexists(_derived(root, task)) \
                or os.path.lexists(gitops.admin_dir(app, task)):
            raise AttachError("the work branch or folder of %s appeared while it was being cut" % task)
        _need_lease(root, task, worker, fence)
        parent = safefs.make_dir(root, _rel("worktrees"))
        try:
            folder = safefs.make_dir(root, os.path.join(parent, task), exclusive=True)
        except FileExistsError:
            raise AttachError("the work folder of %s appeared while it was being cut" % task) from None
        made_branch = made_tree = made_record = False
        try:
            # the branch is made first and on its own: `git branch` refuses a name that exists, so a branch made by
            # someone else a moment after the check above is never ours to delete. The admin folder is found by its
            # HEAD (this cut's branch), whatever name git gave it.
            gitops.git(app, "branch", "--quiet", "--no-track", branch, sha)
            made_branch = True
            made_tree = True                           # set before the add: a failed add can leave an admin folder
            gitops.git(app, "worktree", "add", "--quiet", folder, branch)
            gitops.prove_worktree(app, task, folder, branch)
            head = gitops.in_worktree(app, task, folder, "rev-parse", "--verify", "HEAD")
            if head != sha:
                raise AttachError("the new worktree of %s is at %s, not the fetched base %s" % (task, head[:12], sha[:12]))
            payload = {"schema": 1, "task": task, "branch": branch, "worktree": folder, "base_sha": sha,
                       "baseline_key": base["key"], "fetch_url": cfg.url, "cut_at": util.now_iso(), "worker": worker}
            _write_record(root, task, payload)
            made_record = True
            _commit_event(root, task, worker, fence, session, payload)
        except BaseException:
            _undo(root, app, task, branch, folder, made_branch, made_tree, made_record)
            raise
    return payload


def _clean(app, task, folder) -> bool:
    return gitops.in_worktree(app, task, folder, "status", "--porcelain", "--untracked-files=normal") == ""


def _resume(root, cfg, app, task, branch, view, sha, hits, *, worker, session, fence) -> dict:
    """The task already has a proved work branch (its lease ran out and the task was claimed again) and the
    upstream base was just fetched as `sha`. Nothing the task did is thrown away.

      * no commit of its own (the branch still sits on its recorded base and that base is an ancestor of
        `sha`), a clean worktree, a fetched tree without ALPACA-MANIFEST, the branch checked out nowhere
        else and no ignored file in the way: the branch moves to `sha` in its own worktree (never in app/),
        with the baseline of `sha` made first, and the record names the new base;
      * otherwise the branch stays; the record gets `upstream_sha` and `behind` (commits of `sha` the
        branch lacks) and, when a reason other than commits kept it, `kept`, so the claim line and
        `attach check` show how stale the base is and why.
    The base comes from the `attach-branch` event, and when that commit is no longer an ancestor of the
    branch (a worker rewrote history) from git's merge base, so a rewritten branch can still be claimed.
    Whatever fails here leaves the branch and the worktree exactly as they were; if the event step fails
    after the branch moved, the next resume finds the branch at `sha` and writes the event."""
    from alpaca import util
    folder = view["worktree"]
    head = gitops.git(app, "rev-parse", "--verify", "refs/heads/%s^{commit}" % branch)
    old = view["base_sha"]
    if gitops.run(app, "merge-base", "--is-ancestor", old, head).rc != 0:
        mb = gitops.run(app, "merge-base", head, sha)
        old = mb.out.strip() if mb.rc == 0 and _SHA.fullmatch(mb.out.strip()) else None
    advance = (old == head and head != sha and not hits and _clean(app, task, folder)
               and gitops.run(app, "merge-base", "--is-ancestor", head, sha).rc == 0)
    healing = head == sha and old != head        # the branch already moved; only the event is missing
    base = baseline.ensure(root, sha, task=task, session=session) if (advance or healing) else None
    with _locked(root):
        proven.config_unchanged(root, cfg, app)
        gitops.prove_worktree(app, task, folder, branch)         # again: the files may have changed meanwhile
        _need_lease(root, task, worker, fence)
        elsewhere = [w["path"] for w in gitops.worktree_list(app) if w["branch"] == "refs/heads/%s" % branch and w["path"] != folder]
        kept = None
        if hits:
            kept = "the fetched base holds ALPACA-MANIFEST (%s)" % hits[0]
        elif elsewhere:
            kept = "the branch is also checked out in %s" % elsewhere[0]
        moved = False
        if advance and kept is None:
            if gitops.git(app, "rev-parse", "--verify", "refs/heads/%s^{commit}" % branch) != head \
                    or not _clean(app, task, folder):
                raise AttachError("the work branch of %s changed while it was being moved to the fresh base" % task)
            gitops.in_worktree(app, task, folder, "merge", "--ff-only", "--no-overwrite-ignore", "--quiet", sha, check=False)
            now = gitops.in_worktree(app, task, folder, "rev-parse", "--verify", "HEAD")
            if now == sha:
                gitops.prove_worktree(app, task, folder, branch)
                moved = True
            elif now == head:
                kept = "a file in the work folder that git ignores is in the way of the new base"
            else:
                raise AttachError("the work branch of %s changed during the move to the fresh base" % task)
        if moved or healing:
            behind, new_base, key = 0, sha, base["key"]
        else:
            behind = int(gitops.git(app, "rev-list", "--count", "refs/heads/%s..%s" % (branch, sha)) or 0)
            new_base, key = old or view["base_sha"], view.get("baseline_key")
        payload = {"schema": 1, "task": task, "branch": branch, "worktree": folder, "base_sha": new_base,
                   "baseline_key": key, "fetch_url": cfg.url, "worker": worker, "upstream_sha": sha,
                   "behind": behind, "resumed_at": util.now_iso()}
        if kept:
            payload["kept"] = kept
        _write_record(root, task, payload)
        _commit_event(root, task, worker, fence, session, payload, resumed=True)
    return payload


# ------------------------------------------------------------------------------------ the claim hook

def _lines(task, info) -> list:
    branch, folder, base = info.get("branch"), info.get("worktree"), info.get("base_sha")
    if info.get("resumed_at") and info.get("upstream_sha"):
        line = ("alpaca attach: %s resumed work branch %s in %s (base %s, upstream now %s, %s behind)"
                % (task, branch, folder, str(base)[:12], str(info["upstream_sha"])[:12], info.get("behind", 0)))
        if info.get("kept"):
            line += " - kept on its base: %s" % info["kept"]
        return [line]
    return ["alpaca attach: %s work branch %s in %s (base %s)" % (task, branch, folder, str(base)[:12])]


def on_claim(root, task, worker, res, *, session) -> list:
    """Called by `after_claim` once the lease is won. A first claim (`res` carries the claim event) cuts
    the branch, or resumes the one it already has; a renewal needs the proved work folder to exist.
    Returns the lines to print and prints nothing; raises AttachError, which `after_claim` turns into a
    failed claim."""
    _need_task(task)
    if "claim" in res:
        info = cut(root, task, worker=worker, session=session, fence=res.get("fence"))
    else:
        cfg = config.load(root)
        info = proven.branch_view(root, cfg, task)
        if info is None:
            raise AttachError("%s has no work branch yet (it was claimed before attach); release it and claim it again" % task)
        gitops.prove_worktree(_app(root, cfg), task, info["worktree"], info["branch"])
    return _lines(task, info)


def after_claim(conn, root, task, worker, res, *, session):
    """The one end point of an attach claim. Returns None when the claim stands (after printing the work
    branch line), else the text of the refusal. Nothing is printed before the lease was checked again after
    the last side effect.

    A claim that did not win the lease neither cuts nor releases. A won claim that fails for any reason, a
    surprise included, releases the lease it took, only while this worker still holds it, so the task goes
    back to open and a later holder keeps its lease. A renewal never releases (the lease is older than this
    call). A Ctrl-C or SystemExit releases and is raised again."""
    import sqlite3
    from alpaca import claims
    if res["status"] != claims.CLAIMED:
        return "%s holds %s" % (res.get("worker"), task)
    fresh = "claim" in res
    try:
        lines = on_claim(root, task, worker, res, session=session)
        if not still_holds(conn, task, worker, res.get("fence")):
            raise AttachError("the lease on %s moved on during the claim; claim it again" % task)
    except BaseException as exc:
        note = ""
        if fresh:
            try:
                release_if_held(conn, task, worker, res.get("fence"), session=session)
            except Exception as failed:
                note = " (the lease release also failed: %s)" % gitops.scrub(failed)[:200]
        if isinstance(exc, (AttachError, OSError, sqlite3.Error)):
            return "attach: %s%s" % (exc, note)
        if isinstance(exc, Exception):
            return "attach: internal %s: %s%s" % (type(exc).__name__, gitops.scrub(exc)[:200], note)
        raise
    for line in lines:
        print(line)
    return None
