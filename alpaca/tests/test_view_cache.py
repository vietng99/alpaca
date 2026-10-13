"""t-584: /hub/research.json and /hub/mission.json answer from a cache while their inputs hold.

The cache is serve.Live.view, keyed on the filtered record head (_overview_head), the stamps the
view reads outside the record (research.view.source_stamp, project.yaml) and the OVERVIEW_S age
bound. Also: project_cfg.load is memoized by file stamp, a mission snapshot is not decoded whole, and
the malformed-JSON branch of jobs.events_of stays on its partial index."""
import copy
import json
import os
import random
import threading
import time

import pytest

from alpaca import db, mission, serve
from alpaca import project as project_cfg
from alpaca.research import jobs, view
from alpaca.tests.plain_op import plain_op
from alpaca.tests.test_mission import by_id, definition


@pytest.fixture(autouse=True)
def blocking_views(monkeypatch):
    """The key and age tests below read a rebuilt view at once: no stale body is served. The
    stale-while-revalidate tests turn VIEW_STALE_S back on."""
    monkeypatch.setattr(serve, "VIEW_STALE_S", 0)


def mapped_project(tmp_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    for name in ("parser", "usage", "view"):
        (root / "src" / (name + ".txt")).write_text(name)
    mission.define(root, definition(), session="owner", expected_revision=0)
    return root


def drop_stamp(body):
    value = json.loads(body)
    value.pop("checked_at", None)
    return value


def test_project_load_is_memoized_by_file_stamp(tmp_path):
    root = str(tmp_path)
    assert project_cfg.load(root) == {}
    project_cfg.save(root, {"resource_class": "small", "budget": {"totals": {"small": 7}}})
    first = project_cfg.load(root)
    first["budget"]["totals"]["small"] = 99                  # a caller's edit never reaches the cache
    assert project_cfg.load(root)["budget"]["totals"]["small"] == 7
    with open(project_cfg.path(root), "a", encoding="utf-8") as fh:
        fh.write("extra: 1\n")                                # same inode, new size
    assert project_cfg.load(root)["extra"] == 1
    calls = []
    import yaml
    real = yaml.safe_load
    yaml.safe_load = lambda *a, **k: calls.append(1) or real(*a, **k)
    try:
        for _ in range(20):
            project_cfg.load(root)
    finally:
        yaml.safe_load = real
    assert calls == []                                       # twenty more loads parse nothing


def test_mission_view_equals_uncached_and_follows_the_record(tmp_path):
    root = mapped_project(tmp_path)
    live = serve.Live(str(root))
    body, etag = live.mission_view(None)
    assert drop_stamp(body) == {k: v for k, v in mission.project(str(root)).items() if k != "checked_at"}
    assert live.mission_view(None) == (body, etag)           # unchanged inputs: the same bytes
    # a mission check is a new event: the next read shows it
    result = mission.run_check(root, "parser", "behavior", ["python3", "-c", "pass"], session="owner")
    assert result["result"] == "PASS"
    after, _ = live.mission_view(None)
    assert by_id(json.loads(after), "parser")["state"] == "verified"
    assert by_id(json.loads(body), "parser")["state"] != "verified"


def test_mission_view_ignores_quiet_events_and_sees_a_project_edit(tmp_path, monkeypatch):
    root = mapped_project(tmp_path)
    conn = db.connect(str(root))
    calls = []
    real = mission.project
    monkeypatch.setattr(mission, "project", lambda *a, **k: calls.append(1) or real(*a, **k))
    live = serve.Live(str(root))
    try:
        live.mission_view(None)
        for kind in serve.OVERVIEW_QUIET_KINDS:
            db.append_event(conn, session="s", actor="a", kind=kind, data={})
        live.mission_view(None)
        assert len(calls) == 1
        project_cfg.save(str(root), {"resource_class": "large"})  # project.yaml changed
        live.mission_view(None)
        assert len(calls) == 2
        project_cfg.save(str(root), {"resource_class": "small"})  # and again, same length
        live.mission_view(None)
        assert len(calls) == 3
    finally:
        conn.close()


def test_mission_view_sees_a_source_file_change_after_the_age_bound(tmp_path, monkeypatch):
    root = mapped_project(tmp_path)
    live = serve.Live(str(root))
    mission.run_check(root, "view", "behavior", ["python3", "-c", "pass"], session="owner")
    first, _ = live.mission_view(None)
    assert by_id(json.loads(first), "view")["state"] == "verified"
    (root / "src" / "parser.txt").write_text("changed contract")
    assert live.mission_view(None)[0] == first               # sources are not stamped: held for OVERVIEW_S
    monkeypatch.setattr(serve, "OVERVIEW_S", 0)
    assert by_id(json.loads(live.mission_view(None)[0]), "view")["state"] == "stale"


def test_mission_slim_snapshots_give_the_same_projection(tmp_path, monkeypatch):
    """The fold reads a snapshot's changed and initialized lists only; the cut-down payload must give
    the output the whole payload gives."""
    root = mapped_project(tmp_path)
    mission.run_check(root, "parser", "behavior", ["python3", "-c", "pass"], session="owner")
    (root / "src" / "usage.txt").write_text("edited")
    mission.scan(root, session="owner")
    got = mission.project(str(root))
    whole = mission._event
    # the reference decodes every payload whole (the query's extra slim column is dropped)
    monkeypatch.setattr(mission, "_event", lambda row, slim=False: {k: v for k, v in whole(row).items() if k != "slim"})
    reference = mission.project(str(root))
    got.pop("checked_at"), reference.pop("checked_at")
    assert got == reference
    assert any(n["last_changed"] for n in got["nodes"])


def test_mission_historical_view_is_cached_per_snapshot(tmp_path):
    root = mapped_project(tmp_path)
    live = serve.Live(str(root))
    at = mission.project(str(root))["snapshots"][0]["id"]
    one = live.mission_view(str(at))
    assert live.mission_view(str(at)) == one
    with pytest.raises(ValueError):
        live.mission_view("0")                               # a bad snapshot is an error, never cached
    assert ("mission", "0") not in live._views


def research_root(project_root):
    from alpaca.tests.test_research_view import _conn, _start
    conn = _conn(project_root)
    _start(conn)
    return conn


def test_research_view_equals_uncached_and_ignores_quiet_events(project):
    conn = research_root(project)
    root = str(project)
    live = serve.Live(root)
    try:
        body, etag = live.research_view(None)
        assert json.loads(body) == json.loads(json.dumps(view.runs(root)))
        for kind in serve.OVERVIEW_QUIET_KINDS:
            db.append_event(conn, session="s", actor="a", kind=kind, data={})
        assert live.research_view(None) == (body, etag)
        one = live.research_view("rr-001")
        assert json.loads(one[0]) == json.loads(json.dumps(view.run(root, "rr-001")))
        assert live.research_view("rr-009") is None          # unknown run: no body, nothing cached
        assert ("research", "rr-009") not in live._views
    finally:
        conn.close()


def test_research_view_sees_a_new_event_a_run_file_and_a_project_edit(project, monkeypatch):
    conn = research_root(project)
    root = str(project)
    calls = []
    real = view.runs
    monkeypatch.setattr(view, "runs", lambda r: calls.append(1) or real(r))
    live = serve.Live(root)
    try:
        live.research_view(None)
        live.research_view(None)
        assert len(calls) == 1
        from alpaca.research import events
        events.emit(conn, events.STOP, run="rr-001", ref="rr-001", op="op-001", session="research-rr-001",
                    data={"reason": "owner", "final": False})
        live.research_view(None)
        assert len(calls) == 2                               # a research event
        run_dir = os.path.join(root, ".alpaca", "research", "rr-001")
        os.makedirs(run_dir, exist_ok=True)
        live.research_view(None)
        before = len(calls)
        with open(os.path.join(run_dir, "runner.lock"), "w") as fh:
            fh.write("1")
        live.research_view(None)
        assert len(calls) == before + 1                      # a run file (the runner probe)
        live.research_view(None)
        assert len(calls) == before + 1
        project_cfg.save(root, {"resource_class": "large"})
        live.research_view(None)
        assert len(calls) == before + 2                      # project.yaml
        monkeypatch.setattr(serve, "OVERVIEW_S", 0)
        live.research_view(None)
        assert len(calls) == before + 3                      # the age bound
    finally:
        conn.close()


def test_view_waiters_reuse_the_computation_in_flight(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    db.connect(str(root)).close()
    live = serve.Live(str(root))
    calls, started, release, results = [], threading.Event(), threading.Event(), []

    def slow():
        calls.append(1)
        started.set()
        release.wait(5)
        return {"n": len(calls)}
    key = ("same",)
    first = threading.Thread(target=lambda: results.append(live.view("v", "a", key, slow)))
    first.start()
    assert started.wait(5)
    waiter = threading.Thread(target=lambda: results.append(live.view("v", "a", key, slow)))
    waiter.start()
    time.sleep(0.2)
    release.set()
    first.join(5)
    waiter.join(5)
    assert len(calls) == 1 and len(results) == 2 and results[0] == results[1]
    assert live.view("v", "a", None, slow) is not None and len(calls) == 2   # a None key is never reused


def test_view_cache_is_bounded(tmp_path):
    live = serve.Live(str(tmp_path))
    for n in range(serve.VIEW_KEEP + 10):
        live.view("v", str(n), ("k",), lambda n=n: {"n": n})
    assert len(live._views) == serve.VIEW_KEEP and set(live._view_locks) == set(live._views)


def test_job_events_malformed_branch_stays_off_the_kind_scan(project):
    conn = research_root(project)
    try:
        db.append_event(conn, session="s", actor="a", kind="research-job", ref="x", data={"scope": "rr-001", "job": "j1"})
        assert [e["data"]["job"] for e in jobs.events_of(conn, "rr-001", jobs.PROTOCOL_KINDS)] == ["j1"]
        marks = ",".join("?" for _ in jobs.PROTOCOL_KINDS)
        plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM events WHERE NOT json_valid(data) AND"
                            " CASE WHEN +kind IN (%s) THEN json_extract(data, '$.scope') END=? AND id>?" % marks,
                            list(jobs.PROTOCOL_KINDS) + ["rr-001", 0]).fetchall()
        assert "idx_events_invalid_json_id" in " ".join(r[3] for r in plan)
    finally:
        conn.close()


ODD_SNAPSHOTS = [
    {"changed": "parser", "initialized": []},          # a string, not a list
    {"changed": None, "initialized": None},            # nulls
    ["not", "an", "object"],                           # valid JSON, not an object
    None,                                              # JSON null
    {"changed": ["parser"], "initialized": ["parser"], "result": "PASS", "node": "parser"},
    {"changed": "[]"},                                 # a string holding JSON
]


@pytest.mark.parametrize("payload", ODD_SNAPSHOTS)
def test_mission_odd_snapshot_rows_read_as_the_whole_decode_reads_them(tmp_path, monkeypatch, payload):
    """A snapshot row the cut-down read cannot take is decoded whole: the projection, or the error it
    raises, is what decoding every payload gives (the old code)."""
    root = mapped_project(tmp_path)
    conn = db.connect(str(root))
    latest = mission._latest(conn, "snapshot")
    data = dict(latest["data"], **payload) if isinstance(payload, dict) else payload
    ev = db.append_event(conn, session="owner", actor="owner", kind="mission-snapshot", data={"x": 1})
    conn.execute("UPDATE events SET data=? WHERE id=?", (json.dumps(data), ev["id"]))
    db.append_event(conn, session="owner", actor="owner", kind="mission-snapshot", data=latest["data"])
    conn.commit()
    conn.close()

    def outcome():
        try:
            got = mission.project(str(root))
            got.pop("checked_at")
            return "ok", got
        except Exception as exc:
            return "err", type(exc).__name__, str(exc)
    new = outcome()
    whole = mission._event
    monkeypatch.setattr(mission, "_event", lambda row, slim=False: {k: v for k, v in whole(row).items() if k != "slim"})
    assert new == outcome()


def test_view_locks_stay_bounded_for_unknown_keys(tmp_path):
    live = serve.Live(str(tmp_path))
    for n in range(500):
        assert live.view("research", "rr-%03d" % n, ("k",), lambda: None) is None
    assert len(live._view_locks) == 0 and len(live._views) == 0
    with pytest.raises(ValueError):
        live.view("mission", "bad", ("k",), lambda: (_ for _ in ()).throw(ValueError("bad")))
    assert len(live._view_locks) == 0


def test_eviction_never_frees_a_slot_that_is_computing(tmp_path):
    live = serve.Live(str(tmp_path))
    calls, started, release = [], threading.Event(), threading.Event()

    def slow():
        calls.append(1)
        started.set()
        release.wait(5)
        return {"slow": 1}
    results = []
    first = threading.Thread(target=lambda: results.append(live.view("v", "slow", ("k",), slow)))
    first.start()
    assert started.wait(5)
    for n in range(serve.VIEW_KEEP + 5):                 # fill and overflow the table while it computes
        live.view("v", str(n), ("k",), lambda n=n: {"n": n})
    waiter = threading.Thread(target=lambda: results.append(live.view("v", "slow", ("k",), slow)))
    waiter.start()
    time.sleep(0.2)
    release.set()
    first.join(5)
    waiter.join(5)
    assert len(calls) == 1 and len(results) == 2 and results[0] == results[1]
    assert len(live._views) <= serve.VIEW_KEEP and set(live._view_locks) == set(live._views)


def test_view_age_runs_from_the_start_of_the_compute(tmp_path, monkeypatch):
    live = serve.Live(str(tmp_path))
    clock = [100.0]
    monkeypatch.setattr(serve.time, "monotonic", lambda: clock[0])

    def slow():
        clock[0] += 10                                   # the compute takes 10 s of the 15 s bound
        return {"n": 1}
    live.view("v", "a", ("k",), slow)
    clock[0] += 4                                        # 14 s after the inputs were read: still held
    calls = []
    live.view("v", "a", ("k",), lambda: calls.append(1) or {"n": 2})
    assert calls == []
    clock[0] += 2                                        # 16 s: read again
    live.view("v", "a", ("k",), lambda: calls.append(1) or {"n": 2})
    assert calls == [1]


def wait_for(check, seconds=5):
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(0.005)
    return check()


def consistent(live):
    """Every stored view has a lock entry; no lock entry is left with 0 callers and no view."""
    with live._view_guard:
        for slot in live._views:
            assert slot in live._view_locks, ("view without lock", slot)
        for slot, held in live._view_locks.items():
            assert held[1] >= 0, ("negative caller count", slot, held[1])
            if held[1] == 0:
                assert slot in live._views, ("orphan lock", slot)
        assert set(live._view_retry) <= set(live._views)


def fake_clock(monkeypatch, start=100.0):
    clock = [start]
    monkeypatch.setattr(serve.time, "monotonic", lambda: clock[0])
    return clock


def test_aged_body_is_served_at_once_and_n_readers_cause_one_rebuild(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "VIEW_STALE_S", 60)
    live = serve.Live(str(tmp_path))
    clock = fake_clock(monkeypatch)
    live.view("v", "a", ("k",), lambda: {"n": 1})
    clock[0] += serve.OVERVIEW_S + 1                       # only the clock moved: the key is the same
    calls, started, release = [], threading.Event(), threading.Event()

    def slow():
        calls.append(1)
        started.set()
        release.wait(5)
        return {"n": 2}
    got = [live.view("v", "a", ("k",), slow) for _ in range(20)]
    assert started.wait(5)
    assert all(json.loads(g[0]) == {"n": 1} for g in got)  # every reader got the old body, none waited
    release.set()
    assert wait_for(lambda: json.loads(live.view("v", "a", ("k",), slow)[0]) == {"n": 2})
    assert calls == [1]                                    # one rebuild for all of them
    assert wait_for(lambda: not live._view_building)


def test_a_key_that_moved_is_computed_for_the_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "VIEW_STALE_S", 60)
    live = serve.Live(str(tmp_path))
    live.view("v", "a", ("k1",), lambda: {"n": 1})
    assert json.loads(live.view("v", "a", ("k2",), lambda: {"n": 2})[0]) == {"n": 2}   # not the old body
    assert live._views[("v", "a")][1] == ("k2",) and not live._view_building


