"""Behavioral coverage for the project-contained operations hub read models."""
import json
import os
import hashlib
from pathlib import Path
import time

import pytest

from alpaca import db, profile


class Fake(profile.Profile):
    """A stand-in domain profile: each keyword replaces one hook."""

    def __init__(self, **hooks):
        self.__dict__.update(hooks)


def use(monkeypatch, fake):
    monkeypatch.setattr(profile, "load", lambda root: fake)
    return fake


@pytest.fixture
def record(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    conn = db.connect(str(root))
    yield root, conn
    conn.close()


def event(conn, kind="task-move", **kw):
    return db.append_event(conn, session=kw.pop("session", "engineer"), actor="codex",
                           kind=kind, **kw)


def put(root, rel, data):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if isinstance(data, dict) else data)
    return path


def test_history_pages_every_event_once_even_when_new_events_arrive(record):
    from alpaca import hub
    root, conn = record
    for n in range(70):
        event(conn, data={"note": "step %s" % n})
    first = hub.history(root, limit=17)
    assert [e["id"] for e in first["items"]] == list(range(70, 53, -1))
    event(conn, data={"note": "new arrival"})
    items, page = list(first["items"]), first
    while page["has_more"]:
        page = hub.history(root, before=page["next_before"], limit=17)
        assert page["total"] == 70
        items.extend(page["items"])
    assert [e["id"] for e in items] == list(range(70, 0, -1))
    assert page["next_before"] is None


def test_history_filters_preserve_failures_and_literal_search(record):
    from alpaca import hub
    root, conn = record
    event(conn, "heartbeat")
    event(conn, "session-start", session="probe")
    failed = event(conn, "drain-failed", ref="t-021", data={"error": "lock 100% full"})
    event(conn, "task-move", ref="t-022", data={"note": "lock other"})
    found = hub.history(root, query="100%", session="engineer", ref="t-021")
    assert [e["id"] for e in found["items"]] == [failed["id"]]
    assert "lock 100% full" in found["items"][0]["summary"]
    assert hub.history(root)["total"] == 2
    assert hub.history(root, kind="all")["total"] == 4
    assert hub.history(root, kind="drain-failed")["total"] == 1
    with pytest.raises(ValueError):
        hub.history(root, before="not-a-cursor")


def test_history_folds_a_run_of_identical_failures_into_one_row(record):
    # t-606: a retried job left one drain-failed per retry; Activity shows the run once, counted.
    from alpaca import hub
    root, conn = record
    first = event(conn, "task-move", ref="t-001", data={"to": "doing"})
    for n in range(30):
        event(conn, "drain-failed", session="s%d" % (n % 4),
              data={"where": "collector", "error": "OSError: snapshot unavailable; retry required"})
    other = event(conn, "drain-failed", data={"where": "collector", "error": "OSError: disk full"})
    page = hub.history(root, kind="all", limit=10)
    assert [e["kind"] for e in page["items"]] == ["drain-failed", "task-move"]
    folded = page["items"][0]
    assert folded["id"] == other["id"] and "disk full" in folded["summary"]
    assert folded["repeat"] == 31 and folded["sessions"] == 5 and folded["variants"] == 2
    assert "_sessions" not in folded and "_variants" not in folded
    assert page["items"][1]["id"] == first["id"] and "repeat" not in page["items"][1]
    assert page["total"] == 32 and not page["has_more"]
    json.dumps(page)


def test_history_folds_a_same_instant_burst_but_never_task_moves(record):
    # a collector pass appended 128 capture-deferred in one second, one per session
    from alpaca import hub
    root, conn = record
    for n in range(128):
        event(conn, "capture-deferred", session="s%d" % n,
              data={"hook": "collector", "remaining": 500 - n, "through_event": n})
    for n in range(3):
        event(conn, "task-move", ref="t-%03d" % n, data={"to": "done"})
    items = hub.history(root, kind="all")["items"]
    assert [e["kind"] for e in items] == ["task-move"] * 3 + ["capture-deferred"]
    assert items[-1]["repeat"] == 128 and items[-1]["sessions"] == 128


def test_history_fold_pages_without_losing_or_repeating_events(record, monkeypatch):
    from alpaca import hub
    root, conn = record
    monkeypatch.setattr(hub, "HISTORY_FOLD_SCAN", 7)
    for n in range(20):
        event(conn, "drain-failed", data={"where": "collector", "error": "same"})
        if n % 5 == 4:
            event(conn, data={"note": "step %s" % n})
    seen, page = 0, hub.history(root, kind="all", limit=3)
    while True:
        seen += sum(e.get("repeat", 1) for e in page["items"])
        if not page["has_more"]:
            break
        page = hub.history(root, kind="all", before=page["next_before"], limit=3)
    assert seen == 24


def test_history_watermark_also_freezes_probe_classification(record):
    from alpaca import hub
    root, conn = record
    event(conn)
    event(conn, "session-start", session="probe")
    event(conn)
    first = hub.history(root, limit=1)
    assert first["total"] == 2
    event(conn, session="probe", data={"note": "starts work after first page"})
    second = hub.history(root, before=first["next_before"])
    assert second["total"] == 2
    assert [e["id"] for e in second["items"]] == [1]


