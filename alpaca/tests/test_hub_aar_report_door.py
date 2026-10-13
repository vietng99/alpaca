"""(cause C5; trace 0.1) The op view links the AAR report, and the link opens it through the real
server. Today no reader serves .alpaca/aar/ (paths.BROWSER_RUNTIME_OPEN, hub._document_allowed,
/file closes .alpaca). Whichever door the build opens, ways this fails after it lands:

  * the link is drawn but 404s (the Library route needs the catalog or _proof_refs)
  * widening the allowlist to "aar" also serves <op>.md.seal.json, editor leftovers (.md~), stray
    files, and lets a proof seal copy them as evidence (paths.browser_serves is the seal's rule too)
  * an endpoint taking an op id builds aar.report_path without checking the op: op=../transcripts/x
    reads .alpaca/transcripts/x.md, a private runtime file inside the root
  * an op that is not on the record still gets its .md served from .alpaca/aar/
The server is the real handler (serve.make_handler) with a web login, as test_serve_vault_browser.
"""
import base64
import http.server
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from alpaca.tests.hub_aar_kit import OP, op_view, page, rel, report, report_refs, seal, setup
from alpaca import paths, serve

LOGIN = "reader:test-password"
SECRET = "kestrel cipher wheel under the north quay"
NEEDLE = "Alpha follow-up"                      # an action item row of the sealed report


@pytest.fixture
def server(project, monkeypatch):
    conn = setup(project, monkeypatch)
    seal(conn, project)
    Path(project, ".alpaca", "files-auth").write_text(LOGIN)
    live = serve.Live(str(project))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(live, str(project)))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield Path(project), "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def fetch(url, route):
    req = urllib.request.Request(url + route)
    req.add_header("Authorization", "Basic " + base64.b64encode(LOGIN.encode()).decode())
    try:
        r = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as exc:
        r = exc
    return r.status, r.read().decode("utf-8", "replace")


def aar_endpoints():
    return sorted(p for p in serve.GENERIC_PATHS if "aar" in p.lower())


def doors(url, path=None, op=None):
    """(route, status, body) of every door that could serve an AAR file."""
    routes = []
    if path is not None:
        q = urllib.parse.quote(path, safe="")
        routes += ["/hub/document.json?path=" + q, "/files/read?path=" + q, "/file?path=" + q]
        routes += ["%s?path=%s" % (e, q) for e in aar_endpoints()]
    if op is not None:
        routes += ["%s?op=%s" % (e, urllib.parse.quote(op, safe="")) for e in aar_endpoints()]
    return [(r,) + fetch(url, r) for r in routes]


def _plant(root):
    aar_dir = root / ".alpaca" / "aar"
    (aar_dir / ("%s.md~" % OP)).write_text(SECRET, encoding="utf-8")
    (aar_dir / "notes.txt").write_text(SECRET, encoding="utf-8")
    (aar_dir / "op-999.md").write_text("# AAR op-999\n\n" + SECRET, encoding="utf-8")
    (root / ".alpaca" / "transcripts").mkdir(parents=True, exist_ok=True)
    (root / ".alpaca" / "transcripts" / "x.md").write_text(SECRET, encoding="utf-8")


def test_some_door_serves_the_sealed_report(server):
    _root, url = server
    got = doors(url, rel(OP), OP)
    assert any(s == 200 and NEEDLE in b for _r, s, b in got), \
        "no door serves the sealed report:\n" + "\n".join("%s -> %s %s" % (r, s, b[:120]) for r, s, b in got)


def test_the_link_in_the_op_view_resolves(server, tmp_path):
    root, url = server
    refs = report_refs(op_view(page(root), OP, tmp_path), OP)
    assert refs, "the op view has no report link"
    served = []
    for v in refs:
        if "path=" in v:
            q = urllib.parse.parse_qs(v.split("?", 1)[1] if "?" in v else v).get("path") or []
            for p in q:
                served += doors(url, p)
        elif v.startswith("/"):
            served.append((v,) + fetch(url, v))
        elif v.startswith(".alpaca/") or v.endswith(".md"):
            served += doors(url, v)
        elif v.startswith("#"):
            # a hash route: the page reads the report by op through an aar endpoint
            served += doors(url, None, OP)
    assert any(s == 200 and NEEDLE in b for _r, s, b in served), \
        "the link %s opens nothing:\n%s" % (refs, "\n".join("%s -> %s" % (r, s) for r, s, _b in served))


def test_sibling_and_stray_files_are_refused(server):
    root, url = server
    _plant(root)
    targets = [".alpaca/aar/%s.md.seal.json" % OP, ".alpaca/aar/%s.md~" % OP, ".alpaca/aar/notes.txt",
               ".alpaca/aar/op-999.md", ".alpaca/aar/../transcripts/x.md", ".alpaca/transcripts/x.md"]
    leaks = []
    for t in targets:
        leaks += [(r, s) for r, s, b in doors(url, t) if s == 200 and (SECRET in b or "sha256" in b)]
    for op in ("../transcripts/x", "op-999", "%s.md~" % OP, "../aar/notes", OP + "/../../transcripts/x"):
        leaks += [(r, s) for r, s, b in doors(url, None, op) if s == 200 and SECRET in b]
    assert not leaks, "doors serve files they must not: %s" % leaks


def test_the_listing_does_not_show_siblings(server):
    root, url = server
    _plant(root)
    status, body = fetch(url, "/files/list?path=" + urllib.parse.quote(".alpaca/aar", safe=""))
    if status != 200:
        return                                   # the folder is not browsable at all: nothing listed
    names = json.dumps(json.loads(body))
    for bad in (".seal.json", ".md~", "notes.txt", "op-999.md"):
        assert bad not in names, "/files/list shows %s" % bad


def test_the_proof_seal_rule_does_not_widen_to_siblings(server):
    root, _url = server
    _plant(root)
    for t in (".alpaca/aar/%s.md.seal.json" % OP, ".alpaca/aar/%s.md~" % OP, ".alpaca/aar/notes.txt",
              ".alpaca/transcripts/x.md"):
        assert not paths.browser_serves(str(root), str(root / t)), \
            "paths.browser_serves now admits %s: a proof seal may copy it into .evidence/" % t
    assert report(root).is_file()
