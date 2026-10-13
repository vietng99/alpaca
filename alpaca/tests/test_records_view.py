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


def _fake_briefing(monkeypatch, calls, gate=None):
    from alpaca.wiki import service

    class Brief:
        gaps, truncated, items = ["g"], False, ()

        def __init__(self, n):
            self.text = "build %d" % n

    def build(root, op=None, task=None, max_chars=2500):
        calls.append((op, task))
        if gate:
            gate.wait(5)
        return Brief(len(calls))

    monkeypatch.setattr(service, "briefing", build)
    monkeypatch.setattr(hub, "_WIKI_BRIEFING_CACHE", {})
    monkeypatch.setattr(hub, "_WIKI_BRIEFING_BUILDS", {})
    monkeypatch.setattr(hub, "WIKI_BRIEFING_MIN_GAP_S", 0)     # rebuild as soon as an input moves


def _settle():
    for thread in list(hub._WIKI_BRIEFING_BUILDS.values()):
        thread.join(5)


def test_a_briefing_is_built_once_until_the_record_moves(monkeypatch, tmp_path):
    from alpaca import db
    calls = []
    _fake_briefing(monkeypatch, calls)
    conn = db.connect(str(tmp_path))
    db.append_event(conn, session="s", actor="a", kind="task-move", ref="t-001")
    first = hub.wiki_briefing(tmp_path, op="op-001")
    assert hub.wiki_briefing(tmp_path, op="op-001") == first and len(calls) == 1
    first["gaps"].append("mutated")
    assert hub.wiki_briefing(tmp_path, op="op-001")["gaps"] == ["g"]
    hub.wiki_briefing(tmp_path, task="t-001")                  # another subject is its own entry
    assert len(calls) == 2
    # quiet kinds do not start a rebuild; a transcript snapshot and a record event do
    db.append_event(conn, session="s", actor="a", kind="heartbeat")
    hub.wiki_briefing(tmp_path, op="op-001")
    _settle()
    assert len(calls) == 2
    db.append_event(conn, session="s", actor="a", kind="transcript-snapshot")
    hub.wiki_briefing(tmp_path, op="op-001")
    _settle()
    assert len(calls) == 3
    conn.close()


def test_a_moved_store_serves_the_old_briefing_at_once_and_the_new_one_after_the_rebuild(monkeypatch, tmp_path):
    import threading
    from alpaca import db
    from alpaca.wiki.config import Config
    calls, gate = [], threading.Event()
    _fake_briefing(monkeypatch, calls)
    db.connect(str(tmp_path)).close()
    store = Config.for_project(tmp_path).db_path
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(b"x")
    assert hub.wiki_briefing(tmp_path, op="op-001")["text"] == "build 1"
    _fake_briefing_gate = threading.Event()
    from alpaca.wiki import service

    def slow(root, op=None, task=None, max_chars=2500):
        calls.append(op)
        _fake_briefing_gate.wait(5)
        return type("B", (), {"text": "build %d" % len(calls), "gaps": [], "truncated": False, "items": ()})()

    monkeypatch.setattr(service, "briefing", slow)
    store.write_bytes(b"xy")                                   # the collector wrote the store
    # many readers while the rebuild runs: all answer with the old briefing, one build starts
    readers = [hub.wiki_briefing(tmp_path, op="op-001")["text"] for _ in range(5)]
    assert readers == ["build 1"] * 5 and len(calls) == 2
    _fake_briefing_gate.set()
    _settle()
    assert hub.wiki_briefing(tmp_path, op="op-001")["text"] == "build 2" and len(calls) == 2
    # the age bound also starts a rebuild
    key = next(iter(hub._WIKI_BRIEFING_CACHE))
    stamp, sig, value = hub._WIKI_BRIEFING_CACHE[key]
    hub._WIKI_BRIEFING_CACHE[key] = (stamp - hub.WIKI_BRIEFING_TTL_S - 1, sig, value)
    assert hub.wiki_briefing(tmp_path, op="op-001")["text"] == "build 2"
    _settle()
    assert len(calls) == 3


