"""op-008 t-065 stage s5-keepup: capture keeps up at SessionEnd and PreCompact (FR-8; OB-18).

A hook after a normal session (the Stop hook captured the turns as they ended) lands the events it
has not captured and then the transcript, inside the hook's deadline, and records `capture-deferred`
only when the budget truly runs out. It renders nothing it landed before. The Absorber reads the
echo index of the store on its first use, not at start, so a capture whose documents all pass the
hashgate pays no load, and the lazy index gives the answers the eager one gave. Real drain, real
write path, no mock beyond the spies a test names.

These tests fake the drain's clock and spy on its documents in this process, so the hooks' capture
runs in this process too (`runner_fakes.run_inline`, the runner's contract without the child
process); the child process and its deadline are tested in alpaca/tests/test_capture_runner.py.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import append, events, store  # noqa: E402

from alpaca import db as record                           # noqa: E402
from alpaca.hooks import common, pre_compact, session_end  # noqa: E402
from alpaca.wiki import service                           # noqa: E402
from alpaca.wiki.config import Config                     # noqa: E402
from alpaca.wiki.ingest import absorb, drain              # noqa: E402
from alpaca.wiki.engine.echo import EchoIndex             # noqa: E402

@pytest.fixture(autouse=True)
def _capture_in_this_process(monkeypatch):
    from alpaca import capture_runner
    from alpaca.tests import runner_fakes
    monkeypatch.setattr(capture_runner, "run_process", runner_fakes.run_inline)


SESSION = "s-keep"
SESSION_EVENTS = 300        # a few hundred events, as a long working session leaves
RESIDUAL = 6                # events after the last Stop capture


def _kinds(project, kind, session=None):
    return [e for e in events(project) if e["kind"] == kind and (session is None or e["session"] == session)]


def _register_transcript(project, session=SESSION, messages=120):
    path = Path(project, ".alpaca", "transcripts", session + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for n in range(messages):
        role = "user" if n % 2 == 0 else "assistant"
        lines.append(json.dumps({"type": role, "timestamp": "2026-09-01T10:%02d:00Z" % (n % 60),
                                 "message": {"role": role,
                                             "content": "message %d about the lantern rota and the ember queue" % n}}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    conn = record.connect(str(project))
    try:
        record.upsert(conn, "sessions", "sid", {"sid": session, "started": "2026-09-01T00:00:00+00:00",
                                                "transcript": str(path)})
    finally:
        conn.close()
    return path


def _warm_session(project, total=SESSION_EVENTS, residual=RESIDUAL):
    """`total` events of one session, all but the last `residual` captured the way the Stop hook
    captures them (uncaptured events only, no transcript); the transcript is registered."""
    from alpaca import cli
    cli.main(["init"])
    _register_transcript(project)
    first = [append(project, "result", {"body": "turn %d of the lantern rota" % n}, session=SESSION)
             for n in range(total - residual)]
    service.capture(project, sessions=[SESSION], session_events=True, uncaptured_only=True,
                    transcripts=False, deadline_s=None)
    rest = [append(project, "result", {"body": "late turn %d of the lantern rota" % n}, session=SESSION)
            for n in range(residual)]
    return first, rest


def _absorbed_docs(monkeypatch):
    """Spy on the write door: the doc ids each absorb call receives, in order."""
    seen = []
    real = absorb.Absorber.absorb_text

    def spy(self, doc_id, text, **kwargs):
        seen.append(doc_id)
        return real(self, doc_id, text, **kwargs)
    monkeypatch.setattr(absorb.Absorber, "absorb_text", spy)
    return seen


def _held_events(project):
    return service._store_scan(Config.for_project(str(project)))["captured"]


def _transcript_docs(project):
    with store(project) as conn:
        return conn.execute("SELECT COUNT(*) FROM docs WHERE doc_id LIKE 'raw/transcripts/%'").fetchone()[0]


def _slow_clock(monkeypatch, step=1.0):
    """Make the drain's clock advance `step` seconds per reading (one reading per document), so a
    budget of N seconds admits about N / step documents, whatever the machine's speed."""
    ticks = iter(range(10 ** 6))
    real = time.monotonic
    monkeypatch.setattr(drain, "time", type("Clock", (), {
        "monotonic": staticmethod(lambda: real() + next(ticks) * step)}))


