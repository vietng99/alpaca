"""op-008 t-069 stage s1-capture: Codex capture through the one drain (FR-7; OA-4; contract done bar 3;
design/rune2-memory/implementation-plan.md t-069).

Codex runs no hooks: its lifecycle is the explicit operator verbs (alpaca/operator.py,
docs/operators.md). `session checkpoint` records its `session-checkpoint` event and the transcript
snapshot, then runs the Stop hook's capture step (`stop.capture_open_session`: the open session's
uncaptured event notes, no transcript), so an open Codex session is captured as an open Claude
session is at each Stop (OA-4); `session end` lands the rest and the transcript; `session stop`
captures nothing (OB-18). Every capture goes through `service.capture` and the one drain, so a Codex
session and a Claude session land in one store, each event once, each note naming its operator and
session. Real record, real drain, real store; a spy only counts calls and passes them on.
"""
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import append, events, sockets_blocked, store, vault  # noqa: E402

from alpaca import db as record                           # noqa: E402
from alpaca import operator                               # noqa: E402
from alpaca.wiki import service                           # noqa: E402
from alpaca.wiki.ingest import drain                      # noqa: E402


@pytest.fixture
def calls(monkeypatch):
    """Every `service.capture` and `drain.capture` call, passed on to the real function. The capture
    runner runs the work in this process (`runner_fakes.run_inline`), so the spies see the calls the hooks and
    verbs make (the child process is tested in alpaca/tests/test_capture_runner.py)."""
    from alpaca import capture_runner
    from alpaca.tests import runner_fakes
    monkeypatch.setattr(capture_runner, "run_process", runner_fakes.run_inline)
    seen = {"service": [], "drain": []}
    real_service, real_drain = service.capture, drain.capture

    def service_spy(root, **kwargs):
        seen["service"].append(kwargs)
        return real_service(root, **kwargs)

    def drain_spy(root, events_, **kwargs):
        seen["drain"].append(kwargs)
        return real_drain(root, events_, **kwargs)

    monkeypatch.setattr(service, "capture", service_spy)
    monkeypatch.setattr(drain, "capture", drain_spy)
    return seen


