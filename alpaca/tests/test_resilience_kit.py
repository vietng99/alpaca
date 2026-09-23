"""M3.12: the resilience kit for unattended runs.

Proof for task M3.12. Five modules, ported against the RECORD rather than the earlier harness file
formats (progress = the events count plus the row cursor, both read from the store):

  * pulse_watch.classify(conn) -> healthy | HUNG | DEAD from real progress (a beat that stops
    advancing is HUNG at the first stall and DEAD at the second consecutive stall),
  * worker_launch.lock(root, worker) refusing a second launcher for the same worker,
  * kicker.check(root) firing only when the record is quiet AND no live claim holds the work,
  * reset_wait.parse(headers) folding a rate-limit header pair into one reset instant,
  * budget.posture(conn) entering HOLD when a run falls to or below the project.yaml reserve.

Every module ported from the earlier harness carries a --selftest control table; here each such table is a
pytest wrapper that fails on any non-PASS. budget has no predecessor: its posture, concurrency
tiers and preflight floors are net-new to the three doctrine docs and their tests are net-new.

FixedClock stamps every write and drives every instant, so the classification, the lease
liveness and the reset math are asserted against deterministic times, per Step 1 and Done-when.
"""
import datetime

import pytest

from alpaca import board, clock, db, project as proj, util
from alpaca.resilience import budget, kicker, pulse_watch, reset_wait, worker_launch


@pytest.fixture(autouse=True)
def _fixed_clock():
    """Deterministic, monotonically advancing timestamps for every write in a test."""
    util.set_clock(clock.FixedClock(start="2026-09-16T00:00:00+00:00", step=1))
    try:
        yield
    finally:
        util.set_clock(None)


def _write_project_yaml(root, **extra):
    """Write a minimal project.yaml into a throwaway root so a module that reads the resource
    class and the reserve reads them from the file, never from code."""
    cfg = {
        "name": "t",
        "resource_class": "small",
        "budget": {"reserve": 10, "totals": {"small": 100, "medium": 500, "large": 2000}},
        "concurrency": {"tiers": {"small": 1, "medium": 3, "large": 8}},
    }
    cfg.update(extra)
    proj.save(root, cfg)


def _add_row(conn, row_id, op="OP1"):
    """Append one obligation row so the row cursor can advance independently of the events count."""
    db.upsert(conn, "rows", "id",
              {"id": row_id, "kind": "item", "op": op, "phase": "build", "statement": "s",
               "status": "open"})


# =========================================================================== pulse_watch
def test_pulse_progress_is_events_plus_row_cursor(project):
    """Real progress is the events count plus the row cursor. Appending a non-pulse event or a
    row each advances it; a pulse beat itself does not (it must not inflate its own signal)."""
    conn = db.connect(project)
    base = pulse_watch.progress(conn)
    db.append_event(conn, session="s", actor="agent", kind="heartbeat", data={"t": "x"})
    assert pulse_watch.progress(conn) == base + 1
    _add_row(conn, "R1")
    assert pulse_watch.progress(conn) == base + 2
    # a pulse beat writes an event but must NOT count toward progress (no self-inflation).
    before = pulse_watch.progress(conn)
    pulse_watch.beat(conn, session="s")
    assert pulse_watch.progress(conn) == before


def test_pulse_healthy_while_progress_advances(project):
    """While each beat sees strictly more real progress than the last, the run is healthy."""
    conn = db.connect(project)
    pulse_watch.beat(conn, session="s")
    db.append_event(conn, session="s", actor="agent", kind="heartbeat", data={"n": 1})
    pulse_watch.beat(conn, session="s")
    assert pulse_watch.classify(conn) == "healthy"
    _add_row(conn, "R1")
    pulse_watch.beat(conn, session="s")
    assert pulse_watch.classify(conn) == "healthy"


def test_pulse_hung_then_dead_when_progress_stalls(project):
    """A beat that stops advancing is HUNG at the first stall and DEAD at the second consecutive
    stall, from real progress sources, under the fixed clock. Done-when, positive and negative."""
    conn = db.connect(project)
    pulse_watch.beat(conn, session="s")            # baseline
    db.append_event(conn, session="s", actor="agent", kind="heartbeat", data={"n": 1})
    pulse_watch.beat(conn, session="s")            # advanced -> healthy
    assert pulse_watch.classify(conn) == "healthy"
    pulse_watch.beat(conn, session="s")            # no new work -> first stall
    assert pulse_watch.classify(conn) == "HUNG"
    pulse_watch.beat(conn, session="s")            # still no new work -> second stall
    assert pulse_watch.classify(conn) == "DEAD"
    # recovery: real work resumes and the very next beat is healthy again.
    db.append_event(conn, session="s", actor="agent", kind="heartbeat", data={"n": 2})
    pulse_watch.beat(conn, session="s")
    assert pulse_watch.classify(conn) == "healthy"


