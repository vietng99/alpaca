"""t-070 RC4: one capture runner with a deadline enforced from outside the work, and one outcome record
for every path (fix plan RC4; findings A-4, A-6, A-8; the t-334 review's session-views follow-up).

The cause these tests pin (A-4, `.alpaca/proofs/op-008/t-070.logs/rc4/`): the hooks' hard deadline was
a SIGALRM handled in the hook's own process, and a call blocked in the kernel (a `pwrite64` to a SQLite
temp file of the echo-index sort, 56 s on a loaded disk) holds that signal off until it returns. So:

- every entry point (Stop, PreCompact, SessionEnd, the Codex checkpoint verb, the collector's wiki
  capture and preservation jobs, the session-views refresh) is fed work that outlasts its deadline
  and does not let a signal in (`runner_fakes.hold` blocks SIGALRM, as the kernel did), and must
  return within its deadline plus a margin and leave the same outcome record;
- the echo-index read no longer sorts the corpus through temp files (`absorb.echo_rows`);
- with the observability flag on and no collector alive, `bin/alpaca doctor`, `wiki status` and one
  rate-limited record event say so (A-8).

The hook tests run each hook as its own process (`python -m alpaca.hooks.<name>`, as Claude Code
runs it). A `sitecustomize` on the test's PYTHONPATH puts `hold` in place of `service.capture` in
every interpreter that starts, so the same test reads the hook's behaviour on a tree without the
runner (red on 1f4bc0e4e) and with it.
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from alpaca import db, observability

try:                     # absent on a tree before RC4: the tests then fail on what they pin, not on import
    from alpaca import capture_runner
except ImportError:      # pragma: no cover - only the red-first run on 1f4bc0e4e takes this
    capture_runner = None

PACKAGE_ROOT = str(Path(db.__file__).resolve().parents[1])
DEADLINE_S = 3.0           # ALPACA_HOOK_DEADLINE_S of the hook processes below
MARGIN_S = 2.5             # the in-process waits below, which a defect holds for 10 s
HOOK_MARGIN_S = 8.0        # interpreter start, the outcome record and a loaded host
HOLD_S = 30.0              # how long the held capture would run if nothing stopped it; a hook
                           # that waits it out returns after 30 s, far past DEADLINE_S + HOOK_MARGIN_S

SITECUSTOMIZE = '''import os
if os.environ.get("ALPACA_TEST_HOLD_CAPTURE"):
    from alpaca.tests import runner_fakes
    runner_fakes.install_hold()
'''


def _events(root, kind=None, session=None):
    conn = db.connect_readonly(str(root))
    try:
        out = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id")]
    finally:
        conn.close()
    for row in out:
        row["data"] = json.loads(row["data"]) if row["data"] else None
    return [r for r in out if (kind is None or r["kind"] == kind)
            and (session is None or r["session"] == session)]


def _open_session(project, sid, n=3):
    from alpaca import cli
    if not Path(project, ".alpaca", "alpaca.db").exists():
        cli.main(["init"])
    conn = db.connect(str(project))
    try:
        for i in range(n):
            db.append_event(conn, session=sid, actor="agent", kind="result",
                            data={"body": "turn %d of the lantern rota" % i})
    finally:
        conn.close()


def _hold_env(tmp_path, monkeypatch, hold_s=HOLD_S):
    """The environment of a hook process (and of every child it starts) whose capture holds."""
    inject = tmp_path / "inject"
    inject.mkdir(exist_ok=True)
    (inject / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    marks = tmp_path / "hold-pids.txt"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(inject), PACKAGE_ROOT]),
               ALPACA_TEST_HOLD_CAPTURE="1", ALPACA_TEST_HOLD_S=str(hold_s),
               ALPACA_TEST_HOLD_MARK=str(marks), ALPACA_HOOK_DEADLINE_S=str(DEADLINE_S),
               PYTHONDONTWRITEBYTECODE="1")
    for name in ("ALPACA_ROOT", "CLAUDE_PROJECT_DIR", "ALPACA_SESSION_ID"):
        env.pop(name, None)
    return env, marks


def _run_hook(project, name, payload, env):
    start = time.monotonic()
    proc = subprocess.run([sys.executable, "-m", "alpaca.hooks." + name], input=json.dumps(payload),
                          capture_output=True, text=True, cwd=str(project), env=env, timeout=120)
    return time.monotonic() - start, proc


def _gone_or_zombie(pid, within_s=5.0):
    """The held child was killed: it is gone or a zombie waiting to be reaped."""
    end = time.monotonic() + within_s
    while time.monotonic() < end:
        try:
            state = Path("/proc/%d/stat" % pid).read_text().rsplit(") ", 1)[1].split()[0]
        except (OSError, IndexError):
            return True
        if state in ("Z", "X"):
            return True
        time.sleep(0.05)
    return False


# ------------------------------------------------------------------ the three hooks
HOOKS = [
    ("stop", "Stop"),
    ("pre_compact", "PreCompact"),
    ("session_end", "SessionEnd"),
]


@pytest.mark.parametrize("name, event", HOOKS)
def test_a_hook_whose_capture_holds_off_signals_returns_by_its_deadline_and_records_the_deferral(
        project, tmp_path, monkeypatch, name, event):
    """A-4 and A-6: the capture blocks with SIGALRM held off (the kernel did this to the hook's alarm
    for 56 s). The hook still returns within its deadline plus the margin, its decision output is
    unchanged, the child that held is killed, and the record holds one `capture-deferred` for this
    hook, as a capture its budget cut short leaves."""
    sid = "s-hold-" + name
    _open_session(project, sid)
    Path(project, "style", "banned.txt").write_text("delve\n", encoding="utf-8")
    env, marks = _hold_env(tmp_path, monkeypatch)
    payload = {"session_id": sid, "cwd": str(project), "hook_event_name": event, "reason": "other",
               "trigger": "auto", "reply": "We should delve in."}
    seconds, proc = _run_hook(project, name, payload, env)
    assert proc.returncode == 0
    assert seconds < DEADLINE_S + HOOK_MARGIN_S, (seconds, proc.stderr[-2000:])
    if name == "stop":                                   # the decision line is the hook's output
        assert json.loads(proc.stdout)["decision"] == "block"
    else:
        assert proc.stdout == ""
    deferred = _events(project, "capture-deferred", sid)
    assert [e["data"] for e in deferred] == [{"hook": name, "through_event": 0, "remaining": 7}]
    assert not _events(project, "drain-failed", sid)
    held = [int(x) for x in marks.read_text().split()]
    assert len(held) == 1 and held[0] != proc.returncode
    assert _gone_or_zombie(held[0])


def test_a_hook_whose_capture_fails_records_drain_failed_and_keeps_its_output(project, tmp_path):
    """A failure in the child is recorded as `drain-failed` with the screened error, the same record
    a capture failure left before the runner, and the hook's output does not change."""
    sid = "s-broken"
    _open_session(project, sid)
    from alpaca.hooks import pre_compact
    old = capture_runner.CAPTURE
    capture_runner.CAPTURE = "alpaca.tests.runner_fakes:broken"
    try:
        out = pre_compact.handle({"session_id": sid, "cwd": str(project), "trigger": "auto"})
    finally:
        capture_runner.CAPTURE = old
    assert out == {"error": "RuntimeError: store exploded"}
    failed = _events(project, "drain-failed", sid)
    assert [e["data"] for e in failed] == [{"where": "pre_compact", "error": "RuntimeError: store exploded"}]
    assert not _events(project, "capture-deferred", sid)


