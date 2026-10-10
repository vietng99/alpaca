"""op-008 t-430 (owner decision dec-f812470497fb): the record names each transcript version a capture
lands, so a rebuild at an earlier cutoff reads the version the store held then.

When a capture lands a transcript version (the hooks and the operator verbs through
`service.capture`, the collector's wiki consumer, `wiki recover`), the record gets one
`transcript-version` event: ref `transcript:<session>`, writer session `wiki-capture`, actor
`alpaca-wiki`, data {content_sha256, source_bytes, source_sha256, capture_head, held_before}. A
landing of a version the record already names as the session's newest appends nothing, and a replay
or an import writes nothing to the record. A rebuild at a cutoff before the record head reads each
transcript at the newest version event at or below the cutoff; a session the record cannot place
there (an old session captured before the change, a registration never captured) is best-effort and
named in the report's `transcript_cutoff.best_effort` with how it was read.

Real record, real drain, real store, real collector consumer and operator verbs; nothing is mocked
but the capture runner's child process, which runs inline (`runner_fakes.run_inline`).
"""
import hashlib
import json
import os
import sys
import threading
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import append, events, store  # noqa: E402

from alpaca import db as record                   # noqa: E402
from alpaca import observability                  # noqa: E402
from alpaca.wiki import recovery, service         # noqa: E402
from alpaca.wiki.ingest import drain              # noqa: E402

KIND = "transcript-version"
WRITER = "wiki-capture"
OPERATORS = ("claude", "codex")


