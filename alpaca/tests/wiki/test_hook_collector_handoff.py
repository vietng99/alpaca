"""Merge of op-008 into main (2026-10-06), review blocker B2: the hooks hand wiki capture to a running
collector.

On the live store the Stop hook's capture step (`stop.capture_open_session`) read the echo index of
every active block on each turn end (about 30 s), so the 8 s hook deadline cut it every time: each
turn end waited about 8 s, landed nothing and competed with the collector for the wiki write lock.
It ran because `.alpaca/observability/enabled` is absent on the live project while a `collect watch`
collector runs and captures every event anyway. SessionEnd and PreCompact pay the same index read.

Now each of the three skips its capture call while this project's watch collector is alive
(`observability.collector_alive`: the collector lock of this root is held by the process the record's
`obs:collector` row names, read from /proc/locks without taking the lock) and captures exactly as
before otherwise. The hooks' other work and the Stop decision line do not change.
"""
import fcntl
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import append, events  # noqa: E402

from alpaca import db as record                           # noqa: E402
from alpaca import observability                          # noqa: E402
from alpaca.wiki import service                           # noqa: E402
from alpaca.wiki.config import Config                     # noqa: E402

pytestmark = pytest.mark.skipif(not Path("/proc/locks").is_file(), reason="needs Linux /proc/locks")


def _lock_path(project) -> Path:
    return Path(project, ".alpaca", "observability", "collector.lock")


def _write_collector_row(project, *, pid, mode="watch", identity=None):
    """The `obs:collector` row a collector pass writes (observability._run)."""
    if identity is None:
        identity = observability._process_identity(pid)
    conn = record.connect(str(project))
    try:
        with record.transaction(conn):
            record.meta_set(conn, "obs:collector", json.dumps({
                "pid": pid, "process_identity": identity, "heartbeat": "2026-10-07T00:00:00+00:00",
                "mode": mode}))
    finally:
        conn.close()


@contextmanager
def _held_lock(project):
    """Hold this root's collector lock in this process, as `collect watch` holds it for its life."""
    path = _lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


@contextmanager
def _live_collector(project):
    """A running watch collector of this root, in this test process: the lock held and the record
    row naming this process."""
    with _held_lock(project):
        _write_collector_row(project, pid=os.getpid())
        yield


class _Calls:
    """The `service.capture` calls the capture runner's children made, read from the spy's file."""

    def __init__(self, path):
        self.path = path

    def _read(self):
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line]

    def __len__(self):
        return len(self._read())

    def __getitem__(self, i):
        return self._read()[i]

    def __eq__(self, other):
        return self._read() == other


def _spy_capture(monkeypatch, project=None):
    """Every `service.capture` the hooks run: the capture runner's child runs
    `alpaca.tests.runner_fakes:spy`, which writes each call's keyword arguments to a file and then
    runs the real capture (t-070 RC4: the hooks capture in a child process)."""
    from alpaca import capture_runner
    path = Path(project or os.getcwd(), "capture-spy.jsonl")
    monkeypatch.setenv("ALPACA_TEST_SPY", str(path))
    monkeypatch.setattr(capture_runner, "CAPTURE", "alpaca.tests.runner_fakes:spy")
    return _Calls(path)


def _store_file(project) -> Path:
    return Config.for_project(str(project)).db_path


def _open_session(project, sid):
    from alpaca import cli
    cli.main(["init"])
    for n in range(3):
        append(project, "result", {"body": "open session note %d about the lantern rota" % n},
               session=sid)


def _kinds(project, kind):
    return [e for e in events(project) if e["kind"] == kind]


# ------------------------------------------------------------------ the three hooks, collector alive
def test_stop_capture_step_is_skipped_while_the_collector_runs(project, monkeypatch):
    from alpaca.hooks import stop
    _open_session(project, "s-live")
    calls = _spy_capture(monkeypatch, project)
    with _live_collector(project):
        assert stop.capture_open_session({"session_id": "s-live", "cwd": str(project),
                                          "_strict": True}) is None
    assert calls == []
    assert not _store_file(project).exists()                  # no wiki write at all
    assert not _kinds(project, "drain-failed") and not _kinds(project, "capture-deferred")