def test_a_hook_capture_that_finishes_lands_its_events_through_the_child(project):
    """The normal case: the child captures and the hook records nothing beyond the capture."""
    from alpaca.hooks import session_end
    from alpaca.wiki import service
    sid = "s-normal"
    _open_session(project, sid)
    session_end.handle({"session_id": sid, "cwd": str(project), "reason": "exit"})
    assert not _events(project, "capture-deferred", sid) and not _events(project, "drain-failed", sid)
    assert service.status(project)["capture"]["captured_events"] >= 4      # the notes and session-end


# ------------------------------------------------------------------ the Codex checkpoint verb
def test_the_checkpoint_verb_returns_by_its_deadline_when_the_capture_holds(project, tmp_path, monkeypatch):
    """The verb has no hook window: its capture may run its budget (`stop.CAPTURE_CAP_S`) plus the
    runner's grace, then the runner stops it and records `capture-deferred` (hook `checkpoint`)."""
    from alpaca import operator
    from alpaca.hooks import stop
    from alpaca.tests import runner_fakes
    from alpaca.wiki import service
    _open_session(project, "codex-a")
    env, marks = _hold_env(tmp_path, monkeypatch)
    for key in ("PYTHONPATH", "ALPACA_TEST_HOLD_CAPTURE", "ALPACA_TEST_HOLD_S", "ALPACA_TEST_HOLD_MARK"):
        monkeypatch.setenv(key, env[key])
    monkeypatch.setattr(runner_fakes, "REAL", service.capture)
    monkeypatch.setattr(service, "capture", runner_fakes.hold)       # a tree that captures in-process
    monkeypatch.setattr(stop, "CAPTURE_CAP_S", 1.0)
    if capture_runner is not None:
        monkeypatch.setattr(capture_runner, "GRACE_S", 1.0)
        # the child is isolated (t-070 round 2 C-A) and runs no sitecustomize of the test's: it is
        # asked to run `hold` by name
        monkeypatch.setattr(capture_runner, "CAPTURE", "alpaca.tests.runner_fakes:hold")
    start = time.monotonic()
    operator.run("start", str(project), "codex-a", operator="codex")
    operator.run("checkpoint", str(project), "codex-a", operator="codex", note="half way")
    seconds = time.monotonic() - start
    assert seconds < 2.0 + HOOK_MARGIN_S, seconds
    deferred = _events(project, "capture-deferred", "codex-a")
    assert [e["data"] for e in deferred] == [{"hook": "checkpoint", "through_event": 0, "remaining": 7}]