def test_pulse_no_baseline_is_healthy_not_a_false_dead(project):
    """One beat with nothing before it has no baseline to compare against; it must read healthy,
    never a false DEAD (the first observed beat must not be treated as a stall)."""
    conn = db.connect(project)
    assert pulse_watch.classify(conn) == "healthy"
    pulse_watch.beat(conn, session="s")
    assert pulse_watch.classify(conn) == "healthy"


def test_pulse_selftest_wrapper(project):
    """The ported --selftest control table (able-to-pass and able-to-fail) all PASS."""
    assert pulse_watch.selftest() == 0


# =========================================================================== worker_launch
def test_worker_launch_second_launcher_is_refused(project):
    """A second launcher for the SAME worker is refused by the re-entry lock while the first
    holds it; once released the lock is re-acquirable. Done-when, positive and negative."""
    lock1 = worker_launch.lock(project, "w1")
    try:
        with pytest.raises(worker_launch.LockHeldError):
            worker_launch.lock(project, "w1")
    finally:
        lock1.release()
    lock2 = worker_launch.lock(project, "w1")      # released -> re-acquirable
    lock2.release()


def test_worker_launch_different_workers_do_not_contend(project):
    """Two DIFFERENT workers on the same box hold distinct locks and do not contend."""
    a = worker_launch.lock(project, "wa")
    b = worker_launch.lock(project, "wb")
    a.release()
    b.release()


def test_worker_launch_lock_is_a_context_manager(project):
    """The lock releases on block exit, so the same worker is launchable again afterward."""
    with worker_launch.lock(project, "wc"):
        pass
    with worker_launch.lock(project, "wc"):
        pass


def test_worker_launch_selftest_wrapper(project):
    """The ported --selftest control table (able-to-pass, release-on-exit, able-to-fail,
    ordering, release-on-driver-throw) all PASS."""
    assert worker_launch.selftest() == 0


# =========================================================================== kicker
def test_kicker_no_fire_within_grace(project):
    """A record touched within Grace is normal cadence: NO-FIRE."""
    conn = db.connect(project)
    db.append_event(conn, session="s", actor="agent", kind="heartbeat", data={"n": 1})
    now = "2026-09-16T00:00:30+00:00"               # 30s after the last event
    assert kicker.check(project, now=now, grace_seconds=300, conn=conn) == "NO-FIRE"


def test_kicker_fires_when_quiet_and_no_live_claim(project):
    """Grace exceeded and the ledger clear (no live claim): FIRE."""
    conn = db.connect(project)
    db.append_event(conn, session="s", actor="agent", kind="heartbeat", data={"n": 1})
    now = "2026-09-16T01:00:00+00:00"               # an hour later, well past Grace
    assert kicker.check(project, now=now, grace_seconds=300, conn=conn) == "FIRE"


def test_kicker_refused_while_a_live_claim_holds_the_work(project):
    """Grace exceeded but a live claim holds a row: REFUSED, never a double-dispatch against a
    merely-slow-but-alive worker. The load-bearing negative control."""
    conn = db.connect(project)
    db.append_event(conn, session="s", actor="agent", kind="heartbeat", data={"n": 1})
    # a live claim whose lease outlasts `now`.
    db.append_event(conn, session="w1", actor="w1", kind=board.CLAIM_KIND, ref="R1",
                    data={"worker": "w1", "lease_until": "2026-09-16T02:00:00+00:00"})
    now = "2026-09-16T01:00:00+00:00"
    assert kicker.check(project, now=now, grace_seconds=300, conn=conn) == "REFUSED"


def test_kicker_fires_once_the_claim_lease_has_passed(project):
    """A claim whose lease has passed is not live: the row is up for grabs again and the kicker
    FIREs. Positive counterpart to the REFUSED control."""
    conn = db.connect(project)
    db.append_event(conn, session="s", actor="agent", kind="heartbeat", data={"n": 1})
    db.append_event(conn, session="w1", actor="w1", kind=board.CLAIM_KIND, ref="R1",
                    data={"worker": "w1", "lease_until": "2026-09-16T00:10:00+00:00"})
    now = "2026-09-16T01:00:00+00:00"               # past both Grace and the lease
    assert kicker.check(project, now=now, grace_seconds=300, conn=conn) == "FIRE"


