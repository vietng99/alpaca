"""op-012 t-137: the Records page reads the official records and the wiki, and writes nothing.

`hub.records` lists every operation with its open and close, the recorded decisions with their parts,
and the sealed proofs of done tasks; `hub.wiki_status` and `hub.wiki_briefing` read the wiki through
alpaca.wiki.service, behind the same web login as the reports. The fixture is the Studio's own
(alpaca/tests/studio_fixture.py): four ops, one closed, three decisions and seven sealed proofs."""
import base64
import hashlib
import http.server
import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from alpaca import hub, serve
from alpaca.tests import studio_shots


@pytest.fixture(scope="module")
def atlas(tmp_path_factory):
    work = str(tmp_path_factory.mktemp("records"))
    if os.path.realpath(work).startswith(("/home/", "/Users/")):
        pytest.skip("the Studio fixture is built outside a home folder: run with --basetemp under /tmp")
    return studio_shots.build_fixture(work)


def _record_files(root):
    """The record file's bytes and an absent-or-empty -wal file as None: a read-only connection may
    leave SQLite's coordination files (db.connect_readonly); a write changes the record or the wal."""
    db = Path(root, ".alpaca", "alpaca.db")
    wal = Path(str(db) + "-wal")
    return (hashlib.sha256(db.read_bytes()).hexdigest(),
            hashlib.sha256(wal.read_bytes()).hexdigest() if wal.exists() and wal.stat().st_size else None)


def test_the_records_list_operations_decisions_and_sealed_proofs_and_write_nothing(atlas):
    before = _record_files(atlas)
    ops = hub.records(atlas, "ops")
    assert ops["counts"] == {"ops": 4, "decisions": 3, "proofs": 7} and ops["total"] == 4
    closed = [x for x in ops["items"] if x["status"] == "closed"]
    assert [x["id"] for x in closed] == ["op-001"]
    assert closed[0]["close_basis"] == "all three tasks done with sealed proofs" and closed[0]["closed"]
    assert all(x["close_basis"] is None for x in ops["items"] if x["status"] != "closed")
    decisions = hub.records(atlas, "decisions")["items"]
    assert len(decisions) == 3 and all(d["choice"] and d["options"] for d in decisions)
    assert [d["ts"] for d in decisions] == sorted((d["ts"] for d in decisions), reverse=True)
    proofs = hub.records(atlas, "proofs")["items"]
    assert len(proofs) == 7
    for p in proofs:
        assert p["path"].startswith(".alpaca/proofs/") and p["path"].endswith(".md")
        assert Path(atlas, p["path"]).is_file(), p
    assert _record_files(atlas) == before


def test_a_search_keeps_the_matching_rows_and_paging_holds_on_bad_input(atlas):
    found = hub.records(atlas, "proofs", query="PLOTS")
    assert found["total"] >= 1 and all("plots" in json.dumps(i).lower() for i in found["items"])
    assert hub.records(atlas, "proofs", query="no-such-words-here")["items"] == []
    page = hub.records(atlas, "proofs", limit=2)
    assert len(page["items"]) == 2 and page["next_offset"] == 2
    assert hub.records(atlas, "proofs", limit=2, offset=6)["next_offset"] is None
    assert hub.records(atlas, "nonsense", limit="bad", offset="bad")["section"] == "ops"


@pytest.mark.parametrize("pointer, path", [
    ("local:.alpaca/proofs/op-001/t-001.md", ".alpaca/proofs/op-001/t-001.md"),
    ("local:.alpaca/proofs/../files-auth.md", None),
    ("local:docs/readme.md", None),
    ("remote:github.com/x/y", None),
    ("local:.alpaca/proofs/op-001/t-001.evidence/x.log", None),
    (None, None)])
def test_only_a_proof_report_under_the_proofs_folder_is_linked(pointer, path):
    assert hub._proof_path(pointer) == path


@pytest.mark.parametrize("op, task", [(None, None), ("op-1", "t-1"), ("op-x", None), (None, "../t-1"),
                                      ("op-1; DROP", None)])
def test_a_briefing_names_one_op_or_one_task(atlas, op, task):
    with pytest.raises(ValueError):
        hub.wiki_briefing(atlas, op=op, task=task)


def test_a_briefing_of_a_project_with_no_wiki_store_says_so(atlas):
    brief = hub.wiki_briefing(atlas, op="op-001")
    assert brief["subject"] == "op-001" and "evidence-only" in brief["text"]


def test_the_wiki_status_is_read_once_a_minute(monkeypatch, tmp_path):
    from alpaca.wiki import service
    calls = []
    monkeypatch.setattr(service, "status", lambda root: calls.append(root) or {
        "serving": {"state": "available"}, "corpus": {"documents": 2}, "providers": {"secret": "x"},
        "switches": {"a": 1}, "capture": {"state": "ok", "path": "/somewhere"}})
    monkeypatch.setattr(hub, "_WIKI_STATUS_CACHE", {})
    first = hub.wiki_status(str(tmp_path), now=100.0)
    assert hub.wiki_status(str(tmp_path), now=100.0 + hub.WIKI_STATUS_TTL_S - 1) == first and len(calls) == 1
    hub.wiki_status(str(tmp_path), now=100.0 + hub.WIKI_STATUS_TTL_S + 1)
    assert len(calls) == 2
    # only the summary leaves: no provider settings, switches or capture paths
    assert "providers" not in first and "switches" not in first and first["capture"] == {"state": "ok"}
    assert first["corpus"] == {"documents": 2} and first["available"] is True


@pytest.fixture
def server(project):
    live = serve.Live(project)
    live.refresh()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(live, project))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield project, "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _fetch(url, auth=None):
    req = urllib.request.Request(url)
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    try:
        r = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as exc:
        r = exc
    return r.status, r.read()


def test_the_wiki_routes_need_the_web_login_and_the_records_route_answers(server):
    root, url = server
    status, body = _fetch(url + "/hub/records.json?section=decisions")
    assert status == 200 and json.loads(body)["section"] == "decisions"
    for route in ("/hub/wiki-status.json", "/hub/wiki-briefing.json?op=op-001"):
        assert _fetch(url + route)[0] == 403, route              # no login configured: closed
    Path(root, ".alpaca/files-auth").write_text("reader:test-password")
    assert _fetch(url + "/hub/wiki-briefing.json?op=op-001")[0] == 401
    status, body = _fetch(url + "/hub/wiki-briefing.json?op=op-001", "reader:test-password")
    assert status == 200 and json.loads(body)["subject"] == "op-001"
    status, body = _fetch(url + "/hub/wiki-briefing.json?op=bad", "reader:test-password")
    assert status == 400 and "name one op" in json.loads(body)["error"]
