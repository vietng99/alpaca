"""M3.8 proof: the dispatch protocol, the sole-spawner rule, and the heavy bracket.

Drives the Done-when on BOTH the positive and the negative path (spec 5.8:493-506, 7.3:761-771;
section 6 ADAPT row 84, the controller minus the two-human phase sign-off), a FixedClock (M2.2)
driving every lease and verdict instant:

  * an orchestrator posts assignments over the board; each is a `dispatch-assign` event plus a
    handoff message to the worker, and only the orchestrator may post them (Step 1, Step 4).
  * a worker claims its assigned row (M2.5) and lands a verdict row (the M1.13 contract) plus a
    result message; the fold advances the discharged rows and reopens the failed ones with their
    recorded reason (Step 1).
  * the sole-spawner rule: a budget is declared up front; a spawn from anywhere but the
    orchestrator is recorded as an event AND refused, and a spawn past the budget is refused
    (Step 4).
  * the heavy bracket quiesces, snapshots the chain head, stamps the op, dispatches ONE acceptance
    verdict by a blind verifier, and reaps exactly one verdict event (Step 3).
  * a worker that runs its OWN acceptance check on a row it built is refused (the blind-pair rule
    carried into dispatch; Step 1's negative path).
"""
import pytest

from alpaca import claims, cli, clock, db, messages, util
from alpaca.checklist import verdict_row
from alpaca.formation import dispatch
from alpaca.gates import verdict as vc


T0 = "2026-01-01T00:00:00+00:00"


def _setup(project):
    cli.main(["init"])
    return db.connect(project)


def _row(conn, rid, *, op="op-001", phase="build", step="s1",
         statement="do the thing properly here now", proof="local:spec.md"):
    r = {
        "id": rid, "kind": "item", "op": op, "phase": phase, "step": step,
        "statement": statement, "proof": proof, "where_": "", "how": "", "when_": "",
        "why": "", "session": None, "operator": None, "status": "open", "tag": "Specced",
        "content_hash": util.sha256_hex("row/" + rid), "prev_hash": None, "supersedes": None,
    }
    db.upsert(conn, "rows", "id", r)
    return r


def _events(conn, kind):
    return [e for e in db.events(conn, limit=10 ** 9) if e["kind"] == kind]


# ----------------------------------------------------------------- assign / claim / verdict
def test_orchestrator_assigns_rows_as_events_plus_messages(project):
    conn = _setup(project)
    _row(conn, "r-1"); _row(conn, "r-2")
    out = dispatch.assign(conn, "op-001", ["r-1", "r-2"], {"r-1": "alice", "r-2": "bob"})
    assert [a["worker"] for a in out] == ["alice", "bob"]
    # one dispatch-assign event per row, authored by the orchestrator, keyed on the row.
    ev = _events(conn, dispatch.ASSIGN_KIND)
    assert {e["ref"] for e in ev} == {"r-1", "r-2"}
    assert all(e["actor"] == dispatch.ORCHESTRATOR for e in ev)
    # a handoff message reached each worker.
    assert any(m["to_"] == "alice" for m in messages.read(conn, "alice"))
    assert any(m["to_"] == "bob" for m in messages.read(conn, "bob"))
    assert dispatch.worker_of(conn, "op-001", "r-1") == "alice"


def test_a_non_orchestrator_may_not_assign(project):
    conn = _setup(project)
    _row(conn, "r-1")
    with pytest.raises(dispatch.SoleSpawnerRefused):
        dispatch.assign(conn, "op-001", ["r-1"], {"r-1": "alice"}, by="alice")
    # the refusal is itself an event on the record.
    assert _events(conn, dispatch.SPAWN_REFUSED_KIND)


def test_worker_claims_and_lands_a_verdict_and_the_fold_advances(project):
    conn = _setup(project)
    r = _row(conn, "r-ok")
    dispatch.assign(conn, "op-001", ["r-ok"], {"r-ok": "alice"})
    claims.take(conn, "r-ok", "alice", minutes=60, now=T0)
    # a DIFFERENT worker runs the acceptance check (blind verifier) and discharges PASS.
    fx = clock.FixedClock(start=T0)
    res = dispatch.land_verdict(conn, "r-ok", r["content_hash"], "vera", vc.PASS,
                                acceptance=True, level="L2", clock=fx)
    assert res["verdict_event"]["kind"] == verdict_row.KIND
    # a result message carrying the verdict was posted, pointing at the row.
    assert any(m["kind"] == "result" and m["ref"] == "r-ok"
               for m in messages.read(conn))
    folded = dispatch.fold(conn, "op-001")
    assert "r-ok" in folded["advanced"]
    assert folded["reopened"] == []


