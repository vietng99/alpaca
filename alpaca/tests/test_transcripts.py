"""E1 proof: the transcript snapshot.

The record points at a transcript in the operator's own storage, which is cleared after thirty
days. These controls hold the copy that outlives it:

  * a registered transcript is copied into `.alpaca/transcripts/<sid>.jsonl`, with the subagent
    files under `<sid>/subagents/**` where parse_session reads them;
  * a grown transcript costs one tail append, and the local copy still equals the source byte
    for byte;
  * a rewritten or truncated source keeps the old copy under a numbered name and copies whole;
  * a source that vanished is reported, and the local copy is left alone;
  * two hooks snapshotting one session at the same moment append the tail once, not twice: the
    second one reports `busy` and does nothing;
  * a rewrite cut off part way through never leaves the canonical name missing;
  * a session id that names a path cannot leave `.alpaca/transcripts/`, and the recorded path is
    relative and inside it;
  * numbered old copies and leaked temporary files are both bounded;
  * the snapshot event carries the path, the bytes, the sha256, the subagent count and whether
    the source is gone;
  * `checks` warns for a registered transcript with no copy, and for a copy behind its source;
  * the Stop, PreCompact, SessionEnd and SubagentStop hooks all reach it;
  * the analytics reader falls back to the local copy when the registered path is gone.

Every control asserts a positive and a negative path.
"""
import hashlib
import json
import os
import subprocess
import sys
import time

import pytest

from alpaca import cli, db, operator, paths, pool, transcripts
from alpaca.analytics import build_index

ENV = lambda project: {**os.environ, "CLAUDE_PROJECT_DIR": project,
                       "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.dirname(__file__)))}

SID = "sess-one"


def hook(module, payload, project):
    return subprocess.run([sys.executable, "-m", module], input=json.dumps(payload),
                          capture_output=True, text=True, encoding="utf-8",
                          cwd=project, env=ENV(project))


def _turn(n):
    return json.dumps({"type": "user", "timestamp": "2026-09-18T10:0%d:00.000Z" % (n % 10),
                       "message": {"role": "user", "content": "prompt %d" % n}}) + "\n"


def _source(tmp_path, sid=SID, turns=2):
    """A transcript in storage outside the project, with one subagent file beside it."""
    src_dir = tmp_path / "claude" / "projects" / "proj"
    src_dir.mkdir(parents=True, exist_ok=True)
    src = src_dir / ("%s.jsonl" % sid)
    src.write_text("".join(_turn(i) for i in range(turns)), encoding="utf-8")
    sub = src_dir / sid / "subagents" / "task-a"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "agent-1.jsonl").write_text(_turn(9), encoding="utf-8")
    return str(src)


def _register(project, sid, src):
    conn = db.connect(project)
    db.upsert(conn, "sessions", "sid", {"sid": sid, "started": "2026-09-18T10:00:00+00:00",
                                        "transcript": src})
    conn.close()


def _sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


# ------------------------------------------------------------------- copy, append, rewrite
def test_snapshot_copies_the_transcript_and_its_subagents(project, tmp_path):
    cli.main(["init"])
    src = _source(tmp_path)
    _register(project, SID, src)
    out = transcripts.snapshot(project, SID)
    local = transcripts.local_path(project, SID)
    assert out["mode"] == "copy" and out["source_missing"] is False
    assert os.path.isfile(local) and _sha(local) == _sha(src)
    assert out["path"] == os.path.join(".alpaca", "transcripts", "%s.jsonl" % SID)
    assert out["bytes"] == os.path.getsize(src) and out["sha256"] == _sha(src)
    # the subagent file lands where parse_session looks for it, not flattened beside the copy.
    sub = os.path.join(project, ".alpaca", "transcripts", SID, "subagents", "task-a", "agent-1.jsonl")
    assert os.path.isfile(sub) and out["subagents"] == 1
    # negative: nothing was written outside the transcript dir.
    assert not os.path.exists(os.path.join(project, ".alpaca", "transcripts", "agent-1.jsonl"))


