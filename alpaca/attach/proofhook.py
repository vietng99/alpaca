"""What a proof report and a verification need to know about the attached repository (op-062 t-650, R6).

In attach mode the work is not in the studio's git repository. It is in a linked worktree of the attached
clone (`.alpaca/attach/worktrees/<task>`, cut by t-648 on `<branch_prefix><task>`). The studio's own proof
code sees none of it, so `alpaca/proof.py` and `alpaca/research/verify_gate.py` call the small functions
here, and only when the project is in attach mode and the task has a branch file:

    header_line     the `- app git:` line of a proof report: head, branch, dirty count, base, commits ahead
    seal_block      the `attach` block of a seal payload: repo, branch, full head, base, dirty count
    take_app_patch  the work branch against the base it was cut from, committed and uncommitted work both
    is_attach_path  a path under the attached clone or under the task worktrees
    dot_part_allowed  whether the dot folders below such a path (`.github`, `.husky`) may be kept as evidence

Every function returns None for a project that is not in attach mode (`config.mode`) and for a task that
has no attach record, branch file or worktree folder, so a plain project's proof header, seal payload and
kept folder are what they were.

Who the task is bound to. The branch and the base of a task come from the `attach-branch` event on the
record (written by `alpaca/attach/worktree.py`). The record is the studio's trust anchor: append-only, with
`alpaca` as its only writer (project rule 1). The branch file
`.alpaca/attach/branches/<task>.json` is writable by the agent, so it is only checked against the record:
a file that is a link, unreadable or different is a refusal, and a task that has an attach record, a branch
file or a worktree folder is never sealed as a plain task (op-062 t-650 fix round 1, class: fail open on an
input the agent can change).

What the sealed diff stands for (fix round 2, class: the seal trusts state of the attached clone). Every git
call runs with replace objects and the graft file off (`gitops.clean_env`), the clone must not be shallow, the
app diff names its own path prefixes, and `take_app_patch` also returns the tree id of the staged copy of the
index: the verifier hashes `tree/app` after the apply and refuses unless it is that tree. A work branch that
adds, moves or drops a submodule entry is refused (`ATTACH-SUBMODULE`), because the verifier lays no submodule.
An attach seal also refuses a kept folder that is reached through a link (`kept_folder_problem`).

Nothing here writes a file. `take_app_patch` hands the patch to the caller, which keeps it. Every git call
goes through `gitops` (clean environment, hooks off, no prompt), and the patch is taken only after the
worktree's config was read for keys that make `git add` or `git status` run a command (PM t-650.5).

No git runs in the work folder by its own `.git` file (op-062 integration of t-650 with t-648): the folder's
gitfile is read by `gitops.gitfile_target`, git there names the task's admin folder (`gitops.run_in_worktree`,
or `GIT_DIR` and `GIT_WORK_TREE` for the staging copy), object reads run in the clone, and the folder must pass
`gitops.prove_worktree`. The record is read through `proven.branch_record`.
"""
from __future__ import annotations

import os
import re
import sqlite3

from alpaca.attach import AttachError, STATE_DIR, config, gitops, proven

#: dot names that never become evidence copies, even below the attached clone. The generic rule of
#: `proof._not_kept` is wider (any dot name); under an attached clone only these stay refused, because a
#: repository's own `.github/`, `.husky/` or `.eslintrc` is code a reviewer needs and holds no credential.
DOT_DENY = (".git", ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".netrc", ".npmrc", ".pypirc",
            ".git-credentials")
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
#: config keys that make git run a command of the repository on `git add -A` or `git status`
_EXEC_KEYS = r"^(filter\..*\.(clean|smudge|process)|diff\..*\.textconv|core\.fsmonitor)$"
_FALSE = ("false", "0", "no", "off", "")


_TASK = re.compile(r"t-[0-9]{1,6}")
_RECORD_KIND = "attach-branch"


def _recorded(conn, task):
    """The data of the latest `attach-branch` event of `task` (a dict), or None when it has none. Raises
    sqlite3.Error when the record cannot be read. The query is `proven.branch_record`, the one reader of what
    attach decides on (op-062 integration of t-650)."""
    return proven.branch_record(conn, task)