def _in_hook_window(fn):
    """Run `fn` the way `fail_open` runs a hook: with the hard deadline's window set, so
    `common.capture_budget` is the budget the real hook gets. Returns (result, seconds)."""
    _t0 = time.monotonic()
    common._WINDOW["end"] = _t0 + common.HOOK_DEADLINE_S
    try:
        return fn(), time.monotonic() - _t0
    finally:
        common._WINDOW.pop("end", None)


# ------------------------------------------------------------------ the hook paths keep up
@pytest.mark.parametrize("module, name", [(session_end, "session_end"), (pre_compact, "pre_compact")])
def test_hook_after_a_normal_session_lands_every_residual_event_and_the_transcript_in_its_deadline(
        project, monkeypatch, module, name):
    first, rest = _warm_session(project)
    assert len(_held_events(project)) == SESSION_EVENTS - RESIDUAL
    assert _transcript_docs(project) == 0
    seen = _absorbed_docs(monkeypatch)

    result, seconds = _in_hook_window(lambda: module.handle(
        {"session_id": SESSION, "cwd": str(project), "reason": "exit", "trigger": "auto"}))

    assert seconds < common.HOOK_DEADLINE_S                 # inside the hook's hard deadline
    assert _kinds(project, "capture-deferred") == [] and _kinds(project, "drain-failed") == []
    held = _held_events(project)
    assert set(rest) <= held                                # every residual event note landed
    assert {e["id"] for e in events(project) if e["session"] == SESSION} <= held   # the hook's own too
    assert _transcript_docs(project) == 1                   # and the transcript after them
    # nothing of the 294 captured events was rendered again; the transcript came last.
    assert len(seen) <= RESIDUAL + 3 and "/transcripts/" in seen[-1]
    assert not any("/transcripts/" in d for d in seen[:-1])
    if name == "pre_compact":
        assert result["drain"]["deferred"] is None


def test_hook_capture_of_a_session_the_stop_hook_never_saw_defers_only_when_the_budget_runs_out(
        project, monkeypatch):
    """A cold session (no Stop capture) of a few hundred events: the hook lands what its budget
    allows, events first, records `capture-deferred` with the remaining count, and a later capture
    finishes the rest. Nothing is lost and nothing is recorded twice."""
    from alpaca import cli
    cli.main(["init"])
    _register_transcript(project)
    ids = [append(project, "result", {"body": "cold turn %d of the ember queue" % n}, session=SESSION)
           for n in range(SESSION_EVENTS)]
    _slow_clock(monkeypatch)                                 # a budget of 50.5 s admits 51 documents
    monkeypatch.setattr(common, "capture_budget", lambda *a, **k: 50.5)
    out = session_end.handle({"session_id": SESSION, "cwd": str(project), "reason": "exit"})
    assert out["ended"]
    monkeypatch.undo()
    deferred = _kinds(project, "capture-deferred")
    assert len(deferred) == 1
    data = json.loads(deferred[0]["data"]) if isinstance(deferred[0]["data"], str) else deferred[0]["data"]
    held = _held_events(project)
    assert 0 < len(held) < SESSION_EVENTS
    assert data["hook"] == "session_end" and data["remaining"] > 0
    assert data["through_event"] == max(held)               # the notes landed in event order
    assert _transcript_docs(project) == 0                   # events before the transcript
    # the next hook (a PreCompact of the same session) picks up exactly the rest.
    done = pre_compact.handle({"session_id": SESSION, "cwd": str(project), "trigger": "auto"})
    assert done["drain"]["deferred"] is None
    assert set(ids) <= _held_events(project) and _transcript_docs(project) == 1
    assert len(_kinds(project, "capture-deferred")) == 1