def test_documents_exclude_dependency_files_before_pagination_and_include_proofs(record, monkeypatch):
    from alpaca import hub
    root, conn = record
    use(monkeypatch, Fake(paths=lambda: {"tools": ["domain/tools"], "documents": ["domain/spec"]}))
    for n in range(100):
        put(root, "tools/dependency/doc-%03d.md" % n, "# Dependency\n")
    for n in range(5):
        put(root, "domain/tools/dependency/doc-%03d.md" % n, "# Dependency\n")
    put(root, ".venv/hidden.md", "# Hidden")
    put(root, "node_modules/hidden.md", "# Hidden")
    put(root, ".alpaca/proofs/op-001/t-001.md", "# Simulation report\n\nAll three tests passed.\n")
    put(root, ".alpaca/proofs/op-001/t-001.kept/copy.md", "# Attachment copy")
    put(root, ".alpaca/wiki/decisions/clock.md", "# Clock decision\n\nUse the recorded clock.\n")
    put(root, "runbooks/flow.md", "# Flow runbook")
    event(conn, "proof-report", ref="t-001", data={"path": ".alpaca/proofs/op-001/t-001.md"})
    docs = hub.documents(root, limit=1)
    assert docs["total"] == 3
    assert docs["has_more"] and docs["next_offset"] == 1
    proofs = hub.documents(root, category="proofs")
    assert len(proofs["items"]) == 1
    proof = proofs["items"][0]
    assert proof["title"] == "Simulation report"
    assert proof["sealed"] is True and proof["task"] == "t-001"
    assert proof["summary"] == "All three tests passed."
    assert hub.documents(root, category="tools", limit=200)["total"] == 105
    assert hub.documents(root, category="all")["total"] == 3
    put(root, "domain/spec/acceptance.md", "# Acceptance\n")
    runbooks = hub.documents(root, category="runbooks")["items"]
    assert sorted(i["path"] for i in runbooks) == ["domain/spec/acceptance.md", "runbooks/flow.md"]


def _fresh_documents(hub, root, **params):
    """The catalog answer with both caches dropped: what an uncached read returns."""
    hub._CATALOG.clear()
    hub._TEXT_CACHE.clear()
    hub._FOLDER_MEMO.clear()
    return hub.documents(root, **params)


def test_cached_catalog_equals_uncached_for_every_param(record, monkeypatch):
    from alpaca import hub
    root, conn = record
    use(monkeypatch, Fake(paths=lambda: {"tools": ["domain/tools"], "documents": ["domain/spec"]}))
    put(root, "docs/a.md", "# Alpha\n\nFirst report.\n")
    put(root, "docs/b.html", "<html><head><title>Beta page</title></head></html>")
    put(root, "docs/decisions/c.md", "# Gamma\n")
    put(root, "tools/dep/d.md", "# Dependency\n")
    put(root, "domain/spec/runbook.yaml", "runbook: 3\ngoal: tune\n")
    put(root, "top.md", "# Top\n")
    put(root, ".alpaca/proofs/op-001/t-001.md", "# Proof\n\nDone.\n")
    event(conn, "proof-report", ref="t-001", data={"path": ".alpaca/proofs/op-001/t-001.md"})
    cases = [{}, {"limit": 1}, {"limit": 1, "offset": 1}, {"category": "all"}, {"category": "proofs"},
             {"category": "runbooks"}, {"category": "decisions"}, {"category": "tools"},
             {"category": "all", "query": "alpha"}, {"category": "all", "ext": "html"},
             {"category": "all", "ext": "yml"}]
    for params in cases:
        want = _fresh_documents(hub, root, **params)
        assert hub.documents(root, **params) == want        # the first read fills the cache
        assert hub.documents(root, **params) == want        # the second is answered from it
    assert hub.documents(root, category="all")["total"] > 1


def test_cached_catalog_sees_added_removed_renamed_and_edited_documents(record):
    from alpaca import hub
    root, conn = record
    put(root, "docs/a.md", "# Alpha\n")
    paths = lambda **kw: sorted(i["path"] for i in hub.documents(root, category="all", **kw)["items"])
    assert paths() == ["docs/a.md"]
    assert paths() == ["docs/a.md"]                         # now cached
    put(root, "docs/deep/new/b.md", "# Beta\n")            # a new document in a new folder
    assert paths() == ["docs/a.md", "docs/deep/new/b.md"]
    put(root, "top.md", "# Top\n")                         # a new document at the project root
    assert "top.md" in paths()
    (root / "docs/a.md").rename(root / "docs/renamed.md")
    assert paths() == ["docs/deep/new/b.md", "docs/renamed.md", "top.md"]
    (root / "docs/deep/new/b.md").unlink()
    assert paths() == ["docs/renamed.md", "top.md"]
    put(root, "docs/renamed.md", "# Retitled and longer\n")  # an edit changes no listing
    titles = lambda: {i["path"]: i["title"] for i in hub.documents(root, category="all")["items"]}
    assert titles()["docs/renamed.md"] == "Retitled and longer"
    put(root, "specs/runbook.md", "# Plan\n")             # a root that did not exist yet
    assert "specs/runbook.md" in paths()


