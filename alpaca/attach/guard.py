"""The attach rules of the git guard (op-062 t-651, build plan 6.5, requirement R7).

`alpaca/hooks/git_guard.py` calls `check(command, cwd)` for every Bash command that mentions `git`
or `gh`. The answer is None (not our business, or fine) or a `Deny`. Four promises hold:

  * Nothing in a project that was never attached pays for this: the first thing `check` does is look
    for `.alpaca/attach/state.json` in the folders above the working directory and above every path
    the command names. No such file, no work, no answer.
  * The studio is chosen from the TARGET of each git call (`-C`, `--git-dir`, `--work-tree`,
    `GIT_DIR`, `GIT_WORK_TREE`, in the text or in the session's environment, `cd`), not from the
    session's working directory. A session sitting in another project that runs `git -C /trial/app push`
    is judged by the trial studio's rules.
  * The rules fail closed for an attached target. Every git call gets a state: closed (the text fixes
    its program word, subcommand and words), open (a program such as xargs, parallel, find -exec or
    watch adds words the text does not show) or unresolved (a word is built at run time, an option is
    not one the guard reads, a lookup failed). A closed call is allowed only when its subcommand is on
    the table below and its words pass that entry's rules; an open call only for a short list of reads;
    an unresolved call never. A command that cannot be read, or that the rules fail on, is denied while
    a studio is involved.
  * A part built at run time counts wherever it sits in a word, not only at the start. The shell cuts the
    result of an unquoted `$(...)`, backtick or `$NAME` into words, so `origin$(printf ' --upload-pack=x')`
    reaches git as two. `_lift` marks a substitution outside quotes `_SPLIT` and one in double quotes `_MARK`
    (one word); an argument word with either, or with a variable the text does not give, is judged like any
    word built at run time, a global option or a wrapper word the shell can cut makes the call unresolved, and
    a value position is excused only for a word that cannot be cut (op-062 t-676, round 6). A `$(` or backtick
    that `_lift` left in a word was single-quoted and stays text; a `$NAME` is not told apart after `shlex`.
  * Program text the guard cannot read is not a free pass: inline interpreter code, a shell fed by a
    pipe, xargs or a runner, and awk or sed program text are denied when they mention git or gh.

What the text layer cannot see passes it: a path with no readable `git` or `gh` word in the Bash text
(a script on disk, `make push`, a word built at run time such as `$(printf gi)t`), a Write or Edit of
the clone's git config or hooks, and a failure to find the studio (the hook fails open). The second
layer, `remote.<upstream>.pushurl` and the `url.<trap>.pushInsteadOf` entries for each spelling of the
upstream's address (t-697), both set at attach time in the shared git config, hold a push that names the
remote and a push that spells the address out, but it is config the agent can write through any of those
paths, and a `url.<x>.insteadOf` rule in the global config (which config_digest does not hash) rewrites
it. Only a credential the clone and the session do not hold removes that residue (an orchestrator decision
for t-654 or a follow-up); a strict xfail in test_attach_push_block.py pins it. This file is the first
layer only.

Rules, for a target that is the attached clone ("attached-main") or one of its task worktrees
("attached-worktree"):
  * always denied: push, send-pack, send-email, imap-send, daemon, writes to remotes, config,
    worktrees, tags, refs and notes, history rewriting, switching branches, a `-c` key that is not on
    the inert list, a GIT_* variable set in the command that is not on the allow-list, `--no-verify`
    (or any abbreviation of it), `commit -n`, `--exec-path`, `--upload-pack|--receive-pack|--exec`,
    `--output` on the commands that write a file with it, `<external command> push` (lfs, subtree),
    any subcommand that is not on the table, and any command text that git itself runs (`rebase -x`,
    `submodule foreach`, `bisect run`, `grep -O`, a `!` alias) when that text would be denied;
  * denied unless the target is a task worktree on its work branch, as `alpaca.attach.proven.checkout_at`
    proves it (the `attach-branch` event of the task, the folder derived from the task id, and
    `gitops.prove_worktree`; a folder made by hand, another branch, a detached HEAD or a rewritten `.git`
    file is not one): commit, merge, rebase, reset, cherry-pick, revert, am, pull, stash, clean, restore,
    and the index and file commands add, rm, mv, apply, update-index, read-tree, checkout-index,
    sparse-checkout. In the main clone they are always denied. No git runs in a task work folder by its
    own `.git` file (gitops.run refuses it); a lookup there goes through `gitops.run_in_worktree`;
  * allowed: status, diff, log, show, fetch, rev-parse, ls-files, worktree list, and the other reads
    on the table.
`gh` while involved: `api` with a write method or a body is denied; every other gh command is
allowed only with a read verb (view, list, status, diff, checks, ...), so an unlisted write verb is
denied too; a gh call that a program adds words to needs its group and read verb in the text.
"""
from __future__ import annotations

import os
import re
import shlex
from collections import namedtuple

from alpaca.attach import STATE_DIR, config, gitops, gitopts, proven

#: studio (root of the attach studio), reason (one sentence), worktrees (where task worktrees live)
Deny = namedtuple("Deny", "studio reason worktrees")

MAX_DEPTH = 4
_RANK = {"attached-main": 2, "attached-worktree": 1}

_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
_VAR = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
#: a `git` or `gh` word inside other text
_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])(git|gh)(?![A-Za-z0-9_-])")
#: what a subcommand word may hold once expanded
_SUBWORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")
#: the word that stands for a command substitution once it is lifted out of the text; it holds `$`, so
#: `_expand` reads it as a value that is not known
_MARK = "$__alpaca_subst__"
#: the same for a substitution outside quotes: the shell cuts its result into words, wherever it sits in a word
_SPLIT = "$__alpaca_split__"
_MARK_RE = re.compile(re.escape(_MARK) + "|" + re.escape(_SPLIT))
#: the one unquoted substitution that stays one word: the folder of the repository, a path
_ONE_WORD = re.compile(r"\s*git\s+rev-parse\s+--show-toplevel\s*")
_BLANK = re.compile(r"\s")
_KEYWORDS = {"then", "do", "else", "elif", "if", "while", "until", "!", "{", "}", "coproc"}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "ash", "fish", "tcsh", "csh"}
_INTERPRETER = re.compile(r"^(python[0-9.]*|node|nodejs|ruby|perl|php|deno|bun|lua|pwsh)$")
_INLINE_FLAGS = {"-c", "-e", "-E", "-r", "--eval", "--command", "-command"}
#: programs that run their own program text; a git or gh word in it cannot be judged
_CODE_TOOLS = {"awk", "gawk", "mawk", "nawk", "sed", "gsed"}

#: wrappers that run the rest of their words: (flags, options that take a value, positional words to drop)
_WRAPPERS = {
    "command": ({"-p", "-v", "-V"}, set(), 0),
    "builtin": (set(), set(), 0),
    "exec": ({"-c", "-l"}, {"-a"}, 0),
    "nohup": (set(), set(), 0),
    "setsid": ({"-c", "-f", "-w", "--ctty", "--fork", "--wait"}, set(), 0),
    "time": ({"-p", "-v", "-a", "-q", "--portability", "--verbose", "--append", "--quiet"},
             {"-f", "-o", "--format", "--output"}, 0),
    "stdbuf": (set(), {"-i", "-o", "-e", "--input", "--output", "--error"}, 0),
    "unbuffer": ({"-p"}, set(), 0),
    "doas": ({"-n", "-s"}, {"-u", "-C"}, 0),
    "nice": (set(), {"-n", "--adjustment"}, 0),
    "ionice": ({"-t", "--ignore"}, {"-c", "-n", "--class", "--classdata"}, 0),
    "timeout": ({"--preserve-status", "--foreground", "-v", "--verbose"},
                {"-s", "-k", "--signal", "--kill-after"}, 1),
    "sudo": ({"-n", "-E", "-H", "-S", "-k", "-A", "-B", "-P", "-b", "--non-interactive", "--preserve-env",
              "--set-home", "--stdin", "--askpass", "--bell", "--preserve-groups", "--background"},
             {"-u", "-g", "-h", "-p", "-C", "-R", "-T", "-U", "-D", "--user", "--group", "--host", "--prompt",
              "--close-from", "--chroot", "--command-timeout", "--other-user", "--chdir"}, 0),
    "busybox": (set(), set(), 0),
}
_XARGS_VALUE = {"-I", "-n", "-P", "-d", "-L", "-s", "-E", "-a", "-l", "--max-args", "--max-procs", "--delimiter",
                "--max-lines", "--arg-file", "--max-chars", "--eof", "--process-slot-var", "--replace"}

#: git options before the subcommand: those that take the next word as a value (the `--opt=value` form
#: needs no entry), and the flags. Any other option word there makes the call unresolved.
_GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix",
                   "--attr-source", "--list-cmds"}
_GIT_FLAGS = {"-p", "--paginate", "-P", "--no-pager", "--bare", "--no-replace-objects", "--no-lazy-fetch",
              "--literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs",
              "--no-optional-locks", "--no-advice", "--version", "-v", "--help", "-h", "--html-path", "--man-path",
              "--info-path", "--exec-path"}

#: `-c key=value` keys that change where git pushes, what it runs, or who it asks for a secret (first check,
#: for its reason); every key off the inert list below is denied after it
_BAD_CONFIG = ("core.hookspath", "core.sshcommand", "core.askpass", "core.fsmonitor", "core.gitproxy",
               "core.pager", "core.editor", "url.", "remote.", "credential.", "alias.", "include.",
               "includeif.", "protocol.", "http.", "pager.", "diff.external", "filter.", "gpg.", "sequence.editor",
               "interactive.difffilter", "core.alternaterefscommand", "uploadpack.", "receive.",
               "difftool.", "mergetool.", "sendemail.", "ssh.")