def test_a_hook_whose_events_are_all_captured_renders_only_the_transcript(project, monkeypatch):
    from alpaca import cli
    cli.main(["init"])
    _register_transcript(project)
    for n in range(12):
        append(project, "result", {"body": "settled turn %d of the lantern rota" % n}, session=SESSION)
    service.capture(project, sessions=[SESSION], session_events=True, deadline_s=None)
    seen = _absorbed_docs(monkeypatch)
    report = service.capture(project, sessions=[SESSION], session_events=True, uncaptured_only=True,
                             deadline_s=None)
    # no event note again; the transcript is read and passes the hashgate as unchanged.
    assert len(seen) == 1 and "/transcripts/" in seen[0]
    assert (report["ingested"], report["skipped"]) == (0, 1)


def test_the_source_of_a_captured_event_lands_through_uncaptured_only_without_its_note(project, monkeypatch):
    """A proof event whose note landed but whose source did not (the proof file was not there yet)
    is not done: the hook path lands the source alone and leaves the note as it was."""
    import hashlib
    from alpaca import cli
    cli.main(["init"])
    text = "# Proof\n\n## At a glance\n\nThe cinder lock stayed held overnight.\n"
    rel = ".alpaca/proofs/op-1/t-1.md"
    eid = append(project, "proof-report",
                 {"path": rel, "sha256": hashlib.sha256(text.encode()).hexdigest(), "bytes": len(text)},
                 session=SESSION, op="op-1", ref="t-1")
    first = service.capture(project, sessions=[SESSION], session_events=True, uncaptured_only=True,
                            transcripts=False, deadline_s=None)
    assert [g["reason"] for g in first["source_gaps"]] == ["proof-file-missing"]
    assert eid in _held_events(project)
    Path(project, rel).parent.mkdir(parents=True)
    Path(project, rel).write_text(text, encoding="utf-8")
    seen = _absorbed_docs(monkeypatch)
    second = service.capture(project, sessions=[SESSION], session_events=True, uncaptured_only=True,
                             transcripts=False, deadline_s=None)
    assert second["source_gaps"] == [] and second["notes"] == 1
    assert [d.split("/")[1] for d in seen] == ["sources"]    # the source only, no note again
    third = service.capture(project, sessions=[SESSION], session_events=True, uncaptured_only=True,
                            transcripts=False, deadline_s=None)
    assert third["notes"] == 0                               # done: note and source are held


def test_uncaptured_first_then_transcript_when_the_budget_stops_between_documents(project, monkeypatch):
    from alpaca import cli
    cli.main(["init"])
    _register_transcript(project)
    ids = [append(project, "result", {"body": "ordered turn %d of the lantern rota" % n}, session=SESSION)
           for n in range(4)]
    ticks = iter(range(100))
    monkeypatch.setattr(drain, "time", type("Clock", (), {"monotonic": staticmethod(lambda: next(ticks))}))
    # the drain reads the clock once per document: 0, 1, 2, 3 land the four notes, 4 passes 3.5.
    report = drain.capture(str(project), events(project, set(ids)), sessions=[SESSION], deadline=3.5)
    assert report["notes"] == 4 and report["transcripts"] == []
    assert report["deferred"] == {"through_event": ids[-1], "remaining": 1}   # the transcript waits


# ------------------------------------------------------------------ idempotence under concurrency (OB-18)
def test_concurrent_stop_session_end_and_pre_compact_capture_each_event_once(project):
    from alpaca import cli
    from alpaca.hooks import stop
    cli.main(["init"])
    _register_transcript(project, "s-conc", messages=20)
    ids = [append(project, "result", {"body": "concurrent hook note %d" % n}, session="s-conc")
           for n in range(40)]
    service.capture(project, deadline_s=60)                  # the store exists, as at any later capture
    failures = []

    def run(fn):
        try:
            fn()
        except Exception as exc:                             # a lock timeout is a defect, not a skip
            failures.append(exc)
    payload = {"session_id": "s-conc", "cwd": str(project), "reason": "exit", "trigger": "auto"}
    threads = [threading.Thread(target=run, args=(fn,)) for fn in (
        lambda: stop.capture_open_session(dict(payload, _strict=True)),
        lambda: session_end.handle(dict(payload, _strict=True)),
        lambda: pre_compact.handle(dict(payload, _strict=True)))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    with store(project) as conn:
        for eid in ids:
            rows = conn.execute("SELECT doc_id FROM docs WHERE doc_id LIKE ?",
                                ("raw/%%/%06d-%%" % eid,)).fetchall()
            assert len(rows) == 1, (eid, [r[0] for r in rows])
            assert conn.execute("SELECT COUNT(*) FROM blocks WHERE doc_id=? AND status='active'",
                                (rows[0][0],)).fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM docs WHERE doc_id LIKE 'raw/transcripts/%'"
                            ).fetchone()[0] == 1


