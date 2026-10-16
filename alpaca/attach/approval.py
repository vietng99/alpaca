"""`alpaca attach approve`: the approval of one exact head commit (op-062 t-686, R8; build plan P15, P16).

A handoff leaves the studio only for a commit somebody approved, with the same record and the same revocation
chain as `alpaca release approve`, but in attach's own receipt kind (`attach-approval`), so a release approval of
the same sha authorizes nothing here and the reverse.

  * The receipt binds the head sha, the task, the repository (owner/name), the upstream URL, the work branch, the
    base branch and the digest of the gate set. `require` compares each of them with the project now, so an approval
    given before the URL, the branch, the base or a gate changed is void.
  * `--by owner` is typed in a terminal outside an agent session (`alpaca.release.cli.owner_confirmation`).
  * `--by agent` needs `--delegation PATH#Section`. The section holds one line
    `attach-delegation: <dec-id> <owner/name> <actions>`; the repository must be `attach.repository`, the decision
    must be in the record, and the actions (push, pr, merge) are the most the agent may do with the approval.
    `require` reads the section again at every use, so removing the line withdraws the delegation.
  * A check that found a gate red at its base (or at the preview base) needs an owner approval: an agent approval of
    such a head is refused, and so is a handoff on it.
  * The latest decision for a (task, sha) wins and a revocation wins when it is later. A row that cannot be read
    refuses everything: the receipt files and the event chain are verified, so deleting a revocation brings nothing back.
"""
from __future__ import annotations

import re

from alpaca.attach import AttachError, check, config, gitops, proven
from alpaca.attach.receipts import AttachApprovalReceipt

APPROVERS = ("owner", "agent")
DECISIONS = ("approve", "revoke")
ACTIONS = proven.ACTIONS
#: what a typed owner approval covers unless `--actions` says more: the merge stays a separate word
OWNER_ACTIONS = ("push", "pr")
_SHA = re.compile(r"[0-9a-f]{40}")
_TASK = re.compile(r"t-[0-9]{1,6}")


def need_task(task) -> str:
    if not isinstance(task, str) or not _TASK.fullmatch(task):
        raise AttachError("%r is not a task id (t-123)" % str(task)[:40])
    return task


def work_state(root, cfg, task) -> dict:
    """The proved facts of a task's work branch: `{"app", "view", "folder", "branch", "head"}`. Refuses a task with no
    work branch, a folder `prove_worktree` does not accept, and a head that differs from the branch ref."""
    need_task(task)
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
        raise AttachError("%s cannot be handed off: %s" % (task, exc)) from None
    head = gitops.in_worktree(app, task, folder, "rev-parse", "--verify", "HEAD")
    ref_sha = gitops.git(app, "rev-parse", "--verify", "refs/heads/%s^{commit}" % branch)
    if head != ref_sha:
        raise AttachError("the work folder of %s is at %s but %s is at %s" % (task, head[:12], branch, ref_sha[:12]))
    return {"app": app, "view": view, "folder": folder, "branch": branch, "head": head}


def _repository(cfg) -> str:
    return cfg.repository or ""


def _actions(requested, allowed, what) -> list:
    """`requested` (a list, a comma string or None) as a list in the order of ACTIONS; at most `allowed`."""
    if requested is None or requested == "":
        return [a for a in ACTIONS if a in allowed]
    names = requested.split(",") if isinstance(requested, str) else list(requested)
    names = [n.strip() for n in names if str(n).strip()]
    bad = [n for n in names if n not in ACTIONS]
    if bad or not names:
        raise AttachError("actions are a comma list of %s" % ", ".join(ACTIONS))
    over = [n for n in names if n not in allowed]
    if over:
        raise AttachError("%s does not allow %s" % (what, ", ".join(over)))
    return [a for a in ACTIONS if a in names]


