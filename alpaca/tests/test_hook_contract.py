"""M1.5 proof: the hook failure and cost contract.

Every one of the seven hook entry points is fail-open: it exits 0 within a bounded
wall time on a payload that is empty, malformed, oversized, or arrives on a stdin
that never closes, and the capture-only hooks (PreToolUse, PostToolUse, SubagentStop,
SessionEnd) print nothing on any of those paths. PreToolUse is the strictest of them:
a line on its stdout can block a tool call, so it must stay silent on every path.
The plumbing that guarantees this lives in
alpaca/hooks/common.py: common.read_stdin(timeout_s) is non-blocking and
common.fail_open(fn) carries a hard deadline. alpaca doctor checks that every hook
event in .claude/settings.json declares a timeout at or below the ceiling.

Positive and negative paths are both asserted: a well-formed payload still exits 0
and the capture hooks stay silent (positive), and the four hostile payloads plus a
wedged stdin exit 0 too (negative).
"""
import inspect
import json
import os
import subprocess
import sys
import time

import pytest

from alpaca import doctor
from alpaca.tests.conftest import REPO

# Every hook module by its import path, and which of them must never write stdout.
HOOKS = [
    "alpaca.hooks.session_start",
    "alpaca.hooks.user_prompt",
    "alpaca.hooks.pre_tool",
    "alpaca.hooks.post_tool",
    "alpaca.hooks.stop",
    "alpaca.hooks.subagent_stop",
    "alpaca.hooks.session_end",
]
CAPTURE_ONLY = {"alpaca.hooks.pre_tool", "alpaca.hooks.post_tool",
                "alpaca.hooks.subagent_stop", "alpaca.hooks.session_end"}

# Four hostile payloads. Each must still end in exit 0.
HOSTILE = {
    "empty": "",
    "whitespace": "   \n  ",
    "malformed": "not json at all {[}]",
    "oversized": '{"session_id":"s1","junk":"' + ("a" * (5 * 1024 * 1024)),
}

# A generous ceiling: no single hook invocation may take longer than this on any
# of the tested paths. The stdin guard and the hard deadline both keep it well under.
WALL_CEILING_S = 20.0


def _env(project):
    return {**os.environ,
            "CLAUDE_PROJECT_DIR": project,
            "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.dirname(__file__)))}


def _run(module, payload_text, project, timeout=WALL_CEILING_S):
    """Run one hook to completion, feeding payload_text on a stdin that then closes."""
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, "-m", module], input=payload_text,
                       capture_output=True, text=True, encoding="utf-8",
                       cwd=project, env=_env(project), timeout=timeout)
    return p, time.monotonic() - t0


# ---------------------------------------------------------------- interface shape
def test_read_stdin_takes_a_timeout():
    sig = inspect.signature(__import__("alpaca.hooks.common", fromlist=["common"]).read_stdin)
    assert "timeout_s" in sig.parameters


def test_fail_open_swallows_exception_and_exits_zero():
    from alpaca.hooks import common

    @common.fail_open
    def boom():
        raise RuntimeError("handler blew up")

    with pytest.raises(SystemExit) as ei:
        boom()
    assert ei.value.code == 0


def test_fail_open_returns_zero_on_clean_return():
    from alpaca.hooks import common

    @common.fail_open
    def fine():
        return "ignored"

    with pytest.raises(SystemExit) as ei:
        fine()
    assert ei.value.code == 0


def test_fail_open_skips_the_cost_record_when_asked(monkeypatch):
    """The duration record is the only thing on the wrapper's path that opens the record."""
    from alpaca.hooks import common
    seen = []
    monkeypatch.setattr(common, "_record_duration", lambda fn, start: seen.append(fn.__name__))

    @common.fail_open(record_cost=False)
    def quiet():
        return None

    @common.fail_open
    def loud():
        return None

    for fn in (quiet, loud):
        with pytest.raises(SystemExit) as ei:
            fn()
        assert ei.value.code == 0
    assert seen == ["loud"]