def test_the_codex_checkpoint_skips_capture_while_the_collector_runs(project, monkeypatch):
    from alpaca.hooks import pre_compact
    _open_session(project, "s-codex")
    calls = _spy_capture(monkeypatch, project)
    with _live_collector(project):
        out = pre_compact.checkpoint({"session_id": "s-codex", "cwd": str(project), "_strict": True,
                                      "note": "half way"})
    assert calls == [] and out == {"drain": None}
    assert len(_kinds(project, "session-checkpoint")) == 1      # its own event still lands
    assert not _store_file(project).exists()


def test_pre_compact_and_session_end_skip_capture_while_the_collector_runs(project, monkeypatch):
    from alpaca.hooks import pre_compact, session_end
    _open_session(project, "s-end")
    calls = _spy_capture(monkeypatch, project)
    with _live_collector(project):
        compact = pre_compact.handle({"session_id": "s-end", "cwd": str(project), "_strict": True,
                                      "trigger": "auto"})
        ended = session_end.handle({"session_id": "s-end", "cwd": str(project), "_strict": True,
                                    "reason": "exit"})
    assert calls == []
    assert compact == {"drain": None}
    assert set(ended) == {"ended"}                               # the verb output is unchanged
    assert len(_kinds(project, "pre-compact")) == 1 and len(_kinds(project, "session-end")) == 1
    conn = record.connect_readonly(str(project))
    try:
        assert conn.execute("SELECT ended FROM sessions WHERE sid='s-end'").fetchone()[0]
    finally:
        conn.close()
    assert not _store_file(project).exists()
    assert not _kinds(project, "drain-failed") and not _kinds(project, "capture-deferred")


def _stop_main(monkeypatch, payload):
    """The real Stop `main` body (not the deadline wrapper); returns what it wrote to stdout."""
    from alpaca.hooks import common, stop
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(common, "read_stdin", lambda *a, **k: dict(payload))
    stop.main.__wrapped__()
    return out.getvalue()


def test_the_stop_decision_line_is_the_same_with_the_collector_alive_or_absent(project, monkeypatch):
    from alpaca import cli
    cli.main(["init"])
    Path(project, "style", "banned.txt").write_text("delve\n", encoding="utf-8")
    block = {"session_id": "s-dec", "cwd": str(project), "reply": "We should delve in."}
    allow = {"session_id": "s-dec", "cwd": str(project), "reply": "Plain words here."}
    absent = [_stop_main(monkeypatch, block), _stop_main(monkeypatch, allow)]
    with _live_collector(project):
        alive = [_stop_main(monkeypatch, block), _stop_main(monkeypatch, allow)]
    assert alive == absent
    assert json.loads(alive[0])["decision"] == "block" and alive[1] == ""


# ------------------------------------------------------------------ no live collector: as before
def _capture_runs(project, monkeypatch):
    from alpaca.hooks import stop
    calls = _spy_capture(monkeypatch, project)
    stop.capture_open_session({"session_id": "s-open", "cwd": str(project), "_strict": True})
    return calls


def test_stop_captures_as_before_without_a_collector(project, monkeypatch):
    _open_session(project, "s-open")
    calls = _capture_runs(project, monkeypatch)
    assert len(calls) == 1 and calls[0]["transcripts"] is False
    assert service.status(project)["capture"]["captured_events"] >= 3


def test_pre_compact_and_session_end_capture_as_before_without_a_collector(project, monkeypatch):
    from alpaca.hooks import pre_compact, session_end
    _open_session(project, "s-end")
    calls = _spy_capture(monkeypatch, project)
    compact = pre_compact.handle({"session_id": "s-end", "cwd": str(project), "_strict": True})
    session_end.handle({"session_id": "s-end", "cwd": str(project), "_strict": True})
    assert len(calls) == 2 and compact["drain"]["notes"] >= 3


