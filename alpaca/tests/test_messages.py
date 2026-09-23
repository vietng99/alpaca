"""M2.4 proof: the messages table, its verbs, and the native channel rule.

Positive paths: a message carries ts, from, to, kind, body, pointer; the seven kinds and the
three `to` forms are accepted; messages are durable and replay in recorded order; a pointer to a
board row, a prior message, or a local evidence file resolves. Negative paths: an empty from or
to, an unknown kind, and a pointer that does not resolve are all refused before anything lands.
"""
import os
import pytest
from alpaca import cli, db, messages


def _conn(project):
    cli.main(["init"])
    return db.connect(project)


def _make_task(project):
    cli.main(["op", "new", "x"])
    cli.main(["task", "add", "--title", "task", "op-001", "s"])   # yields t-001


# ---- positive: shape and kinds --------------------------------------------------

def test_message_carries_the_six_fields(project):
    conn = _conn(project)
    row = messages.post(conn, "orch", "w1", "handoff", "take t-001")
    assert row["ts"]
    assert row["sender"] == "orch"
    assert row["to_"] == "w1"
    assert row["kind"] == "handoff"
    assert row["body"] == "take t-001"
    assert row["ref"] is None            # pointer optional; absent here


def test_seven_kinds_accepted(project):
    conn = _conn(project)
    assert set(messages.KINDS) == {"claim", "handoff", "question", "result",
                                   "blocker", "decision", "note"}
    for k in messages.KINDS:
        assert messages.post(conn, "a", "b", k, "body " + k)["kind"] == k


def test_unknown_kind_refused(project):
    conn = _conn(project)
    with pytest.raises(messages.MessageError):
        messages.post(conn, "a", "b", "gossip", "x")
    assert db.rows(conn, "messages") == []


def test_three_to_forms(project):
    conn = _conn(project)
    _make_task(project)
    messages.post(conn, "a", "agent-b", "note", "to an agent")
    messages.post(conn, "a", "t-001", "note", "to a task")
    messages.post(conn, "a", "broadcast", "note", "to everyone")
    assert [m["to_"] for m in messages.read(conn)] == ["agent-b", "t-001", "broadcast"]


def test_empty_from_or_to_refused(project):
    conn = _conn(project)
    with pytest.raises(messages.MessageError):
        messages.post(conn, "a", "", "note", "x")
    with pytest.raises(messages.MessageError):
        messages.post(conn, "", "b", "note", "x")
    assert db.rows(conn, "messages") == []


# ---- positive: durability and replay --------------------------------------------

def test_replay_in_recorded_order(project):
    conn = _conn(project)
    for i in range(5):
        messages.post(conn, "a", "b", "note", "m%d" % i)
    assert [m["body"] for m in messages.read(conn)] == ["m0", "m1", "m2", "m3", "m4"]


def test_read_to_narrows_and_includes_broadcast(project):
    conn = _conn(project)
    messages.post(conn, "a", "w1", "note", "for w1")
    messages.post(conn, "a", "w2", "note", "for w2")
    messages.post(conn, "a", "broadcast", "note", "for all")
    assert [m["body"] for m in messages.read(conn, to="w1")] == ["for w1", "for all"]


def test_read_since_id_watermark(project):
    conn = _conn(project)
    first = messages.post(conn, "a", "b", "note", "first")
    messages.post(conn, "a", "b", "note", "second")
    assert [m["body"] for m in messages.read(conn, since=first["id"])] == ["second"]


def test_durable_across_reconnect(project):
    conn = _conn(project)
    messages.post(conn, "a", "b", "note", "persisted")
    conn.close()
    conn2 = db.connect(project)
    assert [m["body"] for m in messages.read(conn2)] == ["persisted"]


def test_one_event_per_message(project):
    conn = _conn(project)
    before = len(db.events(conn, kind="msg"))
    messages.post(conn, "a", "b", "note", "x")
    assert len(db.events(conn, kind="msg")) == before + 1


# ---- positive: pointer resolves -------------------------------------------------

def test_pointer_to_board_row_resolves(project):
    conn = _conn(project)
    _make_task(project)
    assert messages.post(conn, "w1", "orch", "result", "done", pointer="t-001")["ref"] == "t-001"


def test_pointer_to_prior_message_resolves(project):
    conn = _conn(project)
    first = messages.post(conn, "a", "b", "note", "first")
    row = messages.post(conn, "a", "b", "result", "see prior", pointer=first["id"])
    assert row["ref"] == "msg:%d" % first["id"]


def test_pointer_to_local_file_resolves(project):
    conn = _conn(project)
    with open(os.path.join(project, "src", "evidence.txt"), "w", encoding="utf-8") as fh:
        fh.write("x")
    row = messages.post(conn, "a", "b", "result", "ev", pointer="local:src/evidence.txt")
    assert row["ref"] == "local:src/evidence.txt"


# ---- negative: pointer does not resolve -----------------------------------------

def test_unresolvable_pointer_refused(project):
    conn = _conn(project)
    for bad in ("t-999", "local:src/missing.txt", "msg:999"):
        with pytest.raises(messages.MessageError):
            messages.post(conn, "a", "b", "result", "x", pointer=bad)
    assert db.rows(conn, "messages") == []


# ---- the CLI verb, extended additively ------------------------------------------

def test_cli_msg_post_and_read(project, capsys):
    _conn(project)
    assert cli.main(["msg", "post", "--from", "orch", "--to", "w1",
                     "--kind", "handoff", "take t-001"]) == 0
    cli.main(["msg", "read", "--to", "w1"])
    assert "take t-001" in capsys.readouterr().out


def test_cli_msg_unknown_kind_refused(project):
    _conn(project)
    assert cli.main(["msg", "post", "--from", "a", "--to", "b",
                     "--kind", "gossip", "x"]) == 1


def test_cli_msg_unresolvable_pointer_refused(project):
    _conn(project)
    assert cli.main(["msg", "post", "--from", "a", "--to", "b", "--kind", "result",
                     "x", "--ref", "t-999"]) == 1
