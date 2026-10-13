"""A05 (cause C1, C3 cost): one rule, five readers. The op close gate, `aar owed`, `aar owed <op>`,
the hub's AAR state (aarview.project) and `aar waive` must agree on which triggers hold each op.

Ways this still fails after it lands:
  * the gate blocks on a loose trigger while `aar owed <op>` says "no AAR owed" and the hub shows
    the op as not owed (a silent page)
  * plain `aar owed` lists the loose trigger but not the ops it blocks, or names an op the gate lets
    close
  * after `aar owe <op> --event`, the hub or `aar owed <other>` still shows the trigger on the
    other op
  * the new per-op "open when recorded" read is done once per op, so `aar owed` and the hub grow a
    query per op on a record with many open ops (main has 44)"""
import pytest

from alpaca import aarview, cli, db
from alpaca.tests import aarowedkit as k

from alpaca.tests import aarheldkit as kit


@pytest.fixture(autouse=True)
def flow_profile(monkeypatch):
    kit.flow_profile(monkeypatch)


def _hub(project, conn):
    ops = [dict(r) for r in conn.execute("SELECT * FROM ops ORDER BY id")]
    states, _marks = aarview.project(conn, project, ops)
    return states


def _hub_ids(states, op):
    s = states.get(op) or {}
    assert "error" not in s, s
    return {t["id"] for t in s.get("triggers") or []}


def _setup(project, capsys):
    conn = k.two_ops(project)
    code, out = k.run(capsys, "aar", "owe", k.OP, "--reason", kit.REASON)
    assert code == cli.PASS, out
    owe = k.last_id(conn, "aar-owe")
    loose = kit.fail(project, "cli")
    k.finish(project, conn, "t-001")
    k.finish(project, conn, "t-002")
    return conn, owe, loose


def test_every_reader_names_the_same_triggers_for_each_op(project, capsys):
    conn, owe, loose = _setup(project, capsys)
    line1 = k.assert_blocked(conn, *k.close(capsys, k.OP), k.OP, events=(owe, loose))
    line2 = k.assert_blocked(conn, *k.close(capsys, k.OTHER), k.OTHER, events=(loose,))
    assert not kit.has(line2, owe), line2
    one, two = k.owed(capsys, k.OP), k.owed(capsys, k.OTHER)
    assert kit.has(one, owe) and kit.has(one, loose), one
    assert kit.has(two, loose) and not kit.has(two, owe), two
    assert "no aar owed" not in two.lower(), two
    assert kit.blocks_named(capsys, loose) >= {k.OP, k.OTHER}, k.owed(capsys)
    states = _hub(project, conn)
    assert _hub_ids(states, k.OP) >= {owe, loose}, states.get(k.OP)
    assert _hub_ids(states, k.OTHER) >= {loose}, states.get(k.OTHER)
    assert owe not in _hub_ids(states, k.OTHER)
    for op in (k.OP, k.OTHER):
        assert states[op].get("blocks_close") is True, states[op]


def test_after_attribution_every_reader_drops_it_from_the_other_op(project, capsys):
    conn, owe, loose = _setup(project, capsys)
    code, out = kit.owe_event(capsys, k.OP, loose)
    assert code == cli.PASS, out
    two = k.owed(capsys, k.OTHER)
    assert not kit.has(two, loose), two
    assert not kit.is_loose(capsys, loose)
    states = _hub(project, conn)
    assert loose not in _hub_ids(states, k.OTHER), states.get(k.OTHER)
    code, out = k.close(capsys, k.OTHER)
    k.assert_closed(conn, code, out, k.OTHER)
    k.assert_blocked(conn, *k.close(capsys, k.OP), k.OP)
    assert k.OP in _hub(project, conn)


def _selects(monkeypatch, fn):
    seen = []
    real = db.connect

    def counting(*a, **kw):
        c = real(*a, **kw)
        c.set_trace_callback(lambda s: seen.append(s) if "SELECT" in s.upper() else None)
        return c
    monkeypatch.setattr(db, "connect", counting)
    try:
        fn()
    finally:
        monkeypatch.setattr(db, "connect", real)
    return len(seen)


def test_the_hub_fold_does_not_query_once_per_blocked_op(project, capsys, monkeypatch):
    """Same 42 open ops, with and without a loose trigger that blocks all of them: the hub's reads
    may not grow with the number of blocked ops (aar.answered per op is there in both runs)."""
    k.two_ops(project)
    for n in range(40):
        assert cli.main(["op", "new", "filler op %d" % n, "--done-when", "nothing"]) == 0

    def hub():
        c = db.connect(project)
        try:
            _hub(project, c)
        finally:
            c.close()

    quiet = _selects(monkeypatch, hub)
    kit.fail(project, "cli")
    loud = _selects(monkeypatch, hub)
    assert loud <= quiet + 10, (quiet, loud)


def test_owed_alone_stays_flat_with_many_blocked_ops(project, capsys, monkeypatch):
    k.two_ops(project)
    kit.fail(project, "cli")
    few = _selects(monkeypatch, lambda: k.owed(capsys))
    for n in range(40):
        assert cli.main(["op", "new", "filler op %d" % n, "--done-when", "nothing"]) == 0
    kit.fail(project, "cli")
    many = _selects(monkeypatch, lambda: k.owed(capsys))
    assert many <= few + 10, (few, many)
