import datetime, json
from alpaca import cli, db, ops
from alpaca.tests import proofkit

def _setup(project):
    cli.main(["init"]); return db.connect(project)

def test_op_new_and_list(project, capsys):
    conn = _setup(project)
    assert cli.main(["op", "new", "add hello verb", "--done-when", "hello prints"]) == 0
    assert ops.current_op(conn)["id"] == "op-001"
    cli.main(["op", "list"]); out = capsys.readouterr().out
    assert "op-001" in out and "add hello verb" in out

def test_task_lifecycle(project, capsys):
    conn = _setup(project)
    cli.main(["op", "new", "x"])
    assert cli.main(["task", "add", "--title", "task", "op-001", "write the verb", "--phase", "build"]) == 0
    assert ops.first_open_task(conn)["id"] == "t-001"
    assert cli.main(["task", "claim", "t-001", "--by", "agent-a", "--minutes", "30"]) == 0
    assert db.rows(conn, "tasks", "id=?", ("t-001",))[0]["status"] == "doing"
    proof = proofkit.seal_for(project, "t-001")
    assert cli.main(["task", "move", "t-001", "done", "--proof", proof]) == 0
    assert db.rows(conn, "tasks", "id=?", ("t-001",))[0]["status"] == "done"
    kinds = [e["kind"] for e in db.events(conn)]
    assert kinds[-3:] == ["task-claim", "task-move", "task-move"] or "task-move" in kinds

def test_done_requires_proof(project):
    conn = _setup(project); cli.main(["op", "new", "x"]); cli.main(["task", "add", "--title", "task", "op-001", "s"])
    assert cli.main(["task", "move", "t-001", "done"]) == 1

def test_lease_expiry_returns_task(project):
    conn = _setup(project); cli.main(["op", "new", "x"]); cli.main(["task", "add", "--title", "task", "op-001", "s"])
    cli.main(["task", "claim", "t-001", "--by", "w1", "--minutes", "1"])
    later = (datetime.datetime.now().astimezone() + datetime.timedelta(minutes=5)).isoformat(timespec="seconds")
    assert ops.expire_leases(conn, later) == ["t-001"]
    assert db.rows(conn, "tasks", "id=?", ("t-001",))[0]["status"] == "open"

def test_op_close_needs_all_tasks_done(project):
    conn = _setup(project); cli.main(["op", "new", "x"]); cli.main(["task", "add", "--title", "task", "op-001", "s"])
    assert cli.main(["op", "close", "op-001", "--basis", "done"]) == 2
    cli.main(["task", "move", "t-001", "done", "--proof", proofkit.seal_for(project, "t-001")])
    assert cli.main(["op", "close", "op-001", "--basis", "done"]) == 0

def test_op_close_already_closed_is_refused(project, capsys):
    conn = _setup(project); cli.main(["op", "new", "x"])
    cli.main(["op", "close", "op-001", "--basis", "done"])
    n_events = len(db.events(conn))
    assert cli.main(["op", "close", "op-001", "--basis", "again"]) == 1
    assert "already closed" in capsys.readouterr().out
    assert len(db.events(conn)) == n_events

def test_msg_post_and_read(project, capsys):
    _setup(project)
    assert cli.main(["msg", "post", "--from", "orch", "--to", "w1", "--kind", "handoff", "take t-001"]) == 0
    cli.main(["msg", "read", "--to", "w1"]); assert "take t-001" in capsys.readouterr().out

def test_op_close_unknown_op_fails(project):
    _setup(project)
    assert cli.main(["op", "close", "op-999", "--basis", "x"]) == 1

def test_task_add_unknown_op_fails(project):
    conn = _setup(project)
    assert cli.main(["task", "add", "--title", "task", "op-999", "orphan"]) == 1
    assert db.rows(conn, "tasks") == []

def test_msg_post_empty_to_fails(project):
    conn = _setup(project)
    assert cli.main(["msg", "post", "--from", "a", "--to", "", "--kind", "note", "x"]) == 1
    assert db.rows(conn, "messages") == []

def test_claim_only_open_tasks(project):
    conn = _setup(project); cli.main(["op", "new", "x"]); cli.main(["task", "add", "--title", "task", "op-001", "s"])
    cli.main(["task", "move", "t-001", "done", "--proof", proofkit.seal_for(project, "t-001")])
    assert cli.main(["task", "claim", "t-001", "--by", "w1"]) == 1
    assert db.rows(conn, "tasks", "id=?", ("t-001",))[0]["status"] == "done"

def test_task_title_is_short_and_recorded(project, capsys):
    conn = _setup(project); cli.main(["op", "new", "x"])
    long_statement = "B.0 cva6 first physical run: 6_final.gds non-empty, route DRC 0, KLayout DRC 0"
    assert cli.main(["task", "add", "op-001", long_statement]) == 1
    assert cli.main(["task", "add", "op-001", long_statement, "--title", "  "]) == 1
    assert "--title is required" in capsys.readouterr().out
    assert cli.main(["task", "add", "op-001", long_statement, "--title", "x" * 61]) == 1
    assert cli.main(["task", "add", "op-001", long_statement, "--title", "B.0 cva6 first GDS"]) == 0
    assert db.rows(conn, "tasks") and len(db.rows(conn, "tasks")) == 1
    row = db.rows(conn, "tasks", "id=?", ("t-001",))[0]
    assert row["title"] == "B.0 cva6 first GDS" and row["statement"] == long_statement
    assert cli.main(["task", "title", "t-001", "cva6 first GDS"]) == 0
    assert cli.main(["task", "title", "t-001", "   "]) == 1
    row = db.rows(conn, "tasks", "id=?", ("t-001",))[0]
    assert row["title"] == "cva6 first GDS" and row["statement"] == long_statement
    renamed = [e for e in db.events(conn) if e["kind"] == "task-title"]
    assert len(renamed) == 1 and renamed[0]["data"] == {"from": "B.0 cva6 first GDS", "to": "cva6 first GDS"}
    capsys.readouterr(); cli.main(["task", "list"])
    assert "cva6 first GDS" in capsys.readouterr().out
