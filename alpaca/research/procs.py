"""The one process runner of the research engine: agents, gate stages, plugin checks and holdout
checks all start here. Each child gets a new session and writes its output to a file; on a
timeout or a stop every descendant is killed, and the environments given to children are built
from allowlists.

Design: design/research-engine/architecture.md sections 6.5, 6.6 and 12.7 (FR-019, FR-020,
FR-022). It keeps no record: callers record what `run` returns.
"""
from __future__ import annotations

import ctypes
import gc
import json
import os
import signal
import subprocess
import threading
import time

from alpaca import util
from alpaca.research import jail
from alpaca.research.types import ProcResult

TAG_VAR = "ALPACA_RESEARCH_LAUNCH"
# No deny list of provider keys: both environments are allowlists (gate_environment below;
# launcher.build_env, section 6.5), so nothing needs to be removed from the operator's.

_POLL_S = 0.05                  # how often run() looks at its child
_PR_SET_CHILD_SUBREAPER = 36    # linux/prctl.h

# names gate_environment sets itself, and prefixes it never lets `extra` add (section 12.7)
_FIXED = ("PATH", "HOME", "TMPDIR")
_REFUSED_PREFIXES = ("GIT_", "ALPACA_")

# live children of this process: pid -> {"tag", "stopped", "killed"}
_LIVE: dict = {}
_LIVE_LOCK = threading.RLock()     # re-entrant: stop_all may run in a signal handler