def test_kicker_unreadable_on_an_empty_record(project):
    """An empty record has no last-touch to judge: STATE-UNREADABLE (fails safe, never a blind
    fire against state it cannot resolve)."""
    conn = db.connect(project)
    assert kicker.check(project, now="2026-09-16T01:00:00+00:00", grace_seconds=300,
                        conn=conn) == "STATE-UNREADABLE"


def test_kicker_selftest_wrapper():
    """The ported --selftest control table (live claim REFUSED, clear FIRE, empty UNREADABLE)
    all PASS, against the record rather than a STATE.md ledger section."""
    assert kicker.selftest() == 0


# =========================================================================== reset_wait
def test_reset_wait_delta_seconds_into_one_instant():
    """A Retry-After delta-seconds header folds into one reset instant `now + delta`."""
    now = datetime.datetime(2026, 9, 16, 0, 0, 0, tzinfo=datetime.timezone.utc)
    r = reset_wait.parse({"Retry-After": "120"}, now=now)
    assert r["source"] == "retry-after-delta"
    assert r["reset_at"] == now + datetime.timedelta(seconds=120)
    assert r["wait_cost_seconds"] == 120
    assert r["parse_error"] is False


def test_reset_wait_provider_header_wins_the_pair():
    """When both headers are present the provider-authoritative reset wins, and the pair folds
    into that one instant. Done-when: a rate-limit header pair parses into one reset instant."""
    now = datetime.datetime(2026, 9, 16, 0, 0, 0, tzinfo=datetime.timezone.utc)
    r = reset_wait.parse(
        {"Retry-After": "120", "anthropic-ratelimit-unified-reset": "2026-09-16T00:05:00Z"},
        now=now)
    assert r["source"] == "provider-header"
    assert r["reset_at"] == datetime.datetime(2026, 9, 16, 0, 5, 0, tzinfo=datetime.timezone.utc)
    assert r["wait_cost_seconds"] == 300


def test_reset_wait_absent_headers_fall_back_without_error():
    """No headers: the fallback path, reset_at None (undefined, NOT zero), parse_error False."""
    now = datetime.datetime(2026, 9, 16, 0, 0, 0, tzinfo=datetime.timezone.utc)
    r = reset_wait.parse({}, now=now)
    assert r["source"] == "absent-fallback"
    assert r["reset_at"] is None
    assert r["wait_cost_seconds"] is None
    assert r["parse_error"] is False


def test_reset_wait_malformed_headers_never_raise():
    """A present-but-malformed header falls through to the fallback with parse_error True and
    never raises: a provider header-format change must not become an unhandled exception."""
    now = datetime.datetime(2026, 9, 16, 0, 0, 0, tzinfo=datetime.timezone.utc)
    r = reset_wait.parse({"Retry-After": "garbage",
                          "x-ratelimit-reset": "also-garbage"}, now=now)
    assert r["source"] == "absent-fallback"
    assert r["parse_error"] is True
    assert r["wait_cost_seconds"] is None


def test_reset_wait_selftest_wrapper():
    """The ported --selftest control table all PASS."""
    assert reset_wait.selftest() == 0


# =========================================================================== budget (net-new)
def test_budget_go_above_the_reserve(project):
    """A run with plenty of allowance remaining is GO. The total and the reserve are read from
    project.yaml, never from code."""
    _write_project_yaml(project)
    conn = db.connect(project)
    budget.spend(conn, 40, session="s")            # small total 100, reserve 10 -> remaining 60
    assert budget.remaining(conn, root=project) == 60
    assert budget.posture(conn, root=project) == "GO"


def test_budget_holds_at_or_below_the_reserve(project):
    """A run that falls to or below the reserve enters HOLD rather than dying mid-op. The
    boundary is read from project.yaml. Done-when, the load-bearing budget control."""
    _write_project_yaml(project)
    conn = db.connect(project)
    budget.spend(conn, 89, session="s")            # remaining 11 -> still above reserve 10 -> GO
    assert budget.posture(conn, root=project) == "GO"
    budget.spend(conn, 1, session="s")             # remaining 10 -> at the reserve -> HOLD
    assert budget.posture(conn, root=project) == "HOLD"
    budget.spend(conn, 20, session="s")            # deeper below -> still HOLD, never dies
    assert budget.posture(conn, root=project) == "HOLD"