def test_fail_open_has_a_hard_deadline():
    """A handler that would run forever is cut off and still exits 0, fast."""
    from alpaca.hooks import common

    @common.fail_open(deadline_s=1)
    def wedged():
        time.sleep(60)

    t0 = time.monotonic()
    with pytest.raises(SystemExit) as ei:
        wedged()
    elapsed = time.monotonic() - t0
    assert ei.value.code == 0
    assert elapsed < 10, "hard deadline did not cut off a wedged handler (%.1fs)" % elapsed


# ------------------------------------------------------------ negative: hostile input
@pytest.mark.parametrize("module", HOOKS)
@pytest.mark.parametrize("kind", list(HOSTILE))
def test_hook_exits_zero_on_hostile_payload(project, module, kind):
    from alpaca import cli
    cli.main(["init"])
    p, elapsed = _run(module, HOSTILE[kind], project)
    assert p.returncode == 0, "%s on %s payload: rc=%s stderr=%s" % (module, kind, p.returncode, p.stderr)
    assert elapsed < WALL_CEILING_S, "%s on %s took %.1fs" % (module, kind, elapsed)
    if module in CAPTURE_ONLY:
        assert p.stdout == "", "%s must stay silent on %s, got %r" % (module, kind, p.stdout[:200])


# ------------------------------------------------------------- positive: clean input
@pytest.mark.parametrize("module", HOOKS)
def test_hook_exits_zero_on_clean_payload(project, module):
    from alpaca import cli
    cli.main(["init"])
    p, elapsed = _run(module, json.dumps({"session_id": "s1", "cwd": project}), project)
    assert p.returncode == 0, p.stderr
    assert elapsed < WALL_CEILING_S
    if module in CAPTURE_ONLY:
        assert p.stdout == ""