def _record_of(root, task, conn):
    """(record or None, error text or None). Opens the record read-only when the caller passes no connection."""
    own = None
    try:
        if conn is None:
            from alpaca import db
            own = conn = db.connect_readonly(str(root))
        return _recorded(conn, task), None
    except (sqlite3.Error, OSError) as exc:
        return None, "the record could not be read (%s)" % type(exc).__name__
    finally:
        if own is not None:
            own.close()


def _bind(root, task, conn=None):
    """(config, branch dict) of an attach task, or None for a task that is not one. The branch dict holds
    `branch` and `base_sha` from the record. Raises ValueError (text starting ATTACH-) when the task is an
    attach task whose binding cannot be trusted: the project's attach: block is invalid, the record has no
    entry, or the branch file is a link, unreadable or says something else than the record."""
    if not isinstance(task, str) or not _TASK.fullmatch(task):
        return None
    cfg = config.mode(root)
    state = os.path.lexists(os.path.join(str(root), *STATE_DIR.split("/"), "state.json"))
    rec, err = _record_of(root, task, conn)
    bfile = os.path.join(str(root), *STATE_DIR.split("/"), "branches", "%s.json" % task)
    marked = rec is not None or ((cfg is not None or state) and (
        os.path.lexists(bfile) or os.path.lexists(_worktree_path(root, task))))
    if err and (cfg is not None or state):
        raise ValueError("ATTACH-RECORD %s" % err)
    if not marked:
        return None
    if cfg is None:
        raise ValueError("ATTACH-CONFIG the attach: block in project.yaml is invalid or gone, but %s has an "
                         "attach record, branch file or worktree; run alpaca doctor" % task)
    if rec is None:
        raise ValueError("ATTACH-WORKTREE %s has no attach-branch record, so its branch and base cannot be "
                         "trusted; claim the task again" % task)
    branch, base = rec.get("branch"), rec.get("base_sha")
    if not isinstance(branch, str) or not branch or not isinstance(base, str) or not _SHA.fullmatch(base):
        raise ValueError("ATTACH-WORKTREE the attach-branch record of %s holds no valid branch and base" % task)
    if branch != cfg.branch_prefix + task:
        raise ValueError("ATTACH-WORKTREE the attach-branch record of %s names %s, not its work branch %s%s"
                         % (task, branch, cfg.branch_prefix, task))
    if os.path.lexists(bfile):
        # the plain file is only compared with the record: a difference refuses, it never decides what is
        # sealed (`config.task_branch` now answers from the record itself, op-062 integration of t-650)
        got = config.read_branch_file(root, task)
        if got is None:
            raise ValueError("ATTACH-WORKTREE the branch file of %s is a link or cannot be read; it must "
                             "match the record" % task)
        if got.get("branch") != branch or got.get("base_sha") != base:
            raise ValueError("ATTACH-WORKTREE the branch file of %s does not match the record (branch or "
                             "base differ)" % task)
    return cfg, {"branch": branch, "base_sha": base}


def _worktree_path(root, task) -> str:
    """Where the task's worktree must be. The branch file also names a path, but that file is writable by
    an agent, so the path is computed and never read back from it."""
    return os.path.join(os.path.realpath(str(root)), *STATE_DIR.split("/"), "worktrees", task)


def _first(rc_out):
    return (rc_out.out or "").strip()


