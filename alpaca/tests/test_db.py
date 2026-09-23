import os, sqlite3
from alpaca import db, paths

def test_connect_creates_schema(project):
    conn = db.connect(project)
    names = {r["name"] for r in db.rows(conn, "sqlite_master", "type='table'")}
    assert {"events", "sessions", "ops", "tasks", "messages", "decisions", "meta"} <= names
    assert os.path.isfile(paths.db_path(project))

def test_append_chains_and_verifies(project):
    conn = db.connect(project)
    e1 = db.append_event(conn, session="s1", actor="agent", kind="session-start", data={"cwd": "/x"})
    e2 = db.append_event(conn, session="s1", actor="agent", kind="heartbeat", data={"tool": "Read"})
    assert e1["prev_hash"] == db.GENESIS
    assert e2["prev_hash"] == e1["hash"]
    assert db.verify_chain(conn) == (True, "2 events")

def test_tamper_is_detected(project):
    conn = db.connect(project)
    db.append_event(conn, session="s1", actor="agent", kind="a", data={})
    db.append_event(conn, session="s1", actor="agent", kind="b", data={})
    # This test models out-of-band corruption after bypassing the normal write guard.
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute("UPDATE events SET kind='c' WHERE id=1"); conn.commit()
    ok, reason = db.verify_chain(conn)
    assert ok is False and "1" in reason

def test_events_filter(project):
    conn = db.connect(project)
    for k in ("x", "y", "x"):
        db.append_event(conn, session="s", actor="a", kind=k)
    assert [e["kind"] for e in db.events(conn, kind="x")] == ["x", "x"]
    assert db.last_event(conn)["kind"] == "x"

def test_upsert_and_rows(project):
    conn = db.connect(project)
    db.upsert(conn, "ops", "id", {"id": "op-001", "intent": "i", "status": "open"})
    db.upsert(conn, "ops", "id", {"id": "op-001", "intent": "i", "status": "closed"})
    got = db.rows(conn, "ops", "id=?", ("op-001",))
    assert len(got) == 1 and got[0]["status"] == "closed"

def test_two_connections_append_alternately_keep_one_chain(project):
    a = db.connect(project); b = db.connect(project)
    for i in range(6):
        db.append_event(a if i % 2 == 0 else b, session="s", actor="x", kind="k%d" % i)
    ok, reason = db.verify_chain(db.connect(project))
    assert ok is True and reason == "6 events"
