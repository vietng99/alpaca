"""The live server keeps its port through reloads and does not refold analytics on every poll."""
import os
import socket
import json
import threading
import urllib.request

import pytest

from alpaca import serve


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize('path', ['/hub/overview.json', '/hub/research.json'])
def test_usage_index_pauses_between_sessions_for_page_request(project, monkeypatch, path):
    from alpaca import db, hub
    from alpaca.analytics import metrics_index
    from alpaca.research import view
    for sid in ('first', 'second'):
        conn = db.connect(project)
        db.upsert(conn, 'sessions', 'sid', {'sid': sid, 'started': '2026-09-22T00:00:00Z'})
        conn.close()
    live = serve.Live(project)
    live.data_rev = 'fixture'
    live.data_json = json.dumps({'sessions': [{'sid': sid, 'class': 'work'}
                                            for sid in ('first', 'second')]})
    first_done, next_started = threading.Event(), threading.Event()
    resume_index, page_started, release_page = threading.Event(), threading.Event(), threading.Event()
    original_analysis = metrics_index._analysis_row

    def analysis(root, sid):
        if sid == 'second':
            next_started.set()
        result = original_analysis(root, sid)
        if sid == 'first':
            first_done.set()
            assert resume_index.wait(5)
        return result

    monkeypatch.setattr(metrics_index, '_analysis_row', analysis)
    module, name = (hub, 'overview') if path.endswith('overview.json') else (view, 'runs')
    original_page = getattr(module, name)

    def page(root):
        page_started.set()
        assert release_page.wait(5)
        return original_page(root)

    monkeypatch.setattr(module, name, page)
    httpd = serve._Server(('127.0.0.1', 0), serve.make_handler(live, project))
    httpd.daemon_threads = True
    server = threading.Thread(target=httpd.serve_forever, daemon=True)
    server.start()
    results, failures = {}, []

    def index_job():
        try:
            results['index'] = live.analytics_index()
        except Exception as exc:
            failures.append(exc)

    def page_job():
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d%s' % (httpd.server_port, path), timeout=5) as response:
                results['page'] = response.status
                response.read()
        except Exception as exc:
            failures.append(exc)

    index_thread = threading.Thread(target=index_job, daemon=True)
    page_thread = threading.Thread(target=page_job, daemon=True)
    try:
        index_thread.start()
        assert first_done.wait(5)
        page_thread.start()
        assert page_started.wait(5)
        resume_index.set()
        assert not next_started.wait(.2), 'usage rebuild competes with the active page request'
    finally:
        resume_index.set()
        release_page.set()
        index_thread.join(5)
        if page_thread.ident is not None:
            page_thread.join(5)
        httpd.shutdown()
        httpd.server_close()
    assert not failures
    assert results['page'] == 200
    assert next_started.is_set()
    assert len(json.loads(results['index'][0])['sessions']) == 2


def test_reload_hands_the_listening_socket_to_the_successor(monkeypatch):
    first, port = serve._bind(_free_port())
    try:
        fd = first.socket.fileno()
        monkeypatch.setenv(serve.LISTEN_FD_ENV, str(fd))
        # a client that connects between the exec and the successor's accept loop is queued,
        # not refused
        client = socket.create_connection(("127.0.0.1", port), timeout=2)
        second, second_port = serve._bind(port)
        assert second_port == port
        assert second.socket.fileno() == fd
        assert serve.LISTEN_FD_ENV not in os.environ
        conn, _ = second.socket.accept()
        conn.close()
        client.close()
    finally:
        first.socket.close()


def test_failed_foreground_read_releases_analytics_priority(project):
    live = serve.Live(project)
    with pytest.raises(ValueError):
        with live.foreground_read('/hub/overview.json'):
            raise ValueError('failed page read')
    assert live._foreground_reads == 0, 'failed page read left usage analytics paused'
    resumed = threading.Event()
    worker = threading.Thread(target=lambda: (live.yield_analytics(), resumed.set()), daemon=True)
    worker.start()
    # a paused analytics waits out yield_analytics' 2 s bound; a free one returns at once
    assert resumed.wait(1.5), 'failed page read left usage analytics paused'
    worker.join(1)