def _start_ticks(pid: int) -> int | None:
    """Field 22 of /proc/<pid>/stat, the process start time, so a pid is known again only when
    it is the same process."""
    try:
        with open("/proc/%d/stat" % pid, "r", encoding="ascii", errors="replace") as fh:
            text = fh.read()
        return int(text[text.rfind(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _register(registry: str | None, pid: int, argv: list, cwd: str, tag: str | None,
              started: str) -> str | None:
    if not registry:
        return None
    os.makedirs(registry, exist_ok=True)
    path = os.path.join(registry, "%d.json" % pid)
    body = {"pid": pid, "start_ticks": _start_ticks(pid), "argv": [str(a) for a in argv],
            "cwd": cwd, "tag": tag, "started": started}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(body, fh, sort_keys=True)
    os.replace(tmp, path)
    return path


def run(argv: list, *, cwd: str, env: dict, timeout_s: float, log: str, stdin: str | None = None,
        tag: str | None = None, registry: str | None = None) -> "ProcResult":
    """New session, stdin from the file or /dev/null, stdout and stderr to `log`; on timeout or
    stop_all, kill_tree; the child listed in `registry` while it runs.

    `env` is passed as it is: callers build it with gate_environment or the launcher's allowlist.
    `registry` is a folder (paths.pids_dir); the child is listed there as <pid>.json with its
    start time, argv, cwd and tag, and the file is removed when the child is gone. `rc` is None
    when the child was killed on a timeout or a stop; `killed` lists every pid kill_tree
    signalled."""
    import subprocess

    if not argv:
        raise ValueError("an empty argv")
    parent = os.path.dirname(os.path.abspath(log))
    os.makedirs(parent, exist_ok=True)
    started = util.now_iso()
    t0 = time.monotonic()
    stdin_path = stdin if stdin is not None else os.devnull
    with open(stdin_path, "rb") as fin, open(log, "wb") as fout:
        child = subprocess.Popen([str(a) for a in argv], cwd=cwd, env=dict(env), stdin=fin,
                                 stdout=fout, stderr=subprocess.STDOUT,
                                 start_new_session=True, close_fds=True)
    entry = {"tag": tag, "stopped": False, "killed": (), "done": threading.Event()}
    with _LIVE_LOCK:
        _LIVE[child.pid] = entry
    listed = None
    timed_out = False
    killed: tuple = ()
    try:
        listed = _register(registry, child.pid, argv, cwd, tag, started)
        deadline = t0 + float(timeout_s)
        while True:
            try:
                child.wait(timeout=_POLL_S)
                break
            except subprocess.TimeoutExpired:
                pass
            if entry["stopped"]:
                child.wait()
                break
            if time.monotonic() >= deadline:
                timed_out = True
                killed = kill_tree(child.pid, tag=tag)
                child.wait()
                break
    except BaseException:
        # an error here (the registry not writable, an interrupt) never leaves the child running
        kill_tree(child.pid, tag=tag)
        child.wait()
        raise
    finally:
        with _LIVE_LOCK:
            _LIVE.pop(child.pid, None)
            stopped = bool(entry["stopped"])
        if listed:
            try:
                os.remove(listed)
            except FileNotFoundError:
                pass
    if stopped:
        entry["done"].wait(timeout=10.0)     # stop_all fills `killed` before it sets done
        killed = tuple(sorted(set(killed) | set(entry["killed"])))
    rc = None if (timed_out or stopped) else child.returncode
    return ProcResult(rc=rc, timed_out=timed_out, stopped=stopped, killed=killed, log=log,
                      started=started, ended=util.now_iso(),
                      duration_s=round(time.monotonic() - t0, 3))


def _tagged(tag: str) -> list:
    """Every live process of this user whose environment holds TAG_VAR=tag."""
    want = ("%s=%s" % (TAG_VAR, tag)).encode("utf-8")
    uid, me = os.getuid(), os.getpid()
    found = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return found
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == me:
            continue
        try:
            if os.stat("/proc/%d" % pid).st_uid != uid:
                continue
            with open("/proc/%d/environ" % pid, "rb") as fh:
                entries = fh.read().split(b"\0")
        except OSError:
            continue
        if want in entries:
            found.append(pid)
    return found


def _group_members(group: int) -> list:
    """Every live process whose process group is `group`, read from /proc/<pid>/stat."""
    found = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return found
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name, "r", encoding="ascii", errors="replace") as fh:
                text = fh.read()
            if int(text[text.rfind(")") + 2:].split()[2]) == group:
                found.append(int(name))
        except (OSError, ValueError, IndexError):
            continue
    return found


def _kill(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGKILL)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def kill_tree(pid: int, *, tag: str | None = None, lead_only: bool = False) -> tuple:
    """SIGKILL to pid, to every pid telemetry.descendants(pid) listed before the kill, to its
    process group, and to every process of this user whose environment has TAG_VAR=tag. With
    lead_only the group is killed only when pid leads it (its pgid is its own pid), so a reap
    never takes a group the pid only joined (an MCP child or a helper in the driver's group;
    in-session-runtime.md section 28.2).

    Returns the pids signalled, sorted: the members of the process group are listed from /proc
    before the group kill, so they are recorded too. This process is never signalled, and
    neither is its own process group."""
    from alpaca.runlog import telemetry

    me = os.getpid()
    try:
        group = os.getpgid(pid)
    except OSError:
        group = None
    if group is not None and group == os.getpgid(0):
        group = None                       # never this process's own group
    if lead_only and group is not None and group != pid:
        group = None                       # a group the pid does not lead is left alone
    tree = set(telemetry.descendants(pid))
    if group is not None:
        tree.update(_group_members(group))  # listed first, so the group kill is recorded too
    tree.discard(me)
    killed = set()
    if pid != me and _kill(pid):
        killed.add(pid)
    for p in sorted(tree):
        if p != pid and _kill(p):
            killed.add(p)
    if group is not None:
        try:
            os.killpg(group, signal.SIGKILL)
        except OSError:
            pass
    if tag:
        for p in _tagged(tag):
            if _kill(p):
                killed.add(p)
    return tuple(sorted(killed))


def stop_all() -> int:
    """kill_tree for every live child of this process; the number killed."""
    with _LIVE_LOCK:
        live = list(_LIVE.items())
        for _pid, entry in live:
            entry["stopped"] = True
    total = 0
    for pid, entry in live:
        got: tuple = ()
        try:
            got = kill_tree(pid, tag=entry.get("tag"))
        finally:
            entry["killed"] = got
            entry["done"].set()
        total += len(got)
    return total


def become_subreaper() -> bool:
    """prctl(PR_SET_CHILD_SUBREAPER, 1) through ctypes; False where unsupported."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
    except (OSError, AttributeError):
        return False
    try:
        return prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    except (OSError, ctypes.ArgumentError):
        return False


def _zombie_children() -> list:
    """Pids of the zombie children of this process, from /proc/<pid>/stat (state Z, ppid ==
    os.getpid()). Empty where /proc is not there."""
    me = os.getpid()
    found = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return found
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name, "r", encoding="ascii", errors="replace") as fh:
                text = fh.read()
            fields = text[text.rfind(")") + 2:].split()
            if fields[0] == "Z" and int(fields[1]) == me:
                found.append(int(name))
        except (OSError, ValueError, IndexError):
            continue
    return found


def _owned_pids() -> set:
    """Pids that a live Popen object still owns: its returncode is None, so its own wait or poll
    must be the one to reap it (a reap by anyone else makes that wait fail with ECHILD, and
    Python then records returncode 0, which would turn a failing gate into a passing one). Covers
    subprocess.Popen and multiprocessing's popen classes, found through the garbage collector."""
    owned = set()
    for obj in gc.get_objects():
        kind = type(obj)
        if not (isinstance(obj, subprocess.Popen)
                or kind.__module__.startswith("multiprocessing.popen_")):
            continue
        try:
            pid = getattr(obj, "pid", None)
            if isinstance(pid, int) and getattr(obj, "returncode", None) is None:
                owned.add(pid)
        except Exception:  # noqa: BLE001
            continue
    return owned


def reap_orphans() -> list:
    """Reap the zombie children of this process that no Popen object owns: the orphans a
    subreaper adopts (become_subreaper), which nothing else waits for and which would fill the
    process table until pids.max. A zombie is taken only through os.waitpid(pid, WNOHANG), so
    nothing blocks and a pid that is not a child is skipped. The zombies are listed first and the
    owners second, so a Popen created in between cannot own a listed pid. The reaped pids."""
    zombies = _zombie_children()
    if not zombies:
        return []
    keep = _owned_pids()
    reaped = []
    for pid in zombies:
        if pid in keep:
            continue
        try:
            got, _status = os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            continue
        if got == pid:
            reaped.append(pid)
    return reaped


def runner_environment() -> dict:
    """release.environment.command_environment() (alpaca/release/environment.py:7-17)."""
    from alpaca.release.environment import command_environment
    return command_environment()


def gate_path(tools_real: tuple = ()) -> str:
    """Pure: the PATH gate_environment sets for tools_real (below). Every fingerprint point of an
    attempt resolves its tools on it (fingerprint.take's path_env, section 3.2).

    jail.GATE_PATH first, then the folder of each file of tools_real that lies outside /usr, in
    order and once each."""
    folders = jail.GATE_PATH.split(":")
    for tool in tools_real:
        real = str(tool)
        if real == "/usr" or real.startswith("/usr/"):
            continue
        folder = os.path.dirname(real)
        if folder and folder not in folders:
            folders.append(folder)
    return ":".join(folders)


def gate_environment(extra: dict | None = None, *, home: str, tools_real: tuple = ()) -> dict:
    """Built from an allowlist, as the agent environment is (section 6.5), never from the
    operator's environment: command_environment() (alpaca/release/environment.py:7-17) keeps
    everything but a short deny list, so cloud keys and other tokens would reach attempt code and
    could land in gate logs and evidence. The names: PATH, HOME=home (the per-stage or per-check
    home of the mount table, section 5.2), TMPDIR=/tmp (the jail's own tmpfs), PWD (the stage's
    cwd), LANG, LC_ALL and TZ (the operator's when set, else C.UTF-8 for LANG), TERM=dumb,
    NO_COLOR=1, SHELL=/bin/sh, USER and LOGNAME (the operator's), and extra (WORKTREE,
    CONTRACT_DIR, EVIDENCE_DIR, REPRODUCER_DIR, SEED, REPEAT and the knob values the stage names).
    Nothing else passes: no provider key, proxy variable, GIT_* variable, ALPACA_* variable, forge
    token or SSH agent. A gate jail has no network, so a proxy there would only name a host it
    cannot reach. PATH is set, never inherited: jail.GATE_PATH, then the folder of each file of
    tools_real that lies outside /usr, in order; the runner interpreter's folder,
    <root>/.venv/bin, which command_environment() puts first (alpaca/release/environment.py:16,
    bin/alpaca-python:36-37), is not on it, and no PATH folder may name the project root. The
    PATH value is gate_path(tools_real).

    The signature has no cwd, so PWD comes from `extra` (the caller passes the stage's cwd as
    extra["PWD"]). An `extra` key that would set PATH, HOME or TMPDIR, a GIT_* or ALPACA_* name,
    or a proxy variable raises ValueError: extra never widens the allowlist."""
    env = {
        "PATH": gate_path(tools_real),
        "HOME": home,
        "TMPDIR": "/tmp",
        "TERM": "dumb",
        "NO_COLOR": "1",
        "SHELL": "/bin/sh",
    }
    for name in ("LANG", "LC_ALL", "TZ"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    env.setdefault("LANG", "C.UTF-8")
    user = os.environ.get("USER") or os.environ.get("LOGNAME")
    if not user:
        try:
            import pwd
            user = pwd.getpwuid(os.getuid()).pw_name
        except (ImportError, KeyError):
            user = None
    if user:
        env["USER"] = os.environ.get("USER") or user
        env["LOGNAME"] = os.environ.get("LOGNAME") or user
    for key, value in (extra or {}).items():
        name = str(key)
        if (name in _FIXED or name.upper().startswith(_REFUSED_PREFIXES)
                or name.lower().endswith("_proxy")):
            raise ValueError("gate environment refuses the name %r" % name)
        env[name] = str(value)
    return env
