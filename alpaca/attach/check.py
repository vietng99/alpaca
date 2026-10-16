"""`alpaca attach check`: the gates on the branch head and on a merge preview, judged against the baseline
(op-062 t-649, R5, R14; build plan 6.3, P9, P12).

A check answers one question before anything leaves the studio: does this work branch break something
that worked, on its own and when it meets the newest upstream base? It runs the attach gates twice, each
time in fresh clones of a small scratch repository, and compares both runs with the baseline of the
commit the branch was cut from and the preview with the baseline of the commit it merges into
(`alpaca.attach.baseline`; the two are one when upstream has not moved):

  * a gate that passed at its base and failed at the head or on the preview is a regression and fails the check;
  * a gate that already failed at its base is `red_at_base` (the cut point) or `red_at_preview_base` (the merge
    target): it does not fail the check, it sets `needs_owner_review` (the handoff needs an owner card and the PR
    body says so);
  * a merge conflict fails the check and names the files; the preview gates are then not run.

A gate passes on exit status alone (exit 0 and no timeout, P9); no gate is skipped when another fails.

Order of code that is not ours (N8): the baselines (upstream's code) first, then every git fact (both seeds and the
merge, in folders this check claims with a mkdir that must not find the name taken), then the head gates, then the
preview gates. After each gate set the seeds are read again; a seed whose HEAD, tree or status changed fails the check.
The whole run is inside an `alpaca.attach.reap.Reaper`: what honest gate code leaves running is killed between gates,
and again before the run folders are deleted (adversarial round 2, N9b and N9c; the limits are in
`alpaca/attach/reap.py` and in the threat model below).

Threat model. The check judges an honest branch: its code can be wrong in any way a well-meant change can be wrong,
and the check must catch that. Gate code (the branch's tests and build) may fail, hang, time out, print anything,
edit tracked files in its own clone, leave processes running (also after a double fork or `setsid`; the reaper kills
them between gates), and commit `.gitattributes` merge drivers, binary files, renames and deletes. The clone's
config, `rerere`, merge drivers and the caller's git environment do not change the verdict.

The check is not a defence against branch code that works against it. Gate code runs as the same user as the check
and can make its own tests exit 0 (`conftest.py`), write the check's memory (`/proc/<pid>/mem`), rewrite seeds, the
record or the studio code, kill the check, or put a program first on PATH. Probes of each are kept as references in
`.alpaca/proofs/op-062/t-649.logs/follow-ups.md`. Only gates isolated under another user or on read-only mounts would
change this, and that is the release runner's part (`alpaca/release/checks.run_gates`). For a hidden conflict,
GitHub's mergeability check on the pull request is the backstop. The author of a work branch is the studio's own
worker, which has a shell as the same user, so this boundary costs nothing that a sandboxed gate alone would buy.

The merge preview is a committed merge made in a scratch seed repository (P12, pre-mortem t-649.1 to .3):

  * the seed is built with `baseline.make_seed`, which fetches the two commits from the attached clone by
    name. Nothing is registered in `app/.git`: no worktree, no `MERGE_HEAD`, no ref, no `rr-cache`;
  * the merge is `git merge-tree --write-tree` of the two verified shas (no working tree is touched) and the
    result is committed with `git commit-tree`, then checked out in the seed. The gate clones
    (`git clone --no-local <seed>`) therefore see the merged tree, where a merge left uncommitted in a working
    tree would be lost on the first clone;
  * the seed is a fresh repository: the clone's `merge.*.driver`, `rerere` and other repo-local settings do
    not apply, and `--attr-source=<empty tree>` switches off the merge attributes of the tree itself
    (`* merge=union` in a `.gitattributes` would resolve a conflict silently).

Everything the check makes lives under `.alpaca/attach/runs/<task>/<run>/` (never `/tmp`, T10) and is
deleted when the check ends, also after a timeout, a conflict or an error. A run folder left by a killed
check is deleted at the start of the next. A lock per task keeps two checks of one task from deleting each
other's folders.

The result is kept three ways: the `checks/<task>/<check-id>.json` file for people, the `attach-check`
event in the record (the whole result with the file's sha256, the authority `latest` reads), and the
verb's output. Every file and folder comes from `safefs`; every process is a git call through `gitops` or a
gate command through the release runner (`baseline.run_gate_set_each`).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

from alpaca.attach import AttachError, STATE_DIR, baseline, config, gitops, proven, reap, safefs, worktree

SCHEMA = 1
HEAD_REF = "refs/preview/head"
MERGE_REF = "refs/preview/merge"
#: the most conflicted paths a result keeps (the count is kept whole)
MAX_CONFLICTS = 200
#: characters of a gate's output tail kept on the record event (the file keeps the baseline's longer tail)
EVENT_TAIL = 500
PREVIEW_MESSAGE = "alpaca merge preview"
UNNAMED = "(git reported a conflict and named no file)"

_TASK = re.compile(worktree.TASK_ID)
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]*")
_REF = re.compile(r"refs/[A-Za-z0-9][A-Za-z0-9_./-]*")
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass
class CheckResult:
    task: str
    head: str
    base_sha: str
    preview_base: str
    verdict: str                                   # "PASS" | "FAIL"
    conflicts: list
    head_results: list
    preview_results: list
    red_at_base: list
    regressions: list
    needs_owner_review: bool
    gate_set: str
    path: str
    check_id: str = ""
    branch: str = ""
    created_at: str = ""
    conflicts_total: int = 0
    reason: str = ""
    file_sha256: str = ""
    #: the gates that are red at `preview_base`, the commit the preview merges into (R14); `red_at_base` is the
    #: gates red at `base_sha`, the commit the branch was cut from. Older events do not hold this key.
    red_at_preview_base: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data) -> "CheckResult":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


# ----------------------------------------------------------------------------------- the preview

def _need_task(task) -> str:
    if not isinstance(task, str) or not _TASK.fullmatch(task):
        raise AttachError("%r cannot name a work branch (a task id looks like t-123)" % str(task)[:40])
    return task


def _need_sha(value, what) -> str:
    text = str(value or "").strip().lower()
    if not _SHA.fullmatch(text):
        raise AttachError("%s is not a full commit sha" % what)
    return text


def _names(raw) -> list:
    """The conflicted paths of `git merge-tree --name-only -z`: the NUL-separated parts after the tree id."""
    out = []
    for part in raw.split(b"\0")[1:]:
        if part:
            name = part.decode("utf-8", "replace")
            if name not in out:
                out.append(name)
    return out


def merge_preview(app, seed, *, branch, base_ref, head=None, base=None):
    """Make the merge of `branch` into `base_ref` as a commit in the empty folder `seed` (made by the caller
    through `safefs.make_dir`) and return `(seed, [])`; with a conflict return `(None, [paths])` and leave
    the seed to the caller to delete. `head` and `base` are the full shas the two names must have (read
    from `app` when not given). Only reads `app`; nothing in it changes (P12)."""
    if not isinstance(branch, str) or not _BRANCH.fullmatch(branch) or ".." in branch or branch.endswith((".lock", "/")):
        raise AttachError("%r is not a branch name to preview" % str(branch)[:70])
    if not isinstance(base_ref, str) or not _REF.fullmatch(base_ref) or ".." in base_ref:
        raise AttachError("%r is not a ref name to preview against" % str(base_ref)[:70])
    head_ref = "refs/heads/%s" % branch
    head = _need_sha(head or gitops.git(app, "rev-parse", "--verify", "%s^{commit}" % head_ref), "the branch head")
    base = _need_sha(base or gitops.git(app, "rev-parse", "--verify", "%s^{commit}" % base_ref), "the base")
    if not os.path.isdir(str(seed)) or os.path.islink(str(seed)) or os.listdir(str(seed)):
        raise AttachError("the merge preview needs an empty folder of its own; %s is not" % os.path.basename(str(seed)))
    baseline.make_seed(app, seed, {baseline.BASE_REF: (base_ref, base), HEAD_REF: (head_ref, head)})
    if os.path.lexists(os.path.join(str(seed), ".git", "info", "attributes")):
        # $GIT_DIR/info/attributes outranks `--attr-source`: `* merge=union` there would resolve a conflict silently
        raise AttachError("the merge preview seed holds merge attributes of its own; it was not made by this check")
    empty = gitops.git(seed, "hash-object", "-t", "tree", "--stdin", input=b"")
    before = _merge_inputs(seed)
    # the verified shas, not the seed's refs: git would read refs/preview/* again from disk, and the merge and the
    # commit-tree below must name the same two commits (adversarial round 3, P3a; a deviation from plan step 8)
    res = gitops.run(seed, "--attr-source=%s" % empty, "merge-tree", "--write-tree", "--name-only", "--no-messages",
                     "-z", base, head, binary=True)
    if _merge_inputs(seed) != before:
        # merge-tree reads `.git/info/attributes` and `.git/config` in its own process; a file planted there and
        # removed again while it ran leaves the folder's ctime changed (adversarial round 2, N9c, second layer)
        raise AttachError("something changed the merge preview seed while git merged it; the check decided nothing")
    if res.rc == 1:
        return None, _names(res.out) or [UNNAMED]
    if res.rc != 0:
        err = " ".join(res.err.decode("utf-8", "replace").split())[-300:]
        if "unrelated histories" in err:
            raise AttachError("the branch and the upstream base share no history, so there is nothing to merge: %s" % gitops.scrub(err))
        raise AttachError("git merge-tree failed (it needs git 2.40 or newer): %s" % (gitops.scrub(err) or "exit %d" % res.rc))
    tree = res.out.split(b"\0")[0].decode("ascii", "replace").strip()
    merged = gitops.git(seed, "commit-tree", tree, "-p", base, "-p", head, "-m", PREVIEW_MESSAGE, identity=True)
    if gitops.git(seed, "rev-parse", "--verify", "%s^{tree}" % merged) != tree:
        raise AttachError("the merge preview commit does not hold the merged tree")
    gitops.git(seed, "update-ref", MERGE_REF, merged)
    gitops.git(seed, "checkout", "-q", "--detach", merged)
    return Path(seed), []


def _merge_inputs(seed) -> tuple:
    """What git reads besides the objects when it merges in `seed`: the lstat (inode, size, mtime, ctime) of
    `.git`, `.git/info` and `.git/config`, and whether `.git/info/attributes` exists. merge-tree writes only
    under `.git/objects`, so none of these changes when nothing else touches the seed."""
    out = []
    for parts in ((".git",), (".git", "info"), (".git", "config"), (".git", "info", "attributes")):
        try:
            st = os.lstat(os.path.join(str(seed), *parts))
            out.append((st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns))
        except OSError:
            out.append(None)
    return tuple(out)


# ----------------------------------------------------------------------------------- the rules

def _compact(results) -> list:
    return [{"name": g["name"], "returncode": g["returncode"], "timed_out": g["timed_out"], "passed": g["passed"],
             "duration_s": g["duration_s"], "detail": g["detail"], "log_tail": g.get("log_tail") or ""} for g in results]


def _red(gates) -> list:
    return [g["name"] for g in gates if not g.get("passed")]


def judge(base_gates, head_results, preview_results, conflicts, preview_base_gates=None, drift=()):
    """`(verdict, regressions, red_at_base, reason)` from the baseline's gate results, the two runs and the
    conflicts. Exit status only (P9): `passed` is exit 0 and no timeout. A gate with no baseline entry, or
    one that is missing from a run, is a regression, never a pass by absence.

    The head run is compared with the baseline of the commit the branch was cut from (`base_gates`). The
    preview run is compared with the baseline of the commit it merges into (`preview_base_gates`, which is
    `base_gates` when upstream has not moved): a gate that upstream fixed and the merge breaks again is a
    regression, not "red at base" (R14, adversarial round 1 N1). `drift` names seeds a gate changed; each one
    fails the check."""
    if preview_base_gates is None:
        preview_base_gates = base_gates
    regressions = []
    for where, results, gates in (("head", head_results, base_gates), ("preview", preview_results, preview_base_gates)):
        at_base = {g.get("name"): g for g in gates}
        for r in results:
            was = at_base.get(r["name"])
            if (was is None or was.get("passed")) and not r["passed"]:
                regressions.append({"gate": r["name"], "at": where, "returncode": r["returncode"],
                                    "timed_out": bool(r["timed_out"])})
    reasons = []
    if conflicts:
        reasons.append("merge conflict in %d file%s" % (len(conflicts), "" if len(conflicts) == 1 else "s"))
    if regressions:
        reasons.append("gate regression: " + ", ".join("%s (%s)" % (r["gate"], r["at"]) for r in regressions))
    for note in drift:
        reasons.append(note)
    return ("FAIL" if reasons else "PASS"), regressions, _red(base_gates), "; ".join(reasons)


# ----------------------------------------------------------------------------------- the run

def _rel(*parts) -> str:
    return "/".join((STATE_DIR,) + parts)


def _clean(app, task, folder) -> bool:
    return gitops.in_worktree(app, task, folder, "status", "--porcelain", "--untracked-files=normal") == ""


def _check_id() -> str:
    return "%s-%s" % (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), uuid.uuid4().hex[:8])


def _record(root, session, result, task) -> None:
    from alpaca import db
    conn = db.connect(str(root))
    try:
        data = result.to_dict()
        for key in ("head_results", "preview_results"):
            data[key] = [dict(g, log_tail=(g.get("log_tail") or "")[-EVENT_TAIL:]) for g in data[key]]
        db.append_event(conn, session=session, actor="alpaca", kind="attach-check", op=baseline._op_of(conn, task), ref=task,
                        data={"check_id": result.check_id, "task": task, "verdict": result.verdict, "head": result.head,
                              "base_sha": result.base_sha, "preview_base": result.preview_base, "path": result.path,
                              "file_sha256": result.file_sha256, "result": data})
    finally:
        conn.close()


def run(root, task, *, session="cli") -> CheckResult:
    """Check the work branch of `task` and return the result (also kept in `checks/` and the record).

    Refuses, with a one-line AttachError and nothing run: no work branch, a work folder that is not the
    proved folder of the task on its branch, uncommitted changes in it, a clone whose git config changed
    since attach (P8), no gates. A conflict or a regression is a result with verdict FAIL, not a refusal."""
    task = _need_task(task)
    cfg = config.load(root)
    if not cfg.gates:
        raise AttachError("no attach gates")
    app = str(cfg.repo_path(root))
    if not gitops.is_toplevel(app):
        raise AttachError("%s/ is not a git checkout of its own; run alpaca attach status" % cfg.repo)
    view = proven.branch_view(root, cfg, task)
    if view is None:
        raise AttachError("%s has no work branch; claim the task first (alpaca task claim)" % task)
    folder, branch = view["worktree"], view["branch"]
    try:
        gitops.prove_worktree(app, task, folder, branch)
    except AttachError as exc:
        raise AttachError("%s cannot be checked: %s" % (task, exc)) from None
    if not _clean(app, task, folder):
        raise AttachError("the work folder of %s has uncommitted changes; commit or discard them first "
                          "(the check judges the committed head)" % task)
    proven.config_unchanged(root, cfg, app)
    fd = safefs.open_lock(root, _rel("checks", "%s.lock" % task))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _locked_run(root, cfg, app, task, view, session)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _claim(root, path) -> str:
    """Make the folder `path` and fail when the name is already taken. A folder a gate will run in, or git will
    write into, is this check's own: an existing one was made by something else (N8)."""
    try:
        return safefs.make_dir(root, path, exclusive=True)
    except FileExistsError:
        raise AttachError("%s already exists; the check made nothing and ran nothing" % os.path.basename(str(path))) from None


