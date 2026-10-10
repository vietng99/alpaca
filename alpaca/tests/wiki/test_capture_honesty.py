"""op-008 t-065 stage s1-capture: capture honesty (FR-8; design/rune2-memory/architecture.md 9.2, 9.5;
OB-18, OA-3).

A transcript missing at hook time no longer drops the session's event notes. A marking-scanner trip
lands the document under wiki/private/ with a `capture-quarantined` record event, and capture goes
on. A deadline stops the capture between documents and the hook appends `capture-deferred`. The
Stop hook captures an open session's uncaptured event notes after its decision line is flushed.
`wiki status` lists the missing event ids. The hooks and the collector reach the drain only through
`service.capture`. Real drain, real write path; a stub appears only where a test says so.
"""
import io
import json
import os
import re
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import append, capture, events, vault  # noqa: E402

from alpaca import db as record                           # noqa: E402
from alpaca.wiki import service                           # noqa: E402
from alpaca.wiki.ingest import drain                      # noqa: E402

SECRET = "Handling NOFORN material: the vault door code is amber."


def _kinds(project, kind):
    return [e for e in events(project) if e["kind"] == kind]


def _data(event):
    return json.loads(event["data"]) if isinstance(event["data"], str) else event["data"]


def _captured(project):
    return service.status(project)["capture"]["captured_events"]


def _served(answer) -> str:
    """Everything an answer hands the caller except the echoed question."""
    return json.dumps({k: answer[k] for k in ("answer_text", "citations", "sources")})


def _register_missing_transcript(project, session, name="gone.jsonl"):
    conn = record.connect(str(project))
    try:
        record.upsert(conn, "sessions", "sid", {"sid": session, "started": "2026-09-01T00:00:00+00:00",
                                                "transcript": str(Path(project, name))})
    finally:
        conn.close()


# ------------------------------------------------------------------ missing transcript
def test_missing_transcript_keeps_the_event_notes(project):
    first = append(project, "result", {"body": "The lantern rota moved to Tuesday."}, session="s-miss")
    second = append(project, "result", {"body": "The lantern rota moved again."}, session="s-miss")
    _register_missing_transcript(project, "s-miss")
    report = drain.run(str(project), "s-miss")          # never raises on the missing transcript
    assert report["notes"] == 2 and report["transcripts"] == []
    assert [m["session"] for m in report["missing_transcripts"]] == ["s-miss"]
    assert report["missing_transcripts"][0]["source"].endswith("gone.jsonl")
    assert _captured(project) == 2
    held = {e["id"] for e in events(project)}
    assert {first, second} <= held
    answer = service.query(project, "lantern rota moved Tuesday")
    assert answer["outcome"] == "grounded"


def test_missing_transcript_through_service_capture_and_hooks_records_the_failure(project):
    from alpaca.hooks import pre_compact, session_end
    append(project, "result", {"body": "The lantern rota moved to Tuesday."}, session="s-hook")
    _register_missing_transcript(project, "s-hook")
    out = pre_compact.handle({"session_id": "s-hook", "cwd": str(project), "trigger": "auto"})
    assert out["drain"]["notes"] >= 1 and out["drain"]["missing_transcripts"]
    failed = _kinds(project, "drain-failed")
    assert len(failed) == 1 and _data(failed[0])["where"] == "pre_compact"
    assert "transcript unavailable: s-hook" in _data(failed[0])["error"]
    assert _captured(project) >= 1                       # the notes landed all the same
    session_end.handle({"session_id": "s-hook", "cwd": str(project), "reason": "exit"})
    assert [_data(e)["where"] for e in _kinds(project, "drain-failed")] == ["pre_compact", "session_end"]


