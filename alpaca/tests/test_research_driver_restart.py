"""A restart of the driver's own CLI process (in-session-runtime.md section 24.2, t-360 FU-7).

The runtime removes the driver's shell snapshot when the CLI process exits, and the new process
writes a new one at its first Bash command. The record holds the restart as a `session-start` of
the driver session (source `resume`, `startup`, or none) newer than the newest `research-driver`
event. That makes the driver gone: the run ends RUN-DRIVER-GONE, and the driver's `research resume`
re-finds the snapshot and takes a new ledger. A compaction or a clear keeps the process and is no
restart. The removal of the stored snapshot is no change once a restart row is on the record, so
the resume's engine check passes; with no restart row it is `removed` and ends the run (a live CLI
with no snapshot runs a login shell, check round 2 N1).

Synthetic data only: a project, a home and a config folder of the test's own; invented session ids
and snapshot names.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from alpaca import db
from alpaca.research import jobs, session_engine as eng, session_runtime
from alpaca.tests import research_session_kit as rk


def _put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _tick():
    time.sleep(0.03)


def _finder(monkeypatch, value):
    monkeypatch.setattr(eng, "find_driver_snapshot", lambda pid=None: value)


def _forbid_finder(monkeypatch):
    monkeypatch.setattr(eng, "find_driver_snapshot",
                        lambda pid=None: pytest.fail("looked for a snapshot outside a driver verb"))


def _world(project, monkeypatch):
    """A scope whose driver the real `register_driver` registers with snapshot A. (world, A)."""
    w = rk.JudgeWorld(monkeypatch, project)
    a = os.path.join(w.ctx["cfg"], "shell-snapshots", "snapshot-bash-1700000000101-drvA01.sh")
    _put(a, "export PATH=/usr/bin\n")
    _put(os.path.join(w.ctx["cfg"], "shell-snapshots", "snapshot-bash-1700000000102-oth102.sh"),
         "export PATH=/usr/bin\n")
    _finder(monkeypatch, a)
    w.driver = session_runtime.register_driver(w.conn, w.root, w.SCOPE, w.sid, cause="start",
                                               level=6, runtime="claude", run=w.SCOPE)
    return w, a


def _life(w, kind, data=None, session=None):
    db.append_event(w.conn, session=session or w.sid, actor="hook", kind=kind, data=data or {})


def _watch_diff(w):
    drv = jobs.newest_driver(w.conn, w.SCOPE)
    lev = jobs.current_ledger_event(w.conn, w.SCOPE, w.SCOPE)
    ledger = eng.load_ledger((lev["data"] or {}).get("context"))
    _cur, diff = jobs.context_now(w.conn, w.root, w.SCOPE, drv, ledger,
                                  check_session=drv["data"].get("session"))
    return diff


def _snapshot_changes(w):
    return [(x["path"], x["change"]) for x in _watch_diff(w) if "shell-snapshots" in x["path"]]


@pytest.fixture
def world(project, monkeypatch):
    w, a = _world(project, monkeypatch)
    w.a = a
    yield w
    w.close()


GONE = {
    "start resume": ("session-start", {"source": "resume"}),
    "start startup": ("session-start", {"source": "startup"}),
    "start with no source": ("session-start", {}),
    "start with an unknown source": ("session-start", {"source": "something-new"}),
    "end": ("session-end", {}),
}
KEPT = {
    "start compact": ("session-start", {"source": "compact"}),
    "start clear": ("session-start", {"source": "clear"}),
}


@pytest.mark.parametrize("name", sorted(GONE))
def test_a_restart_or_an_end_after_the_driver_event_makes_the_driver_gone(world, name):
    w = world
    assert session_runtime.driver_gone(w.conn, w.root, w.SCOPE) is False
    kind, data = GONE[name]
    _life(w, kind, data)
    assert session_runtime.driver_gone(w.conn, w.root, w.SCOPE) is True


@pytest.mark.parametrize("name", sorted(KEPT))
def test_a_compaction_or_a_clear_keeps_the_driver(world, name):
    w = world
    kind, data = KEPT[name]
    _life(w, kind, data)
    assert session_runtime.driver_gone(w.conn, w.root, w.SCOPE) is False


@pytest.mark.parametrize("after", sorted(KEPT))
@pytest.mark.parametrize("name", sorted(GONE))
def test_a_restart_stays_gone_after_a_compaction_or_a_clear(world, name, after):
    """Any restart row newer than the driver event decides, not the newest row: a compaction or a
    clear runs inside the new process, so it does not bring the old process back (check round 1,
    B1)."""
    w = world
    kind, data = GONE[name]
    _life(w, kind, data)
    assert session_runtime.driver_gone(w.conn, w.root, w.SCOPE) is True
    kind, data = KEPT[after]
    _life(w, kind, data)
    assert session_runtime.driver_gone(w.conn, w.root, w.SCOPE) is True
    _life(w, "session-start", {"source": "compact"})
    assert session_runtime.driver_gone(w.conn, w.root, w.SCOPE) is True


@pytest.mark.parametrize("rows", [[], ["start compact"], ["start clear"], ["start compact", "start clear"]]
                         + [[n] for n in sorted(GONE)] + [["start compact", n] for n in sorted(GONE)]
                         + [[n, "start compact"] for n in sorted(GONE)])
def test_the_removal_skip_reads_the_rows_the_driver_watch_reads(world, rows):
    """Check round 2 N1: the stored snapshot's removal is no change only after a restart row of
    the driver session; with none (a compaction or a clear keeps the process) it is `removed`, so
    an in-process removal, which makes the live CLI run a login shell, ends the run."""
    w, a = world, world.a
    for name in rows:
        kind, data = dict(GONE, **KEPT)[name]
        _life(w, kind, data)
    os.remove(a)
    drv = jobs.newest_driver(w.conn, w.SCOPE)
    restarted = any(n in GONE for n in rows)
    assert jobs.driver_restarted(w.conn, drv) is restarted
    assert session_runtime.driver_gone(w.conn, w.root, w.SCOPE) is restarted
    assert _snapshot_changes(w) == ([] if restarted else [(a, "removed")])


def test_the_restart_rule_needs_a_driver_event_and_a_session():
    assert jobs.driver_restarted(None, None) is False
    assert jobs.driver_restarted(None, {"id": 3, "data": {}}) is False


def test_compactions_before_and_after_a_restart_do_not_hide_it(world):
    w = world
    _life(w, "session-start", {"source": "compact"})
    _life(w, "session-start", {"source": "clear"})
    assert session_runtime.driver_gone(w.conn, w.root, w.SCOPE) is False
    _life(w, "session-start", {"source": "resume"})
    _life(w, "session-start", {"source": "compact"})
    _life(w, "session-start", {"source": "clear"})
    assert session_runtime.driver_gone(w.conn, w.root, w.SCOPE) is True


@pytest.mark.parametrize("after", sorted(KEPT))
def test_a_restart_then_a_compaction_still_guards_the_new_snapshot(world, monkeypatch, after):
    """The masked shape of check round 1: the old process removes A, the record holds session-end,
    session-start (resume) and then a compaction or a clear of the new process, and the new process
    writes and rewrites B. B is not a member and A's removal is no change, so the guard is the driver
    gone: the run ends RUN-DRIVER-GONE and the driver's resume makes B the member."""
    w, a = world, world.a
    os.remove(a)
    _life(w, "session-end", {})
    _life(w, "session-start", {"source": "resume"})
    kind, data = KEPT[after]
    _life(w, kind, data)
    b = os.path.join(w.ctx["cfg"], "shell-snapshots", "snapshot-bash-1700000000901-drvB09.sh")
    _put(b, "export PATH=/usr/bin\n")
    _tick()
    _put(b, "alias ls='rm -rf'\n")
    _forbid_finder(monkeypatch)
    assert _snapshot_changes(w) == []
    assert session_runtime.driver_gone(w.conn, w.root, w.SCOPE) is True
    # the driver's resume writes a newer driver event; the old restart rows no longer count
    _finder(monkeypatch, b)
    session_runtime.register_driver(w.conn, w.root, w.SCOPE, w.sid, cause="resume", level=6,
                                    runtime="claude", run=w.SCOPE)
    _forbid_finder(monkeypatch)
    assert session_runtime.driver_gone(w.conn, w.root, w.SCOPE) is False
    _tick()
    _put(b, "export PATH=/evil:/usr/bin\n")
    assert _snapshot_changes(w) == [(b, "touched")]