def test_stalled_foreground_read_cannot_pause_analytics_indefinitely(project):
    live = serve.Live(project)
    resumed = threading.Event()
    with live.foreground_read('/hub/research.json'):
        worker = threading.Thread(target=lambda: (live.yield_analytics(), resumed.set()), daemon=True)
        worker.start()
        assert resumed.wait(4), 'a stuck page reader permanently stopped usage analytics'
    worker.join(1)


def test_backlog_is_larger_than_the_stdlib_default():
    assert serve._Server.request_queue_size >= 64


def test_strict_port_does_not_drift(monkeypatch):
    holder, port = serve._bind(_free_port())
    try:
        monkeypatch.setenv("ALPACA_SERVE_STRICT_PORT", "1")
        with pytest.raises(OSError):
            serve._bind(port)
        monkeypatch.delenv("ALPACA_SERVE_STRICT_PORT")
        drifted, other = serve._bind(port)
        assert other != port
        drifted.server_close()
    finally:
        holder.server_close()


def test_watcher_folds_at_most_once_per_interval(project, monkeypatch):
    calls = []
    clock = [1000.0]
    signature = ["a"]
    monkeypatch.setattr(serve.time, "time", lambda: clock[0])
    monkeypatch.setattr(serve.Live, "_input_sig", lambda self, *_: signature[0])
    monkeypatch.setattr(serve.build_index, "collect", lambda root: calls.append(1) or [])
    monkeypatch.setattr(serve.build_index, "_fold", lambda root, collected: {"sessions": [], "n": len(calls)})
    live = serve.Live(project)
    live.refresh(throttle=True)
    assert live.data_ready.wait(10) and len(calls) == 1      # the fold runs off the watcher thread
    signature[0] = "b"
    clock[0] += 5
    live.refresh(throttle=True)
    assert len(calls) == 1, "a changed input inside FOLD_MIN_S waits for the next window"
    clock[0] += serve.FOLD_MIN_S
    live.refresh(throttle=True)
    live._fold_thread.join(10)      # data_ready is already set by the first fold, so it cannot signal this one
    assert len(calls) == 2
    signature[0] = "c"
    live.refresh()
    assert len(calls) == 3, "a direct refresh is never throttled"