def test_collector_keeps_event_notes_and_records_a_gap_when_the_transcript_is_absent(project):
    # Merge of op-008 into main (2026-10-06), decision D1: a registered transcript that is ABSENT
    # (neither the locator nor the retained copy exists) is an explicit gap, the policy the live
    # collector runs (main cea450f95): the consumer advances its checkpoint after committing a
    # `transcript-unavailable` issue and a snapshot retry. Before the merge this test asserted the
    # op-008 policy (status failed, checkpoint 0). The notes still land first (FR-8).
    from alpaca import observability
    from alpaca.observability import consumers
    conn = record.connect(str(project))
    path = Path(project, "absent.jsonl")
    observability.expect_source(str(project), "s-col", "claude", locator=str(path))
    event = record.append_event(conn, session="s-col", actor="test", kind="result",
                                data={"body": "The lantern rota moved to Tuesday."})
    result = consumers.consume(str(project), conn)
    assert result["wiki"] == {"status": "ok", "through_event": event["id"]}
    assert _captured(project) >= 1                       # the notes landed
    gaps = [dict(r) for r in conn.execute(
        "SELECT * FROM obs_issue WHERE code='transcript-unavailable' AND resolved_at IS NULL")]
    assert len(gaps) == 1 and "s-col" in gaps[0]["detail"] and str(path) not in gaps[0]["detail"]
    conn.close()


def test_collector_keeps_event_notes_and_the_checkpoint_when_the_transcript_is_malformed(project):
    # Merge decision D2: a transcript that is present but malformed (or unreadable, or a directory)
    # still fails the consumer so the batch retries; the event notes land first all the same (FR-8).
    from alpaca import observability
    from alpaca.observability import consumers
    conn = record.connect(str(project))
    path = Path(project, "broken.jsonl")
    path.write_text('{"type": "user"}\nnot json\n')
    observability.expect_source(str(project), "s-col", "claude", locator=str(path))
    record.append_event(conn, session="s-col", actor="test", kind="result",
                        data={"body": "The lantern rota moved to Tuesday."})
    result = consumers.consume(str(project), conn)
    assert result["wiki"]["status"] == "failed" and result["wiki"]["through_event"] == 0
    assert _captured(project) >= 1                       # notes landed; only the checkpoint waits
    assert not list(conn.execute("SELECT 1 FROM obs_issue WHERE code='transcript-unavailable'"))
    conn.close()


# ------------------------------------------------------------------ marking quarantine
def test_marking_trip_lands_under_private_with_a_record_event(project):
    clean = append(project, "result", {"body": "The lantern rota moved to Tuesday."}, session="s-q")
    marked = append(project, "result", {"body": SECRET}, session="s-q", op="op-q")
    report = drain.capture(str(project), events(project, {clean, marked}))
    assert len(report["quarantined"]) == 1
    entry = report["quarantined"][0]
    assert entry["doc_id"] == "wiki/private/raw/op-q/%06d-result.md" % marked
    assert (vault(project) / entry["doc_id"]).is_file()
    assert report["notes"] == 2                          # the trip did not abort the capture
    from native_helpers import store
    with store(project) as conn:
        row = conn.execute("SELECT private,privacy_scanned,path FROM docs WHERE doc_id=?",
                           (entry["doc_id"],)).fetchone()
        assert row["private"] == 1 and row["path"] == entry["doc_id"]
        clean_row = conn.execute("SELECT private FROM docs WHERE doc_id LIKE ?",
                                 ("raw/%%/%06d-result.md" % clean,)).fetchone()
        assert clean_row["private"] == 0
    recorded = _kinds(project, "capture-quarantined")
    assert len(recorded) == 1 and recorded[0]["actor"] == "alpaca-wiki"
    assert _data(recorded[0]) == {"doc_id": entry["doc_id"], "rule_id": entry["rule_id"]}
    assert entry["rule_id"] == "US-DISSEM"
    # the quarantined note is never served, and both events count as captured.
    answer = service.query(project, "vault door code amber")
    assert (answer["outcome"], answer["reason"]) == ("refused", "not-servable")
    assert "amber" not in _served(answer)
    assert _captured(project) == 2


