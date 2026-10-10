"""One capture runner for every capture path (t-070 RC4): the Stop, PreCompact and SessionEnd hooks,
the Codex checkpoint verb, and the collector's wiki capture, preservation jobs and session-views
refresh.

Why the deadline is enforced from outside the work (A-4). A hook's hard deadline was a SIGALRM
handler in the hook's own process. Python runs a signal handler only between bytecodes of the main
thread, and the kernel delivers a signal to a thread asleep in an uninterruptible system call only
when the call returns. The capture's echo-index read sorted every active block through SQLite temp
files in /var/tmp, on a spinning disk; under that disk's load one `pwrite64` to a temp file waited
56 s, so the 8 s deadline acted only at 60 s, and the hook landed nothing and recorded
nothing (`.alpaca/proofs/op-008/t-070.logs/rc4/`). Any blocking call can do the same. So the work
now runs where the caller can stop waiting for it:

- `run_process` (the hooks and the checkpoint verb): the work runs in a child interpreter in its own
  session, with no access to the hook's stdout. At the deadline the parent kills the child's process
  group (SIGKILL) and returns without waiting for it to die. A killed child never leaves a half
  transaction: SQLite's WAL ignores uncommitted frames, the kernel drops its locks when it exits, and
  the Writer repairs a ledger whose sync it missed when it next opens. The child's side is
  `alpaca/capture_child.py`, the one place that imports the work by the name it is given.
- `Lane` (the collector): the work runs in one long-lived worker thread per lane, so the wiki lane
  keeps the collector's shared absorber and its echo index. The caller waits at most its budget, then
  abandons the job: the loop moves on, the job ends on its own (a thread cannot be killed), and the
  lane answers `busy` until it has. Capture is idempotent, so a retry lands only what is left.

Every path ends in one outcome record (`record`): a capture cut short appends `capture-deferred`
{hook, through_event, remaining}; a capture that failed, or a transcript it could not read, appends
`drain-failed` {where, error}. `remaining` is None when the work was stopped before it counted its
documents. With the observability flag on and no collector alive the hooks capture nothing;
`note_absent_collector` records that once per `ABSENT_NOTE_S` (A-8) as `capture-no-collector`, a
state, not a capture failure (t-070 round 2 C-D, I-5).
"""
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time

#: The work a capture job runs: `service.capture(root, **kwargs)`, in the child or the lane.
CAPTURE = "alpaca.wiki.service:capture"
#: Seconds a capture outside a hook window (the checkpoint verb, a test) may run past its own
#: cooperative budget before the runner stops it.
GRACE_S = 5.0
#: Seconds before the kill at which the child stops between two blocks by itself (`hard_s`), so a
#: deadline that is only a slow document rolls back cleanly and reports exactly how far it got.
HARD_LEAD_S = 0.5
#: With observability on and no collector alive (A-8), the hooks note it at most once per this many
#: seconds for the project.
ABSENT_NOTE_S = 600.0
#: The record meta key holding when the absent collector was last checked or noted.
ABSENT_KEY = "capture:no-collector-noted"
#: Text of the A-8 record outcome, the doctor check and `wiki status`.
NO_COLLECTOR = "observability enabled but no collector alive"
#: The record kind of the A-8 note (t-070 round 2 C-D, I-5): a state report with the fields
#: {where, error} it had as `drain-failed`. No reader counts it as a capture failure (the hub's capture
#: errors and failure filter, the session classes, the record graph's `failed_with` rule, the
#: cockpit's failure lane). Notes recorded before as `drain-failed` keep being read as they were.
NO_COLLECTOR_KIND = "capture-no-collector"


class Result:
    """What one run of a job came to.

    `status`: `ok` (the work returned `value`), `failed` (it raised; `error` is the screened
    `Type: message`, `exc` the exception when it was raised in this process), `overrun` (the runner
    stopped waiting at the deadline: the child was killed, or the lane job abandoned), `busy` (a lane
    still runs an abandoned job, so this one never started). `progress` is the last report of the
    work: `{"through_event", "done", "total"}`."""

    __slots__ = ("status", "value", "error", "exc", "progress", "seconds")

    def __init__(self, status, *, value=None, error=None, exc=None, progress=None, seconds=0.0):
        self.status, self.value, self.error, self.exc = status, value, error, exc
        self.progress, self.seconds = progress, seconds

    def __repr__(self):
        return "Result(%s, %.3fs, error=%r, progress=%r)" % (self.status, self.seconds, self.error,
                                                             self.progress)