def _problem(root, cfg, task, br):
    """Why the worktree cannot be read, or None. 'gone' alone means the folder is missing."""
    wt = _worktree_path(root, task)
    if not os.path.lexists(wt):
        return "gone"
    bad = config.state_dir_problem(root)
    if bad:
        return bad
    for part in (os.path.dirname(wt), wt):
        if os.path.islink(part):
            return "%s is a link; the worktree of %s must be a real folder of the studio" % (
                os.path.relpath(part, os.path.realpath(str(root))), task)
    if not os.path.isdir(wt):
        return "the worktree of %s is not a folder" % task
    base = br.get("base_sha")
    if not isinstance(base, str) or not _SHA.fullmatch(base):
        return "the record of %s holds no valid base sha" % task
    app = os.path.realpath(str(cfg.repo_path(root)))
    # no git runs in the work folder by its own `.git` file (t-648: `gitops.run` refuses a work folder): the
    # file is read by gitops without following a link, and git is run with the task's admin folder named on
    # the command line (`gitops.run_in_worktree`) or in the clone itself (op-062 integration of t-650)
    named = gitops.gitfile_target(wt)
    if named is None or os.path.dirname(named) != os.path.join(app, ".git", "worktrees"):
        return "the worktree of %s is not a linked worktree of %s/" % (task, cfg.repo)
    # the worktree's own git folder must be this task's admin folder in the clone: a copied `.git` file or
    # another task's folder cannot pass for this task's (its gitdir back-pointer is read by prove_worktree)
    if named != gitops.admin_dir(app, task):
        return "the git folder of the worktree of %s does not point back at it" % task
    if gitops.run(app, "cat-file", "-e", "%s^{commit}" % base).rc != 0:
        return "the base %s of %s is not a commit of %s/" % (base[:12], task, cfg.repo)
    # attach init refuses a shallow clone; a `shallow` file written later cuts the history the commit count
    # and the diff base are read from (the same class as a replace ref or a graft, fix round 2)
    if gitops.run(app, "rev-parse", "--is-shallow-repository").out.strip() == "true":
        return "the attached repo of %s is shallow; attach needs the full history" % task
    on = gitops.run_in_worktree(app, task, wt, "symbolic-ref", "--short", "-q", "HEAD",
                                env=gitops.clean_env({"GIT_OPTIONAL_LOCKS": "0"}))
    now = _first(on) if on.rc == 0 else ""
    if now != br.get("branch"):
        return "the worktree of %s is %s, not on its branch %s" % (
            task, "on %s" % now if now else "on a detached HEAD", br.get("branch"))
    # t-648's proof of a work folder: the gitfile, the admin folder's gitdir, commondir and HEAD, what git
    # reports through the explicit runner, and the clone's worktree list
    try:
        gitops.prove_worktree(app, task, wt, br["branch"])
    except AttachError as exc:
        return str(exc)
    return None


def exec_findings(wt, app, task) -> list:
    """The settings of the worktree's git config that make `git add -A` or `git status` run a command, one
    short line each, [] when there are none. Read from the config files (includes followed) and never by
    running anything the repository names. The host's own global config is not read: it is not foreign."""
    from alpaca.gates import git_containment
    out = []
    for tok, path, lineno, _detail in git_containment.scan(wt):
        if tok != git_containment.R_HOOKSPATH:      # hooks are switched off for every call made here
            out.append("%s (%s:%d)" % (tok, os.path.basename(os.path.dirname(path)) + "/" + os.path.basename(path), lineno))
    got = gitops.run_in_worktree(app, task, wt, "config", "--get-regexp", _EXEC_KEYS)
    if got.rc == 0:
        for line in got.out.splitlines():
            key, _, value = line.partition(" ")
            if key.lower() == "core.fsmonitor" and value.strip().lower() in _FALSE:
                continue
            out.append("config %s" % key)
    return sorted(set(out))


def _refuse_exec(wt, app, task):
    found = exec_findings(wt, app, task)
    if found:
        raise ValueError("GIT-FILTER the attached repo's git config runs a command on git add or status: %s; "
                         "remove those settings from the clone" % ", ".join(found[:4]))


def header_line(root, task, conn=None):
    """The text after `- app git: ` in a proof report, or None when this is not an attach task."""
    try:
        view = _bind(root, task, conn)
    except ValueError as exc:
        return "not read (%s)" % " ".join(str(exc).split())[:120]
    if view is None:
        return None
    cfg, br = view
    try:
        why = _problem(root, cfg, task, br)
        if why == "gone":
            return "worktree gone"
        if why:
            return "not read (%s)" % why
        wt, app = _worktree_path(root, task), _app(root, cfg)
        found = exec_findings(wt, app, task)
        if found:
            return "not read (git config runs a command: %s)" % ", ".join(found[:3])
        snap = _snapshot(wt, app, task, br["base_sha"])
        return "%s on %s, %d dirty file(s), base %s, %d commit(s) ahead" % (
            snap["head"][:12], snap["branch"], snap["dirty"], br["base_sha"][:12], snap["ahead"])
    except (AttachError, ValueError, OSError) as exc:
        return "not read (%s)" % " ".join(str(exc).split())[:120]