def test_quarantine_is_recorded_once_per_document(project):
    marked = append(project, "result", {"body": SECRET}, session="s-q2", op="op-q")
    drain.capture(str(project), events(project, {marked}))
    again = drain.capture(str(project), events(project, {marked}))
    assert len(again["quarantined"]) == 1 and again["skipped"] == 1
    assert len(_kinds(project, "capture-quarantined")) == 1


def test_quarantined_transcript_lands_under_private_and_event_notes_stay_public(project):
    folder = Path(project, ".alpaca", "transcripts")
    folder.mkdir(parents=True)
    path = folder / "s-qt.jsonl"
    path.write_text(json.dumps({"type": "user", "timestamp": "2026-09-01T10:00:00Z",
                                "message": {"content": SECRET}}) + "\n", encoding="utf-8")
    eid = append(project, "result", {"body": "The lantern rota moved to Tuesday."}, session="s-qt")
    report = drain.run(str(project), "s-qt", transcript_path=str(path))
    assert len(report["transcripts"]) == 1
    assert report["transcripts"][0]["doc_id"].startswith("wiki/private/raw/transcripts/")
    assert [q["doc_id"] for q in report["quarantined"]] == [report["transcripts"][0]["doc_id"]]
    assert "amber" not in _served(service.query(project, "vault door code amber"))
    assert service.query(project, "lantern rota moved Tuesday")["outcome"] == "grounded"
    assert eid in {e["id"] for e in events(project)}


def test_recovery_replays_a_quarantined_note_without_failing(project):
    from alpaca.wiki import recovery
    append(project, "result", {"body": SECRET}, session="s-rq", op="op-q")
    append(project, "result", {"body": "The lantern rota moved to Tuesday."}, session="s-rq")
    report = recovery.run(str(project), session="s-rq")
    assert report["status"] == "ok", report
    assert len(_kinds(project, "capture-quarantined")) == 1


# ------------------------------------------------------------------ deadline
def test_deadline_records_capture_deferred(project):
    from alpaca.hooks import pre_compact
    for n in range(3):
        append(project, "result", {"body": "note %d of the lantern rota" % n}, session="s-dl")
    deferred = service.capture(project, sessions=["s-dl"], session_events=True, deadline_s=0)
    assert deferred["notes"] == 0
    assert deferred["deferred"] == {"through_event": 0, "remaining": 3}
    # a hook whose budget is spent appends the event, with the through_event it reached.
    out = _capture_through_hook(project, pre_compact, "s-dl")
    left = out["drain"]["deferred"]["remaining"]
    assert left >= 3                                      # the three notes and the pre-compact event
    rows = _kinds(project, "capture-deferred")
    assert len(rows) == 1
    assert _data(rows[0]) == {"hook": "pre_compact", "through_event": 0, "remaining": left}
    # nothing was lost: the next capture lands them all, and a finished capture records nothing.
    done = service.capture(project, sessions=["s-dl"], session_events=True, deadline_s=60)
    assert done["deferred"] is None and _captured(project) >= 3
    assert len(_kinds(project, "capture-deferred")) == 1


def _capture_through_hook(project, module, session):
    from alpaca.hooks import common
    real = common.capture_budget
    common.capture_budget = lambda *a, **k: 0
    try:
        return module.handle({"session_id": session, "cwd": str(project), "trigger": "auto"})
    finally:
        common.capture_budget = real


def test_deadline_cuts_between_documents_and_reports_the_last_event_landed(project, monkeypatch):
    ids = [append(project, "result", {"body": "note %d of the lantern rota" % n}, session="s-mid")
           for n in range(4)]
    ticks = iter(range(100))
    fake = type("Clock", (), {"monotonic": staticmethod(lambda: next(ticks))})
    monkeypatch.setattr(drain, "time", fake)
    report = drain.capture(str(project), events(project, set(ids)), deadline=2.5)
    assert report["notes"] == 3 and report["deferred"] == {"through_event": ids[2], "remaining": 1}
    assert _captured(project) == 3


