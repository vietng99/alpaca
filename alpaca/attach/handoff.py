"""`alpaca attach handoff` and `alpaca attach merge`: the only way a work branch leaves the studio (op-062 t-686,
R8; build plan section 6.8, P6, P7, P13 to P19).

    alpaca attach check t-123                      # the gates on the head and on the merge preview
    alpaca attach approve t-123 <sha> --by owner   # the exact head commit
    alpaca attach handoff t-123                    # dry run: branch, commits, files, gates, preview, behind, text
    alpaca attach handoff t-123 --execute --expect <sha>
    alpaca attach merge t-123 --execute --expect <sha>   # only under an approval that covers merge

What a handoff refuses (each is a one-line AttachError; nothing is pushed on any of them):

  * no work branch, an unsound work folder, uncommitted changes, a clone whose git config changed since attach (P8);
  * no check, a check that failed, a check of another head, or one made with another gate set;
  * the upstream base moved since the check (`git ls-remote` against the check's preview base, P17);
  * the work branch already exists upstream (P14), unless it is the head of an earlier push whose pull request failed
    (`PUSHED_NO_PR`); a head whose push left a receipt `PUSHED_UNVERIFIED` is never retried;
  * no approval of exactly this sha under the project as it is now (`alpaca.attach.approval.require`), and, when the
    check found a gate red at its base, an approval that is not the owner's;
  * the outgoing scan refuses (`alpaca.attach.scan.outgoing`: studio strings, studio files, research packs, `.alpaca/`
    paths, the commit identity, the branch name, the title and the body);
  * on `--execute`: no `--expect`, an `--expect` that is not the head, or a plan that differs from the reviewed dry run
    in any field (the body digest included, so pass the same `--body-file`).

The push goes to the URL recorded at attach, never through the remote name, whose push URL is the trap set at init (P6).
It does not run in the clone. `_push` makes a bare folder for it under `.alpaca/attach/push/` after the repository's own
`pre-push` hook has run (that hook runs in an environment that holds no token, P7: their hooks win, and never see the
credential), with no refs, no config of the clone and no hook, holding a checked copy of exactly the objects the scan read
(`scan.copy_store`; t-686 fix round 2, B3, replaced round 1's `objects/info/alternates` line, which also lent the push the
clone's commit-graph, bitmaps and multi-pack-index). The command names one refspec, `<sha>:refs/heads/<branch>`, with
`--force-with-lease=refs/heads/<branch>:` (an empty expected value: the branch must not exist upstream), `--porcelain`,
`--no-verify`, `--no-follow-tags`, `--no-signed`, `--no-thin`, `--recurse-submodules=no` and the off value of the settings
in `PUSH_OFF`, with no shallow file read. Afterwards the porcelain report must name exactly
that one new branch and the upstream's whole ref list must differ from the one read before only by it; otherwise the receipt
is `PUSHED_UNVERIFIED`, no pull request is opened and a rerun refuses (t-686 fix round 1, B1; this replaces the build plan's
`git -C app push` of section 6.8, whose `push.followTags` gap the adversarial check found). The pull request is opened through
`alpaca.attach.github` (an injected `api=` in tests); an open pull request for the same branch and head is reused.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat

from alpaca.attach import AttachError, STATE_DIR, approval, check, config, github, gitops, proven, safefs, scan
from alpaca.attach.receipts import AttachHandoffReceipt, AttachMergeReceipt

DRY_RUN = "DRY_RUN"
PUSHED = "PUSHED"
PUSHED_NO_PR = "PUSHED_NO_PR"
#: the push went out, but the push report or the upstream's ref list afterwards did not show exactly the approved
#: branch at the approved sha; no pull request is opened and a person looks at the upstream (t-686 fix round 1, B1)
PUSHED_UNVERIFIED = "PUSHED_UNVERIFIED"
MERGED = "MERGED"
ZERO = "0" * 40
HOOK_TIMEOUT = 600
PUSH_DIR = scan.COPY_DIR
#: one-process values for the push (`-c key=value`), each the off value of a key that adds a ref, an object or a
#: program to a push. They outrank the repository's config, which the push no longer reads at all (`_push`); both
#: layers stay so that neither one is the only thing between a planted key and the upstream.
PUSH_OFF = ("push.followTags=false", "push.recurseSubmodules=no", "submodule.recurse=false", "push.gpgSign=false",
            "push.pushOption=", "push.default=nothing", "push.autoSetupRemote=false", "push.negotiate=false",
            "remote.pushDefault=", "transfer.fsckObjects=false") + scan.STORE_OFF
MAX_BODY = 64 << 10
MAX_TITLE = 200
MAX_COMMITS = 200
MAX_FILES = 1000
_SHA = re.compile(r"[0-9a-f]{40}")


# ------------------------------------------------------------------------------------------ the receipts

def _handoffs(root) -> list:
    """Every recorded handoff receipt as `(path, receipt)`, oldest first. A damaged one refuses."""
    from alpaca.release.receipts import load_receipt
    found = []
    for data in proven.receipt_rows(root, "attach-handoff"):
        try:
            found.append((data["receipt"], load_receipt(root, data.get("receipt", ""), AttachHandoffReceipt)))
        except (KeyError, ValueError, OSError, TypeError):
            raise AttachError("an attach handoff record is missing or damaged; restore it before another handoff") from None
    return found


def handoffs(root, task=None, statuses=None) -> list:
    """The handoff receipts of `task` (or of every task), oldest first, optionally of the given statuses."""
    return [(p, r) for p, r in _handoffs(root)
            if (task is None or r.task == task) and (statuses is None or r.status in statuses)]


def _merges(root, task=None) -> list:
    from alpaca.release.receipts import load_receipt
    found = []
    for data in proven.receipt_rows(root, "attach-merge"):
        try:
            rec = load_receipt(root, data.get("receipt", ""), AttachMergeReceipt)
        except (KeyError, ValueError, OSError, TypeError):
            raise AttachError("an attach merge record is missing or damaged") from None
        if task is None or rec.task == task:
            found.append(rec)
    return found


def merges(root, task=None) -> list:
    return _merges(root, task)


def _record(root, session, receipt):
    from alpaca.release.receipts import record_receipt
    try:
        return record_receipt(root, session, receipt)
    except (ValueError, OSError) as exc:
        raise AttachError("the %s receipt could not be recorded: %s" % (receipt.kind, gitops.scrub(exc))) from None


def _canon(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# ------------------------------------------------------------------------------------------ git facts

def _remote_sha(app, url, ref):
    """The sha the upstream itself gives for `ref`, or None when it has none. Run with no repository
    (`GIT_DIR=/dev/null`), so nothing in the clone's config can change where it asks."""
    out = gitops.remote(app, "ls-remote", "--", url, ref, url=url, no_repo=True, timeout=120)
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        if name.strip() == ref and _SHA.fullmatch(sha.strip()):
            return sha.strip()
    return None