def test_a_grown_transcript_costs_one_append(project, tmp_path, monkeypatch):
    cli.main(["init"])
    src = _source(tmp_path)
    _register(project, SID, src)
    transcripts.snapshot(project, SID)
    with open(src, "a", encoding="utf-8") as fh:
        fh.write(_turn(3))
    calls = []
    monkeypatch.setattr(transcripts, "_full_copy",
                        lambda s, d: calls.append(d) or (_ for _ in ()).throw(AssertionError("full copy")))
    out = transcripts.snapshot(project, SID)
    assert out["mode"] == "append" and calls == []
    assert _sha(transcripts.local_path(project, SID)) == _sha(src)
    # negative: a second snapshot with nothing new appends nothing.
    assert transcripts.snapshot(project, SID)["mode"] == "current"


def test_a_rewritten_source_keeps_the_old_copy_and_copies_whole(project, tmp_path):
    cli.main(["init"])
    src = _source(tmp_path, turns=3)
    _register(project, SID, src)
    transcripts.snapshot(project, SID)
    old = _sha(transcripts.local_path(project, SID))
    with open(src, "w", encoding="utf-8") as fh:
        fh.write(_turn(7) + _turn(8) + _turn(9))       # same length, different bytes
    out = transcripts.snapshot(project, SID)
    assert out["mode"] == "rewrite"
    kept = transcripts.local_path(project, SID) + ".1"
    assert os.path.isfile(kept) and _sha(kept) == old
    assert _sha(transcripts.local_path(project, SID)) == _sha(src)
    # negative: a truncated source is a rewrite too, never a silent shorter copy.
    with open(src, "w", encoding="utf-8") as fh:
        fh.write(_turn(7))
    assert transcripts.snapshot(project, SID)["mode"] == "rewrite"
    assert os.path.isfile(transcripts.local_path(project, SID) + ".2")


def test_a_vanished_source_never_deletes_the_local_copy(project, tmp_path):
    cli.main(["init"])
    src = _source(tmp_path)
    _register(project, SID, src)
    transcripts.snapshot(project, SID)
    before = _sha(transcripts.local_path(project, SID))
    os.unlink(src)
    out = transcripts.snapshot(project, SID)
    assert out["source_missing"] is True and out["mode"] == "none"
    assert _sha(transcripts.local_path(project, SID)) == before
    # negative: a session with no registered transcript at all is a no-op, not an error.
    conn = db.connect(project)
    db.upsert(conn, "sessions", "sid", {"sid": "bare", "started": "2026-09-18T11:00:00+00:00"})
    conn.close()
    bare = transcripts.snapshot(project, "bare")
    assert bare["source_missing"] is True and bare["bytes"] == 0 and bare["sha256"] is None


def test_a_source_that_is_already_the_local_copy_is_left_alone(project, tmp_path):
    cli.main(["init"])
    local = transcripts.local_path(project, SID)
    os.makedirs(os.path.dirname(local), exist_ok=True)
    with open(local, "w", encoding="utf-8") as fh:
        fh.write(_turn(1))
    _register(project, SID, local)
    out = transcripts.snapshot(project, SID)
    assert out["mode"] == "local" and out["source_missing"] is False
    assert not os.path.exists(local + ".1")


# ----------------------------------------------------- two hooks on one session at one moment
def test_a_snapshot_inside_another_one_is_skipped_as_busy(project, tmp_path, monkeypatch):
    """A SubagentStop beside a Stop: both read the same local size and both append the same tail.

    The reentrant call here lands in exactly that window -- the size has been read, the append has
    not happened yet -- which is what two hooks do to each other by chance.
    """
    cli.main(["init"])
    src = _source(tmp_path, turns=2)
    _register(project, SID, src)
    transcripts.snapshot(project, SID)
    with open(src, "a", encoding="utf-8") as fh:
        fh.write(_turn(3) + _turn(4))
    inner, state = [], {"done": False}
    real = transcripts._is_prefix

    def reentrant(s, d, n):
        if not state["done"]:
            state["done"] = True
            inner.append(transcripts.snapshot(project, SID))
        return real(s, d, n)

    monkeypatch.setattr(transcripts, "_is_prefix", reentrant)
    out = transcripts.snapshot(project, SID)
    # the second hook reports what it did, which is nothing: no copy, no size, no digest.
    assert inner and inner[0]["mode"] == "busy"
    assert inner[0]["bytes"] is None and inner[0]["sha256"] is None
    assert out["mode"] == "append"
    # the tail landed ONCE: the local copy is the source byte for byte, not the source plus a tail.
    local = transcripts.local_path(project, SID)
    assert os.path.getsize(local) == os.path.getsize(src) and _sha(local) == _sha(src)
    # negative: with nothing holding the lock, an ordinary snapshot is never reported busy.
    assert transcripts.snapshot(project, SID)["mode"] == "current"