def test_capture_budget_never_exceeds_its_cap_or_the_hook_window():
    from alpaca.hooks import common
    assert common.capture_budget(10.0) == 10.0           # outside a hook only the cap applies
    common._WINDOW["end"] = __import__("time").monotonic() + 3.0
    try:
        assert 0.5 <= common.capture_budget(10.0) <= 2.0
        assert common.capture_budget(1.0, share=0.5) <= 1.0
    finally:
        common._WINDOW.pop("end", None)


# ------------------------------------------------------------------ status: missing ids
def test_status_lists_missing_event_ids_below_the_highest_captured(project):
    ids = [append(project, "result", {"body": "event %d" % n}, session="s-st") for n in range(5)]
    drain.capture(str(project), events(project, {ids[0], ids[2], ids[3]}))
    report = service.status(project)["capture"]
    assert report["missing_event_ids"] == [ids[1]]
    assert report["missing_event_ids_below_head"] == 1
    assert report["missing_events"] == 2                 # ids[1] and the not-yet-captured tail
    drain.capture(str(project), events(project, {ids[1]}))
    assert service.status(project)["capture"]["missing_event_ids"] == []


# ------------------------------------------------------------------ the Stop hook (OB-18, OA-3)
def _stop_main(project, monkeypatch, payload, *, capture_target=None):
    """Run the real Stop `main` body (not the deadline wrapper) with a stdout buffer that records
    flushes, so a test can see what was on stdout when the capture started. `capture_target` names
    the work the capture runner's child runs in place of `service.capture` (alpaca/tests/runner_fakes.py)."""
    from alpaca import capture_runner
    from alpaca.hooks import common, stop
    out = io.StringIO()
    flushed = []
    real_flush = out.flush
    out.flush = lambda: (flushed.append(out.getvalue()), real_flush())[1]
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(common, "read_stdin", lambda *a, **k: payload)
    if capture_target is not None:
        monkeypatch.setattr(capture_runner, "CAPTURE", capture_target)
    stop.main.__wrapped__()
    return out.getvalue(), flushed