# ------------------------------------------------------------------ the runner itself
def test_a_killed_child_leaves_no_half_transaction_and_no_lock(project):
    """A child killed inside a wiki write transaction: WAL drops the uncommitted write, the kernel
    drops its locks, and the next capture lands as usual."""
    from alpaca.wiki import service
    from alpaca.wiki.config import Config
    from alpaca.wiki.store.db import DB
    sid = "s-half"
    _open_session(project, sid)
    service.capture(project, sessions=[sid], session_events=True, deadline_s=None)
    result = capture_runner.run_process("alpaca.tests.runner_fakes:hold_in_transaction", {}, root=project,
                                        kill_s=4.0, progress=True)
    assert result.status == "overrun" and result.progress == {"through_event": 0, "done": 0, "total": 1}
    store = DB(Config.for_project(project))
    try:
        assert store.get_meta("rc4-half-written") is None
    finally:
        store.close()
    _open_session(project, sid, n=1)
    report = service.capture(project, sessions=[sid], session_events=True, uncaptured_only=True,
                             deadline_s=30)
    assert report["deferred"] is None and report["notes"] >= 1


def test_the_child_reports_ok_failed_and_a_silent_exit(project):
    ok = capture_runner.run_process("alpaca.tests.runner_fakes:echo", {"value": [1, "two"]}, root=project,
                                    kill_s=20.0)
    assert ok.status == "ok" and ok.value == {"root": str(project), "value": [1, "two"]}
    failed = capture_runner.run_process("alpaca.tests.runner_fakes:broken", {}, root=project, kill_s=20.0)
    assert failed.status == "failed" and failed.error == "RuntimeError: store exploded"
    silent = capture_runner.run_process("alpaca.tests.runner_fakes:vanish", {}, root=project, kill_s=20.0)
    assert silent.status == "failed" and "no report" in silent.error


def test_the_outcome_record_is_one_shape_for_every_ending():
    where = "stop"
    ok = capture_runner.Result("ok", value={"deferred": {"through_event": 9, "remaining": 2},
                                            "missing_transcripts": [{"session": "s", "error": "gone"}]})
    assert capture_runner.outcome_events(where, ok) == [
        ("capture-deferred", {"hook": "stop", "through_event": 9, "remaining": 2}),
        ("drain-failed", {"where": "stop", "error": "transcript unavailable: s: gone"})]
    over = capture_runner.Result("overrun", progress={"through_event": 4, "done": 3, "total": 10})
    assert capture_runner.outcome_events(where, over) == [
        ("capture-deferred", {"hook": "stop", "through_event": 4, "remaining": 7})]
    early = capture_runner.Result("overrun")
    assert capture_runner.outcome_events(where, early) == [
        ("capture-deferred", {"hook": "stop", "through_event": 0, "remaining": None})]
    busy = capture_runner.Result("busy")
    assert capture_runner.outcome_events("collector", busy, batch=5) == [
        ("capture-deferred", {"hook": "collector", "through_event": 0, "remaining": 5})]
    failed = capture_runner.Result("failed", error="OSError: password is hunter22x")
    (kind, data), = capture_runner.outcome_events(where, failed)
    assert kind == "drain-failed" and data["where"] == "stop"
    assert data["error"].startswith("OSError: password is ") and "hunter22x" not in data["error"]
    assert capture_runner.outcome_events(where, capture_runner.Result("ok", value={})) == []


