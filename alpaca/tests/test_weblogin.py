"""Web sign-in: the vault login page, the session cookie, the failure throttle and the workspace hub."""
import base64
import http.server
import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from alpaca import serve, weblogin


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    """The hub reads the host workspace registry; point it at a tmp file, never ~/.config."""
    monkeypatch.setenv('ALPACA_WORKSPACES', str(tmp_path / 'registry' / 'workspaces.json'))


@pytest.fixture
def server(project):
    Path(project, '.alpaca/files-auth').write_text(':246810')
    live = serve.Live(project)
    live.refresh()
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), serve.make_handler(live, project))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield project, 'http://127.0.0.1:%d' % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def call(url, path, method='GET', body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method, headers=dict(headers or {}))
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        r = urllib.request.build_opener(NoRedirect).open(req, timeout=15)
    except urllib.error.HTTPError as exc:
        r = exc
    return r.status, r.headers, r.read()


def sign_in(url, code='246810', client=None):
    headers = {'CF-Connecting-IP': client} if client else {}
    status, headers, body = call(url, '/auth/login', 'POST', {'code': code}, headers)
    cookie = (headers.get('Set-Cookie') or '').split(';')[0]
    return status, cookie, json.loads(body)


def test_locked_page_serves_the_login_page_without_a_browser_dialog(server):
    _, url = server
    status, headers, body = call(url, '/', headers={'Accept': 'text/html'})
    assert status == 401
    assert 'WWW-Authenticate' not in headers
    assert b'data-page="login"' in body and body.isascii()
    status, headers, body = call(url, '/hub/overview.json')
    assert status == 401 and json.loads(body)['login'] == '/login'
    assert call(url, '/login')[0] == 200
    for name in serve.LOGIN_ASSETS:
        assert call(url, '/login/assets/' + name)[0] == 200, name
    assert call(url, '/login/assets/../serve.py')[0] == 404
    assert call(url, '/login/assets/hub.js')[0] == 404
    assert json.loads(call(url, '/login/info.json')[2]) == {'configured': True, 'user': False, 'signed_in': False}


def test_cookie_sign_in_opens_the_hub_and_sign_out_closes_it(server):
    root, url = server
    status, cookie, body = sign_in(url)
    assert status == 200 and body == {'ok': True} and cookie.startswith(weblogin.cookie_name(root) + '=')
    status, _, page = call(url, '/', headers={'Cookie': cookie, 'Accept': 'text/html'})
    assert status == 200 and b'data-page="hub"' in page and page.isascii()
    assert call(url, '/hub/', headers={'Cookie': cookie})[0] == 200
    listing = json.loads(call(url, '/workspaces.json', headers={'Cookie': cookie})[2])
    assert listing['workspaces'][0]['name'] == 'Alpaca' and listing['workspaces'][0]['href'] == '/hub/'
    status, headers, _ = call(url, '/login?next=/hub/', headers={'Cookie': cookie})
    assert status == 302 and headers['Location'] == '/hub/'
    status, headers, _ = call(url, '/login?next=//evil.example', headers={'Cookie': cookie})
    assert status == 302 and headers['Location'] == '/'
    status, headers, _ = call(url, '/auth/logout', 'POST')
    assert status == 204 and 'Max-Age=0' in headers['Set-Cookie']
    assert headers['Set-Cookie'].startswith(weblogin.cookie_name(root) + '=;')


def test_wrong_code_forged_cookie_and_changed_credential_are_refused(server):
    root, url = server
    assert sign_in(url, '000000')[0] == 401
    assert call(url, '/health.json', headers={'Cookie': 'alpaca_session=9999999999.00'})[0] == 401
    _, cookie, _ = sign_in(url)
    assert call(url, '/health.json', headers={'Cookie': cookie})[0] == 200
    # Changing the credential invalidates every issued cookie.
    Path(root, '.alpaca/files-auth').write_text(':135790')
    assert call(url, '/health.json', headers={'Cookie': cookie})[0] == 401


def test_basic_auth_still_works_for_scripts(server):
    _, url = server
    ok = {'Authorization': 'Basic ' + base64.b64encode(b':246810').decode()}
    assert call(url, '/health.json', headers=ok)[0] == 200


def test_throttle_locks_a_client_after_repeated_failures(server):
    _, url = server
    for _ in range(weblogin.CLIENT_MAX):
        assert sign_in(url, '111111', client='203.0.113.9')[0] == 401
    status, _, body = sign_in(url, '246810', client='203.0.113.9')
    assert status == 429 and body['retry_after'] > 0
    # A wrong Basic header from the locked client is refused too, and another client is unaffected.
    bad = {'Authorization': 'Basic ' + base64.b64encode(b':246810').decode(), 'CF-Connecting-IP': '203.0.113.9'}
    assert call(url, '/health.json', headers=bad)[0] == 401
    assert sign_in(url, client='198.51.100.4')[0] == 200