def say(path: Path, operator: str, n: int) -> None:
    """Append one exchange to a native transcript in the operator's own format (a Claude session
    file, or a Codex rollout whose first line is its `session_meta`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if operator == "claude":
        rows = [{"type": "assistant", "timestamp": "2026-03-01T08:%02d:00Z" % n,
                 "message": {"content": [{"type": "thinking", "thinking": "private %d" % n}]}},
                {"type": "user", "timestamp": "2026-03-01T08:%02d:10Z" % n,
                 "message": {"content": "Where is task t-1 now (%d)?" % n}},
                {"type": "assistant", "timestamp": "2026-03-01T08:%02d:20Z" % n,
                 "message": {"content": [{"type": "text", "text": "Task t-1 is done (%d)." % n}]}}]
    else:
        rows = [] if path.exists() else [{"type": "session_meta", "timestamp": "2026-03-01T08:00:00Z",
                                          "payload": {"id": str(uuid.uuid4()), "cwd": "/x"}}]
        rows += [{"type": "response_item", "timestamp": "2026-03-01T08:%02d:10Z" % n,
                  "payload": {"type": "message", "role": "user",
                              "content": [{"type": "input_text",
                                           "text": "Where is task t-1 now (%d)?" % n}]}},
                 {"type": "response_item", "timestamp": "2026-03-01T08:%02d:20Z" % n,
                  "payload": {"type": "message", "role": "assistant",
                              "content": [{"type": "output_text",
                                           "text": "Task t-1 is done (%d)." % n}]}}]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in rows))


def register(project, tmp_path, sid, operator) -> Path:
    """A native transcript outside the root, registered as the hooks and the collector register one."""
    native = tmp_path / ("native-" + operator) / (sid + ".jsonl")
    native.parent.mkdir(parents=True, exist_ok=True)
    observability.expect_source(project, sid, operator, locator=str(native))
    return native


def versions(project, sid=None) -> list:
    """The `transcript-version` events, oldest first: (event row, data)."""
    return [(e, json.loads(e["data"])) for e in events(project)
            if e["kind"] == KIND and (sid is None or e["ref"] == "transcript:" + sid)]


def head(project) -> int:
    conn = record.connect_readonly(str(project))
    try:
        return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])
    finally:
        conn.close()


def current(project, sid) -> list:
    with store(project) as conn:
        return [r[0] for r in conn.execute(
            "SELECT content_sha256 FROM source_versions WHERE source_ref=? AND status='current'",
            ("transcript:" + sid,))]


def cap(project, sid):
    return service.capture(project, sessions=[sid], deadline_s=None)


# ------------------------------------------------------------------ what a capture records
@pytest.mark.parametrize("operator", OPERATORS)
def test_each_new_transcript_version_is_recorded_once(project, tmp_path, operator):
    """One event per new version (the event volume), none for a capture of a version the record
    already names (idempotent), and its fields name exactly what the store holds."""
    record.connect(project).close()
    native = register(project, tmp_path, "s-v", operator)
    say(native, operator, 1)
    cap(project, "s-v")
    cap(project, "s-v")                                    # the same version again: nothing new
    assert len(versions(project, "s-v")) == 1
    say(native, operator, 2)
    for _ in range(3):
        cap(project, "s-v")
    found = versions(project, "s-v")
    assert len(found) == 2, found
    (first_row, first), (row, data) = found
    raw = native.read_bytes()
    assert data == {"content_sha256": current(project, "s-v")[0], "source_bytes": len(raw),
                    "source_sha256": hashlib.sha256(raw).hexdigest(),
                    "capture_head": data["capture_head"], "held_before": True}
    assert first["held_before"] is False and first["source_bytes"] < data["source_bytes"]
    for event, fields in found:
        assert (event["session"], event["actor"], event["op"]) == (WRITER, "alpaca-wiki", None)
        assert fields["capture_head"] < event["id"]          # the head the capture read, before it
    # a version event is the store's own bookkeeping, never a source: no note of it lands
    assert not drain.renders_as_source(KIND, data)
    service.capture(project, events=events(project), deadline_s=None)
    with store(project) as conn:
        noted = conn.execute("SELECT COUNT(*) FROM docs WHERE doc_id LIKE ?",
                             ("%%/%06d-%%" % row["id"],)).fetchone()[0]
    assert noted == 0
    assert len(versions(project, "s-v")) == 2


def test_concurrent_captures_of_one_version_record_it_once(project, tmp_path):
    """The check and the append are one record transaction: captures racing on one new version
    (a Stop hook, the collector) leave one event."""
    record.connect(project).close()
    native = register(project, tmp_path, "s-race", "claude")
    say(native, "claude", 1)
    cap(project, "s-race")
    say(native, "claude", 2)
    errors = []

    def run():
        try:
            cap(project, "s-race")
        except Exception as exc:                       # pragma: no cover - reported below
            errors.append(exc)
    workers = [threading.Thread(target=run) for _ in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert errors == []
    assert len(versions(project, "s-race")) == 2


def test_the_collector_the_operator_verbs_and_recover_record_versions(project, tmp_path, monkeypatch):
    """Every capture door records the version it lands: the collector's wiki consumer, the Codex
    operator verbs (the capture runner, as the hooks use it), and `wiki recover` (a catch-up lands a
    version no capture landed; a second recover lands nothing new and records nothing)."""
    from alpaca import capture_runner, operator
    from alpaca.observability import consumers
    from alpaca.tests import runner_fakes
    monkeypatch.setattr(capture_runner, "run_process", runner_fakes.run_inline)
    record.connect(project).close()
    # the collector
    native = register(project, tmp_path, "s-col", "claude")
    say(native, "claude", 1)
    consumers._capture_wiki(project, [], ["s-col"])
    assert [d["source_bytes"] for _, d in versions(project, "s-col")] == [native.stat().st_size]
    # the Codex operator verbs: start finds the rollout, end captures it through the runner
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    native_id = str(uuid.UUID("0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5c"))
    rollout = (tmp_path / "home" / ".codex" / "sessions" / "2026" / "10" / "04" /
               ("rollout-2026-10-04T08-00-00-%s.jsonl" % native_id))
    rollout.parent.mkdir(parents=True)
    rollout.write_text(json.dumps({"type": "session_meta", "timestamp": "2026-10-04T08:00:00Z",
                                   "payload": {"id": native_id, "cwd": project}}) + "\n",
                       encoding="utf-8")
    say(rollout, "codex", 1)
    sid = "codex-" + native_id
    assert operator.run("start", project, sid, operator="codex")["transcript"] == str(rollout.resolve())
    append(project, "result", {"body": "net shed item"}, session=sid)
    operator.run("end", project, sid, reason="handoff")
    assert len(versions(project, sid)) == 1, versions(project, sid)
    # wiki recover: a registered session no capture landed
    late = register(project, tmp_path, "s-rec", "codex")
    say(late, "codex", 1)
    report = recovery.run(project, session="s-op")
    assert report["status"] == "ok", report
    (event, data), = versions(project, "s-rec")
    assert data["held_before"] is False and data["capture_head"] == report["through_event"]
    assert event["id"] < report["recovery_event"]
    before = len(versions(project))
    recovery.run(project, session="s-op")
    assert len(versions(project)) == before


def test_a_rebuild_from_an_empty_store_does_not_claim_the_store_held_nothing(project, tmp_path):
    """`wiki recover` on an empty store records what it landed with `held_before` None: the store it
    replaces may have held a version no event names, so an earlier cutoff stays best-effort."""
    from native_helpers import reset_vault
    record.connect(project).close()
    native = register(project, tmp_path, "s-old", "claude")
    say(native, "claude", 1)
    drain.capture(project, [], sessions=["s-old"])         # the capture before the change
    say(native, "claude", 2)
    reset_vault(project)
    recovery.run(project, session="s-op")
    (_, data), = versions(project, "s-old")
    assert data["held_before"] is None


def test_a_replay_or_an_import_writes_nothing_to_the_record(project, tmp_path):
    """Only a capture records a version: a replay (a catch-up, a rebuild at the head or at an
    earlier cutoff) and an import leave the record as they found it."""
    from alpaca.wiki import portable
    from convergence_helpers import shadow
    record.connect(project).close()
    native = register(project, tmp_path, "s-r", "codex")
    say(native, "codex", 1)
    append(project, "result", {"body": "Task t-1 is open."}, session="s-r")
    cutoff = head(project)
    say(native, "codex", 2)
    append(project, "result", {"body": "Task t-1 is done."}, session="s-r")
    n = head(project)
    assert recovery.replay(project)["status"] == "ok"
    assert head(project) == n and versions(project) == []
    for at in (cutoff, None):
        other = shadow(project, "replay-%s" % at)
        assert recovery.replay(other, cutoff=at)["status"] == "ok"
        assert head(other) == n
    out = tmp_path / "x.jsonl"
    portable.export_store(project, out)
    other = shadow(project, "imported")
    assert portable.import_store(other, out)["status"] == "ok"
    assert head(other) == n


# ------------------------------------------------------------------ the rebuild report
def test_old_sessions_are_best_effort_and_named_in_the_rebuild_report(project, tmp_path):
    """A rebuild at a cutoff before the head names how it read each transcript. Exact: `recorded`
    (a version event at or below the cutoff) and `recorded_after_cutoff` (the first one comes later
    and the store held none before it: nothing lands). Best-effort, in `best_effort` with `how`: a
    session captured before the change (`current-file`, or `snapshot` when a transcript-snapshot at
    or below the cutoff names a version), and one whose first recorded landing came later but
    replaced a version no event names (`first-recorded-version`: read at that version, so later
    growth leaves the rebuild as it is)."""
    from alpaca import transcripts
    from convergence_helpers import shadow
    from native_helpers import at
    record.connect(project).close()
    talks = {sid: register(project, tmp_path, sid, op) for sid, op in (
        ("s-new", "claude"), ("s-old", "codex"), ("s-snap", "claude"), ("s-mixed", "codex"),
        ("s-later", "claude"))}
    for sid, path in talks.items():
        say(path, "codex" if path.parent.name.endswith("codex") else "claude", 1)
    cap(project, "s-new")                                          # after the change
    drain.capture(project, [], sessions=["s-old", "s-snap", "s-mixed"])   # before the change
    transcripts.snapshot(project, "s-snap", event=True)
    at(project, "result", {"body": "Task t-1 is open on the review."}, "2026-03-01T09:00:00+00:00",
       session="s-new")
    cutoff = head(project)
    say(talks["s-mixed"], "codex", 2)
    cap(project, "s-mixed")                                        # first recorded, held one before
    cap(project, "s-later")                                        # first landing, after the cutoff
    say(talks["s-old"], "codex", 2)
    at(project, "result", {"body": "Task t-1 is done on the review."}, "2026-03-01T09:30:00+00:00",
       session="s-new")

    def rebuilt(name):
        root = shadow(project, name)
        report = recovery.replay(root, cutoff=cutoff)
        assert report["status"] == "ok", report
        with store(root) as conn:
            held = {r[0]: r[1] for r in conn.execute(
                "SELECT source_ref, content_sha256 FROM source_versions "
                "WHERE source_ref LIKE 'transcript:%' AND status='current'")}
        return report["transcript_cutoff"], held
    seen, held = rebuilt("best-effort-1")
    assert seen["recorded"] == ["s-new"]
    assert seen["recorded_after_cutoff"] == ["s-later"]
    assert {e["session"]: e["how"] for e in seen["best_effort"]} == {
        "s-old": "current-file", "s-snap": "snapshot", "s-mixed": "first-recorded-version"}
    assert "transcript:s-later" not in held
    say(talks["s-mixed"], "codex", 3)
    cap(project, "s-mixed")
    again, held_again = rebuilt("best-effort-2")
    assert again == seen
    assert held_again["transcript:s-mixed"] == held["transcript:s-mixed"]


def test_a_recorded_version_whose_bytes_are_gone_is_reported_missing(project, tmp_path):
    """The version a rebuild at an earlier cutoff must read is no longer the start of the file (the
    file was rewritten): nothing of it lands, the rebuild is incomplete and names it, never another
    version."""
    from convergence_helpers import shadow
    record.connect(project).close()
    native = register(project, tmp_path, "s-gone", "claude")
    say(native, "claude", 1)
    cap(project, "s-gone")
    cutoff = head(project)
    native.write_text("", encoding="utf-8")
    say(native, "claude", 7)
    append(project, "result", {"body": "later"}, session="s-gone")
    root = shadow(project, "gone")
    report = recovery.replay(root, cutoff=cutoff)
    assert report["status"] == "incomplete"
    assert [m["session"] for m in report["missing_transcripts"]] == ["s-gone"]
    assert report["missing_transcripts"][0]["reason"] == "transcript-version-unavailable"
    with store(root) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_versions WHERE source_ref='transcript:s-gone'"
                            ).fetchone()[0] == 0


# ------------------------------------------------------------------ races (review T-1, T-3, T-4)
def _store_seqs(project, sid) -> dict:
    """content_sha256 -> the store's own `version_seq` of each version of the transcript."""
    with store(project) as conn:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT content_sha256, version_seq FROM source_versions WHERE source_ref=?",
            ("transcript:" + sid,))}