def test_body_past_the_stale_cap_makes_the_reader_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "VIEW_STALE_S", 60)
    live = serve.Live(str(tmp_path))
    clock = fake_clock(monkeypatch)
    live.view("v", "a", ("k",), lambda: {"n": 1})
    clock[0] += 61
    release, done = threading.Event(), []

    def slow():
        release.wait(5)
        return {"n": 2}
    reader = threading.Thread(target=lambda: done.append(live.view("v", "a", ("k",), slow)))
    reader.start()
    time.sleep(0.3)
    assert done == []                                    # waiting for the build, not served the old body
    release.set()
    reader.join(5)
    assert json.loads(done[0][0]) == {"n": 2}


def test_failed_rebuild_keeps_the_old_body_then_raises_past_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "VIEW_STALE_S", 60)
    live = serve.Live(str(tmp_path))
    clock = fake_clock(monkeypatch)
    live.view("v", "a", ("k",), lambda: {"n": 1})
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("record busy")
    clock[0] += 20
    assert json.loads(live.view("v", "a", ("k",), boom)[0]) == {"n": 1}
    assert wait_for(lambda: calls == [1] and not live._view_building)
    assert live.failures["view_refresh"]["count"] == 1
    assert json.loads(live.view("v", "a", ("k",), boom)[0]) == {"n": 1}
    time.sleep(0.1)
    assert calls == [1]                                  # backed off: no second try yet
    clock[0] += serve.VIEW_RETRY_S + 1
    assert json.loads(live.view("v", "a", ("k",), boom)[0]) == {"n": 1}
    assert wait_for(lambda: calls == [1, 1] and not live._view_building)
    clock[0] += 61                                       # past the cap from the input read
    with pytest.raises(RuntimeError):
        live.view("v", "a", ("k",), boom)
    assert json.loads(live.view("v", "a", ("k",), lambda: {"n": 9})[0]) == {"n": 9}


