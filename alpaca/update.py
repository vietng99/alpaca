"""alpaca update: move a project to the newest public release.

    alpaca update [--check] [--yes] [--repo URL] [--prefix DIR] [--bin-dir DIR] [--target DIR]

It reads the newest vN.N.N tag of the release repository (git ls-remote, never a prompt), compares
it with the project's own version, and when the tag is newer:

  1. installs that release next to the current one with the release's own install.sh: every file
     is checked against its MANIFEST.json sha256, the release lands in <prefix>/releases/<id>, and
     the <prefix>/current link moves last, so a failed install leaves the old release current;
  2. hands off to the newly installed release (an exec), so the code that replaces the project's
     files is never the code being replaced;
  3. shows the `alpaca upgrade` plan for the project, asks (unless --yes), saves every local file
     the upgrade would replace under <project>/.alpaca/update/<stamp>/, applies the upgrade, and
     checks the project's .venv and `alpaca doctor`.

--check reports the project, installed and newest versions and changes nothing. A refusal never
changes anything: offline, no release tag, a pending upgrade journal, a newest release older than
the project (no downgrade), a development tree, no terminal to ask on.

Exit codes: 0 PASS, 1 FAIL, 2 BLOCKED, 64 usage. The sha256 check proves a tree matches its own
MANIFEST.json, not who published it; the repository address is the trust anchor.

The flags --target, --prefix, --bin-dir, --repo, --handoff and --yes are the hand-off interface
between two releases of this verb. Keep them stable.
"""
import argparse
import contextlib
import fcntl
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from alpaca import VERSION as RUNNING_VERSION
from alpaca import cli, new, paths, releasetags, util

GATE, GATE_CHECK = "alpaca-update", "alpaca-update-check"
#: the tree this code runs from (an installed release, a project, or a development checkout)
INSTALL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER_MARK = "# alpaca launcher, written by Alpaca install.sh"
LS_REMOTE_TIMEOUT, CLONE_TIMEOUT = 30, 300
#: extra install.sh arguments; tests set ["--no-bootstrap"] so no test needs a package index
_INSTALL_EXTRA = []

PASS, FAIL, BLOCKED = 0, 1, 2


class _Refuse(Exception):
    def __init__(self, code, reason, evidence=None):
        super().__init__(reason)
        self.code, self.reason, self.evidence = code, reason, evidence


class _Mismatch(_Refuse):
    """The prefix and the launcher folder do not belong to one install. --check reports it too: the
    flags are wrong, not the host."""


def _blocked(reason):
    return _Refuse(BLOCKED, reason)


def _fail(reason):
    return _Refuse(FAIL, reason)


def say(text):
    print("alpaca update: %s" % text, flush=True)


# ------------------------------------------------------------------ seams (tests patch only these)
def _child_env():
    env = new._child_env()
    env = releasetags.git_env(env)
    env["ALPACA_UPDATE_ACTIVE"] = "1"
    return env