def _snapshot(wt, app, task, base) -> dict:
    """head, branch, dirty count and commits ahead of `base`; no lock and no index write (status refreshes
    stat data otherwise). `--ignore-submodules=all`: without it git runs status inside every populated
    submodule with that submodule's own config, which nothing here has read (fix round 1, A1). Every call
    names the task's admin folder (`gitops.run_in_worktree`), so the folder's `.git` file decides nothing."""
    env = gitops.clean_env({"GIT_OPTIONAL_LOCKS": "0"})

    def run(*args):
        return gitops.run_in_worktree(app, task, wt, *args, env=env)
    head = run("rev-parse", "--verify", "HEAD")
    if head.rc != 0:
        raise AttachError("the worktree has no commit")
    branch = run("symbolic-ref", "--short", "-q", "HEAD")
    status = run("status", "--porcelain", "--ignore-submodules=all")
    if status.rc != 0:
        raise AttachError("git status failed in the worktree")
    ahead = run("rev-list", "--count", "%s..HEAD" % base)
    return {"head": _first(head), "branch": _first(branch) or "(detached)",
            "dirty": len([line for line in status.out.splitlines() if line.strip()]),
            "ahead": int(_first(ahead) or 0) if ahead.rc == 0 else 0}


def _app(root, cfg) -> str:
    """The real path of the attached clone."""
    return os.path.realpath(str(cfg.repo_path(root)))


def _refusal_of(fn):
    """`fn` with an AttachError (git would not start, git ran out of time) or an OSError turned into the
    ValueError the seal turns into a refusal, so no failure of a git call ends a seal as a crash (fix round 1)."""
    def wrapped(*args, **kw):
        try:
            return fn(*args, **kw)
        except (AttachError, OSError) as exc:
            raise ValueError("ATTACH-WORKTREE %s" % " ".join(str(exc).split())[:200]) from None
    wrapped.__name__, wrapped.__doc__ = fn.__name__, fn.__doc__
    return wrapped


@_refusal_of
def seal_block(root, task, conn=None):
    """The `attach` block of a seal payload, {"repo","branch","head","base","dirty"}, or None for a task that
    is not an attach task. A worktree that is gone gives head and dirty None and "worktree": "gone", so the
    seal says it could not read it. Raises ValueError (a refusal of the seal) when the task is an attach task
    that cannot be bound to the record, when the worktree is not the task's linked worktree on its branch, or
    when its config runs commands. In an attach project (an attach: block or state.json) it does not return
    None for a task that has a record, a branch file or a worktree folder (tests: test_attach_proof_binding.py)."""
    view = _bind(root, task, conn)
    if view is None:
        return None
    cfg, br = view
    why = _problem(root, cfg, task, br)
    if why == "gone":
        return {"repo": cfg.repo, "branch": br["branch"], "head": None, "base": br["base_sha"],
                "dirty": None, "worktree": "gone"}
    if why:
        raise ValueError("ATTACH-WORKTREE %s" % why)
    wt, app = _worktree_path(root, task), _app(root, cfg)
    _refuse_exec(wt, app, task)
    snap = _snapshot(wt, app, task, br["base_sha"])
    return {"repo": cfg.repo, "branch": snap["branch"], "head": snap["head"], "base": br["base_sha"],
            "dirty": snap["dirty"]}


#: `git add -A` and `git diff` read config and attributes from the same places `exec_findings` read: no
#: global attributes file; the environment is `gitops.clean_env` (no global or system config, fix round 1, A6c)
_PATCH_CONFIG = ("core.attributesFile=/dev/null",)
#: the app diff names its paths and colours itself, so a `diff.dstPrefix`, `diff.noprefix`, `diff.relative` or
#: colour key of the clone does not change the paths in the patch (fix round 2, B3; test: test_attach_proof_
#: content.py, FORMAT_KEYS). A key not listed that changes what the patch builds shows up as another tree id
#: when the verifier hashes `tree/app` against `tree.app.tree`.
_PATCH_ARGS = ("--src-prefix=a/", "--dst-prefix=b/", "--no-relative", "--no-color")