def _upstream_refs(app, url) -> dict:
    """Every ref the upstream lists (`git ls-remote -- <url>` with no repository), as `{name: sha}`: branches, tags
    (peeled lines too), `HEAD` and whatever else it advertises. A line that does not parse refuses, so a list that
    was cut short or is not what git prints is never compared as if it were whole."""
    out = gitops.remote(app, "ls-remote", "--", url, url=url, no_repo=True, timeout=120)
    refs = {}
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        if not _SHA.fullmatch(sha.strip()) or not name.strip():
            raise AttachError("the upstream's ref list could not be read (a line is not '<sha> <ref>')")
        refs[name.strip()] = sha.strip()
    return refs


def _ref_changes(before, after, ref, head) -> list:
    """The names whose value after the push is not what a push of `head` to `ref` alone gives: `before` with `ref`
    set to `head`. Sorted."""
    expected = dict(before)
    expected[ref] = head
    return sorted(n for n in set(expected) | set(after) if expected.get(n) != after.get(n))


def _porcelain_problems(out, head, ref) -> list:
    """`git push --porcelain` names every ref it sent, one tab-separated line each (`<flag> TAB <from>:<to> TAB
    <summary>`). The approved push is exactly one line: a new branch, `<head>:<ref>`."""
    rows = [line.split("\t") for line in out.splitlines() if "\t" in line]
    if len(rows) != 1:
        return ["the push report names %d refs, not 1" % len(rows)]
    flag, spec = rows[0][0], rows[0][1] if len(rows[0]) > 1 else ""
    if flag != "*" or spec != "%s:%s" % (head, ref):
        return ["the push report is not a new branch at the approved sha"]
    return []


def _clone_objects(app) -> str:
    """The clone's object folder, `<clone>/.git/objects`, refused when git names another folder or it is a link."""
    objects = os.path.join(os.path.realpath(str(app)), ".git", "objects")
    asked = gitops.git(app, "rev-parse", "--path-format=absolute", "--git-path", "objects")
    if os.path.islink(objects) or not os.path.isdir(objects) or os.path.realpath(asked) != objects or "\n" in objects:
        raise AttachError("the clone's object folder is not <clone>/.git/objects; nothing was pushed")
    return objects