def test_open_session_events_captured_at_stop(project, monkeypatch):
    """Events appended in an open session are captured by one Stop hook run, before any SessionEnd,
    and the run captures no transcript."""
    from alpaca import cli
    cli.main(["init"])
    path = Path(project, ".alpaca", "transcripts", "s-open.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "user", "message": {"content": "TRANSCRIPT_ONLY_MARKER"}}) + "\n")
    conn = record.connect(str(project))
    record.upsert(conn, "sessions", "sid", {"sid": "s-open", "started": "2026-09-01T00:00:00+00:00",
                                            "transcript": str(path)})
    conn.close()
    ids = [append(project, "result", {"body": "open session note %d" % n}, session="s-open")
           for n in range(3)]
    assert _captured(project) == 0
    _stop_main(project, monkeypatch, {"session_id": "s-open", "cwd": str(project)})
    assert not _kinds(project, "session-end")
    held = service.status(project)
    assert held["capture"]["captured_events"] >= 4       # the three notes and the turn-end event
    assert held["corpus"]["transcript_documents"] == 0   # transcripts wait for SessionEnd
    assert {e["id"] for e in events(project)} >= set(ids)
    # a second Stop captures only what is new (the previous turn-end), each event once.
    _stop_main(project, monkeypatch, {"session_id": "s-open", "cwd": str(project)})
    notes = [p.name for p in vault(project).rglob("*.md") if "raw" in p.parts]
    assert len(notes) == len(set(notes))
    assert len(notes) == _captured(project) == len(events(project)) - 1   # all but the last turn-end


def test_stop_capture_starts_after_the_decision_line_is_flushed(project, monkeypatch):
    """The capture runner (t-070 RC4) is started only after the decision line is on stdout and
    flushed, with the Stop step's arguments: uncaptured events, no transcript, half the ceiling."""
    import time
    from alpaca import capture_runner, cli
    cli.main(["init"])
    Path(project, "style", "banned.txt").write_text("delve\n", encoding="utf-8")
    seen = {}

    def stub(target, kwargs, *, root, kill_s, progress=False):
        seen["stdout"] = sys.stdout.getvalue()
        seen["flushes"] = len(flushed_ref)
        seen["kwargs"] = kwargs
        seen["left"] = kwargs["deadline_at"] - time.monotonic()
        seen["target"] = target
        return capture_runner.Result("ok", value={"deferred": None, "missing_transcripts": []})

    flushed_ref = []
    from alpaca.hooks import common, stop
    out = io.StringIO()
    real_flush = out.flush
    out.flush = lambda: (flushed_ref.append(out.getvalue()), real_flush())[1]
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(common, "read_stdin", lambda *a, **k: {
        "session_id": "s-dec", "cwd": str(project), "reply": "We should delve in."})
    monkeypatch.setattr(capture_runner, "run_process", stub)
    stop.main.__wrapped__()
    decision = json.loads(seen["stdout"])
    assert decision["decision"] == "block" and seen["flushes"] == 1
    assert seen["target"] == capture_runner.CAPTURE == "alpaca.wiki.service:capture"
    assert seen["kwargs"]["transcripts"] is False and seen["kwargs"]["uncaptured_only"] is True
    assert 0 < seen["left"] <= 10.0


def test_stop_decision_output_is_the_same_with_capture_on_and_off(project, monkeypatch):
    from alpaca import cli
    from alpaca.hooks import stop
    cli.main(["init"])
    Path(project, "style", "banned.txt").write_text("delve\n", encoding="utf-8")
    payload = {"session_id": "s-same", "cwd": str(project), "reply": "We should delve in."}
    on, _ = _stop_main(project, monkeypatch, dict(payload))
    monkeypatch.setattr(stop, "capture_open_session", lambda p: None)
    off, _ = _stop_main(project, monkeypatch, dict(payload))
    assert json.loads(on) == json.loads(off) and json.loads(on)["decision"] == "block"


def test_stop_capture_failure_is_recorded_and_never_changes_the_output(project, monkeypatch):
    from alpaca import cli
    cli.main(["init"])
    out, _ = _stop_main(project, monkeypatch, {"session_id": "s-bad", "cwd": str(project)},
                        capture_target="alpaca.tests.runner_fakes:broken")
    assert out == ""
    failed = _kinds(project, "drain-failed")
    assert len(failed) == 1 and _data(failed[0])["where"] == "stop"
    assert "store exploded" in _data(failed[0])["error"]


def test_stop_deadline_records_capture_deferred(project, monkeypatch):
    from alpaca import cli
    from alpaca.hooks import common
    cli.main(["init"])
    monkeypatch.setattr(common, "capture_budget", lambda *a, **k: 0)
    _stop_main(project, monkeypatch, {"session_id": "s-late", "cwd": str(project)})
    rows = _kinds(project, "capture-deferred")
    assert len(rows) == 1 and _data(rows[0])["hook"] == "stop"
    assert _data(rows[0])["remaining"] >= 1


def test_session_stop_verb_runs_no_capture(project, monkeypatch):
    from alpaca import capture_runner, cli, operator
    cli.main(["init"])
    calls = []
    monkeypatch.setattr(service, "capture", lambda *a, **k: calls.append(k))
    monkeypatch.setattr(capture_runner, "run_process", lambda *a, **k: calls.append(k))
    operator.run("start", str(project), "s-verb", operator="codex")
    operator.run("stop", str(project), "s-verb", operator="codex")
    assert calls == []


def _open_store(project):
    """Create the project's store through the write door, as a first capture does. Merge of op-008
    into main (2026-10-06): these tests warmed up with `service.capture(project, deadline_s=60)`,
    which opened an absorber on the op-008 branch; with main's early return (`drain.capture`, nothing
    to land opens no absorber, main 3a2f32481/df6229ddf) it creates no store, so the concurrent
    captures below raced the store's creation (`database is locked` at `PRAGMA journal_mode=WAL`,
    `no such table: blocks`), which these tests say they do not claim."""
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    cfg = Config.for_project(project)
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)       # as `drain.capture` does first
    Absorber(cfg).close()
    assert vault(project).joinpath("rune.db").is_file()