def test_a_rewrite_never_leaves_the_canonical_name_missing(project, tmp_path, monkeypatch):
    """The old copy takes its numbered name only after the new bytes are on disk."""
    cli.main(["init"])
    src = _source(tmp_path, turns=3)
    _register(project, SID, src)
    transcripts.snapshot(project, SID)
    dst = transcripts.local_path(project, SID)
    old = _sha(dst)
    with open(src, "w", encoding="utf-8") as fh:
        fh.write(_turn(7) + _turn(8) + _turn(9))       # same length, different bytes: a rewrite
    real_keep = transcripts._keep

    class Cutoff(BaseException):
        """Stands in for the hard hook deadline, which is a BaseException too."""

    def keep_then_die(d):
        real_keep(d)
        raise Cutoff()

    monkeypatch.setattr(transcripts, "_keep", keep_then_die)
    with pytest.raises(Cutoff):
        transcripts.copy_forward(src, dst)
    # the only copy of this transcript is still under its own name, with its own bytes.
    assert os.path.isfile(dst) and _sha(dst) == old
    # negative: the interrupted copy left no temporary file behind to grow into a leak.
    d = os.path.dirname(dst)
    assert [n for n in os.listdir(d) if n.startswith(".tmp-")] == []


# ------------------------------------------------------------------ a hostile session id
def test_a_session_id_that_names_a_path_stays_inside_the_transcript_dir(project, tmp_path):
    cli.main(["init"])
    assert transcripts.slug is pool.slug                # one slug rule, not two spellings of it
    tdir = os.path.abspath(paths.transcript_dir(project))
    for sid in ("../../escape", "/etc/passwd", "a/b"):
        p = os.path.abspath(transcripts.local_path(project, sid))
        assert os.path.dirname(p) == tdir and os.sep not in os.path.basename(p)
        assert os.path.abspath(transcripts.subagents_dir(project, sid)).startswith(tdir + os.sep)
    # a hostile id still snapshots, and what the record carries is relative and inside the dir.
    src = _source(tmp_path, sid="../../escape")
    _register(project, "../../escape", src)
    rel = transcripts.snapshot(project, "../../escape", event=True)["path"]
    assert not os.path.isabs(rel)
    assert rel.startswith(os.path.join(".alpaca", "transcripts") + os.sep)
    assert os.path.isfile(os.path.join(project, rel))
    conn = db.connect(project)
    assert db.events(conn, kind="transcript-snapshot")[-1]["data"]["path"] == rel
    conn.close()
    # negative: an ordinary session id is not mangled on the way to its path.
    assert os.path.basename(transcripts.local_path(project, "codex-1.2_a")) == "codex-1.2_a.jsonl"


# ------------------------------------------------------------------------- bounded leftovers
def test_old_generations_are_preserved_and_leaked_temp_files_are_bounded(project, tmp_path):
    cli.main(["init"])
    src = _source(tmp_path, turns=3)
    _register(project, SID, src)
    transcripts.snapshot(project, SID)
    dst = transcripts.local_path(project, SID)
    tdir = os.path.dirname(dst)
    for i in range(5):                                  # five rewrites offer five old copies
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("".join(_turn(i + j + 1) for j in range(3)))
        assert transcripts.snapshot(project, SID)["mode"] == "rewrite"
    base = os.path.basename(dst)
    kept = [n for n in os.listdir(tdir)
            if n.startswith(base + ".") and n[len(base) + 1:].isdigit()]
    assert len(kept) == 5
    # Every historical generation survives automatic collection.
    assert sorted(int(n[len(base) + 1:]) for n in kept) == [1, 2, 3, 4, 5]
    # the canonical copy is never a candidate, and it still matches the source.
    assert os.path.isfile(dst) and _sha(dst) == _sha(src)
    # a temp file older than the cutoff is swept; one that may belong to a copy running now stays.
    stale = os.path.join(tdir, ".tmp-stale")
    fresh = os.path.join(tdir, ".tmp-fresh")
    for p in (stale, fresh):
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("x")
    os.utime(stale, (0, time.time() - transcripts.TMP_MAX_AGE_S - 60))
    transcripts.snapshot(project, SID)
    assert not os.path.exists(stale) and os.path.isfile(fresh)