def _push(root, app, task, url, head, ref, base, scanned) -> tuple:
    """Push `head` to `ref` of `url` and return `(output, problems)`: git's porcelain report and what is wrong with
    it or with the upstream's ref list before and after. The command carries exactly one refspec and the off value of
    every setting that could add more (`--no-follow-tags`, `--recurse-submodules=no`, `--no-signed`, `PUSH_OFF`).

    It runs from a fresh bare folder that holds a checked copy of the objects (`scan.copy_store`), made after the
    repository's own pre-push hook has run: exactly the objects of `head --not base`, each read from the clone and checked
    against its name, plus the commits and trees on the base side a walk reads, with no `objects/info/alternates`, refs,
    hooks, commit-graph, bitmap or multi-pack-index. The copy's walk must list exactly `scanned`, the objects the scan
    read, or nothing is pushed. git runs with `scan.STORE_OFF` and no shallow file, `--no-thin` (a thin pack would read
    objects of the base the copy does not hold), and the folder must be unchanged from its proof to the end of the push
    (t-686 fix round 2, B3: round 1's folder borrowed the clone's object folder, and with it the clone's commit-graph;
    `investigate-r2.md`)."""
    before = _upstream_refs(app, url)
    _clone_objects(app)
    rel, folder, walk, state = scan.copy_store(root, app, head, base, task)
    try:
        now = frozenset(sha for sha, _p in walk)
        if now != frozenset(scanned):
            raise AttachError("the objects the push would send are not the objects the scan read (%d now, %d scanned, %d "
                              "differ); nothing was pushed" % (len(now), len(scanned), len(now ^ frozenset(scanned))))
        if scan.folder_state(folder) != state:
            raise AttachError("the push folder changed after its objects were proved; nothing was pushed")
        out = gitops.remote(folder, "push", "--porcelain", "--no-verify", "--no-follow-tags", "--no-signed", "--no-thin",
                            "--recurse-submodules=no", "--force-with-lease=%s:" % ref, "--", url, "%s:%s" % (head, ref),
                            url=url, timeout=600, overrides=PUSH_OFF, env_extra={"GIT_SHALLOW_FILE": scan.NO_SHALLOW})
        moved = scan.folder_state(folder) != state
    finally:
        safefs.remove_tree(root, rel)
    problems = _porcelain_problems(out, head, ref)
    if moved:
        problems.append("the push folder changed while git read it, so what was sent is not proved")
    try:
        changed = _ref_changes(before, _upstream_refs(app, url), ref, head)
    except AttachError as exc:
        return out, problems + ["the upstream's refs could not be read after the push (%s)" % gitops.scrub(exc)]
    if changed:
        shown = ", ".join(n[:60] for n in changed[:5])
        problems.append("the upstream's refs differ from the approved push in %d name(s): %s" % (len(changed), shown))
    return out, problems


def _clean(app, task, folder) -> bool:
    return gitops.in_worktree(app, task, folder, "status", "--porcelain", "--untracked-files=normal") == ""


def _text_line(value, what, limit) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AttachError("the pull request %s is empty" % what)
    value = value.strip()
    if len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise AttachError("the pull request %s must be one line of at most %d characters" % (what, limit))
    return value


def _read_body(path) -> str:
    """The text of the body file the owner named on the command line, read once from a plain file."""
    name = os.path.basename(str(path))
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        raise AttachError("the body file %s cannot be read (a plain file is needed)" % name) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AttachError("the body file %s is not a plain file" % name)
        data = os.read(fd, MAX_BODY + 1)
    except OSError:
        raise AttachError("the body file %s cannot be read" % name) from None
    finally:
        os.close(fd)
    if len(data) > MAX_BODY:
        raise AttachError("the body file %s is larger than %d bytes" % (name, MAX_BODY))
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise AttachError("the body file %s is not UTF-8 text" % name) from None


def _gates(ck) -> list:
    preview = {g["name"]: g for g in ck.preview_results}
    red = set(ck.red_at_base) | set(ck.red_at_preview_base)
    return [{"name": g["name"], "head": bool(g["passed"]),
             "preview": bool(preview[g["name"]]["passed"]) if g["name"] in preview else None,
             "red_at_base": g["name"] in red} for g in ck.head_results]


def _gate_lines(gates) -> list:
    return ["  - %s: %s / %s%s" % (g["name"], "pass" if g["head"] else "FAIL",
                                  "not run" if g["preview"] is None else ("pass" if g["preview"] else "FAIL"),
                                  " (red at base)" if g["red_at_base"] else "") for g in gates]


def _plain_body(user, gates) -> str:
    """The body when the owner turned the generated section off (`attach.pr_footer: false`, op-062 t-690): the text of
    the approved body file, then a short summary of the gates by name and exit status. It names no tool, no path, no
    task, no sha and no identity; a studio that wants nothing else sends the body file alone and gets exactly that."""
    lines = []
    if user and user.strip():
        lines += [user.strip(), ""]
    lines += ["Checks run on this branch and on a trial merge with the base branch (exit status only):"]
    lines += _gate_lines(gates)
    return "\n".join(lines) + "\n"