def test_a_new_proof_shows_and_a_walk_reads_only_the_folders_that_moved(record, monkeypatch):
    """t-593: one new file in a proof folder is one folder walked again, not every path resolved."""
    from alpaca import hub
    root, conn = record
    for i in range(5):
        put(root, ".alpaca/proofs/op-001/t-00%d.md" % i, "# Proof %d\n" % i)
        put(root, "docs/set%d/r.md" % i, "# Report %d\n" % i)
    paths = lambda: sorted(i["path"] for i in hub.documents(root, category="all")["items"])
    before = paths()
    assert ".alpaca/proofs/op-001/t-009.md" not in before
    put(root, ".alpaca/proofs/op-001/t-009.md", "# Late proof\n")
    calls = []
    real = hub._contained
    monkeypatch.setattr(hub, "_contained", lambda r, v: calls.append(v) or real(r, v))
    after = paths()
    walked = len(calls)
    assert after == sorted(before + [".alpaca/proofs/op-001/t-009.md"])
    assert after == sorted(i["path"] for i in _fresh_documents(hub, root, category="all")["items"])
    assert walked < len(calls) // 2        # the moved folder and the root folders, not every file


def test_a_file_written_while_its_folder_is_listed_shows_on_the_next_read(record, monkeypatch):
    """t-593 review B1: the folder stamp is taken before the listing, so a write that lands during
    the listing is not kept under the newer stamp."""
    from alpaca import hub
    root, conn = record
    put(root, "docs/a.md", "# A\n")
    real = os.scandir
    state = {"armed": True, "seen": 0}      # the signature lists docs first, the walk second

    def racing(path):
        got = real(path)
        if state["armed"] and str(path).endswith("/docs") and state.__setitem__("seen", state["seen"] + 1) is None \
                and state["seen"] == 2:
            state["armed"] = False
            list_first = list(got)           # the listing the walk keeps ...
            put(root, "docs/late.md", "# Late\n")   # ... then a write lands before the walk returns
            got.close()
            return iter_scan(list_first)
        return got
    class iter_scan:
        def __init__(self, entries): self.entries = entries
        def __enter__(self): return iter(self.entries)
        def __exit__(self, *a): return False
        def __iter__(self): return iter(self.entries)
        def close(self): pass
    monkeypatch.setattr(os, "scandir", racing)
    paths = lambda: sorted(i["path"] for i in hub.documents(root, category="all")["items"])
    paths()
    monkeypatch.setattr(os, "scandir", real)
    assert "docs/late.md" in paths()
    assert "docs/late.md" in paths()


def test_cached_catalog_never_lists_a_file_outside_the_allowlist(record, tmp_path):
    from alpaca import hub
    root, conn = record
    put(root, "docs/ok.md", "# Fine\n")
    external = put(tmp_path, "secret.md", "# External secret\n")
    hub.documents(root, category="all")                     # warm the cache
    (root / "docs/escape.md").symlink_to(external)
    put(root, "docs/x.evidence/copy.md", "# Evidence copy\n")
    put(root, ".venv/lib/hidden.md", "# Hidden\n")
    put(root, "docs/.private/p.md", "# Private\n")
    event(conn, "proof-report", ref="t-external", data={"path": str(external)})
    for params in ({}, {"category": "all"}, {"category": "proofs"}, {"category": "runbooks"}):
        for _ in range(2):
            listed = json.dumps(hub.documents(root, **params))
            assert "secret" not in listed and "Evidence copy" not in listed
            assert "Hidden" not in listed and "Private" not in listed and "escape.md" not in listed
    assert [i["path"] for i in hub.documents(root, category="all")["items"]] == ["docs/ok.md"]


def _document_outcome(hub, root, path):
    try:
        return hub.document(root, path)
    except FileNotFoundError as error:
        return "refused: %s" % error


def test_cached_document_authorization_equals_uncached(record, tmp_path):
    from alpaca import hub
    root, conn = record
    put(root, "docs/ok.md", "# Fine\n")
    put(root, "docs/gone.md", "# Gone\n")
    put(root, "notes/outside.md", "# Not in a catalog root\n")
    put(root, "specs/runbook.md", "# Plan\n")
    external = put(tmp_path, "secret.md", "# External secret\n")
    (root / "docs/link.md").symlink_to(root / "docs/ok.md")
    (root / "docs/escape.md").symlink_to(external)
    for rel in ("docs/ok.md", "docs/gone.md"):
        hub.document(root, rel)                              # warm the cache
    (root / "docs/gone.md").unlink()
    for rel in ("docs/ok.md", "docs/gone.md", "notes/outside.md", "specs/runbook.md",
                "docs/link.md", "docs/escape.md", "../secret.md"):
        cached = _document_outcome(hub, root, rel)
        hub._CATALOG.clear()
        hub._TEXT_CACHE.clear()
        assert cached == _document_outcome(hub, root, rel), rel
    assert hub.document(root, "docs/ok.md")["text"] == "# Fine\n"
    assert _document_outcome(hub, root, "docs/gone.md").startswith("refused")
    assert _document_outcome(hub, root, "notes/outside.md").startswith("refused")
    assert _document_outcome(hub, root, "docs/escape.md").startswith("refused")
    assert hub.document(root, "docs/link.md")["path"] == "docs/ok.md"