_BAD_CONFIG_RE = re.compile(r"^(diff\..+\.(command|textconv)|merge\..+\.driver|submodule\..+\.update)$")
#: `-c` and `--config-env` keys that change only how git shows or names things (an allow-list)
_INERT_CONFIG_PREFIX = ("color.", "advice.", "i18n.")
_INERT_CONFIG = {"column.ui", "core.quotepath", "core.autocrlf", "core.safecrlf", "core.filemode", "core.abbrev",
                 "user.name", "user.email", "merge.conflictstyle", "merge.renames", "diff.renames", "diff.algorithm",
                 "diff.noprefix", "log.decorate", "log.date", "format.pretty", "status.short", "status.branch",
                 "submodule.recurse", "init.defaultbranch", "rebase.autostash", "pull.rebase", "pull.ff",
                 "fetch.prune", "gc.auto", "maintenance.auto"}
#: variables set in the command text that do what a bad `-c` or `--exec-path` does (first check, for its reason)
_BAD_ENV = {"GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
            "GIT_EXEC_PATH", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS", "GIT_PROXY_COMMAND"}
_BAD_ENV_PREFIX = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
#: GIT_* variables a command may set for git (an allow-list): the targets, which the guard follows, and inert names
_GIT_ENV_TARGETS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE")
_GIT_ENV_INERT = {"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE", "GIT_COMMITTER_NAME",
                  "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE", "GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS",
                  "GIT_CEILING_DIRECTORIES", "GIT_NO_REPLACE_OBJECTS", "GIT_MERGE_AUTOEDIT"}
_GIT_TRACE_VALUES = {"0", "1", "2", "true", "false"}
#: other variables that name a program git runs, a file the shell loads, or where git finds its config
_ENV_DENY = {"EDITOR", "VISUAL", "SSH_ASKPASS", "BASH_ENV", "ENV", "LD_PRELOAD", "LD_LIBRARY_PATH", "PATH", "HOME",
             "XDG_CONFIG_HOME"}
_PAGERS = {"cat", "less", "more"}
#: `PATH=$PATH:<literal folders>` only adds folders behind the ones git already searches (t-676)
_PATH_APPEND = re.compile(r"(\$PATH|\$\{PATH\})(:(~/)?[A-Za-z0-9_./+-]+)+")

#: programs whose words the guard reads as names and patterns, not as a command line (t-676): a `git` word
#: among them is no call. What they load or run from disk is outside this layer (see `_arg_only`).
#: `make` is here only in its target-only form (`_make_targets_only`)
_ARG_ONLY = {"pytest", "py.test"}
_MAKE_FLAG = re.compile(r"-[ksnBwiqrR]+|-j[0-9]*|--(keep-going|silent|quiet|dry-run|just-print|always-make|"
                        r"no-print-directory|jobs(=[0-9]+)?)")
_MAKE_TARGET = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./+-]*")
_MAKE_ENV = {"MAKEFLAGS", "GNUMAKEFLAGS", "MAKEFILES", "MAKE", "MAKELEVEL"}
#: programs that run the words they are given as one command and add none of their own (t-676); with plain
#: words only (nothing for the shell they use to read again) a git call among them is closed, not open
_NO_ADDED_WORDS = {"watch", "flock", "strace", "ltrace", "chronic"}
_PLAIN_WORD = re.compile(r"[A-Za-z0-9_./:=@%+,^~-]+")

#: programs that only print or search their words; a `git` word among them is text, not a call
_PRINTERS = {"echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "ack", "man", "which", "type", "whatis",
             "whereis", "apropos", "cat", "less", "more", "head", "tail", "wc", "tr", "cut", "sort", "uniq", "test",
             "[", "[[", "tee", "jq", "ls", "touch", "mkdir", "rm", "cp", "mv", "true", "false", "read"}

_KNOWN = frozenset("""add am annotate apply archive bisect blame branch bundle cat-file check-attr check-ignore checkout
cherry cherry-pick clean clone commit commit-tree config count-objects credential describe diff diff-tree difftool
fast-import fetch filter-branch filter-repo for-each-ref format-patch fsck gc grep hash-object help hook init log
ls-files ls-remote ls-tree maintenance merge merge-base merge-tree mergetool mktree mv name-rev notes pack-objects
prune pull push range-diff rebase reflog remote repack replace rerere reset restore rev-list rev-parse revert rm
send-pack shortlog show show-branch show-ref sparse-checkout stash status submodule switch symbolic-ref tag
unpack-objects update-index update-ref var verify-commit verify-tag version whatchanged worktree write-tree
diff-index diff-files lfs bisect check-ref-format""".split())

#: commands that need the target to be a task worktree on a work branch (denied outright in the main clone)
_GATED = {"commit", "merge", "rebase", "reset", "cherry-pick", "revert", "am", "pull", "stash", "clean", "restore",
          "add", "rm", "mv", "apply", "update-index", "read-tree", "checkout-index", "sparse-checkout"}
_ALWAYS_DENIED = {
    "send-pack": "git send-pack publishes commits",
    "update-ref": "git update-ref writes refs directly",
    "filter-branch": "git filter-branch rewrites history",
    "filter-repo": "git filter-repo rewrites history",
    "replace": "git replace changes what history shows",
    "switch": "git switch moves the checkout to another branch",
    "fast-import": "git fast-import writes objects and refs directly",
    "send-email": "git send-email sends commits out of the studio as mail",
    "imap-send": "git imap-send sends commits out of the studio to a mail server",
    "daemon": "git daemon serves the repository to the network",
}

# ---- the CLOSED table: every subcommand open in an attached studio. A subcommand not here is denied. ----
#: reads (their option rules below still apply)
_READS = set("""status diff log show blame annotate grep cat-file rev-parse rev-list shortlog describe name-rev
merge-base show-ref for-each-ref ls-files ls-tree diff-tree diff-index diff-files check-ignore check-attr
count-objects cherry show-branch whatchanged var version help verify-commit verify-tag format-patch range-diff
merge-tree hash-object fsck check-ref-format fetch ls-remote archive""".split())
#: object writers that move no ref (not gated, so `git commit-tree HEAD^{tree} -m x` stays open in the clone)
_OBJECT_WRITERS = {"commit-tree", "write-tree", "mktree"}
#: repository upkeep and new repositories
_UPKEEP = {"init", "clone", "gc", "prune", "repack"}
#: commands with read forms judged one by one in `_git_reason`
_FORMS = {"remote", "config", "worktree", "branch", "tag", "stash", "notes", "symbolic-ref", "reflog", "submodule",
          "lfs", "bisect", "clean", "checkout", "maintenance"}
_LFS_READ = {"ls-files", "status", "env", "version", "locks", "pull", "fetch"}

# ---- the OPEN table: a call a program adds words to (xargs, parallel, find -exec, watch, ...). ----
#: added words may be anything
_OPEN_ANY = set("""status ls-files ls-tree cat-file rev-parse check-ignore check-attr count-objects show-ref
for-each-ref name-rev merge-base describe cherry add rm mv restore""".split())
#: added words are allowed only after a `--` the text shows (they can be options otherwise: `--output=`)
_OPEN_DASHDASH = {"log", "show", "diff", "diff-tree", "shortlog", "blame", "grep", "rev-list"}

#: options that make a fetch-like command run a program of the caller's choosing
_RUNS_PROGRAM = {"--upload-pack", "--receive-pack", "--exec"}
_FETCH_LIKE = {"fetch", "pull", "clone", "ls-remote", "archive"}
#: commands that write a file of the caller's choosing with --output (t-651.logs/fix-r3-open-options.log)
_OUTPUT_WRITERS = {"annotate", "archive", "blame", "diff", "diff-files", "diff-index", "diff-tree", "format-patch",
                   "log", "range-diff", "rev-list", "shortlog", "show", "whatchanged"}
_BRANCH_SHORT = set("dDmMcCfu")
_BRANCH_LONG = {"--delete", "--move", "--copy", "--force", "--set-upstream-to", "--unset-upstream",
                "--edit-description", "--set-upstream", "--track", "--no-track", "--create-reflog"}
_CONFIG_LONG = {"--global", "--system", "--local", "--worktree", "--file", "--blob", "--get", "--get-all",
                "--get-regexp", "--get-urlmatch", "--replace-all", "--add", "--unset", "--unset-all",
                "--rename-section", "--remove-section", "--list", "--fixed-value", "--edit", "--get-color",
                "--get-colorbool", "--type", "--bool", "--int", "--bool-or-int", "--bool-or-str", "--path",
                "--expiry-date", "--null", "--name-only", "--includes", "--no-includes", "--show-origin",
                "--show-scope", "--default", "--comment", "--no-type", "--value", "--url", "--all", "--regexp",
                "--no-value", "--no-show-origin", "--no-show-scope", "--no-name-only", "--no-null"}
_CONFIG_READ = {"--get", "--get-all", "--get-regexp", "--list", "-l", "--get-urlmatch", "--get-color",
                "--get-colorbool"}
_CONFIG_WRITE = {"--add", "--unset", "--unset-all", "--replace-all", "--edit", "-e", "--remove-section",
                 "--rename-section"}
_TAG_WRITE_SHORT = set("asufdmFe")
_TAG_WRITE_LONG = {"--annotate", "--sign", "--local-user", "--force", "--delete", "--message", "--file", "--edit",
                   "--trailer", "--create-reflog", "--cleanup"}
