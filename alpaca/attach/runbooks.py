"""Runbooks and research packs in an attached project (op-062 t-653, requirement R15).

Three things, all inert in a project without an `attach:` block:

  * `attach_on` and `pack_outside_allowed` are what `alpaca/runbook.py` and `alpaca/research/contract.py` ask
    before they relax a rule. The runbook checker accepts `${ATTACH_WORKTREE}` (the absolute path of the task's
    work folder) only when `attach_on` says yes, and the three refusals of a pack outside `repo.path`
    (`PACK-OUTSIDE-REPO` in the checker, `CONTRACT-PACK-OUTSIDE-REPO` in the loader and in `check_overlap`)
    lift only when `pack_outside_allowed` says yes: attach is on, `repo.path` is the attached repository itself
    (the same path, not another spelling of it), and the pack folder is a plain folder of the studio.
  * `run_stage` is the small stage runner behind `alpaca attach stage` (AC-16). `alpaca/runbook.py` has a checker
    and the check evaluator but no executor for a plain stage, so a stage cannot "run in the task worktree"
    without one. It runs one stage of a format 1 or 2 runbook with `${ATTACH_WORKTREE}` set, judges the stage's
    checks, writes the evidence under `.alpaca/attach/stages/<task>/<stage>/` and commits nothing.
  * `land` puts a patch (what a research run's champion changed) on the task's work branch as one commit. It
    refuses a patch that touches `.alpaca/`, adds a file that is byte for byte a file of the studio or of a
    research pack, or lands on a work folder that has changes of its own (PM t-653.4: the pack is never
    committed, and what a stage wrote into its tree does not ride along). The commit is built from the tree
    that was checked (`commit-tree`, then `update-ref` against the head that was read first), never from the
    live index, and a per-task lock keeps two lands, or a land and a stage, of one task apart.

Every file this module writes goes through `safefs`; every git command through `gitops`; the stage command and
the checks run with the credential variables removed from the process (P5).
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import re
import stat

from alpaca import barrier, runbook
from alpaca.attach import AttachError, STATE_DIR, config, gitops, proven, safefs

ATTACH_VARS = runbook.ATTACH_VARS
#: the stage ids and task ids this module puts in a path
_STAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DEFAULT_TIMEOUT = 600
#: the studio folders whose files `land` treats as a research pack, beside the folders a caller names
PACK_FOLDERS = ("research",)
#: what `land` refuses to put on a work branch whatever the patch says (the folder names are those of
#: `scan.EXTRA_PROTECTED`, which the outgoing scan refuses again at handoff)
PROTECTED = (".alpaca/", ".git/", "ALPACA-MANIFEST", "RESUME.md", "CHECKLIST.md")


# --------------------------------------------------------------------------- what the checker asks

def attach_on(project_root) -> bool:
    """True when the project at `project_root` has a valid `attach:` block (build plan P1). Never raises."""
    try:
        return config.mode(str(project_root)) is not None
    except Exception:                                                         # noqa: BLE001
        return False


def _inside(path, folder) -> bool:
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def pack_outside_allowed(project_root, repo_abs, *, folder=None) -> bool:
    """May a research pack lie outside the repository `repo_abs`?

    Yes only when the project is attached, `repo_abs` is the attached repository (its real path equals the
    real path of `<project>/<attach.repo>` and its spelling holds no link: `app/../app` is the repository,
    a link named `app-link` that points at it is not), and, when `folder` is given, the pack folder is a
    folder of the studio that is neither the studio root, nor `.alpaca`, nor inside the repository, nor a
    folder that holds the repository. Never raises."""
    try:
        cfg = config.mode(str(project_root))
        if cfg is None:
            return False
        root = os.path.realpath(str(project_root))
        app = os.path.realpath(str(cfg.repo_path(project_root)))
        if os.path.realpath(str(repo_abs)) != app:
            return False
        spelled = os.path.normpath(os.path.abspath(str(repo_abs)))
        if spelled not in (app, os.path.normpath(os.path.join(os.path.abspath(str(project_root)), cfg.repo))):
            return False
        if folder is not None:
            real = os.path.realpath(str(folder))
            if real == root or not _inside(real, root) or _inside(real, app) or _inside(app, real):
                return False
            if os.path.relpath(real, root).split(os.sep)[0] in (".alpaca", ".git"):
                return False
        return True
    except Exception:                                                         # noqa: BLE001
        return False


# --------------------------------------------------------------------------- the stage runner

def _op_of(conn, task):
    row = conn.execute("SELECT op FROM tasks WHERE id=?", (task,)).fetchone()
    return row["op"] if row is not None else None


def _in(app, task, folder, *args, **kw):
    """git in the task's work folder, stdout without the final newline. `-C <folder>` follows the explicit
    `--git-dir` and `--work-tree` of `gitops.in_worktree`, whose process starts in the clone: `git apply` writes
    its files relative to the process's folder, so without it a patch would land in the clone's main checkout."""
    return gitops.in_worktree(app, task, folder, "-C", folder, *args, **kw)


def _run_in(app, task, folder, *args, env=None, identity=False, input=None):
    """`gitops.run_in_worktree` with the same `-C <folder>`; returns the whole Result."""
    return gitops.run_in_worktree(app, task, folder, "-C", folder, *args, env=env, identity=identity, input=input)


_TASK_ID = re.compile(r"t-[0-9]{1,6}")


@contextlib.contextmanager
def _task_lock(root, task):
    """One exclusive, non-blocking lock per task for `run_stage` and `land` (PM t-653.4): a stage writes into the
    work folder and `land` commits from it, so the two never run together, and neither do two lands. The lock
    file lies outside `runs/<task>/land`, which `land` removes. A second caller is refused at once rather than
    left waiting behind a stage that may run for its whole timeout."""
    if not isinstance(task, str) or not _TASK_ID.fullmatch(task):
        raise AttachError("%r cannot name a work folder (a task id looks like t-123)" % str(task)[:40])
    config.load(root)            # a project without attach: is refused before any attach state is made
    fd = safefs.open_lock(root, "%s/locks/%s.lock" % (STATE_DIR, task))
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise AttachError("another stage or land of %s is running; wait for it to end, then run this again" % task) from None
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _work_folder(root, task):
    cfg = config.load(root)
    folder = proven.task_checkout(root, task)
    if folder is None:
        raise AttachError("task %s has no proved work folder in this project; claim the task first "
                          "(alpaca attach status shows the work folders)" % task)
    return cfg, str(cfg.repo_path(root)), str(folder)


def _status_files(app, task, folder) -> list:
    out = _in(app, task, folder, "status", "--porcelain=v1", "-z", "--untracked-files=all", check=True)
    names, items = [], out.split("\0")
    index = 0
    while index < len(items):
        item = items[index]
        index += 1
        if len(item) > 3:
            names.append(item[3:])
            if item[0] in "RC":                      # a rename or copy lists the old name next
                index += 1
    return names


def _stage_of(data, stage_id):
    for stage in data.get("stages") or []:
        if isinstance(stage, dict) and stage.get("id") == stage_id:
            return stage
    raise AttachError("the runbook has no stage %r" % stage_id)


def _runbook_file(root, runbook_path) -> str:
    real = os.path.realpath(str(runbook_path))
    studio = os.path.realpath(str(root))
    if not _inside(real, studio) or not os.path.isfile(real):
        raise AttachError("%s is not a runbook file of this studio" % os.path.basename(str(runbook_path)))
    if _inside(real, os.path.join(studio, ".alpaca")):
        raise AttachError("a runbook is a studio file; one under .alpaca/ is the studio's own state (attach's "
                          "work folders, frozen research contracts)")
    try:
        app = os.path.realpath(str(config.load(root).repo_path(root)))
    except Exception:                                                         # noqa: BLE001
        app = None
    if app is None or _inside(real, app) or _inside(app, real):
        raise AttachError("a runbook is a studio file; one inside the attached repository is upstream content "
                          "and never runs as a studio runbook")
    return real


def _stage_env(stage, values) -> dict:
    env = {}
    given = stage.get("env")
    if isinstance(given, dict):
        for name, value in given.items():
            if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
                raise AttachError("stage env name %r is not a plain variable name" % str(name)[:40])
            env[name] = runbook.substitute(str(value), values)
    return env


def run_stage(root, task, runbook_path, stage_id, *, session="cli") -> dict:
    """Run one stage (see `_run_stage`) under the task's lock: a land or another stage of `task` that is
    running makes this one refuse with AttachError instead of writing into the folder beside it."""
    with _task_lock(root, task):
        return _run_stage(root, task, runbook_path, stage_id, session=session)


def _run_stage(root, task, runbook_path, stage_id, *, session="cli") -> dict:
    """Run one stage of a format 1 or 2 runbook in the task's work folder and judge its checks (AC-16).

    The runbook must pass `runbook.check`. `RUNBOOK_DIR`, `STAGE`, `ATTEMPT` (1), `EVIDENCE_DIR`
    (`.alpaca/attach/stages/<task>/<stage>/`) and `ATTACH_WORKTREE` are substituted in `run`, `workdir` and
    `env`; the resolved `workdir` must lie (real path) inside the work folder or the runbook folder. The
    command is `sh -c run`, started by `release.checks.run_command` with the credential variables out of the
    environment. Each check is judged by `runbook.evaluate` with `attach_worktree=` the work folder. Nothing
    is committed: the files the stage wrote are listed, and a stage that moved the branch fails.

    Returns the dict that is also written to `result.json` and recorded as an `attach-stage` event."""
    from alpaca import db, util
    from alpaca.gates import verdict
    from alpaca.release import checks
    from pathlib import Path
    cfg, app, worktree = _work_folder(root, task)
    if not isinstance(stage_id, str) or not _STAGE_ID.fullmatch(stage_id) or ".." in stage_id:
        raise AttachError("%r cannot name a stage folder" % str(stage_id)[:40])
    rb_file = _runbook_file(root, runbook_path)
    report = runbook.check(rb_file)
    if report["code"] != verdict.PASS:
        first = "; ".join("%s %s" % (e["code"], e["where"] or "-") for e in report["errors"][:3])
        raise AttachError("the runbook does not pass `alpaca runbook check` (%s): %s" % (verdict.name_of(report["code"]), first))
    data, _report = runbook.load(rb_file)
    fmt = runbook.runbook_format(data)
    if fmt not in runbook.FORMATS:
        raise AttachError("alpaca attach stage runs a format 1 or 2 runbook; this one is format %s (a research "
                          "runbook runs through the research engine)" % fmt)
    stage = _stage_of(data, stage_id)
    command = stage.get("run")
    if not isinstance(command, str) or not command.strip():
        raise AttachError("stage %s has no `run` (an owner gate has nothing to run)" % stage_id)
    rb_dir = os.path.dirname(rb_file)
    relative = "%s/stages/%s/%s" % (STATE_DIR, task, stage_id)
    safefs.remove_tree(root, relative)
    evidence = safefs.make_dir(root, relative)
    values = dict(runbook.knob_values(data))
    values.update(RUNBOOK_DIR=rb_dir, STAGE=stage_id, ATTEMPT=1, EVIDENCE_DIR=evidence, ATTACH_WORKTREE=worktree)
    text = runbook.substitute(command, values)
    workdir = runbook.substitute(stage["workdir"], values) if isinstance(stage.get("workdir"), str) else ""
    cwd = os.path.normpath(os.path.join(rb_dir, workdir)) if workdir else rb_dir
    real_cwd = os.path.realpath(cwd)
    if not os.path.isdir(real_cwd) or not (_inside(real_cwd, os.path.realpath(worktree)) or _inside(real_cwd, os.path.realpath(rb_dir))):
        raise AttachError("the workdir of stage %s resolves to %s, outside the work folder and the runbook folder"
                          % (stage_id, os.path.basename(cwd) or cwd))
    env = _stage_env(stage, values)
    argv = ["sh", "-c", text]
    if env:
        argv = ["env"] + ["%s=%s" % item for item in sorted(env.items())] + argv
    timeout = stage.get("timeout") if isinstance(stage.get("timeout"), (int, float)) and not isinstance(stage.get("timeout"), bool) else DEFAULT_TIMEOUT
    head_before = _in(app, task, worktree, "rev-parse", "HEAD")
    log = Path(evidence) / "stage.log"
    with gitops.process_env_scrubbed():
        code, timed_out = checks.run_command(argv, real_cwd, timeout, log)
        judged, ok = [], not timed_out
        for chk in stage.get("checks") or []:
            if not isinstance(chk, dict):
                continue
            got, why = runbook.evaluate(chk, workdir=real_cwd, runbook_dir=rb_dir, exit_code=None if timed_out else code,
                                        knobs=runbook.knob_values(data), stage=stage_id, attempt=1,
                                        evidence_dir=evidence, attach_worktree=worktree)
            judged.append({"id": chk.get("id"), "type": chk.get("type"), "verdict": verdict.name_of(got), "reason": why})
            ok = ok and got == verdict.PASS
    head_after = _in(app, task, worktree, "rev-parse", "HEAD")
    moved = head_after != head_before
    written = _status_files(app, task, worktree)
    result = {"schema": 1, "task": task, "stage": stage_id, "runbook": os.path.relpath(rb_file, os.path.realpath(str(root))),
              "verdict": "PASS" if ok and not moved else "FAIL", "exit_code": code, "timed_out": bool(timed_out),
              "checks": judged, "worktree": worktree, "evidence_dir": evidence, "log": str(log),
              "head": head_after, "head_moved": moved, "files_written": written, "committed": False,
              "at": util.now_iso()}
    if moved:
        result["reason"] = "the stage moved the work branch; a stage never commits, land the change with alpaca attach land"
    config.write_json_atomic(os.path.join(evidence, "result.json"), result, root=root)
    conn = db.connect(str(root))
    try:
        db.append_event(conn, session=session, actor="alpaca", kind="attach-stage", op=_op_of(conn, task), ref=task,
                        data={"stage": stage_id, "runbook": result["runbook"], "verdict": result["verdict"],
                              "exit_code": code, "timed_out": bool(timed_out), "head": head_after,
                              "files_written": written[:50], "checks": [(c["id"], c["verdict"]) for c in judged]})
    finally:
        conn.close()
    return result


# --------------------------------------------------------------------------- landing a patch

def _numstat_paths(out) -> list:
    """The paths a patch names, from `git apply --numstat -z` (a rename lists its two names)."""
    paths, items, index = [], out.split("\0"), 0
    while index < len(items):
        item = items[index]
        index += 1
        if not item:
            continue
        fields = item.split("\t", 2)
        if len(fields) < 3:
            raise AttachError("git apply printed a line this reader does not know")
        if fields[2] == "":                           # a rename: the next two entries are the old and new name
            paths += [items[index], items[index + 1]]
            index += 2
        else:
            paths.append(fields[2])
    return paths


def _protected_hit(rel) -> bool:
    """True when a patch path may not reach the attached repository: a path that climbs or is absolute, or one
    the outgoing scan refuses at handoff. It asks the same rule (`barrier.path_is_protected`), so case does not
    matter and `.alpaca/` is refused at any depth, not only at the root."""
    norm = "/".join(os.path.normpath(rel).split(os.sep))
    if norm.startswith("../") or norm == ".." or os.path.isabs(norm):
        return True
    return barrier.path_is_protected(norm, PROTECTED) is not None


def recorded_pack_folders(root) -> list:
    """The studio folders of the research runbooks this project has frozen: the `runbook` of every
    `research-contract` event on the record, as a folder below the studio root. Whatever the event holds that
    is not a plain path below the root is skipped."""
    from alpaca import db
    real = os.path.realpath(str(root))
    conn = db.connect_readonly(real)
    try:
        rows = conn.execute("SELECT data FROM events WHERE kind='research-contract'").fetchall()
    except Exception:                                                         # noqa: BLE001
        return []
    finally:
        conn.close()
    found = []
    for row in rows:
        try:
            runbook_path = json.loads(row["data"]).get("runbook")
        except (ValueError, AttributeError):
            continue
        if not isinstance(runbook_path, str) or os.path.isabs(runbook_path) or ".." in runbook_path.split("/"):
            continue
        folder = os.path.dirname(runbook_path)
        if folder and folder not in found:
            found.append(folder)
    return found


def _hash_files(real, files) -> set:
    """Git blob ids of the named regular files, hashed by git (`hash-object --no-filters`), the empty blob left out.
    One implementation with the handoff scan (`scan.hash_files`): a path with a newline is hashed on its own and a
    batch that git cannot finish is hashed file by file, so no file hides the others (t-686 fix round 1, B2 sibling)."""
    from alpaca.attach import scan
    found = scan.hash_files(real, list(files))
    found.discard("e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")
    return found


def _regular_files(base) -> list:
    """Every regular file under `base`, links neither followed nor listed; `__pycache__` and `.git` skipped."""
    files = []
    for folder, dirs, names in os.walk(base, followlinks=False):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        for leaf in names:
            path = os.path.join(folder, leaf)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            files.append(path)
    return files


def frozen_pack_folders(root) -> list:
    """The frozen copies of the studio packs: `.alpaca/research/<run>/contract/files/@studio` of every run.
    Each step must be a plain folder (no link) whose real path stays inside the studio; one that is not is
    skipped. A run's agents read these bytes, so they are as much a pack file as the file on disk now."""
    real = os.path.realpath(str(root))
    research = os.path.join(real, ".alpaca", "research")
    if os.path.islink(research) or not os.path.isdir(research):
        return []
    found = []
    for run in sorted(os.listdir(research)):
        base = os.path.join(research, run, "contract", "files", "@studio")
        folder = research
        for part in (run, "contract", "files", "@studio"):
            folder = os.path.join(folder, part)
            if os.path.islink(folder) or not os.path.isdir(folder):
                break
        else:
            if _inside(os.path.realpath(base), real):
                found.append(base)
    return found