def approve(root, task, sha, *, approver, delegation="", decision="approve", actions=None, session="cli"):
    """Record an approval or a revocation of `sha` for `task`; returns `(path of the receipt file, receipt)`.

    The owner's typed confirmation is the caller's (`run_verb`), as in `alpaca release approve`: this function
    records what it is told, so a script that calls it as `owner` has the owner's responsibility."""
    from alpaca.release.receipts import record_receipt
    if approver not in APPROVERS or decision not in DECISIONS:
        raise AttachError("approver must be owner or agent; decision must be approve or revoke")
    need_task(task)
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise AttachError("an approval needs the full 40-character commit sha")
    cfg = config.load(root)
    app = str(cfg.repo_path(root))
    delegation = (delegation or "").strip()
    if approver == "owner" and delegation:
        raise AttachError("--delegation is for --by agent; an owner approval is typed, not delegated")
    if decision == "approve":
        state = work_state(root, cfg, task)
        if state["head"] != sha:
            raise AttachError("%s is not the head of %s (%s); approve the commit the branch is at now"
                              % (sha[:12], task, state["head"][:12]))
        proven.config_unchanged(root, cfg, app)
        branch = state["branch"]
    else:
        if gitops.run(app, "cat-file", "-e", sha + "^{commit}").rc != 0:
            raise AttachError("%s is not a commit of %s/" % (sha[:12], cfg.repo))
        branch = cfg.branch_prefix + task
    held = {"path": "", "sha256": "", "dec": "", "actions": list(OWNER_ACTIONS)}
    if approver == "agent" and (decision == "approve" or delegation):
        held = proven.delegation(root, delegation)
        if not cfg.repository or held["repository"] != cfg.repository:
            raise AttachError("the delegation names %s but attach.repository is %s"
                              % (held["repository"], cfg.repository or "not set"))
    if decision == "approve":
        if approver == "owner":
            use = list(OWNER_ACTIONS) if actions is None else _actions(actions, ACTIONS, "an owner approval")
        else:
            use = _actions(actions, held["actions"], "the delegation")
        if approver == "agent":
            found = check.latest(root, task)
            if found is not None and found.head == sha and found.needs_owner_review:
                raise AttachError("the check of %s found gates red at base (%s); only an owner approval can pass it"
                                  % (task, ", ".join(sorted(set(found.red_at_base) | set(found.red_at_preview_base)))))
    else:
        use = []
    receipt = AttachApprovalReceipt(
        sha, approver, decision, task, _repository(cfg),
        proven.fetch_url(root, cfg) if decision == "approve" else cfg.url, branch, cfg.base,
        cfg.gate_set_digest(), delegation if approver == "agent" else "", held["sha256"] if approver == "agent" else "",
        held["dec"] if approver == "agent" else "", use, "APPROVED" if decision == "approve" else "REVOKED",
        bool(cfg.pr_footer), gitops.identity_label(proven.identity(root, cfg)))
    try:
        path = record_receipt(root, session, receipt)
    except (ValueError, OSError) as exc:
        raise AttachError("the approval could not be recorded: %s" % gitops.scrub(exc)) from None
    return path, receipt


def _rows(root) -> list:
    """Every recorded attach approval as `(path, receipt)`, oldest first. A row or file that cannot be read refuses."""
    from alpaca.release.receipts import load_receipt
    found = []
    for data in proven.receipt_rows(root, "attach-approval"):
        try:
            found.append((data["receipt"], load_receipt(root, data.get("receipt", ""), AttachApprovalReceipt)))
        except (KeyError, ValueError, OSError, TypeError):
            raise AttachError("an attach approval record is missing or damaged; no approval counts until it is restored") from None
    return found


def current(root, task, sha, *, with_path=False):
    """The latest decision for `(task, sha)` when it is an approval, else None (nothing recorded, or a revocation
    later than the approval). Refuses (AttachError) when the record or any approval receipt does not verify."""
    latest = None
    for path, receipt in _rows(root):
        if receipt.task == task and receipt.source_sha == sha:
            latest = (path, receipt)
    if latest is None or latest[1].decision != "approve":
        return None
    return latest if with_path else latest[1]