#: short options whose value is the next word (so a message such as `-m '--x'` is not read as an option)
_SHORT_VALUE = {"commit": "mFCct", "tag": "mFu", "merge": "mFsX", "rebase": "xsX", "cherry-pick": "mX",
                "revert": "mX", "clone": "obucj", "fetch": "jo", "pull": "sXjo", "format-patch": "ov", "archive": "o",
                "branch": "u", "ls-remote": "o"}

#: gh groups and the verbs that write to the remote or run code (None: every verb)
_GH_WRITE = {
    "pr": {"create", "merge", "close", "reopen", "edit", "ready", "review", "comment", "update-branch", "lock",
           "unlock"},
    "issue": {"create", "edit", "close", "reopen", "comment", "delete", "transfer", "lock", "unlock", "pin", "unpin"},
    "repo": {"delete", "edit", "rename", "sync", "archive", "unarchive", "create", "fork", "deploy-key"},
    "release": None,
    "workflow": {"run", "enable", "disable"},
    "run": {"rerun", "cancel", "delete"},
    "alias": {"set", "delete", "import"},
    "extension": {"install", "exec", "upgrade", "remove"},
    "secret": {"set", "delete", "remove"},
    "variable": {"set", "delete", "remove"},
    "ssh-key": {"add", "delete"},
    "gpg-key": {"add", "delete"},
    "label": {"create", "edit", "delete", "clone"},
    "gist": {"create", "edit", "delete", "rename"},
}

#: gh outside `api` is read-only only for these verbs (an allow-list, so a new or unlisted write verb is denied)
_GH_READ_VERBS = {"view", "list", "ls", "status", "diff", "checks", "watch", "download", "clone", "get", "check",
                  "verify", "search", "browse"}
#: options of `gh api` (and the options before the group) whose value is text: a filter, a header, a template
_GH_API_VALUE = {"-q", "--jq", "-t", "--template", "-H", "--header", "--hostname", "-R", "--repo", "--cache", "-p",
                 "--preview"}
#: gh commands with no remote write at all, whatever word follows
_GH_READ_GROUPS = {"", "search", "help", "version", "status", "browse", "completion"}


# ------------------------------------------------------------------------------------ studios and paths

def _within(path, folder) -> bool:
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def studios_of(path) -> list:
    """Real paths of every attach studio that holds `path`: a folder above it with `.alpaca/attach/state.json`.
    One stat per folder above the path; nothing else is read."""
    out = []
    cur = os.path.realpath(path)
    while True:
        if os.path.isfile(os.path.join(cur, STATE_DIR, "state.json")):
            out.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            return out
        cur = parent


def _view(studio):
    """(real path of the attached clone, work branch prefix). A state file or block that cannot be read
    falls back to the defaults, so a broken file never switches the guard off."""
    try:
        view = config.guard_view(studio)
    except Exception:
        view = None
    if view is None:
        return os.path.join(studio, config.DEFAULTS["repo"]), config.DEFAULTS["branch_prefix"]
    return os.path.realpath(view[0]), view[1]


def _git_info(real):
    """(real toplevel, real git common dir) of the repository around `real`, or None. None in a task work
    folder: git there would follow the folder's own `.git` file, which the worker can rewrite (t-648), and
    `classify` reads such a path by its place."""
    if gitops.in_work_folder(real):
        return None
    here = real
    while not os.path.isdir(here):
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent
    try:
        done = gitops.run(here, "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir",
                          timeout=5)
    except Exception:
        return None
    lines = done.out.split("\n")
    if done.rc != 0 or len(lines) < 2 or not lines[0] or not lines[1]:
        return None
    return os.path.realpath(lines[0]), os.path.realpath(lines[1])


def classify(path, studio) -> str:
    """"attached-main" (the clone, or its git folder), "attached-worktree" (a task worktree of the clone,
    or the git folder of one), "studio" (anywhere else in the studio) or "other"."""
    studio = os.path.realpath(studio)
    app, _prefix = _view(studio)
    app_git = os.path.join(app, ".git")
    worktrees = os.path.join(studio, STATE_DIR, "worktrees")
    real = os.path.realpath(path)
    if _within(real, app_git):
        return "attached-worktree" if _within(real, os.path.join(app_git, "worktrees")) else "attached-main"
    if _within(real, app) or _within(real, worktrees):
        if not os.path.exists(real):
            return "attached-main" if _within(real, app) else "attached-worktree"
        info = _git_info(real)
        if info is not None:
            top, common = info
            if top == app:
                return "attached-main"
            if common == app_git:
                return "attached-worktree"
            return "studio"
        return "attached-main" if _within(real, app) else "attached-worktree"
    return "studio" if _within(real, studio) else "other"


def _branch_of(path, studio):
    """The work branch checked out at `path`, or None. Only `proven.checkout_at` answers (op-062 t-648, class
    review r2: work-branch facts come from one verified reader): the path must be in the proved work folder
    of a task (or its admin folder), on `<prefix><task>`. A lookup that fails is None, so the gate denies."""
    try:
        found = proven.checkout_at(os.path.realpath(studio), path)
    except Exception:
        return None
    return found["branch"] if found else None


# ------------------------------------------------------------------------------------ reading a command

class _Call:
    """One command found in the text. kind: git, gh, inline (interpreter or shell code), code (awk or sed
    program text) or unresolved (a program word or wrapper the guard cannot read). state: closed, open or
    unresolved (git calls; `why` says what could not be read). `piped`: inline code that is the whole command
    text because the shell or interpreter reads it from a pipe, xargs or a runner."""

    def __init__(self, kind, here, argv=(), dirs=(), configs=(), exec_path=False, sub=None, code="", env=None,
                 state="closed", why=None, piped=False):
        self.kind, self.here, self.argv = kind, here, list(argv)
        self.dirs, self.configs, self.exec_path, self.sub, self.code = list(dirs), list(configs), exec_path, sub, code
        self.env = dict(env or {})
        self.state, self.why, self.piped = state, why, piped
        self.runtime = []                        # indexes of words built at run time
        self.split = []                          # indexes of those the shell can cut into several words

    @property
    def args(self):
        return self.argv


def _expand(token, env, here=None):
    """`token` with $VAR taken from the command's own assignments, then the process environment, and ~ expanded;
    None when a variable or a command substitution stays unresolved. $PWD is the folder the command has reached
    (`here`), not the hook's own; $OLDPWD is never known."""
    def value(match):
        name = match.group(1) or match.group(2)
        if name in (_MARK[1:], _SPLIT[1:]):
            return match.group(0)
        if name == "PWD" and name not in env:
            return here if here is not None else match.group(0)
        if name == "OLDPWD" and name not in env:
            return match.group(0)
        found = env.get(name, os.environ.get(name))
        return found if found is not None else match.group(0)
    out = os.path.expanduser(_VAR.sub(value, token))
    return None if "$" in out or "`" in out else out


def _marked(text):
    """True when `text` holds the word of a lifted `$(...)` or backtick substitution."""
    return _MARK in text or _SPLIT in text


def _show(text):
    """`text` as the agent should read it: `$(...)` for every lifted substitution."""
    return _MARK_RE.sub("$(...)", text)


def _run_time(word, env, here):
    """True when part of the word is built at run time: a substitution, or a variable the text does not give.
    A `$(` or backtick that is still in the word was single-quoted (`_lift` leaves no other), so it is text."""
    if _marked(word):
        return True
    if "$" not in word and "`" not in word:
        return False
    return _expand(re.sub(r"\$\(|`", "", word), env, here) is None


def _splits(word, env, here):
    """True when the shell can cut the word into more than one: a substitution outside quotes, or a variable
    the text sets to a run-time value or to text with a blank in it. A variable the text never sets and the
    process does not hold is empty at run time, and a double-quoted substitution is one word. After `shlex`
    the quotes of a variable are gone, so a variable that holds a blank counts even when it was quoted."""
    if _SPLIT in word:
        return True
    for found in _VAR.finditer(word):
        name = found.group(1) or found.group(2)
        if name in (_MARK[1:], _SPLIT[1:]) or (name in ("PWD", "OLDPWD") and name not in env):
            continue
        value = env.get(name, os.environ.get(name))
        if value is not None and (_marked(value) or _BLANK.search(value)):
            return True
    return False


def _long_value_part(word):
    """True for `--name=value` with the whole name in the text: a quoted substitution in the value cannot make
    the word another option."""
    return re.match(r"--[A-Za-z][A-Za-z0-9-]*=", word) is not None


def _runtime_arg(word, env, here):
    """True when an argument word of a git call is built at run time in a way that could make it an option.
    Not so: a double-quoted substitution in the value of a long option whose name is in the text, which is one
    word and cannot rename the option."""
    if not _run_time(word, env, here):
        return False
    if _long_value_part(word) and _SPLIT not in word and not _run_time(_MARK_RE.sub("", word), env, here):
        return False
    return True


def _join(here, token, env):
    """`token` as a path from `here`; None when it cannot be known."""
    part = _expand(token, env, here)
    if part is None or here is None:
        return None
    return os.path.normpath(os.path.join(here, part))


_HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|(\\?)([A-Za-z_][A-Za-z0-9_]*))")


def _heredoc_at(text, i):
    """(delimiter, quoted, strip tabs, index after the operator) for a here-document at `i`, or None. A quoted
    delimiter keeps the body literal; an unquoted one lets the shell run the `$(...)` in it."""
    if not text.startswith("<<", i) or text.startswith("<<<", i):
        return None
    m = _HEREDOC.match(text, i)
    if not m:
        return None
    if m.group(5) is not None:
        return m.group(5), bool(m.group(4)), bool(m.group(1)), m.end()
    delim = m.group(2) if m.group(2) is not None else m.group(3)
    return delim, True, bool(m.group(1)), m.end()