def _seed_state(seed) -> tuple:
    """`(HEAD sha, HEAD tree, porcelain status)` of a seed, read through gitops (which drops hooks and fsmonitor)."""
    return (gitops.git(seed, "rev-parse", "--verify", "HEAD"), gitops.git(seed, "rev-parse", "--verify", "HEAD^{tree}"),
            gitops.git(seed, "status", "--porcelain", "--untracked-files=all"))


def _drifted(seeds) -> list:
    """One note per seed whose HEAD, tree or status differs from what it was before any gate ran. Gate code runs
    with the user's rights, so it can move a seed between two gates; the next gate would then clone another tree
    (adversarial round 1, N9). A seed that cannot be read counts as changed. This is detection after a gate set,
    not prevention: gate code that undoes its change before the set ends is not seen, and no check inside
    this module can stop code that runs with the user's rights."""
    notes = []
    for where, (seed, before) in seeds.items():
        try:
            now = _seed_state(seed)
        except AttachError:
            now = None
        if now != before:
            notes.append("a gate changed the %s seed" % where)
    return notes


def _locked_run(root, cfg, app, task, view, session) -> CheckResult:
    from alpaca import util
    folder, branch = view["worktree"], view["branch"]
    gitops.prove_worktree(app, task, folder, branch)                 # again: the lock may have waited
    head = gitops.in_worktree(app, task, folder, "rev-parse", "--verify", "HEAD")
    ref_sha = gitops.git(app, "rev-parse", "--verify", "refs/heads/%s^{commit}" % branch)
    if head != ref_sha:
        raise AttachError("the work folder of %s is at %s but %s is at %s" % (task, head[:12], branch, ref_sha[:12]))
    if not _clean(app, task, folder):
        raise AttachError("the work folder of %s has uncommitted changes; commit or discard them first" % task)
    preview_base = worktree.fetch_base(root, cfg)
    base_sha = view["base_sha"]
    check_id = _check_id()
    runs = _rel("runs", task)
    drift = []
    # every gate below runs inside the reaper: whatever a gate leaves running (a double fork, a setsid) is handed
    # to this process and killed when the gate ends, before the next clone, and again before the folders go (N9c)
    with reap.Reaper() as reaper:
        base = baseline.ensure(root, base_sha, task=task, session=session)  # a new gate set makes a new baseline (P11)
        # the preview is judged at the commit it merges into; the baseline is kept by sha and gate set, so an
        # upstream that has not moved costs nothing and one that moved runs upstream's own gates once (N1)
        target = base if preview_base == base_sha else baseline.ensure(root, preview_base, task=task, session=session)
        reaper.reap()
        safefs.remove_tree(root, runs)                               # a killed check's leftovers; the lock is held
        run_dir = _claim(root, _rel("runs", task, check_id))
        try:
            # every folder is claimed (mkdir that must not find the name taken) and both seeds, the merge among
            # them, are built here, before any gate runs: a conflict is a git fact about two commits, and the
            # branch's own code (a test runner loads conftest.py) must not get to a folder git still has to use (N8)
            head_seed = _claim(root, os.path.join(run_dir, "seed-head"))
            head_work = _claim(root, os.path.join(run_dir, "work-head"))
            baseline.make_seed(app, head_seed, {HEAD_REF: ("refs/heads/%s" % branch, head)})
            preview_seed = _claim(root, os.path.join(run_dir, "seed-preview"))
            preview_work = _claim(root, os.path.join(run_dir, "work-preview"))
            seed, conflicts = merge_preview(app, preview_seed, branch=branch, head=head, base=preview_base,
                                            base_ref="refs/remotes/%s/%s" % (cfg.upstream, cfg.base))
            seeds = {"head": (head_seed, _seed_state(head_seed))}
            if seed is not None:
                seeds["preview"] = (preview_seed, _seed_state(preview_seed))
            head_results = baseline.run_gate_set_each(cfg, head_seed, head_work, reaper)
            reaper.reap()                                            # nothing a gate started reads the seeds with us
            drift += _drifted(seeds)
            preview_results = []
            if seed is not None and not drift:
                preview_results = baseline.run_gate_set_each(cfg, seed, preview_work, reaper)
                reaper.reap()
                drift += _drifted(seeds)
        finally:
            try:
                reaper.reap()                                        # nothing left to write while the folders go
            finally:
                try:
                    safefs.remove_tree(root, runs)
                except AttachError:
                    pass
    verdict, regressions, red, reason = judge(base["gates"], head_results, preview_results, conflicts,
                                              preview_base_gates=target["gates"], drift=drift)
    red_target = _red(target["gates"])
    rel = _rel("checks", task, "%s.json" % check_id)
    result = CheckResult(task=task, head=head, base_sha=base_sha, preview_base=preview_base, verdict=verdict,
                         conflicts=conflicts[:MAX_CONFLICTS], head_results=_compact(head_results),
                         preview_results=_compact(preview_results), red_at_base=red, regressions=regressions,
                         needs_owner_review=bool(red or red_target), gate_set=cfg.gate_set_digest(), path=rel, check_id=check_id,
                         branch=branch, created_at=util.now_iso(), conflicts_total=len(conflicts), reason=reason,
                         red_at_preview_base=red_target)
    data = (json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")
    result.file_sha256 = hashlib.sha256(data).hexdigest()          # of the file as written, which does not hold it
    safefs.write_bytes(root, rel, data, mode=0o600)
    try:
        _record(root, session, result, task)
    except BaseException:
        try:
            safefs.remove(root, rel)                                  # no event, no check: a retry redoes both
        except AttachError:
            pass
        raise
    return result


def latest(root, task):
    """The newest recorded check of `task` as a CheckResult, or None. Read from the `attach-check` event,
    which only `run` writes; the files under `checks/` are output for people and decide nothing."""
    if not isinstance(task, str) or not _TASK.fullmatch(task):
        return None
    for _, data in proven._events(root, "attach-check", ref=task, limit=1):
        found = data.get("result")
        if isinstance(found, dict) and found.get("task") == task and found.get("verdict") in ("PASS", "FAIL"):
            try:
                return CheckResult.from_dict(found)
            except TypeError:
                return None
    return None


# ----------------------------------------------------------------------------------- the verb

def add_parser(verbs):
    p = verbs.add_parser("check", help="run the attach gates on a task's branch head and on a merge preview")
    p.add_argument("task", help="the task whose work branch to check (t-123)")
    p.add_argument("--json", action="store_true", help="print one JSON object")


def _lines(res) -> list:
    base = set(res.red_at_base)
    lines = ["alpaca attach check: %s branch %s head %s base %s preview base %s" % (
        res.task, res.branch, res.head[:12], res.base_sha[:12], res.preview_base[:12])]
    preview = {g["name"]: g for g in res.preview_results}
    for g in res.head_results:
        p = preview.get(g["name"])
        lines.append("  gate %s: head %s, preview %s%s" % (
            g["name"], "PASS" if g["passed"] else "FAIL",
            "not run" if p is None else ("PASS" if p["passed"] else "FAIL"),
            ", red at base" if g["name"] in base else ""))
    for path in res.conflicts[:20]:
        lines.append("  conflict: %s" % _one_line(path))
    if res.conflicts_total > min(len(res.conflicts), 20):
        lines.append("  ... and %d more conflicted files" % (res.conflicts_total - min(len(res.conflicts), 20)))
    for r in res.regressions:
        lines.append("  regression: gate %s %s%s" % (r["gate"], r["at"], " (timed out)" if r["timed_out"] else ""))
    if res.red_at_base:
        lines.append("  red at base: %s (the handoff needs an owner review)" % ", ".join(res.red_at_base))
    if res.red_at_preview_base:
        lines.append("  red at the preview base: %s (the handoff needs an owner review)" % ", ".join(res.red_at_preview_base))
    lines.append("  record: %s" % res.path)
    return lines


def _one_line(text) -> str:
    return "".join(c if c.isprintable() else "?" for c in str(text))[:300]


def run_verb(args) -> int:
    from alpaca import cli as alpaca_cli
    from alpaca.attach.cli import finish
    root = alpaca_cli._root()
    try:
        res = run(root, args.task, session=getattr(args, "session", None) or "cli")
    except AttachError as exc:
        return finish(args, "check", "BLOCKED", str(exc), task=getattr(args, "task", None))
    receipt = {"check_id": res.check_id, "head": res.head, "base_sha": res.base_sha, "preview_base": res.preview_base,
               "verdict": res.verdict, "conflicts": res.conflicts, "regressions": res.regressions,
               "red_at_base": res.red_at_base, "red_at_preview_base": res.red_at_preview_base, "needs_owner_review": res.needs_owner_review, "path": res.path,
               "file_sha256": res.file_sha256}
    return finish(args, "check", res.verdict, res.reason, lines=_lines(res), receipt=receipt, task=res.task)


SUBVERBS = {"check": (add_parser, run_verb)}
