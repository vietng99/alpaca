"""M2.5 proof: claims, leases, expiry and takeover, keyed on the worker.

Asserts the Done-when on BOTH the positive and the negative path, with a FixedClock driving
every lease and staleness instant (spec 5.7:460-462, 5.4:352-354; M2 "a lease expires and the
row returns"):

  * a claim makes the row live and the board derives `doing`; the claim event and the first
    heartbeat land BEFORE the current-state mutation, so a session peeking in the gap sees a
    live claim from the record (Step 3).
  * an expired lease returns its row to todo WITH a recorded `lease-expired` event; a tracker
    task returns to `open` (Step 1, M2 done-when).
  * a second claim on a LIVE row is recorded as concurrency-detected rather than silently
    overwriting the incumbent (Step 4); the signal is readable for the resolve pass (M4.5).
  * a lease and its fence token refuse a late writer from an expired-then-superseded lease
    (Step 2): after a takeover, the old holder's fence is stale and a renew on it is refused.
  * a takeover is keyed on the WORKER, not the session: the same worker across two sessions
    renews rather than conflicting; a different worker takes over and bumps the fence.
  * renew before expiry extends the lease so the row stays live past the original instant.
  * alpaca task claim moves onto claims: the CLI verb lands a claims `claim` event and the derived
    board column is `doing`.
"""
import pytest

from alpaca import board, claims, cli, clock, db, util
from alpaca.checklist import verdict_row
from alpaca.gates import verdict as vc
from alpaca.tests import proofkit


# --------------------------------------------------------------- fixtures / helpers
def _setup(project):
    cli.main(["init"])
    return db.connect(project)


def _row(conn, rid, *, op=None, phase="build", step="s1",
         statement="do the thing properly here now", proof="local:spec.md"):
    r = {
        "id": rid, "kind": "item", "op": op, "phase": phase, "step": step,
        "statement": statement, "proof": proof, "where_": "", "how": "", "when_": "",
        "why": "", "session": None, "operator": None, "status": "open", "tag": "Specced",
        "content_hash": util.sha256_hex("row/" + rid), "prev_hash": None, "supersedes": None,
    }
    db.upsert(conn, "rows", "id", r)
    return r


def _make_task(project):
    cli.main(["op", "new", "x"])
    cli.main(["task", "add", "--title", "task", "op-001", "s"])   # yields t-001


def _kinds_for(conn, rid):
    return [e["kind"] for e in db.events(conn, limit=10 ** 9) if e["ref"] == rid]


def _card_column(conn, rid):
    for c in board.view(conn)["cards"]:
        if c["row_id"] == rid:
            return c["column"]
    raise AssertionError("row %r not on the board" % rid)


T0 = "2026-01-01T00:00:00+00:00"