def _screen(text):
    """An error text may quote what failed: the one secret screen (t-070 RC2) before the record."""
    from alpaca.analytics import detail
    return detail.screen(str(text))


def _describe(exc):
    return _screen("%s: %s" % (type(exc).__name__, exc))


# ------------------------------------------------------------------ the child process (hooks, verb)
def _package_root():
    """The directory that holds this `alpaca` package, so the child imports the same tree."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _same_package(reported):
    """True when the child reports it ran `alpaca` from this process's package root."""
    return bool(reported) and os.path.realpath(reported) == os.path.realpath(_package_root())


def run_process(target, kwargs, *, root, kill_s, progress=False):
    """Run `target(root, **kwargs)` in a child interpreter and wait at most `kill_s` seconds.

    `kwargs` must be JSON. `deadline_at` and `hard_at` in it are `time.monotonic()` values (the clock
    is system-wide), turned into the work's `deadline_s` and `hard_s` when the child starts, so the
    child's start-up comes out of the same budget. With `progress`, the work gets a `progress`
    callback whose reports come back here and name how far an overrun got.

    The child runs in its own session with stdin, stdout and stderr on /dev/null and reports on a
    pipe of its own, so a child left behind holds nothing the hook's caller waits on. At the deadline
    its process group gets SIGKILL and this returns at once: a child asleep in an uninterruptible
    call dies when the call returns, and nothing here waits for that. Any exception in this process
    while waiting (the hook's own SIGALRM included) kills the child first.

    The child starts through the one launcher (`alpaca.child`, t-070 round 2 C-A; t-428):
    `python -I alpaca/boot.py`, which loads this package by its location, with the parent's import
    path minus the package root, the working folder and every other Alpaca tree, no PYTHONPATH, run
    from the package root, with the served root's SQLite temp folder. It reports the
    package it imported with its result; a child that ran another tree's `alpaca` is a failure."""
    from alpaca import child
    start = time.monotonic()
    read_end, write_end = os.pipe()
    env = child.child_env(served_root=str(root))
    job = json.dumps({"target": target, "root": str(root), "kwargs": kwargs, "fd": write_end,
                      "progress": bool(progress)})
    try:
        proc = subprocess.Popen(child.python_argv("alpaca.capture_child", served_root=str(root)),
                                cwd=_package_root(), env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, pass_fds=(write_end,),
                                start_new_session=True)
    except BaseException:
        os.close(read_end)
        os.close(write_end)
        raise
    os.close(write_end)
    last, buffer, final, closed = None, b"", None, False
    try:
        try:
            proc.stdin.write(job.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass                                    # a child that died at once reports below
        end = start + max(0.0, float(kill_s))
        while final is None:
            left = end - time.monotonic()
            if left <= 0:
                break
            ready, _, _ = select.select([read_end], [], [], left)
            if not ready:
                continue
            chunk = os.read(read_end, 65536)
            if not chunk:                           # the child closed its end: it is done or dead
                closed = True
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if "progress" in message:
                    last = message["progress"]
                elif "ok" in message or "error" in message:
                    final = message
    except BaseException:
        _kill(proc)
        os.close(read_end)
        raise
    os.close(read_end)
    seconds = time.monotonic() - start
    if final is None:
        if not closed:                              # the deadline: stop it, do not wait for it
            _kill(proc)
            return Result("overrun", progress=last, seconds=seconds)
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            _kill(proc)
        return Result("failed", error="capture worker exited with status %s and no report" % proc.returncode,
                      progress=last, seconds=seconds)
    try:
        proc.wait(timeout=1.0)                      # it has reported and is leaving: reap it
    except subprocess.TimeoutExpired:
        _kill(proc)
    if not _same_package(final.get("package")):
        return Result("failed", error="capture worker ran alpaca from %s, not %s" % (
            final.get("package"), _package_root()), progress=last, seconds=seconds)
    if "ok" in final:
        return Result("ok", value=final["ok"], progress=last, seconds=seconds)
    return Result("failed", error=final.get("error"), progress=last, seconds=seconds)


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    proc.poll()


def _child_kwargs(kwargs, progress):
    out = dict(kwargs)
    now = time.monotonic()
    for absolute, relative in (("deadline_at", "deadline_s"), ("hard_at", "hard_s")):
        if absolute in out:
            at = out.pop(absolute)
            out[relative] = None if at is None else max(0.0, float(at) - now)
    if progress is not None:
        out["progress"] = progress
    return out


# ------------------------------------------------------------------ the collector's lanes
class _Job:
    __slots__ = ("fn", "args", "kwargs", "done", "result", "progress", "started", "label")

    def __init__(self, fn, args, kwargs, label=None):
        self.fn, self.args, self.kwargs, self.label = fn, args, kwargs, label
        self.done, self.result, self.progress, self.started = threading.Event(), None, None, time.monotonic()


class Lane:
    """One worker thread that runs one job at a time for a long-lived caller (the collector).

    `run` hands the job to the lane's thread and waits at most `budget_s`. A job past its budget is
    abandoned: `run` returns `overrun` and the caller goes on; the job keeps running (a thread cannot
    be stopped from outside) and the lane answers `busy` to every `run` until it ends. The thread
    lives as long as the process, so state it keeps per thread (the collector's shared absorber and
    its echo index, `consumers._SharedAbsorber`) survives from one job to the next. An exception the
    job raises is handed back as `failed` with the exception itself in `exc`, so the caller can raise
    it again as if it had run the job itself."""

    def __init__(self, name):
        self.name = name
        self._cv = threading.Condition()
        self._job = None
        self._thread = None

    def started(self):
        """Whether the lane's thread was ever started (a lane that ran nothing holds nothing)."""
        with self._cv:
            return self._thread is not None and self._thread.is_alive()

    def busy_since(self):
        """`time.monotonic()` when the job the lane still runs started, or None when it is free."""
        with self._cv:
            return None if self._job is None else self._job.started

    def busy_with(self):
        """The `label` the job the lane still runs was given (`run`), or None when it is free or the
        job has none: the collector tells its echo index warm-up from an abandoned capture by it."""
        with self._cv:
            return None if self._job is None else self._job.label

    def run(self, fn, *args, budget_s, progress=False, label=None, **kwargs):
        job = _Job(fn, args, kwargs, label)
        if progress:
            def report(**fields):
                job.progress = dict(fields)
            job.kwargs = dict(kwargs, progress=report)
        with self._cv:
            if self._job is not None:
                held = self._job
                return Result("busy", progress=held.progress, seconds=time.monotonic() - held.started)
            self._job = job
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._serve, name="alpaca-lane-" + self.name,
                                                daemon=True)
                self._thread.start()
            self._cv.notify_all()
        if not job.done.wait(None if budget_s is None else max(0.0, float(budget_s))):
            return Result("overrun", progress=job.progress, seconds=time.monotonic() - job.started)
        return job.result

    def _serve(self):
        while True:
            with self._cv:
                while self._job is None or self._job.done.is_set():
                    self._cv.wait()
                job = self._job
            try:
                value = job.fn(*job.args, **job.kwargs)
            except BaseException as exc:
                job.result = Result("failed", error=_describe(exc), exc=exc, progress=job.progress,
                                    seconds=time.monotonic() - job.started)
            else:
                job.result = Result("ok", value=value, progress=job.progress,
                                    seconds=time.monotonic() - job.started)
            with self._cv:
                self._job = None
            job.done.set()