# ----------------------------------------------------------- negative: stdin never closes
@pytest.mark.parametrize("module", HOOKS)
def test_hook_exits_zero_when_stdin_never_closes(project, module):
    """A caller that opens the pipe, sends a partial fragment and never closes it
    cannot wedge the hook: the stdin guard returns and the hook exits 0 in bounded time."""
    from alpaca import cli
    cli.main(["init"])
    proc = subprocess.Popen([sys.executable, "-m", module],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", cwd=project, env=_env(project))
    try:
        proc.stdin.write('{"session_id": "s1"')   # partial JSON, no newline, never closed
        proc.stdin.flush()
    except BrokenPipeError:
        pass
    t0 = time.monotonic()
    try:
        out, err = proc.communicate(timeout=WALL_CEILING_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise AssertionError("%s wedged on a never-closing stdin" % module)
    elapsed = time.monotonic() - t0
    assert proc.returncode == 0, "%s: rc=%s err=%s" % (module, proc.returncode, err)
    assert elapsed < WALL_CEILING_S
    if module in CAPTURE_ONLY:
        assert out == "", "%s must stay silent, got %r" % (module, out[:200])


# --------------------------------------------------------------- what a hook costs to run
def _hook_cost_keys(project):
    from alpaca import db
    conn = db.connect(project)
    try:
        return {r["key"] for r in conn.execute("SELECT key FROM meta WHERE key LIKE 'hook-cost:%'")}
    finally:
        conn.close()


def test_pre_tool_use_records_no_hook_cost_row(project):
    """PreToolUse runs before EVERY tool call, so its cost is paid over and over in one turn.

    It stays off the record completely: the duration row was the one thing on its path that opened
    sqlite, and what is left is a process start and one pool append.
    """
    from alpaca import cli
    cli.main(["init"])
    base = {"session_id": "s1", "cwd": project, "tool_name": "Read",
            "tool_input": {"file_path": "/a"}}
    p, _ = _run("alpaca.hooks.pre_tool", json.dumps(base), project)
    assert p.returncode == 0 and p.stdout == ""
    assert _hook_cost_keys(project) == set()
    # positive: every other hook still records what it cost, so the analytics keep the picture.
    p, _ = _run("alpaca.hooks.post_tool", json.dumps(dict(base, tool_response="ok")), project)
    assert p.returncode == 0
    assert {k for k in _hook_cost_keys(project) if k.startswith("hook-cost:s1:")}


def _modules_imported(module, payload_text, project):
    """Every module one hook invocation loads, read off the interpreter's own import log."""
    p = subprocess.run([sys.executable, "-X", "importtime", "-m", module], input=payload_text,
                       capture_output=True, text=True, encoding="utf-8",
                       cwd=project, env=_env(project), timeout=WALL_CEILING_S)
    assert p.returncode == 0
    assert p.stdout == "", "%s must stay silent, got %r" % (module, p.stdout[:200])
    return {ln.rsplit("|", 1)[-1].strip() for ln in p.stderr.splitlines() if "|" in ln}


def test_pre_tool_use_never_loads_the_record(project):
    """sqlite is what this hook is measured against: a real invocation may not load it at all."""
    from alpaca import cli
    cli.main(["init"])
    payload = json.dumps({"session_id": "s1", "cwd": project, "tool_name": "Read",
                          "tool_input": {"file_path": "/a"}})
    loaded = _modules_imported("alpaca.hooks.pre_tool", payload, project)
    assert "alpaca.pool" in loaded, "the hook did not get as far as its pool append"
    assert "sqlite3" not in loaded and "alpaca.db" not in loaded
    # negative: a hook that does reach the record loads both, so the probe can tell them apart.
    loaded = _modules_imported("alpaca.hooks.post_tool",
                               json.dumps(json.loads(payload) | {"tool_response": "ok"}), project)
    assert "sqlite3" in loaded and "alpaca.db" in loaded


# ------------------------------------------------------------------ doctor timeout gate
def _write_settings(project, settings):
    d = os.path.join(project, ".claude")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "settings.json"), "w", encoding="utf-8") as fh:
        json.dump(settings, fh)


def _real_settings():
    with open(os.path.join(REPO, ".claude", "settings.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _named(res, name):
    for c in res:
        if c["name"] == name:
            return c
    return None


def test_real_settings_register_the_capture_hooks():
    """The three capture events are wired to their modules, inside the timeout ceiling."""
    s = _real_settings()["hooks"]
    for event, module in (("PreToolUse", "pre_tool"), ("PostToolUse", "post_tool"),
                          ("SubagentStop", "subagent_stop")):
        entries = [h for grp in s.get(event, []) for h in grp.get("hooks", [])]
        assert entries, "%s registers no hook" % event
        assert any("alpaca.hooks.%s" % module in h.get("command", "") for h in entries), event
        assert all(0 < h["timeout"] <= doctor.HOOK_TIMEOUT_CEILING for h in entries), event
    # negative: an event nothing is wired to is absent, not registered empty.
    assert "Notification" not in s


def test_doctor_passes_hook_timeouts_on_real_settings(project):
    _write_settings(project, _real_settings())
    c = _named(doctor.checks(project), "hook-timeouts")
    assert c is not None, "doctor grew no hook-timeouts check"
    assert c["level"] == "ok", c


def test_doctor_flags_timeout_over_ceiling(project):
    s = _real_settings()
    s["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] = 99999
    _write_settings(project, s)
    c = _named(doctor.checks(project), "hook-timeouts")
    assert c is not None and c["level"] == "error", c
    assert "SessionEnd" in c["detail"]


def test_doctor_flags_missing_timeout(project):
    s = _real_settings()
    del s["hooks"]["PostToolUse"][0]["hooks"][0]["timeout"]
    _write_settings(project, s)
    c = _named(doctor.checks(project), "hook-timeouts")
    assert c is not None and c["level"] == "error", c
    assert "PostToolUse" in c["detail"]
