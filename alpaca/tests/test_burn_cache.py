"""t-583: the Sessions board keeps its per-session work; an unchanged session is not redone.

The cached board must equal one computed from empty caches for every window, a session that moves
is read again, a newly recorded rate card reprices, and a slow window does not hold up another.
"""
import json
import os
import threading
import time
from pathlib import Path

import pytest

from alpaca import db, observability, serve, transcripts
from alpaca.analytics import build_index, burn, detail, pricing, ratecards
from alpaca.tests.test_session_insights import NOW, dump, event, register, resp, session, task


def cold():
    """Every board cache emptied: what a freshly started server computes."""
    with burn._LOCK:
        burn._FILES.clear()
        burn._SPEND.clear()
        burn._LINKS.clear()
        burn._ROWS.clear()


def board(root, window="24h", **kw):
    value = burn.board(root, build_index.collect(root), window, now=NOW, **kw)
    value.pop("generated")
    return json.dumps(value, sort_keys=True)


def build(project):
    """Two sessions, a claimed task with an off-task edit, a done cycle, and a subagent copy."""
    root = str(Path(project).resolve())
    t = NOW - 5000
    session(project, "s1", [
        resp("c1", t + 200, "Edit", {"file_path": root + "/alpaca/web/burn.js"}),
        resp("o1", t + 400, "Write", {"file_path": root + "/docs/unrelated.md"}),
        resp("b1", t + 600, "Bash", {"command": "cat docs/other.md"}),
        resp("x1", t + 800)])
    session(project, "s2", [resp("y%d" % i, t + 100 * i, "Edit", {"file_path": root + "/src/a%d.py" % i}) for i in range(5)])
    task(project, "t-901", "op-9", "Fix the board in alpaca/web/burn.js")
    event(project, "claim", t + 100, "s1", "t-901", "op-9")
    event(project, "task-move", t + 700, "s1", "t-901", "op-9", {"from": "doing", "to": "done"})
    event(project, "claim", t + 150, "s2", "t-901", "op-9")
    return root


def test_cached_board_equals_an_uncached_one_for_every_window(project):
    build(project)
    cases = [dict(window="24h"), dict(window="7d"), dict(window="all"),
             dict(window="24h", start=NOW - 3 * 3600, end=NOW - 3600)]
    for case in cases:
        cold()
        fresh = board(project, **case)
        assert board(project, **case) == fresh, case              # warm
        assert board(project, **case) == fresh, case              # warm again, same objects
    other = board(project, "7d")
    assert other == board(project, "7d") and "t-901" in other


def test_a_moved_session_is_read_again_and_the_rest_are_not(project, monkeypatch):
    root = build(project)
    cold()
    first = board(project)
    seen = []
    real = burn.insights.targets
    monkeypatch.setattr(burn.insights, "targets", lambda call, r, **kw: seen.append(call["id"]) or real(call, r, **kw))
    assert board(project) == first and seen == []                 # nothing moved: no path was resolved
    t = NOW - 1000
    records = [resp("c1", NOW - 4800, "Edit", {"file_path": root + "/alpaca/web/burn.js"}),
               resp("o1", NOW - 4600, "Write", {"file_path": root + "/docs/unrelated.md"}),
               resp("b1", NOW - 4400, "Bash", {"command": "cat docs/other.md"}),
               resp("x1", NOW - 4200),
               resp("n1", t, "Write", {"file_path": root + "/docs/new.md"})]
    dump(transcripts.local_path(project, "s1"), records)           # s1 grows by one response
    moved = board(project)
    assert moved != first and set(seen) == {"tu-c1", "tu-o1", "tu-b1", "tu-n1"}
    assert "tu-y1" not in seen                                      # s2 was not touched
    monkeypatch.undo()
    cold()
    assert board(project) == moved
    sessions = {x["sid"]: x for x in json.loads(moved)["sessions"]}
    assert sessions["s1"]["requests"] == 5


