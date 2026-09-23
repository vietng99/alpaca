"""A same-host re-entry lock and launcher for one worker (M3.12).

Ported from the earlier harness ops/worker-launch.py (doctrine/heartbeat-os-kicker.md). The mechanism is
unchanged: before a worker launcher does any real work it acquires an exclusive, non-blocking
re-entry lock keyed on the worker id. A second launcher for the SAME worker finds the lock held
and is REFUSED immediately: it does not wait, retry, or force-kill the holder. The lock is held in
try/finally so it releases on every exit path, and the kernel auto-releases it the instant the
owning process dies, so a crash cannot strand it.

The one OS-specific branch (selected once at import by sys.platform) is the syscall pair:
fcntl.flock(LOCK_EX | LOCK_NB) on Linux/macOS (default), msvcrt.locking(LK_NBLCK) on Windows
(backup). Both give the same structural guarantee.

Ported against the record's tree: the lockfile lives under the project's own `.alpaca/` runtime
directory (gitignored, inside the one root the harness may write), named per worker so two
DIFFERENT workers on the same box never contend. This launcher does not reimplement the ledger
claim (that is alpaca.claims, a distinct scope: this lock is same-host process-exclusivity, the claim
is cross-session write-step correctness); the two guards layer.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, Union

from alpaca import paths

Invoke = Union[str, Callable[[], None], None]


class LockHeldError(Exception):
    """Raised when the re-entry lock is already held by another live instance of the worker."""


def _lock_dir(root) -> Path:
    """The per-project lock directory under the runtime tree the harness owns."""
    d = Path(paths.runtime_dir(str(root))) / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def lock_path_for(root, worker) -> Path:
    """The lockfile path for `worker` under `root`, named uniquely per worker so two different
    workers on one box do not contend on the same lock."""
    return _lock_dir(root) / (".worker-lock-%s" % worker)


class ReentryLock:
    """An exclusive, non-blocking lock on a lockfile, released by the OS on process death and
    explicitly on every normal or error exit path. Also a context manager.

    The sys.platform branch is the ONLY OS-specific logic here: flock on Linux/macOS,
    msvcrt.locking on Windows. Both release the instant the owning process dies."""

    def __init__(self, lock_path):
        self.lock_path = Path(lock_path)
        self._fh = None

    def acquire(self) -> "ReentryLock":
        self._fh = open(self.lock_path, "a+b")
        try:
            if sys.platform.startswith("win"):
                import msvcrt
                try:
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    self._fh.close()
                    self._fh = None
                    raise LockHeldError(str(exc)) from exc
            else:
                import fcntl
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    self._fh.close()
                    self._fh = None
                    raise LockHeldError(str(exc)) from exc
        except LockHeldError:
            raise
        except Exception:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
            raise
        return self

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform.startswith("win"):
                import msvcrt
                try:
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def __enter__(self) -> "ReentryLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def lock(root, worker) -> ReentryLock:
    """Acquire the re-entry lock for `worker` under `root` and return the held lock. A second
    call for the same worker while the first is held raises LockHeldError: the launcher refuses to
    double-launch. Release the returned lock (or use it as a context manager) to free it."""
    return ReentryLock(lock_path_for(root, worker)).acquire()


def _run_invoke(invoke) -> None:
    """Run a bound shell-command-string or Python-callable invocation (a library caller may pass
    either; a CLI would only ever supply shell command strings)."""
    if invoke is None:
        return
    if callable(invoke):
        invoke()
        return
    subprocess.run(shlex.split(invoke), text=True, encoding="utf-8", check=True)


def launch(worker, root, *, driver_invoke: Invoke = None, detached_work_check: Invoke = None):
    """Run the re-entry guard once and return the exit code: 0 driver invoked (lock acquired,
    held, released in finally), 5 REFUSED (another live instance holds the lock), 1 the driver
    invocation itself threw (the lock was still released in finally first).

    The detached-work recheck (consume-don't-re-run) runs BEFORE the driver, matching the ported
    ordering."""
    lk = ReentryLock(lock_path_for(root, worker))
    try:
        try:
            lk.acquire()
        except LockHeldError:
            return 5
        if detached_work_check:
            _run_invoke(detached_work_check)
        if driver_invoke:
            _run_invoke(driver_invoke)
        return 0
    finally:
        if lk.held:
            lk.release()


def selftest() -> int:
    """Able-to-pass + able-to-fail controls for the re-entry lock discipline, ported from the earlier harness
    ops/worker-launch.py, on scratch lockfiles only. Returns 0 on all-PASS, else 1."""
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="worker-launch-selftest-")
    res = []
    try:
        root = Path(tmp)
        (root / "ALPACA-MANIFEST").write_text("selftest\n", encoding="utf-8")

        calls = []
        code = launch("W1", root, driver_invoke=lambda: calls.append("driver"))
        res.append(("able-to-pass: fresh launch acquires lock, invokes driver, returns 0",
                    code == 0 and calls == ["driver"]))

        probe = ReentryLock(lock_path_for(root, "W1"))
        try:
            probe.acquire()
            reacq = True
        except LockHeldError:
            reacq = False
        finally:
            probe.release()
        res.append(("release-on-exit: lockfile re-acquirable after a clean launch", reacq))

        held = lock(root, "W2")
        try:
            ran = []
            code = launch("W2", root, driver_invoke=lambda: ran.append("driver"))
            res.append(("able-to-fail: launch against a held lock is REFUSED (exit 5), driver not run",
                        code == 5 and ran == []))
        finally:
            held.release()

        order = []
        launch("W3", root,
               detached_work_check=lambda: order.append("detached"),
               driver_invoke=lambda: order.append("driver"))
        res.append(("ordering: detached-work recheck precedes the driver invocation",
                    order == ["detached", "driver"]))

        def _boom():
            raise RuntimeError("driver blew up")

        threw = False
        try:
            launch("W4", root, driver_invoke=_boom)
        except RuntimeError:
            threw = True
        after = ReentryLock(lock_path_for(root, "W4"))
        try:
            after.acquire()
            released = True
        except LockHeldError:
            released = False
        finally:
            after.release()
        res.append(("release-on-driver-throw: lock released even when the driver raises",
                    threw and released))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n  worker-launch selftest")
    print("  " + "-" * 48)
    for label, ok in res:
        print("  %s  %s" % ("PASS" if ok else "FAIL", label))
    all_pass = all(ok for _, ok in res)
    print("SELFTEST PASS: every worker-launch case passed" if all_pass
          else "SELFTEST FAIL: %d of %d case(s) did not pass"
               % (sum(1 for _, ok in res if not ok), len(res)))
    return 0 if all_pass else 1
