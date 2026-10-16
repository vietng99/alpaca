"""The one git runner of alpaca/attach/ (build plan P4, P5).

Every git call in this package goes through here, and so does the one other command attach runs on
its own (`gh auth token`). The runner never trusts the process it runs in:

  * it drops every variable that points git at another repository, index or object store
    (`gitw.LOCAL_ENV`) and every `GIT_CONFIG_*` variable, so an alpaca call made from inside a studio
    git hook, `git rebase -x` or a test runner acts on the repository it was told to, not on the studio;
  * it drops `GIT_TEMPLATE_DIR` and the author and committer variables, and a call made with
    `identity=True` sets those four to IDENTITY in that one process (they outrank `-c`);
  * it sets `GIT_TERMINAL_PROMPT=0`, no system or global git config, no system or global attributes file,
    no askpass program, and passes `-c core.hooksPath=/dev/null -c core.fsmonitor=false`, so a repository's
    own hook or fsmonitor command never runs because attach looked at it;
  * a network call (`remote`) carries the push token only for github.com, as an HTTP header in
    GIT_CONFIG_* variables of that one process, never in argv and never in a log or an error text;
  * it switches history rewriting off (`GIT_NO_REPLACE_OBJECTS`, `GIT_GRAFT_FILE=/dev/null`), so a
    replace ref or a grafts file planted in the clone cannot change what a sha names (t-648 class
    review r2, S3 and S4). It does not set `GIT_SHALLOW_FILE`: an existing empty file there makes git
    read every repository as shallow, and `clone_problems` would then refuse every clone;
  * a task's work folder is reached only through `in_worktree`, which names the repository on the
    command line (`--git-dir` the admin folder of the clone, `--work-tree` the folder) after
    `prove_worktree` checked the folder, its gitfile and that admin folder byte by byte. The folder's
    own `.git` file is never trusted, and `run` refuses any other git call that acts inside
    `.alpaca/attach/worktrees/` (t-648 class review r2, S1, S2, S5).

`test_attach_gitops.py::test_no_raw_subprocess_in_attach` fails when any other file in this package
starts a process on its own.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import re
import stat
import subprocess
from collections import namedtuple

from alpaca.attach import AttachError, DEFAULT_IDENTITY
from alpaca.watchdog.gitw import LOCAL_ENV

#: the default commit identity of every commit attach makes in the attached repository (P13). The studio's own
#: identity is `attach.commit_name` and `attach.commit_email` (AttachConfig.identity); this is what they default to.
IDENTITY = DEFAULT_IDENTITY

#: variables that carry a credential or hand git a prompt for one. Only `remote` and the API client may
#: see the token (P5); a process attach starts for a product gate sees none of these.
_CREDENTIALS = ("ALPACA_ATTACH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "PUBLIC_REPO_TOKEN", "GIT_ASKPASS", "SSH_ASKPASS")

#: variables that would hand git a prompt, a helper or a credential
_DROP = _CREDENTIALS + ("GIT_EXTERNAL_DIFF", "GIT_PAGER", "GIT_EDITOR", "GIT_SEQUENCE_EDITOR",
         # a template folder copies its hooks into every repository git creates (P7)
         "GIT_TEMPLATE_DIR",
         # these rank above `-c user.name` and `-c user.email`, so an inherited value would author the commit (P13)
         "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
         "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
         # a ceiling or a filesystem limit hides the work tree around the studio from `studio_kind`
         "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
         # these change what a pathspec means, so a check that lists files could match nothing
         "GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS",
         # an inherited value would change which attributes every checkout and merge reads (adversarial N3b)
         "GIT_ATTR_SOURCE")
#: held back from the release runner's own git too: a template's hooks would run in every gate clone, and an
#: attribute source would change the bytes each gate clone checks out (adversarial round 2, N3d)
_GATE_CLONE_DROP = ("GIT_TEMPLATE_DIR", "GIT_ATTR_SOURCE")
_IDENTITY_ENV = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")
_CONFIG_VAR = re.compile(r"^GIT_CONFIG_(COUNT|KEY_\d+|VALUE_\d+)$")
#: every trace switch (GIT_TRACE, GIT_TRACE2*, GIT_TRACE_PACKET, GIT_TRACE_CURL, ...) writes what git does, and with
#: GIT_TRACE2_CONFIG_PARAMS=http.* the push token header, to a file of the caller's choosing (t-652 round 2, R2-11/R2-12);
#: GIT_CURL_VERBOSE prints request headers to stderr, which an error text could carry
_TRACE_VAR = re.compile(r"^(GIT_TRACE|GIT_CURL_VERBOSE)")

_SAFE_PROTOCOLS = "file:git:http:https:ssh"
# core.attributesFile=/dev/null: a global attributes file (eol, filters) would change the bytes of a checkout
# advice.graftFileDeprecated=false: with GIT_GRAFT_FILE set git prints an eight-line hint on every history walk
_QUIET_HOOKS = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.attributesFile=/dev/null",
                "-c", "advice.graftFileDeprecated=false")
_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]*@")
_SCP = re.compile(r"^(?P<user>[^/@:\s]+@)?(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*):(?!//)(?P<path>.+)$")
_SCHEME = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://")

#: what a run returns: the exit status and both streams as text (or bytes when binary)
Result = namedtuple("Result", "rc out err")


def scrub(text) -> str:
    """Text safe to print: URL userinfo (`https://user:token@host`) removed, whatever it was."""
    return _USERINFO.sub(lambda m: m.group("scheme"), str(text))


def clean_env(extra=None) -> dict:
    """The environment of an attach git process (P4). The token is never in it.

    `GIT_NO_REPLACE_OBJECTS=1` and a null graft file: the attached clone is writable by the agent, so a
    plain file at `.git/refs/replace/<sha>` or a line in `.git/info/grafts` could make every later read of
    a recorded commit return other content or other parents. A sha the record names must mean the object
    with that sha (op-062 t-650 fix round 2, B1; the studio's verifier cut and barrier.py hold the same rule)."""
    env = {k: v for k, v in os.environ.items()
           if k not in LOCAL_ENV and k not in _DROP and not _CONFIG_VAR.match(k) and not _TRACE_VAR.match(k)}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_ATTR_NOSYSTEM="1", LC_ALL="C", GIT_ALLOW_PROTOCOL=_SAFE_PROTOCOLS,
               GIT_NO_REPLACE_OBJECTS="1", GIT_GRAFT_FILE=os.devnull)
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    if extra:
        env.update(extra)
    return env