def test_document_added_after_the_catalog_was_built_is_readable(record):
    from alpaca import hub
    root, conn = record
    put(root, "docs/first.md", "# First\n")
    hub.document(root, "docs/first.md")
    assert _document_outcome(hub, root, "docs/later.md").startswith("refused")
    put(root, "docs/later.md", "# Later\n")
    assert hub.document(root, "docs/later.md")["text"] == "# Later\n"
    put(root, "docs/deep/newer.md", "# Newer\n")
    assert hub.document(root, "docs/deep/newer.md")["text"] == "# Newer\n"


def _uncached_view(hub, root, rels):
    """What an uncached read answers for `rels` and the listing, leaving the caches empty."""
    hub._CATALOG.clear()
    hub._TEXT_CACHE.clear()
    view = _catalog_view(hub, root, rels)
    hub._CATALOG.clear()
    hub._TEXT_CACHE.clear()
    return view


def _catalog_view(hub, root, rels):
    served = {}
    for rel in rels:
        try:
            got = hub.document(root, rel)
            served[rel] = (got["path"], got["text"])
        except FileNotFoundError:
            served[rel] = None
    items = hub.documents(root, category="all", limit=200)["items"]
    return served, sorted((i["path"], i["title"]) for i in items)


def test_cached_catalog_follows_a_link_retargeted_outside_the_walked_folders(record):
    import os
    from alpaca import hub
    root, conn = record
    put(root, "notes/v1/x.md", "# Version one\n")
    put(root, "notes/v2/x.md", "# Version two\n")
    os.symlink("v1", root / "notes/cur")
    put(root, "docs/ok.md", "# Ok\n")
    os.symlink("../notes/cur/x.md", root / "docs/link.md")
    rels = ["notes/v1/x.md", "notes/v2/x.md", "docs/link.md"]
    _catalog_view(hub, root, rels)                      # warm
    os.unlink(root / "notes/cur")
    os.symlink("v2", root / "notes/cur")                # notes/ is not a walked folder
    assert _catalog_view(hub, root, rels) == _uncached_view(hub, root, rels)
    assert _catalog_view(hub, root, rels)[0]["docs/link.md"] == ("notes/v2/x.md", "# Version two\n")
    assert _catalog_view(hub, root, rels)[0]["notes/v1/x.md"] is None


def test_cached_catalog_follows_a_file_link_chain_retargeted_outside_the_walked_folders(record):
    import os
    from alpaca import hub
    root, conn = record
    put(root, "notes/x.md", "# X\n")
    put(root, "notes/y.md", "# Y\n")
    os.symlink("x.md", root / "notes/hop.md")
    (root / "docs").mkdir()
    os.symlink("../notes/hop.md", root / "docs/link.md")
    rels = ["notes/x.md", "notes/y.md", "docs/link.md"]
    _catalog_view(hub, root, rels)
    os.unlink(root / "notes/hop.md")
    os.symlink("y.md", root / "notes/hop.md")
    assert _catalog_view(hub, root, rels) == _uncached_view(hub, root, rels)
    assert _catalog_view(hub, root, rels)[0]["docs/link.md"] == ("notes/y.md", "# Y\n")


def test_cached_catalog_follows_a_retargeted_link_at_the_project_root(record):
    import os
    from alpaca import hub
    root, conn = record
    put(root, "docs/a.md", "# A\n")
    put(root, "docs/b.md", "# B\n")
    os.symlink("docs/a.md", root / "top.md")
    rels = ["top.md", "docs/a.md", "docs/b.md"]
    _catalog_view(hub, root, rels)
    os.unlink(root / "top.md")
    os.symlink("docs/b.md", root / "top.md")
    assert _catalog_view(hub, root, rels) == _uncached_view(hub, root, rels)


def test_cached_catalog_sees_a_folder_made_unreadable_and_readable_again(record):
    import os
    from alpaca import hub
    root, conn = record
    put(root, "docs/sub/a.md", "# A\n")
    rels = ["docs/sub/a.md"]
    os.chmod(root / "docs/sub", 0)
    try:
        during = _catalog_view(hub, root, rels)         # a walk that cannot enter the folder
    finally:
        os.chmod(root / "docs/sub", 0o755)              # moves ctime only
    after = _catalog_view(hub, root, rels)
    assert after == _uncached_view(hub, root, rels)
    assert after[0]["docs/sub/a.md"] is not None
    if os.geteuid() != 0:
        assert during[0]["docs/sub/a.md"] is None


def test_cached_title_follows_a_same_size_edit_with_the_time_put_back(record):
    import os
    from alpaca import hub
    root, conn = record
    path = put(root, "docs/a.md", "# Alpha\n")
    _catalog_view(hub, root, [])
    stat = os.stat(path)
    path.write_text("# Gamma\n")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert [i["title"] for i in hub.documents(root, category="all")["items"]] == ["Gamma"]