def _body(user, task, ck, gates, behind, footer=True, identity=None) -> str:
    if not footer:
        return _plain_body(user, gates)
    lines = [user.strip() if user and user.strip() else "Work branch %s of task %s." % (ck.branch, task), "",
             "Alpaca attach handoff", "",
             "- task: %s" % task, "- head: %s" % ck.head,
             "- base: %s (merge preview made on %s), the branch is %d commit(s) behind it" % (
                 "the upstream base", ck.preview_base[:12], behind),
             "- gates (exit status only; head / merge preview):"]
    lines += _gate_lines(gates)
    if ck.needs_owner_review:
        lines.append("- red at base: %s; the owner reviewed this handoff"
                     % ", ".join(sorted(set(ck.red_at_base) | set(ck.red_at_preview_base))))
    who = tuple(identity) if identity else gitops.IDENTITY
    if who == gitops.IDENTITY:
        lines.append("- commits are authored by the Alpaca agent identity (%s <%s>)" % gitops.IDENTITY)
    else:
        lines.append("- commits are authored by %s" % gitops.identity_label(who))
    return "\n".join(lines) + "\n"


def _name_status(app, merge_base, head) -> list:
    raw = gitops.git(app, "diff", "--name-status", "-z", "--no-renames", merge_base, head, binary=True)
    parts = raw.split(b"\0")
    found = []
    for i in range(0, len(parts) - 1, 2):
        found.append("%s %s" % (parts[i].decode("ascii", "replace"), parts[i + 1].decode("utf-8", "replace")))
    return found


# ------------------------------------------------------------------------------------------ the plan

def _fresh_check(root, cfg, app, url, state, task):
    """The latest check, when it still judges what would go out: it passed, it is of this head and branch, it used
    the gate set of now, and the upstream base is where the check saw it (P17)."""
    ck = check.latest(root, task)
    if ck is None:
        raise AttachError("%s has not been checked; run: alpaca attach check %s" % (task, task))
    if ck.verdict != "PASS":
        raise AttachError("the latest check of %s did not pass (%s); fix it and run the check again"
                          % (task, ck.reason or "gate regression or conflict"))
    if ck.head != state["head"] or ck.branch != state["branch"]:
        raise AttachError("the latest check of %s judged %s, not the head %s now; run the check again"
                          % (task, ck.head[:12], state["head"][:12]))
    if ck.gate_set != cfg.gate_set_digest():
        raise AttachError("the gates changed since the check of %s; run the check again" % task)
    now = _remote_sha(app, url, "refs/heads/%s" % cfg.base)
    if now is None:
        raise AttachError("the upstream has no branch %s" % cfg.base)
    if now != ck.preview_base:
        raise AttachError("the upstream base %s moved since the check (%s, now %s); run: alpaca attach check %s"
                          % (cfg.base, ck.preview_base[:12], now[:12], task))
    return ck