def test_a_lane_abandons_a_job_past_its_budget_and_answers_busy_until_it_ends():
    lane = capture_runner.Lane("test")
    release = threading.Event()

    def slow(progress=None):
        progress(through_event=3, done=1, total=4)
        release.wait(10)
        return "late"
    start = time.monotonic()
    first = lane.run(slow, budget_s=0.3, progress=True)
    assert first.status == "overrun" and time.monotonic() - start < 2.0
    assert first.progress == {"through_event": 3, "done": 1, "total": 4}
    assert lane.run(lambda: "next", budget_s=0.3).status == "busy"
    release.set()
    end = time.monotonic() + 5
    while lane.busy_since() is not None and time.monotonic() < end:
        time.sleep(0.01)
    again = lane.run(lambda: "next", budget_s=5)
    assert again.status == "ok" and again.value == "next"

    def boom():
        raise KeyError("x")
    failed = lane.run(boom, budget_s=5)
    assert failed.status == "failed" and isinstance(failed.exc, KeyError)


# ------------------------------------------------------------------ the collector
def _collector_root(tmp_path, n=3):
    root = str(tmp_path / "collector")
    os.makedirs(root)
    conn = db.connect(root)
    for i in range(n):
        db.append_event(conn, session="s-col", actor="agent", kind="result", data={"body": "note %d" % i})
    return root, conn


def _wait_free(lane, within_s=15.0):
    end = time.monotonic() + within_s
    while lane.busy_since() is not None and time.monotonic() < end:
        time.sleep(0.02)
    assert lane.busy_since() is None


def test_the_collector_abandons_a_wiki_capture_past_its_budget_and_the_loop_goes_on(tmp_path, monkeypatch):
    """The wiki consumer's capture blocks; the loop waits `CAPTURE_BUDGET_S`, abandons it, keeps the
    checkpoint, records one `capture-deferred` (hook `collector`), and the other consumers run. While
    the abandoned capture still runs, a retry answers busy and records nothing new; once it ends, the
    retry lands the batch."""
    from alpaca.observability import consumers
    root, conn = _collector_root(tmp_path)
    release = threading.Event()
    real = consumers._capture_wiki
    seen = []

    def blocked(root_, rows, sessions, progress=None, **kwargs):
        if progress is not None:
            progress(through_event=0, done=0, total=len(rows))
        release.wait(10)
        return {"notes": 0}
    monkeypatch.setattr(consumers, "_capture_wiki", blocked)
    monkeypatch.setattr(consumers, "CAPTURE_BUDGET_S", 0.5, raising=False)
    monkeypatch.setattr(consumers, "CONSUMERS", {"wiki": consumers._wiki,
                                                 "other": lambda root_, rows: seen.extend(rows)})
    try:
        start = time.monotonic()
        result = consumers.consume(root, conn)
        assert time.monotonic() - start < 0.5 + MARGIN_S
        assert result["wiki"]["status"] == "failed" and result["wiki"]["error"] == "CaptureAbandoned"
        # the other consumer ran over the batch, and the collector's own outcome event after it
        assert result["other"]["status"] == "ok"
        assert [r["kind"] for r in seen] == ["result"] * 3 + ["capture-deferred"]
        checkpoint = conn.execute("SELECT event_id FROM obs_consumer_checkpoint WHERE consumer='wiki'").fetchone()
        assert checkpoint[0] == 0
        deferred = _events(root, "capture-deferred")
        assert [e["data"] for e in deferred] == [{"hook": "collector", "through_event": 0, "remaining": 3}]
        again = consumers.consume(root, conn, retry_consumers=("wiki",))
        assert again["wiki"]["error"] == "CaptureAbandoned"
        assert len(_events(root, "capture-deferred")) == 1                 # quiet: the same outcome
    finally:
        release.set()
        _wait_free(consumers.LANES["wiki"])
    monkeypatch.setattr(consumers, "_capture_wiki", lambda root_, rows, sessions, progress=None, **kw: {"notes": len(rows)})
    done = consumers.consume(root, conn, retry_consumers=("wiki",))
    assert done["wiki"]["status"] == "ok"
    assert db.meta_get(conn, "capture:last-outcome:collector:s-col") is None   # a clean run clears it
    conn.close()
    assert real is not None


