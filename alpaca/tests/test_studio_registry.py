"""t-476: one page registry (alpaca/web/pages.json) for the Studio. serve.py builds the asset allowlist,
the hub page's stylesheet links, its inline copy of the registry and the reserved server paths from it;
the hub (hub.js, liveflags.js, loader.js through pages.js) reads that copy. Pre-mortem 2 of
design/studio-template/design.md: the served links keep the cascade order the hand-kept hub.html had,
every registry file answers, and every file under alpaca/web is accounted for."""
import http.server
import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from alpaca import serve

WEB = Path(__file__).resolve().parents[1] / "web"
REGISTRY = json.loads((WEB / "pages.json").read_text(encoding="utf-8"))
# hub.html:8-21 before the registry: the cascade order a stylesheet must keep
LINKS_BEFORE = ["hub.css", "analytics.css", "burn.css", "inflight.css", "vcs.css", "cockpit.css", "roadmap.css",
                "mission.css", "runlog.css", "live.css", "research.css", "records.css", "beam.css",
                "vendor/arthur/arthur-avatar.css", "theme.css"]
# license and provenance notes kept next to the vendored files, never served
NOTES = {"vendor/README.md", "vendor/OFL.txt", "vendor/world-atlas-LICENSE.txt"}
# the pages themselves: the Studio at /hub/ (serve.hub_page), the sign-in page and the workspace hub at
# /login and / (serve.py `_login_page`)
PAGES = {"hub.html", "login.html", "workspaces.html"}


@pytest.fixture
def server(project):
    live = serve.Live(project)
    live.refresh()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(live, project))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def fetch(url):
    try:
        r = urllib.request.urlopen(url, timeout=15)
    except urllib.error.HTTPError as exc:
        r = exc
    return r.status, r.headers, r.read()


def test_the_served_stylesheet_links_keep_the_cascade_order(server):
    status, _, body = fetch(server + "/hub/")
    page = body.decode("ascii")
    assert status == 200
    assert re.findall(r'<link rel="stylesheet" href="/hub/assets/([^"]+)">', page) == LINKS_BEFORE
    # hub.html itself keeps no list: one marker, filled by the server
    html = (WEB / "hub.html").read_text(encoding="utf-8")
    assert html.count("<!-- studio:pages -->") == 1 and "<link" not in html and "<!-- studio:pages -->" not in page


def test_the_hub_page_carries_the_registry_inline(server):
    _, _, body = fetch(server + "/hub/")
    copy = re.search(r'<script type="application/json" id="studio-pages">(.*?)</script>', body.decode("ascii"), re.S)
    assert copy and json.loads(copy.group(1)) == REGISTRY
    # the copy sits before the hub module that reads it
    assert body.index(b'id="studio-pages"') < body.index(b'src="/hub/assets/hub.js"')


def test_every_registry_file_answers_and_every_web_file_is_accounted_for(server):
    assets = serve.hub_assets()
    for name, kind in assets.items():
        status, headers, _ = fetch(server + "/hub/assets/" + name)
        assert status == 200 and headers["Content-Type"] == kind, name
    on_disk = {p.relative_to(WEB).as_posix() for p in WEB.rglob("*") if p.is_file()}
    assert set(assets) <= on_disk, set(assets) - on_disk
    unaccounted = on_disk - set(assets) - set(serve.LOGIN_ASSETS) - NOTES - PAGES
    assert not unaccounted, unaccounted
    assert fetch(server + "/hub/assets/vendor/README.md")[0] == 404


def test_the_pages_and_their_order_are_todays():
    pages = sorted(REGISTRY["pages"], key=lambda p: p["nav"])
    assert [(p["id"], p["label"]) for p in pages] == [
        ("overview", "Overview"), ("roadmap", "Roadmap"), ("cockpit", "Cockpit"), ("inflight", "In-flight"),
        ("research", "Research"), ("work", "Work & evidence"), ("runs", "Runs & logs"), ("live", "Live logs"),
        ("activity", "Activity"), ("messages", "Messages"), ("library", "Library"), ("records", "Records"),
        ("sessions", "Sessions"), ("vcs", "Version control")]
    assert sorted(p["nav"] for p in pages) == list(range(1, len(pages) + 1))
    assert [p["id"] for p in pages if p.get("gate") == "profile"] == ["runs", "live"]
    assert [p["id"] for p in pages if p.get("divider")] == ["messages"]
    assert not any(p["id"] == "mission" for p in pages)     # a view of Roadmap, not a page
    # the hub's own lists are gone: it reads the registry
    hub = (WEB / "hub.js").read_text(encoding="utf-8")
    assert "ALL_PAGES" not in hub and "PROFILE_PAGES" not in hub and "import {PAGES,PAGE} from './pages.js';" in hub
    assert "ROUTE_NEEDS=Object.fromEntries(PAGES" in (WEB / "liveflags.js").read_text(encoding="utf-8")
    assert "p.loader" in (WEB / "loader.js").read_text(encoding="utf-8")
    assert (WEB / "pages.json").read_bytes().isascii() and (WEB / "pages.js").read_bytes().isascii()


def test_every_path_the_server_dispatches_is_reserved_from_profiles():
    """GENERIC_PATHS comes from the route table and the registry endpoints; every path literal the
    request dispatch compares stays in it, so the reserved list cannot drift from the code."""
    source = (Path(serve.__file__)).read_text(encoding="utf-8")
    handler = source[source.index("def _get(self"):source.index("def _post(self")]
    compared = set(re.findall(r'path == "(/[^"]*)"', handler))
    for group in re.findall(r'path in \(([^)]*)\)', handler):
        compared |= set(re.findall(r'"(/[^"]*)"', group))
    compared -= {"/auth/logout", "/login/", "/login/info.json"}   # POST, and the public /login/ prefix
    assert compared and compared <= serve.GENERIC_PATHS, sorted(compared - serve.GENERIC_PATHS)
    for path in serve.GENERIC_PATHS:
        assert '"%s"' % path in source or any(path in part.get("endpoints", [])
                                              for part in [REGISTRY["shell"]] + REGISTRY["pages"]), path