def test_cookie_and_throttle_units(tmp_path):
    root = str(tmp_path)
    os.makedirs(os.path.join(root, '.alpaca'))
    value = weblogin.issue(root, ':1', now=1000)
    assert weblogin.valid(root, ':1', value, now=1001)
    assert not weblogin.valid(root, ':1', value, now=1000 + weblogin.TTL_S + 1)
    assert not weblogin.valid(root, ':2', value, now=1001)
    assert oct(os.stat(os.path.join(root, '.alpaca', weblogin.KEY_FILE)).st_mode & 0o777) == '0o600'
    assert weblogin.credential_ok('u:p', 'u', 'p') and not weblogin.credential_ok('u:p', '', 'p')
    name = weblogin.cookie_name(root)
    assert name.startswith('alpaca_session_') and len(name) == len('alpaca_session_') + 8
    assert weblogin.read_cookie(root, 'a=1; %s=x.y; b=2' % name) == 'x.y'
    assert weblogin.read_cookie(root, 'a=1; alpaca_session=x.y; b=2') == ''
    t = weblogin.Throttle(window=60, client_max=2, global_max=3)
    t.fail('a', now=0); t.fail('a', now=1)
    assert t.blocked('a', now=2) > 0 and t.blocked('b', now=2) == 0
    t.fail('b', now=3)
    assert t.blocked('c', now=4) > 0          # the global cap closes sign-in for everyone
    assert t.blocked('c', now=100) == 0       # until the window passes


def test_browser_with_cached_basic_password_must_sign_in_and_can_sign_out(server):
    _, url = server
    basic = 'Basic ' + base64.b64encode(b':246810').decode()
    browser = {'Authorization': basic, 'Sec-Fetch-Mode': 'navigate', 'Accept': 'text/html'}
    status, _, body = call(url, '/', headers=browser)
    assert status == 401 and b'data-page="login"' in body
    _, cookie, _ = sign_in(url)
    assert call(url, '/', headers=dict(browser, Cookie=cookie))[0] == 200
    status, headers, _ = call(url, '/logout', headers={'Cookie': cookie})
    assert status == 302 and headers['Location'] == '/login' and 'Max-Age=0' in headers['Set-Cookie']
    # After sign-out the cached Basic password alone does not get the browser back in.
    assert call(url, '/', headers=browser)[0] == 401


def test_page_theme_defaults(server):
    _, url = server
    login = call(url, '/login')[2]
    assert b'data-theme="dark" data-theme-key="alpaca.login-theme"' in login
    _, cookie, _ = sign_in(url)
    hub = call(url, '/', headers={'Cookie': cookie})[2]
    # The workspace hub opens dark on its own key; the cockpit behind a tile keeps light on alpaca.theme.
    assert b'data-theme="dark" data-theme-key="alpaca.hub-theme"' in hub
    assert b'Alpaca</span> Hub' in hub and b'Alpaca</span> Hub' in login


def test_instances_keep_separate_cookie_slots_and_the_old_name_reads_signed_out(server, tmp_path):
    """G9: cookies ignore the port, so two instances on 127.0.0.1 share one jar; the per-instance
    name keeps each session in its own slot, and a cookie under the old shared name is ignored."""
    root, url = server
    other = str(tmp_path / 'other')
    os.makedirs(os.path.join(other, '.alpaca'))
    assert weblogin.cookie_name(root) != weblogin.cookie_name(other)
    _, cookie, _ = sign_in(url)
    value = cookie.split('=', 1)[1]
    assert call(url, '/health.json', headers={'Cookie': cookie})[0] == 200
    # the same value under the old shared name, or under another instance's name, is signed out
    assert call(url, '/health.json', headers={'Cookie': 'alpaca_session=' + value})[0] == 401
    assert call(url, '/health.json', headers={'Cookie': weblogin.cookie_name(other) + '=' + value})[0] == 401
    status, _, body = call(url, '/', headers={'Cookie': 'alpaca_session=' + value, 'Accept': 'text/html'})
    assert status == 401 and b'data-page="login"' in body
    # both cookies in one jar: each instance reads its own
    jar = cookie + '; ' + weblogin.cookie_name(other) + '=x.y'
    assert weblogin.read_cookie(root, jar) == value and weblogin.read_cookie(other, jar) == 'x.y'
    assert call(url, '/health.json', headers={'Cookie': jar})[0] == 200