def test_a_collector_capture_failure_is_recorded_as_drain_failed_once(tmp_path, monkeypatch):
    """A-8 (a): a collector capture that fails leaves `drain-failed` (where `collector`), as a hook's
    does, besides its obs issue; the retries of the same batch add no second one."""
    from alpaca.observability import consumers
    root, conn = _collector_root(tmp_path)

    def broken(root_, rows, sessions, progress=None, **kwargs):
        raise OSError("disk said no")
    monkeypatch.setattr(consumers, "_capture_wiki", broken)
    monkeypatch.setattr(consumers, "CONSUMERS", {"wiki": consumers._wiki})
    for _ in range(3):
        assert consumers.consume(root, conn, retry_consumers=("wiki",))["wiki"]["error"] == "OSError"
    failed = _events(root, "drain-failed")
    assert [e["data"] for e in failed] == [{"where": "collector", "error": "OSError: disk said no"}]
    conn.close()


def test_a_preservation_job_whose_snapshot_hangs_is_abandoned_and_retried(tmp_path, monkeypatch):
    from alpaca import transcripts
    from alpaca.observability import consumers
    root, conn = _collector_root(tmp_path)
    consumers.queue_snapshot(conn, "s-col", event_id=1)
    release = threading.Event()

    def hung(root_, session, *a, **k):
        release.wait(10)
        return {"mode": "none"}
    monkeypatch.setattr(transcripts, "snapshot", hung)
    monkeypatch.setattr(consumers, "CAPTURE_BUDGET_S", 0.5, raising=False)
    try:
        start = time.monotonic()
        result = consumers.run_snapshot_jobs(root, conn)
        assert time.monotonic() - start < 0.5 + MARGIN_S
        assert result == {"completed": 0, "failed": 1}
        job = conn.execute("SELECT status, error FROM obs_projection_job").fetchone()
        assert tuple(job) == ("retry", "CaptureAbandoned")
    finally:
        release.set()
        _wait_free(consumers.LANES["wiki"])
        conn.close()


def test_a_view_refresh_past_its_budget_is_abandoned_and_stays_pending(tmp_path, monkeypatch):
    """The t-334 follow-up: an analytics build that hangs (a transcript read that never returns)
    held the whole collector loop. Now the loop waits `VIEW_BUDGET_S`, the view stays pending with an
    open issue, and the pad view and the other consumers are not held."""
    from alpaca.observability import consumers
    root, conn = _collector_root(tmp_path)
    folder = Path(root, ".alpaca", "observability")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "enabled").touch()
    release = threading.Event()
    pads = []
    monkeypatch.setattr(consumers, "VIEW_WRITERS", {"pad": lambda r: pads.append(r),
                                                    "analytics": lambda r: release.wait(10)})
    monkeypatch.setattr(consumers, "VIEW_BUDGET_S", 0.5, raising=False)
    try:
        start = time.monotonic()
        state = consumers._refresh_views(root, [{"id": 1, "kind": "session-end", "session": "s-col"}])
        assert time.monotonic() - start < 0.5 + MARGIN_S
        assert pads == [root]
        assert state["analytics"]["pending"] is True and state["analytics"]["error"] == "ViewAbandoned"
        issues = [r[0] for r in conn.execute("SELECT code FROM obs_issue WHERE resolved_at IS NULL")]
        assert "analytics-refresh-failed" in issues
    finally:
        release.set()
        _wait_free(consumers.LANES["views"])
        conn.close()