def test_a_new_rate_card_reprices(project):
    record = resp("r1", NOW - 600)
    record["message"]["model"] = "claude-opus-9"
    session(project, "s1", [record])
    cold()
    before = json.loads(board(project))
    assert before["totals"]["cost_usd"] == 0 and before["totals"]["unpriced_requests"] == 1
    conn = db.connect(project)
    db.append_event(conn, session="cli", actor="analytics", kind=ratecards.EVENT, ref="claude-opus-9",
                    data={"model": "claude-opus-9", "provider": "anthropic",
                          "source_url": pricing.CLAUDE_PRICING, "source_urls": [pricing.CLAUDE_PRICING],
                          "rates": ["5", "0.5", "6.25", "10", None, "25"], "context_threshold_tokens": None,
                          "verified_at": "2026-09-25", "fast_multiplier": None,
                          "label": {"method": "agent-entered", "verified_by": "cli",
                                    "recorded_at": "2026-09-25T00:00:00+00:00"}})
    conn.close()
    after = board(project)
    assert json.loads(after)["totals"]["cost_usd"] > 0 and json.loads(after)["totals"]["unpriced_requests"] == 0
    cold()
    assert board(project) == after


def test_independent_windows_do_not_block_each_other(project, monkeypatch):
    build(project)
    live = serve.Live(project)
    live.refresh()
    entered, release = threading.Event(), threading.Event()
    real = burn.board

    def slow(root, sessions, window="24h", **kw):
        if window == "24h":
            entered.set()
            assert release.wait(20)
        return real(root, sessions, window, **kw)
    monkeypatch.setattr(burn, "board", slow)
    result = {}
    worker = threading.Thread(target=lambda: result.update(day=live.burn("24h")))
    worker.start()
    try:
        assert entered.wait(20)
        t0 = time.monotonic()
        body, _etag = live.burn("7d")                              # 24h is mid computation
        assert time.monotonic() - t0 < 15 and json.loads(body)["window"]["key"] == "7d"
        assert "day" not in result                                  # and 24h is still waiting
    finally:
        release.set()
        worker.join(30)
    assert json.loads(result["day"][0])["window"]["key"] == "24h"


# ---- served stale-while-revalidate (Live.burn) -------------------------------------------------

def live_with_counter(project, monkeypatch, gate=None, fail=None):
    """A Live whose board builds are counted and can be held; returns (live, builds)."""
    live = serve.Live(project)
    live.refresh()
    builds = []
    real = burn.board

    def counted(root, sessions, window="24h", **kw):
        builds.append(window)
        if gate is not None and len(builds) > 1:
            assert gate.wait(20)
        if fail is not None and fail.is_set() and len(builds) > 1:
            raise RuntimeError("build failed")
        return real(root, sessions, window, **kw)
    monkeypatch.setattr(burn, "board", counted)
    return live, builds