def _build(root, task, *, title, body_text, resume_ok=False) -> dict:
    """Everything a handoff or a merge reads, proved, or an AttachError. Pushes nothing."""
    cfg = config.load(root)
    if not cfg.repository:
        raise AttachError("attach.repository (owner/name) is not set in project.yaml; a handoff opens a pull request in it")
    state = approval.work_state(root, cfg, task)
    app, folder, branch, head = state["app"], state["folder"], state["branch"], state["head"]
    if not _clean(app, task, folder):
        raise AttachError("the work folder of %s has uncommitted changes; commit or discard them first" % task)
    proven.config_unchanged(root, cfg, app)
    url = proven.fetch_url(root, cfg)
    ck = _fresh_check(root, cfg, app, url, state, task)
    there = _remote_sha(app, url, "refs/heads/%s" % branch)
    if there is not None:
        if any(r.outgoing.get("head") == head for _p, r in handoffs(root, task, (PUSHED_UNVERIFIED,))):
            raise AttachError("an earlier push of %s (%s) was not shown to be only the approved branch (receipt status %s); "
                              "look at the upstream's refs yourself. No pull request is opened for it from here"
                              % (branch, head[:12], PUSHED_UNVERIFIED))
        resumed = [r for _p, r in handoffs(root, task, (PUSHED_NO_PR,)) if r.outgoing.get("head") == head]
        if not (resume_ok and there == head and resumed):
            raise AttachError("the branch %s already exists upstream (%s); a second handoff is a new decision, so "
                              "nothing is pushed over it" % (branch, there[:12]))
    rec = approval.require(root, task, head, action=("push", "pr"), needs_owner=ck.needs_owner_review)
    base = ck.preview_base
    shas = gitops.git(app, "rev-list", "--reverse", "%s..%s" % (base, head)).split()
    if not shas:
        raise AttachError("%s has no commit of its own beyond the upstream base; there is nothing to hand off" % task)
    subjects = gitops.git(app, "log", "--reverse", "--format=%H %s", "%s..%s" % (base, head)).splitlines()
    merge_base = gitops.git(app, "merge-base", base, head)
    files = _name_status(app, merge_base, head)
    behind = int(gitops.git(app, "rev-list", "--count", "%s..%s" % (head, base)) or "0")
    gates = _gates(ck)
    newest = subjects[-1].split(" ", 1)
    the_title = _text_line(title or (newest[1] if len(newest) > 1 else task), "title", MAX_TITLE)
    who = proven.identity(root, cfg)                 # the studio's one commit identity, proved against the record
    body = _body(body_text, task, ck, gates, behind, footer=cfg.pr_footer, identity=who)
    result = scan.outgoing(root, folder, head, base, branch=branch, title=the_title, body=body)
    if result.refused:
        raise AttachError("the outgoing scan refuses this branch: %s" % (result.reason or "see the scan report"))
    outgoing = {"repository": cfg.repository, "url": url, "upstream": cfg.upstream, "branch": branch, "head": head,
                "base": cfg.base, "base_sha": ck.base_sha, "preview_base": ck.preview_base, "behind": behind,
                "commits": [s[:12] + s[40:] for s in subjects[:MAX_COMMITS]], "commits_total": len(shas),
                "files": files[:MAX_FILES], "files_total": len(files), "gates": gates, "gate_set": ck.gate_set,
                "conflicts": [], "check_id": ck.check_id, "needs_owner_review": bool(ck.needs_owner_review),
                "red_at_base": sorted(set(ck.red_at_base) | set(ck.red_at_preview_base)), "title": the_title,
                "pr_footer": bool(cfg.pr_footer), "commit_identity": gitops.identity_label(who),
                "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "approval": {"attempt": rec.attempt, "approver": rec.approver}, "scan": {
                    "commits": result.commits_scanned, "objects": result.objects_scanned,
                    "objects_sha256": hashlib.sha256("\n".join(sorted(result.object_ids)).encode("ascii")).hexdigest()}}
    return {"cfg": cfg, "state": state, "url": url, "check": ck, "outgoing": outgoing, "body": body, "title": the_title,
            "remote_branch": there, "approval": rec, "scanned": result.object_ids}


def _hook_path(app, folder) -> str:
    """Where git would look for the repository's `pre-push` hook: `core.hooksPath` of the clone's own config
    (relative to the work folder, as git runs hooks), else `<clone>/.git/hooks`. The path may not exist."""
    res = gitops.run(app, "config", "--type=path", "--file", os.path.join(app, ".git", "config"), "--get", "core.hooksPath")
    value = res.out.strip() if res.rc == 0 else ""
    if value:
        base = value if os.path.isabs(value) else os.path.join(folder, value)
    else:
        base = os.path.join(app, ".git", "hooks")
    return os.path.join(base, "pre-push")


def hook_found(app, folder):
    """The path of the repository's own pre-push hook when git would run it (a regular file that is executable), else None."""
    path = _hook_path(app, folder)
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return None
    return path if stat.S_ISREG(mode) and os.access(path, os.X_OK) else None


def _pre_push(cfg, app, folder, url, branch, head) -> str:
    """Run the repository's own pre-push hook the way git would, with an environment that holds no token (P7).
    Returns a one-line note; raises AttachError when the hook refuses."""
    from alpaca.release.environment import command_environment
    path = hook_found(app, folder)
    if path is None:
        return "no pre-push hook in the repository"
    with gitops.process_env_scrubbed():
        env = command_environment()
    line = "refs/heads/%s %s refs/heads/%s %s\n" % (branch, head, branch, ZERO)
    res = gitops.run_hook(path, [cfg.upstream, url], cwd=folder, env=env, stdin_text=line, timeout=HOOK_TIMEOUT)
    if res.rc != 0:
        tail = " ".join(gitops.scrub(res.out).split())[-300:]
        raise AttachError("the repository's own pre-push hook refused the push (exit %d)%s; nothing was pushed"
                          % (res.rc, ": " + tail if tail else ""))
    return "the repository's pre-push hook passed"


def plan(root, task, *, title=None, body_file=None, body=None, api=None, session="cli") -> dict:
    """The dry run: proves everything a push needs, records a `DRY_RUN` receipt holding the whole plan and returns
    `{"outgoing", "body", "receipt", "hook"}`. Pushes nothing and contacts the upstream only to read two refs."""
    approval.need_task(task)
    text = _read_body(body_file) if body_file else (body or "")
    built = _build(root, task, title=title, body_text=text)
    out = built["outgoing"]
    hook = hook_found(built["state"]["app"], built["state"]["folder"])
    path = _record(root, session, AttachHandoffReceipt(task, out, DRY_RUN))
    return {"task": task, "outgoing": out, "body": built["body"], "receipt": str(path),
            "hook": hook if hook is None else os.path.basename(os.path.dirname(hook)) + "/pre-push"}