@contextlib.contextmanager
def process_env_scrubbed():
    """Run release or other code that starts git or a product command itself with the inherited git
    variables and the credential variables removed from this process (PM X1: `release/build.py`
    inherits os.environ; P5: the attach token reaches no gate, and `release/environment.py` does not
    drop it). Restores them on exit.

    The change is to `os.environ` of the whole process, so a second thread that calls `gitops.remote`
    while this block runs would find no token. The CLI runs one claim per process and a thread that
    needs the token must not share a process with a gate run."""
    held = {k: os.environ[k] for k in list(os.environ)
            if k in LOCAL_ENV or k in _CREDENTIALS or k in _GATE_CLONE_DROP or _CONFIG_VAR.match(k)}
    for k in held:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        os.environ.update(held)


def resolve_identity(identity):
    """The (name, email) a call with `identity=` commits as, or None for no identity. `True` is the default identity;
    a pair is the studio's configured one (`AttachConfig.identity`); anything else that is not a pair of
    non-empty texts is a programming error, never a silent fallback to the default."""
    if not identity:
        return None
    if identity is True:
        return IDENTITY
    if (isinstance(identity, (tuple, list)) and len(identity) == 2
            and all(isinstance(x, str) and x and "\n" not in x and "\0" not in x for x in identity)):
        return (identity[0], identity[1])
    raise AttachError("the commit identity is not a (name, email) pair")


def identity_label(identity=None) -> str:
    """`Name <email>` of `identity` (the default one without it)."""
    name, email = IDENTITY if identity is None else identity
    return "%s <%s>" % (name, email)


def identity_env(env, identity=True) -> dict:
    """`env` with the four author and committer variables set to the commit identity (the default one, or the
    pair given). The environment ranks above `-c` and above the repository's own config, so this is what holds the
    identity of a commit (P13)."""
    who = resolve_identity(identity) or IDENTITY
    out = dict(env)
    out.update(zip(_IDENTITY_ENV, (who[0], who[1], who[0], who[1])))
    return out


def _argv(repo, args, identity):
    argv = ["git", "--no-pager", "-C", str(repo), *_QUIET_HOOKS]
    who = resolve_identity(identity)
    if who:
        argv += ["-c", "user.name=%s" % who[0], "-c", "user.email=%s" % who[1]]
    return argv + [str(a) for a in args]