def _skip_bodies(text, i, pending):
    """`i` is just after the newline that ends a line with here-documents: (index after their bodies,
    [(body, quoted)])."""
    bodies = []
    for delim, quoted, tabs in pending:
        lines = []
        while i < len(text):
            j = text.find("\n", i)
            j = len(text) if j < 0 else j
            line, i = text[i:j], min(j + 1, len(text))
            if (line.lstrip("\t") if tabs else line) == delim:
                break
            lines.append(line)
        bodies.append(("\n".join(lines), quoted))
    return i, bodies


def _body_substs(body):
    """The `$(...)` and backtick texts in an unquoted here-document body (quotes there are plain text)."""
    out, i = [], 0
    while i < len(body):
        if body.startswith("$(", i):
            end = _closing(body, i + 2)
            out.append(body[i + 2:end])
            i = end + 1
        elif body[i] == "`":
            j = body.find("`", i + 1)
            if j < 0:
                raise ValueError("an unclosed backtick")
            out.append(body[i + 1:j])
            i = j + 1
        elif body[i] == "\\":
            i += 2
        else:
            i += 1
    return out


def _closing(text, start):
    """The index of the `)` that closes a `$(` whose body starts at `start`. Here-document bodies inside are
    skipped. Raises ValueError when none closes it."""
    depth, i, quote, pending = 1, start, None, []
    while i < len(text):
        c = text[i]
        if quote == "'":
            quote = None if c == "'" else quote
        elif c == "\\":
            i += 1
        elif quote == '"':
            if c == '"':
                quote = None
            elif text.startswith("$(", i):
                depth, i = depth + 1, i + 1
        elif c in "'\"":
            quote = c
        elif c == "<" and _heredoc_at(text, i):
            delim, quoted, tabs, end = _heredoc_at(text, i)
            pending.append((delim, quoted, tabs))
            i = end
            continue
        elif c == "\n" and pending:
            i, _bodies = _skip_bodies(text, i + 1, pending)
            pending = []
            continue
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("an unclosed $(")


def _lift(text):
    """(text with every `$(...)`, `<(...)`, `>(...)` and backtick substitution replaced by the marker word,
    [the texts inside them]). Single quotes keep their content. Here-document bodies leave the text (the
    `<<DELIM` word stays, so a shell or interpreter fed by one is still seen); the substitutions in an unquoted
    body are lifted too. Raises ValueError for an unclosed substitution."""
    out, inner, i, quote, pending = [], [], 0, None, []
    while i < len(text):
        c = text[i]
        if quote is None and c == "<" and _heredoc_at(text, i):
            delim, quoted, tabs, end = _heredoc_at(text, i)
            pending.append((delim, quoted, tabs))
            out.append("<<" + delim)
            i = end
            continue
        if quote is None and c == "\n" and pending:
            out.append("\n")
            i, bodies = _skip_bodies(text, i + 1, pending)
            pending = []
            for body, quoted in bodies:
                if not quoted:
                    inner += _body_substs(body)
            continue
        if quote == "'":
            out.append(c)
            quote = None if c == "'" else quote
            i += 1
            continue
        if c == "\\" and i + 1 < len(text):
            out.append(text[i:i + 2])
            i += 2
            continue
        if c == "'" and quote is None:
            quote = "'"
        elif c == '"':
            quote = None if quote == '"' else '"'
        elif c in "$<>" and text.startswith("(", i + 1) and not (c in "<>" and quote):
            end = _closing(text, i + 2)
            inner.append(text[i + 2:end])
            out.append(_MARK if quote or _ONE_WORD.fullmatch(text[i + 2:end]) else _SPLIT)
            i = end + 1
            continue
        elif c == "`":
            j = i + 1
            while j < len(text) and text[j] != "`":
                j += 2 if text[j] == "\\" else 1
            if j >= len(text):
                raise ValueError("an unclosed backtick")
            body = "`".join(text[i + 1:j].split("\\`"))                 # \` inside is a backtick
            inner.append(body)
            out.append(_MARK if quote or _ONE_WORD.fullmatch(body) else _SPLIT)
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out), inner


def _segments(command):
    from alpaca.hooks import git_guard
    return list(git_guard._segments(re.sub("`", ";", command)))


def _skip_opts(words, with_value=()):
    i = 0
    while i < len(words) and words[i].startswith("-") and words[i] != "--":
        i += 2 if words[i] in with_value and i + 1 < len(words) else 1
    if i < len(words) and words[i] == "--":
        i += 1
    return words[i:]


def _wrapper(base, words, env, here):
    """Take one wrapper (sudo, nice, timeout, ...) off the front: (words, here, why). `why` is set when an
    option is not one the guard understands, so the call it carries cannot be known."""
    flags, valued, drop = _WRAPPERS[base]
    words = words[1:]
    while words and words[0].startswith("-") and words[0] != "-":
        w = words[0]
        name, eq, inline = w.partition("=")
        if w == "--":
            words = words[1:]
            break
        if w in flags or (base == "nice" and re.fullmatch(r"-\d+", w)):
            words = words[1:]
        elif name in valued and eq:
            value, words = inline, words[1:]
            if name in ("-D", "--chdir"):
                here = _join(here, value, env)
        elif w in valued and len(words) > 1:
            value, words = words[1], words[2:]
            if w in ("-D", "--chdir"):
                here = _join(here, value, env)
        elif w[:2] in valued and not w.startswith("--") and len(w) > 2:
            if w[:2] == "-D":
                here = _join(here, w[2:], env)
            words = words[1:]
        else:
            return [], here, "%s option %s is not one the attach guard reads" % (base, w)
    return words[drop:], here, None


def _strip_front(words, env, here):
    """Take assignments, shell keywords and wrappers (env, sudo, nice, timeout, nohup, xargs, ...) off the front
    of a segment. Returns (words, env, here, carrier); a segment that only assigns comes back empty. `here` is
    where the carried command runs (`env -C`, `sudo -D`). carrier: {"open": a program adds words (xargs),
    "places": its placeholder strings, "why": a wrapper option the guard cannot read}."""
    env = dict(env)
    words = list(words)
    car = {"open": False, "places": set(), "why": None}
    while words:
        tok = words[0]
        found = _ASSIGN.match(tok)
        base = os.path.basename(tok)
        if found and not tok.startswith(("-", "=")):
            env[found.group(1)] = found.group(2)
            words.pop(0)
        elif tok in _KEYWORDS:
            words.pop(0)
        elif base in ("export", "declare", "typeset", "local", "readonly"):
            for word in words[1:]:
                got = _ASSIGN.match(word)
                if got:
                    env[got.group(1)] = got.group(2)
            return [], env, here, car
        elif base == "env":
            words = words[1:]
            while words:
                w = words[0]
                name, eq, inline = w.partition("=")
                if w in ("-u", "--unset") and len(words) > 1:
                    env.pop(words[1], None)
                    words = words[2:]
                elif name == "--unset" and eq:
                    env.pop(inline, None)
                    words = words[1:]
                elif w in ("-C", "--chdir") and len(words) > 1:
                    here, words = _join(here, words[1], env), words[2:]
                elif name == "--chdir" and eq:
                    here, words = _join(here, inline, env), words[1:]
                elif w.startswith("-C") and len(w) > 2:
                    here, words = _join(here, w[2:], env), words[1:]
                elif w in ("-S", "--split-string") and len(words) > 1:
                    words = shlex.split(words[1]) + words[2:]
                elif name == "--split-string" and eq:
                    words = shlex.split(inline) + words[1:]
                elif w.startswith("-S") and len(w) > 2:
                    words = shlex.split(w[2:]) + words[1:]
                elif w in ("-i", "--ignore-environment", "-", "-0", "--null"):
                    words = words[1:]
                elif w == "--":
                    words = words[1:]
                    break
                elif w.startswith("-"):
                    car["why"] = "env option %s is not one the attach guard reads" % w
                    return [], env, here, car
                elif _ASSIGN.match(w):
                    got = _ASSIGN.match(w)
                    env[got.group(1)] = got.group(2)
                    words = words[1:]
                else:
                    break
        elif base == "xargs":
            car["open"] = True
            words = words[1:]
            while words and words[0].startswith("-") and words[0] != "--":
                w = words[0]
                name, eq, inline = w.partition("=")
                if w == "-I" and len(words) > 1:
                    car["places"].add(words[1])
                    words = words[2:]
                elif w.startswith("-I"):
                    car["places"].add(w[2:])
                    words = words[1:]
                elif w == "-i" or name == "--replace":
                    car["places"].add(inline if eq else "{}")
                    words = words[1:]
                elif w in _XARGS_VALUE and len(words) > 1:
                    words = words[2:]
                else:
                    words = words[1:]
            if words and words[0] == "--":
                words = words[1:]
        elif base in _WRAPPERS:
            words, here, why = _wrapper(base, words, env, here)
            if why:
                car["why"] = why
                return [], env, here, car
        else:
            break
    return words, env, here, car