def test_two_captures_of_two_versions_record_them_in_the_store_landing_order(project, tmp_path,
                                                                              monkeypatch):
    """Review T-1, interleaved deterministically: capture A lands v1 and is held at its record
    append until capture B, of v2, has finished or waited 1.5 s. The record's version events follow
    the store's own landing order (`version_seq`), so the newest event at every cutoff names the
    version the store held then, and the newest of all names the version it holds now."""
    from convergence_helpers import shadow
    record.connect(project).close()
    native = register(project, tmp_path, "s-ab", "claude")
    say(native, "claude", 1)
    cap(project, "s-ab")                                    # v0, recorded
    say(native, "claude", 2)                                # v1, A's version
    real = drain._record_transcript_version
    a_in, b_done = threading.Event(), threading.Event()
    holder = {}

    def held(*args, **kwargs):
        if threading.current_thread() is holder.get("a"):
            a_in.set()
            b_done.wait(1.5)
        return real(*args, **kwargs)
    monkeypatch.setattr(drain, "_record_transcript_version", held)
    errors = []

    def run(done=None):
        try:
            cap(project, "s-ab")
        except Exception as exc:                            # pragma: no cover - reported below
            errors.append(exc)
        finally:
            if done is not None:
                done.set()
    holder["a"] = threading.Thread(target=run)
    holder["a"].start()
    assert a_in.wait(60)
    say(native, "claude", 3)                                # v2, B's version
    b = threading.Thread(target=run, args=(b_done,))
    b.start()
    b.join()
    holder["a"].join()
    assert errors == []
    seqs = _store_seqs(project, "s-ab")
    found = versions(project, "s-ab")
    order = [seqs[d["content_sha256"]] for _, d in found]
    assert order == sorted(order) and len(set(order)) == len(order), (order, found)
    assert found[-1][1]["content_sha256"] == current(project, "s-ab")[0]
    assert found[-1][1]["source_bytes"] == native.stat().st_size
    # a rebuild at each event's cutoff (below the head) lands the version that event names
    for event, data in found:
        root = shadow(project, "ab-%d" % event["id"])
        append(root, "result", {"body": "after"}, session="s-other")
        assert recovery.replay(root, cutoff=event["id"])["status"] == "ok"
        assert current(root, "s-ab") == [data["content_sha256"]]


