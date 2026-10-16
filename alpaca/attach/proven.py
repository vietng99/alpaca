"""The one reader of what attach decides on (op-062 t-648, class review r2).

Plain files under `.alpaca/attach/` (`state.json`, `branches/<task>.json`, `baseline/<key>.json`) are
output: they are written for people and for `alpaca attach status`, and any process in the studio can
write them. A decision (where a fetch goes, whether the clone's git config is still the attached one,
which folder git acts in, which base a branch sits on) reads this module and nothing else. Each value it
returns is proved against an authority, and a missing proof is a refusal, never a skipped check:

  * `project.yaml` (`attach:`), which only the owner changes;
  * the append-only record: the `attach-init` event holds the URL and the config digest of the clone as
    attach left it, the `attach-branch` events hold each branch's base and baseline key;
  * a folder derived from the task id through `safefs.chain`, which no file can redirect;
  * git, only through `gitops.prove_worktree` and `gitops.in_worktree`;
  * the approval and handoff receipts, which are record events (`receipt_rows`), and the one plain file an agent
    approval cites, the delegation (`delegation`): the owner writes it, a line in it counts only when the record
    holds the decision it names, and the receipt keeps the hash of the bytes that were read (op-062 t-686).

`config.task_branch` and `config.task_checkout` keep their names and delegate here, so the later tasks
that read a task's work folder (refresh, the proof hook, the guard) inherit the proofs.
`alpaca/tests/test_attach_read_guard.py` fails the suite on any other read of those files.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from alpaca.attach import AttachError, STATE_DIR, config, gitops, safefs

_TASK = re.compile(r"t-[0-9]{1,6}")
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _events(root, kind, *, ref=None, limit=None) -> list:
    """[(ts, data dict)] of the events of `kind` (and `ref`), newest first. A row whose data is not a JSON
    object is skipped. The connection is read-only: the git guard asks this from a hook, which must not
    initialize or migrate the record (an absent record reads as no events, so the answer is a refusal)."""
    from alpaca import db
    sql, args = "SELECT ts, data FROM events WHERE kind=?", [kind]
    if ref is not None:
        sql, args = sql + " AND ref=?", args + [ref]
    sql += " ORDER BY id DESC" + (" LIMIT %d" % limit if limit else "")
    conn = db.connect_readonly(str(root))
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    found = []
    for row in rows:
        try:
            data = json.loads(row["data"]) if row["data"] else {}
        except ValueError:
            continue
        if isinstance(data, dict):
            found.append((row["ts"], data))
    return found


# ------------------------------------------------------------------------------------ what attach set up

def init_facts(root, cfg) -> dict:
    """`{"url", "config_digest", "base_sha"}` from the newest `attach-init` event. Never None: a studio with
    no such event, or an event without a digest, is refused, so no caller can skip a check by absence."""
    found = _events(root, "attach-init", limit=1)
    if not found:
        raise AttachError("the record has no attach-init event for this studio; it was not attached with "
                          "alpaca attach init, so attach will not fetch or cut")
    data = found[0][1]
    digest, url = data.get("config_digest"), data.get("url")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest) or not isinstance(url, str):
        raise AttachError("the attach-init event holds no config digest or url; attach will not fetch or cut")
    named = data.get("commit_identity")
    who = (named.get("name"), named.get("email")) if isinstance(named, dict) else gitops.IDENTITY
    return {"url": url, "config_digest": digest, "base_sha": data.get("base_sha"), "identity": who}


def identity(root, cfg) -> tuple:
    """The (name, email) every commit attach makes carries: `attach.commit_name` and `attach.commit_email` of
    project.yaml (the default identity when unset), only while they equal the identity the `attach-init` event
    recorded. The clone's own git config holds that identity and is watched by the config digest, so one identity
    stays fixed for the studio (build plan P13); an event from before the setting existed recorded the default. A
    value edited in project.yaml later is refused, never used."""
    recorded = init_facts(root, cfg)["identity"]
    if tuple(recorded) != tuple(cfg.identity):
        raise AttachError("attach.commit_name and attach.commit_email in project.yaml differ from the commit identity "
                          "recorded when the project was attached (%s); restore them (alpaca attach status)"
                          % gitops.identity_label(tuple(recorded)))
    return tuple(cfg.identity)


def fetch_url(root, cfg) -> str:
    """The URL a fetch may use: `attach.url` of project.yaml, only while it is a valid source and equals the
    URL the `attach-init` event recorded. state.json is not read: a value in it that nothing else carries
    would cut the work branch from another repository (P8)."""
    url = cfg.url
    try:
        valid = gitops.validate_source(url, root)
    except AttachError:
        kind, _ = gitops.split_source(url)
        if kind == "path" or url.lower().startswith("file://"):
            local = url[len("file://"):] if url.lower().startswith("file://") else os.path.expanduser(url)
            if not os.path.isdir(os.path.realpath(local)):
                valid = url             # the folder is gone: the fetch fails with git's own message and the claim fails
            else:
                raise
        else:
            raise
    if init_facts(root, cfg)["url"] != url:
        raise AttachError("attach.url in project.yaml differs from the URL recorded when the project was attached; "
                          "restore it (alpaca attach status)")
    return valid


def config_unchanged(root, cfg, app) -> None:
    """The clone's git config must be as attach left it (P8): a rewritten URL, helper or per-worktree switch
    would send a fetch, and its token, somewhere else. The digest comes from the `attach-init` event."""
    if init_facts(root, cfg)["config_digest"] != gitops.config_digest(app):
        raise AttachError("app git config changed since attach (any key but the core housekeeping, alias and "
                          "branch keys counts); restore it (alpaca doctor shows the attach-git-config row)")


# ------------------------------------------------------------------------------------ a task's branch

def task_folder(root, cfg, task):
    """The work folder of `task` derived from its id: `<real root>/.alpaca/attach/worktrees/<task>`, reached
    through `safefs.chain`, when it is a real folder (not a link, resolving to itself) and not the attached
    clone. Else None. No file names this path."""
    if not isinstance(task, str) or not _TASK.fullmatch(task):
        return None
    try:
        parent, leaf = safefs.chain(root, "%s/worktrees/%s" % (STATE_DIR, task))
    except (AttachError, OSError):
        return None
    folder = os.path.join(parent, leaf)
    try:
        mode = os.lstat(folder).st_mode
    except OSError:
        return None
    if not stat.S_ISDIR(mode) or os.path.realpath(folder) != folder:
        return None
    if folder == os.path.realpath(str(cfg.repo_path(root))):
        return None
    return folder


def branch_event(root, cfg, task):
    """`{"ts", "data"}` of the newest `attach-branch` event for `task` whose branch is `<prefix><task>` and
    whose base is a full sha, else None."""
    if not isinstance(task, str) or not _TASK.fullmatch(task):
        return None
    for ts, data in _events(root, "attach-branch", ref=task):
        if data.get("branch") == cfg.branch_prefix + task and isinstance(data.get("base_sha"), str) \
                and _SHA.fullmatch(data["base_sha"]):
            return {"ts": ts, "data": data}
    return None


def branch_tasks(root) -> list:
    """The task ids that have an `attach-branch` event, oldest first, each once (the ref of the event)."""
    from alpaca import db
    conn = db.connect_readonly(str(root))
    try:
        rows = conn.execute("SELECT ref FROM events WHERE kind='attach-branch' ORDER BY id").fetchall()
    finally:
        conn.close()
    seen = []
    for row in rows:
        task = row["ref"]
        if isinstance(task, str) and _TASK.fullmatch(task) and task not in seen:
            seen.append(task)
    return seen


def branch_record(conn, task):
    """The data of the newest `attach-branch` event of `task` on the open record `conn`: a dict, `{}` when that
    event's data is not a JSON object, None when the task has no such event. Unlike `branch_event` it is not
    filtered, so a caller that must refuse on a bad newest event (the proof hook, op-062 t-650) sees it instead
    of an older one. Raises sqlite3.Error when the record cannot be read. The caller opens `conn` (a seal reads
    it on its own connection), and nothing but the record is read."""
    row = conn.execute("SELECT data FROM events WHERE kind=? AND ref=? ORDER BY id DESC LIMIT 1",
                       ("attach-branch", task)).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row[0]) if row[0] else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def branch_view(root, cfg, task):
    """The proved facts of a task's work branch, or None: the newest `attach-branch` event and the derived
    folder both exist. No git runs here (so `attach status` and doctor stay cheap); `task_checkout` adds
    the git proof. The shape is the one `branches/<task>.json` has, with `worktree` the derived folder."""
    event = branch_event(root, cfg, task)
    folder = task_folder(root, cfg, task) if event else None
    if event is None or folder is None:
        return None
    data = event["data"]
    key = data.get("baseline_key")
    return {"schema": 1, "task": task, "branch": data["branch"], "worktree": folder, "base_sha": data["base_sha"],
            "baseline_key": key if isinstance(key, str) else None, "fetch_url": cfg.url,
            "worker": data.get("worker"), "upstream_sha": data.get("upstream_sha"), "behind": data.get("behind"),
            "resumed": bool(data.get("resumed")), "recorded_at": event["ts"]}


def task_branch(root, task):
    """`branch_view` for the attached project at `root`, or None (not attached, a bad id, no proof)."""
    cfg = config.mode(root)
    return branch_view(root, cfg, task) if cfg is not None else None


def task_of_path(root, cfg, path):
    """The task id whose work folder holds `path` (`<real root>/.alpaca/attach/worktrees/<task>` or below),
    or whose admin folder in the clone's `.git/worktrees` holds it; else None. Only the path is read."""
    real = os.path.realpath(str(path))
    for base in (os.path.join(os.path.realpath(str(root)), STATE_DIR, "worktrees"),
                 os.path.join(os.path.realpath(str(cfg.repo_path(root))), ".git", "worktrees")):
        if real.startswith(base + os.sep):
            task = real[len(base) + 1:].split(os.sep)[0]
            return task if _TASK.fullmatch(task) else None
    return None