def require(root, task, sha, *, action, needs_owner=False):
    """The approval receipt that lets the agent do `action` (push, pr, merge, or a tuple of them) with `sha`, or an
    AttachError that says what is missing. Every binding of the receipt is compared with the project now."""
    actions = (action,) if isinstance(action, str) else tuple(action)
    cfg = config.load(root)
    rec = current(root, task, sha)
    if rec is None:
        raise AttachError("no current approval of %s for %s; run: alpaca attach approve %s %s --by owner"
                          % (sha[:12], task, task, sha))
    now = {"repository": _repository(cfg), "url": proven.fetch_url(root, cfg), "branch": cfg.branch_prefix + task,
           "base": cfg.base, "gate_set": cfg.gate_set_digest(), "pr_footer": bool(cfg.pr_footer),
           "commit_identity": gitops.identity_label(proven.identity(root, cfg))}
    for key, value in now.items():
        if getattr(rec, key) != value:
            raise AttachError("the approval of %s was given for another %s (%s); approve it again"
                              % (sha[:12], " ".join(key.split("_")), "the gates changed" if key == "gate_set" else "now differs"))
    missing = [a for a in actions if a not in rec.actions]
    if missing:
        raise AttachError("the approval of %s does not cover %s%s" % (
            sha[:12], ", ".join(missing),
            "; a merge needs an approval under a delegation that names merge, or --actions push,pr,merge typed by the owner"
            if "merge" in missing else ""))
    if rec.approver == "agent":
        try:
            held = proven.delegation(root, rec.delegation)
        except AttachError as exc:
            raise AttachError("the delegation behind the approval of %s no longer holds: %s" % (sha[:12], exc)) from None
        if held["repository"] != cfg.repository or held["dec"] != rec.dec_id or any(a not in held["actions"] for a in actions):
            raise AttachError("the delegation behind the approval of %s no longer covers %s" % (sha[:12], ", ".join(actions)))
    if needs_owner and rec.approver != "owner":
        raise AttachError("the check of %s found gates red at base; the approval must be the owner's (--by owner), "
                          "not an agent's" % task)
    return rec


# ----------------------------------------------------------------------------------------------- the verb

def add_parser(verbs):
    p = verbs.add_parser("approve", help="approve (or revoke) one exact head commit of a task for handoff")
    p.add_argument("task", help="the task whose work branch is approved (t-123)")
    p.add_argument("sha", help="the full 40-character head commit to approve")
    p.add_argument("--by", choices=APPROVERS, required=True,
                   help="owner: typed in a terminal outside an agent session; agent: needs --delegation")
    p.add_argument("--delegation", default="",
                   help="agent only: PATH#Section of a project file whose section holds "
                        "'attach-delegation: <dec-id> <owner/name> <push,pr,merge>'")
    p.add_argument("--actions", default=None, help="what the approval covers: a comma list of push, pr, merge "
                   "(owner default push,pr; agent default every action the delegation names)")
    p.add_argument("--revoke", action="store_true", help="record a revocation of this sha instead of an approval")
    p.add_argument("--json", action="store_true", help="print one JSON object")


def run_verb(args) -> int:
    from alpaca import cli as alpaca_cli
    from alpaca.attach.cli import finish
    root = alpaca_cli._root()
    task = getattr(args, "task", None)
    try:
        if args.by == "owner":
            from alpaca.release.cli import owner_confirmation
            try:
                owner_confirmation(args.sha)
            except ValueError as exc:
                raise AttachError(str(exc)) from None
        path, rec = approve(root, task, args.sha, approver=args.by, delegation=args.delegation or "",
                            decision="revoke" if args.revoke else "approve", actions=args.actions,
                            session=getattr(args, "session", None) or "cli")
    except AttachError as exc:
        return finish(args, "approve", "BLOCKED", gitops.scrub(exc), task=task)
    lines = ["alpaca attach approve: %s %s %s by %s" % (rec.status.lower(), task, rec.source_sha[:12], rec.approver),
             "  bound to %s, branch %s, base %s" % (rec.url if not rec.repository else "%s (%s)" % (rec.repository, rec.url),
                                                   rec.branch, rec.base)]
    if rec.decision == "approve":
        lines.append("  covers: %s" % ", ".join(rec.actions))
    lines.append("  receipt: %s" % path)
    receipt = {"receipt": str(path), "attempt": rec.attempt, "status": rec.status, "source_sha": rec.source_sha,
               "approver": rec.approver, "actions": rec.actions}
    return finish(args, "approve", "PASS", lines=lines, receipt=receipt, task=task)


SUBVERBS = {"approve": (add_parser, run_verb)}