def test_input_signature_reads_the_event_head_without_scanning_the_chain(project, monkeypatch):
    """t-670: the watcher computes the input signature every POLL_S. Its cost must not grow with
    the number of events: an unindexed MAX(hash) read every event row on every tick."""
    from alpaca import db
    conn = db.connect(project)
    db.append_event(conn, session="s1", actor="agent", kind="heartbeat", data={})
    real = serve.db.connect_readonly
    steps = [0]

    def counted(root):
        reader = real(root)
        reader.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1), 1)
        return reader
    monkeypatch.setattr(serve.db, "connect_readonly", counted)

    def cost():
        steps[0] = 0
        signature = serve._data_sig(project)
        return steps[0], signature
    small, before = cost()
    with db.transaction(conn):
        for _ in range(2000):
            db.append_event(conn, session="s1", actor="agent", kind="heartbeat", data={}, conn_in_txn=True)
    large, after = cost()
    head = conn.execute("SELECT id, hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    assert after != before, "a new event still moves the signature"
    assert small > 0, "the progress handler counted the signature's reads"
    assert large - small < 200, "2000 more events cost %d more SQLite steps" % (large - small)
    assert head[1], "the head row carries the hash the signature reads"


def test_watcher_reads_file_inputs_at_most_every_scan_interval(project, monkeypatch):
    """t-670: a throttled tick reads the transcript stats and the library walk at most every
    SCAN_MIN_S; a direct refresh reads all. t-680: the record part only when the board or the fold
    is due, the transcript stats only when the fold is."""
    from alpaca import db
    monkeypatch.setattr(serve.build_index, "collect", lambda root: [])
    monkeypatch.setattr(serve.build_index, "_fold", lambda root, collected: {"sessions": []})
    conn = db.connect(project)
    transcript = os.path.join(project, ".alpaca", "transcripts", "main.jsonl")
    os.makedirs(os.path.dirname(transcript), exist_ok=True)
    with open(transcript, "w", encoding="utf-8") as fh:
        fh.write("{}\n")
    db.upsert(conn, "sessions", "sid", {"sid": "main", "transcript": transcript})
    stats, walks = [], []
    real_stat, real_walk = serve._stat_revision, serve.scan_library
    monkeypatch.setattr(serve, "_stat_revision", lambda path: stats.append(path) or real_stat(path))
    monkeypatch.setattr(serve, "scan_library", lambda root: walks.append(root) or real_walk(root))
    live = serve.Live(project)
    live.refresh(fold=False)
    assert stats and walks, "a direct refresh reads the files"
    del stats[:], walks[:]
    live.refresh(throttle=True, fold=False)
    assert stats == [] and walks == [], "a throttled tick inside SCAN_MIN_S reads no file"

    db.append_event(conn, session="main", actor="agent", kind="heartbeat", data={})
    live._board_at -= serve.BOARD_MIN_S
    live.refresh(throttle=True, fold=False)
    assert stats == [] and walks == []
    moved = live._pricing_sig
    assert moved == serve._data_sig(project), "a record change moves the signature on the next board tick"
    del stats[:]

    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write("{}\n")
    live.refresh(throttle=True, fold=False)
    assert stats == [] and live._pricing_sig == moved, "a transcript change waits for the next scan"
    live._input_files_at -= serve.SCAN_MIN_S
    live._lib_at -= serve.SCAN_MIN_S
    live.refresh(throttle=True)
    live._fold_thread.join(10)
    assert stats and walks, "past SCAN_MIN_S a throttled tick with the fold due reads the files again"
    del stats[:]
    assert live._pricing_sig != moved and live._pricing_sig == serve._data_sig(project)
    conn.close()


def test_a_transcript_change_alone_does_not_rebuild_the_board(project, monkeypatch):
    """t-670: the board renders from the record and the config files, so a transcript that moved
    (the analytics fold's input) does not rebuild it; a new event still does."""
    from alpaca import db
    conn = db.connect(project)
    transcript = os.path.join(project, ".alpaca", "transcripts", "main.jsonl")
    os.makedirs(os.path.dirname(transcript), exist_ok=True)
    with open(transcript, "w", encoding="utf-8") as fh:
        fh.write("{}\n")
    db.upsert(conn, "sessions", "sid", {"sid": "main", "transcript": transcript})
    builds = []
    real = serve.export.payload
    monkeypatch.setattr(serve.export, "payload", lambda c, *a, **k: builds.append(1) or real(c, *a, **k))
    live = serve.Live(project)
    live.refresh(fold=False)
    assert len(builds) == 1
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write("{}\n")
    live.refresh(fold=False)
    assert len(builds) == 1, "a transcript-only change leaves the board as it is"
    db.append_event(conn, session="main", actor="agent", kind="heartbeat", data={})
    live.refresh(fold=False)
    assert len(builds) == 2, "a new event rebuilds the board"
    conn.close()


def test_an_unwatched_hub_slows_its_watcher_until_a_viewer_returns(project, monkeypatch):
    """t-670: with no stream open and no viewer request for VIEW_GRACE_S, a throttled tick reads
    the files at most every SCAN_IDLE_S and folds at most every FOLD_IDLE_S; a viewer's request
    wakes the watcher, and its next tick folds the inputs that moved."""
    from alpaca import db
    clock = [1000.0]
    monkeypatch.setattr(serve.time, "time", lambda: clock[0])
    folds, stats = [], []
    monkeypatch.setattr(serve.build_index, "collect", lambda root: folds.append(1) or [])
    monkeypatch.setattr(serve.build_index, "_fold", lambda root, collected: {"sessions": [], "n": len(folds)})
    real_stat = serve._stat_revision
    monkeypatch.setattr(serve, "_stat_revision", lambda path: stats.append(path) or real_stat(path))
    conn = db.connect(project)
    transcript = os.path.join(project, ".alpaca", "transcripts", "main.jsonl")
    os.makedirs(os.path.dirname(transcript), exist_ok=True)
    with open(transcript, "w", encoding="utf-8") as fh:
        fh.write("{}\n")
    db.upsert(conn, "sessions", "sid", {"sid": "main", "transcript": transcript})
    live = serve.Live(project)
    live.refresh(throttle=True)
    live._fold_thread.join(10)
    assert len(folds) == 1 and live.watched(), "a fresh server counts as watched for VIEW_GRACE_S"

    clock[0] += serve.VIEW_GRACE_S + serve.FOLD_MIN_S
    assert not live.watched()
    db.append_event(conn, session="main", actor="agent", kind="heartbeat", data={})
    live._input_files_at -= serve.SCAN_MIN_S        # a watched tick would read the files again now
    del stats[:]
    live.refresh(throttle=True)
    assert stats == [], "unwatched, the files wait for SCAN_IDLE_S"
    assert len(folds) == 1, "unwatched, a moved input waits for FOLD_IDLE_S"
    assert not live._wake.is_set()

    live.touch()                                      # a viewer's request
    assert live.watched() and live._wake.is_set(), "the request wakes the watcher"
    live.refresh(throttle=True)
    live._fold_thread.join(10)
    assert len(folds) == 2, "the first watched tick folds what moved"
    conn.close()


def test_first_refresh_serves_the_board_before_the_fold(project, monkeypatch):
    monkeypatch.setattr(serve.build_index, "collect", lambda root: pytest.fail("the fast refresh must not fold"))
    live = serve.Live(project)
    live.refresh(fold=False)
    assert live.board_rev
    assert not live.data_ready.is_set()


def test_dashboard_unit_restarts_on_any_exit_on_its_exact_port(project):
    units = serve.service_units(project, port=7350)
    dashboard = next(body for name, body in units.items() if "dashboard" in name)
    assert "Restart=always" in dashboard
    assert "Environment=ALPACA_SERVE_STRICT_PORT=1" in dashboard


def test_a_reload_successor_does_not_exit_as_already_running(project, monkeypatch):
    calls = []
    monkeypatch.setattr(serve.cli, "_root", lambda: project)
    monkeypatch.setattr(serve, "is_running", lambda root: {"url": "http://127.0.0.1:7350/", "port": 7350, "remote": True})
    monkeypatch.setattr(serve, "files_auth", lambda root: ":pin")
    monkeypatch.setattr(serve, "_run", lambda root, port, keep=False, remote=False: calls.append(port))
    args = type("A", (), {"stop": False, "status": False, "write_services": False, "remote": True,
                          "port": 7350, "detach": False, "keep": True})()
    serve.cmd_serve(args)
    assert calls == [], "without a handed-over socket a live server means already running"
    monkeypatch.setenv(serve.LISTEN_FD_ENV, "99")
    serve.cmd_serve(args)
    assert calls == [7350]


# ---- the In-flight snapshot is served stale-while-revalidate (t-574) ----

def _fake_snapshots(monkeypatch, gate=None, fail=False):
    from alpaca.analytics import inflight
    calls = []

    def snapshot(root, *a, **k):
        calls.append(1)
        if gate is not None:
            assert gate.wait(10)
        if fail and len(calls) > 1:
            raise RuntimeError("read failed")
        return {"n": len(calls)}
    monkeypatch.setattr(inflight, "snapshot", snapshot)
    return calls


def _wait_idle(live):
    for _ in range(200):
        with live._inflight_lock:
            if not live._inflight[2]:
                return
        threading.Event().wait(0.02)
    raise AssertionError("the background rebuild never finished")


def test_inflight_first_read_builds_and_a_fresh_one_is_reused(project, monkeypatch):
    calls = _fake_snapshots(monkeypatch)
    live = serve.Live(project)
    assert live.inflight() == {"n": 1} and live.inflight() == {"n": 1}
    assert len(calls) == 1


def test_inflight_stale_snapshot_is_served_at_once_and_one_rebuild_starts(project, monkeypatch):
    live = serve.Live(project)
    _fake_snapshots(monkeypatch)
    live.inflight()
    gate = threading.Event()
    calls = _fake_snapshots(monkeypatch, gate=gate)
    live._inflight[0] -= serve.INFLIGHT_S + 1
    got = []
    readers = [threading.Thread(target=lambda: got.append(live.inflight())) for _ in range(12)]
    for t in readers:
        t.start()
    for t in readers:
        t.join(2)
    assert got == [{"n": 1}] * 12                 # nobody waited for the blocked rebuild
    threading.Event().wait(0.2)
    assert len(calls) == 1                        # twelve readers, one rebuild
    gate.set()
    _wait_idle(live)
    assert live.inflight() == {"n": 1} and len(calls) == 1


def test_inflight_past_the_age_cap_a_reader_waits_for_a_build(project, monkeypatch):
    live = serve.Live(project)
    _fake_snapshots(monkeypatch)
    live.inflight()
    calls = _fake_snapshots(monkeypatch)
    live._inflight[0] -= serve.INFLIGHT_MAX_S + 1
    assert live.inflight() == {"n": 1} and len(calls) == 1      # the new build, not the old snapshot
    assert live.inflight() == {"n": 1}


def test_inflight_failed_rebuild_keeps_the_old_snapshot_and_backs_off(project, monkeypatch):
    live = serve.Live(project)
    calls = _fake_snapshots(monkeypatch, fail=True)
    assert live.inflight() == {"n": 1}
    live._inflight[0] -= serve.INFLIGHT_S + 1
    assert live.inflight() == {"n": 1}
    _wait_idle(live)
    assert len(calls) == 2 and live._inflight[1] == {"n": 1}
    assert live.inflight() == {"n": 1} and len(calls) == 2      # inside the back-off: no new attempt


def _watched_project(project):
    from alpaca import db
    conn = db.connect(project)
    transcript = os.path.join(project, ".alpaca", "transcripts", "main.jsonl")
    os.makedirs(os.path.dirname(transcript), exist_ok=True)
    with open(transcript, "w", encoding="utf-8") as fh:
        fh.write("{}\n")
    db.upsert(conn, "sessions", "sid", {"sid": "main", "transcript": transcript})
    return conn, transcript


def test_a_watched_tick_reads_the_inputs_only_when_the_board_or_the_fold_is_due(project, monkeypatch):
    """t-680: with busy sessions on the host the record moves every second, so every 0.7 s tick of
    a watched hub read the record parts (12380 meta rows on the live record) and every SCAN_MIN_S
    one stat'ed every transcript, while the board they feed is rebuilt at most every BOARD_MIN_S
    and the fold runs at most every FOLD_MIN_S. A tick now reads the record only when the board or
    the fold is due, and the transcript stats only when the fold is due."""
    from alpaca import db
    clock = [1000.0]
    monkeypatch.setattr(serve.time, "time", lambda: clock[0])
    folds, records, stats, builds = [], [], [], []
    monkeypatch.setattr(serve.build_index, "collect", lambda root: folds.append(1) or [])
    monkeypatch.setattr(serve.build_index, "_fold", lambda root, collected: {"sessions": [], "n": len(folds)})
    real_record, real_stat, real_payload = serve._record_parts, serve._stat_revision, serve.export.payload
    monkeypatch.setattr(serve, "_record_parts", lambda conn: records.append(1) or real_record(conn))
    monkeypatch.setattr(serve, "_stat_revision", lambda path: stats.append(path) or real_stat(path))
    monkeypatch.setattr(serve.export, "payload", lambda c, *a, **k: builds.append(1) or real_payload(c, *a, **k))
    conn, transcript = _watched_project(project)
    live = serve.Live(project)
    live.refresh(throttle=True)
    live._fold_thread.join(10)
    assert len(folds) == 1 and len(builds) == 1 and live.watched()

    def tick(advance):
        clock[0] += advance
        del records[:], stats[:]
        live.refresh(throttle=True)
        if live._fold_thread is not None:
            live._fold_thread.join(10)

    db.append_event(conn, session="main", actor="agent", kind="heartbeat", data={})
    live._input_files_at -= serve.SCAN_MIN_S
    tick(serve.SCAN_MIN_S)
    assert records == [] and stats == [] and len(builds) == 1, "neither the board nor the fold is due"
    tick(serve.BOARD_MIN_S)
    assert records and stats == [] and len(builds) == 2, "the board is due: the record is read, the files are not"
    assert len(folds) == 1
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write("{}\n")
    tick(1)
    assert records == [] and stats == [] and len(builds) == 2
    live._input_files_at -= serve.SCAN_MIN_S
    tick(serve.FOLD_MIN_S)
    assert records and stats and len(folds) == 2, "the fold is due: every input is read and the fold runs"
    assert live._data_sig == serve._data_sig(project)
    conn.close()


def test_an_unwatched_hub_rebuilds_the_board_at_most_every_board_idle_interval(project, monkeypatch):
    """t-680: an unwatched hub rebuilt the board every BOARD_MIN_S (0.34 s of CPU each on the live
    record, the largest share of its idle cost) because busy sessions move the record all the
    time, yet no page is open to take the push. Unwatched it waits BOARD_IDLE_S; a viewer's return
    wakes the watcher and its first tick rebuilds what moved."""
    from alpaca import db
    clock = [1000.0]
    monkeypatch.setattr(serve.time, "time", lambda: clock[0])
    builds = []
    real_payload = serve.export.payload
    monkeypatch.setattr(serve.export, "payload", lambda c, *a, **k: builds.append(1) or real_payload(c, *a, **k))
    conn, _transcript = _watched_project(project)
    live = serve.Live(project)
    live.refresh(throttle=True, fold=False)
    assert len(builds) == 1
    clock[0] += serve.VIEW_GRACE_S
    assert not live.watched()
    db.append_event(conn, session="main", actor="agent", kind="heartbeat", data={})
    live.refresh(throttle=True, fold=False)
    assert len(builds) == 2, "past BOARD_IDLE_S a moved record rebuilds the board"
    db.append_event(conn, session="main", actor="agent", kind="heartbeat", data={})
    clock[0] += serve.BOARD_MIN_S
    live.refresh(throttle=True, fold=False)
    assert len(builds) == 2, "unwatched, a moved record waits for BOARD_IDLE_S"
    clock[0] += serve.BOARD_IDLE_S - serve.BOARD_MIN_S
    live.refresh(throttle=True, fold=False)
    assert len(builds) == 3
    db.append_event(conn, session="main", actor="agent", kind="heartbeat", data={})
    live.touch()
    live.refresh(throttle=True, fold=False)
    assert len(builds) == 3, "inside BOARD_MIN_S of the last build"
    clock[0] += serve.BOARD_MIN_S
    live.refresh(throttle=True, fold=False)
    assert len(builds) == 4, "watched again, the board keeps BOARD_MIN_S"
    conn.close()


def test_a_tick_reads_the_board_liveness_without_serializing_the_board(project, monkeypatch):
    """t-680: every tick took board_snapshot() (parse the 878 KB board, overlay liveness, serialize
    it twice) only to see whether a work session's liveness flipped: 5.8 ms a tick, 1.1% of a
    core at the watched cadence. The tick now reads the liveness of the work sessions alone, parsed
    once per board; a flip still moves the board revision and pushes."""
    clock = [1000.0]
    monkeypatch.setattr(serve.time, "time", lambda: clock[0])
    live = serve.Live(project)
    live.refresh(throttle=True, fold=False)
    live.board_json = json.dumps({"pulse": {"now": {"sessions": {"work": [
        {"sid": "s1", "last_beat": "1970-01-01T00:16:40+00:00", "ended": None, "active": True}]}}}})
    live.board_rev = "fixture-board"
    snapshots = []
    real = serve.Live.board_snapshot
    monkeypatch.setattr(serve.Live, "board_snapshot", lambda self: snapshots.append(1) or real(self))
    live.refresh(throttle=True, fold=False)
    generation, revision = live.gen, json.loads(live.rev_blob())["board"]
    del snapshots[:]
    clock[0] += 1
    live.refresh(throttle=True, fold=False)
    assert snapshots == [] and live.gen == generation, "nothing flipped: no push, no serialization"
    clock[0] += 600
    live.refresh(throttle=True, fold=False)
    assert snapshots == [] and live.gen > generation, "the session went quiet: a push"
    assert json.loads(live.rev_blob())["board"] != revision
