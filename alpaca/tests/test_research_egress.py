"""t-098: the egress proxy of an agent jail and the forwarder inside it (design/research-engine/
architecture.md sections 3.6 and 5.2; FR-019).

Done when: the proxy tunnels a CONNECT to an allowed host:port (through the operator's upstream
proxy when one is set), answers 403 to any other request, and logs every request as one JSON line
with its host, port and whether it was allowed; allowed_hosts gives the runtime's provider hosts,
the base URL's host and the contract's extra hosts; the forwarder listens on loopback, pipes each
connection to the socket, runs its command and exits with the command's code.

Each control has a positive and a negative case. Nothing reaches the network: the allowed host is
an echo server on 127.0.0.1 that the test opens.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import types as pytypes

import pytest

from alpaca.research import egress, jail_forward

HERE = os.path.dirname(os.path.abspath(__file__))
FORWARD = os.path.join(os.path.dirname(HERE), "research", "jail_forward.py")


def _forwarder(tmp_path) -> str:
    """A copy of jail_forward.py alone in a folder, as the launcher puts it in a launch home (run
    from alpaca/research/ its folder would put types.py ahead of the standard library)."""
    folder = tmp_path / "bin"
    folder.mkdir(exist_ok=True)
    target = folder / "jail_forward.py"
    target.write_bytes(open(FORWARD, "rb").read())
    return str(target)


def _echo_server():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)

    def loop():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            data = conn.recv(100)
            conn.sendall(b"echo:" + data)
            conn.close()
    threading.Thread(target=loop, daemon=True).start()
    return server, server.getsockname()[1]


def _ask(sock_path: str, request: bytes, payload: bytes | None = None):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(sock_path)
    s.sendall(request)
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = s.recv(1024)
        if not chunk:
            break
        head += chunk
    body = None
    if payload is not None and b" 200 " in head:
        s.sendall(payload)
        body = s.recv(100)
    s.close()
    return head.split(b"\r\n", 1)[0].decode(), body


@pytest.fixture
def sock_dir(tmp_path):
    folder = tmp_path / "eg"
    folder.mkdir(mode=0o700)
    return str(folder)


def test_allowed_hosts_per_runtime_base_url_and_contract():
    assert egress.allowed_hosts("claude", {}, None) == ("api.anthropic.com:443",)
    assert egress.allowed_hosts("codex", {}, None) == ("api.openai.com:443", "chatgpt.com:443")
    env = {"ANTHROPIC_BASE_URL": "https://gateway.example.org:8443/v1",
           "OPENAI_BASE_URL": "http://other.example.org"}
    contract = pytypes.SimpleNamespace(egress_extra=("pypi.org", "api.anthropic.com"))
    assert egress.allowed_hosts("claude", env, contract) == (
        "api.anthropic.com:443", "gateway.example.org:8443", "pypi.org:443")
    assert egress.allowed_hosts("codex", env, None)[-1] == "other.example.org:443"
    # negative: the control agent reaches no provider, and one runtime's base URL never widens
    # the other's list
    assert egress.allowed_hosts("control", env, None) == ()
    assert "gateway.example.org:8443" not in egress.allowed_hosts("codex", env, None)


def test_the_proxy_tunnels_only_allowed_connects_and_logs_every_request(sock_dir, tmp_path):
    server, port = _echo_server()
    log = str(tmp_path / "out" / "egress.jsonl")
    proxy = egress.start(sock_dir, allow=("127.0.0.1:%d" % port,), log=log)
    try:
        assert os.stat(proxy.path).st_mode & 0o077 == 0
        status, body = _ask(proxy.path, b"CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n" % port, b"hi")
        assert status.split()[1] == "200" and body == b"echo:hi"
        # negative: another host, another port of the same host, and a plain request are refused
        assert _ask(proxy.path, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")[0].split()[1] == "403"
        assert _ask(proxy.path, b"CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n" % (port + 1))[0] \
            .split()[1] == "403"
        assert _ask(proxy.path, b"GET http://127.0.0.1:%d/ HTTP/1.1\r\n\r\n" % port)[0] \
            .split()[1] == "403"
        assert proxy.requests == 4 and proxy.refused == 3
    finally:
        proxy.close()
        server.close()
    lines = [json.loads(x) for x in open(log)]
    assert [(x["method"], x["host"], x["allowed"]) for x in lines] == [
        ("CONNECT", "127.0.0.1", True), ("CONNECT", "example.com", False),
        ("CONNECT", "127.0.0.1", False), ("GET", "127.0.0.1", False)]
    assert all("port" in x and "ts" in x for x in lines)
    assert not os.path.exists(proxy.path)


def test_the_log_stops_at_its_cap_and_counts_what_it_left_out(sock_dir, tmp_path, monkeypatch):
    """Check round 4 of t-098, adversarial m1: a flood of refused requests grew the log with no
    bound. Under a 4 KiB cap, 30 refused CONNECTs with a 2000-character host leave a log under
    the cap; every request is still answered 403 and counted, the lines left out are counted in
    `dropped`, and a logged host holds at most 255 characters. Negative: under a cap the
    requests fit in, every line is written and nothing is dropped."""
    monkeypatch.setattr(egress, "_LOG_MAX", 4096)
    log = str(tmp_path / "capped.jsonl")
    proxy = egress.start(sock_dir, allow=(), log=log)
    host = "h" * 2000 + ".example"
    try:
        for _ in range(30):
            status = _ask(proxy.path, b"CONNECT %s:443 HTTP/1.1\r\n\r\n" % host.encode())[0]
            assert status.split()[1] == "403"
    finally:
        proxy.close()
    lines = [json.loads(x) for x in open(log)]
    assert os.path.getsize(log) <= 4096 and lines
    assert proxy.requests == proxy.refused == 30
    assert proxy.dropped == 30 - len(lines) > 0
    assert all(x["host"] == host[:255] for x in lines)
    monkeypatch.setattr(egress, "_LOG_MAX", 1024 * 1024)
    log2 = str(tmp_path / "whole.jsonl")
    proxy2 = egress.start(sock_dir, allow=(), log=log2)
    try:
        for _ in range(30):
            _ask(proxy2.path, b"CONNECT %s:443 HTTP/1.1\r\n\r\n" % host.encode())
    finally:
        proxy2.close()
    assert len(open(log2).read().splitlines()) == 30 and proxy2.dropped == 0


def test_an_upstream_proxy_carries_the_allowed_tunnel(sock_dir, tmp_path):
    seen = []
    up = socket.socket()
    up.bind(("127.0.0.1", 0))
    up.listen(4)

    def upstream():
        conn, _ = up.accept()
        head = b""
        while b"\r\n\r\n" not in head:
            head += conn.recv(1024)
        seen.append(head.split(b"\r\n", 1)[0].decode())
        conn.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
        data = conn.recv(100)
        conn.sendall(b"via-upstream:" + data)
        conn.close()
    threading.Thread(target=upstream, daemon=True).start()
    proxy = egress.start(sock_dir, allow=("api.anthropic.com:443",),
                         log=str(tmp_path / "e.jsonl"),
                         upstream="http://127.0.0.1:%d" % up.getsockname()[1])
    try:
        status, body = _ask(proxy.path, b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n", b"x")
    finally:
        proxy.close()
        up.close()
    assert status.split()[1] == "200" and body == b"via-upstream:x"
    assert seen == ["CONNECT api.anthropic.com:443 HTTP/1.1"]
    # negative: an upstream the proxy cannot speak to is refused at start
    with pytest.raises(ValueError):
        egress.start(sock_dir, allow=(), log=str(tmp_path / "f.jsonl"),
                     upstream="https://proxy.example.org:3128")


def test_an_unreachable_allowed_host_answers_502_and_is_logged(sock_dir, tmp_path):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()                              # nothing listens there now
    log = str(tmp_path / "e.jsonl")
    proxy = egress.start(sock_dir, allow=("127.0.0.1:%d" % port,), log=log)
    try:
        status, _ = _ask(proxy.path, b"CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n" % port)
    finally:
        proxy.close()
    assert status.split()[1] == "502"
    entry = json.loads(open(log).read().splitlines()[0])
    assert entry["allowed"] is True and entry["error"]


def test_the_forwarder_pipes_loopback_to_the_socket_and_returns_the_code(tmp_path):
    sock_path = str(tmp_path / "s.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(4)

    def loop():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            data = conn.recv(100)
            conn.sendall(b"socket:" + data)
            conn.close()
    threading.Thread(target=loop, daemon=True).start()
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    client = ("import socket, sys\n"
              "s = socket.create_connection(('127.0.0.1', %d), timeout=10)\n"
              "s.sendall(b'hello'); print(s.recv(100).decode()); sys.exit(7)\n" % port)
    done = subprocess.run([sys.executable, _forwarder(tmp_path), str(port), sock_path, "--",
                           sys.executable,
                           "-c", client], capture_output=True, text=True, timeout=60)
    server.close()
    assert done.returncode == 7, done.stderr
    assert done.stdout.strip() == "socket:hello"
    # negative: a malformed command line is a usage error, and runs nothing
    assert jail_forward.main(["8119", sock_path]) == 64
    assert jail_forward.main(["notaport", sock_path, "--", "true"]) == 64
    assert jail_forward.main(["8119", sock_path, "--"]) == 64


def test_a_child_killed_by_a_signal_exits_128_plus_the_signal(tmp_path):
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    done = subprocess.run([sys.executable, _forwarder(tmp_path), str(port),
                           str(tmp_path / "none.sock"), "--",
                           sys.executable, "-c", "import os, signal; os.kill(os.getpid(), 9)"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 128 + 9
    # negative: a normal exit keeps its own code
    ok = subprocess.run([sys.executable, _forwarder(tmp_path), str(port),
                         str(tmp_path / "none.sock"), "--",
                         sys.executable, "-c", "pass"], capture_output=True, timeout=60)
    assert ok.returncode == 0