def test_fresh_reader_does_not_wait_behind_a_background_rebuild(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "VIEW_STALE_S", 60)
    live = serve.Live(str(tmp_path))
    clock = fake_clock(monkeypatch)
    live.view("v", "a", ("k1",), lambda: {"n": 1})
    clock[0] += serve.OVERVIEW_S + 1
    release, started = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(10)
        return {"n": 2}
    live.view("v", "a", ("k1",), slow)                   # aged: starts the background build
    assert started.wait(5)
    clock[0] -= serve.OVERVIEW_S                         # a body still fresh for another reader's key
    live._views[("v", "a")] = (clock[0], ("k1",), b"{}", "e", clock[0])
    done = []
    reader = threading.Thread(target=lambda: done.append(live.view("v", "a", ("k1",), slow)), daemon=True)
    reader.start()
    reader.join(1.0)
    release.set()
    assert done, "a reader with a fresh key blocked on the slot lock behind the rebuild"
    assert wait_for(lambda: not live._view_building)


def test_older_key_reader_never_relabels_a_newer_body(tmp_path, monkeypatch):
    """R2: a reader whose key read was slow (it read k1, then the head moved and a k2 reader stored)
    enters its compute late. view() stamps key_at before it calls the key function, so the older
    read is known to be older and never replaces the newer body."""
    monkeypatch.setattr(serve, "VIEW_STALE_S", 60)
    live = serve.Live(str(tmp_path))
    live.view("v", "a", ("k0",), lambda: {"n": 0})
    calls, read_k1, go = [], threading.Event(), threading.Event()

    def build():
        calls.append(1)
        return {"n": len(calls)}

    def slow_key():
        read_k1.set()
        go.wait(5)                                       # the stamp is slow under contention
        return ("k1",)
    got = []
    older = threading.Thread(target=lambda: got.append(live.view("v", "a", slow_key, build)), daemon=True)
    older.start()
    assert read_k1.wait(5)
    live.view("v", "a", ("k2",), build)                  # a reader that read k2 later stores first
    stored = len(calls)
    go.set()
    older.join(5)
    assert live._views[("v", "a")][1] == ("k2",), "an older key replaced the newer key's body"
    assert len(calls) == stored, "the older reader recomputed although a newer body was stored"
    before = len(calls)
    live.view("v", "a", ("k2",), build)                  # no record change since k2: served, not rebuilt
    assert len(calls) == before and got


