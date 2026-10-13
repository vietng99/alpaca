"""op-057 t-616: the deferred task state. Bar 1: a move into deferred needs --reason and
--reactivate-when. Bar 2: a deferred task moves to open only. Bar 3: op close takes done plus
deferred tasks and lists the deferred ones with their triggers."""
import json, random, string
import pytest
from alpaca import cli, claims, db, ops
from alpaca.tests import proofkit

R, W = "waiting on the vendor", "when the vendor ships v2"


def _setup(project, tasks=1):
    cli.main(["init"]); cli.main(["op", "new", "x", "--done-when", "d"])
    for i in range(tasks):
        assert cli.main(["task", "add", "--title", "task %d" % (i + 1), "op-001", "s%d" % (i + 1), "--phase", "build"]) == 0
    return db.connect(project)

def _move(*a):
    return cli.main(["task", "move", *a])

def _defer(tid="t-001", reason=R, when=W):
    return _move(tid, "deferred", "--reason", reason, "--reactivate-when", when)

def _status(conn, tid="t-001"):
    return db.rows(conn, "tasks", "id=?", (tid,))[0]["status"]

def _snap(conn):
    n = conn.execute("SELECT COUNT(*), MAX(id) FROM events").fetchone()
    return (n[0], n[1], [dict(r) for r in db.rows(conn, "tasks", "1=1 ORDER BY id")])

