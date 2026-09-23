"""The sign-in page and the workspace hub carry the globe design.

The sign-in page draws a rotating world globe (coastlines from the vendored world-atlas data),
binary rings around the code field, a relay feed and the unlock sequence. The globe is a neutral
world: it marks no place, draws no route and singles out no country. These tests pin that the page
loads the globe, that the server hands out the globe and its data before a session exists, that
the globe and the relay feed stay neutral, and that every carried file is listed in the notices.
"""
import http.server
import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from alpaca import serve

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "alpaca" / "web"
UI = ("login.html", "workspaces.html", "vault.css", "vault.js", "vault-globe.js")


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_WORKSPACES", str(tmp_path / "registry" / "workspaces.json"))


@pytest.fixture
def server(project):
    Path(project, ".alpaca/files-auth").write_text(":246810")
    live = serve.Live(project)
    live.refresh()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(live, project))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def get(url):
    try:
        r = urllib.request.urlopen(urllib.request.Request(url), timeout=15)
    except urllib.error.HTTPError as exc:
        r = exc
    return r.status, r.headers, r.read()


def read(name):
    return (WEB / name).read_text(encoding="utf-8")


def test_sign_in_page_mounts_the_globe_and_the_rings():
    html = read("login.html")
    assert '<canvas id="globe"' in html and '<canvas id="rings"' in html
    assert 'src="/login/assets/vault-globe.js"' in html
    assert 'src="/login/assets/vault.js"' in html
    assert 'id="relay"' in html and 'id="syslog"' in html
    js = read("vault.js")
    assert "window.AlpacaGlobe.mount(globe)" in js
    assert "window.AlpacaGlobe = { mount: mount };" in read("vault-globe.js")


def test_globe_and_its_data_are_public_login_assets(server):
    assert serve.LOGIN_ASSETS["vault-globe.js"].startswith("text/javascript")
    assert serve.LOGIN_ASSETS["countries-110m.json"] == "application/json"
    for name in serve.LOGIN_ASSETS:
        assert (WEB / name).is_file(), name
    status, headers, body = get(server + "/login/assets/vault-globe.js")
    assert status == 200 and headers["Content-Type"].startswith("text/javascript")
    assert b"AlpacaGlobe" in body
    status, headers, body = get(server + "/login/assets/countries-110m.json")
    assert status == 200 and headers["Content-Type"] == "application/json"
    assert json.loads(body)["type"] == "Topology"
    status, _, body = get(server + "/login")
    assert status == 200 and b"vault-globe.js" in body


def test_coastline_data_is_the_world_atlas_110m_topology():
    topo = json.loads(read("countries-110m.json"))
    assert topo["type"] == "Topology"
    assert set(topo["objects"]) == {"countries", "land"}
    assert len(topo["objects"]["countries"]["geometries"]) == 177
    assert topo["bbox"][0] == -180 and topo["bbox"][2] == 180
    assert "scale" in topo["transform"] and "translate" in topo["transform"]


def test_globe_marks_no_place_and_singles_out_no_country():
    js = read("vault-globe.js")
    # no labels: the neutral globe draws no text at all
    assert "fillText" not in js
    # no named markers, relay targets or routes
    assert not re.search(r"\bname:\s*'", js)
    assert not re.search(r"\b(NODES|AIR_DESTS|SHIP_LOOP|HOME)\b", js)
    # no country picked out of the atlas by its numeric id
    assert not re.search(r"\.id\)?\s*===", js)
    # the coastlines come from this server only
    assert "https://" not in js and "http://" not in js
    assert "'/login/assets/countries-110m.json'" in js
    # what stays: coastlines, graticule, satellites, moon, comet, rotation, reduced motion
    for kept in ("landRings", "drawGraticule", "drawSatGlyph", "paintMoon", "drawComet",
                 "prefers-reduced-motion", "rot -="):
        assert kept in js, kept


def test_relay_feed_shows_link_telemetry_not_places():
    js = read("vault.js")
    start = js.index("/* ---------- relay decrypt feed")
    block = js[start:js.index("function mountRelay", start)]
    rows = re.findall(r"\[('[^']*'(?:, '[^']*')*)\]", block)
    assert len(rows) >= 10
    for row in rows:
        first = row.split(",")[0].strip().strip("'")
        assert re.fullmatch(r"(SAT \d{2}|Node [0-9A-F]{4})", first), row


def test_carried_ui_files_are_pure_ascii():
    for name in UI + ("countries-110m.json", "vendor/README.md"):
        data = (WEB / name).read_bytes()
        assert data.isascii(), name


def test_every_carried_ui_file_is_in_the_notices():
    notices = (ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    for name in UI:
        assert name in notices, name
    assert "contributed by the project owner" in notices
    assert "countries-110m.json" in notices and "ISC" in notices and "Natural Earth" in notices
    for font in ("plex-sans.woff2", "plex-sans-medium.woff2", "plex-mono.woff2"):
        assert font in notices, font
    assert "SIL Open Font License 1.1" in notices
    assert (WEB / "vendor" / "OFL.txt").is_file()


def test_pages_show_no_em_dash():
    for name in ("login.html", "workspaces.html"):
        text = read(name)
        assert "&mdash;" not in text and "&#8212;" not in text and "&#x2014;" not in text.lower(), name


def test_theme_keys_and_default_are_described_as_they_are():
    assert 'data-theme="dark" data-theme-key="alpaca.login-theme"' in read("login.html")
    assert 'data-theme="dark" data-theme-key="alpaca.hub-theme"' in read("workspaces.html")
    doc = (ROOT / "docs" / "operations-hub.md").read_text(encoding="utf-8")
    assert "alpaca.login-theme" in doc and "alpaca.hub-theme" in doc
    header = read("vault.js").split("(function", 1)[0] + read("vault.js").split("var THEME_KEY", 1)[0][-400:]
    assert "defaults to light" not in header and "shares alpaca.theme" not in header


def test_globe_loads_its_data_from_this_server_only():
    src = read("vault-globe.js")
    assert "CDN" not in src and "cdn" not in src.lower().replace("cdnjs", "")
    assert "urls = ['/login/assets/countries-110m.json']" in src