# ------------------------------------------------------------------------------- the event
def test_snapshot_event_carries_the_five_fields(project, tmp_path):
    cli.main(["init"])
    src = _source(tmp_path)
    _register(project, SID, src)
    transcripts.snapshot(project, SID, event=True)
    conn = db.connect(project)
    e = db.events(conn, kind="transcript-snapshot")[-1]
    assert {"path", "bytes", "sha256", "subagents", "source_missing", "generation", "privacy"} <= set(e["data"])
    assert e["data"]["sha256"] == _sha(src) and e["data"]["subagents"] == 1
    assert e["data"]["source_missing"] is False
    assert db.verify_chain(conn)[0]
    # negative: the default path records nothing.
    n = len(db.events(conn, kind="transcript-snapshot", limit=1000))
    transcripts.snapshot(project, SID)
    assert len(db.events(conn, kind="transcript-snapshot", limit=1000)) == n
    conn.close()


# ------------------------------------------------------------------------------ doctor checks
def test_checks_warn_on_a_missing_or_stale_copy(project, tmp_path):
    cli.main(["init"])
    conn = db.connect(project)
    assert transcripts.checks(project, conn) == []          # nothing registered, nothing to say
    src = _source(tmp_path)
    _register(project, SID, src)
    name, level, detail = transcripts.checks(project, conn)[0]
    assert (name, level) == ("transcript-snapshots", "warn") and "no local copy" in detail
    transcripts.snapshot(project, SID, conn=conn)
    assert transcripts.checks(project, conn)[0][1] == "ok"
    with open(src, "a", encoding="utf-8") as fh:
        fh.write(_turn(4))
    # an open session is behind between two boundaries: said, not warned about.
    name, level, detail = transcripts.checks(project, conn)[0]
    assert level == "ok" and "1 open session(s) between snapshots" in detail
    # once the session has ended, a copy behind its source is a recoverable loss: a warning.
    db.patch(conn, "sessions", "sid", SID, {"ended": "2026-01-01T00:00:00+00:00"})
    name, level, detail = transcripts.checks(project, conn)[0]
    assert level == "warn" and "behind the source" in detail and SID in detail
    # a source that is gone with no local copy cannot be recovered: counted, not a warning.
    os.unlink(transcripts.local_path(project, SID)); os.unlink(src)
    name, level, detail = transcripts.checks(project, conn)[0]
    assert level == "ok" and "1 lost before the first snapshot" in detail
    conn.close()


def test_the_snapshot_verb_backfills_a_session_that_ended_before_the_hooks(project, tmp_path, capsys):
    cli.main(["init"])
    src = _source(tmp_path)
    _register(project, SID, src)
    assert not os.path.isfile(transcripts.local_path(project, SID))
    assert cli.main(["analytics", "snapshot"]) == cli.PASS
    assert os.path.isfile(transcripts.local_path(project, SID))
    conn = db.connect(project)
    events = db.events(conn, kind="transcript-snapshot", limit=1000)
    assert len(events) == 1 and events[0]["data"]["bytes"] == os.path.getsize(src)
    assert "1 snapshot(s), 0 source(s) already gone" in capsys.readouterr().out
    conn.close()


# --------------------------------------------------------------------------------- the hooks
def test_stop_snapshots_without_recording_an_event(project, tmp_path):
    cli.main(["init"])
    src = _source(tmp_path)
    _register(project, SID, src)
    p = hook("alpaca.hooks.stop", {"session_id": SID, "cwd": project}, project)
    assert p.returncode == 0 and p.stdout == ""
    assert os.path.isfile(transcripts.local_path(project, SID))
    conn = db.connect(project)
    assert db.events(conn, kind="transcript-snapshot") == []
    conn.close()