def test_held_before_is_read_under_the_store_write_lock(project, tmp_path, monkeypatch):
    """Review T-3: capture A reads the store's projections, then another capture lands the first
    version of the session before A lands its own. A's event must say the store held a version
    before it (`held_before` true): the store's state under the write lock, not A's read at start."""
    from alpaca.wiki import faults
    record.connect(project).close()
    native = register(project, tmp_path, "s-hb", "codex")
    say(native, "codex", 1)

    class Arm:
        fired = False

        def passed(self, name):
            if name == "drain-land" and not Arm.fired:
                Arm.fired = True                # A has rendered v1 and read the store: B lands v2
                say(native, "codex", 2)
                cap(project, "s-hb")
    monkeypatch.setattr(faults, "_ARM", Arm())
    cap(project, "s-hb")
    monkeypatch.setattr(faults, "_ARM", None)
    found = versions(project, "s-hb")
    assert [d["held_before"] for _, d in found] == [False, True], found


def test_a_capture_on_a_root_with_no_record_creates_none(project):
    """Review T-4: recording a version opens the record only when it exists; a capture of a
    retained transcript copy on a root with no record leaves no record file behind."""
    from alpaca import paths, transcripts
    copy = Path(transcripts.local_path(project, "s-none"))
    say(copy, "claude", 1)
    assert not os.path.exists(paths.db_path(project))
    out = cap(project, "s-none")
    assert out["transcripts"] and out["versions_recorded"] == 0
    assert not os.path.exists(paths.db_path(project))