def settle(live, builds, count, seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not (len(builds) >= count and not any(e[4] for e in live._burn.values())):
        time.sleep(0.02)


def test_a_fresh_key_builds_once_and_is_then_served_from_memory(project, monkeypatch):
    build(project)
    live, builds = live_with_counter(project, monkeypatch)
    results = []
    threads = [threading.Thread(target=lambda: results.append(live.burn("7d"))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert builds == ["7d"] and len({r[1] for r in results}) == 1
    t0 = time.monotonic()
    again = live.burn("7d")
    assert again == results[0] and builds == ["7d"] and time.monotonic() - t0 < 0.1


def test_a_moved_input_serves_the_old_board_at_once_and_the_new_one_after_the_rebuild(project, monkeypatch):
    build(project)
    gate = threading.Event()
    live, builds = live_with_counter(project, monkeypatch, gate=gate)
    old_body, old_etag = live.burn("24h")
    assert builds == ["24h"] and '"refreshing"' not in old_body
    with live.lock:
        live.data_rev = "moved"                                    # the fold moved
    t0 = time.monotonic()
    served = [live.burn("24h") for _ in range(5)]                  # the rebuild is held open
    assert time.monotonic() - t0 < 1
    end = time.monotonic() + 10
    while len(builds) < 2 and time.monotonic() < end:
        time.sleep(0.01)
    assert builds == ["24h", "24h"]                                 # five requests, one rebuild
    for body, etag in served:
        assert json.loads(body).pop("refreshing") is True
        assert json.loads(body)["totals"] == json.loads(old_body)["totals"]
    gate.set()
    settle(live, builds, 2)
    body, etag = live.burn("24h")
    assert '"refreshing"' not in body and etag != old_etag and builds == ["24h", "24h"]
    assert live._burn[("24h", None, None)][0][0] == "moved"


def test_a_failed_rebuild_keeps_the_old_board_and_retries_later(project, monkeypatch):
    build(project)
    fail = threading.Event()
    live, builds = live_with_counter(project, monkeypatch, fail=fail)
    old = live.burn("24h")
    fail.set()
    with live.lock:
        live.data_rev = "moved"
    live.burn("24h")
    settle(live, builds, 2)
    body, _etag = live.burn("24h")
    served = json.loads(body)
    assert served.pop("refreshing") is True and served == json.loads(old[0])   # flagged while the retry is pending
    assert builds == ["24h", "24h"]                                 # backing off: no second attempt yet
    fail.clear()
    live._burn[("24h", None, None)][5] = 0.0
    live.burn("24h")
    settle(live, builds, 3)
    assert builds == ["24h"] * 3 and live._burn[("24h", None, None)][0][0] == "moved"


def test_a_board_past_the_stale_bound_is_rebuilt_before_it_is_served(project, monkeypatch):
    build(project)
    live, builds = live_with_counter(project, monkeypatch)
    live.burn("24h")
    live._burn[("24h", None, None)][3] -= serve.BURN_STALE_S + 1
    live.burn("24h")
    assert builds == ["24h"]                                        # old but its inputs are the same: still current
    with live.lock:
        live.data_rev = "moved"
    body, _etag = live.burn("24h")
    assert builds == ["24h", "24h"] and '"refreshing"' not in body  # waited for the new board


# ---- a rate card recorded while two windows build -----------------------------------------------

def test_a_rate_card_recorded_while_another_window_builds_does_not_reach_that_board(project, monkeypatch):
    record = resp("r1", NOW - 600)
    record["message"]["model"] = "claude-opus-9"
    session(project, "s1", [record])                                # no claim: its spend is "unclaimed"
    cold()
    entered, release = threading.Event(), threading.Event()
    real = burn.insights.cycles

    def held(events):
        if threading.current_thread().name == "day":
            entered.set()
            assert release.wait(20)
        return real(events)
    monkeypatch.setattr(burn.insights, "cycles", held)
    out = {}

    def run(name, window):
        out[name] = burn.board(project, build_index.collect(project), window, now=NOW)
    day = threading.Thread(target=run, args=("day", "24h"), name="day")
    day.start()
    assert entered.wait(20)                                         # 24h has read the session and is mid-signals
    conn = db.connect(project)
    db.append_event(conn, session="cli", actor="analytics", kind=ratecards.EVENT, ref="claude-opus-9",
                    data={"model": "claude-opus-9", "provider": "anthropic",
                          "source_url": pricing.CLAUDE_PRICING, "source_urls": [pricing.CLAUDE_PRICING],
                          "rates": ["5", "0.5", "6.25", "10", None, "25"], "context_threshold_tokens": None,
                          "verified_at": "2026-09-25", "fast_multiplier": None,
                          "label": {"method": "agent-entered", "verified_by": "cli",
                                    "recorded_at": "2026-09-25T00:00:00+00:00"}})
    conn.close()
    run("week", "7d")                                               # re-prices the shared parse state
    release.set()
    day.join(30)
    week = out["week"]["sessions"][0]["signals"]["offtask"]["unclaimed"]["cost"]
    assert out["week"]["totals"]["cost_usd"] > 0 and week > 0
    assert out["day"]["totals"]["cost_usd"] == 0                     # priced with the cards it started with ...
    assert out["day"]["sessions"][0]["signals"]["offtask"]["unclaimed"]["cost"] == 0   # ... in every figure


# ---- the child listing is kept only when it was read whole and its stamp sees what it reads -----

def links_of(project, sid="s1"):
    return next(x for x in json.loads(board(project))["sessions"] if x["sid"] == sid)["child_links"]


def test_a_subagent_directory_swapped_for_a_symlink_is_seen(project):
    build(project)
    base = Path(transcripts.subagents_dir(project, "s1"))
    dump(base / "agent-aaa.jsonl", [resp("k1", NOW - 3000)])
    assert links_of(project)
    real = base.parent / "subagents-real"
    os.rename(base, real)
    os.symlink(real, base)                                          # the tree behind it did not change
    kept = links_of(project)
    cold()
    assert kept == links_of(project)


def test_a_listing_that_failed_is_not_kept(project, monkeypatch):
    build(project)
    base = Path(transcripts.subagents_dir(project, "s1"))
    dump(base / "agent-aaa.jsonl", [resp("k1", NOW - 3000)])
    real, calls = detail._children, []

    def flaky(*a, **kw):
        import inspect
        if any(f.function == "child_links" for f in inspect.stack()[1:4]):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("transient")
        return real(*a, **kw)
    monkeypatch.setattr(detail, "_children", flaky)
    assert links_of(project) == {}                                  # served as no children ...
    later = links_of(project)                                       # ... but read again, nothing having moved
    cold()
    assert later and later == links_of(project)


def test_a_relative_registered_locator_is_stamped_against_the_root(project, monkeypatch):
    build(project)
    root = str(Path(project).resolve())
    Path(root, "rel").mkdir()
    Path(root, "rel", "kid9.jsonl").write_text("{}\n")
    monkeypatch.setattr(detail, "_obs_sources", lambda conn, r, sid, parent_only=False: [
        {"source_id": "kid9", "session": "kid9", "locator": "rel/kid9.jsonl"}])
    conn = db.connect_readonly(project)
    stamp = burn._link_stamp(root, conn, "s1")
    conn.close()
    row = next(x for x in stamp if isinstance(x, tuple) and x[0] == "kid9")
    assert row[1] == os.path.join(root, "rel", "kid9.jsonl") and row[2] is True      # found where _children looks


def test_the_cycle_share_table_is_bounded(project, monkeypatch):
    build(project)
    monkeypatch.setattr(burn, "SPEND_KEEP", 1)
    cold()
    board(project)
    assert len(burn._SPEND) <= 1


# ---- the server side of the stale-while-revalidate board ----------------------------------------

def live_counted(project, monkeypatch, hold=None):
    live = serve.Live(project)
    live.refresh()
    builds, active, peak = [], [0], [0]
    real = burn.board
    lock = threading.Lock()

    def counted(root, sessions, window="24h", **kw):
        with lock:
            builds.append(window)
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            if hold is not None and hold.get(window) is not None and len(builds) > 1:
                assert hold[window].wait(20)
            return real(root, sessions, window, **kw)
        finally:
            with lock:
                active[0] -= 1
    monkeypatch.setattr(burn, "board", counted)
    return live, builds, peak


def test_a_board_served_stale_is_flagged_and_the_next_one_is_new(project, monkeypatch):
    build(project)
    live, builds, _ = live_counted(project, monkeypatch)
    old_body, _etag = live.burn("24h")
    live._burn[("24h", None, None)][3] -= serve.BURN_STALE_S - 10       # built 290 s ago
    with live.lock:
        live.data_rev = "moved-four-minutes-ago"
    body, _etag = live.burn("24h")
    served = json.loads(body)
    assert served.pop("refreshing") is True and served == json.loads(old_body)
    end = time.monotonic() + 20
    while time.monotonic() < end and (len(builds) < 2 or live._burn[("24h", None, None)][4]):
        time.sleep(0.02)
    assert len(builds) == 2
    fresh, _etag = live.burn("24h")
    assert "refreshing" not in json.loads(fresh) and json.loads(fresh)["generated"] >= served["generated"]


def test_a_key_being_built_is_never_evicted_so_one_key_never_builds_twice(project, monkeypatch):
    build(project)
    monkeypatch.setattr(serve, "BURN_KEEP", 1)
    hold = {"24h": threading.Event()}
    live, builds, peak = live_counted(project, monkeypatch, hold=hold)
    live.burn("24h")
    live._burn[("24h", None, None)][3] -= serve.BURN_STALE_S + 1
    with live.lock:
        live.data_rev = "moved"
    r1 = threading.Thread(target=live.burn, args=("24h",))
    r1.start()                                                          # past the bound: holds the 24h gate, blocked
    end = time.monotonic() + 10
    while builds.count("24h") < 2 and time.monotonic() < end:
        time.sleep(0.01)
    live.burn("7d")                                                     # stores 7d and would evict 24h
    r2 = threading.Thread(target=live.burn, args=("24h",))
    r2.start()
    time.sleep(0.5)
    concurrent = builds.count("24h") == 3
    hold["24h"].set()
    r1.join(30)
    r2.join(30)
    assert not concurrent, "two concurrent builds of the 24h key (builds=%r)" % builds
    assert builds.count("24h") == 2 and live._burn_locks.get(("24h", None, None), [0, 0])[1] == 0


def test_past_the_stale_bound_a_failing_rebuild_backs_off_and_reports_the_error(project, monkeypatch):
    build(project)
    fail = threading.Event()
    live, builds, _ = live_counted(project, monkeypatch)
    live.burn("24h")
    real = burn.board

    def broken(root, sessions, window="24h", **kw):
        builds.append(window)
        raise RuntimeError("build failed")
    monkeypatch.setattr(burn, "board", broken)
    live._burn[("24h", None, None)][3] -= serve.BURN_STALE_S + 1
    with live.lock:
        live.data_rev = "moved"
    for _ in range(3):
        with pytest.raises(RuntimeError):
            live.burn("24h")
    assert builds.count("24h") == 2                                     # the first build, then one failed try; two requests backed off
    live._burn[("24h", None, None)][5] = 0.0
    monkeypatch.setattr(burn, "board", real)
    body, _etag = live.burn("24h")
    assert '"refreshing"' not in body and live._burn[("24h", None, None)][0][0] == "moved"


def test_a_failed_refresh_does_not_clear_the_flag_of_a_newer_board(project, monkeypatch):
    build(project)
    live, builds, _ = live_counted(project, monkeypatch)
    live.burn("24h")
    mine = live._burn[("24h", None, None)]
    newer = list(mine)
    newer[4] = True                                                     # a newer entry with its own flag set
    live._burn[("24h", None, None)] = newer
    live._burn_locks[("24h", None, None)][1] += 1
    mine[4] = True
    monkeypatch.setattr(burn, "board", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    live._burn_refresh(("24h", None, None), mine)
    assert mine[4] is False and newer[4] is True


def test_while_a_retry_is_pending_the_stale_board_is_still_flagged(project, monkeypatch):
    build(project)
    fail = threading.Event()
    live, builds = live_with_counter(project, monkeypatch, fail=fail)
    live.burn("24h")
    fail.set()
    with live.lock:
        live.data_rev = "moved"
    live.burn("24h")                                                    # starts the background rebuild, which fails
    settle(live, builds, 2)
    body, _etag = live.burn("24h")                                      # inside the back-off
    assert live._burn[("24h", None, None)][0][0] != "moved"             # still the old board ...
    assert json.loads(body)["refreshing"] is True                       # ... so the page keeps polling


def test_the_error_re_raised_during_back_off_does_not_grow(project, monkeypatch):
    build(project)
    fail = threading.Event()
    live, builds = live_with_counter(project, monkeypatch, fail=fail)
    live.burn("24h")
    fail.set()
    live._burn[("24h", None, None)][3] -= serve.BURN_STALE_S + 1
    with live.lock:
        live.data_rev = "moved"
    with pytest.raises(RuntimeError):
        live.burn("24h")                                                # the failed build stores the error
    depth = []
    for _ in range(50):
        with pytest.raises(RuntimeError) as caught:
            live.burn("24h")
        tb, d = caught.value.__traceback__, 0
        while tb:
            d += 1
            tb = tb.tb_next
        depth.append(d)
    assert builds.count("24h") == 2 and depth[-1] <= depth[0] + 2, depth[-5:]