def _codex_transcript(path: Path, native: str, cwd: str, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "session_meta", "timestamp": "2026-10-04T08:00:00Z",
             "payload": {"id": native, "cwd": cwd}},
            {"type": "response_item", "timestamp": "2026-10-04T08:00:01Z",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": text}]}},
            {"type": "response_item", "timestamp": "2026-10-04T08:00:02Z",
             "payload": {"type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "text": "Done: " + text}]}}]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _notes_of(project, event_ids) -> dict:
    """event id -> [doc_id] of the event-note documents the store holds for it."""
    with store(project) as conn:
        return {eid: [r[0] for r in conn.execute(
            "SELECT doc_id FROM docs WHERE doc_id LIKE ? AND doc_id NOT LIKE 'raw/sources/%'",
            ("%%raw/%%/%06d-%%" % eid,))] for eid in event_ids}


def _versions(project, event_ids) -> dict:
    """event id -> how many versions the store recorded of its note (one: never landed twice in
    different bytes)."""
    with store(project) as conn:
        return {eid: conn.execute("SELECT COUNT(*) FROM source_versions WHERE source_ref=?",
                                  ("event:%d" % eid,)).fetchone()[0] for eid in event_ids}


def _active_transcripts(project, session) -> list:
    with store(project) as conn:
        return sorted({r[0] for r in conn.execute(
            "SELECT b.doc_id FROM blocks b WHERE b.status='active' AND b.ordinal=0 "
            "AND b.doc_id LIKE 'raw/transcripts/%' AND instr(b.text, ?) > 0", ("session: " + session,))})


def _session_events(project, session) -> list:
    return [e for e in events(project) if e["session"] == session]


# ------------------------------------------------------------------ OA-4
def test_checkpoint_captures_an_open_codex_session_with_the_stop_step(project, calls):
    """The checkpoint verb runs the Stop hook's capture step: the open session's uncaptured event
    notes land, the transcript does not (it waits for `session end`), the transcript snapshot is
    still recorded, and the session stays open."""
    tp = _codex_transcript(Path(project, "codex-open.jsonl"), "n-open", project,
                           "TRANSCRIPT_ONLY_MARKER lantern rota")
    operator.run("start", project, "codex-open", operator="codex", transcript=str(tp))
    ids = [append(project, "result", {"body": "The lantern rota moved to Tuesday %d." % n},
                  session="codex-open", op="op-cx", ref="t-cx") for n in range(3)]
    calls["service"].clear()
    out = operator.run("checkpoint", project, "codex-open", note="Next: check the lantern rota")
    (step,) = calls["service"]
    assert step["sessions"] == ["codex-open"] and step["session_events"] is True
    assert step["uncaptured_only"] is True and step["transcripts"] is False
    assert 0 < step["deadline_s"] <= 10.0
    assert out["drain"]["deferred"] is None and out["drain"]["transcripts"] == []
    held = _notes_of(project, ids)
    assert all(len(docs) == 1 for docs in held.values()), held
    kinds = [e["kind"] for e in _session_events(project, "codex-open")]
    assert "session-checkpoint" in kinds and "transcript-snapshot" in kinds and "session-end" not in kinds
    checkpoint = next(e for e in _session_events(project, "codex-open") if e["kind"] == "session-checkpoint")
    assert _notes_of(project, [checkpoint["id"]])[checkpoint["id"]]      # its note lands too
    status = service.status(project)
    assert status["corpus"]["transcript_documents"] == 0
    assert Path(project, ".alpaca", "transcripts", "codex-open.jsonl").is_file()    # the snapshot
    conn = record.connect(str(project))
    try:
        assert not record.rows(conn, "sessions", "sid=?", ("codex-open",))[0]["ended"]
    finally:
        conn.close()


def test_checkpoint_stop_and_end_capture_each_event_once(project, calls):
    """Two checkpoints, a `session stop` and a `session end` of one Codex session: every source
    event is captured once, `stop` adds no capture, the transcript lands once at `end`, and the
    drain is reached only through `service.capture`. The whole lifecycle runs with socket creation
    blocked: no capture step opens a network connection (K1)."""
    tp = _codex_transcript(Path(project, "codex-once.jsonl"), "n-once", project, "the quay crane log")
    with sockets_blocked():
        operator.run("start", project, "codex-once", operator="codex", transcript=str(tp))
        first = [append(project, "result", {"body": "crane note %d" % n}, session="codex-once")
                 for n in range(4)]
        operator.run("checkpoint", project, "codex-once")
        second = [append(project, "result", {"body": "crane note %d" % n}, session="codex-once")
                  for n in range(4, 7)]
        operator.run("checkpoint", project, "codex-once")
        before_stop = len(calls["service"])
        operator.run("stop", project, "codex-once")
        assert len(calls["service"]) == before_stop                   # session stop: no capture
        operator.run("end", project, "codex-once", reason="complete")
    assert len(calls["drain"]) == len(calls["service"])               # one drain, one way in
    sources = [e["id"] for e in _session_events(project, "codex-once")
               if drain.renders_as_source(e["kind"], json.loads(e["data"]))]
    held = _notes_of(project, sources)
    assert all(len(docs) == 1 for docs in held.values()), held
    assert set(first + second) <= set(held)
    assert set(_versions(project, sources).values()) == {1}
    assert len(_active_transcripts(project, "codex-once")) == 1
    capture = service.status(project)["capture"]
    assert capture["missing_events"] == 0 and capture["missing_event_ids"] == []


def test_a_transcript_found_at_start_and_named_again_at_end_has_one_active_projection(
        project, tmp_path, monkeypatch, calls):
    """A `codex-<uuid>` session finds its native transcript at start (operator.py:38-46); the same
    session is ended, resumed and ended again naming another copy of the file with `--transcript`.
    The store keeps one active transcript projection for the session (identity
    `transcript:<session>`, drain.py `projection_identity`), and no event is captured twice."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    native = str(uuid.UUID("0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"))
    found = _codex_transcript(tmp_path / "home" / ".codex" / "sessions" / "2026" / "10" / "04" /
                              ("rollout-2026-10-04T08-00-00-%s.jsonl" % native), native, project,
                              "the net shed inventory")
    sid = "codex-" + native
    started = operator.run("start", project, sid, operator="codex")
    assert started["transcript"] == str(found.resolve())
    ids = [append(project, "result", {"body": "net shed item %d" % n}, session=sid) for n in range(3)]
    operator.run("checkpoint", project, sid)
    operator.run("end", project, sid, reason="handoff")
    assert len(_active_transcripts(project, sid)) == 1
    copy = Path(project, "named-again.jsonl")
    shutil.copyfile(found, copy)
    operator.run("start", project, sid, operator="codex")
    operator.run("end", project, sid, reason="complete", transcript=str(copy))
    assert len(_active_transcripts(project, sid)) == 1
    held = _notes_of(project, ids)
    assert all(len(docs) == 1 for docs in held.values()), held
    assert set(_versions(project, ids).values()) == {1}


# ------------------------------------------------------------------ Codex and Claude, one store
def test_codex_and_claude_sessions_share_one_store_with_their_provenance(project, monkeypatch, calls):
    """A Claude session captured by its Stop hook and a Codex session captured by its checkpoint
    verb, both about one task: one store holds both, each note names its own operator and session
    (FR-6), the source versions keep them apart, and a question about the task reaches both."""
    from alpaca.hooks import common, stop
    operator.run("start", project, "claude-a", operator="claude")
    operator.run("start", project, "codex-b", operator="codex")
    claude = append(project, "result", {"body": "The kestrel queue backup moved to the night shift."},
                    session="claude-a", op="op-k", ref="t-k")
    codex = append(project, "result", {"body": "The kestrel queue backup now runs at 02:00."},
                   session="codex-b", op="op-k", ref="t-k")
    monkeypatch.setattr(common, "read_stdin", lambda *a, **k: {"session_id": "claude-a", "cwd": project})
    stop.main.__wrapped__()                                           # the Claude Stop hook body
    operator.run("checkpoint", project, "codex-b")
    assert len(calls["drain"]) == len(calls["service"]) == 2
    notes = _notes_of(project, [claude, codex])
    assert all(len(docs) == 1 for docs in notes.values()), notes
    claude_text = (vault(project) / notes[claude][0]).read_text(encoding="utf-8")
    codex_text = (vault(project) / notes[codex][0]).read_text(encoding="utf-8")
    assert re.search(r"^operator: claude$", claude_text, re.M) and "session: claude-a" in claude_text
    assert re.search(r"^operator: codex$", codex_text, re.M) and "session: codex-b" in codex_text
    with store(project) as conn:
        rows = {r["source_ref"]: (r["operator"], r["session"]) for r in conn.execute(
            "SELECT source_ref, operator, session FROM source_versions WHERE source_ref IN (?, ?)",
            ("event:%d" % claude, "event:%d" % codex))}
    assert rows == {"event:%d" % claude: ("claude", "claude-a"), "event:%d" % codex: ("codex", "codex-b")}
    answer = service.query(project, "kestrel queue backup")
    cited = json.dumps(answer.get("sources") or []) + json.dumps(answer.get("citations") or [])
    context = service.context(project, "kestrel queue backup", max_chars=4000)
    assert "night shift" in cited + context and "02:00" in cited + context


# ------------------------------------------------------------------ no second pipeline
def test_the_operator_verbs_reach_the_drain_only_through_service_capture():
    """No second hook pipeline: the operator verbs and the hooks they run call `service.capture`
    and never the drain or the Absorber themselves (one door, as for the Claude hooks)."""
    root = Path(drain.__file__).resolve().parents[2]
    for name in ("operator.py", "hooks/pre_compact.py", "hooks/stop.py", "hooks/session_end.py",
                 "hooks/session_start.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert not re.search(r"drain\.(run|capture)\(", text), name
        assert "wiki.ingest import drain" not in text and "wiki.ingest.drain" not in text, name
        assert "Absorber(" not in text and "absorb_text(" not in text, name
    assert "stop.capture_open_session(" in (root / "hooks" / "pre_compact.py").read_text(encoding="utf-8")