def _at(minutes):
    import datetime
    return (datetime.datetime.fromisoformat(T0)
            + datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")


# --------------------------------------------------------------- claim -> live / doing
def test_a_claim_makes_the_row_live_and_the_board_shows_doing(project):
    conn = _setup(project)
    r = _row(conn, "r-live")
    # the board derives its column against the installed clock, so drive both from one FixedClock.
    try:
        util.set_clock(clock.FixedClock(start=T0))
        res = claims.take(conn, r["id"], "alice", minutes=60)
        assert res["status"] == claims.CLAIMED
        live = claims.live(conn, r["id"])
        assert live and live["worker"] == "alice"
        assert _card_column(conn, r["id"]) == board.DOING
    finally:
        util.set_clock(None)


def test_claim_and_first_heartbeat_land_before_the_mutation(project):
    conn = _setup(project)
    _make_task(project)
    claims.take(conn, "t-001", "w1", minutes=30, now=T0)
    kinds = _kinds_for(conn, "t-001")
    # the claim event precedes the first heartbeat, and both precede nothing that overwrites
    # the derived live claim: a peek via the record (live, which reads events) sees it.
    assert kinds.index(board.CLAIM_KIND) < kinds.index(claims.HEARTBEAT_KIND)
    assert claims.live(conn, "t-001", now=T0)["worker"] == "w1"
    # the current-state mutation followed: the tracker row is doing.
    assert db.rows(conn, "tasks", "id=?", ("t-001",))[0]["status"] == "doing"


# --------------------------------------------------------------- expiry returns the row
def test_an_expired_lease_returns_the_obligation_row_to_todo_with_an_event(project):
    conn = _setup(project)
    r = _row(conn, "r-exp")
    try:
        util.set_clock(clock.FixedClock(start=T0))
        claims.take(conn, r["id"], "alice", minutes=10)
        assert _card_column(conn, r["id"]) == board.DOING
        util.set_clock(clock.FixedClock(start=_at(20)))
        freed = claims.expire_due(conn)
        assert freed == [r["id"]]
        assert claims.EXPIRE_KIND in _kinds_for(conn, r["id"])
        assert claims.live(conn, r["id"]) is None
        assert _card_column(conn, r["id"]) == board.TODO
    finally:
        util.set_clock(None)


def test_an_expired_lease_returns_a_tracker_task_to_open(project):
    conn = _setup(project)
    _make_task(project)
    claims.take(conn, "t-001", "w1", minutes=10, now=T0)
    assert db.rows(conn, "tasks", "id=?", ("t-001",))[0]["status"] == "doing"
    assert claims.expire_due(conn, now=_at(20)) == ["t-001"]
    assert db.rows(conn, "tasks", "id=?", ("t-001",))[0]["status"] == "open"


def test_expire_due_leaves_a_live_lease_alone(project):
    conn = _setup(project)
    r = _row(conn, "r-still")
    claims.take(conn, r["id"], "alice", minutes=60, now=T0)
    assert claims.expire_due(conn, now=_at(10)) == []
    assert claims.live(conn, r["id"], now=_at(10))["worker"] == "alice"


# --------------------------------------------------------------- concurrency, not overwrite
def test_a_second_claim_on_a_live_row_is_concurrency_not_overwrite(project):
    conn = _setup(project)
    r = _row(conn, "r-conc")
    first = claims.take(conn, r["id"], "alice", minutes=60, now=T0)
    res = claims.take(conn, r["id"], "bob", minutes=60, now=_at(1))
    assert res["status"] == claims.CONCURRENCY
    # the incumbent is untouched: still alice, still her fence.
    live = claims.live(conn, r["id"], now=_at(1))
    assert live["worker"] == "alice"
    assert live["fence"] == first["fence"]
    # the signal is on the record for the resolve pass (M4.5).
    sigs = claims.concurrency_signals(conn)
    assert len(sigs) == 1
    assert sigs[0]["data"]["incumbent"] == "alice"
    assert sigs[0]["data"]["challenger"] == "bob"


# --------------------------------------------------------------- fence refuses a late writer
def test_a_late_writer_from_a_superseded_lease_is_refused(project):
    conn = _setup(project)
    r = _row(conn, "r-fence")
    a = claims.take(conn, r["id"], "alice", minutes=10, now=T0)
    # bob seizes the row by takeover; his fence is higher than alice's.
    b = claims.takeover(conn, r["id"], "bob", minutes=10, now=_at(1))
    assert b["fence"] > a["fence"]
    # alice, a late writer from the superseded lease, is refused on her stale fence.
    with pytest.raises(claims.StaleLease):
        claims.renew(conn, r["id"], "alice", fence=a["fence"], minutes=10, now=_at(2))
    # and refused even without naming the fence, because the holder changed.
    with pytest.raises(claims.StaleLease):
        claims.renew(conn, r["id"], "alice", minutes=10, now=_at(2))


def test_renew_on_a_never_claimed_row_is_refused(project):
    conn = _setup(project)
    r = _row(conn, "r-nolease")
    with pytest.raises(claims.StaleLease):
        claims.renew(conn, r["id"], "alice", minutes=10, now=T0)


# --------------------------------------------------------------- keyed on worker, not session
def test_takeover_is_keyed_on_the_worker_not_the_session(project):
    conn = _setup(project)
    r = _row(conn, "r-key")
    a1 = claims.take(conn, r["id"], "alice", minutes=30, now=T0, session="s1")
    # the SAME worker on a DIFFERENT session is not a conflict: it renews in place.
    a2 = claims.take(conn, r["id"], "alice", minutes=30, now=_at(1), session="s2")
    assert a2["status"] == claims.CLAIMED
    assert a2["fence"] == a1["fence"]
    assert claims.live(conn, r["id"], now=_at(1))["worker"] == "alice"
    # a DIFFERENT worker takes over: the holder and the fence both change.
    b = claims.takeover(conn, r["id"], "bob", minutes=30, now=_at(2))
    assert b["prev_worker"] == "alice"
    assert b["fence"] > a1["fence"]
    assert claims.live(conn, r["id"], now=_at(2))["worker"] == "bob"


# --------------------------------------------------------------- renew extends the lease
def test_renew_before_expiry_extends_the_lease(project):
    conn = _setup(project)
    r = _row(conn, "r-renew")
    claims.take(conn, r["id"], "alice", minutes=10, now=T0)      # lease to T0+10
    claims.renew(conn, r["id"], "alice", minutes=10, now=_at(5))  # lease to T0+15
    # past the ORIGINAL expiry the lease is still live because it was renewed.
    assert claims.live(conn, r["id"], now=_at(12))["worker"] == "alice"
    assert claims.expire_due(conn, now=_at(12)) == []


# --------------------------------------------------------------- FixedClock via set_clock
def test_fixed_clock_drives_expiry_without_an_explicit_now(project):
    conn = _setup(project)
    r = _row(conn, "r-fc")
    try:
        util.set_clock(clock.FixedClock(start=T0))
        claims.take(conn, r["id"], "alice", minutes=10)
        util.set_clock(clock.FixedClock(start=_at(20)))
        assert claims.expire_due(conn) == [r["id"]]
    finally:
        util.set_clock(None)


# --------------------------------------------------------------- alpaca task claim onto claims
def test_alpaca_task_claim_moves_onto_claims(project):
    conn = _setup(project)
    _make_task(project)
    assert cli.main(["task", "claim", "t-001", "--by", "w1", "--minutes", "30"]) == vc.PASS
    assert db.rows(conn, "tasks", "id=?", ("t-001",))[0]["status"] == "doing"
    # the claim landed a claims `claim` event, so the record shows the live claim.
    assert board.CLAIM_KIND in _kinds_for(conn, "t-001")
    assert claims.live(conn, "t-001") is not None


def test_alpaca_task_claim_still_refuses_a_non_open_task(project):
    conn = _setup(project)
    cli.main(["op", "new", "x"])
    cli.main(["task", "add", "--title", "task", "op-001", "s"])
    cli.main(["task", "move", "t-001", "done", "--proof", proofkit.seal_for(project, "t-001")])
    assert cli.main(["task", "claim", "t-001", "--by", "w1"]) == vc.FAIL
