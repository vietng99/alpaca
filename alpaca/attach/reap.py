"""Stop what a gate leaves running (op-062 t-649, adversarial round 2: N9c, N9b, and A3 in part).

The release runner starts each gate in a session of its own and kills that process group only on a
timeout or a stop (`alpaca/release/checks.py:175-204`). A gate that exits 0 leaves its descendants running,
and a descendant that called `setsid` is not in the group at all. Such a process from the branch's code
could act on a later gate's clone (N9b) or on the merge of a later check (N9c).

`Reaper` closes that route for what honest gate code leaves running, as far as the limits below allow:

  * on entry it makes this process a child subreaper (`prctl(PR_SET_CHILD_SUBREAPER)`): a descendant whose
    parent exits is handed to this process, not to init, also after a double fork or `setsid`;
  * `reap()` kills every adopted process and all of its descendants with SIGKILL (through a pidfd, sent only
    when the process start time still matches the one read from /proc), waits on each adopted child,
    and repeats until none is left. The release runner calls `start` and `finish` as each gate starts
    and ends (`baseline.run_gate_set_each` hands it the reaper), so it kills what honest gate code leaves
    running (a test server, a double fork, a `setsid` daemon) between gates, before the seeds are read again
    and before the check folders are deleted. It does not win every race: a process that forks and lets its
    parent exit in a loop can outrun the `/proc` scan (`_table` lists /proc first and reads each entry
    later), and at `DEADLINE_S` the check is refused and the subreaper flag is put back while that process
    lives, so it is handed to init (adversarial round 3, P3c, a reference in
    `.alpaca/proofs/op-062/t-649.logs/follow-ups.md`);
  * on exit it reaps once more and puts the subreaper flag back as it found it.

What is spared: the children this process already had on entry (the caller's own), and every child in this
process's own session (git calls through gitops and the runner's clones; a gate and its descendants are never
in it, because the runner starts each gate with `start_new_session`). One reaper runs at a time per process
(`_LOCK`); a second thread that runs gates outside a reaper while one is active is not supported, since its
gate would look like a leftover (the CLI runs one verb per process, and `gitops.process_env_scrubbed` already
needs a gate run to have its process to itself).

What it does not reach, stated so no one reads more into it: a process started outside this process tree
(`systemd-run --user`, `at`, cron, a shell of the agent), a gate that kills the check process itself, whose
leftovers are then handed to init, a gate that writes the check's memory (`/proc/<pid>/mem`), and the branch's
own test code, which decides its own gate's exit status. Those run as the same user as the check; see the
threat model in `alpaca/attach/check.py`. Off Linux the module does nothing (there is no subreaper).
"""
from __future__ import annotations

import ctypes
import os
import signal
import sys
import threading
import time

from alpaca.attach import AttachError

PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
#: seconds `reap` keeps killing before it gives up and refuses the check
DEADLINE_S = 30.0
SUPPORTED = sys.platform.startswith("linux") and os.path.isdir("/proc")

_LOCK = threading.Lock()
_LIBC = None


def _libc():
    global _LIBC
    if _LIBC is None:
        try:
            _LIBC = ctypes.CDLL(None, use_errno=True)
        except OSError:
            raise AttachError("the check cannot reach prctl, so it cannot stop what a gate leaves running") from None
    return _LIBC


def subreaper() -> int:
    """This process's child subreaper flag (0 or 1); 0 off Linux."""
    if not SUPPORTED:
        return 0
    value = ctypes.c_int(0)
    if _libc().prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(value), ctypes.c_ulong(0), ctypes.c_ulong(0),
                     ctypes.c_ulong(0)) != 0:
        raise AttachError("prctl could not read the subreaper flag (errno %d)" % ctypes.get_errno())
    return value.value


def _set_subreaper(on) -> None:
    if _libc().prctl(PR_SET_CHILD_SUBREAPER, ctypes.c_ulong(1 if on else 0), ctypes.c_ulong(0), ctypes.c_ulong(0),
                     ctypes.c_ulong(0)) != 0:
        raise AttachError("prctl could not set the subreaper flag (errno %d)" % ctypes.get_errno())


def _stat(pid):
    """`(ppid, session, start time)` of a process from the kernel's /proc/<pid>/stat, or None when it is gone.
    The fields are read after the last `)`, so a command name with spaces or brackets cannot shift them."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    rest = data[data.rfind(b")") + 2:].split()
    try:
        return int(rest[1]), int(rest[3]), int(rest[19])
    except (IndexError, ValueError):
        return None


def _table() -> dict:
    """pid -> `(ppid, session, start time)` of every process this user can see."""
    table = {}
    for name in os.listdir("/proc"):
        if name.isdigit():
            got = _stat(int(name))
            if got is not None:
                table[int(name)] = got
    return table


def _kill(pid, start) -> None:
    """SIGKILL `pid` if it is still the process that started at `start` (a pidfd pins it first)."""
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return
    try:
        got = _stat(pid)
        if got is not None and got[2] == start:
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except OSError:
                pass
    finally:
        os.close(fd)


class Reaper:
    """A context in which every process a gate leaves behind is handed to this process and killed by `reap`.
    It is also the release runner's progress object (`start`, `finish`, `drop`): each call reaps."""

    def __init__(self):
        self.me = os.getpid()
        self.session = os.getsid(0)
        self.spared = set()
        self.was = 0
        self.active = False
        self.reaped = 0

    def __enter__(self):
        if not SUPPORTED:
            return self
        _LOCK.acquire()
        try:
            self.was = subreaper()
            _set_subreaper(True)
            self.spared = {(pid, row[2]) for pid, row in _table().items() if row[0] == self.me}
            self.active = True
        except BaseException:
            _LOCK.release()
            raise
        return self

    def __exit__(self, kind, exc, tb):
        if not self.active:
            return False
        try:
            self.reap()
        except AttachError:
            if kind is None:
                raise
        finally:
            self.active = False
            try:
                _set_subreaper(self.was)
            finally:
                _LOCK.release()
        return False

    def _left(self, table) -> list:
        return [pid for pid, (ppid, session, start) in table.items()
                if ppid == self.me and session != self.session and (pid, start) not in self.spared]

    def reap(self) -> int:
        """Kill every process a gate left behind, and its descendants, until none is left; return how many
        adopted processes were waited on. Raises AttachError when some are still alive after DEADLINE_S."""
        if not self.active:
            return 0
        count, deadline = 0, time.monotonic() + DEADLINE_S
        while True:
            table = _table()
            roots = self._left(table)
            if not roots:
                self.reaped += count
                return count
            if time.monotonic() > deadline:
                raise AttachError("gate code left %d process%s running that the check could not stop"
                                  % (len(roots), "" if len(roots) == 1 else "es"))
            below = {}
            for pid, (ppid, _, _) in table.items():
                below.setdefault(ppid, []).append(pid)
            victims, todo = [], list(roots)
            while todo:
                pid = todo.pop()
                victims.append(pid)
                todo.extend(below.get(pid, ()))
            for pid in victims:
                _kill(pid, table[pid][2])
            for pid in roots:
                count += self._wait(pid, deadline)

    @staticmethod
    def _wait(pid, deadline) -> int:
        """Wait on one adopted child (never `waitpid(-1)`, which could take the exit status another waiter in this process expects)."""
        while True:
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return 0
            if done:
                return 1
            if time.monotonic() > deadline:
                return 0
            time.sleep(0.005)

    # the release runner's progress interface (`alpaca/release/progress.py` `_Quiet`)
    def start(self, name):
        self.reap()

    def finish(self, name, status, duration_s=None):
        self.reap()

    def drop(self, name):
        pass
