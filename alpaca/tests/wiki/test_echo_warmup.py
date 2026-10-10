"""t-462: a collector restart reads the echo index outside the capture budget, and the read costs the
active blocks, not every block the store ever held.

After the t-442 landing (2026-10-08 15:37 to 15:44:51, `.alpaca/proofs/op-008/t-070.logs/land/land.log`)
the restarted collector abandoned every wiki capture and preservation job for about 8 minutes: the
first capture of the new process read the echo index from the live store inside `CAPTURE_BUDGET_S`
(stack: `collector-stack-1543.txt`), and the read walked every one of the store's 4.5 million block
rows in block id order to find its 717,000 active ones. Now:

- the read takes the active rowids from `idx_blocks_status` and the rows in rowid order, in one
  snapshot, and builds the same index (`absorb.echo_rows`);
- a watch collector reads the index on the wiki lane at the start of each pass (`consumers.warm_wiki`),
  and while that read runs the wiki consumer and the preservation jobs are `warming`: a state with
  no `capture-deferred`, no `consumer-failed` issue, no failure count and no backoff. They run on
  the first pass after the read.
"""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from alpaca import db, observability
from alpaca.observability import consumers
from alpaca.wiki import service
from alpaca.wiki.config import Config
from alpaca.wiki.engine.echo import EchoIndex
from alpaca.wiki.ingest import absorb

OLD_ECHO_INDEX_SQL = "SELECT block_id,doc_id,text FROM blocks WHERE +status='active' ORDER BY block_id"


def _append(root, session, n, start=0):
    conn = db.connect(root)
    try:
        for i in range(start, start + n):
            db.append_event(conn, session=session, actor="agent", kind="result",
                            data={"body": "note %d of the lantern rota, shift %d" % (i, i % 3)})
    finally:
        conn.close()


def _events(root, kind):
    conn = db.connect_readonly(root)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM events WHERE kind=? ORDER BY id", (kind,))]
    finally:
        conn.close()


def _store_with_retired_blocks(project):
    """A store whose blocks table holds active and superseded rows: one note captured, then a
    document re-ingested with other text, which retires its old blocks."""
    from alpaca import cli
    cli.main(["init"])
    _append(project, "s-echo", 6)
    service.capture(project, sessions=["s-echo"], session_events=True, deadline_s=None)
    cfg = Config.for_project(project)
    absorber = absorb.Absorber(cfg)
    try:
        absorber.absorb_text("wiki/rota.md", "# Rota\n\nAda keeps the lantern.\n\nBo keeps the bell.\n",
                             kind="wiki")
        absorber.absorb_text("wiki/rota.md", "# Rota\n\nCy keeps the lantern now.\n", kind="wiki",
                             waive_reason="t-462 test: a smaller page on purpose")
    finally:
        absorber.close()
    conn = sqlite3.connect(cfg.db_path.as_uri() + "?mode=ro", uri=True)
    try:
        counts = dict(conn.execute("SELECT status, count(*) FROM blocks GROUP BY status").fetchall())
    finally:
        conn.close()
    assert counts.get("superseded") and counts.get("active", 0) >= 6, counts
    return cfg


def _state(index):
    return {name: {k: (sorted(v) if isinstance(v, set) else v) for k, v in getattr(index, name).items()}
            for name in ("_doc", "_by_doc", "_by_size", "_norm", "_tokens", "_exact", "_postings")}


def test_the_echo_index_read_visits_the_active_rows_only(project):
    """The plans of the statements the absorber runs to read its echo index: no statement walks the
    whole blocks table or its primary key's index (f15a17101: `SCAN blocks USING INDEX
    sqlite_autoindex_blocks_1`), and none sorts through temp files."""
    cfg = _store_with_retired_blocks(project)
    absorber = absorb.Absorber(cfg)
    statements = []
    try:
        absorber.db.conn.set_trace_callback(statements.append)
        absorber.echo_index
        absorber.db.conn.set_trace_callback(None)
        reads = [s for s in statements if s.lstrip().upper().startswith("SELECT") and "blocks" in s]
        assert reads
        plans = []
        for sql in dict.fromkeys(reads):
            plans.append(" | ".join(r[3] for r in absorber.db.conn.execute("EXPLAIN QUERY PLAN " + sql)))
        for plan in plans:
            assert "SCAN blocks" not in plan, plans
            assert "TEMP B-TREE" not in plan, plans
    finally:
        absorber.close()