def checkout_at(root, path):
    """`{"app", "task", "folder", "branch"}` when `path` is in the proved work folder of a task of the attached
    project at `root` (or in that task's admin folder): the `attach-branch` event names the branch, the folder is
    the one derived from the task id, and `prove_worktree` read the folder, its `.git` file, the admin folder and
    what git reports. Else None: a folder made by hand with `git worktree add`, a branch other than
    `<prefix><task>`, a detached HEAD or a rewritten `.git` file is not a work folder. The git guard asks this
    before it opens a commit in a task worktree (op-062 integration of t-648 and t-651)."""
    cfg = config.mode(root)
    if cfg is None:
        return None
    task = task_of_path(root, cfg, path)
    view = branch_view(root, cfg, task) if task else None
    if view is None:
        return None
    app = os.path.realpath(str(cfg.repo_path(root)))
    try:
        gitops.prove_worktree(app, task, view["worktree"], view["branch"])
    except AttachError:
        return None
    return {"app": app, "task": task, "folder": view["worktree"], "branch": view["branch"]}


def task_checkout(root, task):
    """The task's work folder as a Path when its branch is proved and git agrees (`prove_worktree`), else None."""
    cfg = config.mode(root)
    view = branch_view(root, cfg, task) if cfg is not None else None
    if view is None:
        return None
    try:
        gitops.prove_worktree(str(cfg.repo_path(root)), task, view["worktree"], view["branch"])
    except AttachError:
        return None
    return Path(view["worktree"])