def _task_lock(root, task):
    fd = safefs.open_lock(root, "%s/locks/handoff-%s.lock" % (STATE_DIR, task))
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _unlock(fd) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _same(reviewed, current) -> list:
    """The keys in which two plans differ."""
    return sorted(k for k in set(reviewed) | set(current) if _canon(reviewed.get(k)) != _canon(current.get(k)))


def execute(root, task, *, expect, title=None, body_file=None, body=None, api=None, session="cli") -> AttachHandoffReceipt:
    """Push the approved work branch and open its pull request. `expect` must be the full head sha, and the plan
    recomputed now must equal the reviewed dry run in every field."""
    approval.need_task(task)
    if not expect:
        raise AttachError("--expect <sha> is required to push: it names the head commit you reviewed")
    if not isinstance(expect, str) or not _SHA.fullmatch(expect):
        raise AttachError("--expect needs the full 40-character head sha")
    cfg = config.load(root)
    if not cfg.repository:
        raise AttachError("attach.repository (owner/name) is not set in project.yaml; a handoff opens a pull request in it")
    gh = github.GitHub(cfg.repository, api)
    gh.ready()
    text = _read_body(body_file) if body_file else (body or "")
    fd = _task_lock(root, task)
    try:
        built = _build(root, task, title=title, body_text=text, resume_ok=True)
        out, state, url = built["outgoing"], built["state"], built["url"]
        app, folder, branch, head = state["app"], state["folder"], state["branch"], state["head"]
        if expect != head:
            raise AttachError("--expect %s is not the head of %s (%s); review the dry run again" % (expect[:12], task, head[:12]))
        reviewed = [r for _p, r in handoffs(root, task, (DRY_RUN,))]
        if not reviewed:
            raise AttachError("no dry run of %s was recorded; run: alpaca attach handoff %s" % (task, task))
        if reviewed[-1].outgoing.get("head") != expect:
            raise AttachError("the latest dry run of %s is of %s, not of %s; run the dry run again"
                              % (task, str(reviewed[-1].outgoing.get("head"))[:12], expect[:12]))
        changed = _same(reviewed[-1].outgoing, out)
        if changed:
            raise AttachError("the plan changed since the dry run (%s); review the new dry run" % ", ".join(changed))
        note = _pre_push(cfg, app, folder, url, branch, head)
        ck = built["check"]
        # the last look before the push: the approval, the clone's config and the upstream base
        approval.require(root, task, head, action=("push", "pr"), needs_owner=ck.needs_owner_review)
        proven.config_unchanged(root, cfg, app)
        if _remote_sha(app, url, "refs/heads/%s" % cfg.base) != ck.preview_base:
            raise AttachError("the upstream base %s moved while the handoff was prepared; run: alpaca attach check %s"
                              % (cfg.base, task))
        ref = "refs/heads/%s" % branch
        there = _remote_sha(app, url, ref)
        if there is None:
            _out, problems = _push(root, app, task, url, head, ref, ck.preview_base, built["scanned"])
            if problems:
                text = "; ".join(problems)
                _record(root, session, AttachHandoffReceipt(task, out, PUSHED_UNVERIFIED, error=gitops.scrub(text)[:300]))
                raise AttachError("the branch %s was pushed, but %s; no pull request was opened. Look at the upstream's "
                                  "refs and the receipt (status %s) before anything else" % (branch, gitops.scrub(text), PUSHED_UNVERIFIED))
        elif there != head:
            raise AttachError("the branch %s appeared upstream while the handoff was prepared (%s); nothing was pushed"
                              % (branch, there[:12]))
        if _remote_sha(app, url, ref) != head:
            raise AttachError("the upstream does not show %s at %s after the push" % (branch, head[:12]))
        try:
            approval.require(root, task, head, action=("push", "pr"), needs_owner=ck.needs_owner_review)
            pr = gh.find_pr(branch, cfg.base)
            if pr is not None and pr["state"] == "open":
                if pr["head_sha"] != head or pr["base_ref"] != cfg.base:
                    raise AttachError("an open pull request #%d from %s has another head or base" % (pr["number"], branch))
            else:
                pr = gh.open_pr(branch, cfg.base, built["title"], built["body"])
        except AttachError as exc:
            receipt = AttachHandoffReceipt(task, out, PUSHED_NO_PR, error=gitops.scrub(exc)[:300])
            _record(root, session, receipt)
            raise AttachError("the branch %s was pushed but the pull request step failed: %s; run the same command "
                              "again to open it" % (branch, gitops.scrub(exc))) from None
        receipt = AttachHandoffReceipt(task, out, PUSHED, pr_url=pr["html_url"], pr_number=pr["number"], note=note)
        _record(root, session, receipt)
        return receipt
    finally:
        _unlock(fd)