def test_title_cache_is_kept_per_project_root(record, tmp_path, monkeypatch):
    from alpaca import hub
    root, conn = record
    other = tmp_path / "other"
    put(other, "docs/o.md", "# Other\n")
    put(root, "docs/a.md", "# Alpha\n")
    put(root, "docs/b.md", "# Beta\n")
    hub.documents(other.resolve(), category="all")
    monkeypatch.setattr(hub, "_TEXT_CACHE_MAX", 1)
    hub.documents(root, category="all")                 # overflows this root's cache only
    assert str(other.resolve()) in hub._TEXT_CACHE and hub._TEXT_CACHE[str(other.resolve())]


def test_documents_search_and_paths_are_project_contained(record, tmp_path):
    from alpaca import hub
    root, conn = record
    put(root, "docs/result.html", "<html><head><title>Timing results</title></head><body>Passed</body></html>")
    put(root, "docs/another.md", "# Different report")
    external = put(tmp_path, "secret.md", "# External secret")
    (root / "docs/escape.md").symlink_to(external)
    (root / ".alpaca/proofs").symlink_to(tmp_path, target_is_directory=True)
    event(conn, "proof-report", ref="t-external", data={"path": str(external)})
    result = hub.documents(root, query="timing", ext="html")
    assert [i["path"] for i in result["items"]] == ["docs/result.html"]
    assert "secret" not in json.dumps(hub.documents(root))


def test_overview_carries_the_profile_check_and_acceptance_without_writing(record, monkeypatch):
    from alpaca import hub
    root, conn = record
    db.upsert(conn, "tasks", "id", {"id": "t-021", "statement": "Build hub", "status": "doing", "op": "op-006"})
    rid = "a" * 32
    seen = []

    def acceptance(root_, conn_, checked, stamp):
        seen.append(checked)
        return [{"id": "AC-09", "stage": "sim", "status": "BLOCKED", "recorded_status": "PASS",
                 "reason": checked[0]["reason"]}]
    use(monkeypatch, Fake(stages=lambda: ("sim",), acceptance=acceptance, check=lambda r: [
        {"stage": "sim", "verdict": "BLOCKED", "reason": "inputs changed", "receipt_id": rid,
         "recorded_status": "PASS", "recorded_at": 1700.0, "recorded_session": "worker"}]))
    before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    result = hub.overview(root)
    assert seen and seen[0][0]["receipt_id"] == rid
    item = result["items"][0]
    assert item["id"] == "AC-09" and item["status"] == "BLOCKED" and "inputs changed" in item["reason"]
    assert result["counts"]["items_blocked"] == 1
    assert result["tasks"][0]["title"] == "Build hub"
    stage = result["stages"][0]
    assert stage["status"] == "BLOCKED" and stage["recorded_status"] == "PASS"
    assert stage["recorded_at"] == 1700.0 and stage["recorded_session"] == "worker"
    assert result["acceptance"]["status"] == "BLOCKED"
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before


def test_active_jobs_are_runs_whose_receipt_the_check_confirms_running(record, monkeypatch):
    from alpaca import hub
    root, _ = record
    running, stale = "1" * 32, "2" * 32
    runs = {"items": [
        {"id": "j1", "verdict": "RUNNING", "stages": [{"receipt_id": running}]},
        {"id": "j2", "verdict": "RUNNING", "stages": [{"receipt_id": stale}]}], "errors": []}
    use(monkeypatch, Fake(runs=lambda r: runs, check=lambda r: [
        {"stage": "sim", "verdict": "RUNNING", "receipt_id": running},
        {"stage": "lint", "verdict": "INTERRUPTED", "receipt_id": stale}]))
    result = hub.overview(root)
    assert result["active_jobs"] == ["j1"] and result["counts"]["active_jobs"] == 1
    assert hub.runs(root)["total"] == 2


def test_overview_without_a_profile_is_read_only_and_reports_capture_failure(record):
    from alpaca import hub
    root, conn = record
    event(conn, "drain-failed", ref="t-021", data={"error": "wiki ledger locked", "recovery": "retry drain"})
    before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    result = hub.overview(root)
    assert result["stages"] == [] and result["items"] == []
    assert result["capture"]["status"] == "error"
    assert "wiki ledger locked" in result["capture"]["errors"][0]["summary"]
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
    assert not (root / ".alpaca/flow").exists()