def _last_move(conn, tid="t-001"):
    r = conn.execute("SELECT data FROM events WHERE kind='task-move' AND ref=? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    return json.loads(r["data"])

def _to_state(conn, state):
    if state == "doing":
        assert cli.main(["task", "claim", "t-001", "--by", "w1"]) == 0
    elif state == "blocked":
        assert _move("t-001", "blocked", "--reason", "stuck") == 0


# ---- bar 1
@pytest.mark.parametrize("start", ["open", "doing", "blocked"])
@pytest.mark.parametrize("flags", [[], ["--reason", "r"], ["--reactivate-when", "w"],
                                   ["--reason", " ", "--reactivate-when", "w"],
                                   ["--reason", "r", "--reactivate-when", "  "]])
def test_defer_needs_reason_and_trigger(project, capsys, start, flags):
    conn = _setup(project); _to_state(conn, start); before = _snap(conn); capsys.readouterr()
    assert _move("t-001", "deferred", *flags) == 1
    assert "GATE alpaca-task-move: FAIL" in capsys.readouterr().out
    assert _snap(conn) == before and _status(conn) == start

@pytest.mark.parametrize("start", ["open", "doing", "blocked"])
def test_defer_records_reason_and_trigger(project, start):
    conn = _setup(project); _to_state(conn, start)
    assert _defer(reason="  %s " % R, when=" %s " % W) == 0
    row = db.rows(conn, "tasks", "id=?", ("t-001",))[0]
    assert row["status"] == "deferred" and row["claimant"] is None and row["lease_until"] is None
    assert _last_move(conn) == {"from": start, "to": "deferred", "proof": None, "reason": R, "reactivate_when": W}
    got = ops.deferral(conn, "t-001")
    assert got["reason"] == R and got["reactivate_when"] == W

def test_defer_ends_the_claim(project):
    conn = _setup(project); _to_state(conn, "doing")
    assert _defer() == 0
    kinds = [e["kind"] for e in db.events(conn)]
    assert "claim-release" in kinds[:kinds.index("task-move")]
    assert claims.live(conn, "t-001") is None

def test_defer_event_fold_reads_deferred(project):
    # B01: the research state fold takes the newest status event; the release must not follow the move.
    from alpaca.research import state
    conn = _setup(project); _to_state(conn, "doing")
    assert _defer() == 0
    assert state._task_status(conn, "t-001") == "deferred"

@pytest.mark.parametrize("target", ["done", "blocked", "doing"])
def test_deferral_written_mid_move_is_not_overwritten(project, monkeypatch, target):
    # B02: another session defers the task after the guard read the row and before the write.
    conn = _setup(project)
    real = ops._intent_refused
    def racing(*a, **k):
        monkeypatch.setattr(ops, "_intent_refused", real)
        other = db.connect(project)
        assert _defer() == 0
        other.close()
        return False
    monkeypatch.setattr(ops, "_intent_refused", racing)
    args = ["--reason", "x"] if target == "blocked" else []
    assert _move("t-001", target, *args) == 1
    assert _status(conn) == "deferred"
    moves = [e["data"]["to"] for e in db.events(conn) if e["kind"] == "task-move"]
    assert moves == ["deferred"]

def test_reactivate_when_goes_only_with_deferred(project):
    conn = _setup(project); before = _snap(conn)
    assert _move("t-001", "blocked", "--reactivate-when", "x") == 1
    assert _snap(conn) == before

@pytest.mark.parametrize("when", ["after:t10", "op:op-001", "date:2026-02-30", "after:t-001",
                                  "after:t-999", "op:op-999 closed"])
def test_defer_refuses_a_trigger_that_cannot_fire(project, when):
    conn = _setup(project); before = _snap(conn)
    assert _defer(when=when) == 1 and _snap(conn) == before

@pytest.mark.parametrize("when", [W, "after:t-002", "AFTER:T-002", "op:op-001 closed", "date:2001-01-01",
                                  "when the API lands, after:t-002"])
def test_defer_accepts_free_text_and_forms(project, when):
    conn = _setup(project, 2)
    assert _defer(when="  %s  " % when) == 0
    assert _last_move(conn)["reactivate_when"] == when


# ---- bar 2
def test_deferred_moves_to_open_only(project, capsys):
    conn = _setup(project); assert _defer() == 0
    ptr = proofkit.seal_for(project, "t-001"); before = _snap(conn); capsys.readouterr()
    for argv in (["done", "--proof", ptr], ["done", "--proof", ptr, "--by", "research-runner"], ["doing"],
                 ["blocked", "--reason", "x"], ["deferred", "--reason", "r", "--reactivate-when", "w"]):
        assert _move("t-001", *argv) == 1
        assert "move it to open first" in capsys.readouterr().out
    assert _snap(conn) == before and _status(conn) == "deferred"

def test_deferred_back_to_open_then_normal_flow(project):
    conn = _setup(project); assert _defer() == 0
    assert _move("t-001", "open") == 0
    assert _last_move(conn) == {"from": "deferred", "to": "open", "proof": None, "reason": None}
    assert cli.main(["task", "claim", "t-001", "--by", "w1"]) == 0
    assert _move("t-001", "done", "--proof", proofkit.seal_for(project, "t-001")) == 0

def test_claim_of_deferred_is_refused(project):
    conn = _setup(project); _defer(); before = _snap(conn)
    assert cli.main(["task", "claim", "t-001", "--by", "w1"]) == 1 and _snap(conn) == before

def test_refused_move_runs_no_gate(project, monkeypatch):
    from alpaca import intentgate
    seen = []
    monkeypatch.setattr(intentgate, "task_gate", lambda *a, **k: seen.append(a) or None)
    conn = _setup(project); _defer(); seen.clear()
    assert _move("t-001", "doing") == 1
    assert _move("t-001", "done", "--proof", "local:x") == 1
    assert seen == []

def test_claims_cannot_take_deferred_out(project):
    conn = _setup(project); _to_state(conn, "doing"); _defer()
    later = "2999-01-01T00:00:00+00:00"
    for call in (lambda: claims.release(conn, "t-001", "w1"), lambda: claims.take(conn, "t-001", "w2"),
                 lambda: claims.takeover(conn, "t-001", "w2"), lambda: claims.expire_due(conn, now=later),
                 lambda: ops.expire_leases(conn, later)):
        try: call()
        except claims.ClaimError: pass
        assert _status(conn) == "deferred"

def test_research_close_leaves_deferred(project):
    from alpaca.research import proofs
    conn = _setup(project, 0)
    # a deferred task is left as it is by the research close
    assert cli.main(["task", "add", "--title", "r", "op-001", "s", "--phase", "build"]) == 0
    assert _defer() == 0
    assert proofs.close(conn, project, "t-001", session="engine", to="blocked", reason="x") == 0
    assert _status(conn) == "deferred"


# ---- bar 3
def _closed_data(conn):
    r = conn.execute("SELECT data FROM events WHERE kind='op-close' ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(r["data"])

def _done(project, tid):
    assert cli.main(["task", "claim", tid, "--by", "w1"]) == 0
    assert _move(tid, "done", "--proof", proofkit.seal_for(project, tid)) == 0

def test_op_close_lists_deferred(project):
    conn = _setup(project, 3); _done(project, "t-001")
    assert _defer("t-003", "r3", "after:t-001") == 0 and _defer("t-002", "r2", "later") == 0
    assert cli.main(["op", "close", "op-001", "--basis", "b"]) == 0
    assert _closed_data(conn) == {"basis": "b", "deferred": [
        {"id": "t-002", "reason": "r2", "reactivate_when": "later"},
        {"id": "t-003", "reason": "r3", "reactivate_when": "after:t-001"}]}

def test_op_close_all_deferred_closes(project):
    _setup(project, 2); _defer("t-001"); _defer("t-002")
    assert cli.main(["op", "close", "op-001", "--basis", "b"]) == 0

def test_op_close_open_plus_deferred_blocks(project, capsys):
    conn = _setup(project, 2); _defer("t-002"); capsys.readouterr()
    assert cli.main(["op", "close", "op-001", "--basis", "b"]) == 2
    out = capsys.readouterr().out
    assert "BLOCKED (1 tasks not done: t-001)" in out and "t-002" not in out
    assert db.rows(conn, "ops", "id=?", ("op-001",))[0]["status"] == "open"

def test_op_close_deferred_without_trigger_blocks(project, capsys):
    conn = _setup(project); db.patch(conn, "tasks", "id", "t-001", {"status": "deferred"}); capsys.readouterr()
    assert cli.main(["op", "close", "op-001", "--basis", "b"]) == 2
    assert "no trigger on the record: t-001" in capsys.readouterr().out

def test_op_close_uses_the_newest_trigger(project):
    conn = _setup(project); _defer(when="first"); _move("t-001", "open"); _defer(when="second")
    assert cli.main(["op", "close", "op-001", "--basis", "b"]) == 0
    assert [d["reactivate_when"] for d in _closed_data(conn)["deferred"]] == ["second"]

def test_op_close_without_deferred_keeps_old_data(project):
    conn = _setup(project); _done(project, "t-001")
    assert cli.main(["op", "close", "op-001", "--basis", "b"]) == 0
    assert _closed_data(conn) == {"basis": "b"}


# ---- parser
def _forms(text):
    return [(f["kind"], f["target"]) for f in ops.parse_trigger(text)["forms"]]

@pytest.mark.parametrize("text,forms", [
    ("after:t-010", [("after", "t-010")]), ("AFTER:T-010", [("after", "t-010")]),
    ("OP:OP-012 CLOSED", [("op-closed", "op-012")]), ("date:2026-12-01", [("date", "2026-12-01")]),
    ("  after:t-010  ", [("after", "t-010")]),
    ("after:t-010; date:2026-12-01", [("after", "t-010"), ("date", "2026-12-01")]),
    ("when the API lands, after:t-002", [("after", "t-002")]),
    ("after:t-010 after:t-010", [("after", "t-010")]),
    ("when the vendor ships v2", []), ("after the release", []), ("note: wait for the Q3 budget", []),
    ("afterwards, maybe", []), ("the op closed last week", []), ("https://example.com/issue/12", []),
    ("ask Linh: is the budget approved?", []), ("", []), ("   ", []), (None, []),
])
def test_parse_trigger_good(text, forms):
    got = ops.parse_trigger(text)
    assert _forms(text) == forms and got["errors"] == []

@pytest.mark.parametrize("text", [
    "after:t10", "after:t-", "after:", "after:task-2", "after:op-001", "op:op-012", "op:op-012 close",
    "op:op-002 done", "op:12 closed", "op:t-001 closed", "date:", "date:2026-12-1", "date:2026-12-1x",
    "date:2026-02-30", "date:2026-13-01", "date:2026-00-10", "date:26-12-01", "date:2026/12/01",
    "date:tomorrow", "date:next week"])
def test_parse_trigger_near_misses(text):
    got = ops.parse_trigger(text)
    assert got["forms"] == [] and got["errors"]

def test_parse_trigger_never_raises():
    rng = random.Random(616); pool = string.printable + "é中\x00"
    bits = ["after:", "op:", "date:", "t-1", "op-2", " closed", "2026-01-01"]
    for _ in range(2000):
        text = "".join(rng.choice(bits) if rng.random() < 0.2 else rng.choice(pool) for _ in range(rng.randint(0, 40)))
        got = ops.parse_trigger(text)
        assert isinstance(got["forms"], list) and isinstance(got["errors"], list) and "text" in got


# ---- readers and old flows
def test_deferred_task_reads_without_crash(project, capsys):
    from alpaca import export, pad
    conn = _setup(project); _defer(); capsys.readouterr()
    assert cli.main(["task", "list"]) == 0
    assert capsys.readouterr().out.split("\n")[0][7:15] == "deferred"
    pad.render(project); export.payload(conn, project)
    assert ops.first_open_task(conn) is None

def test_statuses():
    assert ops.STATUSES[:4] == ("open", "doing", "done", "blocked") and "deferred" in ops.STATUSES

# ---- research runner: a parked task is not worked (round 2, C01)
def _fake_runner(conn, tasks):
    import types
    from alpaca.research import runner
    fake = types.SimpleNamespace(conn=conn, start={"repair_tasks": list(tasks)}, run="run-1",
                                 session="s-run", next_round=lambda: 1)
    fake.status = lambda task: runner._Runner.status(fake, task)
    return runner._Runner, fake

def _claim_events(conn):
    return conn.execute("SELECT COUNT(*) FROM events WHERE kind='claim' AND ref='t-001'").fetchone()[0]

@pytest.mark.parametrize("park", ["deferred", "blocked"])
def test_resumed_run_leaves_a_parked_task_alone(project, park):
    from alpaca.research import state
    conn = _setup(project)
    assert cli.main(["task", "claim", "t-001", "--by", "run-1"]) == 0
    assert (_defer() if park == "deferred" else _move("t-001", "blocked", "--reason", "r")) == 0
    before = _claim_events(conn)
    cls, fake = _fake_runner(conn, ["t-001"])
    cls.take_leases(fake)
    assert _claim_events(conn) == before
    if park == "deferred": assert claims.live(conn, "t-001") is None
    assert state._task_status(conn, "t-001") == park

def test_run_stop_and_loop_skip_a_deferred_task(project):
    from alpaca.research import lifecycle
    assert lifecycle._SETTLED == ("done", "blocked", "deferred")

def test_close_take_back_skips_a_task_deferred_in_the_window(project, monkeypatch):
    from alpaca.research import proofs
    conn = _setup(project)
    taken = []
    monkeypatch.setattr(proofs.leases, "live", lambda c, t: None)
    monkeypatch.setattr(proofs.leases, "take", lambda *a, **k: taken.append(a))
    monkeypatch.setattr(proofs, "_research", lambda c, t: {"run": "run-1"})
    real = cli.main
    def racing(argv):
        monkeypatch.setattr(cli, "main", real)
        assert _defer() == 0
        return 1
    monkeypatch.setattr(cli, "main", racing)
    code = proofs.close(conn, str(project), "t-001", session="s-run", to="blocked", reason="r")
    assert code != 0 and taken == [] and _status(conn) == "deferred"
