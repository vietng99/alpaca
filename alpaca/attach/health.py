"""Doctor rows and `alpaca attach status` for attach mode (R2).

Every row is silent when project.yaml has no `attach:` key (R11): a plain project's doctor output is
byte for byte what it was. The rows read files and run local git only; none of them touches the network.

    attach-config      the block parses and every rule holds; names the mode
    attach-repo        the clone is a real folder of the studio, a git top level of its own
    attach-upstream    the upstream remote exists and its base branch was fetched
    attach-git-config  the clone's config keys that steer a push have not changed since attach
    attach-worktrees   every task worktree exists and belongs to the clone; the studio did not move
    attach-push-checks which checks stand between an agent and a push

A git row runs only when attach-repo passed: `git -C app` on a plain folder would answer for the
studio repository that contains it, and a doctor row must never read the wrong repository.
"""
from __future__ import annotations

import glob
import json
import os

from alpaca.attach import AttachError, config, gitops


def _printable(text) -> str:
    """A recorded path as one safe line: a control character (an escape sequence, a newline) becomes ?."""
    return "".join(c if c.isprintable() else "?" for c in str(text))


MODE_NAME = "attach"


def _repo_name(block):
    value = block.get("repo") if isinstance(block, dict) else None
    return value if isinstance(value, str) else None


def repo_problems(root, repo) -> list:
    """What is wrong with the attached clone `repo` of the studio at `root`; [] when it is sound."""
    from alpaca.release import config as rconfig
    if not isinstance(repo, str) or not repo:
        return ["attach.repo is not set"]
    if "/" in repo or repo.startswith(".") or repo in ("", ".", ".."):
        return ["attach.repo %s is not one plain folder name of the studio root" % repo]
    try:
        path = str(rconfig.contained(root, repo))
    except ValueError as exc:
        return ["attach.repo %s is not a real folder inside the studio (%s)" % (repo, exc)]
    if not os.path.isdir(path):
        return ["%s/ is missing from the studio; run alpaca attach init again or clone it back" % repo]
    dot = os.path.join(path, ".git")
    if os.path.islink(dot):
        return ["%s/.git is a link" % repo]
    if os.path.isfile(dot):
        return ["%s/.git is a gitfile that points elsewhere; the clone must hold its own .git folder" % repo]
    if not os.path.isdir(dot):
        return ["%s/ is not a git repository of its own (it has no .git folder)" % repo]
    if not gitops.is_toplevel(path):
        return ["%s/ is not a git repository of its own (git reports another top level)" % repo]
    out = gitops.clone_problems(path, "%s/" % repo)
    kind = gitops.studio_kind(root)
    if kind == "link":
        out.append("the studio path is a link; use its real path")
    elif kind == "borrowed":
        out.append("the studio is a linked git worktree or a folder inside another repository; git then reads the "
                   "ignore rules from outside the studio")
    if kind in ("own", "borrowed"):                  # a link or a git-less studio has no exclude file to name
        if gitops.git(root, "ls-files", "-s", "--", repo, check=False).startswith("160000"):
            out.append("the studio tracks %s as an embedded repository (a gitlink); un-track it" % repo)
        if gitops.run(root, "check-ignore", "-q", "--", "%s/x" % repo).rc != 0:
            out.append("the studio does not ignore %s/, so git add would stage it; add /%s/ to "
                       ".git/info/exclude" % (repo, repo))
    return out


def _worktree_rows(root, cfg, state, add) -> None:
    app = str(cfg.repo_path(root))
    studio = os.path.realpath(str(root))
    problems, notes = [], []
    moved = bool(state) and isinstance(state.get("root"), str) and state["root"] != studio
    branch_files = sorted(glob.glob(os.path.join(str(config.state_dir(root)), "branches", "t-*.json")))
    for bf in branch_files:
        task = os.path.basename(bf)[:-5]
        folder = os.path.join(studio, ".alpaca", "attach", "worktrees", task)
        if not os.path.isdir(folder):
            recorded = (config.read_branch_file(root, task) or {}).get("worktree")       # report only
            problems.append("%s: worktree %s is missing" % (task, recorded if isinstance(recorded, str) else folder))
            continue
        try:
            gitops.prove_worktree(app, task, folder, cfg.branch_prefix + task)
        except AttachError as exc:
            problems.append("%s: %s" % (task, exc))
    listing = gitops.git(app, "worktree", "list", "--porcelain", check=False)
    outside = [line[len("worktree "):] for line in listing.splitlines()
               if line.startswith("worktree ") and not os.path.realpath(line[len("worktree "):]).startswith(studio + os.sep)]
    if outside:
        problems.append("git lists worktrees outside the studio (%s)" % ", ".join(outside[:3]))
    if moved and problems:
        problems.append("the studio moved from %s to %s" % (_printable(state["root"]), studio))
    if problems:
        level = "error" if (branch_files or outside) else "warn"
        add("attach-worktrees", level, "; ".join(problems[:4]) + "; run `git -C %s worktree repair` from the studio" % cfg.repo)
    elif moved:
        # nothing is broken yet (no task worktree holds the old path), but state.json names the old folder
        add("attach-worktrees", "warn", "the studio moved from %s to %s since attach; no task worktree exists yet, "
            "so none needs repair (run `git -C %s worktree repair` from the studio if one is made later)"
            % (_printable(state["root"]), studio, cfg.repo))
    else:
        add("attach-worktrees", "ok", "%d task worktree(s), all inside the studio" % len(branch_files))