# ------------------------------------------------------------------------------------ approvals and handoffs

def receipt_rows(root, kind) -> list:
    """The data dicts of the `release-<kind>` events (`attach-approval`, `attach-handoff`, `attach-merge`), oldest
    first, after the whole event chain verified. A record whose chain does not verify is refused, so an edited or
    deleted row cannot bring an earlier approval back (the rule of `alpaca.release.approval.current`). A row whose
    data is not a JSON object is returned as `{}`, which the caller treats as damaged."""
    from alpaca import db
    conn = db.connect_readonly(str(root))
    try:
        ok, reason = db.verify_chain(conn)
        if not ok:
            raise AttachError("the event record does not verify (%s); attach will not read approvals from it" % reason)
        rows = conn.execute("SELECT data FROM events WHERE kind=? ORDER BY id", ("release-" + kind,)).fetchall()
    finally:
        conn.close()
    found = []
    for row in rows:
        try:
            data = json.loads(row["data"]) if row["data"] else {}
        except ValueError:
            data = {}
        found.append(data if isinstance(data, dict) else {})
    return found


def decision_recorded(root, dec_id) -> bool:
    """True when the record holds a `decision` event for `dec_id`. Whether the owner proved it is a different
    question (`alpaca.ownerword.decision_is_owner`); see docs/attach.md, Known limits."""
    from alpaca import db
    if not isinstance(dec_id, str) or not re.fullmatch(r"dec-[0-9a-f]{12}", dec_id):
        return False
    conn = db.connect_readonly(str(root))
    try:
        return conn.execute("SELECT 1 FROM events WHERE kind='decision' AND ref=? LIMIT 1", (dec_id,)).fetchone() is not None
    finally:
        conn.close()


