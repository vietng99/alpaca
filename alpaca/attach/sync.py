"""`alpaca attach sync`: what happened upstream to the studio's handoffs (op-062 t-686, R10; build plan P18).

Sync fetches the upstream base and reports; it merges, rebases, deletes and pushes nothing. The only ref it moves is
the fetched upstream base (`refs/remotes/<upstream>/<base>`), through `alpaca.attach.worktree.fetch_base`, the same
fetch every claim and check makes (explicit URL, proved config, no tags, no FETCH_HEAD).

For every task with a handoff receipt that pushed a branch:

  * merged when the handed-off head is an ancestor of the fetched base (a merge commit or a fast-forward), or when
    GitHub reports `merged_at` for the pull request (a squash or a rebase merge makes new commits, so ancestry
    cannot see it);
  * closed when the pull request is closed and was not merged;
  * open otherwise (with a note when the pull request could not be read: no token, no network).

Every work branch the studio holds that is not merged and sits behind the fetched base is listed as behind, with its
count (and how many commits it is ahead). When a codebase map exists, the study module flags the map areas that the base's new commits touched
(`study.prepare`); a failure there is a note, not a failure of the sync.

The result is one `attach-sync` event on op-0.
"""
from __future__ import annotations

import os

from alpaca.attach import AttachError, config, github, gitops, handoff, proven, study, worktree
from alpaca.attach.approval import need_task


def _is_ancestor(app, head, ref) -> bool:
    return gitops.run(app, "merge-base", "--is-ancestor", head, ref).rc == 0


def _event(root, session, data) -> None:
    from alpaca import db
    conn = db.connect(str(root))
    try:
        db.append_event(conn, session=session or "cli", actor="alpaca", kind="attach-sync", op="op-0", data=data)
    finally:
        conn.close()


def run(root, *, api=None, session="cli") -> dict:
    """Fetch the upstream base and report. Returns `{"base_sha", "merged", "closed", "open", "behind",
    "stale_areas", "notes"}`; each of merged, closed and open is a list of `{"task", "head", "pr_number", "pr_url", ...}`."""
    cfg = config.load(root)
    app = str(cfg.repo_path(root))
    base_sha = worktree.fetch_base(root, cfg)
    ref = "refs/remotes/%s/%s" % (cfg.upstream, cfg.base)
    gh = github.GitHub(cfg.repository, api) if cfg.repository else None
    notes, merged, closed, still_open = [], [], [], []
    last = {}
    for _path, rec in handoff.handoffs(root, statuses=(handoff.PUSHED, handoff.PUSHED_NO_PR, handoff.PUSHED_UNVERIFIED)):
        last[rec.task] = rec                                                    # the newest push of a task wins
    already = {m.task: m for m in handoff.merges(root)}
    for task in sorted(last):
        rec = last[task]
        head = rec.outgoing.get("head")
        item = {"task": task, "head": head, "branch": rec.outgoing.get("branch"), "pr_number": rec.pr_number,
                "pr_url": rec.pr_url}
        if not isinstance(head, str) or gitops.run(app, "cat-file", "-e", head + "^{commit}").rc != 0:
            notes.append("%s: the handed-off commit %s is not in the clone any more" % (task, str(head)[:12]))
            still_open.append(dict(item, note="commit missing"))
            continue
        if _is_ancestor(app, head, ref):
            merged.append(dict(item, by="ancestry"))
            continue
        pr = None
        if gh is not None and rec.pr_number:
            try:
                pr = gh.get_pr(rec.pr_number)
            except AttachError as exc:
                notes.append("%s: pull request #%d could not be read (%s)" % (task, rec.pr_number, gitops.scrub(exc)))
        if pr is not None and pr["merged"]:
            merged.append(dict(item, by="merged_at", merged_at=pr["merged_at"], merge_commit_sha=pr["merge_commit_sha"]))
        elif pr is not None and pr["state"] == "closed":
            closed.append(dict(item, state="closed"))
        else:
            still_open.append(dict(item, note="" if pr is not None else "state unknown"))
    gone = {i["task"] for i in merged}
    behind = []
    for task in proven.branch_tasks(root):
        view = proven.branch_view(root, cfg, task)
        if view is None or task in gone or task in already and already[task].status == handoff.MERGED:
            continue
        counted = gitops.run(app, "rev-list", "--count", "refs/heads/%s..%s" % (view["branch"], ref))
        ahead = gitops.run(app, "rev-list", "--count", "%s..refs/heads/%s" % (ref, view["branch"]))
        if counted.rc != 0 or ahead.rc != 0:
            notes.append("%s: the work branch %s could not be compared with the base" % (task, view["branch"]))
            continue
        if int(counted.out.strip() or 0) > 0:
            behind.append({"task": task, "branch": view["branch"], "behind": int(counted.out.strip()),
                           "ahead": int(ahead.out.strip() or 0), "handed_off": task in last})
    stale = []
    map_file = os.path.join(os.path.realpath(str(root)), study.MAP_PATH)
    if os.path.isfile(map_file) and not os.path.islink(map_file):
        try:
            info = study.prepare(root, session=session)
            stale = list(info["map"].get("stale_areas") or [])
        except AttachError as exc:
            notes.append("the codebase map could not be refreshed: %s" % gitops.scrub(exc))
    result = {"base_sha": base_sha, "merged": merged, "closed": closed, "open": still_open, "behind": behind,
              "stale_areas": stale, "notes": notes}
    _event(root, session, {"base_sha": base_sha, "merged": [i["task"] for i in merged], "closed": [i["task"] for i in closed],
                           "open": [i["task"] for i in still_open], "behind": [b["task"] for b in behind],
                           "stale_areas": stale, "notes": notes})
    return result


def add_parser(verbs):
    p = verbs.add_parser("sync", help="fetch the upstream base and report merged, closed and behind handoffs "
                                      "(changes no branch)")
    p.add_argument("--json", action="store_true", help="print one JSON object")


def run_verb(args) -> int:
    from alpaca import cli as alpaca_cli
    from alpaca.attach.cli import finish
    root = alpaca_cli._root()
    try:
        res = run(root, session=getattr(args, "session", None) or "cli")
    except AttachError as exc:
        return finish(args, "sync", "BLOCKED", gitops.scrub(exc))
    lines = ["alpaca attach sync: upstream base at %s" % res["base_sha"][:12]]
    for label in ("merged", "closed", "open"):
        for item in res[label]:
            lines.append("  %s: %s%s%s" % (label, item["task"], " (#%d)" % item["pr_number"] if item["pr_number"] else "",
                                          " by %s" % item["by"] if item.get("by") else ""))
    for item in res["behind"]:
        lines.append("  behind: %s is %d commit(s) behind the base" % (item["task"], item["behind"]))
    if res["stale_areas"]:
        lines.append("  codebase map areas to refresh: %s" % ", ".join(res["stale_areas"]))
    lines += ["  note: %s" % n for n in res["notes"]]
    if len(lines) == 1:
        lines.append("  no handoffs, and no work branch is behind")
    return finish(args, "sync", "PASS", lines=lines, receipt=res)


SUBVERBS = {"sync": (add_parser, run_verb)}