# ------------------------------------------------------------------ the blocking call itself
def test_the_echo_index_read_sorts_nothing_through_temp_files(project):
    """A-4's blocking call: `ORDER BY block_id` over `idx_blocks_status` made SQLite sort every active
    block's text through temp files (`USE TEMP B-TREE FOR ORDER BY`). The read then walked the primary
    key in order (RC4), and now takes the active rowids and their rows in rowid order (t-462,
    `absorb.echo_rows`): neither sorts through temp files, and it returns the same rows."""
    from alpaca.wiki import service
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest import absorb
    from alpaca.wiki.store import write
    _open_session(project, "s-echo", n=5)
    service.capture(project, sessions=["s-echo"], session_events=True, deadline_s=None)
    import sqlite3
    conn = sqlite3.connect(Config.for_project(project).db_path.as_uri() + "?mode=ro", uri=True)
    try:
        before = "SELECT block_id,doc_id,text FROM blocks WHERE status='active' ORDER BY block_id"
        plan = lambda sql: " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql))
        assert "TEMP B-TREE" in plan(before)
        for sql in (absorb.ECHO_ROWIDS_SQL, absorb.ECHO_ROWS_SQL % "1,2", write.ACTIVE_BLOCKS_SQL):
            assert "TEMP B-TREE" not in plan(sql), plan(sql)
        read = sorted((block_id, doc_id, text) for block_id, text, doc_id in absorb.echo_rows(conn))
        assert read == conn.execute(before).fetchall()
        assert len(read) >= 5
    finally:
        conn.close()


# ------------------------------------------------------------------ A-8: flag on, no collector
def _flag_on(project):
    folder = Path(project, ".alpaca", "observability")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "enabled").touch()


def test_flag_on_and_no_collector_shows_in_doctor_and_wiki_status(project, capsys):
    from alpaca import cli
    from alpaca.wiki import service
    cli.main(["init"])
    assert observability.capture_path(project)["warning"] is None
    assert service.status(project)["collector"]["captured_by"] == "hooks"
    _flag_on(project)
    path = service.status(project)["collector"]
    assert path == {"observability_enabled": True, "collector_alive": False, "captured_by": "nobody",
                    "warning": "observability enabled but no collector alive"}
    capsys.readouterr()
    cli.main(["doctor"])
    out = capsys.readouterr().out
    line = [l for l in out.splitlines() if " collector " in l]
    assert line and line[0].startswith("WARN") and "observability enabled but no collector alive" in line[0]