def _strip(words, env, here):
    """`_strip_front`, and a carried call whose wrapper took a word the shell can cut into several is unresolved:
    `timeout 5$(printf ' git push')`, `env FOO=$(...) git`, `nice -n $X`. The assignments at the very front are
    the shell's own (`FOO=$(...) git status`) and are not cut."""
    words = list(words)
    out, env, at, car = _strip_front(words, env, here)
    if out and not car["why"]:
        n = len(words) - len(out)
        taken = words[:n] if n >= 0 and words[n:] == out else words
        lead = 0
        while lead < len(taken) and _ASSIGN.match(taken[lead]) and not taken[lead].startswith(("-", "=")):
            lead += 1
        cut = next((w for w in taken[lead:] if _splits(w, env, at)), None)
        if cut is not None:
            car["why"] = ("the word %s before the program holds a part built at run time that the shell can cut "
                          "into more words; put it in double quotes" % cut)
    return out, env, at, car


def _program(word, env, here):
    """(base name, None) for a program word; (None, None) when the word is built at run time; (None, text)
    when a variable holds a whole command (`X='git push'; $X`). A literal last component behind a run-time
    prefix (`$(git rev-parse --show-toplevel)/bin/tool`) is that component."""
    exp = _expand(word, env, here)
    if exp is None:
        head, _slash, last = word.rpartition("/")
        if last and "$" not in last and "`" not in last and not _splits(head, env, here):
            return last, None
        return None, None
    if "$" in word and re.search(r"\s", exp.strip()):
        return None, exp
    return os.path.basename(exp), None


def _git_call(words, prog, here, env, car):
    """Read a git command: the folders it works in, the -c keys, the subcommand and its words, and its state.
    `prog` is the program word's base name (`git`, or `git-X` for subcommand X)."""
    cur = here
    why = None
    sets_target = "GIT_DIR" in env or "GIT_WORK_TREE" in env
    git_dir = env.get("GIT_DIR") if sets_target else os.environ.get("GIT_DIR")
    work_tree = env.get("GIT_WORK_TREE") if sets_target else os.environ.get("GIT_WORK_TREE")
    extra = [env.get(n, os.environ.get(n)) for n in ("GIT_COMMON_DIR", "GIT_INDEX_FILE")]
    configs, exec_path = [], False
    i = 1
    if prog.startswith("git-"):
        sub, rest = prog[4:], words[1:]
    else:
        while i < len(words) and words[i].startswith("-"):
            opt = words[i]
            name, eq, inline = opt.partition("=")
            value, step = None, 1
            if _splits(opt, env, here):
                why = why or ("the git option %s holds a part built at run time that the shell can cut into more "
                              "words; put it in double quotes" % opt)
            if eq and name.startswith("--"):
                value = inline
            elif opt in _GIT_VALUE_OPTS:
                if i + 1 >= len(words):
                    why = why or "git option %s has no value in the command text" % opt
                    break
                value, step = words[i + 1], 2
                name = opt
                if _splits(value, env, here):
                    why = why or ("the value %s of git option %s holds a part built at run time that the shell "
                                  "can cut into more words; put it in double quotes" % (value, opt))
            if name not in _GIT_VALUE_OPTS and name not in _GIT_FLAGS:
                why = why or "git option %s is not one the attach guard reads" % opt
            elif name == "-C" and value is not None:
                cur = _join(cur, value, env)
            elif name in ("-c", "--config-env") and value is not None:
                key, _eq, val = value.partition("=")
                exp = _expand(key, env, here)
                if exp is None:
                    why = why or "git %s %s has a key built at run time" % (name, value)
                configs.append((exp if exp is not None else key, val, name))
            elif name == "--git-dir" and value is not None:
                git_dir = value
            elif name == "--work-tree" and value is not None:
                work_tree = value
            elif name == "--exec-path":
                exec_path = True
            i += step
        sub = words[i] if i < len(words) else None
        rest = words[i + 1:] if sub is not None else []
    if sub is not None:
        exp = _expand(sub, env, here)
        if exp is None or not _SUBWORD.fullmatch(exp) or sub in car["places"]:
            why = why or "the git subcommand %s is not in the command text" % sub
        else:
            sub = exp
    # the words after the subcommand, with variables the text sets expanded (and split, as the shell does). A
    # word with a part the text does not give, anywhere in it, could be any option or several; `_git_reason`
    # judges them, and a `--` that ends the options excuses the ones behind it.
    args, runtime, split = [], [], []
    for word in rest:
        got = _expand(word, env, here) if "$" in word else word
        if got is None:
            if _runtime_arg(word, env, here):
                runtime.append(len(args))
                if _splits(word, env, here):
                    split.append(len(args))
            args.append(word)
        elif got != word:
            args.extend(got.split() or [""])
        else:
            args.append(word)
    rest = args
    dirs =[_join(cur, given, env) for given in (git_dir, work_tree) if given]
    if not dirs:
        dirs = [cur]
    dirs += [_join(cur, given, env) for given in extra if given]
    state = "unresolved" if why else ("open" if car["open"] else "closed")
    call = _Call("git", here, rest, dirs=dirs, configs=configs, exec_path=exec_path, sub=sub, env=env, state=state,
                 why=why)
    call.runtime, call.split = runtime, split
    return call


def _shell_text(words):
    """(the -c text or None, True when a -c flag is present) for a shell's words."""
    for i, word in enumerate(words[1:], start=1):
        name, eq, inline = word.partition("=")
        if name == "--command" and eq:
            return inline, True
        if word == "--command" or (word.startswith("-") and not word.startswith("--") and "c" in word[1:]):
            return (words[i + 1] if i + 1 < len(words) else None), True
    return None, False


def _make_targets_only(args, env):
    """True when a `make` call holds only the flags and target names it is safe to read as names: no `-f`,
    `--eval`, `NAME=value` or other option, and no MAKEFLAGS-like variable from the text."""
    if any(name in env for name in _MAKE_ENV):
        return False
    skip = False
    for word in args:
        if skip:
            skip = False
        elif word in ("-C", "--directory"):
            skip = True
        elif word.startswith("--directory=") or (word.startswith("-C") and len(word) > 2):
            continue
        elif word.startswith("-"):
            if not _MAKE_FLAG.fullmatch(word):
                return False
        elif not _MAKE_TARGET.fullmatch(word):
            return False
    return not skip


def _arg_only(prog, words, env, car):
    """True when the guard reads the program's words as names and patterns, so a `git` word among them is no
    call (t-676): `pytest -k git`, `make git-hooks`. A program that xargs adds words to is not read this way
    for make, whose added words could be `-f -` or `--eval=`. What pytest or make loads or runs from disk
    (`-p`, `--pyargs`, conftest files, test files, make recipes, `--basetemp` removing its folder) is not read
    here: that is the program-on-disk residue the module docstring states, owned by t-654."""
    if prog in _ARG_ONLY:
        return True
    return prog == "make" and not car["open"] and _make_targets_only(words[1:], env)


def _runner(words, here, env, depth, out, command, car):
    """A program that runs its words as a command (watch, find -exec, parallel, flock, strace, ...): the git or
    gh call among them is open (the program can add words); a shell or interpreter among them reads text the
    guard does not see; a word that holds a git or gh word and shell syntax is read as command text."""
    lead, _value = _program(words[0], env, here)
    for k, word in enumerate(words[1:], start=1):
        base, _value = _program(word, env, here)
        base = base or ""
        if (base == "git" or base.startswith("git-") or base == "gh") and any(
                _splits(w, env, here) for w in words[1:k]):
            out.append(_Call("unresolved", here, why="a program runs git or gh and a word before it holds a part "
                             "built at run time that the shell can cut into more words"))
            return
        if base == "git" or base.startswith("git-"):
            # watch, flock and strace run the words they are given and add none: with plain words, which the
            # shell they use cannot read again as more words or an assignment, the call is closed (t-676)
            adds = not (lead in _NO_ADDED_WORDS and not car["open"] and all(_PLAIN_WORD.fullmatch(w) for w in words[1:])
                        and not any(_ASSIGN.match(w) for w in words[1:k]))
            # a NAME=value among the words before the git word is the shell's assignment for it (GIT_DIR=...)
            given = dict(env)
            for w in words[1:k]:
                got = _ASSIGN.match(w)
                if got and not w.startswith(("-", "=")):
                    given[got.group(1)] = got.group(2)
            out.append(_git_call(words[k:], base, here, given, {"open": adds, "places": set(), "why": None}))
            return
        if base == "gh":
            out.append(_Call("gh", here, words[k + 1:], state="open"))
            return
        if base in _SHELLS or _INTERPRETER.match(base):
            out.append(_Call("inline", here, code=command, piped=True))
            return
    for word in words[1:]:
        if _TOKEN.search(word) and re.search(r"[\s;&|<>()$`]", word):
            try:
                more = _calls(word, here, env, depth + 1, [])
            except ValueError:
                more = []
            if any(c.kind in ("git", "gh") for c in more):
                out.extend(more)
            else:
                out.append(_Call("unresolved", here, why="a program runs a text that mentions git or gh and "
                                 "cannot be read"))
            return