def test_the_fold_reopens_a_failed_row_with_its_reason(project):
    conn = _setup(project)
    r = _row(conn, "r-bad")
    dispatch.assign(conn, "op-001", ["r-bad"], {"r-bad": "alice"})
    claims.take(conn, "r-bad", "alice", minutes=60, now=T0)
    fx = clock.FixedClock(start=T0)
    dispatch.land_verdict(conn, "r-bad", r["content_hash"], "vera", vc.FAIL,
                          reason="assertion X did not hold", acceptance=True, clock=fx)
    folded = dispatch.fold(conn, "op-001")
    assert "r-bad" not in folded["advanced"]
    reopened = {x["row"]: x["reason"] for x in folded["reopened"]}
    assert reopened["r-bad"] == "assertion X did not hold"
    # a reopen event carrying the reason is on the record.
    rev = _events(conn, dispatch.REOPEN_KIND)
    assert any(e["ref"] == "r-bad" and e["data"].get("reason") == "assertion X did not hold"
               for e in rev)


def test_a_worker_that_runs_its_own_acceptance_check_is_refused(project):
    conn = _setup(project)
    r = _row(conn, "r-self")
    dispatch.assign(conn, "op-001", ["r-self"], {"r-self": "alice"})
    claims.take(conn, "r-self", "alice", minutes=60, now=T0)
    fx = clock.FixedClock(start=T0)
    with pytest.raises(dispatch.SelfAcceptanceRefused):
        dispatch.land_verdict(conn, "r-self", r["content_hash"], "alice", vc.PASS,
                              acceptance=True, clock=fx)
    # nothing was discharged: the row stays open.
    assert verdict_row.status_fold(conn, "r-self") == verdict_row.OPEN
    # the refusal is an event.
    assert _events(conn, dispatch.SELF_ACCEPTANCE_REFUSED_KIND)


# ----------------------------------------------------------------- sole-spawner budget
def test_the_declared_budget_bounds_the_spawns(project):
    conn = _setup(project)
    _row(conn, "r-1"); _row(conn, "r-2"); _row(conn, "r-3")
    dispatch.declare_budget(conn, "op-001", 2)
    assert dispatch.declared_budget(conn, "op-001") == 2
    dispatch.assign(conn, "op-001", ["r-1", "r-2"], {"r-1": "alice", "r-2": "bob"})
    assert dispatch.spawn_count(conn, "op-001") == 2
    with pytest.raises(dispatch.BudgetExceeded):
        dispatch.assign(conn, "op-001", ["r-3"], {"r-3": "carol"})


def test_a_spawn_from_a_non_orchestrator_is_an_event_and_a_refusal(project):
    conn = _setup(project)
    with pytest.raises(dispatch.SoleSpawnerRefused):
        dispatch.spawn(conn, "op-001", "mallory", by="mallory")
    ev = _events(conn, dispatch.SPAWN_REFUSED_KIND)
    assert any(e["data"].get("worker") == "mallory" for e in ev)


# ----------------------------------------------------------------- the heavy bracket
def test_the_heavy_bracket_snapshots_stamps_and_reaps_one_verdict(project):
    conn = _setup(project)
    r = _row(conn, "r-hb")
    dispatch.assign(conn, "op-001", ["r-hb"], {"r-hb": "alice"})
    claims.take(conn, "r-hb", "alice", minutes=60, now=T0)
    before = len(verdict_row._verdict_events(conn, "r-hb"))
    fx = clock.FixedClock(start=T0)
    out = dispatch.heavy_bracket(conn, "op-001", "r-hb", r["content_hash"], "vera",
                                 vc.PASS, level="L2", clock=fx)
    # quiesce/snapshot captured the chain head, the op was stamped, exactly one verdict reaped.
    assert out["snapshot"]
    assert out["stamp"]["op"] == "op-001"
    assert out["reaped"] == 1
    assert len(verdict_row._verdict_events(conn, "r-hb")) == before + 1


def test_the_heavy_bracket_refuses_a_self_acceptance(project):
    conn = _setup(project)
    r = _row(conn, "r-hb2")
    dispatch.assign(conn, "op-001", ["r-hb2"], {"r-hb2": "alice"})
    fx = clock.FixedClock(start=T0)
    with pytest.raises(dispatch.SelfAcceptanceRefused):
        dispatch.heavy_bracket(conn, "op-001", "r-hb2", r["content_hash"], "alice",
                               vc.PASS, level="L2", clock=fx)