def test_concurrent_first_requests_start_one_build(monkeypatch, tmp_path):
    import threading
    calls, gate = [], threading.Event()
    _fake_briefing(monkeypatch, calls, gate)
    results = []
    threads = [threading.Thread(target=lambda: results.append(hub.wiki_briefing(tmp_path, op="op-001")["text"]))
               for _ in range(6)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(10)
    assert results == ["build 1"] * 6 and len(calls) == 1


def test_a_failed_first_build_reaches_its_reader_and_is_not_kept(monkeypatch, tmp_path):
    from alpaca.wiki import service

    def broken(root, **kw):
        raise RuntimeError("store unreadable")

    _fake_briefing(monkeypatch, [])
    monkeypatch.setattr(service, "briefing", broken)
    with pytest.raises(RuntimeError):
        hub.wiki_briefing(tmp_path, op="op-001")
    assert not hub._WIKI_BRIEFING_CACHE and not hub._WIKI_BRIEFING_BUILDS


@pytest.fixture
def heads(monkeypatch, tmp_path):
    """A fake briefing whose text is the record's newest task-move id, with a switch to fail."""
    from alpaca import db
    from alpaca.wiki import service
    state = {"calls": 0, "fail": False, "delay": 0.0}

    def build(root, op=None, task=None, max_chars=2500):
        import time
        state["calls"] += 1
        time.sleep(state["delay"])
        if state["fail"]:
            raise RuntimeError("store unreadable")
        c = db.connect_readonly(str(root))
        try:
            n = c.execute("SELECT COALESCE(MAX(id),0) FROM events WHERE kind='task-move'").fetchone()[0]
        finally:
            c.close()
        return type("B", (), {"text": "head=%d" % n, "gaps": [], "truncated": False, "items": ()})()

    monkeypatch.setattr(service, "briefing", build)
    monkeypatch.setattr(hub, "_WIKI_BRIEFING_CACHE", {})
    monkeypatch.setattr(hub, "_WIKI_BRIEFING_BUILDS", {})
    conn = db.connect(str(tmp_path))
    yield tmp_path, conn, state
    conn.close()


def _move(conn):
    from alpaca import db
    db.append_event(conn, session="s", actor="a", kind="task-move", ref="t-001")


def _age(seconds):
    key = next(iter(hub._WIKI_BRIEFING_CACHE))
    stamp, sig, value = hub._WIKI_BRIEFING_CACHE[key]
    hub._WIKI_BRIEFING_CACHE[key] = (stamp - seconds, sig, value)


def test_a_briefing_older_than_the_maximum_age_is_waited_for_not_served(heads):
    root, conn, state = heads
    _move(conn)
    first = hub.wiki_briefing(root, op="op-001")
    assert first["text"] == "head=1" and first["refreshing"] is False and first["built_at"] > 0
    _age(3600)                                    # nobody read it for an hour, and the record moved
    for _ in range(5):
        _move(conn)
    served = hub.wiki_briefing(root, op="op-001")
    assert served["text"] == "head=6" and served["refreshing"] is False and state["calls"] == 2


def test_within_the_maximum_age_the_old_briefing_is_served_and_marked_refreshing(heads):
    root, conn, state = heads
    _move(conn)
    hub.wiki_briefing(root, op="op-001")
    _age(hub.WIKI_BRIEFING_TTL_S + 1)
    _move(conn)
    stale = hub.wiki_briefing(root, op="op-001")
    assert stale["text"] == "head=1" and stale["refreshing"] is True
    _settle()
    assert hub.wiki_briefing(root, op="op-001")["text"] == "head=2"


def test_a_rebuild_that_keeps_failing_raises_once_the_old_briefing_is_too_old(heads):
    root, conn, state = heads
    _move(conn)
    hub.wiki_briefing(root, op="op-001")
    state["fail"] = True
    _move(conn)
    assert hub.wiki_briefing(root, op="op-001")["text"] == "head=1"      # young enough: served, rebuild fails
    _settle()
    _age(hub.WIKI_BRIEFING_MAX_AGE_S)
    with pytest.raises(RuntimeError):
        hub.wiki_briefing(root, op="op-001")
    state["fail"] = False
    assert hub.wiki_briefing(root, op="op-001")["text"] == "head=2"


def test_the_age_of_a_briefing_counts_from_when_its_build_began(heads):
    import time
    root, conn, state = heads
    _move(conn)
    state["delay"] = 0.4
    before = time.monotonic()
    hub.wiki_briefing(root, op="op-001")
    stamp = next(iter(hub._WIKI_BRIEFING_CACHE.values()))[0]
    assert stamp - before < 0.2


def test_background_builds_of_a_subject_are_at_least_the_minimum_gap_apart(monkeypatch, tmp_path):
    from alpaca import db
    calls = []
    _fake_briefing(monkeypatch, calls)
    monkeypatch.setattr(hub, "WIKI_BRIEFING_MIN_GAP_S", 10)
    conn = db.connect(str(tmp_path))
    first = hub.wiki_briefing(tmp_path, op="op-001")
    db.append_event(conn, session="s", actor="a", kind="task-move", ref="t-001")
    soon = hub.wiki_briefing(tmp_path, op="op-001")             # inputs moved, but the build is fresh
    _settle()
    assert len(calls) == 1 and soon["text"] == first["text"] and soon["refreshing"] is False
    key = next(iter(hub._WIKI_BRIEFING_CACHE))
    stamp, sig, value = hub._WIKI_BRIEFING_CACHE[key]
    hub._WIKI_BRIEFING_CACHE[key] = (stamp - hub.WIKI_BRIEFING_MIN_GAP_S - 1, sig, value)
    later = hub.wiki_briefing(tmp_path, op="op-001")            # past the gap: served at once, one rebuild
    assert later["text"] == first["text"] and later["refreshing"] is True
    _settle()
    assert len(calls) == 2 and hub.wiki_briefing(tmp_path, op="op-001")["text"] == "build 2"
    conn.close()