def _calls(command, here, env, depth, out):
    """Collect every git, gh, inline, code and unresolved call in `command` into `out`. Raises ValueError for
    text the shell lexer cannot read, or nested too deep."""
    if depth > MAX_DEPTH:
        raise ValueError("nested too deep")
    outer, inners = _lift(command)
    for text in inners:
        _calls(text, here, env, depth + 1, out)
    for seg in _segments(outer):
        words, env, at, car = _strip(seg, env, here)
        if car["why"]:
            out.append(_Call("unresolved", at, why=car["why"]))
            continue
        if not words:
            continue
        prog, value = _program(words[0], env, at)
        if value is not None:
            _calls(" ".join([value] + [shlex.quote(w) for w in words[1:]]), at, env, depth + 1, out)
            continue
        if prog is None:
            out.append(_Call("unresolved", at, why="the program word %s is built at run time" % words[0]))
            continue
        if prog in ("cd", "pushd", "popd"):
            target = next((w for w in words[1:] if not w.startswith("-") or w == "-"), None)
            if prog == "popd" or target == "-" or (prog == "pushd" and target is None):
                here = None                          # the folder before or on the stack is not in the text
            elif target is not None:
                here = _join(here, target, env)
            else:
                here = os.path.expanduser("~")
        elif prog in _SHELLS:
            text, flag = _shell_text(words)
            script = any(not w.startswith(("-", "<")) for w in words[1:])
            fed = any(w.startswith("<<") for w in words[1:])
            cut = any(_splits(w, env, at) for w in words[1:])
            if car["open"] or cut or (flag and (text is None or _marked(text))) or (not flag and (fed or not script)):
                # the shell's text comes from a pipe, from xargs or from a run-time word, or a word the shell
                # cuts may add a flag such as -c: judge the whole text
                out.append(_Call("inline", at, code=command, piped=True))
            elif flag:
                _calls(text, at, env, depth + 1, out)
        elif prog == "eval":
            _calls(" ".join(words[1:]), at, env, depth + 1, out)
        elif prog == "git" or prog.startswith("git-"):
            out.append(_git_call(words, prog, at, env, car))
        elif prog == "gh":
            out.append(_Call("gh", at, words[1:], state="open" if car["open"] else "closed"))
        elif _INTERPRETER.match(prog):
            flagged = [i for i, w in enumerate(words[1:], start=1) if w in _INLINE_FLAGS]
            code = None
            if flagged and flagged[0] + 1 < len(words) and not car["open"]:
                code = flagged[0] + 1
                out.append(_Call("inline", at, code=words[code]))
            elif (car["open"] or flagged or any(w == "-" or w.startswith("<<") for w in words[1:])
                  or not any(not w.startswith(("-", "<")) for w in words[1:])):
                out.append(_Call("inline", at, code=command, piped=True))
            # an argument that holds a git or gh word is judged as code too: a flag the list above does not
            # know (`pwsh -Co`, an abbreviation) can still make it the program text
            for n, word in enumerate(words[1:], start=1):
                if n != code and _TOKEN.search(word):
                    out.append(_Call("inline", at, code=word))
        elif prog in _CODE_TOOLS:
            out.append(_Call("code", at, words[1:]))
        elif _arg_only(prog, words, env, car):
            pass
        elif prog not in _PRINTERS:
            _runner(words, at, env, depth, out, command, car)
    return out


# ------------------------------------------------------------------------------------ the rules

def _bad_config(key) -> bool:
    low = key.lower()
    return any(low.startswith(bad) for bad in _BAD_CONFIG) or bool(_BAD_CONFIG_RE.match(low))


def _inert_config(key, value) -> bool:
    low = key.lower()
    if low == "commit.gpgsign":
        return value.lower() in ("false", "no", "off", "0")
    return low in _INERT_CONFIG or low.startswith(_INERT_CONFIG_PREFIX)


def _env_reason(env):
    """Why a variable the command sets is denied for a git call against an attached target, or None."""
    for name in sorted(env):
        value = env[name]
        if name in _BAD_ENV or name.startswith(_BAD_ENV_PREFIX):
            return ("the variable %s set for git changes its config, the programs it runs or the secret it uses"
                    % name)
        if name.startswith("GIT_TRACE"):
            if value.strip().lower() not in _GIT_TRACE_VALUES:
                return "the variable %s=%s makes git write a file the command picks" % (name, value)
        elif name.startswith("GIT_") and name not in _GIT_ENV_TARGETS and name not in _GIT_ENV_INERT:
            return "the variable %s is not one of the git settings open in an attached studio" % name
        elif name == "PATH" and _PATH_APPEND.fullmatch(value):
            continue
        elif name in _ENV_DENY:
            return "the variable %s set for git names a program, a startup file or where git finds its config" % name
        elif name in ("PAGER", "GIT_PAGER") and value not in _PAGERS:
            return "the variable PAGER=%s names a program git runs" % value
    return None


def _positional(args):
    return [a for a in args if not a.startswith("-")]


def _short_letters(args):
    return {c for a in args if a.startswith("-") and not a.startswith("--") for c in a[1:]}


def _resolve(name, options):
    """The long option `name` (before any `=`) stands for: itself, or the one option it is a prefix of; None
    when it is no option or the prefix of more than one (git refuses those, so they are not read here)."""
    if name in options:
        return name
    found = [o for o in options if o.startswith(name)]
    return found[0] if len(found) == 1 else None


def _resolve_opts(sub, args):
    """(set of the resolved long options and short flag words, the first word that could not be resolved or
    None) for a subcommand in gitopts.LONG. Stops at `--`; skips the values of options that take one."""
    longs, takes = gitopts.LONG[sub], gitopts.TAKES_VALUE[sub]
    short_value = _SHORT_VALUE.get(sub, "")
    out, skip = set(), False
    for word in args:
        if skip:
            skip = False
            continue
        if word == "--":
            break
        if word.startswith("--"):
            name, eq, _v = word.partition("=")
            got = _resolve(name, longs)
            if got is None:
                return out, word
            out.add(got)
            skip = got in takes and not eq
        elif word.startswith("-") and len(word) > 1:
            out.add(word)
            for n, letter in enumerate(word[1:], start=1):
                if letter in short_value:
                    skip = n == len(word) - 1
                    break
    return out, None


#: options of the reads that take the next word as a value (for subcommands gitopts.py does not list)
_READ_VALUE = {"-m", "-F", "-n", "-S", "-G", "-L", "-U", "--format", "--pretty", "--grep", "--author", "--committer",
               "--since", "--until", "--after", "--before", "--max-count", "--skip", "--date", "--unified"}


def _value_positions(sub, args):
    """Indexes of the words that are the value of the option before them, up to the `--` that ends the options.
    A `--` that is an option's value (`git fetch -o -- origin`) ends nothing."""
    longs = gitopts.LONG.get(sub)
    takes = gitopts.TAKES_VALUE.get(sub, frozenset())
    short = _SHORT_VALUE.get(sub, "")
    out = set()
    for n, word in enumerate(args[:-1]):
        if n in out:
            continue
        if word == "--":
            break
        if word.startswith("--") and "=" not in word:
            name = _resolve(word, longs) if longs else word
            if name in takes or (longs is None and word in _READ_VALUE):
                out.add(n + 1)
        elif word.startswith("-") and len(word) > 1:
            if word[-1] in short or (longs is None and word in _READ_VALUE):
                out.add(n + 1)
    return out


def _options_end(args, values):
    """Index of the `--` that ends the options (one that is an option's value does not), or len(args)."""
    return next((n for n, word in enumerate(args) if word == "--" and n not in values), len(args))


def _prefix_of(args, option, shortest):
    """True when a word before `--` is `option` or an abbreviation of it at least `shortest` long."""
    for word in args:
        if word == "--":
            return False
        name = word.partition("=")[0]
        if word.startswith("--") and len(name) >= shortest and option.startswith(name):
            return True
    return False


def _commit_no_verify(args) -> bool:
    """True when a `commit` word list carries -n (alone or in a cluster such as -an or -nm). The message that
    follows -m, -F, -C, -c or -t is a value, not a flag."""
    takes = set("mFCct")
    skip = False
    for word in args:
        if skip:
            skip = False
            continue
        if word == "--":
            return False
        if word.startswith("--") or not word.startswith("-"):
            if word.startswith("--"):
                got = _resolve(word.partition("=")[0], gitopts.LONG["commit"])
                skip = got in gitopts.TAKES_VALUE["commit"] and "=" not in word
            continue
        for n, letter in enumerate(word[1:], start=1):
            if letter == "n":
                return True
            if letter in takes:
                skip = n == len(word) - 1
                break
    return False


def _pre_reason(call):
    """The checks that hold for every git call against an attached target, before its subcommand: -c keys,
    variables, --exec-path, and a call the text does not fix."""
    for key, value, how in call.configs:
        if _bad_config(key):
            return "git -c %s changes where git pushes, which program it runs or which secret it uses" % key
        if not _inert_config(key, value):
            return "git %s %s is not one of the inert settings open in an attached studio" % (how, key)
    bad = _env_reason(call.env)
    if bad:
        return bad
    if call.exec_path:
        return "git --exec-path points git at another set of programs"
    if call.state == "unresolved":
        return call.why or "the git call cannot be read from the command text"
    return None


def _open_reason(call):
    """Why an open git call (a program adds words to it) is denied, or None to judge it by the table."""
    sub, args = call.sub, call.args
    if sub is None:
        return "a program runs git with its subcommand among words the command text does not show"
    if sub in _OPEN_ANY:
        return None
    if sub in _OPEN_DASHDASH:
        shown = args[:next((n for n, a in enumerate(args) if a in (":::", "::::")), len(args))]
        if "--" in shown and not any("{" in a for a in shown[:shown.index("--")]):
            return None
        return ("git %s with words a program adds is open only after a `--` in the command text, so the added "
                "words are paths" % sub)
    return ("git %s with words a program adds (xargs, parallel, find -exec, watch) is not one of the reads open "
            "that way" % sub)