def test_the_new_read_builds_the_index_the_ordered_read_built(project):
    cfg = _store_with_retired_blocks(project)
    conn = sqlite3.connect(cfg.db_path.as_uri() + "?mode=ro", uri=True)
    try:
        before = EchoIndex()
        before.load((r[0], r[2], r[1]) for r in conn.execute(OLD_ECHO_INDEX_SQL))
        after = absorb.load_echo_index(conn)
        assert _state(after) == _state(before)
        assert len(after._doc) == conn.execute("SELECT count(*) FROM blocks WHERE status='active'").fetchone()[0]
        for text in ("Ada keeps the lantern.", "Cy keeps the lantern now.", "note 3 of the lantern rota, shift 0"):
            assert after.detect(text) == before.detect(text)
    finally:
        conn.close()


def test_the_read_sees_one_snapshot(project, monkeypatch):
    """A block another connection retires while the read is between two chunks is still read as the
    snapshot had it: the index is the store at one moment, never half of two."""
    cfg = _store_with_retired_blocks(project)
    monkeypatch.setattr(absorb, "ECHO_CHUNK", 1)
    reader = sqlite3.connect(str(cfg.db_path))
    writer = sqlite3.connect(str(cfg.db_path), timeout=5)
    try:
        active = [r[0] for r in reader.execute(
            "SELECT block_id FROM blocks WHERE status='active' ORDER BY rowid")]
        rows = absorb.echo_rows(reader)
        first = next(rows)
        writer.execute("UPDATE blocks SET status='superseded' WHERE block_id=?", (active[-1],))
        writer.commit()
        seen = [first[0]] + [r[0] for r in rows]
        assert sorted(seen) == sorted(active)
        assert not reader.in_transaction
    finally:
        reader.close()
        writer.close()


# ------------------------------------------------------------------ the collector's warm-up
@pytest.fixture
def collector(tmp_path, monkeypatch):
    """A record with a wiki store, a fresh pair of collector lanes (a restarted process), the watch
    mode set, and an echo index read that waits until the test releases it."""
    root = str(tmp_path / "collector")
    os.makedirs(root)
    _append(root, "s-col", 3)
    monkeypatch.delenv("ALPACA_COLLECTOR_WATCH", raising=False)
    first = observability.run_once(root)                    # creates the store, consumes the first rows
    assert first["consumers"]["wiki"]["status"] == "ok"
    assert Config.for_project(root).db_path.exists()
    monkeypatch.setattr(consumers, "LANES", consumers._lanes())
    monkeypatch.setenv("ALPACA_COLLECTOR_WATCH", "1")
    release, started = threading.Event(), threading.Event()
    real = EchoIndex.load

    def slow(self, rows):
        started.set()
        release.wait(20)
        return real(self, rows)
    monkeypatch.setattr(EchoIndex, "load", slow)
    monkeypatch.setattr(consumers, "CAPTURE_BUDGET_S", 1.0, raising=False)
    try:
        yield root, release, started
    finally:
        release.set()
        lane = consumers.LANES["wiki"]
        end = time.monotonic() + 20
        while lane.busy_since() is not None and time.monotonic() < end:
            time.sleep(0.02)
        lane.run(consumers._SHARED.close, budget_s=5.0)


