"""M1.2: one transaction per record mutation, and onboarding inside it.

A record mutation is an event append plus a current-state write. Either both
land or neither does. A fault injected between the two leaves the record clean.
"""
import os
import pytest
from alpaca import cli, db, project as proj


# ---- (a) fault between the event append and the row write

def test_raise_inside_transaction_rolls_back_event_and_row(project):
    conn = db.connect(project)
    db.upsert(conn, "ops", "id", {"id": "op-001", "intent": "i", "status": "open"})
    db.append_event(conn, session="s", actor="a", kind="op-open", op="op-001")
    head_before = db.last_event(conn)["hash"]
    n_before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            db.append_event(conn, session="s", actor="a", kind="op-close",
                            op="op-001", conn_in_txn=True)
            db.patch(conn, "ops", "id", "op-001", {"status": "closed"})
            raise RuntimeError("fault between append and row write")

    # Neither the event nor the row write survived.
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == n_before
    assert db.last_event(conn)["hash"] == head_before
    assert db.rows(conn, "ops", "id=?", ("op-001",))[0]["status"] == "open"
    ok, _ = db.verify_chain(conn)
    assert ok is True


def test_transaction_commits_event_and_row_together(project):
    conn = db.connect(project)
    db.upsert(conn, "ops", "id", {"id": "op-001", "intent": "i", "status": "open"})
    with db.transaction(conn):
        db.append_event(conn, session="s", actor="a", kind="op-close",
                        op="op-001", conn_in_txn=True)
        db.patch(conn, "ops", "id", "op-001", {"status": "closed"})
    assert db.rows(conn, "ops", "id=?", ("op-001",))[0]["status"] == "closed"
    assert [e["kind"] for e in db.events(conn)] == ["op-close"]
    ok, _ = db.verify_chain(conn)
    assert ok is True


def test_append_event_in_txn_does_not_commit_early(project):
    # An event appended with conn_in_txn=True is not durable until the
    # surrounding transaction commits; a rollback erases it.
    conn = db.connect(project)
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            db.append_event(conn, session="s", actor="a", kind="x", conn_in_txn=True)
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


# ---- (b) two alternating connections still produce a verifiable chain

def test_alternating_connections_keep_one_verifiable_chain(project):
    a = db.connect(project)
    b = db.connect(project)
    for i in range(6):
        db.append_event(a if i % 2 == 0 else b, session="s", actor="x", kind="k%d" % i)
    ok, reason = db.verify_chain(db.connect(project))
    assert ok is True and reason == "6 events"


# ---- (c) onboarding is one transaction: a fault after project.yaml leaves no half-open state

def test_onboard_fault_after_yaml_leaves_no_half_open_state(project, monkeypatch):
    cli.main(["init"])
    n_before = db.connect(project).execute("SELECT COUNT(*) FROM events").fetchone()[0]
    # Fault injected at the last record write inside the onboard transaction,
    # after project.yaml has already been written to disk.
    real_meta_set = db.meta_set

    def boom(conn, key, value):
        if key == "onboarded":
            raise RuntimeError("fault after project.yaml write")
        return real_meta_set(conn, key, value)

    monkeypatch.setattr(db, "meta_set", boom)
    rc = cli.main(["onboard", "--name", "demo", "--who", "a:owner", "--what", "w"])
    assert rc == cli.INTERNAL

    # project.yaml was written before the fault, proving the fault came after it.
    assert os.path.exists(os.path.join(project, "project.yaml"))

    conn = db.connect(project)
    # The onboarded flag is unset and op-0 is absent: never half-open.
    assert db.meta_get(conn, "onboarded") is None
    assert not proj.is_onboarded(project)
    assert db.rows(conn, "ops", "id=?", ("op-0",)) == []
    # The record rolled back cleanly: no onboard events survived, and it verifies.
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == n_before
    kinds = [e["kind"] for e in db.events(conn)]
    assert "onboard" not in kinds and "op-open" not in kinds and "op-close" not in kinds
    ok, _ = db.verify_chain(conn)
    assert ok is True


def test_onboard_success_is_atomic_and_flag_set(project):
    cli.main(["init"])
    assert cli.main(["onboard", "--name", "demo", "--who", "a:owner", "--what", "w"]) == 0
    conn = db.connect(project)
    assert db.meta_get(conn, "onboarded") is not None
    assert db.rows(conn, "ops", "id=?", ("op-0",))[0]["status"] == "closed"
    kinds = [e["kind"] for e in db.events(conn)]
    assert "onboard" in kinds and "op-open" in kinds and "op-close" in kinds
    ok, _ = db.verify_chain(conn)
    assert ok is True
