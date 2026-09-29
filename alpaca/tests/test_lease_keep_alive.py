"""t-027: a working session keeps the task leases it holds, and a lapsed lease is shown as lapsed.

A claim carries a 60 minute lease. Before this task nothing renewed it: the hook heartbeats carry no
row, and `board.live_claims` / `sessions_view.claims_by_session` read only the `claim` events, so a
renewal heartbeat was not counted either. A session working for more than an hour on a claimed task
then showed on the cockpit as "No task claimed" while its task remained in `doing`
after its lease expired.

Asserted here:
  * every claim view counts a renewal heartbeat on the holder's fence;
  * `claims.keep_alive` leaves a fresh lease alone, renews one past half its length, takes a lapsed
    one again with a new fence, and never touches another session's lease, a released or expired
    row, or a finished task;
  * `sessions_view.lapsed_by_session` names a still-doing task whose holder's lease ran out;
  * the PostToolUse and UserPromptSubmit hooks run `keep_alive` for their own session;
  * `pulse.now` carries the lapsed ids beside the claims for the cockpit.
"""
import datetime
import json
import os
import subprocess
import sys

import pytest

from alpaca import board, claims, cli, clock, db, export, ops, util
from alpaca import sessions_view as sv

T0 = "2026-01-01T00:00:00+00:00"