# ------------------------------------------------------------------------------------------ the merge

def merge(root, task, *, expect=None, method="merge", execute=False, api=None, session="cli"):
    """The plan of a merge (a dict), or with `execute` the merge itself (an AttachMergeReceipt).

    A merge needs an approval of exactly this head that covers `merge` (an agent approval under a delegation that
    names merge, or an owner approval typed with --actions push,pr,merge), the pull request this studio opened for
    this head, the upstream base where the latest check saw it, and it pins the head sha in the call, so GitHub
    refuses when the branch moved."""
    approval.need_task(task)
    if method not in github.METHODS:
        raise AttachError("the merge method is one of %s" % ", ".join(github.METHODS))
    if execute:
        if not expect:
            raise AttachError("--expect <sha> is required to merge: it names the head commit you reviewed")
        if not isinstance(expect, str) or not _SHA.fullmatch(expect):
            raise AttachError("--expect needs the full 40-character head sha")
    cfg = config.load(root)
    if not cfg.repository:
        raise AttachError("attach.repository (owner/name) is not set in project.yaml")
    gh = github.GitHub(cfg.repository, api)
    if execute:
        gh.ready()
    fd = _task_lock(root, task)
    try:
        state = approval.work_state(root, cfg, task)
        app, head, branch = state["app"], state["head"], state["branch"]
        if expect and expect != head:
            raise AttachError("--expect %s is not the head of %s (%s)" % (expect[:12], task, head[:12]))
        proven.config_unchanged(root, cfg, app)
        url = proven.fetch_url(root, cfg)
        pushed = [r for _p, r in handoffs(root, task, (PUSHED,)) if r.outgoing.get("head") == head and r.pr_number]
        if not pushed:
            raise AttachError("%s has no pull request for %s; hand it off first (alpaca attach handoff)" % (task, head[:12]))
        if any(m.head == head and m.status == MERGED for m in _merges(root, task)):
            raise AttachError("the pull request of %s was already merged by this studio" % task)
        ck = _fresh_check(root, cfg, app, url, state, task)
        rec = approval.require(root, task, head, action="merge", needs_owner=ck.needs_owner_review)
        number = pushed[-1].pr_number
        pr = gh.get_pr(number)
        if pr["state"] != "open" or pr["merged"]:
            raise AttachError("pull request #%d is %s, not open" % (number, "merged" if pr["merged"] else pr["state"]))
        if pr["head_sha"] != head:
            raise AttachError("pull request #%d is at %s, not at the approved head %s" % (number, pr["head_sha"][:12], head[:12]))
        if pr["base_ref"] != cfg.base or pr["head_ref"] != branch:
            raise AttachError("pull request #%d is not %s into %s" % (number, branch, cfg.base))
        info = {"task": task, "head": head, "pr_number": number, "pr_url": pr["html_url"], "method": method,
                "base": cfg.base, "preview_base": ck.preview_base, "approval": {"attempt": rec.attempt, "approver": rec.approver}}
        if not execute:
            return info
        done = gh.merge_pr(number, head, method)
        receipt = AttachMergeReceipt(task, head, number, done["sha"], method, MERGED)
        _record(root, session, receipt)
        return receipt
    finally:
        _unlock(fd)


# ------------------------------------------------------------------------------------------ the verbs

def add_handoff_parser(verbs):
    p = verbs.add_parser("handoff", help="dry run of a handoff (branch, commits, files, gates, preview) or, with "
                                         "--execute --expect SHA, push the approved branch and open its pull request")
    p.add_argument("task", help="the task whose work branch is handed off (t-123)")
    p.add_argument("--execute", action="store_true", help="push and open the pull request (needs --expect)")
    p.add_argument("--expect", default=None, help="the full head sha you reviewed in the dry run")
    p.add_argument("--title", default=None, help="the pull request title (default: the subject of the newest commit)")
    p.add_argument("--body-file", default=None, help="a text file for the top of the pull request body "
                                                     "(give the same file at --execute)")
    p.add_argument("--json", action="store_true", help="print one JSON object")


def add_merge_parser(verbs):
    m = verbs.add_parser("merge", help="merge the pull request of an approved handoff (needs an approval that covers merge)")
    m.add_argument("task", help="the task whose pull request is merged (t-123)")
    m.add_argument("--execute", action="store_true", help="merge (needs --expect)")
    m.add_argument("--expect", default=None, help="the full head sha the pull request must be at")
    m.add_argument("--method", default="merge", choices=github.METHODS, help="merge, squash or rebase (default merge)")
    m.add_argument("--json", action="store_true", help="print one JSON object")


