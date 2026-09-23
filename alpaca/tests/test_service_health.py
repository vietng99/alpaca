"""Fault controls for service truthfulness, authentication and read-only HTTP."""
import base64
from contextlib import contextmanager
import http.server
import json
from pathlib import Path
import threading
import urllib.error
import urllib.request

import pytest

from alpaca import db, hub, serve


@contextmanager
def running(root, remote=False, refresh=False):
    live = serve.Live(root)
    if refresh:
        live.refresh()
    handler = serve.make_handler(live, root, remote=remote)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield live, 'http://127.0.0.1:%d' % server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def fetch(url, route, auth=None):
    request = urllib.request.Request(url + route)
    if auth:
        request.add_header('Authorization', 'Basic ' + base64.b64encode(auth.encode()).decode())
    try:
        response = urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, response.read()


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in Path(root).rglob('*') if p.is_file()}


def test_remote_credentials_removed_close_every_route(project, monkeypatch):
    monkeypatch.delenv('ALPACA_FILES_AUTH', raising=False)
    credentials = Path(project, '.alpaca/files-auth')
    credentials.write_text('reader:secret')
    with running(project, remote=True) as (_, url):
        assert fetch(url, '/health.json')[0] == 401
        assert fetch(url, '/health.json', 'reader:secret')[0] == 200
        credentials.unlink()
        for route in ['/', '/health.json', '/events', '/data.json', '/board/data.json',
                      '/hub/overview.json', '/hub/assets/hub.js', '/library.json',
                      '/file?path=README.md', '/live/job.json', '/files/list']:
            code, body = fetch(url, route, 'reader:secret')
            assert code == 503, (route, code)
            assert b'credentials' in body.lower()


def test_remote_missing_or_empty_password_fails_closed(project, monkeypatch):
    monkeypatch.setenv('ALPACA_FILES_AUTH', 'reader:')
    with running(project, remote=True) as (_, url):
        assert fetch(url, '/', 'reader:')[0] == 503
    monkeypatch.delenv('ALPACA_FILES_AUTH')
    with running(project) as (_, url):
        assert fetch(url, '/')[0] == 200
        assert fetch(url, '/files/read?path=README.md')[0] == 403


@pytest.mark.parametrize('credential_source', ['file', 'environment'])
def test_remote_password_only_basic_login(project, monkeypatch, credential_source):
    monkeypatch.delenv('ALPACA_FILES_AUTH', raising=False)
    if credential_source == 'file':
        Path(project, '.alpaca/files-auth').write_text(':secret')
    else:
        monkeypatch.setenv('ALPACA_FILES_AUTH', ':secret')
    with running(project, remote=True) as (_, url):
        assert fetch(url, '/health.json')[0] == 401
        assert fetch(url, '/health.json', ':wrong')[0] == 401
        assert fetch(url, '/health.json', ':secret')[0] == 200


def test_page_freshness_does_not_imply_collection_health(project, monkeypatch):
    monkeypatch.setattr(hub, 'capture_health', lambda root: {
        'status': 'degraded', 'collector': {'status': 'stopped'},
        'sources': [{'status': 'stale'}], 'coverage': {'status': 'partial'},
        'backlog': {'bytes': 100}, 'consumers': {'wiki': {'status': 'pending'}}, 'issues': []}, raising=False)
    live = serve.Live(project)
    live._set_health('fresh', 'current local projection')
    health = json.loads(live.health_json())
    assert health['transport']['status'] == 'live'
    assert health['projection']['state'] == 'fresh'
    assert health['observability']['collector']['status'] == 'stopped'
    assert health['observability']['sources'][0]['status'] == 'stale'
    assert health['status'] == 'degraded'


def test_http_reads_create_no_database_or_projection_files(project):
    before = snapshot(project)
    with running(project, refresh=True) as (live, url):
        assert json.loads(live.health_json())['projection']['state'] == 'fresh'
        for route in ['/health.json', '/data.json', '/board/data.json', '/hub/overview.json',
                      '/hub/history.json', '/hub/runs.json', '/hub/analytics-index.json']:
            assert fetch(url, route)[0] == 200
    assert snapshot(project) == before