def test_concurrent_stop_and_session_end_capture_each_event_once(project):
    from alpaca import cli
    cli.main(["init"])
    ids = [append(project, "result", {"body": "concurrent note %d" % n}, session="s-con")
           for n in range(12)]
    # The store already exists, as it does at the second capture of any project. First-time store
    # creation by several processes at once is a store-layer matter this test does not claim.
    _open_store(project)
    failures = []

    def stop_run():
        try:
            service.capture(project, sessions=["s-con"], session_events=True, uncaptured_only=True,
                            transcripts=False, deadline_s=60)
        except Exception as exc:                          # a lock timeout is a defect, not a skip
            failures.append(exc)

    def end_run():
        try:
            service.capture(project, sessions=["s-con"], session_events=True, deadline_s=60)
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=fn) for fn in (stop_run, stop_run, end_run)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    from native_helpers import store
    with store(project) as conn:
        for eid in ids:
            rows = conn.execute("SELECT doc_id FROM docs WHERE doc_id LIKE ?",
                                ("raw/%%/%06d-%%" % eid,)).fetchall()
            assert len(rows) == 1, (eid, [r[0] for r in rows])
            active = conn.execute("SELECT COUNT(*) FROM blocks WHERE doc_id=? AND status='active'",
                                  (rows[0][0],)).fetchone()[0]
            assert active == 2                            # header block and summary block, once


def test_concurrent_processes_capture_each_event_once(project):
    """The hook processes of OB-18 are separate processes: two Stops and a SessionEnd at once."""
    import subprocess
    ids = [append(project, "result", {"body": "process note %d" % n}, session="s-proc")
           for n in range(30)]
    _open_store(project)                              # as above: the store already exists
    code = ("import sys; from alpaca.wiki import service; "
            "service.capture(sys.argv[1], sessions=['s-proc'], session_events=True, "
            "uncaptured_only=sys.argv[2] == '1', transcripts=False, deadline_s=60)")
    env = {**os.environ, "PYTHONPATH": str(Path(drain.__file__).resolve().parents[3])}
    procs = [subprocess.Popen([sys.executable, "-c", code, str(project), flag], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for flag in ("1", "1", "0")]
    for proc in procs:
        _, err = proc.communicate()
        assert proc.returncode == 0, err[-400:]
    report = service.status(project)["capture"]
    assert report["captured_events"] == report["source_events"] == len(ids)
    from native_helpers import store
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM docs WHERE kind='raw'").fetchone()[0] == len(ids)


# ------------------------------------------------------------------ one door
def test_hooks_and_collector_reach_the_drain_only_through_service_capture():
    """The hooks capture only through the one capture runner (`common.capture_step`, t-070 RC4),
    whose work is `service.capture`; the collector calls `service.capture` on its lane. None of them
    imports the drain."""
    from alpaca import capture_runner
    root = Path(drain.__file__).resolve().parents[2]
    sources = {name: (root / name).read_text(encoding="utf-8") for name in (
        "hooks/session_end.py", "hooks/pre_compact.py", "hooks/stop.py", "observability/consumers.py",
        "hooks/common.py", "capture_runner.py")}
    for name, text in sources.items():
        assert not re.search(r"drain\.(run|capture)\(", text), name
        assert "wiki.ingest import drain" not in text and "wiki.ingest.drain" not in text, name
    for name in ("hooks/session_end.py", "hooks/pre_compact.py", "hooks/stop.py"):
        assert "common.capture_step(" in sources[name], name
        assert "service.capture(" not in sources[name], name
    assert "capture_runner.run_process(capture_runner.CAPTURE" in sources["hooks/common.py"]
    assert capture_runner.CAPTURE == "alpaca.wiki.service:capture"
    assert "service.capture(" in sources["observability/consumers.py"]
    assert "alpaca.wiki.ingest" not in sources["hooks/common.py"]