def _plan_lines(info) -> list:
    out = info["outgoing"]
    lines = ["alpaca attach handoff (dry run): %s" % out["branch"],
             "  into %s at %s, head %s (%d commit(s) behind the base)" % (out["repository"], out["base"], out["head"][:12], out["behind"]),
             "  push to: %s" % gitops.scrub(out["url"]),
             "  commits (%d):" % out["commits_total"]]
    lines += ["    %s" % c for c in out["commits"][:20]]
    if out["commits_total"] > 20:
        lines.append("    ... and %d more" % (out["commits_total"] - 20))
    lines.append("  files (%d):" % out["files_total"])
    lines += ["    %s" % f for f in out["files"][:30]]
    if out["files_total"] > 30:
        lines.append("    ... and %d more" % (out["files_total"] - 30))
    lines.append("  gates (head / merge preview):")
    for g in out["gates"]:
        lines.append("    %s: %s / %s%s" % (g["name"], "pass" if g["head"] else "FAIL",
                                            "not run" if g["preview"] is None else ("pass" if g["preview"] else "FAIL"),
                                            " (red at base)" if g["red_at_base"] else ""))
    lines.append("  merge preview: no conflicts")
    lines.append("  approval: %s" % out["approval"]["approver"])
    lines.append("  pull request body: %s" % ("the text you gave and the generated section" if out["pr_footer"]
                                              else "the text you gave and a gate summary only (the generated section is off)"))
    lines.append("  commit author: %s" % out["commit_identity"])
    lines.append("  pre-push hook: %s" % (info["hook"] or "none in the repository"))
    lines.append("  title: %s" % out["title"])
    lines += ["  | " + ln for ln in info["body"].splitlines()]
    lines.append("  receipt: %s" % info["receipt"])
    lines.append("  to push: alpaca attach handoff %s --execute --expect %s" % (info["task"], out["head"]))
    return lines


def run_handoff(args) -> int:
    from alpaca import cli as alpaca_cli
    from alpaca.attach.cli import finish
    root = alpaca_cli._root()
    task = getattr(args, "task", None)
    session = getattr(args, "session", None) or "cli"
    try:
        if args.execute:
            rec = execute(root, task, expect=args.expect, title=args.title, body_file=args.body_file, session=session)
            lines = ["alpaca attach handoff: pushed %s at %s" % (rec.outgoing["branch"], rec.outgoing["head"][:12]),
                     "  pull request: %s" % rec.pr_url, "  %s" % rec.error]
            return finish(args, "handoff", "PASS", lines=[x for x in lines if x.strip()], task=task,
                          receipt={"status": rec.status, "pr_url": rec.pr_url, "pr_number": rec.pr_number,
                                   "head": rec.outgoing["head"], "branch": rec.outgoing["branch"], "attempt": rec.attempt})
        if args.expect:
            raise AttachError("--expect belongs with --execute; a dry run takes none")
        info = plan(root, task, title=args.title, body_file=args.body_file, session=session)
    except AttachError as exc:
        return finish(args, "handoff", "BLOCKED", gitops.scrub(exc), task=task)
    return finish(args, "handoff", "PASS", lines=_plan_lines(info), task=task,
                  receipt={"status": DRY_RUN, "head": info["outgoing"]["head"], "branch": info["outgoing"]["branch"],
                           "receipt": info["receipt"], "commits": info["outgoing"]["commits_total"],
                           "files": info["outgoing"]["files_total"]})


def run_merge(args) -> int:
    from alpaca import cli as alpaca_cli
    from alpaca.attach.cli import finish
    root = alpaca_cli._root()
    task = getattr(args, "task", None)
    try:
        res = merge(root, task, expect=args.expect, method=args.method, execute=args.execute,
                    session=getattr(args, "session", None) or "cli")
    except AttachError as exc:
        return finish(args, "merge", "BLOCKED", gitops.scrub(exc), task=task)
    if args.execute:
        return finish(args, "merge", "PASS", task=task, lines=[
            "alpaca attach merge: merged pull request #%d at %s (%s)" % (res.pr_number, res.merge_sha[:12], res.method)],
            receipt={"status": res.status, "merge_sha": res.merge_sha, "pr_number": res.pr_number, "method": res.method,
                     "attempt": res.attempt})
    lines = ["alpaca attach merge (dry run): pull request #%d of %s" % (res["pr_number"], task),
             "  head %s into %s by %s, approval by %s" % (res["head"][:12], res["base"], res["method"], res["approval"]["approver"]),
             "  to merge: alpaca attach merge %s --execute --expect %s --method %s" % (task, res["head"], res["method"])]
    return finish(args, "merge", "PASS", lines=lines, task=task, receipt=res)


SUBVERBS = {"handoff": (add_handoff_parser, run_handoff), "merge": (add_merge_parser, run_merge)}