def pack_blob_ids(root, folders=PACK_FOLDERS) -> set:
    """Git blob ids of every regular file under the named studio folders (the research packs) and under the
    frozen copies of the packs that research runs of this project hold (PM t-653.4: after the owner edits a pack,
    the older bytes a run read are still the pack). A folder that is missing or a link is skipped. The empty
    blob is left out."""
    real = os.path.realpath(str(root))
    files = []
    for name in folders:
        base = os.path.join(real, name)
        if os.path.islink(base) or not os.path.isdir(base):
            continue
        files += _regular_files(base)
    for base in frozen_pack_folders(real):
        files += _regular_files(base)
    return _hash_files(real, files)


#: the largest patch `land` reads (a research champion's diff is far smaller)
PATCH_LIMIT = 64 << 20


def _read_patch(patch_path) -> bytes:
    """The bytes of the patch file, read once through one descriptor that is checked to be a plain file. Every
    later step of `land` works on a copy of these bytes, so the file can change after this read and change nothing."""
    name = os.path.basename(str(patch_path))
    try:
        fd = os.open(str(patch_path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise AttachError("the patch %s is not a plain file" % name) from None
        raise AttachError("the patch %s cannot be read" % name) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AttachError("the patch %s is not a plain file" % name)
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > PATCH_LIMIT:
                raise AttachError("the patch %s is larger than %d bytes" % (name, PATCH_LIMIT))
            chunks.append(chunk)
    except OSError:
        raise AttachError("the patch %s cannot be read" % name) from None
    finally:
        os.close(fd)


def _staged_blobs(app, task, worktree, data, index_dir, before) -> tuple:
    """([(status, path, new blob id)], tree id) of what the patch `data` (bytes) would stage on top of the commit
    `before`, worked out on a scratch index so the work folder is not touched: read `before` into the scratch
    index, apply the patch to it (from standard input, so no file is read twice), and name the tree it holds. That
    tree is what `land` commits. The rows are read from the tree object itself (`diff-tree before tree`), which
    cannot change once it is written, not from the scratch index, a studio file that git reads again."""
    index = os.path.join(index_dir, "index")
    env = gitops.clean_env({"GIT_INDEX_FILE": index})
    res = _run_in(app, task, worktree, "read-tree", before, env=env)
    if res.rc != 0:
        raise AttachError("the patch does not apply to the work branch (git read-tree: %s)" % (" ".join(res.err.split())[-200:] or "exit %d" % res.rc))
    res = _run_in(app, task, worktree, "apply", "--cached", "--whitespace=nowarn", env=env, input=data)
    if res.rc != 0:
        raise AttachError("the patch does not apply to the work branch (git apply: %s)" % (" ".join(res.err.split())[-200:] or "exit %d" % res.rc))
    tree = _run_in(app, task, worktree, "write-tree", env=env)
    tree_id = tree.out.strip()
    if tree.rc != 0 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tree_id):
        raise AttachError("git write-tree failed while the patch was read")
    res = _run_in(app, task, worktree, "diff-tree", "-r", "-z", "--raw", "--no-abbrev", "--no-renames", before, tree_id)
    if res.rc != 0:
        raise AttachError("git diff-tree failed while the patch was read")
    items, rows, index_at = res.out.split("\0"), [], 0
    while index_at + 1 < len(items):
        meta, name = items[index_at], items[index_at + 1]
        index_at += 2
        fields = meta.lstrip(":").split()
        if len(fields) >= 5:
            rows.append((fields[4], name, fields[3]))
    return rows, tree_id


def land(root, task, patch_path, *, message, session="cli", pack_folders=()) -> str:
    """Apply `patch_path` to the task's work folder and commit it as the attach identity; return the new head.

    Refuses: a work folder that is not proved or has changes of its own; a patch that is not a plain file,
    does not apply, or names a path under `.alpaca/`, `.git/` or an Alpaca file name; a patch that adds or
    changes a file to content that is byte for byte a file of the studio or of a research pack (the
    folders `research/` and `pack_folders`), under any name; and a land while a stage or another land of the
    task runs. The check runs on a scratch index first, so a refusal leaves the work folder as it was.

    What is checked is what is committed: the patch bytes are read once and every git step gets that copy on
    standard input (no studio file is read again), the head is read once, the tree the scratch index holds is
    written to an object, the checks read the rows of that tree against the head (`diff-tree`), and the commit
    is that tree (`commit-tree`), with the branch moved by `update-ref` only if it still stands at that head.
    A writer that changes the scratch index while git runs changes the tree, and then the checks see it.
    Whatever else reaches the live index meanwhile is not in the commit; it stays in the work folder, where
    `status` shows it."""
    with _task_lock(root, task):
        return _land(root, task, patch_path, message=message, session=session, pack_folders=pack_folders)


def _land(root, task, patch_path, *, message, session, pack_folders) -> str:
    from alpaca import db
    cfg, app, worktree = _work_folder(root, task)
    who = proven.identity(root, cfg)                 # the studio's one commit identity (t-689), proved against the record
    if not isinstance(message, str) or not message.strip() or "\0" in message:
        raise AttachError("a landed change needs a commit message")
    data = _read_patch(patch_path)
    if _status_files(app, task, worktree):
        raise AttachError("the work folder of %s has changes of its own; commit or discard them first, so the landed "
                          "commit holds the patch and nothing a stage wrote" % task)
    scratch = "%s/runs/%s/land" % (STATE_DIR, task)
    safefs.remove_tree(root, scratch)
    index_dir = safefs.make_dir(root, scratch)
    try:
        counted = _run_in(app, task, worktree, "apply", "--numstat", "-z", input=data)
        if counted.rc != 0:
            raise AttachError("git apply cannot read the patch: %s" % (" ".join(counted.err.split())[-200:] or "exit %d" % counted.rc))
        named = _numstat_paths(counted.out)
        if not named:
            raise AttachError("the patch changes no file")
        bad = sorted({p for p in named if _protected_hit(p)})
        if bad:
            raise AttachError("the patch names %d path(s) the attached repository never receives (.alpaca/, .git/ and "
                              "Alpaca file names); first: %s" % (len(bad), bad[0][:80]))
        from alpaca.attach import scan
        folders = list(PACK_FOLDERS) + recorded_pack_folders(root) + list(pack_folders)
        before = _in(app, task, worktree, "rev-parse", "HEAD")
        rows, tree = _staged_blobs(app, task, worktree, data, index_dir, before)
        bad = sorted({name for _s, name, _b in rows if _protected_hit(name)})
        if bad:
            raise AttachError("the patch lands %d path(s) the attached repository never receives (.alpaca/, .git/ and "
                              "Alpaca file names); first: %s" % (len(bad), bad[0][:80]))
        # the studio's files are hashed only at the sizes of the blobs the patch lands (t-686 fix round 1, B2: the set
        # now covers the whole working tree of the studio)
        landed = sorted({blob for status, _n, blob in rows if status != "D"})
        banned = pack_blob_ids(root, tuple(folders))
        if landed:
            sized = _run_in(app, task, worktree, "cat-file", "--batch-check=%(objecttype) %(objectsize)",
                            input="\n".join(landed) + "\n")
            if sized.rc != 0:
                raise AttachError("git cat-file could not size the blobs the patch lands")
            banned = banned | scan.studio_blob_set(root, scan.blob_sizes(sized.out, len(landed)))
        hits = sorted({name for status, name, blob in rows if status != "D" and blob in banned})
        if hits:
            raise AttachError("the patch brings %d file(s) that are byte for byte a file of the studio or of a research "
                              "pack (%s); a pack or a studio file is never committed to the attached repository"
                              % (len(hits), hits[0][:80]))
        res = _run_in(app, task, worktree, "apply", "--index", "--whitespace=nowarn", input=data)
        if res.rc != 0:
            raise AttachError("git apply failed on the work folder: %s" % (" ".join(res.err.split())[-200:] or "exit %d" % res.rc))
        head = ""
        try:
            made = _run_in(app, task, worktree, "commit-tree", tree, "-p", before, "-m", message.strip(), identity=who)
            head = made.out.strip()
            if made.rc != 0 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
                raise AttachError("git commit-tree failed: %s" % (" ".join(made.err.split())[-200:] or "exit %d" % made.rc))
            moved = _run_in(app, task, worktree, "update-ref", "-m", "attach land", "HEAD", head, before, identity=who)
            if moved.rc != 0:
                raise AttachError("the work branch of %s moved while the patch was landing, so nothing was committed (%s)"
                                  % (task, " ".join(moved.err.split())[-160:] or "exit %d" % moved.rc))
        except BaseException:
            undone = _run_in(app, task, worktree, "apply", "-R", "--index", "--whitespace=nowarn", input=data)
            if undone.rc != 0:
                raise AttachError("the patch could not be landed and could not be taken back: the work folder of %s holds "
                                  "it uncommitted; check `git status` there" % task) from None
            raise
    finally:
        safefs.remove_tree(root, scratch)
    conn = db.connect(str(root))
    try:
        db.append_event(conn, session=session, actor="alpaca", kind="attach-land", op=_op_of(conn, task), ref=task,
                        data={"head": head, "before": before, "tree": tree, "files": [n for _s, n, _b in rows][:100]})
    finally:
        conn.close()
    return head


# --------------------------------------------------------------------------- the verbs

def _stage_parser(verbs):
    p = verbs.add_parser("stage", help="run one stage of a runbook in a task's work folder (${ATTACH_WORKTREE})")
    p.add_argument("task", help="the task whose work folder the stage runs in")
    p.add_argument("runbook", help="the runbook file (a studio file, format 1 or 2)")
    p.add_argument("stage", help="the id of the stage to run")
    p.add_argument("--json", action="store_true", help="print one JSON object")


def _stage_run(args) -> int:
    from alpaca import cli as alpaca_cli, util
    from alpaca.attach.cli import finish
    root = alpaca_cli._root()
    try:
        if config.mode(root) is None:
            return finish(args, "stage", "BLOCKED", "this project is not attached (alpaca attach init)")
        result = run_stage(root, args.task, util.from_caller(args.runbook), args.stage,
                           session=getattr(args, "session", None) or "cli")
    except AttachError as exc:
        return finish(args, "stage", "FAIL", str(exc), task=args.task)
    lines = ["alpaca attach stage: %s %s in %s exit %s" % (result["task"], result["stage"], result["worktree"], result["exit_code"])]
    lines += ["  check %s: %s (%s)" % (c["id"], c["verdict"], c["reason"]) for c in result["checks"]]
    lines += ["  wrote %s" % name for name in result["files_written"][:20]]
    lines.append("  evidence %s (log stage.log, result.json); nothing was committed" % result["evidence_dir"])
    receipt = {"stage": result["stage"], "verdict": result["verdict"], "exit_code": result["exit_code"],
               "files_written": result["files_written"], "evidence_dir": result["evidence_dir"]}
    return finish(args, "stage", result["verdict"], result.get("reason", ""), lines=lines, receipt=receipt, task=args.task)


def _land_parser(verbs):
    p = verbs.add_parser("land", help="commit a patch (a research champion's change) on a task's work branch")
    p.add_argument("task", help="the task whose work branch receives the patch")
    p.add_argument("--patch", required=True, help="the patch file (git diff output)")
    p.add_argument("--message", "-m", required=True, help="the commit message")
    p.add_argument("--pack", action="append", default=[], help="another studio folder that holds a research pack (repeatable)")
    p.add_argument("--json", action="store_true", help="print one JSON object")


def _land_run(args) -> int:
    from alpaca import cli as alpaca_cli, util
    from alpaca.attach.cli import finish
    root = alpaca_cli._root()
    try:
        if config.mode(root) is None:
            return finish(args, "land", "BLOCKED", "this project is not attached (alpaca attach init)")
        head = land(root, args.task, util.from_caller(args.patch), message=args.message,
                    session=getattr(args, "session", None) or "cli", pack_folders=tuple(args.pack))
    except AttachError as exc:
        return finish(args, "land", "FAIL", str(exc), task=args.task)
    return finish(args, "land", "PASS", lines=["alpaca attach land: %s is at %s" % (args.task, head[:12])],
                  receipt={"head": head}, task=args.task)


SUBVERBS = {"stage": (_stage_parser, _stage_run), "land": (_land_parser, _land_run)}