def test_pre_compact_and_session_end_record_the_snapshot(project, tmp_path):
    cli.main(["init"])
    src = _source(tmp_path)
    _register(project, SID, src)
    assert hook("alpaca.hooks.pre_compact", {"session_id": SID, "cwd": project}, project).returncode == 0
    conn = db.connect(project)
    assert len(db.events(conn, kind="transcript-snapshot")) == 1
    conn.close()
    assert hook("alpaca.hooks.session_end", {"session_id": SID, "cwd": project,
                                            "reason": "exit"}, project).returncode == 0
    conn = db.connect(project)
    assert len(db.events(conn, kind="transcript-snapshot")) == 2
    assert db.verify_chain(conn)[0]
    conn.close()


def test_subagent_stop_records_its_ids_and_snapshots(project, tmp_path):
    cli.main(["init"])
    src = _source(tmp_path)
    _register(project, SID, src)
    p = hook("alpaca.hooks.subagent_stop",
             {"session_id": SID, "cwd": project, "agent_id": "a-7", "agentType": "Explore"},
             project)
    assert p.returncode == 0 and p.stdout == ""
    conn = db.connect(project)
    e = db.events(conn, kind="subagent-stop")[-1]
    assert e["data"] == {"agent_id": "a-7", "agent_type": "Explore"}
    conn.close()
    assert os.path.isfile(transcripts.local_path(project, SID))


def test_subagent_stop_tolerates_a_payload_with_no_ids(project):
    cli.main(["init"])
    p = hook("alpaca.hooks.subagent_stop", {"session_id": SID, "cwd": project}, project)
    assert p.returncode == 0 and p.stdout == ""
    conn = db.connect(project)
    assert db.events(conn, kind="subagent-stop")[-1]["data"] == {}
    conn.close()


# ------------------------------------------------------------------ analytics falls back to it
def test_analytics_reads_the_local_copy_when_the_source_is_gone(project, tmp_path):
    cli.main(["init"])
    src = _source(tmp_path, turns=2)
    _register(project, SID, src)
    transcripts.snapshot(project, SID)
    os.unlink(src)
    session = [s for s in build_index.collect(project) if s["sid"] == SID][0]
    # positive: the prompts survive the source.
    assert len(session["human_prompts"]) == 2
    assert session["capture"] != "explicit-record"
    # negative: with no local copy the same session falls back to the record-only summary.
    os.unlink(transcripts.local_path(project, SID))
    cache = os.path.join(project, ".alpaca", "analytics", "sessions", "%s.json" % SID)
    if os.path.isfile(cache):
        os.unlink(cache)
    again = [s for s in build_index.collect(project) if s["sid"] == SID][0]
    assert again["capture"] == "explicit-record" and again["human_prompts"] == []


# ------------------------------------------------------------------- the explicit operator path
def test_operator_registers_an_explicit_transcript_on_start_and_end(project, tmp_path):
    cli.main(["init"])
    src = _source(tmp_path, sid="codex-1")
    operator.run("start", project, "codex-1", operator="codex", transcript=src)
    conn = db.connect(project)
    assert db.rows(conn, "sessions", "sid=?", ("codex-1",))[0]["transcript"] == os.path.realpath(src)
    conn.close()
    other = _source(tmp_path / "later", sid="codex-1")
    operator.run("end", project, "codex-1", operator="codex", transcript=other)
    conn = db.connect(project)
    assert db.rows(conn, "sessions", "sid=?", ("codex-1",))[0]["transcript"] == os.path.realpath(other)
    # end snapshots what it registered, and records it.
    assert db.events(conn, kind="transcript-snapshot")[-1]["data"]["bytes"] == os.path.getsize(other)
    conn.close()
    assert os.path.isfile(transcripts.local_path(project, "codex-1"))


def test_operator_refuses_an_unreadable_or_misplaced_transcript(project, tmp_path):
    cli.main(["init"])
    with pytest.raises(operator.SessionError, match="no readable file"):
        operator.run("start", project, "codex-2", operator="codex",
                     transcript=str(tmp_path / "absent.jsonl"))
    src = _source(tmp_path, sid="codex-3")
    operator.run("start", project, "codex-3", operator="codex")
    with pytest.raises(operator.SessionError, match="session start or session end"):
        operator.run("stop", project, "codex-3", operator="codex", transcript=src)
    # negative: no path named means no path registered; discovery never fills it in.
    conn = db.connect(project)
    assert not db.rows(conn, "sessions", "sid=?", ("codex-3",))[0]["transcript"]
    conn.close()
    assert src