def test_page_overview_leaves_task_text_and_messages_to_their_own_routes(record):
    from alpaca import hub, serve
    root, conn = record
    db.upsert(conn, "tasks", "id", {"id": "t-021", "statement": "Build hub", "status": "doing", "op": "op-006"})
    db.upsert(conn, "tasks", "id", {"id": "t-022", "statement": "Second", "status": "open", "op": "op-006"})
    for n in (23, 24, 25):
        db.upsert(conn, "tasks", "id", {"id": "t-0%d" % n, "statement": "More", "status": "open", "op": "op-006"})
    for n, at in ((26, "2026-10-01T10:00:00+07:00"), (27, "2026-10-02T03:00:00+00:00")):
        db.upsert(conn, "tasks", "id", {"id": "t-0%d" % n, "statement": "Done", "status": "done", "op": "op-006", "updated": at})
    event(conn, "msg-post", ref="t-021")
    full = hub.overview(root)
    page = hub.page_overview(full)
    assert {"t-026", "t-027"} <= {t["id"] for t in page["tasks"] if "text" in t}
    assert "messages" not in page and all(k in full["tasks"][0] for k in hub.TASK_TEXT)
    assert [t["id"] for t in page["tasks"]] == [t["id"] for t in full["tasks"]]
    assert not any(k in t for t in page["tasks"] for k in hub.TASK_TEXT)
    assert page["tasks"][0]["title"] == "Build hub" and page["tasks"][0]["status"] == "doing"
    # the doing task carries its text inline (the cockpit draws it), the open one only its text_rev
    assert set(page["tasks"][0]["text"]) == set(hub.TASK_TEXT) and "text" not in page["tasks"][2]
    # nothing-valued keys and the keys no page reads are left out
    assert not any(k in t for t in page["tasks"] for k in ("claimant", "completed_actor", "number", "ref"))
    # the rev follows exactly the text: any one field changing changes it
    base = dict(full["tasks"][2])
    assert hub.text_rev(base) == page["tasks"][2]["text_rev"]
    for field in hub.TASK_TEXT:
        assert hub.text_rev({**base, field: {"changed": field}}) != hub.text_rev(base)
    live = serve.Live(root)
    body, _ = live.overview()
    assert json.loads(body)["tasks"] == page["tasks"]
    whole = json.loads(live.work_view("tasks")[0])["tasks"]
    one = json.loads(live.work_view("tasks", "t-022")[0])["tasks"]
    assert [t["id"] for t in whole] == ["t-021", "t-022", "t-023", "t-024", "t-025", "t-026", "t-027"] and [t["id"] for t in one] == ["t-022"]
    assert one[0]["text_rev"] == page["tasks"][1]["text_rev"]
    assert all(k in whole[0] for k in hub.TASK_TEXT)
    assert json.loads(live.work_view("messages")[0])["messages"] == full["messages"]


def test_overview_surfaces_unreadable_capture_health(record, monkeypatch):
    from alpaca import hub
    root, _ = record
    use(monkeypatch, Fake(capture=lambda r, c: {"status": "missing", "errors": [
        {"path": ".alpaca/domain/capture-health.json", "error": "Expecting property name"}]}))
    result = hub.overview(root)
    assert result["capture"]["status"] == "error"
    assert any(e["kind"] == "capture-health" for e in result["capture"]["errors"])


def test_verified_wiki_recovery_resolves_only_covered_drain_failures(record):
    from alpaca import hub
    root, conn = record
    old = event(conn, "drain-failed", data={"error": "LedgerLockTimeout: abandoned lock"})
    recovered = event(conn, "wiki-recovered", data={"status": "ok", "through_event": old['id'],
                                                    "verified_events": 1})
    assert hub.overview(root)['capture']['status'] == 'ok'
    assert hub.history(root, kind='drain-failed')['total'] == 1
    new = event(conn, 'drain-failed', data={'error': 'new failure'})
    capture = hub.overview(root)['capture']
    assert capture['status'] == 'error'
    assert [e['id'] for e in capture['errors']] == [new['id']]


def test_resolved_failures_cannot_hide_older_unresolved_errors(record):
    from alpaca import hub
    root, conn = record
    analytics = event(conn, 'analytics-build-failed', data={'error': 'index broken'})
    for _ in range(25):
        last = event(conn, 'drain-failed', data={'error': 'LedgerLockTimeout: old lock failure'})
    event(conn, 'wiki-recovered', data={'status': 'ok', 'through_event': last['id'],
                                       'verified_events': 26})
    capture = hub.overview(root)['capture']
    assert capture['status'] == 'error'
    assert [e['id'] for e in capture['errors']] == [analytics['id']]


def test_wiki_recovery_keeps_ambiguous_lifecycle_failures(record):
    from alpaca import hub
    root, conn = record
    pad = event(conn, 'drain-failed', data={'where': 'pre_compact',
                                          'error': 'PermissionError: pad is unwritable'})
    unknown = event(conn, 'drain-failed', data={'where': 'session_end'})
    wiki = event(conn, 'drain-failed', data={'where': 'wiki-recover', 'error': 'disk full'})
    event(conn, 'wiki-recovered', data={'status': 'ok', 'through_event': wiki['id'],
                                       'verified_events': 3})
    assert [e['id'] for e in hub.overview(root)['capture']['errors']] == [unknown['id'], pad['id']]


def test_profile_capture_recovery_does_not_clear_unrelated_or_new_failures(record, monkeypatch):
    from alpaca import hub
    root, conn = record
    unrelated = event(conn, 'capture-failed', data={'error': 'another collector'})
    old = event(conn, 'capture-failed', op='domain-run', data={'error': 'domain mirror failed'})
    assert [e['id'] for e in hub.overview(root)['capture']['errors']] == [old['id'], unrelated['id']]
    health = {'status': 'ok', 'cutoff': old['id'], 'resolved_ops': ['domain-run'],
              'error_kinds': ['domain-capture-error']}
    use(monkeypatch, Fake(capture=lambda r, c: dict(health)))
    assert [e['id'] for e in hub.overview(root)['capture']['errors']] == [unrelated['id']]
    new = event(conn, 'capture-failed', op='domain-run', data={'error': 'new domain failure'})
    own = event(conn, 'domain-capture-error', data={'error': 'mirror stalled'})
    assert [e['id'] for e in hub.overview(root)['capture']['errors']] == [own['id'], new['id'], unrelated['id']]
    assert hub.overview(root)['capture']['profile']['cutoff'] == old['id']


