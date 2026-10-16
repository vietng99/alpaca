"""A Ctrl-C during `alpaca new --attach` (op-062 t-681, S8 of the t-675 report).

`new` used to kill the `attach init` child 0.25 s after a Ctrl-C, which could cut the child's rollback short and leave
the clone, the attach block or the exclude lines behind. `new._run` now forwards the interrupt and waits
`INTERRUPT_GRACE` for the child before it kills it, and never leaves it running.
"""
import os
import signal
import stat
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
import yaml

from alpaca import new as new_mod
from alpaca.attach import STATE_DIR
from alpaca.tests.attachkit import make_upstream
from alpaca.tests.conftest import REPO

# Runs inside the `attach init` child (through PYTHONPATH of the wrapper below). When the child makes the app/ folder
# (the claim, a held region of attach) it writes the marker the test waits for; the first delete after that sleeps, so
# the rollback of the Ctrl-C the test sends takes about DELAY seconds, longer than the 0.25 s the old code allowed.
SITECUSTOMIZE = textwrap.dedent('''
    import os, sys, time
    _MARK, _DELAY = os.environ.get("T681_MARK"), float(os.environ.get("T681_DELAY", "2"))
    _state = {"armed": False, "slept": False, "busy": False}

    def _audit(event, args):
        if _state["busy"] or not _MARK:
            return
        if event == "os.mkdir" and not _state["armed"] and str(args[0]).rstrip("/").endswith("/app"):
            _state["busy"] = True
            try:
                open(_MARK, "w").write("claimed")
                _state["armed"] = True
            finally:
                _state["busy"] = False
        elif _state["armed"] and not _state["slept"] and event in ("os.remove", "os.rmdir", "shutil.rmtree"):
            _state["slept"] = True
            time.sleep(_DELAY)

    sys.addaudithook(_audit)
''')

LAUNCH = ("import sys, runpy\nsys.executable = sys.argv[1]\nsys.argv = ['alpaca'] + sys.argv[2:]\n"
          "runpy.run_module('alpaca', run_name='__main__', alter_sys=True)\n")


def _exclude(root):
    p = Path(root) / ".git" / "info" / "exclude"
    return p.read_text() if p.exists() else ""


def _start_new_attach(tmp_path, upstream):
    """`alpaca new --attach` in its own process group on a pseudo-terminal (the owner's terminal), with the slowed rollback.
    The interpreter `new` runs its children with is a wrapper that adds the slow-down to them."""
    inject = tmp_path / "inject"
    inject.mkdir()
    (inject / "sitecustomize.py").write_text(SITECUSTOMIZE)
    wrapper = tmp_path / "py-wrapper"
    wrapper.write_text("#!/bin/sh\nPYTHONPATH=%s exec %s \"$@\"\n" % (inject, sys.executable))
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    mark = tmp_path / "claimed.mark"
    env = {k: v for k, v in os.environ.items()
           if k not in ("ALPACA_ROOT", "CLAUDE_PROJECT_DIR", "ALPACA_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDECODE",
                        "CODEX_THREAD_ID", "CODEX_SANDBOX")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", PYTHONPATH=REPO,
               ALPACA_WORKSPACES=str(tmp_path / "ws.json"), ALPACA_RECORD_SKIP_ROOT=REPO,
               GIT_AUTHOR_NAME="a", GIT_AUTHOR_EMAIL="a@x", GIT_COMMITTER_NAME="a", GIT_COMMITTER_EMAIL="a@x",
               T681_MARK=str(mark), T681_DELAY="2")
    master, slave = os.openpty()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", LAUNCH, str(wrapper), "new", "--name", "studio-x", "--dir", str(tmp_path / "sx"),
             "--yes", "--no-bootstrap", "--no-serve", "--who", "ada:owner", "--what", "w", "--attach", upstream.url],
            cwd=str(tmp_path), env=env, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)               # its own group: a terminal's Ctrl-C reaches `new` and its child alike
    finally:
        os.close(slave)
    return proc, mark, master


def test_a_ctrl_c_during_new_attach_leaves_the_studio_without_attach_state(tmp_path):
    up = make_upstream(tmp_path, "up")
    proc, mark, master = _start_new_attach(tmp_path, up)
    try:
        deadline = time.monotonic() + 120
        while not mark.exists():
            assert proc.poll() is None and time.monotonic() < deadline, "attach never reached the claim: " + (proc.stdout.read() if proc.poll() is not None else "")
            time.sleep(0.02)
        os.killpg(proc.pid, signal.SIGINT)         # the Ctrl-C: attach is inside its held claim
        out, _ = proc.communicate(timeout=120)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        os.close(master)
    sx = tmp_path / "sx"
    assert proc.returncode != 0 and "alpaca new: ok" not in out, out[-600:]
    assert "attach" not in (yaml.safe_load((sx / "project.yaml").read_text()) or {}), "the attach block was left"
    assert not (sx / "app").exists(), "the clone folder was left: %s" % sorted(os.listdir(sx))
    assert not (sx / STATE_DIR).exists(), "attach's state folder was left"
    exclude = _exclude(sx)
    assert "alpaca attach" not in exclude and "/app/" not in exclude and ".alpaca/attach" not in exclude, exclude


def _child(tmp_path, body):
    """argv of a python child whose body runs after it wrote its pid to tmp_path/pid."""
    code = "import os, signal, time\nopen(%r, 'w').write(str(os.getpid()))\n%s" % (str(tmp_path / "pid"), body)
    return [sys.executable, "-c", code]


def _interrupt_this_process_when(path):
    """A Ctrl-C to this process alone (not to the child) once `path` exists, as `kill -INT` would send it."""
    def go():
        for _ in range(500):
            if path.exists():
                break
            time.sleep(0.01)
        os.kill(os.getpid(), signal.SIGINT)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_run_forwards_the_ctrl_c_and_waits_for_a_child_that_needs_more_than_the_old_quarter_second(tmp_path):
    ready, done = tmp_path / "ready", tmp_path / "done"
    argv = _child(tmp_path, "def h(*a):\n    time.sleep(1.2)\n    open(%r, 'w').write('undone')\n    raise SystemExit(3)\n"
                            "signal.signal(signal.SIGINT, h)\nopen(%r, 'w').write('1')\ntime.sleep(60)\n"
                  % (str(done), str(ready)))
    t = _interrupt_this_process_when(ready)
    with pytest.raises(KeyboardInterrupt):
        new_mod._run(argv, str(tmp_path), dict(os.environ), "child")
    t.join()
    assert done.read_text() == "undone", "the child was killed before it finished its undo"
    assert not _alive(int((tmp_path / "pid").read_text()))


def test_run_kills_a_child_that_ignores_the_ctrl_c_once_the_grace_is_over(tmp_path, monkeypatch):
    monkeypatch.setattr(new_mod, "INTERRUPT_GRACE", 0.5)
    ready = tmp_path / "ready"
    argv = _child(tmp_path, "signal.signal(signal.SIGINT, signal.SIG_IGN)\nopen(%r, 'w').write('1')\ntime.sleep(60)\n"
                  % str(ready))
    t = _interrupt_this_process_when(ready)
    start = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        new_mod._run(argv, str(tmp_path), dict(os.environ), "child")
    t.join()
    assert 0.4 < time.monotonic() - start < 10
    assert not _alive(int((tmp_path / "pid").read_text())), "the child is still running"


def test_the_grace_is_a_bounded_number_of_seconds_well_above_the_old_quarter_second():
    assert 5 <= new_mod.INTERRUPT_GRACE <= 300