ACTIONS = ("push", "pr", "merge")
_DELEGATION_WORD = "attach-delegation:"
_DELEGATION_LINE = re.compile(r"^[ \t]*(?:[-*>][ \t]+)?attach-delegation:[ \t]+(dec-[0-9a-f]{12})[ \t]+"
                              r"([A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*)[ \t]+"
                              r"((?:push|pr|merge)(?:,(?:push|pr|merge))*)[ \t]*$")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")


def _slug(text) -> str:
    return re.sub(r"[ \t]+", "-", re.sub(r"[^a-z0-9 \t-]", "", text.lower().strip()))


def _section(text, name) -> list:
    """The lines of the one markdown section whose heading is `name` (its text, or its GitHub-style slug), up to the
    next heading of the same or a higher level. Raises AttachError for no such section or for two."""
    lines = text.splitlines()
    starts = []
    for i, line in enumerate(lines):
        m = _HEADING.match(line)
        if m and (m.group(2).strip() == name or _slug(m.group(2)) == name):
            starts.append((i, len(m.group(1))))
    if not starts:
        raise AttachError("the delegation file has no section %r" % name)
    if len(starts) > 1:
        raise AttachError("the delegation file has more than one section %r; name a unique one" % name)
    first, level = starts[0]
    end = len(lines)
    for j in range(first + 1, len(lines)):
        m = _HEADING.match(lines[j])
        if m and len(m.group(1)) <= level:
            end = j
            break
    return lines[first + 1:end]


def delegation(root, ref) -> dict:
    """The delegation an agent approval acts under: `PATH#Section` names a project file and one section of it, and the
    section must hold exactly one line `attach-delegation: <dec-id> <owner/name> <actions>` (actions a comma list
    of push, pr, merge). Returns `{"path", "section", "sha256", "dec", "repository", "actions"}`. Raises AttachError
    for a file or section that cannot be used, a line that does not parse, more than one such line, or a decision
    id the record does not hold. The file is the owner's to write; a hash of its bytes goes into the receipt.
    The file is read through `safefs.read_existing` (no link in the chain), and `alpaca.release.approval.delegation_file`
    checks that it is a file of the project."""
    from alpaca.release import approval as rapproval
    if not isinstance(ref, str) or "#" not in ref or not ref.split("#", 1)[1].strip():
        raise AttachError("an agent approval needs the delegation as PATH#Section, a project file and the section "
                          "that holds the line 'attach-delegation: <dec-id> <owner/name> <actions>'")
    path, section = ref.split("#", 1)[0], ref.split("#", 1)[1].strip()
    try:
        digest = rapproval.delegation_file(root, ref)
    except ValueError as exc:
        raise AttachError(str(exc)) from None
    raw = safefs.read_existing(root, path)
    if raw is None:
        raise AttachError("the delegation file %s does not exist" % path)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise AttachError("the delegation file %s changed while it was read; try again" % path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise AttachError("the delegation file %s is not UTF-8 text" % path) from None
    body = _section(text, section)
    named = [ln for ln in body if _DELEGATION_WORD in ln]
    if not named:
        raise AttachError("section %r of %s holds no 'attach-delegation: <dec-id> <owner/name> <actions>' line"
                          % (section, path))
    if len(named) > 1:
        raise AttachError("section %r of %s holds more than one attach-delegation line; keep one" % (section, path))
    m = _DELEGATION_LINE.match(named[0])
    if m is None:
        raise AttachError("the attach-delegation line of %s does not read 'attach-delegation: <dec-id> "
                          "<owner/name> <push,pr,merge>'" % path)
    dec, repository, actions = m.group(1), m.group(2), m.group(3).split(",")
    if not decision_recorded(root, dec):
        raise AttachError("the record holds no decision %s, so the delegation of %s names nothing" % (dec, path))
    return {"path": path, "section": section, "sha256": digest, "dec": dec, "repository": repository,
            "actions": [a for a in ACTIONS if a in actions]}