def test_absent_record_readers_do_not_create_a_database(tmp_path):
    from alpaca import hub
    assert hub.history(tmp_path)["total"] == 0
    assert hub.overview(tmp_path)["tasks"] == []
    assert not (tmp_path / ".alpaca").exists()


def test_progress_feed_separates_unbound_diagnostics_without_hiding_history(record):
    from alpaca import hub
    root, conn = record
    task = event(conn, "task-move", ref="t-023", data={"to": "done"})
    noise = event(conn, "run", session="instrument", ref="selftest", data={"verdict": "PASS"})
    failed_noise = event(conn, "run", session="instrument", ref="schema-test", data={"verdict": "FAIL"})
    flow = event(conn, "run", session="instrument", op="adder8-flow", ref="sim", data={"verdict": "PASS"})
    capture = event(conn, "capture-failed", session="instrument", data={"error": "capture interrupted"})
    progress = hub.history(root, kind="progress", limit=2)
    assert [e["id"] for e in progress["items"]] == [capture["id"], flow["id"]]
    older = hub.history(root, kind="progress", before=progress["next_before"], limit=2)
    assert [e["id"] for e in older["items"]] == [task["id"]]
    assert older["total"] == 3
    for kind in ("work", "all"):
        ids = {e["id"] for e in hub.history(root, kind=kind)["items"]}
        assert {noise["id"], failed_noise["id"]} <= ids


def test_the_work_feed_treats_profile_side_effects_as_no_work(record, monkeypatch):
    from alpaca import hub
    root, conn = record
    start = event(conn, "session-start", session="closer")
    event(conn, "domain-receipt", session="closer", data={"verdict": "PASS"})
    assert start["id"] in {e["id"] for e in hub.history(root, kind="work")["items"]}
    use(monkeypatch, Fake(events=lambda: {"side_effect_kinds": ["domain-receipt"]}))
    assert start["id"] not in {e["id"] for e in hub.history(root, kind="work")["items"]}


def test_subagent_and_session_bookkeeping_stay_out_of_the_progress_feed(record):
    from alpaca import hub
    root, conn = record
    move = event(conn, "task-move", ref="t-001", data={"to": "doing"})
    sub = event(conn, "subagent-stop", session="s1")
    start = event(conn, "session-start", session="s1")
    note = event(conn, "msg", session="s1", data={"body": "hello"})
    broken = event(conn, "subagent-stop", session="s1", data={"error": "agent crashed"})
    progress = {e["id"] for e in hub.history(root, kind="progress")["items"]}
    assert progress == {move["id"], broken["id"]}
    work = {e["id"] for e in hub.history(root, kind="work")["items"]}
    assert sub["id"] not in work and {move["id"], start["id"], note["id"], broken["id"]} <= work
    assert sub["id"] in {e["id"] for e in hub.history(root, kind="all")["items"]}


def test_activity_day_filter_and_hour_counts(record):
    from alpaca import hub
    root, conn = record
    rows = [event(conn, "task-move", ref="t-%03d" % i, data={"to": "done"}) for i in range(4)]
    stamps = ["2026-09-29T08:10:00+07:00", "2026-09-30T08:20:00+07:00",
              "2026-09-30T08:40:00+07:00", "2026-09-30T23:59:00+07:00"]
    for row, ts in zip(rows, stamps):
        conn.execute("UPDATE events SET ts = ? WHERE id = ?", (ts, row["id"]))
    conn.commit()
    day = hub.history(root, kind="progress", day="2026-09-30", calendar=True)
    assert [e["id"] for e in day["items"]] == [rows[3]["id"], rows[2]["id"], rows[1]["id"]]
    assert day["total"] == 3
    counts = {(h["day"], h["hour"]): h["count"] for h in day["hours"]}
    assert counts == {("2026-09-29", 8): 1, ("2026-09-30", 8): 2, ("2026-09-30", 23): 1}
    assert "hours" not in hub.history(root, kind="progress")
    with pytest.raises(ValueError):
        hub.history(root, day="30-09-2026")


_PAGE_PARAMS = [dict(limit=6, kind="progress"), dict(limit=5), dict(kind="progress", limit=100),
                dict(kind="progress", limit=100, calendar=True), dict(kind="all", calendar=True),
                dict(kind="task-move"), dict(kind="progress", limit=1000, calendar=True, day="2026-10-01"),
                dict(session="s1"), dict(ref="t-001", kind="all"), dict(query="crash")]


def _fresh(hub, root, **kw):
    """The page counted from scratch, as before the counts were kept."""
    hub._HISTORY_COUNTS.clear()
    return hub.history(root, **kw)


def _same_everywhere(hub, root):
    for kw in _PAGE_PARAMS:
        kept = hub.history(root, **kw)
        assert kept == _fresh(hub, root, **kw), kw
        # a second read is served from the kept count and stays equal
        assert hub.history(root, **kw) == kept, kw