def _gitlinks(app, treeish, env) -> dict:
    """{path: commit id} of the submodule entries (mode 160000) of `treeish`, from git's own listing. Read in
    the clone: the base and the staged tree are objects of its shared object store."""
    got = gitops.run(app, "ls-tree", "-r", "-z", "--full-tree", treeish, env=env, binary=True)
    if got.rc != 0:
        raise ValueError("ATTACH-WORKTREE git could not list %s in the worktree" % treeish[:12])
    out = {}
    for rec in got.out.split(b"\0"):
        meta, _, name = rec.partition(b"\t")
        if meta.startswith(b"160000 "):
            out[name.decode("utf-8", "surrogateescape")] = meta.split(b" ")[2].decode("ascii")
    return out


def _refuse_gitlink_change(app, base, tree_id, env):
    """The verifier lays only files and links, so a work branch that adds, moves or drops a submodule entry
    (a committed gitlink, or an embedded repository `git add -A` would stage as one) has a change the sealed
    diff cannot show and no verification can test. Refused (fix round 2, B2)."""
    was, now = _gitlinks(app, base, env), _gitlinks(app, tree_id, env)
    changed = sorted(p for p in set(was) | set(now) if was.get(p) != now.get(p))
    if changed:
        raise ValueError("ATTACH-SUBMODULE the work changes the submodule entry %s%s; the verifier cannot check a "
                         "submodule, so this seal is refused" % (changed[0], " and %d more" % (len(changed) - 1)
                                                                  if len(changed) > 1 else ""))


@_refusal_of
def take_app_patch(root, task):
    """(head, patch bytes, base, tree id) of the task's work branch against its base: the commits made since
    the branch was cut, the staged and unstaged edits and the untracked files that are not ignored, taken the
    way `verify_gate.take_tree` takes the studio's (a copy of the index, so the worktree's own index is not
    touched). The tree id is `git write-tree` of that copy: the tree the verifier must end up with after it
    applies the patch to the base (op-062 t-650 fix round 2). None for a task that is not an attach task.
    Raises ValueError when the task cannot be bound, the worktree is gone or is not the task's, when its
    config runs commands (GIT-FILTER), when the work changes a submodule entry (ATTACH-SUBMODULE), when the
    head moved while the patch was taken, or when the patch is over the kept cap; a verification-scope seal
    must not go on without the app diff."""
    from alpaca.research import verify_gate
    view = _bind(root, task)
    if view is None:
        return None
    cfg, br = view
    why = _problem(root, cfg, task, br)
    if why == "gone":
        raise ValueError("ATTACH-WORKTREE the worktree of %s is gone, so its diff cannot be sealed for "
                         "verification" % task)
    if why:
        raise ValueError("ATTACH-WORKTREE %s" % why)
    wt, app = _worktree_path(root, task), _app(root, cfg)
    _refuse_exec(wt, app, task)
    env = gitops.clean_env({"GIT_OPTIONAL_LOCKS": "0"})
    # the staging git runs in the folder with the task's admin folder and the folder named in its environment,
    # so the folder's `.git` file decides nothing (t-648's rule for a work folder, op-062 integration of t-650)
    named = dict(env, GIT_DIR=gitops.admin_dir(app, task), GIT_WORK_TREE=wt)
    with gitops.process_env_scrubbed():
        taken = verify_gate.take_tree(wt, pathspec=(".",), base=br["base_sha"], env=named,
                                      extra_config=_PATCH_CONFIG, skip_gitlinks=True,
                                      diff_args=_PATCH_ARGS, want_tree=True)
    if taken is None:
        raise ValueError("ATTACH-WORKTREE the worktree of %s has no commit to diff" % task)
    head, patch, tree_id = taken
    again = gitops.run_in_worktree(app, task, wt, "rev-parse", "--verify", "HEAD", env=env)
    if again.rc != 0 or _first(again) != head:
        raise ValueError("ATTACH-WORKTREE the worktree of %s moved while its diff was taken; seal again" % task)
    _refuse_gitlink_change(app, br["base_sha"], tree_id, env)
    return head, patch, br["base_sha"], tree_id