# ------------------------------------------------------------------ the one outcome record
def outcome_events(where, result, *, batch=None):
    """The record events (kind, data) a run's `result` leaves; [] for a clean run.

    A capture report (`ok`) that names `deferred` gives `capture-deferred`, and each transcript it
    could not read gives `drain-failed`; an `overrun` or a `busy` lane gives `capture-deferred` with
    what the work reported (`remaining` None when it never counted, or the batch size `batch` when the
    caller knows it); a `failed` run gives `drain-failed` with its screened error."""
    out = []
    if result.status == "ok":
        report = result.value if isinstance(result.value, dict) else {}
        deferred = report.get("deferred")
        if deferred:
            out.append(("capture-deferred", {"hook": where, "through_event": deferred["through_event"],
                                             "remaining": deferred["remaining"]}))
        for entry in report.get("missing_transcripts") or []:
            out.append(("drain-failed", {"where": where, "error": _screen("transcript unavailable: %s: %s" % (
                entry.get("session"), entry.get("error")))}))
    elif result.status in ("overrun", "busy"):
        seen = result.progress or {}
        remaining = None
        if seen.get("total") is not None:
            remaining = max(0, int(seen["total"]) - int(seen.get("done") or 0))
        elif batch is not None:
            remaining = batch
        out.append(("capture-deferred", {"hook": where, "through_event": int(seen.get("through_event") or 0),
                                         "remaining": remaining}))
    elif result.status == "failed":
        out.append(("drain-failed", {"where": where, "error": _screen(result.error or "capture failed")}))
    return out