def _git_reason(call, cls, branch, prefix):
    """Why this git call is denied against an attached target, or None when its subcommand is on the table
    and its words pass that entry's rules. `branch` is a function returning the checked-out branch of the
    target."""
    pre = _pre_reason(call)
    if pre:
        return pre
    sub, args = call.sub, call.args
    if call.state == "open":
        reason = _open_reason(call)
        if reason:
            return reason
    if sub is None:
        return None                              # `git`, `git --version`: global options only, all read above
    values = _value_positions(sub, args)
    end = _options_end(args, values)
    for n in call.runtime:
        # after the `--` that ends the options the words are paths; the value of an option is one word unless
        # the shell cuts it; no option of an _OPEN_ANY command writes or runs
        if n > end or (n in values and n not in call.split) or sub in _OPEN_ANY:
            continue
        hint = ""
        if _marked(args[n]):
            hint = ("; for a revision, run the inner git command first and give its output as a literal word, or "
                    "write it as A..B, A...B or `git diff --merge-base A`")
        return ("the git word %s is built at run time and could be an option or several words; put it after `--` "
                "or give it in double quotes as an option's value%s" % (_show(args[n]), hint))
    if _prefix_of(args[:end], "--no-verify", 6):
        return "--no-verify skips the repository's own hooks"
    if sub == "push":
        return "git push from the attached repository; a change leaves the studio only through `alpaca attach handoff`"
    if sub in _ALWAYS_DENIED:
        return _ALWAYS_DENIED[sub]
    opts = set()
    if sub in gitopts.LONG:
        opts, bad = _resolve_opts(sub, args)
        if bad is not None:
            return ("git %s %s is not an option the attach guard can read (unknown, or an abbreviation of more "
                    "than one)" % (sub, bad))
        if "--no-verify" in opts:
            return "--no-verify skips the repository's own hooks"
    behind = args[end + 1:]                      # a short option the table lacks could take the `--` as its value
    if sub in _FETCH_LIKE and (opts & _RUNS_PROGRAM or (sub in ("clone", "ls-remote", "archive") and "u" in _short_letters(args[:end]))
                               or any(w.startswith("--") and _resolve(w.partition("=")[0], gitopts.LONG[sub])
                                      in _RUNS_PROGRAM for w in behind)):
        return "git %s with --upload-pack, --receive-pack or --exec runs a program of the caller's choosing" % sub
    pos = _positional(args)
    if sub in _OUTPUT_WRITERS or (sub == "stash" and pos[:1] == ["show"]):
        shown = args[:args.index("--")] if "--" in args else args
        if _prefix_of(args, "--output", 5) or (sub in ("format-patch", "archive") and (
                "--output-directory" in opts or "o" in _short_letters(shown))):
            return "git %s --output writes a file of the caller's choosing" % sub
    if sub not in _KNOWN and pos[:1] == ["push"]:
        return "git %s push publishes to a remote; a change leaves the studio only through `alpaca attach handoff`" % sub
    if sub in _FORMS:
        return _form_reason(sub, args, pos, opts, cls, branch, prefix)
    if sub == "fetch":
        if "--update-head-ok" in opts or "u" in _short_letters(args[:end]):
            return "git fetch with --update-head-ok can overwrite a checked-out branch"
        return None
    if sub == "commit" and _commit_no_verify(args):
        return "commit -n skips the repository's own hooks"
    if sub in _GATED:
        return _gate(sub, cls, branch, prefix)
    if sub in _READS or sub in _OBJECT_WRITERS or sub in _UPKEEP:
        return None
    return "git %s is not one of the git commands open in an attached studio" % sub


def _gate(sub, cls, branch, prefix):
    if cls == "attached-main":
        return "git %s in the main clone of the attached repository; do the work in a task worktree" % sub
    name = branch()
    if not name or not name.startswith(prefix):
        return ("git %s outside a work branch; a task worktree must be on a branch that starts with %s"
                % (sub, prefix))
    return None


def _form_reason(sub, args, pos, opts, cls, branch, prefix):
    """The commands with read forms: only those forms are open (stash, clean and checkout fall back to the
    work-branch gate)."""
    if sub == "notes":
        return None if pos[:1] in ([], ["list"], ["show"]) else "git notes %s writes a notes ref" % pos[0]
    if sub == "remote":
        if pos and pos[0] not in ("show", "get-url"):
            return "git remote %s changes or contacts a remote" % pos[0]
        return None
    if sub == "config":
        read, write = set(), set()
        for word in args:
            if word.startswith("--"):
                got = _resolve(word.partition("=")[0], _CONFIG_LONG)
                if got is None:
                    return "git config %s is not an option the attach guard can read" % word
                (read if got in _CONFIG_READ else write if got in _CONFIG_WRITE else set()).add(got)
            elif word in ("-l", "-e"):
                (read if word == "-l" else write).add(word)
        if write:
            return "git config %s changes the config" % sorted(write)[0]
        if read or pos[:1] in (["get"], ["list"]):
            return None
        return "git config without a read flag can change the remote, hooks or identity"
    if sub == "worktree":
        return None if pos[:1] == ["list"] else "git worktree changes the set of task worktrees; attach manages them"
    if sub == "branch":
        if (_short_letters(args) & _BRANCH_SHORT) or (opts & _BRANCH_LONG):
            return "git branch with a delete, rename, copy, force or upstream option changes the work branches"
        return None
    if sub == "tag":
        if "-l" in args or "--list" in opts:
            return None
        if pos or (_short_letters(args) & _TAG_WRITE_SHORT) or (opts & _TAG_WRITE_LONG):
            return "git tag creates or removes a tag"
        return None
    if sub == "symbolic-ref":
        if len(pos) >= 2 or "-d" in args or _prefix_of(args, "--delete", 4):
            return "git symbolic-ref with two words writes a ref"
        return None
    if sub == "reflog":
        return "git reflog %s drops history" % pos[0] if pos[:1] in (["expire"], ["delete"]) else None
    if sub == "submodule":
        if pos[:1] in ([], ["status"], ["summary"], ["foreach"]):
            return None
        return "git submodule %s changes the submodules" % pos[0]
    if sub == "lfs":
        if pos[:1] == ["push"]:
            return "git lfs push publishes to a remote; a change leaves the studio only through `alpaca attach handoff`"
        return None if pos[:1] and pos[0] in _LFS_READ else "git lfs %s is not one of the lfs reads" % (
            pos[0] if pos else "")
    if sub == "bisect":
        return None
    if sub == "maintenance":
        return None if pos[:1] == ["run"] else "git maintenance %s changes the user's scheduler or config" % (
            pos[0] if pos else "")
    if sub == "checkout":
        if "--" not in args:
            return "git checkout without -- moves the checkout to another branch"
        return "git checkout of files in the main clone" if cls == "attached-main" else None
    if sub == "stash":
        return None if pos[:1] in (["list"], ["show"]) else _gate(sub, cls, branch, prefix)
    if sub == "clean":
        if {"-n", "--dry-run"} & set(args) or "--dry-run" in opts or "n" in _short_letters(args):
            return None
        return _gate(sub, cls, branch, prefix)
    return None


def _gh_reason(args, state="closed"):
    words, flags, i = [], [], 0
    while i < len(args):
        a = args[i]
        if a in ("-R", "--repo", "--hostname") and i + 1 < len(args):
            i += 2
            continue
        (flags if a.startswith("-") else words).append(a)
        i += 1
    group = words[0] if words else ""
    verb = words[1] if len(words) > 1 else ""
    if state == "open" and (group in ("", "api") or (group not in _GH_READ_GROUPS and verb not in _GH_READ_VERBS)):
        return "a program runs gh with its group or verb among words the command text does not show"
    if group == "api":
        skip = False
        for a in args:                               # a word built at run time can be -X, -f or a body
            if skip:
                skip = False
            elif a in _GH_API_VALUE:
                skip = True
            elif a.partition("=")[0] in _GH_API_VALUE or not _runtime_arg(a, {}, None):
                continue
            else:
                return ("gh api with the word %s built at run time, which could be -X, a field or a body; write "
                        "the method and the fields as literal words" % a)
        method = None
        for n, a in enumerate(args):
            if a in ("-X", "--method") and n + 1 < len(args):
                method = args[n + 1]
            elif a.startswith("--method="):
                method = a.split("=", 1)[1]
            elif a.startswith("-X") and len(a) > 2:
                method = a[2:]
        if method is not None and method.upper() != "GET":
            return "gh api with method %s writes to the remote" % method.upper()
        for a in args:
            if (a in ("-f", "-F") or a.startswith(("-f", "-F")) and not a.startswith("--")
                    or a.startswith(("--field", "--raw-field", "--input"))):
                return "gh api with a field or input sends a request body"
        return None
    if group in _GH_WRITE:
        verbs = _GH_WRITE[group]
        if verbs is None or verb in verbs:
            return "gh %s %s writes to the remote or runs code" % (group, verb)
    if group in _GH_READ_GROUPS or verb in _GH_READ_VERBS:
        return None
    return ("gh %s %s is not one of the read-only gh commands (%s) open while an attached repository is involved"
            % (group, verb, ", ".join(sorted(_GH_READ_VERBS))))


def _inline_reason(code):
    if re.search(r"\bgit\b", code) and re.search(r"push|send-pack", code):
        return "inline code that runs git push"
    if re.search(r"\bgh\b", code) and re.search(r"\b(pr|release|api|repo|workflow)\b", code):
        return "inline code that runs gh against the remote"
    return None


class _Unread(Exception):
    """A lookup the guard needs (a git alias) failed, so the call cannot be judged."""


