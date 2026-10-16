"""`alpaca attach init <git-url|path>`: clone the existing repository into the studio (R1, R17).

`alpaca new --attach` runs onboarding unchanged and then this same step (P3), so a studio made
without `--attach` can attach later with the same verb. One attach runs in a studio at a time (a
lock on the studio's `.alpaca` folder, held for the whole step; a second run is refused, not
queued). In order, and all of it undone when a step fails (the clone removed, the exclude file and
state.json put back as they were, the terms file this run made removed):

  1. refuse when the block is already there, when the studio is not onboarded, when the target
     folder exists, or when the source is unsafe (option-like, user info, inside the studio);
  2. put the clone and attach's own state folder in `.git/info/exclude`, never in `.gitignore`
     (P2: `.gitignore` is a shipped file and a manifest hash), and prove with `git check-ignore`
     that both are ignored;
  3. clone with no hard links, no alternates, no depth, hooks off, no prompt, and refuse a clone
     that is shallow, borrows objects, sets `core.worktree` or is itself an Alpaca project;
  4. read the gates from the clone's files (or `--gates-file`); an empty gate list is refused (P10);
  5. set the push trap, the commit identity and the config digest, write `state.json` and, when the
     studio's barrier names a term list that is missing, the minimum sealed terms (P23);
  6. write `attach:` and `paths.product_tree` into project.yaml and append `attach-init` on op-0.

Nothing is written into the clone's working tree, and nothing is installed in its hooks (P7).

A Ctrl-C is held (noted, acted on later) while a change and its entry in the rollback's ledger are apart,
and from the catch to the end of the rollback; it acts at once only where the ledger names every change made
so far (`_SigintHold`). A Ctrl-C after the record COMMIT keeps the attach: the record and the files agree.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import os
import re
import shutil
import signal
import sqlite3
import stat
import threading
from pathlib import Path

from alpaca.attach import AttachError, STATE_DIR
from alpaca.attach import config, gitops, safefs

PUSH_TRAP = "alpaca-handoff-only:refused"
#: the config key that rewrites a push to a URL of the upstream into a push to the trap. `remote.<upstream>.pushurl`
#: holds a push that names the remote; this key holds a push that spells the URL out (op-062 t-697). git applies
#: `pushInsteadOf` to a URL given on the command line, and skips it for a configured remote that has its own pushurl,
#: so the two do not overlap.
PUSH_REWRITE_KEY = "url.%s.pushInsteadOf" % PUSH_TRAP
MAX_ANSWERS_BYTES = 1 << 20
CLONE_TIMEOUT = 1800
_BORROWED = ("the studio is a linked git worktree or a folder inside another repository; attach needs a studio "
             "whose .git is its own folder, so that the clone stays out of the studio's commits (run git init in "
             "the studio, or attach from a studio that owns its .git)")
LINKED = ("the studio path is a link; use its real path (run the command from the real folder) so that "
          "attach writes inside the studio itself")
_EXCLUDE_MARK = "# alpaca attach (op-062): the attached clone and attach's own files, never committed"


# ------------------------------------------------------------------------------- the ignore proof

def _exclude_path(root):
    return os.path.join(str(root), ".git", "info", "exclude")


def studio_has_git(root) -> bool:
    """True when the studio owns a `.git` folder of its own (attach may write its exclude file)."""
    return gitops.studio_kind(root) == "own"


def _exclude_problem(root):
    """Why the studio's exclude file cannot be written safely, or None. A link at `.git/info` or at
    `.git/info/exclude` would carry the write to a file outside the studio."""
    info = os.path.dirname(_exclude_path(root))
    for path, label in ((info, ".git/info"), (_exclude_path(root), ".git/info/exclude")):
        if os.path.islink(path):
            return "%s is a link; attach writes its ignore lines only into a file inside the studio" % label
    real_git = os.path.realpath(os.path.join(str(root), ".git"))
    real_info = os.path.realpath(info)
    if os.path.lexists(info) and not os.path.isdir(info):
        return ".git/info is not a folder; attach cannot write its ignore lines there"
    if os.path.lexists(info) and real_info != os.path.join(real_git, "info"):
        return ".git/info resolves outside the studio's .git folder"
    return None


def _read_exclude(root):
    """The exclude file's text, or None when there is none. Raises AttachError when the file is there
    and cannot be read or is not a plain file: None must mean "absent", because the rollback of
    `_write_exclude` deletes the file when it was recorded as absent."""
    problem = _exclude_problem(root)
    if problem:
        raise AttachError(problem)
    data = safefs.read_existing(root, _exclude_path(root))
    return None if data is None else data.decode("utf-8", "surrogateescape")


def _write_exclude(root, text) -> None:
    """Replace the exclude file with `text` (remove it when `text` is None). Raises AttachError, and
    writes nothing, when the file or a folder above it is a link. `safefs` puts the text into a new
    file in `.git/info` that then replaces the old entry, so a hard link to a file outside the studio
    is left as it was; the file keeps its mode, or takes 0644 less the umask."""
    problem = _exclude_problem(root)
    if problem:
        raise AttachError(problem)
    path = _exclude_path(root)
    if text is None:
        try:
            _remove_inside(root, path)
        except (AttachError, OSError):
            pass
        return
    try:
        safefs.write_bytes(root, path, text.encode("utf-8", "surrogateescape"))
    except safefs.WriteFailed as exc:
        raise AttachError("the studio's .git/info/exclude cannot be written: %s" % exc.strerror) from None


def _restore_exclude(root, text) -> None:
    """Rollback of `_write_exclude`: a refusal here is dropped, the error that started the rollback is the one to show."""
    try:
        _write_exclude(root, text)
    except AttachError:
        pass


def ensure_ignored(root, repo) -> list:
    """Put `/<repo>/` and `/.alpaca/attach/` in the studio's .git/info/exclude and prove that
    `<repo>/x` and `.alpaca/attach/worktrees/x` are ignored. Returns the lines it added. Raises
    AttachError, with the exclude file put back, when a tracked or shipped rule beats the exclude
    file, when the exclude file is reached through a link, or when the studio sits in a git work tree
    it does not own (a linked worktree or a folder of a bigger repository), where the exclude file
    git reads is outside the studio. A studio with no git around it has nothing to ignore: returns []."""
    kind = gitops.studio_kind(root)
    if kind == "none":
        return []
    if kind == "link":
        raise AttachError(LINKED)
    if kind == "borrowed":
        raise AttachError(_BORROWED)
    before = _read_exclude(root)                    # refuses a link and an exclude file it cannot read
    lines = ["/%s/" % repo, "/%s/" % STATE_DIR]
    have = set((before or "").splitlines())
    add = [x for x in lines if x not in have]
    if not add:
        _prove_ignored(root, repo)
        return add
    text = before or ""
    if text and not text.endswith("\n"):
        text += "\n"
    if _EXCLUDE_MARK not in have:
        text += _EXCLUDE_MARK + "\n"
    try:
        _write_exclude(root, text + "\n".join(add) + "\n")
        _prove_ignored(root, repo)
    except BaseException:
        # whatever stops the proof (a refusal, a git timeout, Ctrl-C) leaves the exclude file as it was found
        _restore_exclude(root, before)
        raise
    return add


def _prove_ignored(root, repo) -> None:
    for probe in ("%s/x" % repo, "%s/worktrees/x" % STATE_DIR):
        if gitops.run(root, "check-ignore", "-q", "--", probe).rc != 0:
            raise AttachError("git does not ignore %s even with .git/info/exclude set: a .gitignore rule "
                              "overrides it; fix the studio .gitignore first" % probe)


# ------------------------------------------------------------------------------------- the clone

def clone(root, source, dest, *, upstream, reference=None) -> None:
    """`git clone` of `source` into `dest` (a folder that must not exist), safe by construction:
    no hard links or alternates (--no-local), never --depth or --filter (the push barrier refuses a
    shallow history), no template folder (--template= installs no hook), no prompt. Raises AttachError; the caller removes a failed `dest`.
    `reference` names a full clone made earlier (the first chat's read-only copy): its objects are used while cloning
    and then copied in (`--dissociate`), so the new clone borrows nothing and the download is mostly saved."""
    extra = ["--reference", str(reference), "--dissociate"] if reference else []
    gitops.remote(root, "clone", "--no-local", "--template=", *extra, "--origin", upstream, "--", source, str(dest),
                  url=source, timeout=CLONE_TIMEOUT)
    # --template= leaves out the hooks folder; an empty one is the shape every tool that looks for a hook expects
    try:
        safefs.make_dir(root, os.path.join(str(dest), ".git", "hooks"))
    except AttachError:
        pass


def _clone_problems(dest, display) -> list:
    dot = os.path.join(str(dest), ".git")
    if os.path.islink(dot) or not os.path.isdir(dot):
        return ["the clone has no .git folder of its own"]
    return gitops.clone_problems(dest, "the clone")


def _remove_clone(root, repo, created) -> None:
    """Remove the folder this run created, and only that."""
    if not created:
        return
    path = os.path.join(os.path.realpath(str(root)), repo)
    if os.path.islink(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def _resolve_base(app, upstream, wanted):
    """(base branch, its commit sha) of the fetched upstream."""
    base = wanted
    if not base:
        head = gitops.git(app, "symbolic-ref", "-q", "refs/remotes/%s/HEAD" % upstream, check=False)
        prefix = "refs/remotes/%s/" % upstream
        base = head[len(prefix):] if head.startswith(prefix) else config.DEFAULTS["base"]
    if config._bad_base(base):
        raise AttachError("%s is not a valid branch name" % base)
    sha = gitops.git(app, "rev-parse", "--verify", "-q", "refs/remotes/%s/%s^{commit}" % (upstream, base), check=False)
    if not sha:
        raise AttachError("the upstream has no branch %s; pass --base with the branch work should start from" % base)
    return base, sha


def _on_base(app, upstream, base) -> None:
    """Leave the clone on a local branch that tracks upstream/base, so app/ stays on the base."""
    if gitops.git(app, "symbolic-ref", "-q", "--short", "HEAD", check=False) != base:
        gitops.git(app, "checkout", "-q", "-B", base, "--track", "%s/%s" % (upstream, base))


# ------------------------------------------------------------------------------- the attach step

def read_regular(path, what):
    """The bytes of the regular file at `path`, at most MAX_ANSWERS_BYTES (a file with no end, /dev/zero, must
    not fill memory). A FIFO, a device or a folder is refused without being read: the file is opened without
    waiting for a writer, so a pipe nobody writes to cannot hold the attach (and its lock) forever."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise AttachError("the %s cannot be read: %s" % (what, exc.strerror)) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AttachError("the %s is not a regular file; give a file, not a pipe, a device or a folder" % what)
        chunks, total = [], 0                    # os.read on the one checked descriptor: the read guard sees it
        while total <= MAX_ANSWERS_BYTES:
            chunk = os.read(fd, MAX_ANSWERS_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
    except OSError as exc:
        raise AttachError("the %s cannot be read: %s" % (what, exc.strerror)) from None
    finally:
        os.close(fd)
    if len(data) > MAX_ANSWERS_BYTES:
        raise AttachError("the %s is larger than %d bytes" % (what, MAX_ANSWERS_BYTES))
    return data


def check_regular(path, what):
    """Refuse at once, reading none of it, a `path` that is not a regular file of at most MAX_ANSWERS_BYTES. `attach
    init` asks this before its setup gate, so a pipe, a device or a folder given as --answers or --gates-file is
    refused by the same reader text whoever calls (t-647 follow-up 1). `read_regular` checks again when it reads."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise AttachError("the %s cannot be read: %s" % (what, exc.strerror)) from None
    try:
        st = os.fstat(fd)
    except OSError as exc:
        raise AttachError("the %s cannot be read: %s" % (what, exc.strerror)) from None
    finally:
        os.close(fd)
    if not stat.S_ISREG(st.st_mode):
        raise AttachError("the %s is not a regular file; give a file, not a pipe, a device or a folder" % what)
    if st.st_size > MAX_ANSWERS_BYTES:
        raise AttachError("the %s is larger than %d bytes" % (what, MAX_ANSWERS_BYTES))


def _read_answers(answers):
    if answers is None:
        return None
    path = Path(str(answers))
    data = read_regular(path, "answers file")
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "text": data.decode("utf-8", "replace")}


@contextlib.contextmanager
def studio_lock(root):
    """Hold the studio-wide attach lock: an exclusive, non-blocking `flock` on the `.alpaca` folder.
    It leaves no file behind and the kernel drops it when the process ends, even on SIGKILL. A second
    attach in the same studio, from this process or another, is refused with AttachError instead of
    queued, so the checks below always see a studio no other attach is changing."""
    path = os.path.join(str(root), ".alpaca")
    if os.path.islink(path):
        raise AttachError(".alpaca is a link; attach keeps its files only inside the studio")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        raise AttachError("the studio is not onboarded yet; run alpaca onboard first") from None
    except OSError as exc:
        raise AttachError(".alpaca cannot be opened for the attach lock: %s" % exc.strerror) from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise AttachError("another alpaca attach init is running in this studio; wait for it to "
                                  "finish, then run this again") from None
            raise AttachError(".alpaca cannot be locked for the attach: %s" % exc.strerror) from None
        yield
    finally:
        os.close(fd)


def _file_bytes(root, path):
    """The bytes of a plain file inside the studio, or None when there is none. A file that is there but
    cannot be used (a link, a folder, mode 000) raises AttachError, so None always means absent."""
    return safefs.read_existing(root, path)


def _put_bytes(root, path, data) -> None:
    """Put `data` back at `path` (a rollback step; a failure is dropped, the error that started the rollback is the one to show)."""
    try:
        safefs.write_bytes(root, path, data, mode=0o600)
    except AttachError:
        pass


def _remove_inside(root, path, *, tree=False) -> None:
    """Delete `path`, a file or an empty folder (the whole folder when `tree`), but only when every folder
    of its chain, from the real studio root down, is a plain folder: the delete runs on the real parent,
    opened with `O_NOFOLLOW`, through `dir_fd`, so a folder swapped for a link during the run is never
    followed out of the studio. Nothing there is a success. Raises AttachError, and deletes nothing,
    when a folder of the chain is a link or `path` itself is a link (a link at a name this run made was
    not made by this run), and OSError when the system refuses the delete."""
    if not os.path.lexists(str(path)):
        return
    parent, name = safefs.chain(root, path)
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if os.path.realpath(parent) != parent or not os.path.samestat(os.fstat(fd), os.stat(parent)):
            raise AttachError(safefs.LINK % name)      # a folder of the chain was swapped after the check
        try:
            st = os.lstat(name, dir_fd=fd)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(st.st_mode):
            raise AttachError(safefs.LINK % name)
        if stat.S_ISDIR(st.st_mode):
            if tree:
                shutil.rmtree(name, ignore_errors=True, dir_fd=fd)
            else:
                os.rmdir(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)
    finally:
        os.close(fd)


def _try_remove(root, path, notes, what, *, tree=False) -> None:
    """A rollback delete: a refusal for a link is noted for the owner, any other failure is dropped (the
    error that started the rollback is the one to show)."""
    try:
        _remove_inside(root, path, tree=tree)
    except AttachError as exc:
        notes.append("alpaca attach init: WARN %s was not removed (%s)" % (what, gitops.scrub(exc)))
    except OSError:
        pass


def _undo_made(root, made, notes=None) -> None:
    """Remove the files and then the folders (deepest first, only when empty) a run made."""
    notes = [] if notes is None else notes
    for path in made:
        _try_remove(root, path, notes, os.path.basename(str(path)))


def attach(root, source, *, repo="app", upstream="origin", base=None, branch_prefix="alpaca/", gates=None,
           answers=None, session="cli", gates_source=None, reference=None, card=None, pr_footer=None,
           commit_name=None, commit_email=None):
    """Attach `source` to the studio at `root`. Returns the AttachConfig now written to project.yaml.
    `gates_source` says where passed-in gates came from (the gates file's path and digest); it is
    recorded on the attach-init event so a reader can tell sensed gates from supplied ones. The whole
    step runs under `studio_lock`: of two attach runs in one studio, whatever their `--repo` names,
    one finishes and the other is refused. `reference` (a full clone made earlier) saves the download; `card` is
    the first-chat card this attach was confirmed by, recorded on the attach-init event. `pr_footer` (False turns the
    generated section of the pull request body off) and `commit_name` with `commit_email` (the commit identity the owner
    chose) are written to the block only when given; unset, the block holds neither and the defaults apply."""
    with _SigintHold() as hold:
        with studio_lock(root):
            try:
                return _attach_locked(root, source, repo=repo, upstream=upstream, base=base,
                                      branch_prefix=branch_prefix, gates=gates, answers=answers, session=session,
                                      gates_source=gates_source, reference=reference, card=card, hold=hold,
                                      pr_footer=pr_footer, commit_name=commit_name, commit_email=commit_email)
            finally:
                hold.on = True                       # first statement: the lock release and the restore run held


class _SigintHold:
    """The Ctrl-C policy of one attach run: held by default, acted on only where the rollback knows every
    change the run has made so far (a released region).

    While `on` is True a SIGINT is only noted (`held`). `release()` turns the hold off and replays a noted
    Ctrl-C through the caller's handler (the default one raises KeyboardInterrupt); `check()` replays one
    and keeps the hold as it was. In a released region the first SIGINT turns the hold back on and then
    acts, so the unwinding it starts (the clean-up of a git child, the rollback) runs held.

    The rule that makes the hold work: turning it on is a plain attribute store (`hold.on = True`), put as the
    first statement of an `except` or `finally` on the run's exit paths. In CPython 3.14.4 the bytecode from
    the catch to that store has no call, function entry or backward jump, the points where a Python signal
    handler runs (fix-r3-dis1.log); test_attach_interrupts.py checks the form and that bytecode. The store
    must stay a plain store, not a method call or a property.

    `on` starts True and is True when the handler is installed and when it is put back, so neither leaves a
    window. Nothing is installed off the main thread (Python runs signal handlers there only) or when the
    caller's handler is not a Python callable (SIG_IGN and SIG_DFL stay as they are); the flags then change
    nothing. `kept` is set by _commit right after the record COMMIT: from then on the run is attached,
    whatever stops it."""

    def __init__(self):
        self.on = True
        self.held = False
        self.kept = False
        self.old = None
        self.installed = False

    def _handler(self, signum, frame):
        if self.on:
            self.held = True
            return
        self.on = True                               # the first Ctrl-C of a released region acts; later ones wait
        self.old(signum, frame)

    def __enter__(self):
        if threading.current_thread() is threading.main_thread():
            old = signal.getsignal(signal.SIGINT)
            if callable(old):
                self.old = old
                signal.signal(signal.SIGINT, self._handler)
                self.installed = True
        return self

    def __exit__(self, et, ev, tb):
        self.on = True
        if self.installed:
            signal.signal(signal.SIGINT, self.old)
            self.installed = False
        if et is None or not issubclass(et, KeyboardInterrupt):
            self.check()                             # a Ctrl-C held to the end is not lost
        return False

    def check(self):
        """Replay a Ctrl-C noted while the hold was on through the caller's handler."""
        while self.held:
            was = self.on
            self.on = True
            self.held = False
            self.old(signal.SIGINT, None)
            self.on = was

    def release(self):
        """From here a Ctrl-C acts at once. Call it only where the rollback knows every change made so far."""
        self.on = False
        self.check()


# The Ctrl-C map of an attach run (`hold` is the _SigintHold of attach()):
#   released  the checks before the claim (they only read, and --answers may be a FIFO that blocks)
#   held      the claim of <repo>/ up to `created = True`; ensure_ignored; then released for the clone, its
#             checks, base, sensing and validation (they write only inside <repo>/, which `created` owns)
#   held      configure (state.json, the terms file, their temp files) up to `record_started = True`
#   released  only for the call of _commit, which holds again as its first statement
#   held      from the first statement of the `except` to the end of the rollback, and from the COMMIT on
#   released  after the try: the attach is kept and `alpaca attach baseline` can rerun the baseline
# Rule for later code: a new write in the attach run goes in a held region or has its ledger entry set
# before it, and `hold.release()` goes only where the ledger names every change made so far.
def _attach_locked(root, source, *, repo, upstream, base, branch_prefix, gates, answers, session, gates_source,
                   hold, reference=None, card=None, pr_footer=None, commit_name=None, commit_email=None):
    from alpaca import project
    root = str(root)
    hold.release()                                   # released: the checks only read
    if config.raw(root) is not config.MISSING:
        raise AttachError("this project is already attached (project.yaml has an attach: block)")
    if not project.is_onboarded(root):
        raise AttachError("the studio is not onboarded yet; run alpaca onboard first")
    problem = config.state_dir_problem(root)
    if problem:
        raise AttachError(problem)
    state_file = config.state_dir(root) / "state.json"
    if os.path.islink(state_file):
        raise AttachError("%s is a link; attach keeps its files only inside the studio" % "/".join(
            (STATE_DIR, "state.json")))
    display = gitops.validate_source(source, root)
    if base is not None and not str(base).strip():
        raise AttachError("--base is empty; name the branch work should start from, or leave --base out")
    pre = {"repo": repo, "url": display, "upstream": upstream, "base": base or config.DEFAULTS["base"],
           "branch_prefix": branch_prefix, "gates": [{"name": "placeholder", "command": ["true"]}]}
    early = [e for e in config.validate(pre, root) if "attach.url" not in e]
    if early:
        raise AttachError(early[0])
    answers_info = _read_answers(answers)
    real_root = os.path.realpath(root)
    dest = os.path.join(real_root, repo)
    if os.path.lexists(dest):
        raise AttachError("%s already exists in the studio; choose another --repo or remove it first" % repo)
    kind = gitops.studio_kind(root)
    if kind == "link":
        raise AttachError(LINKED)
    if kind == "borrowed":
        raise AttachError(_BORROWED)
    if kind == "own" and _exclude_problem(root):
        raise AttachError(_exclude_problem(root))
    # What the rollback must put back is read first, and a file that is there but cannot be read refuses the
    # attach before anything is written (an unreadable file read as "absent" would be deleted by the rollback).
    before_exclude = _read_exclude(root) if kind == "own" else None
    before_state = _file_bytes(root, state_file)
    yaml_file = project.path(root)
    before_yaml = _file_bytes(root, yaml_file)
    touched_exclude = False                          # set before the exclude file may change (the proof can still fail)
    record_started = False                           # set before project.yaml may change (the COMMIT can still fail)
    had_state_dir = config.state_dir(root).exists()
    made = []                                        # files and folders this run made outside the clone
    created = False                                  # set by the claim below; undo() reads it when it runs

    def undo():
        # Every step stands alone: whatever a step raises (a file that became a link, a folder that cannot be
        # removed, anything else, a second Ctrl-C included) is noted and the steps after it still run. There
        # is no signal mask: the run's SIGINT hold (_SigintHold) is on from the first statement of the
        # `except` that calls undo() to the end of the run, so a Ctrl-C that arrives between two steps is only
        # noted and comes out after the lock is released. A KeyboardInterrupt or SystemExit that a step raises
        # (code, or a thread that is not the main one) is held here: the first one is raised after the last
        # step and replaces the error that started the rollback. What the owner must know is collected and
        # printed once, after the last step, and a stdout that fails cannot stop undo().
        notes = []
        pending = []                                 # the first interrupt held until the last step has run

        def step(what, fn):
            try:
                fn()
            except Exception as exc:                 # noqa: BLE001 - no step may end the ones after it
                notes.append("alpaca attach init: WARN %s was not undone (%s)" % (what, gitops.scrub(exc)))
            except BaseException as exc:             # noqa: BLE001 - a second Ctrl-C or an exit is held, not obeyed
                pending.append(exc)
                notes.append("alpaca attach init: WARN %s was not fully undone (%s interrupted it)"
                             % (what, type(exc).__name__))

        def put_back_exclude():
            try:
                now = _read_exclude(root)
            except AttachError:
                now = False                          # unreadable now: try the put-back, which refuses a link itself
            if now != before_exclude:
                _restore_exclude(root, before_exclude)

        def put_back_yaml():
            # project.yaml is written inside the record transaction, before its COMMIT; when the COMMIT fails
            # the record rolls back, so the file must go back too, or the studio keeps attach: with no attach
            try:
                if _file_bytes(root, yaml_file) != before_yaml:
                    safefs.write_bytes(root, yaml_file, before_yaml)
            except AttachError as exc:
                notes.append("alpaca attach init: WARN project.yaml could not be put back (%s); remove its attach: "
                             "block by hand, then run attach init again" % gitops.scrub(exc))

        def put_back_state():
            try:
                now_state = _file_bytes(root, state_file)
            except AttachError:
                now_state = False                    # unreadable now: the put-back below refuses a link itself
            if now_state != before_state:
                _put_bytes(root, state_file, before_state)

        step("the clone", lambda: _remove_clone(root, repo, created))
        if touched_exclude:
            step("the exclude file", put_back_exclude)
        if record_started and before_yaml is not None:
            step("project.yaml", put_back_yaml)
        if before_state is not None:
            step("state.json", put_back_state)
        else:
            step("state.json", lambda: _try_remove(root, state_file, notes, "state.json"))
        if not had_state_dir:
            step("the state folder",
                 lambda: _try_remove(root, config.state_dir(root), notes, STATE_DIR, tree=True))
        step("the files this run made", lambda: _undo_made(root, reversed(made), notes))
        try:
            for line in notes:
                print(line)
        except Exception:                        # noqa: BLE001 - a closed pipe must not end the rollback
            pass
        except BaseException as exc:             # noqa: BLE001 - held like a step's interrupt
            pending.append(exc)
        if pending:
            raise pending[0]

    hold.on = True                                   # held: from the claim until `created` says it exists
    # Claim the folder before anything else is written, so that a folder made by something that is not an
    # attach run (the lock only holds attach runs apart) still stops this run before it writes. A refusal
    # here runs no rollback: nothing of this run exists yet.
    try:
        dest = safefs.make_dir(root, dest, exclusive=True)
    except FileExistsError:
        raise AttachError("%s already exists in the studio; choose another --repo or remove it first" % repo) from None
    except safefs.WriteFailed as exc:
        raise AttachError("%s/ cannot be created in the studio: %s" % (repo, exc.strerror)) from None
    created = True

    try:
        touched_exclude = kind == "own"
        ensure_ignored(root, repo)                   # still held: the exclude file and its temp file
        hold.release()                               # released: the clone and its reads write only inside app/
        clone(root, display, dest, upstream=upstream, reference=reference)
        problems = _clone_problems(dest, display)
        if problems:
            raise AttachError(problems[0])
        chosen, base_sha = _resolve_base(dest, upstream, base)
        _on_base(dest, upstream, chosen)
        if gates is None:
            from alpaca.attach import sense
            gates, traces = sense.sense_gates(dest, repo)
        else:
            traces = {str(g.get("name")): "gates-file" for g in gates if isinstance(g, dict)}
            gates_source = gates_source or "supplied by the caller"
        if not gates:
            raise AttachError("no attach gates sensed from the repository; pass --gates-file with a JSON "
                              "list of {name, command, timeout} (the command is an argv list)")
        block = {"repo": repo, "url": display, "repository": gitops.repository_of(display), "upstream": upstream,
                 "base": chosen, "branch_prefix": branch_prefix, "gates": gates}
        if pr_footer is not None:
            block["pr_footer"] = bool(pr_footer)
        if commit_name is not None or commit_email is not None:
            block["commit_name"], block["commit_email"] = commit_name, commit_email
        errors = config.validate(block, root)
        if errors:
            raise AttachError(errors[0])
        cfg = config._build(block)
        block["gates"] = [dict(g) for g in cfg.gates]
        hold.on = True                               # held: state.json, the terms file, their temp files
        state = configure(root, cfg, display, base_sha=base_sha, made=made)
        record_started = True
        hold.release()                               # released: the ledger is complete; _commit holds again itself
        _commit(root, block, cfg, state, traces, answers_info, session,
                gates_source or "sensed from the repository files", card, hold=hold)
    except BaseException as exc:
        hold.on = True                               # first statement: no Ctrl-C can cut what follows
        if hold.kept:                                # the record kept the attach: nothing to undo
            raise
        undo()
        if isinstance(exc, sqlite3.Error):
            raise AttachError("attach could not write the record (%s); the studio is as it was, run attach "
                              "init again" % gitops.scrub(exc)) from None
        if isinstance(exc, OSError):
            raise AttachError("attach could not write %s: %s"
                              % (gitops.scrub(exc.filename or "a studio file"), exc.strerror or exc)) from None
        raise
    hold.release()                                   # released: the attach is kept, the baseline can be rerun
    _baseline(root, base_sha, session)
    return cfg


_SCP_LIKE = re.compile(r"^(?:(?P<user>[^@/:\s]+)@)?(?P<host>[^/:@\s]+):(?!//)(?P<path>[^\s]*)$")
_SCHEME = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?:(?P<user>[^@/\s]*)@)?(?P<host>[^/\s]*)(?P<path>/[^\s]*)?$")


def _with_variants(forms, out) -> None:
    """Append each of `forms` to `out`, with a trailing `.git` and a trailing slash, when it is not there yet."""
    for form in forms:
        stem = form.rstrip("/")
        stem = stem[:-4] if stem.endswith(".git") else stem
        if not stem:
            continue
        for item in (form, stem, stem + ".git", stem + "/"):
            if item not in out:
                out.append(item)


def _lower_github_path(url):
    """`url` with the owner/repo part lower-cased when its host is github.com and that part has capitals, else None.
    GitHub ignores the case of owner and repo names, and a prefix rule cannot, so the lower-cased spelling is the one
    extra form that is cheap to hold."""
    m = _SCHEME.match(url)
    if m and (m.group("host") or "").lower().split(":")[0] == "github.com" and m.group("path"):
        low = url[:m.start("path")] + m.group("path").lower()
    else:
        m = _SCP_LIKE.match(url) if "://" not in url else None
        if not m or m.group("host").lower() != "github.com":
            return None
        low = url[:m.start("path")] + m.group("path").lower()
    return low if low != url else None


def push_url_forms(url) -> list:
    """The forms of `_forms_of`, and for a github.com address with capitals in owner/repo also the forms of its
    lower-cased spelling (the only other-case spelling a prefix rule can hold)."""
    out = _forms_of(url)
    low = _lower_github_path((url or "").strip())
    for form in _forms_of(low) if low else []:
        if form not in out:
            out.append(form)
    return out


def _forms_of(url) -> list:
    """The spellings of the upstream's address that a push could name, each a prefix `url.<trap>.pushInsteadOf` rewrites
    (op-062 t-697): the address as it was configured, with and without a trailing `.git`, with a trailing slash,
    and for a host address the https, http, `ssh://` and scp-like `host:path` forms (with and without the `git@` user);
    for a folder the path and its `file://` form. git compares the prefix as written, so any other spelling of the same
    repository (user information, another case, `//`, `/./`, a port, a relative path) is not one of these: that residue
    is stated in docs/attach.md. The stem with no `.git` and no slash is a prefix of other names too (`widgetx.git`):
    git has no exact match, so a push to such a neighbour is refused as well. Returns a list with no repeats; empty
    when `url` is empty."""
    url = (url or "").strip()
    if not url:
        return []
    out: list = []
    bases: list = []
    m = _SCHEME.match(url)
    if m and m.group("scheme").lower() == "file":
        path = m.group("path") or ""
        bases += [url, path] if path else [url]
        bases.append("file://" + path)
    elif m:
        host, path = m.group("host"), (m.group("path") or "").lstrip("/")
        bare_host = host.rsplit(":", 1)[0] if host.count(":") == 1 and host.rsplit(":", 1)[1].isdigit() else host
        bases.append(url)
        if path:
            bases += ["https://%s/%s" % (host, path), "http://%s/%s" % (host, path), "ssh://git@%s/%s" % (host, path),
                      "ssh://%s/%s" % (host, path), "git://%s/%s" % (host, path)]
            user = m.group("user")
            if user and m.group("scheme").lower().startswith("ssh"):
                bases.append("ssh://%s@%s/%s" % (user, host, path))
                bases.append("%s@%s:%s" % (user, bare_host, path))
            bases += ["git@%s:%s" % (bare_host, path), "%s:%s" % (bare_host, path)]
    elif _SCP_LIKE.match(url) and not url.startswith(("/", ".")):
        m = _SCP_LIKE.match(url)
        host, path = m.group("host"), m.group("path").lstrip("/")
        bases.append(url)
        if path:
            bases += ["git@%s:%s" % (host, path), "%s:%s" % (host, path), "ssh://git@%s/%s" % (host, path),
                      "ssh://%s/%s" % (host, path), "https://%s/%s" % (host, path), "http://%s/%s" % (host, path),
                      "git://%s/%s" % (host, path)]
            if m.group("user") and m.group("user") != "git":
                bases += ["%s@%s:%s" % (m.group("user"), host, path), "ssh://%s@%s/%s" % (m.group("user"), host, path)]
    else:
        bases.append(url)
        real = os.path.realpath(url) if os.path.isabs(url) or os.path.exists(url) else None
        for path in [x for x in (url, real) if x]:
            if path not in bases:
                bases.append(path)
            if os.path.isabs(path):
                bases.append("file://" + path)
    _with_variants(bases, out)
    return out


def set_push_rewrites(app, upstream) -> list:
    """Write one `url.<trap>.pushInsteadOf` entry in the clone's local config for every spelling of the upstream's
    address (`push_url_forms`), after removing the ones an earlier run wrote. A push that spells the address out, in
    a word built at run time that the text guard cannot read, is then rewritten to the trap, as a push by the remote's
    name is by `remote.<upstream>.pushurl`. Returns the forms written. The handoff push runs from a bare folder
    with a config of its own, which reads none of this."""
    url = gitops.git(app, "config", "--local", "--get", "remote.%s.url" % upstream, check=False)
    forms = push_url_forms(url)
    gitops.git(app, "config", "--local", "--unset-all", PUSH_REWRITE_KEY, check=False)
    for form in forms:
        gitops.git(app, "config", "--local", "--add", PUSH_REWRITE_KEY, form)
    return forms


def push_rewrites(app) -> list:
    """The `pushInsteadOf` values now set for the trap in the clone's local config."""
    out = gitops.git(app, "config", "--local", "--get-all", PUSH_REWRITE_KEY, check=False)
    return [x for x in out.split("\n") if x]


def configure(root, cfg, source_display, *, base_sha=None, made=None) -> dict:
    """Everything attach sets up around a fresh clone except project.yaml: the ignore proof (P2),
    the push trap (P6, `remote.<upstream>.pushurl` and the `pushInsteadOf` entries of t-697), the commit identity (P13), the config digest and state.json (P8) and the
    sealed-term minimum (P23). Returns the state dict it wrote. `made`, a list, receives the terms
    file and the folders made for it, so the caller can take them away again."""
    from alpaca import util
    app = str(cfg.repo_path(root))
    ensure_ignored(root, cfg.repo)
    gitops.git(app, "config", "--local", "remote.%s.pushurl" % cfg.upstream, PUSH_TRAP)
    set_push_rewrites(app, cfg.upstream)
    gitops.git(app, "config", "--local", "user.name", cfg.identity[0])
    gitops.git(app, "config", "--local", "user.email", cfg.identity[1])
    if base_sha is None:
        base_sha = gitops.git(app, "rev-parse", "--verify", "refs/remotes/%s/%s^{commit}" % (cfg.upstream, cfg.base))
    state = {"schema": 1, "url": source_display, "fetch_url": source_display,
             "config_digest": gitops.config_digest(app), "attached_at": util.now_iso(),
             "base_sha_at_attach": base_sha, "repo": cfg.repo, "upstream": cfg.upstream, "base": cfg.base,
             "root": os.path.realpath(str(root))}
    config.write_json_atomic(config.state_dir(root) / "state.json", state)
    write_terms(root, repo=cfg.repo, made=made)
    return state


def write_terms(root, repo=None, made=None) -> str | None:
    """The minimum sealed terms (build plan 4.3), written only when project.yaml's barrier names a
    term file inside the studio and that file is missing. Returns the relative path, or None. Raises
    AttachError, and writes nothing, when the named path passes through a link or a folder that is
    not a plain part of the studio, or lies inside the attached clone `repo`. When `made` is a list,
    the folders this call creates and then the file are appended to it, in the order made."""
    from alpaca import project
    from alpaca.release import config as rconfig
    cfg = project.load(root)
    barrier = cfg.get("barrier") if isinstance(cfg.get("barrier"), dict) else {}
    named = barrier.get("terms")
    if not isinstance(named, str) or not named or os.path.isabs(named) or ".." in Path(named).parts:
        return None
    if repo and Path(named).parts[:1] == (repo,):
        raise AttachError("barrier.terms %s lies inside the attached clone %s/, which receives no Alpaca file" % (named, repo))
    try:
        target = str(rconfig.contained(os.path.realpath(str(root)), named))
    except ValueError as exc:
        raise AttachError("barrier.terms %s cannot be written inside the studio (%s)" % (named, exc)) from None
    if os.path.lexists(target):
        return None
    lines = ["# attach minimum terms (op-062); add your own below",
             os.path.realpath(str(root)), os.path.realpath(os.path.expanduser("~"))]
    if cfg.get("project_id"):
        lines.append(str(cfg["project_id"]))
    lines += [r"re: /var/tmp/claude-[0-9]+/",
              r"re: \b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
              r"re: \bop-[0-9]{3,}\b"]
    try:
        safefs.write_bytes(os.path.realpath(str(root)), target, ("\n".join(lines) + "\n").encode("utf-8"),
                           create_only=True, made=made)
    except safefs.WriteFailed as exc:
        raise AttachError("barrier.terms %s cannot be created: %s" % (named, exc.strerror)) from None
    return named


def _commit(root, block, cfg, state, traces, answers_info, session, gates_source, card=None, *, hold) -> None:
    """project.yaml and the attach-init event land together or not at all."""
    hold.on = True                                   # held: the yaml write, the COMMIT and `kept` go together
    from alpaca import db, project
    if config.raw(root) is not config.MISSING:
        raise AttachError("this project is already attached (project.yaml has an attach: block)")
    conn = db.connect(str(root))
    doc = project.load(root)
    doc["existing_repo"] = True
    paths_block = dict(doc.get("paths") or {})
    paths_block["product_tree"] = cfg.repo
    doc["paths"] = paths_block
    doc["attach"] = block
    data = {"repo": cfg.repo, "url": cfg.url, "repository": cfg.repository, "upstream": cfg.upstream,
            "base": cfg.base, "base_sha": state["base_sha_at_attach"], "branch_prefix": cfg.branch_prefix,
            "gates": [dict(g) for g in cfg.gates], "traces": traces, "gates_source": gates_source,
            "config_digest": state["config_digest"], "answers": answers_info,
            "commit_identity": {"name": cfg.identity[0], "email": cfg.identity[1]}, "pr_footer": cfg.pr_footer}
    if card:
        data["card"] = card
    try:
        with db.transaction(conn):
            db.append_event(conn, session=session, actor="alpaca", kind="attach-init", op="op-0", data=data,
                            conn_in_txn=True)
            project.save(root, doc)
            hold.check()                             # a Ctrl-C held so far rolls back before the COMMIT
        hold.kept = True
    except BaseException:
        # a COMMIT that fails leaves the transaction open; roll it back here so the event cannot be kept
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _baseline(root, base_sha, session) -> None:
    """The onboarding baseline (t-648), when that module exists. A failure is reported, not fatal:
    the attach itself is already written and `alpaca attach baseline` runs it again."""
    import importlib.util
    if importlib.util.find_spec("alpaca.attach.baseline") is None:
        return
    from alpaca.attach import baseline
    try:
        baseline.ensure(root, base_sha, op="op-0", session=session)
    except Exception as exc:                                       # noqa: BLE001 - reported, not hidden
        print("alpaca attach init: WARN baseline not recorded: %s" % gitops.scrub(exc))


# -------------------------------------------------------------------------------------- the verb

def add_parser(verbs):
    p = verbs.add_parser("init", help="clone an existing repository into this studio and write attach:")
    p.add_argument("source", help="git URL or local path of the repository to attach")
    p.add_argument("--repo", default=config.DEFAULTS["repo"], help="folder of the clone in the studio (default app)")
    p.add_argument("--upstream", default=config.DEFAULTS["upstream"], help="remote name (default origin)")
    p.add_argument("--base", default=None, help="integration branch (default: the upstream's HEAD branch)")
    p.add_argument("--branch-prefix", default=config.DEFAULTS["branch_prefix"], help="work branch prefix (default alpaca/)")
    p.add_argument("--no-pr-footer", action="store_true", help="the pull request body gets no generated section "
                   "(default: it does); a handoff then sends the approved body text and a short gate summary")
    p.add_argument("--commit-name", default=None, help="the name every commit attach makes carries (give both "
                   "--commit-name and --commit-email; default: Alpaca agent)")
    p.add_argument("--commit-email", default=None, help="the email every commit attach makes carries "
                   "(default: alpaca-agent@localhost)")
    p.add_argument("--gates-file", default=None, help="JSON list of {name, command (argv), timeout}; replaces sensing")
    p.add_argument("--answers", default=None, help="the first-chat answers file to record on op-0")
    p.add_argument("--card", default=None, help="a setup card the owner confirmed in the first chat: the upstream, base, "
                                                "branch prefix and gates come from it (the other options are refused)")
    p.add_argument("--json", action="store_true", help="print one JSON object")


def _load_gates(path):
    """(gates, source text) from a JSON gates file; (None, None) without one."""
    import json
    if path is None:
        return None, None
    data = read_regular(path, "gates file")
    try:
        value = json.loads(data.decode("utf-8"))
    except ValueError as exc:
        raise AttachError("the gates file cannot be read as JSON: %s" % exc.__class__.__name__) from None
    if not isinstance(value, list):
        raise AttachError("the gates file must hold a JSON list of gates")
    return value, "gates file %s (sha256 %s)" % (path, hashlib.sha256(data).hexdigest())


def run(args) -> int:
    from alpaca import cli as alpaca_cli
    from alpaca.attach import init
    return init.run(args, alpaca_cli._root())


SUBVERBS = {"init": (add_parser, run)}