def _stop_group(proc, sig, grace):
    """Signal the child's whole process group (it leads its own session, so the group is the child and
    what it started) and wait up to `grace` seconds for it to end; SIGKILL when it does not."""
    if proc.poll() is not None:
        return                      # already reaped: its group id may belong to someone else now
    for step, wait in ((sig, grace), (signal.SIGKILL, None)):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, step)
        try:
            proc.wait(wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _run(argv, cwd=None, env=None, timeout=None, capture=None):
    """Run a command with its output on ours and no stdin; its exit code (127 when it cannot start,
    124 when it ran out of time). With `capture` (a list) the output is also appended to it as text.

    The child leads a session of its own, so a Ctrl-C at the terminal reaches this process only. On
    Ctrl-C the child's group gets SIGINT, so it runs its own cleanup (install.sh removes its stage and
    puts `current` back); it is killed only when it has not ended after 30 seconds. The interrupt is
    then raised again."""
    sys.stdout.flush()
    sys.stderr.flush()
    argv = [str(a) for a in argv]
    pipes = {} if capture is None else dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env if env is not None else _child_env(),
                                stdin=subprocess.DEVNULL, start_new_session=True, **pipes)
    except OSError as e:
        say("cannot run %s: %s" % (argv[0], e))
        return 127
    try:
        try:
            if capture is None:
                proc.wait(timeout)
            else:
                out, _ = proc.communicate(timeout=timeout)
                text = (out or b"").decode("utf-8", "replace")
                capture.append(text)
                sys.stdout.write(text)
                sys.stdout.flush()
            return proc.returncode
        except subprocess.TimeoutExpired:
            _stop_group(proc, signal.SIGTERM, 5)
            say("%s ran out of time after %ds" % (os.path.basename(argv[0]), timeout))
            return 124
        except KeyboardInterrupt:
            _stop_group(proc, signal.SIGINT, 30)
            raise
        except BaseException:
            _stop_group(proc, signal.SIGTERM, 5)
            raise
    finally:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()


def _tags(repo):
    return releasetags.ls_remote(repo, LS_REMOTE_TIMEOUT)


def _exec(path, argv, env):
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(path, argv, env)


def _interactive():
    return sys.stdin.isatty()