def attach_project(root) -> bool:
    """Whether this project's seals keep their files the attach way (`sealkeep`). True for a project with an
    `attach:` block, valid or not, and for one with an `.alpaca/attach` folder (an attach that was started and
    left, or a block that was removed), so a task that is not bound to the record still takes the attach
    keeper. False for a plain project. Never raises: a project it cannot read counts as an attach project."""
    try:
        if config.raw(root) is not config.MISSING:
            return True
        return os.path.lexists(os.path.join(str(root), *STATE_DIR.split("/")))
    except Exception:
        return True


def keep(root, subj, evidence, taken, app_taken):
    """`sealkeep.keep`: the kept folder of an attach seal. Raises ValueError (ATTACH-KEPT) on a failure."""
    from alpaca.attach import sealkeep
    return sealkeep.keep(root, subj, evidence, taken, app_taken)


def write_seal_copy(root, report_path, text):
    """`sealkeep.write_seal_copy`: the `.seal.json` beside the report, written through the folder's descriptor."""
    from alpaca.attach import sealkeep
    return sealkeep.write_seal_copy(root, report_path, text)


def kept_folder_problem(root, kept_dir) -> str | None:
    """Why the folder where an attach seal keeps its files cannot be trusted, or None. The kept files are
    the attached repo's own diff, so an attach seal must not write them through a link: a link at
    `.alpaca/proofs`, at the op folder or at `<id>.evidence`, or a kept path whose real place is outside the
    studio, would carry product code out of it (fix round 2, B8b). lstat only, nothing is written."""
    base = os.path.realpath(str(root))
    path = os.path.abspath(os.fspath(kept_dir))
    rel = os.path.relpath(path, os.path.abspath(str(root)))
    if rel.startswith(".."):
        return "the kept folder %s is outside the studio" % path
    here = os.path.abspath(str(root))
    for part in rel.split(os.sep):
        here = os.path.join(here, part)
        if os.path.islink(here):
            return "%s is a link; the kept folder of a proof must be a real folder of the studio" % (
                os.path.relpath(here, os.path.abspath(str(root))))
    real = os.path.realpath(path)
    if real != base and not real.startswith(base + os.sep):
        return "the kept folder %s resolves outside the studio" % rel
    return None


# ----------------------------------------------------------------------------------- evidence rules

def _under(real, prefix) -> str | None:
    """The part of `real` below `prefix`, or None when it is not below it."""
    prefix = prefix.rstrip(os.sep) + os.sep
    return real[len(prefix):] if real.startswith(prefix) else None


def _remainder(root, path, cfg=None):
    """The part of `path` below the attached clone or below one task's worktree folder, or None."""
    cfg = cfg or config.mode(root)
    if cfg is None:
        return None
    real = os.path.realpath(os.fspath(path))
    base = os.path.realpath(str(root))
    rest = _under(real, os.path.join(base, cfg.repo))
    if rest is not None:
        return rest
    rest = _under(real, os.path.join(base, *STATE_DIR.split("/"), "worktrees"))
    if rest is None:
        return None
    _task, _, below = rest.partition(os.sep)
    return below if below else None


def is_attach_path(root, path) -> bool:
    """Whether `path` lies under the attached clone (`attach.repo`) or a task worktree
    (`.alpaca/attach/worktrees/<task>/`). False for a project that is not in attach mode."""
    try:
        return _remainder(root, path) is not None
    except Exception:
        return False


def dot_part_allowed(root, path) -> bool:
    """Whether every dot component below the attached clone or the task worktree is allowed as evidence:
    `.github`, `.husky`, `.eslintrc` and their kind are, the names in DOT_DENY and any `.env*` are not."""
    try:
        rest = _remainder(root, path)
    except Exception:
        return False
    if rest is None:
        return False
    for part in rest.split(os.sep):
        low = part.lower()
        if low.startswith(".") and (low in DOT_DENY or low.startswith(".env")):
            return False
    return True
