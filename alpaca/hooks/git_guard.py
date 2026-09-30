"""PreToolUse (Bash only): deny git commands that rewrite the shared main tree's working files.

Several sessions work in one checkout at once. A `git stash`, `git reset --hard`, `git checkout --
<path>`, `git restore <path>` or `git clean -f` run by one of them rewrites or removes files the
others are editing, and on 2026-09-30 an accidental `git stash` swapped the live record under
running sessions and corrupted it. This hook denies those commands when they would run against the
project's own repository. The same commands stay allowed in a separate worktree, in a nested
repository and outside the project, and the read-only forms (`git stash list`, `git stash show`,
`git restore --staged`, `git clean -n`) stay allowed everywhere.

It only ever writes one thing on stdout: a PreToolUse deny decision. Every other path, including a
parse failure, prints nothing, so the call goes ahead (fail open). It never opens the record.
"""
import json
import os
import shlex
import subprocess

from alpaca.hooks import common

SEPARATORS = {";", "&&", "||", "|", "&", "\n", "(", ")"}
GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


def _segments(command):
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    seg = []
    for tok in lexer:
        if tok in SEPARATORS or set(tok) <= set(";&|()\n"):
            if seg:
                yield seg
            seg = []
        else:
            seg.append(tok)
    if seg:
        yield seg


def _short_flags(args):
    return {c for a in args if a.startswith("-") and not a.startswith("--") for c in a[1:]}


def rewrites_worktree(sub, args):
    """The reason this git subcommand rewrites or deletes working files, or None."""
    if sub == "stash":
        if args[:1] in (["list"], ["show"]):
            return None
        return "git stash moves every uncommitted change, other sessions' included, out of the tree"
    if sub == "reset" and {"--hard", "--merge", "--keep"} & set(args):
        return "git reset --hard rewrites working files"
    if sub == "checkout":
        if "--" in args or "." in args or {"-f", "--force", "-p", "--patch"} & set(args):
            return "git checkout of paths overwrites working files"
        return None
    if sub == "restore":
        staged = "--staged" in args or "S" in _short_flags(args)
        worktree = "--worktree" in args or "W" in _short_flags(args)
        if worktree or not staged:
            return "git restore overwrites working files"
        return None
    if sub == "clean":
        if "--dry-run" in args or "n" in _short_flags(args):
            return None
        if "--force" in args or "f" in _short_flags(args):
            return "git clean deletes untracked files, other sessions' included"
        return None
    if sub == "switch" and {"--discard-changes", "-f", "--force"} & set(args):
        return "git switch --discard-changes overwrites working files"
    return None


LIVE_RECORD = ".alpaca/alpaca.db"
BRINGS_REVISION = {"merge", "cherry-pick", "rebase", "checkout", "switch", "pull", "revert"}


def _git_out(path, *args):
    try:
        p = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=3,
                           env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def writes_live_record(path, sub, args):
    """The reason a merge, pick or switch would make git write the untracked live record, or None.

    The live record is untracked from 2026-09-30, but older commits still carry it. Git overwrites
    an ignored file without asking, so bringing in a revision that changed it, or switching to one
    that tracks it, would replace the running database."""
    if sub not in BRINGS_REVISION:
        return None
    if sub == "pull":
        return "git pull can bring in a revision that still carries %s" % LIVE_RECORD
    for arg in args:
        if arg.startswith("-") or arg in ("--",):
            continue
        rev = _git_out(path, "rev-parse", "--verify", "-q", arg + "^{commit}")
        if not rev or not _git_out(path, "ls-tree", "--name-only", rev, "--", LIVE_RECORD):
            continue
        if sub in ("checkout", "switch"):
            return "%s tracks %s, so switching to it would overwrite the live record" % (arg, LIVE_RECORD)
        base = _git_out(path, "merge-base", "HEAD", rev)
        changed = _git_out(path, "diff", "--name-only", base or rev + "^", rev, "--", LIVE_RECORD)
        if changed:
            return ("%s changes %s, so git %s would write it over the live record; in that branch's "
                    "worktree run `git rm --cached %s` and commit first" % (arg, LIVE_RECORD, sub, LIVE_RECORD))
    return None


def findings(command, cwd):
    """(target directory, reason) for each working-tree-rewriting git call in a shell command."""
    here = cwd
    out = []
    for seg in _segments(command):
        while seg and "=" in seg[0] and not seg[0].startswith(("-", "=")):
            seg = seg[1:]                       # VAR=value prefixes
        if not seg:
            continue
        if seg[0] == "cd" and len(seg) > 1:
            here = os.path.join(here, os.path.expanduser(seg[1]))
            continue
        if os.path.basename(seg[0]) != "git":
            continue
        target, i = here, 1
        while i < len(seg) and seg[i].startswith("-"):
            opt = seg[i]
            if opt in GIT_OPTS_WITH_VALUE and i + 1 < len(seg):
                if opt == "-C":
                    target = os.path.join(target, os.path.expanduser(seg[i + 1]))
                i += 2
            else:
                i += 1
        if i >= len(seg):
            continue
        reason = rewrites_worktree(seg[i], seg[i + 1:])
        if not reason and seg[i] in BRINGS_REVISION:
            reason = writes_live_record(os.path.normpath(target), seg[i], seg[i + 1:])
        if reason:
            out.append((os.path.normpath(target), reason))
    return out


def _main_toplevel(path):
    """The top of the repository at path when it is a main working tree; None for a linked worktree
    (git worktree add), which has its own files, or when path is not in a repository."""
    try:
        p = subprocess.run(["git", "-C", path, "rev-parse", "--path-format=absolute", "--show-toplevel",
                            "--git-dir", "--git-common-dir"], capture_output=True, text=True, timeout=3,
                           env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
    except (OSError, subprocess.SubprocessError):
        return None
    lines = p.stdout.split("\n")
    if p.returncode != 0 or len(lines) < 3:
        return None
    top, git_dir, common = (os.path.realpath(x) for x in lines[:3])
    return top if git_dir == common else None


def decide(payload):
    """The deny decision for one PreToolUse payload, or None to let the call run."""
    if payload.get("tool_name") != "Bash":
        return None
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or "git" not in command:
        return None
    from alpaca import paths
    cwd = payload.get("cwd") or os.getcwd()
    root = os.path.realpath(paths.root(cwd))
    for target, reason in findings(command, cwd):
        if _main_toplevel(target) == root:
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Alpaca git guard: %s in the shared main tree at %s, where other sessions and the "
                    "live record work. Use `git diff`, `git stash list` or `git status` to inspect, "
                    "revert a single file of your own with an edit, or do this in a separate "
                    "worktree (git worktree add). If the owner wants it here, they run it in their "
                    "own terminal." % (reason, root))}}
    return None


@common.fail_open(record_cost=False)
def main():
    decision = decide(common.read_stdin())
    if decision:
        print(json.dumps(decision))


if __name__ == "__main__":
    main()
