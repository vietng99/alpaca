"""The live server keeps its port through reloads and does not refold analytics on every poll."""
import os
import socket
import json
import threading
import urllib.request

import pytest

from alpaca import serve


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize('path', ['/hub/overview.json', '/hub/research.json'])
def test_usage_index_pauses_between_sessions_for_page_request(project, monkeypatch, path):
    from alpaca import db, hub
    from alpaca.analytics import metrics_index
    from alpaca.research import view
    for sid in ('first', 'second'):
        conn = db.connect(project)
        db.upsert(conn, 'sessions', 'sid', {'sid': sid, 'started': '2026-09-22T00:00:00Z'})
        conn.close()
    live = serve.Live(project)
    live.data_rev = 'fixture'
    live.data_json = json.dumps({'sessions': [{'sid': sid, 'class': 'work'}
                                            for sid in ('first', 'second')]})
    first_done, next_started = threading.Event(), threading.Event()
    resume_index, page_started, release_page = threading.Event(), threading.Event(), threading.Event()
    original_analysis = metrics_index._analysis_row

    def analysis(root, sid):
        if sid == 'second':
            next_started.set()
        result = original_analysis(root, sid)
        if sid == 'first':
            first_done.set()
            assert resume_index.wait(5)
        return result

    monkeypatch.setattr(metrics_index, '_analysis_row', analysis)
    module, name = (hub, 'overview') if path.endswith('overview.json') else (view, 'runs')
    original_page = getattr(module, name)

    def page(root):
        page_started.set()
        assert release_page.wait(5)
        return original_page(root)

    monkeypatch.setattr(module, name, page)
    httpd = serve._Server(('127.0.0.1', 0), serve.make_handler(live, project))
    httpd.daemon_threads = True
    server = threading.Thread(target=httpd.serve_forever, daemon=True)
    server.start()
    results, failures = {}, []

    def index_job():
        try:
            results['index'] = live.analytics_index()
        except Exception as exc:
            failures.append(exc)

    def page_job():
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d%s' % (httpd.server_port, path), timeout=5) as response:
                results['page'] = response.status
                response.read()
        except Exception as exc:
            failures.append(exc)

    index_thread = threading.Thread(target=index_job, daemon=True)
    page_thread = threading.Thread(target=page_job, daemon=True)
    try:
        index_thread.start()
        assert first_done.wait(5)
        page_thread.start()
        assert page_started.wait(5)
        resume_index.set()
        assert not next_started.wait(.2), 'usage rebuild competes with the active page request'
    finally:
        resume_index.set()
        release_page.set()
        index_thread.join(5)
        if page_thread.ident is not None:
            page_thread.join(5)
        httpd.shutdown()
        httpd.server_close()
    assert not failures
    assert results['page'] == 200
    assert next_started.is_set()
    assert len(json.loads(results['index'][0])['sessions']) == 2


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


def test_failed_foreground_read_releases_analytics_priority(project):
    live = serve.Live(project)
    with pytest.raises(ValueError):
        with live.foreground_read('/hub/overview.json'):
            raise ValueError('failed page read')
    assert live._foreground_reads == 0, 'failed page read left usage analytics paused'
    resumed = threading.Event()
    worker = threading.Thread(target=lambda: (live.yield_analytics(), resumed.set()), daemon=True)
    worker.start()
    # a paused analytics waits out yield_analytics' 2 s bound; a free one returns at once
    assert resumed.wait(1.5), 'failed page read left usage analytics paused'
    worker.join(1)


def test_stalled_foreground_read_cannot_pause_analytics_indefinitely(project):
    live = serve.Live(project)
    resumed = threading.Event()
    with live.foreground_read('/hub/research.json'):
        worker = threading.Thread(target=lambda: (live.yield_analytics(), resumed.set()), daemon=True)
        worker.start()
        assert resumed.wait(4), 'a stuck page reader permanently stopped usage analytics'
    worker.join(1)


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
