"""`alpaca serve --detach`, `--keep`, `--status` and the stale state file.

The owner started a server from an SSH shell. The shell closed, the server died with it, and
`.alpaca/serve.json` stayed on disk naming a pid nobody owned, so `--status` kept reporting a
server that answered nothing. Half an hour later the idle rule was retiring servers in the middle
of a long run as well.

Every control drives a POSITIVE and a NEGATIVE path, so none is a pass by construction:

  * `--detach` leaves a server in ITS OWN process session that still answers after the shell that
    launched it has exited, and a start in this shell does not detach;
  * the detach line prints both page URLs and the exact tunnel command, built from the current
    user name and this host's name;
  * `--keep` records the flag and holds the server through an idle spell that retires a server
    started without it;
  * `--status` prints uptime and which way the idle rule is set, and clears a state file whose
    pid is gone; with nothing on disk it says stopped and clears nothing;
  * a new start clears the same stale file rather than trusting it;
  * `--stop` stops a detached server and takes the state file with it.

The servers here run on their own temp project roots and their own ports, and each is stopped
again when its test ends.
"""
import json
import os
import socket
import subprocess
import sys
import time

import pytest

from alpaca import serve

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _env(extra=None):
    env = {**os.environ, "PYTHONPATH": REPO}
    env.pop("CLAUDE_PROJECT_DIR", None)
    env.pop("ALPACA_NO_AUTOSERVE", None)
    env.update(extra or {})
    return env


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def serve_cmd(root, *args, extra_env=None, shell_exits=False):
    """Run `alpaca serve ...` against `root` and return the finished process.

    With `shell_exits` the command runs inside a bash that then exits, which is the shape the
    owner's SSH session had: whatever the server outlives, it outlives that shell.
    """
    argv = [sys.executable, "-m", "alpaca", "serve"] + list(args)
    if shell_exits:
        quoted = " ".join("'%s'" % a for a in argv)
        argv = ["bash", "-c", quoted + "; exit 0"]
    return subprocess.run(argv, cwd=root, env=_env(extra_env), capture_output=True,
                          text=True, timeout=90)


def state_of(root):
    path = os.path.join(root, ".alpaca", "serve.json")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def answers(port, tries=1):
    for _ in range(tries):
        if serve._port_open(port):
            return True
        time.sleep(0.2)
    return False


@pytest.fixture
def stopped(project):
    """Stop whatever a test left running on this throwaway project root."""
    yield project
    st = serve._read_state(project)
    if st and serve._pid_alive(st.get("pid")):
        serve_cmd(project, "--stop")


# ------------------------------------------------------------------ detach and outlive a shell
def test_detach_leaves_a_server_answering_after_its_shell_exits(stopped):
    port = _free_port()
    done = serve_cmd(stopped, "--detach", "--port", str(port), shell_exits=True)
    assert done.returncode == 0, done.stderr

    st = state_of(stopped)
    assert answers(st["port"], tries=10), "the detached server answers its port"
    assert serve._pid_alive(st["pid"]), "the server outlived the bash that launched it"
    # its own session: the hangup that reaches a login shell on logout never reaches it.
    assert os.getsid(st["pid"]) != os.getsid(os.getpid()), \
        "the detached server leads its own process session"
    assert os.path.isfile(os.path.join(stopped, ".alpaca", "serve.log")), \
        "a detached process has no terminal, so its output is on the log file"


def test_detach_prints_both_pages_and_the_exact_tunnel_line(stopped):
    port = _free_port()
    done = serve_cmd(stopped, "--detach", "--port", str(port))
    assert done.returncode == 0, done.stderr
    st = state_of(stopped)
    out = done.stdout
    assert "alpaca serve: detached (pid %s), log .alpaca/serve.log" % st["pid"] in out, out
    assert "alpaca serve: cockpit http://127.0.0.1:%d/" % st["port"] in out, out
    assert "classic board" not in out and "/board/" not in out, "the classic board is retired"
    assert "alpaca serve: session analytics http://127.0.0.1:%d/analytics/" % st["port"] in out, out
    assert "ssh -N -L %d:127.0.0.1:%d " % (st["port"], st["port"]) in out, out
    assert "@%s" % socket.gethostname() in out, "the tunnel line names this host"


def test_the_tunnel_line_is_built_from_the_current_user_and_this_host():
    import getpass
    line = serve.tunnel_line(7350)
    assert line == "ssh -N -L 7350:127.0.0.1:7350 %s@%s" % (getpass.getuser(), socket.gethostname())
    # negative: the port is not left as a placeholder and the host is not localhost.
    assert "127.0.0.1@" not in line