def test_kept_history_counts_equal_fresh_counts_as_the_record_grows(record):
    from alpaca import hub
    root, conn = record
    hub._HISTORY_COUNTS.clear()
    rows = [event(conn, "task-move", ref="t-001", session="s1", data={"to": "doing"}),
            event(conn, "heartbeat", session="s2"), event(conn, "session-start", session="s2"),
            event(conn, "subagent-stop", session="s1"), event(conn, "msg", session="s1", data={"body": "hi"})]
    conn.execute("UPDATE events SET ts = '2026-10-01T09:00:00+07:00' WHERE id <= ?", (rows[-1]["id"],))
    conn.commit()
    _same_everywhere(hub, root)
    # s2 had only a heartbeat; its first work event makes its session-start count (a flip under the kept count)
    for add in (lambda: event(conn, "task-add", ref="t-002", session="s2"),
                lambda: event(conn, "session-start", session="s3"),
                lambda: event(conn, "heartbeat", session="s3"),
                lambda: event(conn, "capture-failed", session="s1", data={"error": "crash"}),
                lambda: event(conn, "drain-failed", session="s4", data={"error": "x"}),
                lambda: event(conn, "session-end", session="s1"), lambda: event(conn, "turn-end", session="s5")):
        add()
        _same_everywhere(hub, root)


def test_a_countable_event_moves_the_total_and_a_quiet_one_does_not(record):
    from alpaca import hub
    root, conn = record
    hub._HISTORY_COUNTS.clear()
    event(conn, "task-move", ref="t-001")
    first = hub.history(root, kind="progress")
    assert first["total"] == 1
    for kind in ("heartbeat", "turn-end", "transcript-snapshot", "subagent-start"):
        event(conn, kind, session="engineer")
        again = hub.history(root, kind="progress")
        assert again["total"] == 1 and [e["id"] for e in again["items"]] == [e["id"] for e in first["items"]]
    event(conn, "task-move", ref="t-002")
    moved = hub.history(root, kind="progress")
    assert moved["total"] == 2 and len(moved["items"]) == 2
    assert hub.history(root, kind="all")["total"] == 6


def test_an_old_page_of_a_cursor_is_counted_at_its_own_watermark(record):
    from alpaca import hub
    root, conn = record
    hub._HISTORY_COUNTS.clear()
    for n in range(4):
        event(conn, "task-move", ref="t-%03d" % n)
    first = hub.history(root, kind="progress", limit=2)
    event(conn, "task-move", ref="t-009")
    assert hub.history(root, kind="progress")["total"] == 5
    older = hub.history(root, kind="progress", limit=2, before=first["next_before"])
    assert older["total"] == 4 == first["total"]
    assert hub.history(root, kind="progress")["total"] == 5


def test_a_rebuilt_record_does_not_serve_the_old_counts(tmp_path):
    from alpaca import hub
    totals = []
    for name, n in (("a", 3), ("b", 3)):
        root = tmp_path / name
        root.mkdir()
        conn = db.connect(str(root))
        for i in range(n):
            event(conn, "task-move", ref="t-%03d" % i, data={"to": name})
        conn.close()
        totals.append(hub.history(root, kind="progress")["total"])
    assert totals == [3, 3]
    # the same path rebuilt with other events of the same count
    root = tmp_path / "a"
    for suffix in ("", "-wal", "-shm"):
        Path(str(root / ".alpaca" / "alpaca.db") + suffix).unlink(missing_ok=True)
    conn = db.connect(str(root))
    event(conn, "heartbeat")
    event(conn, "heartbeat")
    event(conn, "heartbeat")
    conn.close()
    assert hub.history(root, kind="progress")["total"] == 0


def test_a_cursor_from_beyond_the_record_counts_what_is_there_and_keeps_the_count_warm(record):
    import base64
    from alpaca import hub
    root, conn = record
    hub._HISTORY_COUNTS.clear()
    for n in range(6):
        event(conn, "task-move", ref="t-%03d" % n)
    payload = json.dumps({"before": 10 ** 6 + 1, "upper": 10 ** 6}, separators=(",", ":"))
    far = hub.history(root, kind="all", before=base64.urlsafe_b64encode(payload.encode()).decode().rstrip("="))
    assert far["total"] == 6 and far["watermark"] == 6
    key = next(k for k in hub._HISTORY_COUNTS if k[1][0] == "all")
    assert hub._HISTORY_COUNTS[key][0] == 6 and hub._HISTORY_COUNTS[key][2] is not None
    event(conn, "task-move", ref="t-009")
    assert hub.history(root, kind="all")["total"] == 7 == _fresh(hub, root, kind="all")["total"]


def test_a_count_that_raises_leaves_no_lock_behind(record, monkeypatch):
    from alpaca import hub

    class Broken(dict):
        def get(self, *a):
            raise RuntimeError("boom")

    root, conn = record
    event(conn, "task-move", ref="t-001")
    monkeypatch.setattr(hub, "_HISTORY_COUNTS", Broken())
    hub._HISTORY_KEY_LOCKS.clear()
    with pytest.raises(RuntimeError):
        hub.history(root, kind="progress")
    assert not hub._HISTORY_KEY_LOCKS