def _other_live_process():
    """A live process other than this one, with its process identity (the pytest parent's shell
    would do, but a child we own is certain to stay up for the test)."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    for _ in range(100):
        if observability._process_identity(child.pid):
            break
        time.sleep(0.01)
    return child


@pytest.mark.parametrize("shape", ["no-lock-file", "lock-not-held", "once-mode", "dead-pid",
                                   "other-holder", "pid-reused"])
def test_capture_runs_whenever_the_collector_is_not_proven_alive(project, monkeypatch, shape):
    """Every shape short of "this root's watch collector holds this root's lock" captures: a copy
    of the record whose row names another root's live collector ("lock-not-held"), a one-shot
    `collect once` ("once-mode"), a row left by a collector that died ("dead-pid", "pid-reused")
    and a lock held by a process the row does not name ("other-holder")."""
    _open_session(project, "s-open")
    child = None
    try:
        if shape == "no-lock-file":
            _write_collector_row(project, pid=os.getpid())
            assert not _lock_path(project).exists()
            calls = _capture_runs(project, monkeypatch)
        elif shape == "lock-not-held":
            _lock_path(project).parent.mkdir(parents=True, exist_ok=True)
            _lock_path(project).touch()
            _write_collector_row(project, pid=os.getpid())
            calls = _capture_runs(project, monkeypatch)
        else:
            with _held_lock(project):
                if shape == "once-mode":
                    _write_collector_row(project, pid=os.getpid(), mode="once")
                elif shape == "dead-pid":
                    child = _other_live_process()
                    identity = observability._process_identity(child.pid)
                    child.kill(); child.wait()
                    _write_collector_row(project, pid=child.pid, identity=identity)
                elif shape == "other-holder":
                    child = _other_live_process()
                    _write_collector_row(project, pid=child.pid)
                else:                                   # this pid, an older process's identity
                    _write_collector_row(project, pid=os.getpid(), identity="boot:1")
                calls = _capture_runs(project, monkeypatch)
    finally:
        if child is not None and child.poll() is None:
            child.kill(); child.wait()
    assert len(calls) == 1
    assert observability.collector_alive(project) is False


# ------------------------------------------------------------------ the liveness check itself
def test_the_liveness_check_never_takes_the_collector_lock(project, monkeypatch):
    """A collector that starts at the moment a hook checks must never be refused its lock: the check
    reads /proc/locks and never calls flock."""
    _open_session(project, "s-open")
    with _live_collector(project):
        def refuse(*a, **k):
            raise AssertionError("the liveness check took a lock")
        monkeypatch.setattr(fcntl, "flock", refuse)
        assert observability.collector_alive(project) is True


def test_the_liveness_check_is_cheap_and_does_not_wait_on_a_record_writer(project):
    _open_session(project, "s-open")
    from alpaca import paths
    writer = sqlite3.connect(paths.db_path(str(project)), timeout=0, isolation_level=None)
    with _live_collector(project):
        writer.execute("BEGIN IMMEDIATE")                       # another process writing the record
        try:
            times = []
            for _ in range(50):
                start = time.perf_counter()
                assert observability.collector_alive(project) is True
                times.append(time.perf_counter() - start)
        finally:
            writer.execute("ROLLBACK")
            writer.close()
    # waiting on the writer would cost the record's busy timeout (10 s) on every call; a loaded
    # host only stretches a few calls, so the median stays well under the bound
    times.sort()
    assert times[len(times) // 2] < 0.1 and times[-1] < 2.0, times


def test_a_busy_or_unreadable_record_means_not_alive(project, monkeypatch):
    """Anything the check cannot read quickly counts as no collector, so the hook falls back to the
    op-008 capture rather than waiting."""
    _open_session(project, "s-open")
    with _live_collector(project):
        real = sqlite3.connect

        def busy(*a, **k):
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(sqlite3, "connect", busy)
        try:
            assert observability.collector_alive(project) is False
        finally:
            monkeypatch.setattr(sqlite3, "connect", real)
        monkeypatch.setattr(observability, "_PROC_LOCKS", str(Path(project, "no-such-proc-locks")))
        assert observability.collector_alive(project) is False


# ------------------------------------------------------------------ reading /proc/locks (t-334, review2 M1)
def _lock_line(n, info, pid):
    """One /proc/locks holder line for the file whose `os.stat` is `info`."""
    return "%d: FLOCK  ADVISORY  WRITE %d %02x:%02x:%d 0 EOF\n" % (
        n, pid, os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)


def _other_lines(count, start=1):
    """`count` lines of unrelated locks, about 60 bytes each (a long table on a busy host)."""
    return "".join("%d: POSIX  ADVISORY  WRITE %d 00:2a:%d 0 EOF\n" % (start + i, 100000 + i, 7000000 + i)
                   for i in range(count))


class _Table:
    """A /proc/locks stand-in: the n-th open serves the n-th table (the last one again after
    that), at most `page` bytes per read call, as the kernel serves its seq_file."""

    def __init__(self, tables, page=4086):
        self.tables, self.page, self.opens = list(tables), page, 0

    def open(self, path, mode="r", *args, **kwargs):
        if str(path) != observability._PROC_LOCKS:
            return open(path, mode, *args, **kwargs)
        data = self.tables[min(self.opens, len(self.tables) - 1)].encode("ascii")
        self.opens += 1
        page = self.page

        class Reader(io.RawIOBase):
            pos = 0

            def readable(self):
                return True

            def readinto(self, buffer):
                chunk = data[self.pos:self.pos + min(len(buffer), page)]
                buffer[:len(chunk)] = chunk
                self.pos += len(chunk)
                return len(chunk)
        raw = Reader()
        if "b" in mode:
            return raw
        return io.TextIOWrapper(io.BufferedReader(raw), encoding=kwargs.get("encoding") or "ascii",
                                errors=kwargs.get("errors"))


def test_a_holder_line_skipped_by_one_scan_is_found_by_the_next(project, monkeypatch):
    """review2 M1: another process took or dropped a lock between two page reads, so one scan of a
    long table missed the collector's line. The next scan sees it, so the check stays True."""
    _open_session(project, "s-open")
    with _live_collector(project):
        info = os.stat(_lock_path(project))
        holder = _lock_line(400, info, os.getpid())
        shifted = _other_lines(399)                              # the holder's line was skipped
        whole = _other_lines(399) + holder
        assert len(whole) > 4 * 4096
        table = _Table([shifted, whole])
        monkeypatch.setattr(observability, "open", table.open, raising=False)
        assert observability.collector_alive(project) is True
        assert table.opens == 2


