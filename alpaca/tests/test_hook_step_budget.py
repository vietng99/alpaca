"""t-070 RC5 part B: every hook step runs under an enforced budget, so every hook returns within its
deadline plus a margin (the rest of RC4).

RC4 (alpaca/capture_runner.py) moved the wiki capture of Stop, PreCompact and SessionEnd into a child
process the runner kills at the hook's deadline, because a call asleep in the kernel holds the hook's
SIGALRM off until it returns (A-4: a `pwrite64` slept 56 s). The other steps of those hooks (the pad,
the transcript snapshot, the analytics build, the SessionEnd consolidation step) still ran in the
hook's own process under SIGALRM alone (`.alpaca/rune2-development/execution/t-070/notes-rc4.md`,
section 5). Each now runs as a step of the same runner (`common.run_step`, alpaca/hook_steps.py).

Each test runs a hook as its own process (`python -m alpaca.hooks.<name>`, as Claude Code runs it)
with one step's function replaced, in every interpreter that starts, by `runner_fakes.hold_step`:
it blocks SIGALRM, SIGTERM and SIGINT for 30 s, as the kernel did. The hook must return within its
deadline plus the margin, keep its output, kill the held child, and leave the same outcome record
whichever way the step ended: the step's own failure event where it has one
(`analytics-build-failed`, `drain-failed` {where}) and the hook outcome `timeout` naming the step.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from alpaca import db

PACKAGE_ROOT = str(Path(db.__file__).resolve().parents[1])
DEADLINE_S = 3.0           # ALPACA_HOOK_DEADLINE_S of the hook processes below
MARGIN_S = 8.0             # interpreter start, the outcome record and a loaded host
HOLD_S = 30.0              # how long the held step would run if nothing stopped it; a hook that
                           # waits it out returns after 30 s, far past every bound below

SITECUSTOMIZE = '''import os
if os.environ.get("ALPACA_TEST_HOLD_STEP"):
    from alpaca.tests import runner_fakes
    runner_fakes.install_step_hold(os.environ["ALPACA_TEST_HOLD_STEP"])
'''


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


def _env(tmp_path, step, deadline_s=DEADLINE_S):
    inject = tmp_path / "inject"
    inject.mkdir(exist_ok=True)
    (inject / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    marks = tmp_path / "hold-pids.txt"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(inject), PACKAGE_ROOT]),
               ALPACA_TEST_HOLD_STEP=step, ALPACA_TEST_HOLD_S=str(HOLD_S),
               ALPACA_TEST_HOLD_MARK=str(marks), ALPACA_HOOK_DEADLINE_S=str(deadline_s),
               PYTHONDONTWRITEBYTECODE="1")
    for name in ("ALPACA_ROOT", "CLAUDE_PROJECT_DIR", "ALPACA_SESSION_ID"):
        env.pop(name, None)
    return env, marks


def _run_hook(project, name, payload, env):
    start = time.monotonic()
    proc = subprocess.run([sys.executable, "-m", "alpaca.hooks." + name], input=json.dumps(payload),
                          capture_output=True, text=True, cwd=str(project), env=env, timeout=120)
    return time.monotonic() - start, proc


def _events(root, kind, session):
    conn = db.connect_readonly(str(root))
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM events WHERE kind=? AND session=? ORDER BY id",
                                              (kind, session))]
    finally:
        conn.close()
    return [json.loads(r["data"]) if r["data"] else None for r in rows]


def _outcomes(root, session):
    conn = db.connect_readonly(str(root))
    try:
        return [dict(r) for r in conn.execute(
            "SELECT hook,outcome,error_type FROM obs_hook_outcome WHERE session=? ORDER BY id", (session,))]
    finally:
        conn.close()


def _gone_or_zombie(pid, within_s=5.0):
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


def _assert_killed(marks):
    pids = [int(p) for p in marks.read_text().split()] if marks.exists() else []
    assert pids, "the held step never started"
    for pid in pids:
        assert _gone_or_zombie(pid), "held step process %d still running" % pid


# (hook, event name, held step, the step's failure event or None)
CASES = [
    ("stop", "Stop", "pad", None),
    ("stop", "Stop", "snapshot", None),
    ("stop", "Stop", "analytics", "analytics-build-failed"),
    ("pre_compact", "PreCompact", "snapshot", None),
    ("pre_compact", "PreCompact", "pad", None),
    ("session_end", "SessionEnd", "pad", None),
    ("session_end", "SessionEnd", "snapshot", None),
    ("session_end", "SessionEnd", "analytics", "analytics-build-failed"),
]


@pytest.mark.parametrize("hook, event, step, failure", CASES)
def test_a_hook_whose_step_holds_off_signals_returns_by_its_deadline_and_records_it(
        project, tmp_path, hook, event, step, failure):
    sid = "s-hold-%s-%s" % (hook, step)
    _open_session(project, sid)
    Path(project, "style", "banned.txt").write_text("delve\n", encoding="utf-8")
    if step == "analytics" and hook == "stop":
        conn = db.connect(str(project))
        try:
            with db.transaction(conn):
                db.meta_set(conn, "stops:%s" % sid, "9")      # this Stop is the tenth: it builds
        finally:
            conn.close()
    env, marks = _env(tmp_path, step)
    payload = {"session_id": sid, "cwd": str(project), "hook_event_name": event, "reason": "other",
               "trigger": "auto", "reply": "We should delve in."}
    seconds, proc = _run_hook(project, hook, payload, env)
    assert proc.returncode == 0
    assert seconds < DEADLINE_S + MARGIN_S, (seconds, proc.stderr[-2000:])
    if hook == "stop":                                     # the decision line is the hook's output
        assert json.loads(proc.stdout)["decision"] == "block"
    else:
        assert proc.stdout == ""
    _assert_killed(marks)
    outcomes = [o for o in _outcomes(project, sid) if o["hook"] == hook]
    assert outcomes and outcomes[-1]["outcome"] == "timeout", outcomes
    assert step in (outcomes[-1]["error_type"] or ""), outcomes
    if failure:
        recorded = _events(project, failure, sid)
        assert recorded and "StepOverrun: %s" % step in recorded[-1]["error"], recorded


def test_the_session_end_consolidation_step_is_stopped_and_recorded(project, tmp_path):
    """The consolidation step (`consolidate_after_capture`) runs only when half the hook's time is
    left, so this hook gets a 10 s deadline; the held step is stopped within it and leaves
    `drain-failed` {where: session_end_consolidate}, the record its own failure leaves."""
    sid = "s-hold-consolidate"
    _open_session(project, sid)
    Path(project, "project.yaml").write_text("wiki_providers:\n  consolidate_after_capture: true\n",
                                             encoding="utf-8")
    deadline = 10.0
    env, marks = _env(tmp_path, "consolidate", deadline_s=deadline)
    payload = {"session_id": sid, "cwd": str(project), "hook_event_name": "SessionEnd", "reason": "other"}
    seconds, proc = _run_hook(project, "session_end", payload, env)
    assert proc.returncode == 0 and proc.stdout == ""
    assert seconds < deadline + MARGIN_S, (seconds, proc.stderr[-2000:])
    _assert_killed(marks)
    failed = [d for d in _events(project, "drain-failed", sid) if d.get("where") == "session_end_consolidate"]
    assert failed and "StepOverrun: consolidate" in failed[-1]["error"], failed
    assert _outcomes(project, sid)[-1]["outcome"] == "timeout"


def test_a_step_that_finishes_leaves_no_fault_and_the_hook_stays_ok(project, tmp_path):
    """Negative: with nothing held, the same hooks run every step in a child, write the pad, and end
    `ok` with no step failure recorded."""
    sid = "s-steps-ok"
    _open_session(project, sid)
    env, _marks = _env(tmp_path, "", deadline_s=8.0)
    for hook, event in (("stop", "Stop"), ("pre_compact", "PreCompact"), ("session_end", "SessionEnd")):
        seconds, proc = _run_hook(project, hook, {"session_id": sid, "cwd": str(project),
                                                  "hook_event_name": event, "reason": "other"}, env)
        assert proc.returncode == 0, proc.stderr[-2000:]
    assert Path(project, "RESUME.md").is_file()
    assert Path(project, "analytics", "index.html").is_file()           # SessionEnd's build
    assert {o["outcome"] for o in _outcomes(project, sid)} == {"ok"}
    assert _events(project, "analytics-build-failed", sid) == []


def test_outside_a_hook_a_step_runs_in_this_process_and_strict_raises(project, monkeypatch):
    """The `session` verbs call the hooks' handlers outside a hook window: no deadline to enforce, so
    a step runs in the caller's process (a test's monkeypatch reaches it) and, under `_strict`, its
    failure is recorded and raised as before."""
    from alpaca import transcripts
    from alpaca.hooks import common

    def broken(*args, **kwargs):
        raise OSError("transcript store unavailable")
    _open_session(project, "s-inline")
    monkeypatch.setattr(transcripts, "snapshot", broken)
    payload = {"session_id": "s-inline", "cwd": str(project)}
    result = common.run_step(payload, "stop", "snapshot", session="s-inline")
    assert result.status == "failed" and "transcript store unavailable" in result.error
    with pytest.raises(OSError):
        common.run_step(dict(payload, _strict=True), "stop", "snapshot", session="s-inline")