def test_http_reads_never_open_writable_connection(project, monkeypatch):
    connection = db.connect(project)
    connection.close()
    def denied(*args, **kwargs):
        raise AssertionError('HTTP attempted writable db.connect')
    monkeypatch.setattr(db, 'connect', denied)
    with running(project, refresh=True) as (live, url):
        assert json.loads(live.health_json())['projection']['state'] == 'fresh'
        assert fetch(url, '/hub/overview.json')[0] == 200


def test_endpoint_failure_counted_without_leaking_payload(project, monkeypatch, capsys):
    def broken(root):
        raise RuntimeError('private transcript secret /home/owner/source')
    monkeypatch.setattr(hub, 'overview', broken)
    with running(project) as (live, url):
        code, body = fetch(url, '/hub/overview.json?token=secret')
        assert code == 503 and b'private transcript' not in body
        failures = json.loads(live.health_json())['server']['failures']
        assert failures['endpoint']['count'] == 1
        assert failures['endpoint']['last']['type'] == 'RuntimeError'
        assert 'secret' not in json.dumps(failures)
    stderr = capsys.readouterr().err
    assert 'endpoint' in stderr and 'RuntimeError' in stderr and 'secret' not in stderr


def test_supervision_configs_keep_remote_gate_and_project_identity(project):
    import shlex
    units = serve.service_units(project, port=7350)
    other = serve.service_units(str(Path(project).parent / 'other'), port=7350)
    assert set(units).isdisjoint(other)
    assert len(units) == 2
    dashboard = next(body for name, body in units.items() if 'dashboard' in name)
    collector = next(body for name, body in units.items() if 'collector' in name)
    command = shlex.split(next(line.split('=', 1)[1] for line in dashboard.splitlines() if line.startswith('ExecStart=')))
    assert command[-5:] == ['serve', '--keep', '--remote', '--port', '7350']
    assert 'Restart=always' in dashboard and 'RestartSec=5' in dashboard
    assert 'Environment=ALPACA_SERVE_STRICT_PORT=1' in dashboard
    assert 'Restart=on-failure' in collector and 'ALPACA_SERVE_STRICT_PORT' not in collector
    assert 'StandardError=journal' in dashboard
    assert 'collect watch --interval 15' in collector
    assert 'secret' not in dashboard