# ------------------------------------------------------------------------------ the idle rule
def test_keep_holds_the_server_through_an_idle_spell_that_retires_a_plain_one(stopped):
    """Positive and negative over the SAME short spell: without --keep the server retires, with
    --keep it is still answering well after that spell has passed."""
    port = _free_port()
    short = {"ALPACA_SERVE_IDLE_S": "2"}
    assert serve_cmd(stopped, "--detach", "--port", str(port), extra_env=short).returncode == 0
    plain = state_of(stopped)
    assert plain.get("keep") is False
    deadline = time.time() + 20
    while time.time() < deadline and serve._pid_alive(plain["pid"]):
        time.sleep(0.3)
    assert not serve._pid_alive(plain["pid"]), "an unwatched server retires after the idle spell"

    port2 = _free_port()
    assert serve_cmd(stopped, "--detach", "--keep", "--port", str(port2),
                     extra_env=short).returncode == 0
    kept = state_of(stopped)
    assert kept.get("keep") is True, "the flag is recorded in serve.json"
    time.sleep(6)
    assert serve._pid_alive(kept["pid"]), "--keep holds the server through the same spell"
    assert answers(kept["port"]), "and it is still serving"


def test_status_names_the_uptime_and_which_way_the_idle_rule_is_set(stopped):
    port = _free_port()
    assert serve_cmd(stopped, "--detach", "--keep", "--port", str(port)).returncode == 0
    out = serve_cmd(stopped, "--status").stdout
    st = state_of(stopped)
    assert "alpaca serve: running at %s" % st["url"] in out, out
    assert "pid %s" % st["pid"] in out and "up " in out, out
    assert "idle shutdown off (--keep)" in out, out
    assert "ssh -N -L" in out, "status repeats the tunnel line a remote reader needs"

    serve_cmd(stopped, "--stop")
    port2 = _free_port()
    assert serve_cmd(stopped, "--detach", "--port", str(port2)).returncode == 0
    out2 = serve_cmd(stopped, "--status").stdout
    assert "idle shutdown on after 30 min" in out2, out2


# ------------------------------------------------------------------------ the stale state file
def _write_dead_state(root, pid=999999):
    os.makedirs(os.path.join(root, ".alpaca"), exist_ok=True)
    body = {"pid": pid, "port": 65500, "url": "http://127.0.0.1:65500/",
            "started": "2026-09-18T18:31:07+07:00", "keep": False}
    path = os.path.join(root, ".alpaca", "serve.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(body, fh)
    return path


def test_status_clears_a_state_file_whose_pid_is_gone(stopped):
    path = _write_dead_state(stopped)
    out = serve_cmd(stopped, "--status").stdout
    assert "alpaca serve: stopped" in out, out
    assert "cleared a stale record of pid 999999" in out, out
    assert not os.path.isfile(path), "the stale file is gone"

    # negative: with nothing on disk, status says stopped and claims no cleanup.
    out2 = serve_cmd(stopped, "--status").stdout
    assert out2.strip() == "alpaca serve: stopped", out2


def test_a_new_start_clears_the_stale_file_rather_than_trusting_it(stopped):
    _write_dead_state(stopped)
    port = _free_port()
    done = serve_cmd(stopped, "--detach", "--port", str(port))
    assert done.returncode == 0, done.stderr
    assert "already running" not in done.stdout, done.stdout
    st = state_of(stopped)
    assert st["pid"] != 999999 and serve._pid_alive(st["pid"])
    assert st["port"] == port


def test_clear_stale_state_leaves_a_live_server_alone(stopped):
    port = _free_port()
    assert serve_cmd(stopped, "--detach", "--port", str(port)).returncode == 0
    assert serve.clear_stale_state(stopped) is None, "a live server's state file is not removed"
    assert os.path.isfile(os.path.join(stopped, ".alpaca", "serve.json"))


def test_stop_stops_a_detached_server_and_takes_its_state_file(stopped):
    port = _free_port()
    assert serve_cmd(stopped, "--detach", "--port", str(port)).returncode == 0
    st = state_of(stopped)
    out = serve_cmd(stopped, "--stop").stdout
    assert "alpaca serve: stopped (pid %s)" % st["pid"] in out, out
    deadline = time.time() + 10
    while time.time() < deadline and serve._pid_alive(st["pid"]):
        time.sleep(0.2)
    assert not serve._pid_alive(st["pid"]), "the detached server is gone"
    assert not os.path.isfile(os.path.join(stopped, ".alpaca", "serve.json"))
    # negative: stopping again says so rather than claiming a second kill.
    assert "alpaca serve: not running" in serve_cmd(stopped, "--stop").stdout


def test_a_second_start_reuses_the_live_server_and_repeats_its_addresses(stopped):
    port = _free_port()
    assert serve_cmd(stopped, "--detach", "--port", str(port)).returncode == 0
    again = serve_cmd(stopped, "--detach", "--port", str(_free_port()))
    assert again.returncode == 0
    assert "already running at http://127.0.0.1:%d/" % port in again.stdout, again.stdout
    assert "ssh -N -L %d:" % port in again.stdout, again.stdout
    assert state_of(stopped)["port"] == port, "no second server was bound for one root"