def test_flag_on_and_a_live_collector_is_not_a_warning(project):
    import fcntl
    from alpaca import cli
    cli.main(["init"])
    _flag_on(project)
    lock = Path(project, ".alpaca", "observability", "collector.lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        conn = db.connect(project)
        with db.transaction(conn):
            db.meta_set(conn, "obs:collector", json.dumps({
                "pid": os.getpid(), "process_identity": observability._process_identity(os.getpid()),
                "heartbeat": "2026-10-07T00:00:00+00:00", "mode": "watch"}))
        conn.close()
        path = observability.capture_path(project)
        assert path["collector_alive"] is True and path["warning"] is None
        assert observability.checks(project, None)[0][1] == "ok"
    finally:
        os.close(fd)


def test_the_hooks_note_an_absent_collector_once_per_interval(project, monkeypatch):
    """With the flag on the hooks capture nothing; with no collector alive they say so in the record,
    one `capture-no-collector` per `ABSENT_NOTE_S`, never per turn (the kind is t-070 round 2 C-D,
    I-5; it was `drain-failed`)."""
    import io
    from alpaca.hooks import common, pre_compact, session_end, stop
    _open_session(project, "s-flag")
    _flag_on(project)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(common, "read_stdin", lambda *a, **k: {"session_id": "s-flag", "cwd": project})
    for _ in range(3):
        stop.main.__wrapped__()
    pre_compact.handle({"session_id": "s-flag", "cwd": project})
    session_end.handle({"session_id": "s-flag", "cwd": project})
    notes = _events(project, "capture-no-collector")
    assert len(notes) == 1 and notes[0]["data"]["where"] == "stop"
    assert notes[0]["data"]["error"].startswith(capture_runner.NO_COLLECTOR)
    assert not _events(project, "capture-deferred") and not _events(project, "drain-failed")
    monkeypatch.setattr(capture_runner, "ABSENT_NOTE_S", 0.0)
    stop.main.__wrapped__()
    assert len(_events(project, "capture-no-collector")) == 2
    assert not _events(project, "drain-failed")


def _laneOf(events):
    """`laneOf` of the cockpit (`alpaca/web/cockpit.js`) for each event, run by node."""
    import shutil
    node = shutil.which("node")
    if node is None:                                  # pragma: no cover - node is on the dev host
        pytest.skip("node is not installed")
    script = Path(PACKAGE_ROOT, "alpaca", "web", "cockpit.js").as_uri()
    code = ("import(%s).then(m => console.log(JSON.stringify(%s.map(e => m.laneOf(e)))))"
            % (json.dumps(script), json.dumps(events)))
    proc = subprocess.run([node, "--input-type=module", "-e", code], capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_the_absent_collector_note_is_not_a_capture_failure(project):
    """I-5 (t-070 round 2 C-D, `.alpaca/rune2-development/execution/t-070/fix-plan-r2.md` section 6):
    the A-8 note is a state report, recorded as its own kind `capture-no-collector` with the fields
    it had ({where, error}). No reader counts it as a capture failure: the hub's capture errors and
    its failure filter, the session classes (a hook side effect, so a session that only opened,
    got the note and closed stays a probe), the record graph's `failed_with` rule and the cockpit's
    failure lane. The live record already holds such notes as `drain-failed`; every reader keeps
    reading those as it did. The hub shows the newest note as a state line (`capture.states`, t-070
    round 3, I2-4)."""
    from alpaca import cli, hub, sessions_view as sv
    from alpaca.wiki.ingest import drain, record_extract
    cli.main(["init"])
    _flag_on(project)
    conn = db.connect(str(project))
    try:
        for sid in ("p-old", "p-new"):
            db.upsert(conn, "sessions", "sid", {"sid": sid, "started": "2026-10-08T00:00:00+00:00",
                                                "ended": "2026-10-08T00:00:01+00:00", "beats": 0})
            db.append_event(conn, session=sid, actor="agent", kind="session-start")
        old = db.append_event(conn, session="p-old", actor="alpaca", kind="drain-failed",
                              data={"where": "stop", "error": capture_runner.NO_COLLECTOR
                                    + ": the hooks leave wiki capture to the collector"})["id"]
        # a capture failure, the control of the graph reading
        fault = db.append_event(conn, session="p-fault", actor="alpaca", kind="drain-failed",
                                data={"where": "stop", "error": "LedgerLockTimeout: lock held"})["id"]
    finally:
        conn.close()
    assert capture_runner.note_absent_collector(project, "p-new", "session_end") is True
    new = _events(project, "capture-no-collector")
    assert len(new) == 1 and not _events(project, "drain-failed", "p-new")
    new = new[0]
    assert set(new["data"]) == {"where", "error"} and new["data"]["where"] == "session_end"
    assert new["data"]["error"].startswith(capture_runner.NO_COLLECTOR)
    conn = db.connect(str(project))
    try:
        for sid in ("p-old", "p-new"):
            db.append_event(conn, session=sid, actor="agent", kind="session-end")
        # the hub: the capture errors and the failure filter of its feeds
        failing = {r[0] for r in conn.execute("SELECT id FROM events WHERE id IN (?, ?) AND %s"
                                              % hub._FAILURE_SQL, (old, new["id"]))}
        assert failing == {old}
        # the session classes: the note is a side effect of the hook, as the old one was
        view = sv.classify(conn)
        assert view["p-old"]["class"] == sv.PROBE and view["p-new"]["class"] == sv.PROBE
    finally:
        conn.close()
    capture = hub.overview(project)["capture"]
    errors = capture["errors"]
    assert [e["id"] for e in errors if e.get("id") in (old, new["id"])] == [old]
    # t-070 round 3 (check round 2 I2-4): the hub shows the newest note again, as a state line beside
    # the errors, never as one of them
    assert [(s["id"], s["kind"], s["state"]) for s in capture["states"]] == \
        [(new["id"], "capture-no-collector", True)]
    assert capture["states"][0]["recovery"] == "bin/alpaca collect watch"
    # the record graph: `failed_with` reads an error class (`Name: ...`); the note's text starts with
    # none, so neither kind gives an edge, as the old note gave none, while a capture failure does
    rows = {e["id"]: e for e in _events(project) if e["id"] in (old, new["id"], fault)}
    edges = {i: [o.predicate for o in record_extract.extract(drain._note_text(e)[1])]
             for i, e in rows.items()}
    assert edges == {old: [], new["id"]: [], fault: ["failed_with"]}
    # the cockpit's lanes
    assert _laneOf([{"kind": e["kind"], "data": e["data"]} for e in (rows[old], rows[new["id"]])]) \
        == ["failure", "other"]