def test_budget_reads_the_class_total_from_project_yaml(project):
    """Changing the resource class in project.yaml changes the total the posture is measured
    against: the class is data, not code."""
    _write_project_yaml(project, resource_class="medium")
    conn = db.connect(project)
    budget.spend(conn, 400, session="s")           # medium total 500 -> remaining 100 -> GO
    assert budget.remaining(conn, root=project) == 100
    assert budget.posture(conn, root=project) == "GO"


def test_budget_concurrency_tier_from_project_yaml(project):
    """The concurrency tier (max live claims) is read per resource class from project.yaml; a
    tier breach is refused."""
    _write_project_yaml(project)                   # small tier = 1
    conn = db.connect(project)
    assert budget.concurrency_limit(root=project) == 1
    assert budget.concurrency_ok(conn, root=project, now="2026-09-16T00:00:05+00:00") is True
    db.append_event(conn, session="w1", actor="w1", kind=board.CLAIM_KIND, ref="R1",
                    data={"worker": "w1", "lease_until": "2026-09-16T02:00:00+00:00"})
    assert budget.concurrency_ok(conn, root=project, now="2026-09-16T00:00:05+00:00") is True
    db.append_event(conn, session="w2", actor="w2", kind=board.CLAIM_KIND, ref="R2",
                    data={"worker": "w2", "lease_until": "2026-09-16T02:00:00+00:00"})
    # two live claims over a tier of one: the tier is breached.
    assert budget.concurrency_ok(conn, root=project, now="2026-09-16T00:00:05+00:00") is False


def test_budget_preflight_floor_holds_a_run_that_is_below_budget(project):
    """The preflight floor refuses a run whose budget posture is HOLD; it passes a healthy run.
    Preflight floors are net-new to doctrine/preflight-floors.md."""
    _write_project_yaml(project)
    conn = db.connect(project)
    pre = budget.preflight(conn, root=project, now="2026-09-16T00:00:05+00:00")
    assert pre["ok"] is True
    budget.spend(conn, 95, session="s")            # remaining 5 -> HOLD
    pre = budget.preflight(conn, root=project, now="2026-09-16T00:00:05+00:00")
    assert pre["ok"] is False
    assert any("budget" in r for r in pre["reasons"])


def test_budget_selftest_wrapper(project):
    """The net-new budget/concurrency/preflight control table all PASS."""
    assert budget.selftest() == 0


# =========================================================================== composed takeover
def test_composed_takeover_dead_then_stale_fence_refused(project):
    """The takeover half (the earlier harness probe-composed-takeover), composed against the record: a worker
    whose progress stalls to DEAD is fenced out, a fresh worker re-claims under a strictly
    greater fence, and the dead worker's late write under its old fence is REFUSED, never a
    silent double-dispatch."""
    from alpaca import claims
    conn = db.connect(project)
    _add_row(conn, "R1")
    # W1 takes the row and beats with rising progress, then goes quiet.
    claims.take(conn, "R1", "W1", session="s1", now="2026-09-16T00:00:05+00:00")
    fence1 = claims.live(conn, "R1", now="2026-09-16T00:00:06+00:00")["fence"]
    pulse_watch.beat(conn, session="s1")
    db.append_event(conn, session="s1", actor="W1", kind="heartbeat", data={"n": 1})
    pulse_watch.beat(conn, session="s1")
    assert pulse_watch.classify(conn) == "healthy"
    pulse_watch.beat(conn, session="s1")           # first stall
    pulse_watch.beat(conn, session="s1")           # second stall
    assert pulse_watch.classify(conn) == "DEAD"    # W1 is dead by real progress
    # W2 takes over the dead worker's row under a strictly greater fence.
    to = claims.takeover(conn, "R1", "W2", session="s2", now="2026-09-16T00:00:40+00:00")
    assert to["fence"] > fence1
    holder2 = claims.live(conn, "R1", now="2026-09-16T00:00:41+00:00")
    assert holder2["worker"] == "W2"
    # W1 wakes and tries a late renew under its stale fence: REFUSED, never laundered.
    with pytest.raises(claims.StaleLease):
        claims.renew(conn, "R1", "W1", fence=fence1, session="s1",
                     now="2026-09-16T00:00:42+00:00")