def test_a_long_table_served_a_page_per_read_is_read_to_the_end(project, monkeypatch):
    """A table of 20 KB, served at most one page per read call: the holder's line at the end is
    read, in one scan."""
    _open_session(project, "s-open")
    with _live_collector(project):
        info = os.stat(_lock_path(project))
        table = _Table([_other_lines(330) + _lock_line(331, info, os.getpid())])
        monkeypatch.setattr(observability, "open", table.open, raising=False)
        assert observability.collector_alive(project) is True
        assert table.opens == 1


def test_a_real_table_file_longer_than_a_page_is_read_whole(project, monkeypatch):
    """The same with a real file standing in for /proc/locks, the holder's line past 16 KB."""
    _open_session(project, "s-open")
    with _live_collector(project):
        info = os.stat(_lock_path(project))
        fixture = Path(project, "proc-locks-fixture")
        fixture.write_text(_other_lines(300) + _lock_line(301, info, os.getpid()), encoding="ascii")
        assert fixture.stat().st_size > 4 * 4096
        monkeypatch.setattr(observability, "_PROC_LOCKS", str(fixture))
        assert observability.collector_alive(project) is True


def test_an_unheld_lock_costs_three_scans_and_no_more(project, monkeypatch):
    """No holder in any scan: the check gives up after three full scans (bounded), so a copy of the
    record or a stopped collector still falls back to the hook's own capture at once."""
    _open_session(project, "s-open")
    _lock_path(project).parent.mkdir(parents=True, exist_ok=True)
    _lock_path(project).touch()
    _write_collector_row(project, pid=os.getpid())
    table = _Table([_other_lines(300)])
    monkeypatch.setattr(observability, "open", table.open, raising=False)
    times = []
    for _ in range(5):                                  # best of 5: a loaded host stalls one reading
        table.opens = 0
        start = time.perf_counter()
        assert observability.collector_alive(project) is False
        times.append(time.perf_counter() - start)
        assert table.opens == 3
    assert min(times) < 0.25, times
