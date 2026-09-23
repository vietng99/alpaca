"""Behavioral coverage for the project-contained operations hub read models."""
import json
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