def test_key_move_reader_does_not_pay_for_a_background_rebuild(tmp_path, monkeypatch):
    """R1: the record moves while an age-triggered background build runs. The reader with the new key
    waits for the rest of the running build and then one build of its own (one compute per slot at a
    time); the older build never replaces its body."""
    monkeypatch.setattr(serve, "VIEW_STALE_S", 60)
    monkeypatch.setattr(serve, "OVERVIEW_S", 0.2)
    live = serve.Live(str(tmp_path))
    live.view("v", "a", ("k1",), lambda: {"n": 1})
    time.sleep(0.25)                                     # aged, key unchanged
    calls = []

    def build():
        calls.append(1)
        n = len(calls)
        time.sleep(1.0)                                  # stands for the 10-12 s build
        return {"n": n + 1}
    assert json.loads(live.view("v", "a", ("k1",), build)[0]) == {"n": 1}   # served old, rebuild starts
    assert wait_for(lambda: calls == [1])
    t0 = time.monotonic()
    got = live.view("v", "a", ("k2",), build)            # the head moved
    waited = time.monotonic() - t0
    assert waited < 2.5, "the reader waited for more than the rest of the background build plus one build"
    assert wait_for(lambda: not live._view_building)
    assert live._views[("v", "a")][1] == ("k2",) and json.loads(got[0]) == {"n": 3}