def doctor_check(root, add) -> None:
    """Add the attach rows. Adds nothing when project.yaml has no `attach:` key (R11)."""
    block = config.raw(root)
    if block is config.MISSING:
        return
    errors = config.validate(block, root)
    cfg = config.mode(root)
    if errors:
        add("attach-config", "error", "; ".join(errors[:3]))
    else:
        add("attach-config", "ok", "attach mode on: repo %s/, upstream %s/%s, work branches %s<task>, %d gate(s): %s"
            % (cfg.repo, cfg.upstream, cfg.base, cfg.branch_prefix, len(cfg.gates),
               ", ".join(g["name"] for g in cfg.gates)))
    problems = repo_problems(root, _repo_name(block))
    if problems:
        add("attach-repo", "error", "; ".join(problems[:3]))
        return
    add("attach-repo", "ok", "%s/ is a git repository of its own inside the studio" % cfg.repo if cfg else "the clone is sound")
    if cfg is None:
        return
    app = str(cfg.repo_path(root))
    remote = gitops.git(app, "remote", "get-url", cfg.upstream, check=False)
    ref = gitops.git(app, "rev-parse", "--verify", "-q", "refs/remotes/%s/%s^{commit}" % (cfg.upstream, cfg.base), check=False)
    if not remote:
        add("attach-upstream", "error", "remote %s does not exist in %s/" % (cfg.upstream, cfg.repo))
    elif not ref:
        add("attach-upstream", "error", "%s/%s was never fetched in %s/ (the base branch is missing)"
            % (cfg.upstream, cfg.base, cfg.repo))
    else:
        add("attach-upstream", "ok", "%s at %s, base %s/%s at %s" % (cfg.upstream, gitops.scrub(remote), cfg.upstream,
                                                                     cfg.base, ref[:12]))
    state = config.read_state(root)                      # display and "moved" only; the digest comes from the record
    trap = gitops.git(app, "config", "--local", "--get", "remote.%s.pushurl" % cfg.upstream, check=False)
    try:
        from alpaca.attach import proven
        recorded_digest = proven.init_facts(root, cfg)["config_digest"]
    except AttachError as exc:
        recorded_digest = None
        digest_problem = str(exc)
    if state is None:
        add("attach-git-config", "warn", "no .alpaca/attach/state.json (display only; the record holds the config digest)")
    elif recorded_digest is None:
        add("attach-git-config", "error", digest_problem)
    elif recorded_digest != gitops.config_digest(app):
        add("attach-git-config", "error", "app git config changed since attach (any key but the core housekeeping, "
            "alias and branch keys counts); review %s/.git/config" % cfg.repo)
    elif not trap:
        add("attach-git-config", "warn", "the push trap (remote.%s.pushurl) is not set in %s/.git/config" % (cfg.upstream, cfg.repo))
    else:
        from alpaca.attach import repo as attach_repo
        have = attach_repo.push_rewrites(app)
        want = attach_repo.push_url_forms(gitops.git(app, "config", "--local", "--get", "remote.%s.url" % cfg.upstream,
                                                     check=False))
        absent = [x for x in want if x not in have]
        if not have or absent:
            what = ("has no %s entries (a studio attached before t-697 has none)" % attach_repo.PUSH_REWRITE_KEY if not have
                    else "lacks %d spelling(s) of the upstream's address in %s" % (len(absent), attach_repo.PUSH_REWRITE_KEY))
            add("attach-git-config", "warn", "%s/.git/config %s, so a push that spells the upstream's address out is not "
                "rewritten. The remote.%s.pushurl trap still holds a push by the remote's name. A new studio gets the "
                "entries; do not add them by hand, that changes the config digest" % (cfg.repo, what, cfg.upstream))
        else:
            add("attach-git-config", "ok", "config digest unchanged; plain pushes from %s/ are blocked by remote.%s.pushurl, "
                "and a push to the upstream's address by %d of its spellings by %s" % (cfg.repo, cfg.upstream, len(have),
                                                                                       attach_repo.PUSH_REWRITE_KEY))
    _worktree_rows(root, cfg, state, add)
    hook = os.path.join(app, ".git", "hooks", "pre-push")
    own = gitops.git(app, "config", "--local", "--get", "core.hooksPath", check=False)
    present = os.path.isfile(hook) or bool(own)
    from alpaca.attach import repo as attach_repo
    blocks = "pushurl and pushInsteadOf block" if attach_repo.push_rewrites(app) else "pushurl block"
    add("attach-push-checks", "ok", "push checks: git guard, %s, handoff scan; repository pre-push hook %s"
        % (blocks, "present" if present else "absent"))