#: (root, meta key) of the faults this process remembered for `quiet_s`, so a clean run touches the
#: record only when there is a fault of its own to forget.
_QUIET_KEYS = set()


def record(root, session, where, result, *, batch=None, quiet_s=None):
    """Append the outcome of one run (`outcome_events`) to the record; returns the kinds appended.

    `quiet_s` (the collector, which retries one batch until it lands): an outcome equal to the last
    one recorded for `where` and `session` within `quiet_s` seconds is not appended again, so a batch
    retried at every backoff step leaves one event, and a clean run of that session clears the memory
    so its next fault is recorded at once. The hooks record every run, as one hook run is one
    capture."""
    from alpaca import db, util
    events = outcome_events(where, result, batch=batch)
    key = "capture:last-outcome:%s:%s" % (where, session)
    if not events and (quiet_s is None or (str(root), key) not in _QUIET_KEYS):
        return []                                   # nothing to record, no fault of ours to forget
    conn = db.connect(str(root))
    try:
        if not events:                              # a clean collector run: forget the last fault
            _QUIET_KEYS.discard((str(root), key))
            if db.meta_get(conn, key):
                with db.transaction(conn):
                    conn.execute("DELETE FROM meta WHERE key=?", (key,))
            return []
        with db.transaction(conn):
            if quiet_s is not None:
                raw = db.meta_get(conn, key)
                signature = json.dumps([(kind, data.get("hook") or data.get("where"), data.get("error"))
                                        for kind, data in events], sort_keys=True)
                try:
                    last = json.loads(raw) if raw else {}
                except ValueError:
                    last = {}
                _QUIET_KEYS.add((str(root), key))
                if last.get("signature") == signature and time.time() - float(last.get("at") or 0) < quiet_s:
                    return []
                db.meta_set(conn, key, json.dumps({"signature": signature, "at": time.time(),
                                                   "ts": util.now_iso()}))
            for kind, data in events:
                db.append_event(conn, session=session, actor="alpaca", kind=kind, data=data,
                                conn_in_txn=True)
        return [kind for kind, _ in events]
    finally:
        conn.close()


def note_absent_collector(root, session, where):
    """A-8: with the observability flag on, the hooks leave wiki capture to the collector. When no
    collector is alive, nothing captures; record that, at most once per `ABSENT_NOTE_S` for the
    project, as `capture-no-collector` {where, error: NO_COLLECTOR ...} (`NO_COLLECTOR_KIND`; it was
    `drain-failed` until t-070 round 2 C-D). Cheap when there is nothing to say:
    one record meta read; the collector check (`observability.collector_alive`) runs only once the
    interval has passed. Returns True when it appended the event. Never raises."""
    try:
        from alpaca import db, observability, util
        conn = db.connect(str(root))
        try:
            def due():
                try:
                    return time.time() - float(db.meta_get(conn, ABSENT_KEY) or 0) >= ABSENT_NOTE_S
                except (TypeError, ValueError):
                    return True
            if not due():
                return False
            alive = observability.collector_alive(root)
            with db.transaction(conn):
                if not due():                       # another hook got here first
                    return False
                db.meta_set(conn, ABSENT_KEY, repr(time.time()))
                if alive:
                    return False
                db.append_event(conn, session=session, actor="alpaca", kind=NO_COLLECTOR_KIND,
                                conn_in_txn=True,
                                data={"where": where, "error": NO_COLLECTOR + ": the hooks leave wiki capture "
                                      "to the collector, so nothing is captured until one runs "
                                      "(bin/alpaca collect watch); noted at " + util.now_iso()})
            return True
        finally:
            conn.close()
    except Exception as exc:
        print("alpaca capture note unavailable: " + type(exc).__name__, file=sys.stderr)
        return False