def _alias(call, path, studio):
    """The text an unknown git word stands for, from the user's own git config, or None. Raises _Unread when
    the lookup itself fails or times out. In a task work folder the lookup runs only in a proved one, with the
    repository named on the command line (`gitops.run_in_worktree`); an unproved folder raises _Unread."""
    if call.sub is None or call.sub in _KNOWN or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", call.sub):
        return None
    from alpaca.watchdog.gitw import LOCAL_ENV
    env = {k: v for k, v in os.environ.items() if k not in LOCAL_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    here = path
    while not os.path.isdir(here):
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent
    try:
        if gitops.in_work_folder(here):
            found = proven.checkout_at(os.path.realpath(studio), here)
            if found is None:
                raise _Unread("the git alias lookup for %s needs a proved task work folder" % call.sub)
            done = gitops.run_in_worktree(found["app"], found["task"], found["folder"], "config", "--get",
                                          "alias." + call.sub, env=env, timeout=5)
        else:
            done = gitops.run(here, "config", "--get", "alias." + call.sub, env=env, timeout=5)
    except _Unread:
        raise
    except Exception as exc:
        raise _Unread("the git alias lookup for %s failed (%s)" % (call.sub, type(exc).__name__))
    if done.rc not in (0, 1):
        raise _Unread("the git alias lookup for %s failed (exit %s)" % (call.sub, done.rc))
    return done.out.strip() or None if done.rc == 0 else None


def _deny(studio, reason):
    return Deny(studio, _show(reason), os.path.join(studio, STATE_DIR, "worktrees"))


def _judge_git(call, ctx, depth=0):
    base = ctx.studios(ctx.cwd)
    best = None                                  # (rank, class, studio, path)
    unknown = any(d is None for d in call.dirs)
    for d in call.dirs:
        if d is None:
            continue
        seen = []
        for studio in ctx.studios(d) + base:
            if studio in seen:
                continue
            seen.append(studio)
            cls = classify(d, studio)
            rank = _RANK.get(cls, 0)
            if rank and (best is None or rank > best[0]):
                best = (rank, cls, studio, d)
    if best is None:
        if not (unknown and base):
            return None
        # the target folder is not in the text, and the session sits in a studio: judge it as the main clone
        best = (2, "attached-main", base[0], ctx.cwd)
    _rank, cls, studio, path = best
    _app, prefix = _view(studio)
    pre = _pre_reason(call)
    if pre:
        return _deny(studio, pre)
    if depth < 3 and call.sub is not None and call.sub not in _KNOWN:
        try:
            text = _alias(call, path, studio)
        except _Unread as exc:
            return _deny(studio, str(exc))
        if text:
            if text.startswith("!"):
                got = _judge_text(text[1:], path, ctx, depth + 1)
                return got if got else None
            words = shlex.split(text)
            if words:
                expanded = _Call("git", call.here, words[1:] + call.args, dirs=call.dirs, configs=call.configs,
                                 exec_path=call.exec_path, sub=words[0], env=call.env, state=call.state)
                shift = len(words) - 1                   # the alias text goes in front of the call's own words
                expanded.runtime = [n + shift for n in call.runtime]
                expanded.split = [n + shift for n in call.split]
                return _judge_git(expanded, ctx, depth + 1)
    reason = _git_reason(call, cls, lambda: _branch_of(path, studio), prefix)
    if reason is None and depth < 3:
        for text in _inner_texts(call):
            got = _judge_text(text, path, ctx, depth + 1)
            if got:
                return got
    if reason is None:
        return None
    return _deny(studio, reason)


def _inner_texts(call):
    """The command texts a git call runs itself: `rebase -x|--exec`, `submodule foreach`, `bisect run`,
    `difftool|mergetool -x|--extcmd`, `grep -O|--open-files-in-pager`."""
    sub, args = call.sub, call.args
    out = []
    if sub in ("rebase", "difftool", "mergetool"):
        names = ("-x", "--exec") if sub == "rebase" else ("-x", "--extcmd")
        for n, a in enumerate(args):
            long = _resolve(a.partition("=")[0], gitopts.LONG["rebase"]) if sub == "rebase" and a.startswith("--") \
                else a.partition("=")[0]
            if a in names and n + 1 < len(args):
                out.append(args[n + 1])
            elif a.startswith("--") and long in names and "=" in a:
                out.append(a.split("=", 1)[1])
            elif a.startswith("--") and long in names and n + 1 < len(args):
                out.append(args[n + 1])
            elif a.startswith("-x") and len(a) > 2:
                out.append(a[2:])
    elif sub == "grep":
        for a in args:
            if a == "--":
                break
            name = a.partition("=")[0]
            if a.startswith("--") and len(name) >= 4 and "--open-files-in-pager".startswith(name) and "=" in a:
                out.append(a.split("=", 1)[1])
            elif a.startswith("-") and not a.startswith("--") and "O" in a[1:]:
                out.append(a[a.index("O", 1) + 1:])
    elif sub == "submodule":
        pos = _positional(args)
        if pos[:1] == ["foreach"]:
            rest = _skip_opts(args[args.index("foreach") + 1:])
            out.append(rest[0] if len(rest) == 1 else " ".join(shlex.quote(w) for w in rest))
    elif sub == "bisect":
        if args[:1] == ["run"] and len(args) > 1:
            out.append(" ".join(shlex.quote(w) for w in args[1:]))
    return [t for t in out if t]


def _studios_near(ctx, command):
    """Studios that hold the working directory or a path the command names (for a text that cannot be read)."""
    studios = list(ctx.studios(ctx.cwd))
    for token in re.findall(r"[^\s'\";&|()]+", command):
        if "/" in token or token.startswith("."):
            part = _expand(token, {})
            if part:
                studios += [s for s in ctx.studios(os.path.join(ctx.cwd, part)) if s not in studios]
    return studios


def _judge_text(text, here, ctx, depth):
    """The deny for a command text that git or an alias runs, judged from `here`; a text that cannot be read
    is denied while a studio is involved, as at the top level."""
    try:
        more = _calls(text, here, {}, 1, [])
    except ValueError:
        studios = ctx.studios(here) + ctx.studios(ctx.cwd)
        if studios:
            return _deny(studios[0], "git runs a command text that cannot be read")
        return None
    for sub_call in more:
        got = _judge_call(sub_call, ctx, depth)
        if got:
            return got
    return None


def _inline_judgement(call, ctx, depth):
    """Why inline code (or a shell's whole text) is denied while a studio is involved, or None."""
    reason = _inline_reason(call.code)
    if reason:
        return reason
    tokens = len(_TOKEN.findall(call.code))
    if not tokens:
        return None
    if call.piped:
        return ("a shell or interpreter reads its commands from a pipe, xargs or a runner, and the text mentions "
                "git or gh")
    try:
        more = _calls(call.code, call.here, {}, max(depth, 1) + 1, [])
    except ValueError:
        return "inline code that mentions git or gh cannot be read"
    found = 0
    for sub_call in more:
        if sub_call.kind in ("git", "gh"):
            found += 1
        if sub_call.kind == "git" and sub_call.state != "closed":
            return sub_call.why or "inline code runs git with words the guard cannot read"
        got = _judge_call(sub_call, ctx, depth + 1)
        if got:
            return got.reason
    if found < tokens:
        return "inline code that mentions git or gh in a way the guard cannot read; run git directly"
    return None


def _judge_call(call, ctx, depth=0):
    if call.kind == "git":
        return _judge_git(call, ctx, depth)
    studios = ctx.studios(call.here if call.here else ctx.cwd) + ctx.studios(ctx.cwd)
    if not studios:
        return None
    if call.kind == "unresolved":
        reason = call.why
    elif call.kind == "gh":
        reason = _gh_reason(call.args, call.state)
    elif call.kind == "code":
        reason = None
        if any(_TOKEN.search(w) for w in call.args):
            reason = "awk or sed program text that mentions git or gh cannot be judged; run git directly"
    else:
        reason = _inline_judgement(call, ctx, depth)
    if reason is None:
        return None
    return _deny(studios[0], reason)


class _Context:
    def __init__(self, cwd):
        self.cwd, self._cache = cwd, {}

    def studios(self, path):
        if path not in self._cache:
            self._cache[path] = studios_of(path)
        return self._cache[path]


def check(command, cwd):
    """A `Deny`, or None when the command is none of attach's business. A command that cannot be read, or
    that the rules fail on, is denied while a studio holds the working directory or a path the command names.
    Finding the studios can still raise on a broken install; the hook runs this inside its fail-open wrapper."""
    if not isinstance(command, str):
        return None
    plain = re.sub(r"[\\'\"]", "", command)              # g\it, g''it and "g"it read as git
    if "git" not in plain and "gh" not in plain:
        return None
    cwd = os.path.abspath(cwd or os.getcwd())
    ctx = _Context(cwd)
    try:
        calls = _calls(command, cwd, {}, 0, [])
    except Exception as exc:
        studios = _studios_near(ctx, command)
        if studios:
            if isinstance(exc, ValueError):
                return _deny(studios[0], "the command cannot be read (an open quote or too deep a nesting) while "
                             "an attached studio is involved")
            return _deny(studios[0], "the attach guard could not read this command (%s)" % type(exc).__name__)
        return None
    try:
        for call in calls:
            found = _judge_call(call, ctx)
            if found:
                return found
    except Exception as exc:
        studios = _studios_near(ctx, command)
        if studios:
            return _deny(studios[0], "the attach guard could not judge this command (%s)" % type(exc).__name__)
        return None
    return None


def decide(command, cwd):
    """The deny reason (one sentence), or None. None fast when no attach studio is involved."""
    found = check(command, cwd)
    return found.reason if found else None


def message(found) -> str:
    """The text a deny shows the agent."""
    return ("Alpaca attach guard: %s. The attached repository changes only inside a task worktree (%s) and a "
            "change leaves this studio only through `alpaca attach check` and then `alpaca attach handoff`. "
            "Read-only git (status, diff, log, show, fetch) stays open." % (found.reason, found.worktrees))