#: the folders a task's work folder lives in, below the studio root (config.STATE_DIR + "/worktrees")
_WORK_PARTS = (".alpaca", "attach", "worktrees")


def in_work_folder(path) -> bool:
    """True when the real path of `path` is a task's work folder or lies inside one: it has the parts
    `.alpaca/attach/worktrees/<name>`. (A studio that itself sits inside another studio's work folder
    is read the same way and fails closed, with the message of `run`.)"""
    parts = os.path.realpath(str(path)).split(os.sep)
    return any(tuple(parts[i:i + len(_WORK_PARTS)]) == _WORK_PARTS for i in range(len(parts) - len(_WORK_PARTS)))


def _label(args) -> str:
    """The command name of `args` for a message: the first argument that is not a leading `-c key=value` pair."""
    items = list(args)
    while len(items) >= 2 and items[0] == "-c":
        items = items[2:]
    return str(items[0]) if items else ""


def _exec(argv, label, *, timeout, input, binary, env) -> Result:
    data = None
    if input is not None:
        data = input if isinstance(input, bytes) else str(input).encode("utf-8")
    try:
        done = subprocess.run(argv, capture_output=True, env=env, timeout=timeout, input=data,
                              stdin=None if data is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise AttachError("git %s ran out of time after %ss" % (label, timeout)) from None
    except OSError as exc:
        raise AttachError("git could not run: %s" % scrub(exc)) from None
    if binary:
        return Result(done.returncode, done.stdout, done.stderr)
    return Result(done.returncode, done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace"))


def run(repo, *args, timeout=120, input=None, binary=False, identity=False, env=None) -> Result:
    """Run `git -C repo args`. Never raises for a non-zero exit; raises AttachError when git cannot
    start or runs out of time. Output is text (replacement characters on bad bytes) unless binary.

    A repo that is a task's work folder (or inside one) is refused: git there would follow the
    folder's own `.git` file, which the worker can rewrite, to whatever repository it names, and
    would walk up to the studio's repository when the file is gone. Use `in_worktree`."""
    if in_work_folder(repo):
        raise AttachError("git was asked to act in a task work folder (%s) by its own .git file; use "
                          "gitops.in_worktree, which names the repository itself" % os.path.basename(str(repo).rstrip(os.sep)))
    full = env or clean_env()
    if identity:
        full = identity_env(full, identity)
    return _exec(_argv(repo, args, identity), _label(args), timeout=timeout, input=input, binary=binary, env=full)


def _tail(text, limit=300):
    one = " ".join(scrub(text).split())
    return one[-limit:]


def git(repo, *args, check=True, timeout=120, input=None, binary=False, identity=False):
    """`git -C repo args` through the clean environment. Returns stdout with the trailing newline
    removed (bytes, untouched, when binary). A failing git raises AttachError with one scrubbed line
    when `check` is set, and returns whatever it printed (empty on a plain failure) when it is not."""
    res = run(repo, *args, timeout=timeout, input=input, binary=binary, identity=identity)
    if res.rc != 0 and check:
        err = res.err.decode("utf-8", "replace") if binary else res.err
        raise AttachError("git %s failed: %s" % (args[0] if args else "", _tail(err) or "exit %d" % res.rc))
    if binary:
        return res.out
    return res.out[:-1] if res.out.endswith("\n") else res.out


def token():
    """The GitHub token for a push or an API call: ALPACA_ATTACH_TOKEN, else `gh auth token`, else
    None (P5). It is never logged, stored, or put in argv."""
    value = (os.environ.get("ALPACA_ATTACH_TOKEN") or "").strip()
    if value:
        return value
    try:
        done = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    value = done.stdout.strip() if done.returncode == 0 else ""
    return value or None


def remote(repo, *args, url, timeout=600, no_repo=False, overrides=(), env_extra=None) -> str:
    """A git command that talks to `url` (clone, fetch, push, ls-remote). The token rides as a
    github.com extraheader of this one process, and only when `url` is a github.com https URL.

    `no_repo=True` runs the command with `GIT_DIR=/dev/null`, so git reads no repository config at all: a
    `url.<x>.insteadOf` or `remote.*` key of the clone cannot redirect it (t-652 round 2, R2-05). Use it for a
    command that needs no repository, such as `ls-remote <url>`.

    `overrides` is a list of `key=value` strings passed as `-c key=value` before the command, for this process only:
    a command-line value outranks the repository's config (t-686 fix round 1, B1: the push names the keys that could
    add a ref or an object and sets each to its off value, whatever the folder it runs in holds).

    `env_extra` adds variables to the clean environment of this one process (t-686 fix round 2: the push sets
    `GIT_SHALLOW_FILE` to a path that cannot exist, so no shallow file is read)."""
    env = clean_env(env_extra)
    if no_repo:
        env["GIT_DIR"] = os.devnull
    if str(url).startswith("https://github.com/"):
        value = token()
        if value:
            header = base64.b64encode(("x-access-token:" + value).encode()).decode()
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                       GIT_CONFIG_VALUE_0="AUTHORIZATION: basic " + header)
    pairs = [x for kv in overrides for x in ("-c", str(kv))]
    res = run(repo, *pairs, *args, timeout=timeout, env=env)
    if res.rc != 0:
        # git's own message may echo the URL; scrub it, and never include the token (it is not in argv)
        raise AttachError("git %s failed: %s" % (args[0] if args else "", _tail(res.err) or "exit %d" % res.rc))
    return res.out[:-1] if res.out.endswith("\n") else res.out


def run_hook(path, args, *, cwd, env, stdin_text="", timeout=600) -> Result:
    """Run the repository's own hook program `path` with `args` in `cwd` (op-062 t-686, P7: handoff runs the
    repository's pre-push hook itself, in an environment `env` that holds no token). Both streams come back in
    `out`; the program runs in a session of its own and the whole group is killed at the timeout. Raises
    AttachError when it cannot start or runs out of time. `env` is the caller's to build: nothing is added here."""
    try:
        proc = subprocess.Popen([str(path), *[str(a) for a in args]], cwd=str(cwd), env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        raise AttachError("the hook %s could not start: %s" % (os.path.basename(str(path)), scrub(exc))) from None
    try:
        out, _ = proc.communicate(stdin_text.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            pass
        proc.communicate()
        raise AttachError("the hook %s ran out of time after %ss" % (os.path.basename(str(path)), timeout)) from None
    return Result(proc.returncode, out.decode("utf-8", "replace"), "")


# -------------------------------------------------------------------------- a task's work folder

_TASK_ID = re.compile(r"t-[0-9]{1,6}")


def admin_dir(app, task) -> str:
    """`<real app>/.git/worktrees/<task>`: where git keeps the HEAD, index and gitdir of the task's linked
    worktree. `git worktree add` names it after the folder, which is named after the task."""
    if not isinstance(task, str) or not _TASK_ID.fullmatch(task):
        raise AttachError("%r cannot name a work folder (a task id looks like t-123)" % str(task)[:40])
    return os.path.join(os.path.realpath(str(app)), ".git", "worktrees", task)


def run_in_worktree(app, task, folder, *args, timeout=120, identity=False, input=None, env=None) -> Result:
    """The runner under `in_worktree`: the same explicit `--git-dir` and `--work-tree`, and the whole Result
    (exit status included), never raising for a non-zero exit. `env` replaces the clean environment (the git
    guard passes the user's own, so an alias in the user's config is read as the user's git would read it);
    the repository is still the one named on the command line. Call `prove_worktree` first."""
    admin = admin_dir(app, task)
    folder = str(folder)
    if not os.path.isabs(folder):
        raise AttachError("the work folder of %s is not an absolute path" % task)
    argv = ["git", "--no-pager", "-C", os.path.realpath(str(app)), "--git-dir=%s" % admin,
            "--work-tree=%s" % folder, *_QUIET_HOOKS]
    full = dict(env) if env is not None else clean_env()
    who = resolve_identity(identity)
    if who:
        argv += ["-c", "user.name=%s" % who[0], "-c", "user.email=%s" % who[1]]
        full = identity_env(full, who)
    argv += [str(a) for a in args]
    return _exec(argv, args[0] if args else "", timeout=timeout, input=input, binary=False, env=full)


def in_worktree(app, task, folder, *args, check=True, timeout=120, identity=False, input=None):
    """Run git in the work folder `folder` of `task` with the repository named on the command line:
    `--git-dir` is the task's admin folder inside `app/.git` and `--work-tree` is the folder, so the
    folder's own `.git` file (or the lack of one) decides nothing. Only absolute paths may be passed in
    `args` (the process starts in `app`). Call `prove_worktree` first: this only acts where it is told.
    Returns stdout without the final newline; with `check` a failing git raises AttachError."""
    res = run_in_worktree(app, task, folder, *args, timeout=timeout, identity=identity, input=input)
    if res.rc != 0 and check:
        raise AttachError("git %s failed: %s" % (args[0] if args else "", _tail(res.err) or "exit %d" % res.rc))
    return res.out[:-1] if res.out.endswith("\n") else res.out


def _kind(path):
    """"file", "dir", "link", "other" or None (nothing there) for the lstat of `path`."""
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return None
    if stat.S_ISLNK(mode):
        return "link"
    return "dir" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "other"


def _plain_text(path, limit=8192):
    """The text of a small plain file (a link is not followed), or None. The only reader of git's own
    administrative files here (the gitfile and the admin folder's gitdir, commondir and HEAD)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, limit).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def gitfile_target(folder):
    """The real path of the git folder that `folder/.git` names, or None when `.git` is not a plain file with a
    `gitdir:` line (a folder, a link, nothing). Read through `_plain_text`, without following a link and without
    running git in `folder`. The proof hook (op-062 t-650) tells a plain repository and another task's gitfile
    apart with it before `prove_worktree` reads the rest."""
    dot = os.path.join(str(folder), ".git")
    if _kind(dot) != "file":
        return None
    text = (_plain_text(dot) or "").strip()
    if not text.startswith("gitdir:"):
        return None
    target = text[len("gitdir:"):].strip()
    return os.path.realpath(target if os.path.isabs(target) else os.path.join(str(folder), target))


def worktree_list(app) -> list:
    """`[{"path": real path, "branch": "refs/heads/x" or None, "prunable": bool}]` for each worktree git
    lists for `app`, the main one first."""
    found, cur = [], None
    for line in git(app, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            cur = {"path": os.path.realpath(line[len("worktree "):]), "branch": None, "prunable": False}
            found.append(cur)
        elif cur is not None and line.startswith("branch "):
            cur["branch"] = line[len("branch "):]
        elif cur is not None and line.startswith("prunable"):
            cur["prunable"] = True
    return found


def prove_worktree(app, task, folder, branch) -> None:
    """Raise AttachError unless `folder` is the linked worktree of `app` that `task` owns, on `branch`.

    Each step reads one thing the worker or another process could have changed, byte by byte and
    without following a link: the folder, its `.git` file, the admin folder in `app/.git/worktrees`
    (its gitdir, commondir and HEAD files), then what git itself reports through the explicit runner
    (`in_worktree`) and in `app`'s worktree list. The refusal names the repair."""
    app_real = os.path.realpath(str(app))
    admin = admin_dir(app, task)
    folder = str(folder)
    dot = os.path.join(folder, ".git")
    fix = "repair it with: git -C %s worktree repair %s" % (app_real, folder)

    def refuse(why, repair=True):
        raise AttachError("the work folder of %s is not sound: %s%s" % (task, why, "; " + fix if repair else ""))

    if not os.path.isabs(folder) or os.path.basename(folder) != task:
        refuse("it is not the folder named after the task", False)
    if _kind(folder) != "dir" or os.path.realpath(folder) != folder:
        refuse("it is not a real folder (a link, or a path that resolves elsewhere)", False)
    if folder == app_real:
        refuse("it is the attached clone itself", False)
    # the gitfile names this task's admin folder
    if _kind(dot) != "file":
        refuse("it has no .git file of its own")
    text = (_plain_text(dot) or "").strip()
    if not text.startswith("gitdir:"):
        refuse("its .git file is not a gitdir line")
    target = text[len("gitdir:"):].strip()
    target = target if os.path.isabs(target) else os.path.join(folder, target)
    if os.path.realpath(target) != admin:
        # the same task's admin folder at another place is a moved studio (git worktree repair fixes it); another
        # task's or another repository's admin folder is not, and a repair would hand that folder to this one
        refuse("its .git file names %s, not this task's admin folder" % scrub(os.path.realpath(target)),
               repair=os.path.basename(os.path.realpath(target)) == task)
    # the admin folder and its three files
    if _kind(admin) != "dir" or os.path.realpath(admin) != admin:
        refuse("git has no admin folder for it (%s/.git/worktrees/%s)" % (app_real, task))
    for name in ("gitdir", "commondir", "HEAD"):
        if _kind(os.path.join(admin, name)) != "file":
            refuse("its admin folder lacks a plain %s file" % name)
    back = (_plain_text(os.path.join(admin, "gitdir")) or "").strip()
    if os.path.realpath(back) != dot:
        refuse("its admin folder names %s as the gitfile" % scrub(back))
    common = (_plain_text(os.path.join(admin, "commondir")) or "").strip()
    if os.path.realpath(os.path.join(admin, common)) != os.path.join(app_real, ".git"):
        refuse("its admin folder takes its data from %s, not from %s/.git" % (scrub(common), app_real))
    if (_plain_text(os.path.join(admin, "HEAD")) or "").strip() != "ref: refs/heads/%s" % branch:
        refuse("its HEAD is not the work branch %s (check it out again there: git -C %s switch %s)" % (branch, folder, branch), False)
    # what git itself says, with the repository named on the command line
    seen = in_worktree(app, task, folder, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    if os.path.realpath(seen or "/nonexistent") != os.path.join(app_real, ".git"):
        refuse("git reads its data from %s, not from %s/.git" % (scrub(seen), app_real))
    if in_worktree(app, task, folder, "symbolic-ref", "-q", "HEAD", check=False) != "refs/heads/%s" % branch:
        refuse("git does not have the work branch %s checked out there (git -C %s switch %s)" % (branch, folder, branch), False)
    # app/ lists it, on that branch, not as a stale entry
    listed = [w for w in worktree_list(app) if w["path"] == folder]
    if not listed or listed[0]["branch"] != "refs/heads/%s" % branch or listed[0]["prunable"]:
        refuse("git does not list it as the worktree of %s" % branch)


def worktree_admins_on(app, branch) -> list:
    """Paths of the admin folders in `app/.git/worktrees` that are real folders whose HEAD file reads
    `ref: refs/heads/<branch>`. A cut that made `branch` just now uses this to take away exactly its own
    registration, whatever name git gave it."""
    base = os.path.join(os.path.realpath(str(app)), ".git", "worktrees")
    found = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return found
    for name in names:
        path = os.path.join(base, name)
        if _kind(path) == "dir" and (_plain_text(os.path.join(path, "HEAD")) or "").strip() == "ref: refs/heads/%s" % branch:
            found.append(path)
    return found


# ------------------------------------------------------------------------------------ the source

def split_source(source):
    """(kind, value) for a clone source: kind is "url" (scheme://), "scp" (host:path) or "path"."""
    if _SCHEME.match(source):
        return "url", source
    m = _SCP.match(source)
    if m and not source.startswith(("/", "./", "../", "~")) and "/" not in source.split(":", 1)[0]:
        return "scp", source
    return "path", source


def repository_of(display):
    """owner/name when `display` names a github.com repository, else None."""
    m = re.match(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                 r"([A-Za-z0-9][A-Za-z0-9_.-]*)/([A-Za-z0-9][A-Za-z0-9_.-]*?)(?:\.git)?/?$", display)
    return "%s/%s" % m.groups() if m else None


def has_userinfo(url) -> bool:
    """True for a URL with `user@` or `user:pass@` in its authority (the scp form is not a URL)."""
    m = _SCHEME.match(url)
    if not m:
        return False
    rest = url[m.end():]
    authority = rest.split("/", 1)[0]
    return "@" in authority


def validate_source(source, root) -> str:
    """The display form of a clone source, or AttachError. Refuses what `git clone` would read as an
    option, control characters, user info in a URL (the text names the scrubbed form only), a scheme
    that is not file, git, http, https or ssh, a transport helper (`ext::`), and a local path that is
    missing or lies inside the studio. A local path is returned as its absolute real path."""
    if not isinstance(source, str) or not source.strip():
        raise AttachError("the repository source is empty")
    source = source.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in source):
        raise AttachError("the repository source holds a control character")
    if source.startswith("-"):
        raise AttachError("the repository source starts with '-' and would be read as a git option")
    if "::" in source:
        raise AttachError("the repository source names a git transport helper, which attach does not use")
    kind, _ = split_source(source)
    if kind == "url":
        scheme = _SCHEME.match(source).group("scheme").lower()
        if scheme not in ("file", "git", "http", "https", "ssh"):
            raise AttachError("the URL scheme %s:// is not supported (use https, ssh, git or file)" % scheme)
        if has_userinfo(source):
            raise AttachError("the URL %s carries user info; put the credential in ALPACA_ATTACH_TOKEN or "
                              "your ssh agent, not in the URL" % scrub(source))
        if scheme == "file":
            return _local(source[len("file://"):], root)
        return source
    if kind == "scp":
        return source
    return _local(os.path.expanduser(source), root)


def _local(path, root):
    real = os.path.realpath(path)
    if not os.path.isdir(real):
        raise AttachError("%s is not a directory" % path)
    studio = os.path.realpath(root)
    if real == studio or real.startswith(studio + os.sep):
        raise AttachError("%s lies inside the studio; the attached repository must come from outside it" % real)
    return real


# --------------------------------------------------------------------------------- the checkout

def is_toplevel(path) -> bool:
    """`path` is a git working tree of its own: `path/.git` is a real directory (not a link, not a
    gitfile) and git reports `path` itself as the top level (realpaths compared)."""
    dot = os.path.join(str(path), ".git")
    if os.path.islink(os.path.abspath(str(path))) or os.path.islink(dot) or not os.path.isdir(dot):
        return False
    res = run(path, "rev-parse", "--show-toplevel")
    return res.rc == 0 and os.path.realpath(res.out.strip()) == os.path.realpath(str(path))


#: Keys of the clone's own config that the digest does NOT cover (lowercased). Every other local key is hashed
#: (op-062 t-686 fix round 1). A list of "keys that steer a push" missed `push.followTags`, `push.recurseSubmodules`,
#: `push.pushOption`, `push.gpgSign`, `gpg.program`, `submodule.recurse`, `core.gitProxy`, `transfer.fsckObjects` and
#: `pack.threads` (investigator probe `test_probe_config_digest_misses_push_keys`), and the next such key would be
#: missed the same way. What stays out: the `core.*` keys git writes when it creates a clone; `alias.*`, because git
#: does not let an alias replace a built-in command and attach never starts an alias (probe in fix-r1.md); and
#: `branch.*`, which git writes when it makes a tracking branch and which only steers a command given no URL or refspec.
_UNWATCHED = re.compile(r"^(core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|precomposeunicode"
                        r"|symlinks|untrackedcache)|alias\..*|branch\..*)$")


def config_digest(repo) -> str:
    """A digest of every key of `repo/.git/config` except the few in `_UNWATCHED`. The state file stores it at
    attach; check, approve, handoff, merge and sync refuse when it changed. A config that cannot be listed hashes
    as a value no list gives (so a clone whose config cannot be read never matches)."""
    res = run(repo, "config", "--local", "-z", "--list")
    if res.rc != 0:
        return hashlib.sha256(b"config-unreadable").hexdigest()
    items = []
    for chunk in res.out.split("\0"):
        if not chunk:
            continue
        key, _, value = chunk.partition("\n")
        if not _UNWATCHED.match(key.lower()):
            items.append("%s=%s" % (key, value))
    return hashlib.sha256("\n".join(sorted(items)).encode()).hexdigest()


def studio_kind(path) -> str:
    """How the studio at `path` relates to git, as one word:

      "own"       `path/.git` is a real folder and git reports `path` as the top level;
      "link"      `path` itself is a link (use the real path of the studio);
      "borrowed"  `path` is inside a git work tree it does not own (a linked worktree, whose `.git` is a
                  gitfile, or a folder of a bigger repository), or has a `.git` that is not a real folder;
      "none"      git finds no work tree around `path`.

    Attach writes only the studio's own `.git/info/exclude`; in a "borrowed" studio the exclude file that
    git reads lives outside the studio root, so attach refuses there instead of writing it."""
    if os.path.islink(os.path.abspath(str(path))):      # abspath: `link/` and `link/.` are the link itself
        return "link"
    if is_toplevel(path):
        return "own"
    if os.path.lexists(os.path.join(str(path), ".git")):
        return "borrowed"
    res = run(path, "rev-parse", "--is-inside-work-tree")
    return "borrowed" if res.rc == 0 and res.out.strip() == "true" else "none"


#: entries of `.git` that git reads for the repository's identity; one linked outside the clone swaps the store
_GIT_ENTRIES_SCAN_DEEP = ("refs", "logs", "hooks", "info", "worktrees")
_SCAN_LIMIT = 20000


def _links_out(git_dir) -> list:
    """Names (relative to `git_dir`) of links inside it that resolve outside it. The top level and the
    `objects` fan-out level are read in full; refs, logs, hooks, info and worktrees are walked (bounded)."""
    real = os.path.realpath(git_dir)
    found, seen = [], 0

    def outside(path):
        target = os.path.realpath(path)
        return target != real and not target.startswith(real + os.sep)

    def visit(folder, deep):
        nonlocal seen
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        for entry in entries:
            seen += 1
            if seen > _SCAN_LIMIT:
                return
            rel = os.path.relpath(entry.path, git_dir)
            if entry.is_symlink():
                if outside(entry.path):
                    found.append(rel)
            elif entry.is_dir(follow_symlinks=False) and deep:
                visit(entry.path, True)

    visit(git_dir, False)
    for name in _GIT_ENTRIES_SCAN_DEEP:
        folder = os.path.join(git_dir, name)
        if os.path.isdir(folder) and not os.path.islink(folder):
            visit(folder, True)
    objects = os.path.join(git_dir, "objects")
    if os.path.isdir(objects) and not os.path.islink(objects):
        visit(objects, False)
        for sub in ("pack", "info"):
            folder = os.path.join(objects, sub)
            if os.path.isdir(folder) and not os.path.islink(folder):
                visit(folder, False)
    return sorted(set(found))


def clone_problems(path, subject) -> list:
    """What makes the clone at `path` borrow from, or write to, anything outside its own `.git` folder.
    `subject` names it in each line ("the clone", "app/"). [] means the checks found nothing; the caller
    has already shown that `path/.git` is a real folder and `path` is a top level of its own."""
    dot = os.path.join(str(path), ".git")
    out = []
    if os.path.lexists(os.path.join(dot, "commondir")):
        out.append("%s shares its object store with another repository (.git/commondir)" % subject)
    links = _links_out(dot)
    if links:
        out.append("%s has .git entries that link outside it (%s)" % (subject, ", ".join(links[:3])))
    common = run(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.rc == 0 and os.path.realpath(common.out.strip()) != os.path.realpath(dot):
        out.append("%s reads its git data from %s, not from its own .git" % (subject, scrub(common.out.strip())))
    if git(path, "rev-parse", "--is-shallow-repository", check=False) == "true":
        out.append("%s is shallow; attach needs the full history" % subject)
    if os.path.exists(os.path.join(dot, "objects", "info", "alternates")):
        out.append("%s borrows objects from another folder (objects/info/alternates)" % subject)
    if git(path, "config", "--local", "--get", "core.worktree", check=False):
        out.append("%s sets core.worktree" % subject)
    out.extend(_manifest_problems(path, subject))
    return out


def _is_manifest_name(name) -> bool:
    """True for a path whose last part is ALPACA-MANIFEST in any letter case (a repository checked out on a
    case-insensitive file system is refused too)."""
    return bool(name) and name.rsplit("/", 1)[-1].casefold() == "alpaca-manifest"


def tree_manifest_hits(app, sha) -> list:
    """Paths named ALPACA-MANIFEST in the tree of commit `sha`, from the full tree listing (a pathspec
    setting cannot hide one). A commit that holds one, cut into a task's work folder, would make
    `paths.root` stop there instead of at the studio (t-648 adversarial r2, C6)."""
    listing = git(app, "ls-tree", "-r", "-z", "--name-only", sha, timeout=300)
    return [name for name in listing.split("\0") if _is_manifest_name(name)]


def _manifest_problems(path, subject) -> list:
    """A clone that holds ALPACA-MANIFEST in any folder: `paths.root` walks up from the working folder and
    stops at the first one, so a verb run from that folder would write the studio's files into the clone.
    Tracked files are listed in full and compared by name here, so a pathspec setting cannot hide one.
    The name is compared without regard to case, so a repository checked out on a case-insensitive file
    system is refused too."""
    hits = []
    if os.path.lexists(os.path.join(str(path), "ALPACA-MANIFEST")):
        hits.append("ALPACA-MANIFEST")
    tracked = git(path, "ls-files", "-z", check=False, timeout=300)
    for name in tracked.split("\0"):
        if _is_manifest_name(name) and name not in hits:
            hits.append(name)
    if not hits:
        return []
    where = "at " + (hits[0].rsplit("/", 1)[0] + "/" if "/" in hits[0] else "its top level")
    return ["%s is itself an Alpaca project (it has ALPACA-MANIFEST %s); the studio root would be found inside it"
            % (subject, where)]