# ------------------------------------------------------------------ the Absorber's echo index
def _count_loads(monkeypatch):
    loads = []
    real = EchoIndex.load

    def spy(self, rows):
        loads.append(1)
        return real(self, rows)
    monkeypatch.setattr(EchoIndex, "load", spy)
    return loads


def _config(tmp_path):
    cfg = Config.for_vault(tmp_path / "vault")
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def test_absorber_reads_the_echo_index_on_first_use_not_at_start(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    text = "op: o\nrole: note\nkey: \nkind: result\nevent: 1\nsession: s\nts: 2026-01-01T00:00:00Z\n\nThe lantern rota moved.\n"
    first = absorb.Absorber(cfg)
    first.absorb_text("raw/o/000001-note.md", text, kind="raw")
    first.close()
    loads = _count_loads(monkeypatch)
    second = absorb.Absorber(cfg)
    try:
        assert loads == []                                   # start reads nothing
        second.absorb_text("raw/o/000001-note.md", text, kind="raw")    # hashgate: unchanged
        assert loads == []                                   # an unchanged document needs no index
        second.absorb_text("raw/o/000002-note.md", text.replace("event: 1", "event: 2"), kind="raw")
        assert loads == [1]                                  # a new block reads it once
        second.absorb_text("raw/o/000003-note.md", text.replace("event: 1", "event: 3"), kind="raw")
        assert loads == [1]
    finally:
        second.close()


def test_lazy_echo_index_gives_the_answers_of_the_eager_one(tmp_path):
    cfg = _config(tmp_path)
    docs = {"raw/a.md": "The ember queue stalled because the cinder lock stayed held overnight.",
            "raw/b.md": "Ledger shipments arrive on Tuesday and are counted twice.",
            "raw/c.md": "As [[Orrin]] noted, the lantern rota moved to Friday."}
    seed = absorb.Absorber(cfg)
    for doc_id, text in docs.items():
        seed.absorb_text(doc_id, text, kind="wiki")
    seed.close()
    eager = EchoIndex()
    lazy = absorb.Absorber(cfg)
    try:
        for row in lazy.db.conn.execute("SELECT block_id,doc_id,text FROM blocks WHERE status='active' "
                                        "ORDER BY block_id"):
            eager.add(row["block_id"], row["text"], row["doc_id"])
        for probe in ("The ember queue stalled because the cinder lock stayed held overnight.",
                      "Ledger shipments arrive on Tuesday and are counted twice today.",
                      "The cinder lock stayed held overnight and the ember queue stalled again.",
                      "As [[Orrin]] noted, nothing else changed.", "A sentence nobody wrote before."):
            for exclude in (None, "raw/a.md"):
                assert lazy.echo_index.detect(probe, exclude_doc_id=exclude) == eager.detect(
                    probe, exclude_doc_id=exclude)
        assert lazy.echo_index._postings == eager._postings and lazy.echo_index._exact == eager._exact
    finally:
        lazy.close()


@pytest.mark.parametrize("load_first", [False, True])
def test_a_document_retired_before_the_first_load_is_not_an_echo_source(tmp_path, load_first):
    cfg = _config(tmp_path)
    claim = "The ember queue stalled because the cinder lock stayed held overnight."
    seed = absorb.Absorber(cfg)
    seed.absorb_text("raw/a.md", claim, kind="wiki")
    seed.close()
    a = absorb.Absorber(cfg)
    try:
        if load_first:
            assert a.echo_index.detect(claim, exclude_doc_id="raw/other.md")[0] == "quotes"
        a.retire_documents(["raw/a.md"], reason="source-projection-replaced")
        assert a.echo_index.detect(claim, exclude_doc_id="raw/other.md") == ("original", None, None)
    finally:
        a.close()


# ------------------------------------------------------------------ the earlier-projection lookup
def test_narrow_projection_lookup_matches_the_full_scan_for_the_batch_identities(project, monkeypatch):
    """`drain.capture` asks the store only for the sources of the documents it is about to land
    (a batch of at most NARROW_SCAN_LIMIT); the entries it needs are those of a scan of every raw
    document, including `event: 7` beside `event: 71`, a source and a transcript."""
    import hashlib
    from alpaca import cli
    from alpaca.wiki.config import Config
    from alpaca.wiki.store.db import open_db_readonly
    cli.main(["init"])
    _register_transcript(project, messages=6)
    text = "# Proof\n\nThe ember queue stalled.\n"
    rel = ".alpaca/proofs/op-1/t-1.md"
    Path(project, rel).parent.mkdir(parents=True)
    Path(project, rel).write_text(text, encoding="utf-8")
    append(project, "proof-report", {"path": rel, "sha256": hashlib.sha256(text.encode()).hexdigest()},
           session=SESSION, op="op-1", ref="t-1")
    for n in range(80):                                      # event ids 7 and 71 both exist
        append(project, "result", {"body": "note %d of the lantern rota" % n}, session=SESSION)
    service.capture(project, sessions=[SESSION], session_events=True, deadline_s=None)
    from alpaca.wiki.ingest import alpaca_sources
    source_conn = record.connect_readonly(str(project))
    try:
        wanted = drain.normalize_events(source_conn, [e for e in events(project)
                                                      if e["id"] in (7, 71) or e["kind"] == "proof-report"])
    finally:
        source_conn.close()
    texts = []
    for e in wanted:
        texts.append(drain._note_doc(e)[1])
        source = alpaca_sources.render(str(project), e)
        if isinstance(source, alpaca_sources.Rendered):
            texts.append(source.text)
    conn = open_db_readonly(Config.for_project(str(project)))
    try:
        narrow = drain._previous_projections(conn, texts, [SESSION])
        monkeypatch.setattr(drain, "NARROW_SCAN_LIMIT", 0)
        full = drain._previous_projections(conn, texts, [SESSION])
    finally:
        conn.close()
    identities = {drain.projection_identity(t) for t in texts} | {"transcript:" + SESSION}
    assert len(identities) >= 4
    for identity in identities:
        assert sorted(narrow.get(identity, ())) == sorted(full.get(identity, ())), identity
        assert full.get(identity), identity                  # each one is held, so the check is not empty
    assert set(narrow) <= set(full) and len(narrow) < len(full)   # the narrow lookup read less


# ------------------------------------------------------------------ the hard deadline (FR-8)
def _jumping_clock(monkeypatch, step=1.0, calls_before=0):
    """The Absorber's clock, advancing `step` per reading after `calls_before` readings."""
    state = {"n": 0}
    real = time.monotonic

    def monotonic():
        state["n"] += 1
        return real() + max(0, state["n"] - calls_before) * step
    monkeypatch.setattr(absorb, "time", type("Clock", (), {"monotonic": staticmethod(monotonic)}))


def test_absorb_past_its_abort_time_rolls_the_document_back_between_blocks(tmp_path):
    cfg = _config(tmp_path)
    text = "\n\n".join("Paragraph %d about the ember queue and the cinder lock." % n for n in range(6))
    a = absorb.Absorber(cfg)
    try:
        a.abort_at = time.monotonic() - 1.0
        with pytest.raises(absorb.DeadlineExceeded):
            a.absorb_text("raw/long.md", text, kind="wiki")
        assert a.db.conn.execute("SELECT COUNT(*) FROM blocks WHERE doc_id='raw/long.md'").fetchone()[0] == 0
        assert a.db.conn.execute("SELECT COUNT(*) FROM docs WHERE doc_id='raw/long.md'").fetchone()[0] == 0
        a.abort_at = None                                    # the same document lands once the limit is gone
        landed = a.absorb_text("raw/long.md", text, kind="wiki")
        assert landed["blocks"] >= 1 and not landed.get("skipped")
    finally:
        a.close()


def test_a_transcript_reaching_the_hard_deadline_is_deferred_whole_and_lands_later(project, monkeypatch):
    from alpaca import cli
    cli.main(["init"])
    _register_transcript(project, messages=40)
    ids = [append(project, "result", {"body": "short note %d" % n}, session=SESSION) for n in range(2)]
    # two notes read the clock twice each, and the first once more before it reads the echo index
    # outside the write lock (merge of op-008 into main, 2026-10-06, review B3: `absorb_text` checks
    # the abort time before that read, as its block loop did before reading the index under the
    # lock); the next reading, inside the transcript, is past the limit.
    _jumping_clock(monkeypatch, step=100.0, calls_before=5)
    report = service.capture(project, sessions=[SESSION], session_events=True, deadline_s=None, hard_s=50.0)
    monkeypatch.undo()
    assert report["notes"] == 2 and report["transcripts"] == []
    assert report["deferred"] == {"through_event": max(ids), "remaining": 1}
    assert _transcript_docs(project) == 0                    # nothing of it landed
    with store(project) as conn:
        assert conn.execute("SELECT COUNT(*) FROM blocks b JOIN docs d ON d.doc_id=b.doc_id "
                            "WHERE d.doc_id LIKE 'raw/transcripts/%'").fetchone()[0] == 0
    later = service.capture(project, sessions=[SESSION], session_events=True, uncaptured_only=True,
                            deadline_s=None)
    assert later["deferred"] is None and _transcript_docs(project) == 1


def test_a_hook_cut_by_its_hard_deadline_records_capture_deferred(project, monkeypatch):
    from alpaca import cli
    cli.main(["init"])
    _register_transcript(project, messages=10)
    for n in range(30):
        append(project, "result", {"body": "hard turn %d of the lantern rota" % n}, session=SESSION)
    _jumping_clock(monkeypatch, step=1.0)                    # one second per block the Absorber starts
    common._WINDOW["end"] = time.monotonic() + 1.5 + 6.0      # `hook_time_left` is 6 s
    try:
        session_end.handle({"session_id": SESSION, "cwd": str(project), "reason": "exit"})
    finally:
        common._WINDOW.pop("end", None)
        monkeypatch.undo()
    deferred = _kinds(project, "capture-deferred")
    assert len(deferred) == 1
    data = json.loads(deferred[0]["data"]) if isinstance(deferred[0]["data"], str) else deferred[0]["data"]
    held = _held_events(project)
    assert data["hook"] == "session_end" and data["remaining"] > 0
    assert 0 < len(held) < 30 and data["through_event"] == max(held)
    assert _transcript_docs(project) == 0
    assert _kinds(project, "drain-failed") == []


def test_a_transcript_that_cannot_land_before_the_cut_is_deferred_without_being_rendered(project, monkeypatch):
    from alpaca import cli
    cli.main(["init"])
    _register_transcript(project, messages=40)
    ids = [append(project, "result", {"body": "estimate note %d" % n}, session=SESSION) for n in range(3)]
    rendered = []
    real = drain._transcript_doc
    monkeypatch.setattr(drain, "_transcript_doc", lambda *a, **k: (rendered.append(a), real(*a, **k))[1])
    # the notes land inside 0.9 s; the transcript's estimated cost (TRANSCRIPT_BASE_S) passes the cut.
    report = service.capture(project, sessions=[SESSION], session_events=True, deadline_s=None,
                             hard_s=drain.TRANSCRIPT_BASE_S - 0.5)
    assert report["notes"] == 3 and rendered == []
    assert report["deferred"] == {"through_event": max(ids), "remaining": 1}
    # a hook with room for it lands the transcript, and a native file ten times larger costs more.
    assert drain.transcript_cost_s(__file__) > drain.TRANSCRIPT_BASE_S
    assert drain.transcript_cost_s("/no/such/file") == drain.TRANSCRIPT_BASE_S
    done = service.capture(project, sessions=[SESSION], session_events=True, uncaptured_only=True,
                           deadline_s=None, hard_s=drain.TRANSCRIPT_BASE_S + 30.0)
    assert done["deferred"] is None and len(rendered) == 1 and _transcript_docs(project) == 1