def _checkpoint(root, name="wiki"):
    conn = db.connect_readonly(root)
    try:
        row = conn.execute("SELECT * FROM obs_consumer_checkpoint WHERE consumer=?", (name,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _open_issues(root):
    conn = db.connect_readonly(root)
    try:
        return [dict(r) for r in conn.execute("SELECT code, detail FROM obs_issue WHERE resolved_at IS NULL")]
    finally:
        conn.close()


def test_a_restarted_collector_warms_the_echo_index_as_a_state_not_a_failure(collector):
    root, release, started = collector
    _append(root, "s-col", 4, start=3)
    conn = db.connect(root)
    try:
        consumers.queue_snapshot(conn, "s-col", event_id=1)
    finally:
        conn.close()
    head_before = _checkpoint(root)["event_id"]
    for _ in range(3):                                       # passes while the read runs
        start = time.monotonic()
        result = observability._run(root)
        assert time.monotonic() - start < 1.0 + 3.0
        assert started.is_set()
        assert result["consumers"]["wiki"] == {"status": "warming", "through_event": head_before}
        assert result["consumers"]["projection"]["status"] in ("ok", "current")
        assert result["preservation"].get("warming") is True and result["preservation"]["failed"] == 0
    checkpoint = _checkpoint(root)
    assert checkpoint["event_id"] == head_before
    assert checkpoint["failures"] == 0 and checkpoint["next_attempt"] is None and checkpoint["error"] is None
    assert not _events(root, "capture-deferred") and not _events(root, "drain-failed")
    assert not [i for i in _open_issues(root) if i["code"] in ("consumer-failed", "snapshot-failed")]
    conn = db.connect_readonly(root)
    try:
        job = dict(conn.execute("SELECT status, failures FROM obs_projection_job").fetchone())
    finally:
        conn.close()
    assert job == {"status": "pending", "failures": 0}

    release.set()
    lane = consumers.LANES["wiki"]
    end = time.monotonic() + 20
    while lane.busy_since() is not None and time.monotonic() < end:
        time.sleep(0.02)
    result = observability._run(root)
    assert result["consumers"]["wiki"]["status"] == "ok"
    conn = db.connect_readonly(root)
    try:
        head = conn.execute("SELECT max(id) FROM events").fetchone()[0]
    finally:
        conn.close()
    assert _checkpoint(root)["event_id"] >= head - 1
    assert not _events(root, "capture-deferred")


def test_a_collect_once_pass_still_captures_without_a_warm_up(tmp_path, monkeypatch):
    """`collect once` is one pass in its own process: it never leaves the lane warming, and reads the
    index inside its capture as before."""
    root = str(tmp_path / "once")
    os.makedirs(root)
    _append(root, "s-once", 3)
    monkeypatch.delenv("ALPACA_COLLECTOR_WATCH", raising=False)
    monkeypatch.setattr(consumers, "LANES", consumers._lanes())
    try:
        assert observability.run_once(root)["consumers"]["wiki"]["status"] == "ok"
        _append(root, "s-once", 2, start=3)
        monkeypatch.setattr(consumers, "LANES", consumers._lanes())
        result = observability.run_once(root)
        assert result["consumers"]["wiki"]["status"] == "ok"
        assert "wiki_lane" not in result
    finally:
        consumers.LANES["wiki"].run(consumers._SHARED.close, budget_s=5.0)


def _watch_args():
    import argparse
    return argparse.Namespace(collect_action="watch", max_records=1000, source_batch=200, event_batch=200,
                              preservation_batch=8, interval=0.1)


def test_a_watch_pass_warms_under_the_no_follow_collector_lock(collector, tmp_path, monkeypatch):
    """op008/followups (t-424 with t-462): `collect watch` takes t-424's no-follow collector lock and
    every pass inside it starts t-462's warm-up. While the pass warms, the lock is held (a second
    collector cannot take it), and a linked `.alpaca/observability` refuses the watch before any
    pass, with nothing made outside the project."""
    import signal
    from alpaca import cli
    from alpaca.observability import cli as obs_cli
    root, release, started = collector
    _append(root, "s-col", 2, start=3)
    monkeypatch.setattr(signal, "signal", lambda *a: None)
    monkeypatch.setattr(cli, "_root", lambda: root)
    real_run = obs_cli._run
    seen = []

    class OnePass(Exception):
        pass

    def one_pass(*a, **k):
        with pytest.raises(BlockingIOError):
            with observability.collector_lock(a[0]):
                pass
        seen.append((k.get("warm"), real_run(*a, **k)))
        raise OnePass

    monkeypatch.setattr(obs_cli, "_run", one_pass)
    with pytest.raises(OnePass):
        obs_cli.cmd_collect(_watch_args())
    warm, result = seen[0]
    assert warm is True and started.is_set()
    assert result["wiki_lane"] == "warming" and result["consumers"]["wiki"]["status"] == "warming"
    with observability.collector_lock(root):           # released when the watch ended
        pass

    linked = tmp_path / "linked"
    (linked / ".alpaca").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, linked / ".alpaca" / "observability", target_is_directory=True)
    monkeypatch.setattr(cli, "_root", lambda: str(linked))
    seen.clear()
    with pytest.raises(OSError):
        obs_cli.cmd_collect(_watch_args())
    assert seen == [] and list(outside.iterdir()) == []


# ------------------------------------------------------------------ a warm-up that fails or hangs
@pytest.fixture
def restarted(tmp_path, monkeypatch):
    """A record with a wiki store and new rows, a fresh pair of collector lanes, watch mode."""
    root = str(tmp_path / "restarted")
    os.makedirs(root)
    _append(root, "s-col", 3)
    monkeypatch.delenv("ALPACA_COLLECTOR_WATCH", raising=False)
    assert observability.run_once(root)["consumers"]["wiki"]["status"] == "ok"
    monkeypatch.setattr(consumers, "LANES", consumers._lanes())
    monkeypatch.setenv("ALPACA_COLLECTOR_WATCH", "1")
    _append(root, "s-col", 4, start=3)
    release = threading.Event()
    try:
        yield root, release
    finally:
        release.set()
        lane = consumers.LANES["wiki"]
        end = time.monotonic() + 20
        while lane.busy_since() is not None and time.monotonic() < end:
            time.sleep(0.05)
        lane.run(consumers._SHARED.close, budget_s=5.0)


def test_a_warm_up_that_fails_after_the_wait_is_recorded_not_warming(restarted, monkeypatch):
    """Review B-1 (probe_warm.py): the echo index read fails after `WARM_WAIT_S`. The pass after the
    failure reports the lane `failed` and runs the capture, which records the failure (drain-failed,
    a consumer-failed issue, a failure count); it never stays `warming`."""
    root, _release = restarted

    def broken(self, rows):
        time.sleep(1.0)
        raise MemoryError("echo index read failed")
    monkeypatch.setattr(EchoIndex, "load", broken)
    statuses = []
    for _ in range(4):
        result = observability._run(root)
        statuses.append((result.get("wiki_lane"), result["consumers"]["wiki"]["status"]))
        time.sleep(1.3)
    assert statuses[0] == ("warming", "warming"), statuses
    assert ("failed", "failed") in statuses, statuses
    assert _checkpoint(root)["failures"] >= 1 and _checkpoint(root)["error"] == "MemoryError"
    assert _events(root, "drain-failed")
    assert [i for i in _open_issues(root) if i["code"] == "consumer-failed"]


def test_a_warm_up_that_hangs_past_the_ceiling_is_recorded(restarted, monkeypatch):
    """Review B-1: a read that never ends is `warming` only up to `WARM_CEILING_S`; then the lane
    answers `busy` and the capture records `capture-deferred` and a consumer failure."""
    root, release = restarted
    monkeypatch.setattr(consumers, "WARM_CEILING_S", 1.0, raising=False)

    def hang(self, rows):
        release.wait(30)
        raise MemoryError("released")
    monkeypatch.setattr(EchoIndex, "load", hang)
    first = observability._run(root)
    assert first["wiki_lane"] == "warming" and first["consumers"]["wiki"]["status"] == "warming"
    assert not _events(root, "capture-deferred")
    time.sleep(1.2)
    later = observability._run(root)
    assert later["wiki_lane"] == "busy", later
    assert later["consumers"]["wiki"]["status"] == "failed" and later["consumers"]["wiki"]["error"] == "CaptureAbandoned"
    assert _events(root, "capture-deferred")
    assert [i for i in _open_issues(root) if i["code"] == "consumer-failed"]