def test_readers_of_one_moved_key_share_one_build(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "VIEW_STALE_S", 60)
    live = serve.Live(str(tmp_path))
    live.view("v", "a", ("k1",), lambda: {"n": 1})
    calls, release = [], threading.Event()

    def build():
        calls.append(1)
        release.wait(5)
        return {"n": 2}
    results = []
    readers = [threading.Thread(target=lambda: results.append(live.view("v", "a", ("k2",), build))) for _ in range(8)]
    [r.start() for r in readers]
    assert wait_for(lambda: calls)
    time.sleep(0.2)
    release.set()
    [r.join(5) for r in readers]
    assert len(calls) == 1 and len(results) == 8 and all(r == results[0] for r in results)


def test_background_build_reads_the_key_itself(tmp_path, monkeypatch):
    """The aged build stores the key current when it started, not the key of the reader that began it."""
    monkeypatch.setattr(serve, "VIEW_STALE_S", 60)
    live = serve.Live(str(tmp_path))
    clock = fake_clock(monkeypatch)
    now = [("k1",)]
    live.view("v", "a", lambda: now[0], lambda: {"n": 1})
    clock[0] += serve.OVERVIEW_S + 1
    seen = []

    def key():
        seen.append(now[0])
        return now[0]
    live.view("v", "a", key, lambda: {"n": 2})           # aged with k1: the background build starts
    assert wait_for(lambda: not live._view_building)
    assert len(seen) >= 2                                # the reader's read and the build's own read
    assert live._views[("v", "a")][1] == now[0]