def _ask(question):
    try:
        return input(question + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ------------------------------------------------------------------ helpers
def _is_release_tree(root):
    """A release (project.yaml says `template: true`), not a project; the test install.sh uses."""
    try:
        with open(os.path.join(root, "project.yaml"), encoding="utf-8") as fh:
            return re.search(r"^template:[ \t]*true\b", fh.read(), re.M) is not None
    except (OSError, ValueError):
        return False


def _under(path, folder):
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return path == folder or path.startswith(folder + os.sep)


def find_project(target):
    """(root, explicit): the project to update, or (None, False) outside one. A relative --target is
    read from the folder the command was run in."""
    if target:
        root = os.path.realpath(util.from_caller(os.path.expanduser(target)))
        if not os.path.isfile(os.path.join(root, paths.MANIFEST)):
            raise _blocked("--target %s has no %s" % (root, paths.MANIFEST))
        return root, True
    try:
        return paths._walk(os.environ.get("ALPACA_CALLER_CWD") or os.getcwd()), False
    except paths.RootNotFound:
        return None, False


def _launcher_values(path):
    """(ALPACA_HOME, ALPACA_BIN_DIR) of a launcher install.sh wrote at `path`, else None. A value
    holding a quote is stored as '\\'' by install.sh; that is undone here."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    if LAUNCHER_MARK not in head:
        return None
    values = []
    for key in ("ALPACA_HOME", "ALPACA_BIN_DIR"):
        got = re.search(r"^%s='(.*)'$" % key, head, re.M)
        values.append(got.group(1).replace("'\\''", "'") if got else None)
    return tuple(values)


def _launchers():
    """(path, home, bin_dir) of every install.sh launcher on PATH, first match first."""
    seen, found = set(), []
    for entry in (os.environ.get("PATH") or "").split(os.pathsep):
        if not entry:
            continue
        path = os.path.join(entry, "alpaca")
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        values = _launcher_values(path)
        if values:
            found.append((path, values[0], values[1] or os.path.realpath(entry)))
    return found


def _default_prefix():
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "alpaca")


def find_install(prefix=None, bin_dir=None):
    """(prefix, bin_dir) of the installed Alpaca to update. Each miss is a refusal and writes nothing;
    update upgrades an install, it never makes the first one."""
    launchers = _launchers()
    if prefix:
        prefix = os.path.realpath(os.path.expanduser(util.from_caller(prefix)))
    else:
        if launchers and launchers[0][1]:
            prefix = os.path.realpath(launchers[0][1])
        elif os.path.exists(os.path.join(_default_prefix(), "current")):
            prefix = os.path.realpath(_default_prefix())
        else:
            raise _blocked("no installed Alpaca found; run install.sh first, or pass --prefix and --bin-dir")
    if bin_dir:
        bin_dir = os.path.realpath(os.path.expanduser(util.from_caller(bin_dir)))
    else:
        for path, home, where in launchers:
            if home and os.path.realpath(home) == prefix:
                bin_dir = where
                break
    if not bin_dir:
        raise _blocked("no launcher written by install.sh points at %s; pass --bin-dir" % prefix)
    if not os.path.exists(os.path.join(prefix, "current")):
        raise _blocked("%s has no current release; install with install.sh first" % prefix)
    # The prefix and the launcher folder must be one install: install.sh rewrites <bin_dir>/alpaca to
    # point at <prefix>, so a launcher folder of another install would be taken over.
    launcher = os.path.join(bin_dir, "alpaca")
    if os.path.lexists(launcher):
        values = _launcher_values(launcher)
        if values is None:
            raise _Mismatch(BLOCKED, "%s exists and was not written by install.sh; pass --bin-dir with the "
                            "launcher folder of %s (nothing was changed)" % (launcher, prefix))
        if not values[0] or os.path.realpath(values[0]) != prefix:
            raise _Mismatch(BLOCKED, "%s is the launcher of %s, not %s; pass --bin-dir with the launcher "
                            "folder of %s (nothing was changed)" % (launcher, values[0] or "an unknown prefix", prefix, prefix))
    return prefix, bin_dir


def _current(prefix):
    try:
        return os.readlink(os.path.join(prefix, "current"))
    except OSError:
        return None


def _restore_current(prefix, old):
    if old is None:
        return
    tmp = os.path.join(prefix, ".current.tmp")
    with contextlib.suppress(OSError):
        os.remove(tmp)
    os.symlink(old, tmp)
    os.replace(tmp, os.path.join(prefix, "current"))


def _read_launcher(bin_dir):
    """What <bin_dir>/alpaca is: ("link", target text) for a symlink (read or restored as the link, not
    as the bytes it reaches), (bytes, mode) for a file, None when there is none."""
    path = os.path.join(bin_dir, "alpaca")
    if os.path.islink(path):
        try:
            return ("link", os.readlink(path))
        except OSError as e:
            raise _blocked("cannot read the launcher %s: %s; nothing was changed" % (path, e))
    try:
        with open(path, "rb") as fh:
            return fh.read(), os.stat(path).st_mode & 0o7777
    except FileNotFoundError:
        return None
    except OSError as e:
        raise _blocked("cannot read the launcher %s: %s; nothing was changed" % (path, e))


def _restore_launcher(bin_dir, saved):
    """Put the launcher back to `saved` (a rename, so a reader never sees half a file); remove the file
    when there was none; make the link again when it was a link."""
    path = os.path.join(bin_dir, "alpaca")
    if saved is None:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
        return
    tmp = path + ".restore"
    if isinstance(saved[0], str):                    # ("link", target)
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp)
        os.symlink(saved[1], tmp)
        os.replace(tmp, path)
        return
    data, mode = saved
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _undo_install(prefix, bin_dir, old, saved):
    """Return `current` and the launcher to what they were before the install ran. (moved, problems):
    whether anything had to be put back, and one text per step that could not be (never raises).
    An `alpaca.new` that install.sh left because it was killed is removed too."""
    moved, problems = False, []
    if _current(prefix) != old:
        moved = True
        try:
            _restore_current(prefix, old)
        except OSError as e:
            problems.append("current is %s and could not be restored (%s); restore it with: ln -sfn %s %s"
                            % (_current(prefix), e, old, os.path.join(prefix, "current")))
    try:
        if _read_launcher(bin_dir) != saved:
            moved = True
            _restore_launcher(bin_dir, saved)
    except (OSError, _Refuse) as e:
        problems.append("the launcher %s could not be restored (%s)" % (os.path.join(bin_dir, "alpaca"), e))
    with contextlib.suppress(OSError):
        os.remove(os.path.join(bin_dir, "alpaca.new"))
    return moved, problems


def _installed_version(prefix):
    return releasetags.tree_version(os.path.join(prefix, "current")) if prefix else None


@contextlib.contextmanager
def _locked(prefix):
    lock = os.path.join(prefix, ".update.lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        raise _blocked("cannot write %s: %s; nothing was changed" % (lock, e))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise _blocked("another alpaca update holds %s" % lock)
    try:
        yield
    finally:
        os.close(fd)


def _check_project(root, prefix=None, locked=False):
    """The refusals that depend on the project alone (P1.2, P1.3). `locked` says the caller holds the
    project lock, so a journal here is a crashed upgrade, not one that is running."""
    if _is_release_tree(root) or (prefix and _under(root, os.path.join(prefix, "releases"))):
        raise _blocked("%s is an installed release, not a project; run alpaca update inside a project" % root)
    if releasetags.tree_channel(root) == "dev":
        raise _blocked("%s is a development tree (channel dev); it takes its code from git, not from a release" % root)
    from alpaca import upgrade
    if upgrade.pending(root):
        journal = upgrade.status(root) or {}
        if not locked and upgrade.busy(root):
            raise _blocked("an alpaca update or upgrade of %s is running now (phase %s); wait for it to end, "
                           "do not recover it; nothing was changed" % (root, journal.get("phase", "?")))
        raise _blocked("%s has a pending upgrade journal (phase %s); recover it first with bin/alpaca "
                       "upgrade --recover in that project; nothing was changed" % (root, journal.get("phase", "?")))
    if os.path.lexists(upgrade._journal_path(root)):
        raise _blocked("%s has an upgrade journal file that cannot be read (%s); recover it first with "
                       "bin/alpaca upgrade --recover in that project; nothing was changed"
                       % (root, upgrade._journal_path(root)))


def _repo_address(value):
    """The repository address as git will read it. A URL (`scheme://...`) and the scp-like `host:path`
    form (git's rule: a colon with no slash before it) are kept as they are; anything else is a path,
    read from the folder the command was run in, like --target, --prefix and --bin-dir (the launcher
    runs Python in the install root). `./a:b` is the way to name a local folder with a colon."""
    if "://" in value:
        return value
    colon = value.find(":")
    if colon > 0 and "/" not in value[:colon]:
        return value
    return os.path.abspath(util.from_caller(os.path.expanduser(value)))


def _compare(have, newest):
    """PASS reason when the project already has the newest release; a refusal when newest is older."""
    want, got = releasetags.parse_tag(newest), releasetags.parse_version(have)
    if got == want:
        return "already at the newest release %s" % newest
    if want < got:
        raise _blocked("the newest release %s is older than %s; no downgrade (older code refuses a "
                       "newer record, alpaca/migrate.py)" % (newest, have))
    return None


# ------------------------------------------------------------------ the verb
@contextlib.contextmanager
def _signals_as_interrupt():
    """SIGTERM and SIGHUP (a kill, a closed terminal) end an update the way Ctrl-C does, through
    KeyboardInterrupt, so the undo steps run; their default action would end the process at once and
    leave `current` where the installer put it. A signal the parent set to be ignored (nohup) stays
    ignored. Outside the main thread nothing is changed."""
    def handler(signum, frame):
        raise KeyboardInterrupt
    saved = []
    try:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            if signal.getsignal(sig) == signal.SIG_IGN:
                continue
            saved.append((sig, signal.signal(sig, handler)))
    except ValueError:
        pass
    try:
        yield
    finally:
        for sig, old in saved:
            signal.signal(sig, old)


def _cmd_update(args):
    from alpaca.gates import verdict as vc
    gate = GATE_CHECK if args.check else GATE
    try:
        with _signals_as_interrupt():
            code, reason, evidence = _update(args)
    except _Refuse as r:
        code, reason, evidence = r.code, r.reason, r.evidence
    except KeyboardInterrupt:
        # Ctrl-C outside the steps that name their own state: a verdict, not a traceback. Code 1 is the
        # failure code the verdict gate accepts.
        code, reason, evidence = FAIL, ("interrupted; run alpaca update again (an upgrade that was running is "
                                       "recovered on the project's next start)"), None
    return vc.emit_verdict(gate, code, reason, evidence)


def _update(args):
    # An installed release never writes a record into itself: this run's verdict row (alpaca/gates/
    # verdict.py) would otherwise create <release>/.alpaca/alpaca.db, because the launcher starts
    # the release with the release as its root.
    if _is_release_tree(INSTALL_ROOT):
        os.environ["ALPACA_RECORD_SKIP_ROOT"] = INSTALL_ROOT
    handoff = getattr(args, "handoff", None)
    if os.environ.get("ALPACA_UPDATE_ACTIVE") and not handoff:
        raise _blocked("alpaca update is already running in a parent process")

    root, explicit = find_project(args.target)
    if root and not explicit and _is_release_tree(root):
        root = None                      # the launcher ran outside a project: the release is no project
    if root:
        _check_project(root)

    if handoff:
        # Phase 2: the release is installed; the network is not needed again.
        prefix, bin_dir = find_install(args.prefix, args.bin_dir)
        return _phase2(args, root, prefix, bin_dir, handoff)

    # versions
    installed_for_have = None
    if not root:
        with contextlib.suppress(_Refuse):
            installed_for_have = _installed_version(find_install(args.prefix, args.bin_dir)[0])
    have = releasetags.tree_version(root) if root else (installed_for_have or RUNNING_VERSION)
    if not releasetags.parse_version(have):
        raise _blocked("cannot read the Alpaca version of %s" % (root or "the installed release"))
    if root:
        say("project %s is at %s" % (root, have))
    else:
        say("no project here; the installed release is %s" % have)
    if have != RUNNING_VERSION:
        say("running code %s" % RUNNING_VERSION)

    repo = args.repo or os.environ.get("ALPACA_UPDATE_REPO") or releasetags.DEFAULT_REPO
    if repo.startswith("-"):
        raise _blocked("refusing %r: a repository address cannot start with '-'" % repo)
    if re.match(r"(?i)(http|git)://", repo):
        raise _blocked("refusing %s: use https, ssh or a local path" % repo)
    repo = _repo_address(repo)
    try:
        tags = _tags(repo)
    except releasetags.Unreachable as e:
        raise _blocked("cannot read release tags from %s: %s; nothing was changed" % (repo, e))
    newest = releasetags.newest(tags)
    if not newest:
        raise _blocked("%s has no release tag (vX.Y.Z); nothing was changed" % repo)
    say("newest release %s at %s" % (newest, repo))

    same = _compare(have, newest)
    if same and not args.check:
        return PASS, same, None

    # the install
    prefix = bin_dir = None
    install_note = None
    try:
        prefix, bin_dir = find_install(args.prefix, args.bin_dir)
    except _Refuse as r:
        if not args.check or isinstance(r, _Mismatch):
            raise
        install_note = r.reason
    if prefix and root:
        _check_project(root, prefix)
    installed = _installed_version(prefix)
    if installed and releasetags.parse_version(installed) > releasetags.parse_tag(newest):
        raise _blocked("the installed release %s is newer than the newest tag %s; nothing was changed"
                       % (installed, newest))
    if prefix:
        say("installed release %s (prefix %s, launcher folder %s)" % (installed or "unreadable", prefix, bin_dir))
    else:
        say("installed release unknown (%s)" % install_note)

    if args.check:
        if same:
            return PASS, same, None
        return PASS, "%s is newer than %s; run: alpaca update" % (newest, have), None

    if not args.yes and not _interactive():
        raise _blocked("no terminal to ask; pass --yes to update without asking; nothing was changed")

    if installed != newest[1:]:
        _install(args, prefix, bin_dir, repo, newest, installed)
    else:
        say("release %s is already installed" % newest)

    if not root:
        return PASS, "installed %s; run alpaca update inside each project" % newest, [prefix]

    current = os.path.realpath(os.path.join(prefix, "current"))
    if os.path.realpath(INSTALL_ROOT) != current:
        say("handing off to %s" % current)
        argv = ["alpaca", "update", "--target", root, "--prefix", prefix, "--bin-dir", bin_dir,
                "--repo", repo, "--handoff", newest] + (["--yes"] if args.yes else [])
        env = releasetags.git_env(new._child_env())
        try:
            _exec(os.path.join(current, "bin", "alpaca"), argv, env)
        except OSError as e:
            raise _fail("could not hand off to %s: %s; %s is installed and current, the project is unchanged; "
                        "run alpaca update again" % (current, e, newest))
        raise _fail("could not hand off to %s" % current)    # only reached when _exec returns
    return _phase2(args, root, prefix, bin_dir, newest)


def _install(args, prefix, bin_dir, repo, tag, installed):
    """Install `tag` with its own install.sh. The install commits two things, the `current` link and the
    launcher file; when it fails, or the result is not usable, or this process is interrupted, both are
    put back to what they were, so a failed install leaves the old release current."""
    with _locked(prefix):
        if _installed_version(prefix) == tag[1:]:
            return
        if not args.yes and not _ask("Install %s into %s (current: %s), then show the upgrade plan?"
                                     % (tag, prefix, installed or "none")):
            raise _blocked("declined; nothing changed")
        old = _current(prefix)
        saved = _read_launcher(bin_dir)
        try:
            stage = tempfile.mkdtemp(dir=prefix, prefix=".update.")
        except OSError as e:
            raise _blocked("cannot write under %s: %s; nothing was changed" % (prefix, e))
        installing = False
        try:
            tree = os.path.join(stage, "tree")
            env = _child_env()
            say("fetching %s (%s)" % (repo, tag))
            rc = _run(["git", "-c", "credential.helper=", "clone", "-q", "--depth", "1", "--branch", tag,
                       "--", repo, tree], env=env, timeout=CLONE_TIMEOUT)
            if rc != 0:
                raise _fail("git clone of %s at %s failed (exit %d); the current release is unchanged "
                            "(current -> %s)" % (repo, tag, rc, old))
            got = releasetags.tree_version(tree)
            if got != tag[1:]:
                raise _fail("the tag %s holds version %s; nothing was installed; the current release is "
                            "unchanged (current -> %s)" % (tag, got, old))
            argv = ["bash", os.path.join(tree, "install.sh"), "--from", tree, "--prefix", prefix,
                    "--bin-dir", bin_dir]
            if os.environ.get("ALPACA_PYTHON"):
                argv += ["--python", os.environ["ALPACA_PYTHON"]]
            argv += list(_INSTALL_EXTRA)
            installing = True
            rc = _run(argv, env=env)
            problem = None
            new_root = os.path.realpath(os.path.join(prefix, "current"))
            if rc != 0:
                problem = "installing %s failed (exit %d)" % (tag, rc)
            elif releasetags.tree_version(new_root) != tag[1:]:
                problem = "the installed %s is not usable (it is version %s, not %s)" % (
                    tag, releasetags.tree_version(new_root), tag[1:])
            elif releasetags.tree_channel(new_root) != "release":
                problem = "the installed %s is not usable (its channel is %s, not release)" % (
                    tag, releasetags.tree_channel(new_root))
            elif _run([os.path.join(new_root, "bin", "alpaca"), "--version"], env=env, timeout=120) != 0:
                problem = "the installed %s is not usable (alpaca --version failed in it)" % tag
            elif "--no-bootstrap" not in argv:
                # --version answers before any module or package loads, so it cannot tell a release
                # whose .venv is half built; the bootstrap check can.
                check = _run(["bash", os.path.join(new_root, "setup", "bootstrap.sh"), "--check"],
                             cwd=new_root, env=env, timeout=300)
                if check != 0:
                    problem = ("the installed %s is not usable (its .venv is not ready: bootstrap.sh --check "
                               "exit %d)" % (tag, check))
            if problem:
                moved, problems = _undo_install(prefix, bin_dir, old, saved)
                if problems:
                    raise _fail("%s; %s" % (problem, "; ".join(problems)))
                if moved:
                    raise _fail("%s; current and the launcher are back as they were (current -> %s)" % (problem, old))
                raise _fail("%s; the current release is unchanged (current -> %s)" % (problem, old))
            say("installed %s at %s" % (tag, new_root))
        except KeyboardInterrupt:
            # Ctrl-C: install.sh got it too (see _run) and removed its stage; put the two things it
            # may have moved back, and say so instead of printing a traceback.
            problems = _undo_install(prefix, bin_dir, old, saved)[1] if installing else []
            if problems:
                raise _fail("interrupted during the install of %s; %s" % (tag, "; ".join(problems))) from None
            raise _fail("interrupted during the install of %s; current and the launcher are back as they were "
                        "(current -> %s)" % (tag, old)) from None
        except BaseException:
            # Any other error out of the install: leave the old release current. A step that already
            # restored things finds nothing to do here.
            if installing:
                _undo_install(prefix, bin_dir, old, saved)
            raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)


def _save_local_files(root, files):
    """Copy each local file to <root>/.alpaca/update/<UTC stamp>/<rel>; returns the folder."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    folder = os.path.join(root, ".alpaca", "update", stamp)
    n = 1
    while os.path.exists(folder):
        n += 1
        folder = os.path.join(root, ".alpaca", "update", "%s-%d" % (stamp, n))
    try:
        for rel in files:
            src, dst = os.path.join(root, rel), os.path.join(folder, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.islink(src):
                os.symlink(os.readlink(src), dst)
            else:
                shutil.copy2(src, dst)
            say("saved local file: %s" % rel)
    except OSError:
        shutil.rmtree(folder, ignore_errors=True)       # half a copy is no copy; the originals are all still live
        raise
    say("saved %d local file(s) to %s" % (len(files), os.path.relpath(folder, root) + "/"))
    return folder


def _phase2(args, root, prefix, bin_dir, tag):
    """Inside the newly installed release: plan, ask, save, apply, prove."""
    from alpaca import upgrade
    if not root:
        raise _blocked("--handoff needs a project (--target)")
    current = os.path.realpath(os.path.join(prefix, "current")) if prefix else None
    if not current or os.path.realpath(INSTALL_ROOT) != current or RUNNING_VERSION != tag[1:]:
        raise _fail("hand-off for %s reached %s (code %s); it needs the current release at %s holding %s; "
                    "not retrying" % (tag, INSTALL_ROOT, RUNNING_VERSION, current, tag[1:]))
    from alpaca.gates import verdict as vc
    # The project lock covers the journal test, the plan, the saving of local files and the apply: a
    # second update (or a start-up recovery in another session) never works on the scaffolding of
    # this one. It is let go before the venv check and doctor, which can take long and need no lock.
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(upgrade.project_lock(root))
        except upgrade.UpgradeError as e:
            raise _blocked(str(e))
        _check_project(root, prefix, locked=True)
        have = releasetags.tree_version(root)
        if not releasetags.parse_version(have or ""):
            raise _blocked("cannot read the Alpaca version of %s" % root)
        same = _compare(have, tag)        # decided under the lock: a run that waited behind a finished one sees it
        if same:
            return PASS, same, None

        plan = upgrade.plan(root, INSTALL_ROOT)
        summary = upgrade.print_plan(plan)
        say("plan: %s" % summary)
        for rel in plan.local_files:
            say("local file, saved before apply: %s" % rel)
        for rel in plan.deleted_files:
            say("local delete, the release restores it: %s" % rel)
        if not args.yes:
            if not _interactive():
                raise _blocked("no terminal to ask; pass --yes to update without asking; nothing was changed")
            if not _ask("Apply this plan to %s?" % root):
                raise _blocked("declined; %s is installed and current, the project is unchanged; run alpaca "
                               "update again to finish" % tag)
        if plan.local_files:
            try:
                _save_local_files(root, plan.local_files)
            except OSError as e:
                raise _fail("could not save your local files before the upgrade (%s); nothing was applied; %s is "
                            "installed and current; fix that and run alpaca update again" % (e, tag))

        try:
            upgrade.apply(root, INSTALL_ROOT)
        except upgrade.UpgradeLocked as e:
            raise _blocked(vc.error_reason(e))
        except KeyboardInterrupt:
            raise _fail("interrupted during the upgrade; any journal under .alpaca/upgrade/ is recovered on the "
                        "project's next start; then run alpaca update again") from None
        except Exception as e:
            raise _fail("upgrade failed: %s; %s is installed; the journal under .alpaca/upgrade/ is recovered "
                        "on the project's next start; then run alpaca update again" % (vc.error_reason(e), tag))
        say("upgrade applied")

    after = releasetags.tree_version(root)
    if after != tag[1:]:
        raise _fail("the project still says %s after the upgrade" % after)
    env = _child_env()
    if os.path.isfile(os.path.join(root, ".venv", "bin", "python")):
        boot = os.path.join(root, "setup", "bootstrap.sh")
        if _run(["bash", boot, "--check"], cwd=root, env=env) != 0:
            say("the project's .venv does not match the release; running bootstrap")
            if _run(["bash", boot], cwd=root, env=env) != 0:
                raise _fail("the project's .venv does not match %s; run bash setup/bootstrap.sh in %s" % (tag, root))
    else:
        say("the project has no .venv; venv check skipped")
    out = []
    rc = _run([os.path.join(root, "bin", "alpaca"), "doctor"], env=env, capture=out)
    # doctor exits 0 = OK, 1 = WARN, 2 = ERROR (alpaca/doctor.py); but 1 is also what an uncaught
    # exception gives, so a 1 counts as a warning only when doctor printed its own WARN verdict.
    if rc == 1 and "GATE alpaca-doctor: WARN" not in "".join(out):
        raise _fail("%s applied; alpaca doctor exited 1 without a WARN verdict" % tag)
    if rc not in (0, 1):
        raise _fail("%s applied; alpaca doctor exited %d" % (tag, rc))
    return PASS, "updated %s from %s to %s; doctor %s" % (root, have, after, "passed" if rc == 0 else "passed with warnings"), [INSTALL_ROOT]


# ------------------------------------------------------------------ CLI
def _parser(sub):
    u = sub.add_parser("update", help="move this project to the newest release tag of the release repository")
    u.add_argument("--check", action="store_true", help="report the project, installed and newest versions; change nothing")
    u.add_argument("--yes", action="store_true", help="install and apply without asking (the plan is still printed first)")
    u.add_argument("--repo", default=None, help="release repository (default: $ALPACA_UPDATE_REPO, else the public one)")
    u.add_argument("--prefix", default=None, help="install prefix (default: from the installed launcher)")
    u.add_argument("--bin-dir", dest="bin_dir", default=None, help="launcher folder (default: from the installed launcher)")
    u.add_argument("--target", default=None, help="the project to update (default: the project the command runs in)")
    u.add_argument("--handoff", default=None, help=argparse.SUPPRESS)


def _register():
    cli.command("update")(_cmd_update)
    cli.register_parser("update", _parser)


_register()