def test_remote_request_cannot_reuse_local_server(project, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(serve, 'is_running', lambda root: {'url': 'http://127.0.0.1:1/', 'port': 1, 'remote': False})
    monkeypatch.setattr(serve.cli, '_root', lambda: project)
    args = SimpleNamespace(stop=False, status=False, remote=True, detach=False, keep=True, port=1)
    assert serve.cmd_serve(args) == serve.cli.FAIL


def test_dashboard_command_recovers_after_sigkill(project, monkeypatch):
    """Re-execute the generated foreground command against its stale process state."""
    import os
    import shlex
    import socket
    import subprocess
    import sys
    import time
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    Path(project, '.alpaca/files-auth').write_text('reader:restart-test')
    units = serve.service_units(project, port=port, python=sys.executable)
    dashboard = next(body for name, body in units.items() if 'dashboard' in name)
    command = shlex.split(next(line.split('=', 1)[1] for line in dashboard.splitlines() if line.startswith('ExecStart=')))
    package_root = str(Path(serve.__file__).resolve().parent.parent)
    env = dict(os.environ, PYTHONPATH=package_root)
    for key in ('CLAUDE_PROJECT_DIR', 'ALPACA_ROOT', 'ALPACA_FILES_AUTH'):
        env.pop(key, None)
    processes = []
    started = []
    try:
        for attempt in range(2):
            proc = subprocess.Popen(command, cwd=project, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes.append(proc)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    pytest.fail('generated dashboard command exited: %s' % proc.returncode)
                try:
                    code, body = fetch('http://127.0.0.1:%d' % port, '/health.json', 'reader:restart-test')
                    if code == 200:
                        started.append(json.loads(body)['transport']['started_at'])
                        break
                except (OSError, urllib.error.URLError):
                    pass
                time.sleep(.05)
            else:
                pytest.fail('dashboard did not answer')
            assert fetch('http://127.0.0.1:%d' % port, '/health.json')[0] == 401
            proc.kill()
            proc.wait(5)
        assert started[1] > started[0]
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.wait(5)


def test_stalled_watcher_is_stale_even_while_http_answers(project, monkeypatch):
    live = serve.Live(project)
    live._set_health('fresh', 'last fold completed')
    live.checked_at = 1
    monkeypatch.setattr(serve.time, 'time', lambda: 500)
    health = json.loads(live.health_json())
    assert health['transport']['status'] == 'live'
    assert health['projection']['state'] == 'stale'


def test_remote_open_stream_closes_after_credentials_removed(project, monkeypatch):
    monkeypatch.delenv('ALPACA_FILES_AUTH', raising=False)
    monkeypatch.setattr(serve, 'HEARTBEAT_S', .02)
    credentials = Path(project, '.alpaca/files-auth')
    credentials.write_text('reader:stream-test')
    with running(project, remote=True) as (_, url):
        request = urllib.request.Request(url + '/events')
        request.add_header('Authorization', 'Basic ' + base64.b64encode(b'reader:stream-test').decode())
        with urllib.request.urlopen(request, timeout=2) as response:
            while response.readline() != b'event: rev\n':
                pass
            credentials.unlink()
            remaining = response.read()
            assert b'event: rev' not in remaining


def test_http_health_does_not_expose_source_locators(project):
    from alpaca import observability
    observability.expect_source(project, 'health-fixture', 'codex', locator='/private/account/conversation.jsonl')
    result = hub.capture_health(project)
    serialized = json.dumps(result)
    assert '/private/account' not in serialized
    assert result['sources'][0]['session'] == 'health-fixture'
    assert result['sources'][0]['backlog_bytes'] is None
    assert result['coverage']['complete'] is False


def test_health_reports_acceptance_separately_from_http(project):
    result = json.loads(serve.Live(project).health_json())
    assert result['transport']['status'] == 'live'
    assert result['acceptance']['status'] == 'unavailable'
    assert result['status_scope'] == 'projection_and_collection'


def test_cached_board_activity_expires_without_refresh_or_record_writes(project, monkeypatch):
    # A cached deterministic export may truthfully describe activity at its record time.
    # An HTTP read must age that activity against request time, even with no new events.
    monkeypatch.setattr(serve.time, 'time', lambda: 1000.0)
    with running(project) as (live, url):
        board = {'generated': 'original', 'pulse': {'now': {'as_of': '1970-01-01T00:16:40+00:00',
                 'sessions': {'work': [{'sid': 's1', 'last_beat': '1970-01-01T00:16:40+00:00',
                                       'ended': None, 'active': True}]}}}}
        live.board_json = json.dumps(board)
        live.board_rev = 'cached-record-revision'
        before = snapshot(project)
        with urllib.request.urlopen(url + '/board/data.json') as first:
            first_tag = first.headers['ETag']
            assert json.load(first)['pulse']['now']['sessions']['work'][0]['active'] is True
        monkeypatch.setattr(serve.time, 'time', lambda: 1601.0)
        request = urllib.request.Request(url + '/board/data.json', headers={'If-None-Match': first_tag})
        with urllib.request.urlopen(request) as second:
            assert second.status == 200
            assert second.headers['ETag'] != first_tag
            assert json.load(second)['pulse']['now']['sessions']['work'][0]['active'] is False
        assert json.loads(live.board_json) == board
        assert snapshot(project) == before


def test_activity_expiry_pushes_a_board_revision_without_a_new_record(project, monkeypatch):
    monkeypatch.setattr(serve.time, 'time', lambda: 1000.0)
    monkeypatch.setattr(serve, '_data_sig', lambda root: 'stable')
    live = serve.Live(project)
    live._data_sig = 'stable'
    live.data_rev = 'folded'
    live.board_rev = 'folded-board'
    live.board_json = json.dumps({'pulse': {'now': {'sessions': {'work': [
        {'sid':'s1', 'last_beat':'1970-01-01T00:16:40+00:00', 'ended':None, 'active':True}]}}}})
    live._set_health('fresh', 'ready')
    live.refresh()
    revision = json.loads(live.rev_blob())['board']
    generation = live.gen
    before = snapshot(project)
    monkeypatch.setattr(serve.time, 'time', lambda: 1601.0)
    live.refresh()
    assert json.loads(live.rev_blob())['board'] != revision
    assert live.gen > generation
    assert snapshot(project) == before