def test_age_cap_holds_under_failures_and_a_moving_key(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "VIEW_STALE_S", 0.6)
    monkeypatch.setattr(serve, "OVERVIEW_S", 0.2)
    monkeypatch.setattr(serve, "VIEW_RETRY_S", 0.1)
    live = serve.Live(str(tmp_path))
    rng = random.Random(7)
    head = [0]
    violations, peak_threads = [], [0]
    stop = time.monotonic() + 4

    def compute():
        t = time.monotonic()
        time.sleep(rng.random() * 0.3)
        if rng.random() < 0.5:
            raise RuntimeError("busy")
        return {"t": t}

    def reader(slot):
        while time.monotonic() < stop:
            try:
                got = live.view("v", slot, (head[0] // 40,), compute)   # the key moves every ~2 s of reads
            except RuntimeError:
                continue
            if time.monotonic() - json.loads(got[0])["t"] > serve.VIEW_STALE_S + 0.05:
                violations.append(1)
            peak_threads[0] = max(peak_threads[0], sum(1 for t in threading.enumerate() if t.name.startswith("view-")))
            head[0] += 1
            time.sleep(0.01)
    threads = [threading.Thread(target=reader, args=(s,)) for s in ("a", "a", "b", "c")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert wait_for(lambda: not live._view_building, 5)
    assert not violations and peak_threads[0] <= 3
    consistent(live)


def test_eviction_skips_a_rebuilding_slot_and_tables_stay_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "VIEW_STALE_S", 60)
    monkeypatch.setattr(serve, "VIEW_KEEP", 2)
    live = serve.Live(str(tmp_path))
    clock = fake_clock(monkeypatch)
    live.view("v", "a", ("k",), lambda: {"s": "a1"})
    clock[0] += serve.OVERVIEW_S + 1
    release, started = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(10)
        return {"s": "a2"}
    live.view("v", "a", ("k",), slow)
    assert started.wait(5)
    for s in "bcde":
        clock[0] += 1
        live.view("v", s, ("k",), lambda s=s: {"s": s})
    assert ("v", "a") in live._views, "the rebuilding slot was evicted"
    release.set()
    assert wait_for(lambda: not live._view_building)
    live.view("v", "f", ("k",), lambda: {"s": "f"})
    assert len(live._views) <= serve.VIEW_KEEP
    consistent(live)


def test_rebuild_to_none_and_retry_table_do_not_leak(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "VIEW_STALE_S", 60)
    monkeypatch.setattr(serve, "VIEW_KEEP", 2)
    live = serve.Live(str(tmp_path))
    clock = fake_clock(monkeypatch)
    live.view("v", "gone", ("k",), lambda: {"n": 1})
    clock[0] += serve.OVERVIEW_S + 1
    live.view("v", "gone", ("k",), lambda: None)           # the background build finds the run gone
    assert wait_for(lambda: not live._view_building)
    assert ("v", "gone") not in live._views and ("v", "gone") not in live._view_locks
    for i in range(20):                                    # 20 slots, each with a failed rebuild, then evicted
        live.view("v", i, ("k",), lambda: {"n": 1})
        clock[0] += serve.OVERVIEW_S + 1
        live.view("v", i, ("k",), lambda: (_ for _ in ()).throw(RuntimeError("x")))
        assert wait_for(lambda: not live._view_building)
    consistent(live)
    assert len(live._view_retry) <= serve.VIEW_KEEP


def test_thread_start_failure_does_not_wedge_the_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "VIEW_STALE_S", 60)
    live = serve.Live(str(tmp_path))
    clock = fake_clock(monkeypatch)
    live.view("v", "a", ("k",), lambda: {"n": 1})
    clock[0] += serve.OVERVIEW_S + 1
    real = threading.Thread.start

    def refuse(self):
        if self.name.startswith("view-"):
            raise RuntimeError("can't start new thread")
        return real(self)
    monkeypatch.setattr(threading.Thread, "start", refuse)
    assert json.loads(live.view("v", "a", ("k",), lambda: {"n": 2})[0]) == {"n": 1}   # served stale, no error
    monkeypatch.setattr(threading.Thread, "start", real)
    assert ("v", "a") not in live._view_building and live._view_locks[("v", "a")][1] == 0
    live.view("v", "a", ("k",), lambda: {"n": 2})
    assert wait_for(lambda: json.loads(live.view("v", "a", ("k",), lambda: {"n": 3})[0]) == {"n": 2})


def test_stale_read_latency_under_a_cpu_bound_rebuild(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "VIEW_STALE_S", 60)
    monkeypatch.setattr(serve, "OVERVIEW_S", 0)
    live = serve.Live(str(tmp_path))
    live.view("v", "a", ("k",), lambda: {"n": 1})

    def busy():
        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            pass
        return {"n": 2}
    lat = []
    for _ in range(100):
        t0 = time.perf_counter()
        live.view("v", "a", ("k",), busy)
        lat.append(time.perf_counter() - t0)
        time.sleep(0.005)
    lat.sort()
    assert lat[-1] < 0.5                                  # the slowest read never waits for the 1 s build
    wait_for(lambda: not live._view_building, 10)


def _expire_age(monkeypatch, clock):
    clock[0] += serve.OVERVIEW_S + 1


def test_mission_view_age_expiry_serves_old_then_new_and_a_key_move_serves_fresh(tmp_path, monkeypatch):
    """Production VIEW_STALE_S, the real mission view."""
    root = mapped_project(tmp_path)
    monkeypatch.setattr(serve, "VIEW_STALE_S", 60)
    clock = fake_clock(monkeypatch, time.monotonic())
    live = serve.Live(str(root))
    first, _ = live.mission_view(None)
    clock[0] += serve.OVERVIEW_S + 1                       # a pure age expiry: nothing in the record moved
    served, _ = live.mission_view(None)
    assert served == first                                  # old body at once
    assert wait_for(lambda: not live._view_building)
    assert live._views[("mission", None)][0] == clock[0]    # the rebuild stored a new body
    # a key move: a mission check is a new record event, shown on the very next read
    mission.run_check(root, "parser", "behavior", ["python3", "-c", "pass"], session="owner")
    clock[0] += 1                                          # time passes between the check and the read
    got, _ = live.mission_view(None)
    assert by_id(json.loads(got), "parser")["state"] == "verified"


def test_research_view_age_expiry_serves_old_then_new_and_a_key_move_serves_fresh(project, monkeypatch):
    conn = research_root(project)
    root = str(project)
    monkeypatch.setattr(serve, "VIEW_STALE_S", 60)
    clock = fake_clock(monkeypatch, time.monotonic())
    live = serve.Live(root)
    try:
        first, _ = live.research_view(None)
        clock[0] += serve.OVERVIEW_S + 1
        served, _ = live.research_view(None)
        assert served == first
        assert wait_for(lambda: not live._view_building)
        assert live._views[("research", None)][0] == clock[0]
        from alpaca.research import events
        events.emit(conn, events.STOP, run="rr-001", ref="rr-001", op="op-001", session="research-rr-001",
                    data={"reason": "owner", "final": False})
        clock[0] += 1
        got, _ = live.research_view(None)
        assert json.loads(got) == json.loads(json.dumps(view.runs(root)))   # the stop shows on the next read
        assert json.loads(got) != json.loads(first)
    finally:
        conn.close()


class Counter:
    def __init__(self):
        self.lock, self.active, self.peak, self.calls = threading.Lock(), 0, 0, 0

    def build(self, seconds, value=None):
        with self.lock:
            self.active += 1
            self.calls += 1
            self.peak = max(self.peak, self.active)
            n = self.calls
        try:
            time.sleep(seconds)
            return {"n": n, "t": time.monotonic() - seconds} if value is None else value
        finally:
            with self.lock:
                self.active -= 1


def settled(live):
    with live._view_guard:
        assert not live._view_building, live._view_building
        for slot, held in live._view_locks.items():
            assert held[0] == [], ("running list not empty", slot, held[0])
            assert held[1] == 0, ("callers left", slot, held[1])
            assert slot in live._views, ("orphan entry", slot)


# 1 port: an aged body is rebuilding in the background; the key moves; the reader is charged one build.
def test_single_flight_moved_key_reader_waits_rest_of_build_plus_one(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "VIEW_STALE_S", 60)
    monkeypatch.setattr(serve, "OVERVIEW_S", 0.2)
    live = serve.Live(str(tmp_path))
    head = ["k1"]
    c = Counter()
    live.view("v", "a", lambda: (head[0],), lambda: {"n": 0})
    time.sleep(0.25)
    live.view("v", "a", lambda: (head[0],), lambda: c.build(1.0))          # aged: background starts
    assert wait_for(lambda: c.calls == 1)
    head[0] = "k2"
    t0 = time.monotonic()
    live.view("v", "a", lambda: (head[0],), lambda: c.build(1.0))
    waited = time.monotonic() - t0
    assert wait_for(lambda: not live._view_building)
    assert c.peak == 1
    assert waited < 2.5                                  # the rest of the running build plus one build; peak 1
    settled(live)


# 2 port: A reads k1, its key function is slow; B reads k2 later, stores first; A must not overwrite.
def test_function_key_older_read_never_overwrites_a_newer_store(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "VIEW_STALE_S", 60)
    live = serve.Live(str(tmp_path))
    live.view("v", "a", lambda: ("k0",), lambda: {"n": 0})
    c = Counter()
    a_read, b_done = threading.Event(), threading.Event()

    def slow_key_a():
        key = ("k1",)
        a_read.set()
        b_done.wait(5)                                   # source_stamp under contention
        return key
    got = []
    a = threading.Thread(target=lambda: got.append(live.view("v", "a", slow_key_a, lambda: c.build(0.05))))
    a.start()
    assert a_read.wait(5)
    live.view("v", "a", lambda: ("k2",), lambda: c.build(0.05))
    b_done.set()
    a.join(5)
    assert live._views[("v", "a")][1] == ("k2",)
    assert c.calls == 1
    # 3 port: the current key is not recomputed
    live.view("v", "a", lambda: ("k2",), lambda: c.build(0.05))
    assert c.calls == 1
    settled(live)


# 4: a reader with the same key as a running build waits for it; when that build is longer than
# OVERVIEW_S (two builds side by side slow each other), does the reader then compute again?
def test_same_key_waiter_is_not_charged_twice_when_the_build_is_long(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "VIEW_STALE_S", 60)
    monkeypatch.setattr(serve, "OVERVIEW_S", 0.2)
    live = serve.Live(str(tmp_path))
    c = Counter()
    first = threading.Thread(target=lambda: live.view("v", "a", lambda: ("k1",), lambda: c.build(0.5)))
    first.start()
    assert wait_for(lambda: c.calls == 1)
    time.sleep(0.05)
    t0 = time.monotonic()
    live.view("v", "a", lambda: ("k1",), lambda: c.build(0.5))
    waited = time.monotonic() - t0
    first.join(5)
    assert c.calls == 1 and waited < 0.7
    settled(live)


# 5: a busy record (key moves every 50 ms) and readers every 50 ms during 1 s builds: how many
# computes run at once for one slot?
def test_computes_per_slot_stay_bounded_on_a_busy_record(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "VIEW_STALE_S", 60)
    live = serve.Live(str(tmp_path))
    c = Counter()
    head = [0]
    stop = time.monotonic() + 2.0
    threads = []
    while time.monotonic() < stop:
        head[0] += 1
        t = threading.Thread(target=lambda: live.view("v", "a", lambda: (head[0],), lambda: c.build(1.0)))
        t.start()
        threads.append(t)
        time.sleep(0.05)
    [t.join(10) for t in threads]
    settled(live)
    # one running build and one pending, never more: builds of 1 s over 2 s of arrivals need 2 builds
    assert c.peak == 1, "computes for one slot ran %d at once" % c.peak
    assert c.calls <= 3, "%d computes for a 2 s burst of 1 s builds (needed 2, plus one)" % c.calls


# 6: leaks after unknown runs, raising computes, raising key functions and a thread that cannot start.
def test_running_list_and_tables_do_not_leak(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "VIEW_STALE_S", 60)
    monkeypatch.setattr(serve, "OVERVIEW_S", 0.05)
    live = serve.Live(str(tmp_path))
    for i in range(50):
        assert live.view("v", "unknown-%d" % i, lambda: ("k",), lambda: None) is None
    for i in range(20):
        try:
            live.view("v", "bad-%d" % i, lambda: ("k",), lambda: (_ for _ in ()).throw(ValueError("bad at")))
        except ValueError:
            pass
        try:
            live.view("v", "keyfail", lambda: (_ for _ in ()).throw(OSError("db")), lambda: {"n": 1})
        except OSError:
            pass
    live.view("v", "a", lambda: ("k",), lambda: {"n": 1})
    time.sleep(0.06)
    real = threading.Thread.start

    def refuse(self):
        if self.name.startswith("view-"):
            raise RuntimeError("can't start new thread")
        return real(self)
    monkeypatch.setattr(threading.Thread, "start", refuse)
    assert json.loads(live.view("v", "a", lambda: ("k",), lambda: {"n": 2})[0]) == {"n": 1}
    monkeypatch.setattr(threading.Thread, "start", real)
    # background build whose key function raises, then whose compute raises
    calls = []
    live.view("v", "a", lambda: ("k",), lambda: calls.append(1) or (_ for _ in ()).throw(RuntimeError("x")))
    assert wait_for(lambda: not live._view_building)
    settled(live)
    assert set(live._view_locks) == {("v", "a")}


# 7: stress with function keys, failures and moving keys: no body served older than VIEW_STALE_S
# (scaled) from the start of its compute; tables settle.
def test_age_cap_with_function_keys_and_moving_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "VIEW_STALE_S", 0.6)
    monkeypatch.setattr(serve, "OVERVIEW_S", 0.2)
    monkeypatch.setattr(serve, "VIEW_RETRY_S", 0.1)
    live = serve.Live(str(tmp_path))
    rng = random.Random(11)
    head = [0]
    viol, served, raised, peak = [], [0], [0], [0]
    c = Counter()
    stop = time.monotonic() + 5

    def compute():
        if rng.random() < 0.4:
            time.sleep(rng.random() * 0.1)
            raise RuntimeError("busy")
        return c.build(rng.random() * 0.3)

    def key():
        time.sleep(rng.random() * 0.02)
        return (head[0] // 10,)                           # moves every 10 ticks: aged and moved both happen

    def reader(slot):
        while time.monotonic() < stop:
            try:
                got = live.view("v", slot, key, compute)
            except RuntimeError:
                raised[0] += 1
                continue
            served[0] += 1
            age = time.monotonic() - json.loads(got[0])["t"]
            if age > serve.VIEW_STALE_S + 0.05:
                viol.append(age)
            time.sleep(0.01)

    def mover():
        while time.monotonic() < stop:
            head[0] += 1
            time.sleep(0.03)
    ts = [threading.Thread(target=reader, args=(s,)) for s in "aabbc"] + [threading.Thread(target=mover)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert wait_for(lambda: not live._view_building, 5)
    assert not viol, viol[:5]
    settled(live)
