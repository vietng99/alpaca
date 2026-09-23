"""The sign-in redirect target (?next=) and the reduced-motion globe after a resize.

After a sign-in the server (GET /login when already signed in) and the page (vault.js
destination()) both send the browser to ?next=. Both accept only a same-origin absolute path:
"/" then printable ASCII with no backslash, never "//" or "/\\" at the start. A control
character could split the Location header, and a backslash or tab lets the browser read the
target as another host. The last test pins that the still globe (prefers-reduced-motion) is
drawn again after a window resize, since the resize clears the canvas.
"""
import http.server
import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from alpaca import serve, weblogin

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "alpaca" / "web"

BAD = ("/%0d%0aSet-Cookie:%20injected=1", "/%09/evil.example", "/%5cevil.example", "/%5c/evil.example",
       "/%0a/evil.example", "//evil.example", "%2f%2fevil.example", "https://evil.example/",
       "/%20/evil.example", "/%c3%a9", "/%7f/x", "evil.example")


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_WORKSPACES", str(tmp_path / "registry" / "workspaces.json"))


@pytest.fixture
def url(project):
    Path(project, ".alpaca/files-auth").write_text(":246810")
    live = serve.Live(project)
    live.refresh()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(live, project))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def call(url, path, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method, headers=dict(headers or {}))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        r = urllib.request.build_opener(NoRedirect).open(req, timeout=15)
    except urllib.error.HTTPError as exc:
        r = exc
    return r.status, r.headers, r.read()


def signed_in(url):
    status, headers, _ = call(url, "/auth/login", "POST", {"code": "246810"})
    assert status == 200
    return {"Cookie": (headers.get("Set-Cookie") or "").split(";")[0]}


def test_next_cannot_add_a_header(url):
    status, headers, _ = call(url, "/login?next=/%0d%0aSet-Cookie:%20injected=1", headers=signed_in(url))
    assert status == 302 and headers["Location"] == "/"
    assert not any("injected" in value for value in headers.values())
    assert "injected" not in (headers.get_all("Set-Cookie") or [""])[0]


def test_next_refuses_every_off_site_or_unsafe_target(url):
    cookie = signed_in(url)
    for bad in BAD:
        status, headers, _ = call(url, "/login?next=" + bad, headers=cookie)
        assert (status, headers["Location"]) == (302, "/"), bad


def test_next_keeps_a_same_origin_path_with_its_query(url):
    cookie = signed_in(url)
    for good, want in (("/hub/", "/hub/"), ("/hub/tasks.json%3Fa%3D1", "/hub/tasks.json?a=1"), ("/", "/")):
        status, headers, _ = call(url, "/login?next=" + good, headers=cookie)
        assert (status, headers["Location"]) == (302, want), good


def test_safe_next_rule():
    for bad in ("/\r\nSet-Cookie: x=1", "/\\evil", "/\t/evil", "/\n/evil", "//evil", "/ /x", "/\x7f", "x",
                "https://evil/", "", None, "/\u00e9"):
        assert weblogin.safe_next(bad) == "/", repr(bad)
    for good in ("/", "/hub/", "/a?b=1&c=%2F", "/login/assets/vault.js"):
        assert weblogin.safe_next(good) == good


def _js_function(src, name):
    start = src.index("function %s(" % name)
    depth, i = 0, src.index("{", start)
    while True:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    return node


def test_page_destination_stays_on_this_origin():
    node = _node()
    src = (WEB / "vault.js").read_text(encoding="ascii")
    pieces = [line for line in src.splitlines() if line.strip().startswith("var SAFE_NEXT")]
    if "function safeNext(" in src:
        pieces.append(_js_function(src, "safeNext"))
    pieces.append(_js_function(src, "destination"))
    cases = {"/\\x": "/", "/\t/x": "/", "/\n/x": "/", "//x": "/", "/\\evil.example": "/",
             "/\r\n/x": "/", "https://evil.example/": "/", "/ /evil": "/", "x": "/",
             "/ok": "/ok", "/hub/tasks.json?a=1": "/hub/tasks.json?a=1"}
    script = "\n".join(pieces) + """
const cases = %s; const out = {};
for (const next of Object.keys(cases)) {
  global.location = {pathname: '/login', search: '?next=' + encodeURIComponent(next), hash: ''};
  out[next] = destination();
}
console.log(JSON.stringify(out));
""" % json.dumps(cases)
    run = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == cases


GLOBE_IN_NODE = r"""
const queue = [];
const listeners = {};
const stub = () => ({addColorStop() {}, width: 0});
const ctx = new Proxy({}, {get: (t, k) => (k in t ? t[k] : stub), set: (t, k, v) => { t[k] = v; return true; }});
const canvas = {width: 0, height: 0, style: {}, getContext: () => ctx,
  getBoundingClientRect: () => ({width: 400, height: 400, left: 0, top: 0})};
global.window = {
  devicePixelRatio: 1,
  matchMedia: () => ({matches: true}),
  getComputedStyle: () => ({getPropertyValue: () => ''}),
  addEventListener: (name, fn) => { listeners[name] = fn; },
  removeEventListener: () => {},
};
global.document = {documentElement: {}};
global.requestAnimationFrame = (fn) => { queue.push(fn); return queue.length; };
global.cancelAnimationFrame = () => {};
global.fetch = () => Promise.resolve({ok: false});
eval(require('fs').readFileSync(process.argv[1], 'utf8'));
window.AlpacaGlobe.mount(canvas);
setTimeout(() => {
  let n = 0;
  while (queue.length && n < 50) { queue.shift()(performance.now()); n++; }
  const drawnAtMount = n;
  listeners.resize();
  console.log(JSON.stringify({drawnAtMount: drawnAtMount, queuedAfterResize: queue.length}));
}, 50);
"""


def test_reduced_motion_globe_is_drawn_again_after_a_resize():
    node = _node()
    run = subprocess.run([node, "-e", GLOBE_IN_NODE, str(WEB / "vault-globe.js")],
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["drawnAtMount"] >= 1
    assert out["queuedAfterResize"] >= 1