def test_a_start_older_than_the_driver_event_changes_nothing(project, monkeypatch):
    w = rk.JudgeWorld(monkeypatch, project)
    try:
        _life(w, "session-start", {"source": "resume"})
        a = os.path.join(w.ctx["cfg"], "shell-snapshots", "snapshot-bash-1700000000111-drvA11.sh")
        _put(a, "export PATH=/usr/bin\n")
        _finder(monkeypatch, a)
        session_runtime.register_driver(w.conn, w.root, w.SCOPE, w.sid, cause="resume", level=6,
                                        runtime="claude", run=w.SCOPE)
        assert session_runtime.driver_gone(w.conn, w.root, w.SCOPE) is False
        # a start of another session is not the driver's
        _life(w, "session-start", {"source": "resume"}, session="de7ea525-0000-4000-8000-000000000002")
        assert session_runtime.driver_gone(w.conn, w.root, w.SCOPE) is False
    finally:
        w.close()


def test_the_measured_restart_is_the_driver_gone_and_the_resume_guards_the_new_snapshot(
        world, monkeypatch):
    """The shape measured on the host (restart-shape.txt): the old process removes A at its exit,
    the record holds session-end then session-start (resume), and the new process writes B at its
    first Bash call."""
    w, a = world, world.a
    os.remove(a)
    _life(w, "session-end", {})
    _life(w, "session-start", {"source": "resume"})
    b = os.path.join(w.ctx["cfg"], "shell-snapshots", "snapshot-bash-1700000000201-drvB02.sh")
    _put(b, "export PATH=/usr/bin\n")
    _forbid_finder(monkeypatch)
    # the watch: no context change (the removal is no change, B is another file), the driver is gone
    assert _snapshot_changes(w) == []
    assert session_runtime.driver_gone(w.conn, w.root, w.SCOPE) is True
    # the resume: the engine check of the same session passes, register_driver re-finds B
    session_runtime.engine_check(w.conn, w.root, w.SCOPE, w.sid)
    _finder(monkeypatch, b)
    ev = session_runtime.register_driver(w.conn, w.root, w.SCOPE, w.sid, cause="resume", level=6,
                                         runtime="claude", run=w.SCOPE)
    data = ev["data"] if isinstance(ev["data"], dict) else json.loads(ev["data"])
    assert data["driver_snapshot"] == b
    assert session_runtime.driver_gone(w.conn, w.root, w.SCOPE) is False
    _forbid_finder(monkeypatch)
    # B is now the member: its rewrite is a change; a file at A's old path is another session's
    _tick()
    _put(a, "export PATH=/evil:/usr/bin\n")
    assert _snapshot_changes(w) == []
    _put(b, "export PATH=/evil:/usr/bin\n")
    assert _snapshot_changes(w) == [(b, "touched")]


def test_an_unclean_restart_is_the_driver_gone_and_the_resume_guards_the_new_snapshot(
        world, monkeypatch):
    """A kill or a crash: A stays, the record holds a session-start (resume) only, B is written."""
    w, a = world, world.a
    _life(w, "session-start", {"source": "resume"})
    b = os.path.join(w.ctx["cfg"], "shell-snapshots", "snapshot-bash-1700000000401-drvB04.sh")
    _put(b, "export PATH=/usr/bin\n")
    _forbid_finder(monkeypatch)
    assert _snapshot_changes(w) == []
    assert session_runtime.driver_gone(w.conn, w.root, w.SCOPE) is True
    session_runtime.engine_check(w.conn, w.root, w.SCOPE, w.sid)
    _finder(monkeypatch, b)
    session_runtime.register_driver(w.conn, w.root, w.SCOPE, w.sid, cause="resume", level=6,
                                    runtime="claude", run=w.SCOPE)
    _forbid_finder(monkeypatch)
    _tick()
    _put(b, "export PATH=/evil:/usr/bin\n")
    assert _snapshot_changes(w) == [(b, "touched")]
