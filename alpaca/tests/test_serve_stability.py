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
    assert live.data_ready.wait(10) and len(calls) == 1      # the fold runs off the watcher thread
    signature[0] = "b"
    clock[0] += 5
    live.refresh(throttle=True)
    assert len(calls) == 1, "a changed input inside FOLD_MIN_S waits for the next window"
    clock[0] += serve.FOLD_MIN_S
    live.refresh(throttle=True)
    live._fold_thread.join(10)      # data_ready is already set by the first fold, so it cannot signal this one
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


# ---- the In-flight snapshot is served stale-while-revalidate (t-574) ----

def _fake_snapshots(monkeypatch, gate=None, fail=False):
    from alpaca.analytics import inflight
    calls = []

    def snapshot(root, *a, **k):
        calls.append(1)
        if gate is not None:
            assert gate.wait(10)
        if fail and len(calls) > 1:
            raise RuntimeError("read failed")
        return {"n": len(calls)}
    monkeypatch.setattr(inflight, "snapshot", snapshot)
    return calls


def _wait_idle(live):
    for _ in range(200):
        with live._inflight_lock:
            if not live._inflight[2]:
                return
        threading.Event().wait(0.02)
    raise AssertionError("the background rebuild never finished")


def test_inflight_first_read_builds_and_a_fresh_one_is_reused(project, monkeypatch):
    calls = _fake_snapshots(monkeypatch)
    live = serve.Live(project)
    assert live.inflight() == {"n": 1} and live.inflight() == {"n": 1}
    assert len(calls) == 1


def test_inflight_stale_snapshot_is_served_at_once_and_one_rebuild_starts(project, monkeypatch):
    live = serve.Live(project)
    _fake_snapshots(monkeypatch)
    live.inflight()
    gate = threading.Event()
    calls = _fake_snapshots(monkeypatch, gate=gate)
    live._inflight[0] -= serve.INFLIGHT_S + 1
    got = []
    readers = [threading.Thread(target=lambda: got.append(live.inflight())) for _ in range(12)]
    for t in readers:
        t.start()
    for t in readers:
        t.join(2)
    assert got == [{"n": 1}] * 12                 # nobody waited for the blocked rebuild
    threading.Event().wait(0.2)
    assert len(calls) == 1                        # twelve readers, one rebuild
    gate.set()
    _wait_idle(live)
    assert live.inflight() == {"n": 1} and len(calls) == 1


def test_inflight_past_the_age_cap_a_reader_waits_for_a_build(project, monkeypatch):
    live = serve.Live(project)
    _fake_snapshots(monkeypatch)
    live.inflight()
    calls = _fake_snapshots(monkeypatch)
    live._inflight[0] -= serve.INFLIGHT_MAX_S + 1
    assert live.inflight() == {"n": 1} and len(calls) == 1      # the new build, not the old snapshot
    assert live.inflight() == {"n": 1}


def test_inflight_failed_rebuild_keeps_the_old_snapshot_and_backs_off(project, monkeypatch):
    live = serve.Live(project)
    calls = _fake_snapshots(monkeypatch, fail=True)
    assert live.inflight() == {"n": 1}
    live._inflight[0] -= serve.INFLIGHT_S + 1
    assert live.inflight() == {"n": 1}
    _wait_idle(live)
    assert len(calls) == 2 and live._inflight[1] == {"n": 1}
    assert live.inflight() == {"n": 1} and len(calls) == 2      # inside the back-off: no new attempt
