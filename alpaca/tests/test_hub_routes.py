"""Hub routes preserve the server gate and never turn query paths into file access."""
import base64
import http.server
import json
import threading
import urllib.request
import urllib.error
from pathlib import Path

import pytest
from alpaca import serve

@pytest.fixture
def server(project):
    live = serve.Live(project)
    live.refresh()
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), serve.make_handler(live, project))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield project, 'http://127.0.0.1:%d' % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()

def fetch(url, path, auth=None):
    req = urllib.request.Request(url + path)
    if auth:
        req.add_header('Authorization', 'Basic ' + base64.b64encode(auth.encode()).decode())
    try:
        r = urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as exc:
        r = exc
    return r.status, r.headers, r.read()

def test_shell_assets_and_missing_asset(server):
    _, url = server
    status, headers, body = fetch(url, '/hub/')
    assert status == 200
    assert b'Operations' in body and b'hub.js' in body
    assert body.isascii()
    assert body.index(b'theme.js') < body.index(b'hub.css') < body.index(b'theme.css')
    assert b'data-theme-control' in body
    for name, mime in [('hub.css','text/css'), ('hub.js','javascript'), ('icons.js','javascript'), ('cockpit.js','javascript'), ('cockpit.css','text/css'), ('theme.js','javascript'), ('theme.css','text/css'), ('runlog.js','javascript'), ('runlog.css','text/css'), ('live.js','javascript'), ('live.css','text/css')]:
        status, headers, body = fetch(url, '/hub/assets/' + name)
        assert status == 200 and mime in headers['Content-Type']
    assert fetch(url, '/hub/assets/../serve.py')[0] == 404
    assert fetch(url, '/hub/assets/%2e%2e/serve.py')[0] == 404

def test_all_hub_routes_are_authenticated(server):
    root, url = server
    Path(root, '.alpaca/files-auth').write_text('reader:test-password')
    for route in ['/hub/', '/hub/assets/hub.js', '/hub/assets/theme.js', '/hub/assets/cockpit.js', '/hub/overview.json', '/hub/session.json?sid=test']:
        assert fetch(url, route)[0] == 401
    assert fetch(url, '/hub/', 'reader:test-password')[0] == 200

def test_hub_routes_and_bad_parameters(server):
    _, url = server
    for route, key in [('overview','tasks'),('history','items'),('runs','items'),('documents','items')]:
        status, _, body = fetch(url, '/hub/' + route + '.json')
        assert status == 200 and key in json.loads(body)
    for route in ['/hub/history.json?limit=bad', '/hub/documents.json?offset=bad', '/hub/session.json?sid=../../outside']:
        assert fetch(url, route)[0] == 400
    # without a profile there is no receipt log route at all
    assert fetch(url, '/hub/log.json?receipt=../../outside')[0] == 404

def test_profile_routes_answer_json_behind_the_gate_and_map_errors(server, monkeypatch):
    from alpaca import profile
    root, url = server

    def log(root_, query):
        receipt = (query.get('receipt') or [''])[0]
        if len(receipt) != 32:
            raise ValueError('receipt must be 32 hex characters')
        return {'receipt': receipt, 'text': 'second\n'}, 200

    def broken(root_, query):
        raise RuntimeError('/home/secret/path leaked')

    class Fake(profile.Profile):
        def routes(self):
            return {'/hub/log.json': log, '/hub/broken.json': broken,
                    '/live/job.json': lambda r, q: ({'active': None}, 200)}
    monkeypatch.setattr(profile, 'load', lambda r: Fake())
    status, _, body = fetch(url, '/hub/log.json?receipt=' + 'a' * 32)
    assert status == 200 and json.loads(body)['text'] == 'second\n'
    assert fetch(url, '/hub/log.json?receipt=../../outside')[0] == 400
    status, _, body = fetch(url, '/hub/broken.json')
    assert status == 503 and b'secret' not in body
    assert json.loads(fetch(url, '/live/job.json')[2]) == {'active': None}
    Path(root, '.alpaca/files-auth').write_text('reader:test-password')
    for route in ['/hub/log.json?receipt=' + 'a' * 32, '/live/job.json']:
        assert fetch(url, route)[0] == 401

def test_catalog_reports_readable_without_opening_private_state(server):
    root, url = server
    Path(root, '.alpaca/files-auth').write_text('reader:test-password')
    reviews = Path(root, '.alpaca/reviews'); reviews.mkdir(exist_ok=True)
    (reviews / 'report.md').write_text('# Engineering report\nA measured result.')
    status, _, body = fetch(url, '/hub/document.json?path=.alpaca/reviews/report.md', 'reader:test-password')
    assert status == 200 and 'Engineering report' in json.loads(body)['text']
    private = Path(root, '.alpaca/transcripts'); private.mkdir(exist_ok=True)
    (private / 'secret.md').write_text('private session')
    (reviews / 'linked.md').symlink_to(private / 'secret.md')
    for path in ['.alpaca/files-auth', '.alpaca/transcripts/secret.md', '.alpaca/reviews/linked.md', '../outside.md']:
        assert fetch(url, '/hub/document.json?path=' + path, 'reader:test-password')[0] in (400,404)
    status, _, body = fetch(url, '/hub/documents.json', 'reader:test-password')
    assert all('transcripts' not in doc['path'] for doc in json.loads(body)['items'])

def test_document_reader_requires_configured_gate(server):
    _, url = server
    assert fetch(url, '/hub/document.json?path=README.md')[0] == 403

def test_existing_entry_points_join_the_shared_hub(server):
    _, url = server
    for route in ['/analytics/', '/hub/', '/cockpit/', '/cockpit/index.html']:
        status, _, body = fetch(url, route)
        assert status == 200 and b'/hub/assets/hub.js' in body
    # The landing page is the workspace hub; its Alpaca tile links to the cockpit at /hub/.
    status, _, body = fetch(url, '/')
    assert status == 200 and b'data-page="hub"' in body and b'/hub/assets/hub.js' not in body
    assert b'/hub/assets/hub.js' not in fetch(url, '/analytics/legacy/')[2]
    assert b'/hub/assets/hub.js' not in fetch(url, '/cockpit/legacy/')[2]


def test_analytics_routes_validate_session_and_keep_auth_gate(server):
    root, url = server
    status, _, body = fetch(url, '/hub/analytics-index.json')
    assert status == 200 and 'totals' in json.loads(body)
    assert fetch(url, '/hub/analytics.json?sid=../../outside')[0] == 400
    assert fetch(url, '/hub/analytics.json?sid=not-registered')[0] == 404
    for name in ['analytics.js', 'analytics.css']:
        assert fetch(url, '/hub/assets/' + name)[0] == 200
    Path(root, '.alpaca/files-auth').write_text('reader:test-password')
    for route in ['/hub/analytics.json?sid=test', '/hub/analytics-index.json']:
        assert fetch(url, route)[0] == 401
