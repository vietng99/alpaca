"""The live server keeps its port through reloads and does not refold analytics on every poll."""
import os
import socket

import pytest

from alpaca import serve


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_reload_hands_the_listening_socket_to_the_successor(monkeypatch):
    first, port = serve._bind(_free_port())
    try:
        fd = first.socket.fileno()
        monkeypatch.setenv(serve.LISTEN_FD_ENV, str(fd))
        # a client that connects between the exec and the successor's accept loop is queued,
        # not refused
        client = socket.create_connection(("127.0.0.1", port), timeout=2)
        second, second_port = serve._bind(port)
        assert second_port == port
        assert second.socket.fileno() == fd
        assert serve.LISTEN_FD_ENV not in os.environ
        conn, _ = second.socket.accept()
        conn.close()
        client.close()
    finally:
        first.socket.close()


def test_backlog_is_larger_than_the_stdlib_default():
    assert serve._Server.request_queue_size >= 64


def test_strict_port_does_not_drift(monkeypatch):
    holder, port = serve._bind(_free_port())
    try:
        monkeypatch.setenv("ALPACA_SERVE_STRICT_PORT", "1")
        with pytest.raises(OSError):
            serve._bind(port)
        monkeypatch.delenv("ALPACA_SERVE_STRICT_PORT")
        drifted, other = serve._bind(port)
        assert other != port
        drifted.server_close()
    finally:
        holder.server_close()


def test_watcher_folds_at_most_once_per_interval(project, monkeypatch):
    calls = []
    clock = [1000.0]
    signature = ["a"]
    monkeypatch.setattr(serve.time, "time", lambda: clock[0])
    monkeypatch.setattr(serve, "_data_sig", lambda root: signature[0])
    monkeypatch.setattr(serve.build_index, "collect", lambda root: calls.append(1) or [])
    monkeypatch.setattr(serve.build_index, "_fold", lambda root, collected: {"sessions": [], "n": len(calls)})
    live = serve.Live(project)
    live.refresh(throttle=True)
    assert len(calls) == 1 and live.data_ready.is_set()
    signature[0] = "b"
    clock[0] += 5
    live.refresh(throttle=True)
    assert len(calls) == 1, "a changed input inside FOLD_MIN_S waits for the next window"
    clock[0] += serve.FOLD_MIN_S
    live.refresh(throttle=True)
    assert len(calls) == 2
    signature[0] = "c"
    live.refresh()
    assert len(calls) == 3, "a direct refresh is never throttled"


def test_first_refresh_serves_the_board_before_the_fold(project, monkeypatch):
    monkeypatch.setattr(serve.build_index, "collect", lambda root: pytest.fail("the fast refresh must not fold"))
    live = serve.Live(project)
    live.refresh(fold=False)
    assert live.board_rev
    assert not live.data_ready.is_set()


def test_dashboard_unit_restarts_on_any_exit_on_its_exact_port(project):
    units = serve.service_units(project, port=7350)
    dashboard = next(body for name, body in units.items() if "dashboard" in name)
    assert "Restart=always" in dashboard
    assert "Environment=ALPACA_SERVE_STRICT_PORT=1" in dashboard


def test_a_reload_successor_does_not_exit_as_already_running(project, monkeypatch):
    calls = []
    monkeypatch.setattr(serve.cli, "_root", lambda: project)
    monkeypatch.setattr(serve, "is_running", lambda root: {"url": "http://127.0.0.1:7350/", "port": 7350, "remote": True})
    monkeypatch.setattr(serve, "files_auth", lambda root: ":pin")
    monkeypatch.setattr(serve, "_run", lambda root, port, keep=False, remote=False: calls.append(port))
    args = type("A", (), {"stop": False, "status": False, "write_services": False, "remote": True,
                          "port": 7350, "detach": False, "keep": True})()
    serve.cmd_serve(args)
    assert calls == [], "without a handed-over socket a live server means already running"
    monkeypatch.setenv(serve.LISTEN_FD_ENV, "99")
    serve.cmd_serve(args)
    assert calls == [7350]