# --------------------------------------------------------------------------------------- status

def _handoffs(root) -> list:
    out = []
    for path in sorted(glob.glob(os.path.join(str(root), ".alpaca", "releases", "receipts", "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("kind") == "attach-handoff":
            out.append({"task": doc.get("task"), "status": doc.get("status"), "pr_url": doc.get("pr_url", "")})
    return out


def _json_file(path):
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _ids(value) -> list:
    return [x for x in value if isinstance(x, str) and x] if isinstance(value, list) else []


def _stale_shown(root, folder) -> list:
    """The stale areas `status` shows: the default map's own flag (stale.json, top level), then the areas of
    the map the wiki serves (served.json, written by a complete ingest) that are stale. The flag of the served
    map in stale.json counts only when its map and sha are the ones in served.json and it was computed for the
    newest recorded base; a missing or different entry, or any error, reads as every served area stale
    (unknown is stale). Only non-empty strings are shown."""
    flag = _json_file(os.path.join(folder, "stale.json")) or {}
    shown = _ids(flag.get("areas"))
    served = _json_file(os.path.join(folder, "served.json"))
    if served is None or not isinstance(served.get("areas"), list):
        return shown
    ids = [a[0] for a in served["areas"] if isinstance(a, list) and a and isinstance(a[0], str) and a[0]]
    entry = flag.get("served")
    try:
        from alpaca.attach import study                     # imported here: study imports this module
        newest = study._latest_base(root, readonly=True)
    except Exception:                                        # noqa: BLE001 - no record to read: unknown is stale
        newest = None
    if (isinstance(entry, dict) and newest is not None and entry.get("map") == served.get("map")
            and entry.get("sha") == served.get("sha") and entry.get("to_sha") == newest):
        ids = [x for x in _ids(entry.get("areas")) if x in ids]
    return shown + [x for x in ids if x not in shown]


def status(root) -> dict:
    """What attach mode holds, from files only (no git, no network)."""
    block = config.raw(root)
    if block is config.MISSING:
        return {"mode": False}
    cfg = config.mode(root)
    stale = _stale_shown(root, os.path.join(str(config.state_dir(root)), "study"))
    branches = sorted(os.path.basename(p)[:-5] for p in glob.glob(os.path.join(str(config.state_dir(root)), "branches", "t-*.json")))
    return {"mode": True, "valid": cfg is not None, "errors": config.validate(block, root),
            "repo": cfg.repo if cfg else _repo_name(block), "upstream": cfg.upstream if cfg else None,
            "base": cfg.base if cfg else None, "branch_prefix": cfg.branch_prefix if cfg else None,
            "url": cfg.url if cfg else None, "gates": [g["name"] for g in cfg.gates] if cfg else [],
            "worktrees": branches, "stale_areas": stale, "handoffs": _handoffs(root)}


def add_parser(verbs):
    p = verbs.add_parser("status", help="show the attach settings, task worktrees, stale areas and handoffs")
    p.add_argument("--json", action="store_true", help="print one JSON object")


def run(args) -> int:
    from alpaca import cli as alpaca_cli
    from alpaca.attach.cli import finish
    info = status(alpaca_cli._root())
    if not info["mode"]:
        return finish(args, "status", "BLOCKED", "this project is not attached: project.yaml has no attach: block")
    lines = ["alpaca attach status: repo %s/ from %s" % (info["repo"], info["url"]),
             "  upstream %s, base %s, work branches %s<task>" % (info["upstream"], info["base"], info["branch_prefix"]),
             "  gates: %s" % (", ".join(info["gates"]) or "(none)"),
             "  task worktrees: %s" % (", ".join(info["worktrees"]) or "(none)"),
             "  stale areas: %s" % (", ".join(info["stale_areas"]) or "(none)"),
             "  handoffs: %s" % (", ".join("%s %s" % (h["task"], h["status"]) for h in info["handoffs"]) or "(none)")]
    lines += ["  CONFIG %s" % e for e in info["errors"]]
    return finish(args, "status", "PASS" if info["valid"] else "FAIL", "" if info["valid"] else info["errors"][0],
                  lines=lines, receipt=info)


SUBVERBS = {"status": (add_parser, run)}