def _at(minutes, base=T0):
    return (datetime.datetime.fromisoformat(base)
            + datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _task(conn, tid="t-001", status="open"):
    db.upsert(conn, "tasks", "id", {"id": tid, "op": "op-001", "statement": "s", "status": status})


@pytest.fixture
def conn(project):
    cli.main(["init"])
    c = db.connect(project)
    _task(c)
    yield c
    c.close()


def _row_events(conn, rid, kind):
    return [dict(r) for r in conn.execute(
        "SELECT session, kind, data FROM events WHERE ref=? AND kind=? ORDER BY id", (rid, kind))]


# ------------------------------------------------------------------ the fold counts renewals
def test_a_renewal_keeps_the_claim_live_in_every_view(conn):
    claims.take(conn, "t-001", "w1", minutes=60, session="s1", now=T0)
    claims.renew(conn, "t-001", "w1", minutes=60, session="s1", now=_at(50))
    # past the first lease (T0+60) and inside the renewed one (T0+110)
    assert "t-001" in board.live_claims(conn, now=_at(90))
    assert sv.claims_by_session(conn, _at(90)) == {"s1": ["t-001"]}
    assert sv.lapsed_by_session(conn, _at(90)) == {}
    assert board.live_claims(conn, now=_at(90))["t-001"]["since"] == T0, "since stays the take"


def test_a_renewal_on_a_superseded_fence_does_not_count(conn):
    claims.take(conn, "t-001", "w1", minutes=10, session="s1", now=T0)
    claims.takeover(conn, "t-001", "w2", minutes=10, session="s2", now=_at(20))
    # a late heartbeat on fence 1 (the superseded lease) must not extend fence 2
    db.append_event(conn, session="s1", actor="w1", kind="heartbeat", ref="t-001",
                    data={"worker": "w1", "fence": 1, "lease_until": _at(500)})
    assert "t-001" not in board.live_claims(conn, now=_at(60))
    assert board.fold_leases(conn)["t-001"]["session"] == "s2"


def test_the_holder_session_is_the_last_to_take_or_renew(conn):
    claims.take(conn, "t-001", "w1", minutes=60, session="s1", now=T0)
    claims.take(conn, "t-001", "w1", minutes=60, session="s2", now=_at(10))   # same worker renews
    assert sv.claims_by_session(conn, _at(20)) == {"s2": ["t-001"]}


# ------------------------------------------------------------------ keep_alive
def test_keep_alive_leaves_a_fresh_lease_alone(conn):
    claims.take(conn, "t-001", "w1", minutes=60, session="s1", now=T0)
    assert claims.keep_alive(conn, "s1", now=_at(10)) == []
    assert len(_row_events(conn, "t-001", "heartbeat")) == 1, "only the take's own heartbeat"


def test_keep_alive_renews_a_lease_past_half_its_length(conn):
    claims.take(conn, "t-001", "w1", minutes=60, session="s1", now=T0)
    done = claims.keep_alive(conn, "s1", now=_at(31))
    assert [d["row"] for d in done] == ["t-001"] and done[0]["action"] == "renewed"
    assert claims.live(conn, "t-001", now=_at(80))["fence"] == 1
    assert db.rows(conn, "tasks", "id=?", ("t-001",))[0]["lease_until"] == _at(91)
    # the renewed lease is now fresh again: a second call writes nothing
    assert claims.keep_alive(conn, "s1", now=_at(32)) == []


def test_keep_alive_takes_a_lapsed_lease_again(conn):
    claims.take(conn, "t-001", "w1", minutes=60, session="s1", now=T0)
    assert sv.claims_by_session(conn, _at(120)) == {}
    assert sv.lapsed_by_session(conn, _at(120)) == {"s1": ["t-001"]}
    done = claims.keep_alive(conn, "s1", now=_at(120))
    assert done[0]["action"] == "re-claimed" and done[0]["fence"] == 2
    assert sv.claims_by_session(conn, _at(121)) == {"s1": ["t-001"]}
    assert sv.lapsed_by_session(conn, _at(121)) == {}
    reason = json.loads(_row_events(conn, "t-001", "claim")[-1]["data"])["reason"]
    assert "keep_alive" in reason


def test_keep_alive_never_touches_another_session_s_lease(conn):
    claims.take(conn, "t-001", "w1", minutes=60, session="s1", now=T0)
    assert claims.keep_alive(conn, "s2", now=_at(45)) == []
    assert claims.keep_alive(conn, "s2", now=_at(120)) == []
    assert len(_row_events(conn, "t-001", "claim")) == 1


def test_keep_alive_leaves_a_row_another_worker_took_over(conn):
    claims.take(conn, "t-001", "w1", minutes=10, session="s1", now=T0)
    claims.takeover(conn, "t-001", "w2", minutes=60, session="s2", now=_at(20))
    assert claims.keep_alive(conn, "s1", now=_at(100)) == []
    assert claims.live(conn, "t-001", now=_at(70))["worker"] == "w2"


@pytest.mark.parametrize("end", ["release", "expire-then-move", "done"])
def test_keep_alive_leaves_an_ended_lease_or_a_finished_task(conn, end):
    claims.take(conn, "t-001", "w1", minutes=60, session="s1", now=T0)
    if end == "release":
        claims.release(conn, "t-001", "w1", session="s1", now=_at(5))
    elif end == "expire-then-move":
        assert claims.expire_due(conn, now=_at(90)) == ["t-001"]
        cli.main(["task", "move", "t-001", "open"])     # somebody decided after the expiry
    else:
        _task(conn, status="done")
    before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert claims.keep_alive(conn, "s1", now=_at(120)) == []
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
    assert sv.lapsed_by_session(conn, _at(120)) == {}


@pytest.mark.parametrize("sweep", ["claims", "ops"])
def test_keep_alive_takes_back_a_task_the_lease_sweep_returned_to_open(conn, sweep):
    claims.take(conn, "t-001", "w1", minutes=60, session="s1", now=T0)
    if sweep == "claims":
        assert claims.expire_due(conn, now=_at(90)) == ["t-001"]
    else:
        assert ops.expire_leases(conn, now=_at(90)) == ["t-001"]   # the sweep pad.write runs
    assert db.rows(conn, "tasks", "id=?", ("t-001",))[0]["status"] == "open"
    assert claims.keep_alive(conn, "s2", now=_at(100)) == [], "only the session that held it"
    done = claims.keep_alive(conn, "s1", now=_at(100))
    assert done and done[0]["action"] == "re-claimed"
    task = db.rows(conn, "tasks", "id=?", ("t-001",))[0]
    assert (task["status"], task["claimant"]) == ("doing", "w1")
    assert sv.claims_by_session(conn, _at(101)) == {"s1": ["t-001"]}


def test_keep_alive_leaves_a_task_another_worker_claimed_after_the_sweep(conn):
    claims.take(conn, "t-001", "w1", minutes=60, session="s1", now=T0)
    claims.expire_due(conn, now=_at(90))
    claims.take(conn, "t-001", "w2", minutes=60, session="s2", now=_at(95))
    assert claims.keep_alive(conn, "s1", now=_at(100)) == []
    assert claims.live(conn, "t-001", now=_at(100))["worker"] == "w2"


# ------------------------------------------------------------------ the hooks
ENV = lambda project, sid: {**os.environ, "CLAUDE_PROJECT_DIR": project, "ALPACA_SESSION_ID": sid,
                            "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.dirname(__file__)))}


def _hook(module, payload, project):
    return subprocess.run([sys.executable, "-m", module], input=json.dumps(payload),
                          capture_output=True, text=True, encoding="utf-8", cwd=project,
                          env=ENV(project, payload["session_id"]))


def _ago(minutes):
    return _at(-minutes, base=util.now_iso())


@pytest.mark.parametrize("module,payload", [
    ("alpaca.hooks.post_tool", {"tool_name": "Read", "tool_input": {"file_path": "/x"}}),
    ("alpaca.hooks.user_prompt", {"prompt": "go"}),
])
def test_a_hook_takes_its_session_s_lapsed_lease_again(project, module, payload):
    cli.main(["init"])
    conn = db.connect(project)
    _task(conn)
    claims.take(conn, "t-001", "w1", minutes=60, session="s1", now=_ago(120))
    assert sv.claims_by_session(conn, util.now_iso()) == {}
    _hook(module, {"session_id": "s1", **payload}, project)
    held = sv.claims_by_session(conn, sv.record_now(conn))
    assert held == {"s1": ["t-001"]}
    conn.close()


def test_the_post_tool_hook_renews_past_half_and_ignores_other_sessions(project):
    cli.main(["init"])
    conn = db.connect(project)
    _task(conn)
    _task(conn, "t-002")
    claims.take(conn, "t-001", "w1", minutes=60, session="s1", now=_ago(40))
    claims.take(conn, "t-002", "w2", minutes=60, session="s2", now=_ago(40))
    _hook("alpaca.hooks.post_tool", {"session_id": "s1", "tool_name": "Read",
                                     "tool_input": {"file_path": "/x"}}, project)
    assert [e["session"] for e in _row_events(conn, "t-001", "heartbeat")] == ["s1", "s1"]
    assert len(_row_events(conn, "t-002", "heartbeat")) == 1, "s2's lease is s2's to renew"
    conn.close()


def test_no_claim_nudge_for_a_session_whose_lease_lapsed(project):
    cli.main(["init"])
    conn = db.connect(project)
    _task(conn)
    claims.take(conn, "t-001", "w1", minutes=60, session="s1", now=_ago(120))
    _hook("alpaca.hooks.post_tool", {"session_id": "s1", "tool_name": "Edit",
                                     "tool_input": {"file_path": "/x/y.py"}}, project)
    out = _hook("alpaca.hooks.user_prompt", {"session_id": "s1", "prompt": "go"}, project).stdout
    text = json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""
    assert "[alpaca claim]" not in text
    conn.close()


# ------------------------------------------------------------------ the cockpit feed
def test_pulse_now_carries_the_lapsed_ids_beside_the_claims(project):
    util.set_clock(clock.FixedClock("2026-03-01T00:00:00+00:00", step=0))
    try:
        cli.main(["init"])
        conn = db.connect(project)
        db.meta_set(conn, "onboarded", util.now_iso())
        _task(conn)
        claims.take(conn, "t-001", "w1", minutes=60, session="aaaaaaaa-1111",
                    now="2026-02-28T22:00:00+00:00")
        db.upsert(conn, "sessions", "sid", {"sid": "aaaaaaaa-1111", "started": util.now_iso(),
                                            "level": "L2", "beats": 1})
        db.meta_set(conn, "operator:aaaaaaaa-1111", "claude")
        db.append_event(conn, session="aaaaaaaa-1111", actor="agent", kind="heartbeat",
                        data={"tool": "Bash", "ref": "x"})
        work = json.loads(export.render(project))["pulse"]["now"]["sessions"]["work"]
        mine = [s for s in work if s["sid"] == "aaaaaaaa-1111"][0]
        assert mine["claims"] == [] and mine["lapsed"] == ["t-001"]
        conn.close()
    finally:
        util.set_clock(None)


PANEL = r"""
const m = await import(process.argv[2]);
const beat = new Date().toISOString();
const o = {tasks: [{id: 't-111', title: 'DRC memory scaling', status: 'doing'},
                   {id: 't-005', title: 'other work', status: 'doing'}]};
const live = [{sid: 'aaaa1111', active: true, last_beat: beat, claims: [], lapsed: ['t-111']},
              {sid: 'bbbb2222', active: true, last_beat: beat, claims: ['t-005'], lapsed: []},
              {sid: 'cccc3333', active: true, last_beat: beat, claims: []}];
process.stdout.write(JSON.stringify(m.sessionsPanel(o, live, []).split('<article').slice(1)));
"""


def test_the_cockpit_names_a_lapsed_lease_instead_of_no_task(tmp_path):
    import shutil
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on this host")
    js = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web", "cockpit.js")
    runner = tmp_path / "panel.mjs"
    runner.write_text(PANEL, encoding="utf-8")
    done = subprocess.run([node, str(runner), js], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    cards = {c.split('title="')[1].split('"')[0]: c for c in json.loads(done.stdout)}
    lapsed, held, bare = cards["aaaa1111"], cards["bbbb2222"], cards["cccc3333"]
    assert "Lease lapsed." in lapsed and "t-111" in lapsed and "No task claimed" not in lapsed
    assert "is-unclaimed" not in lapsed.split(">")[0]
    assert "No task claimed" not in held and "Lease lapsed" not in held
    assert "No task claimed" in bare
