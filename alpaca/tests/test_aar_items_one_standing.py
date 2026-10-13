"""t-633 (done_bar[1], signed brief D5): there is one standing After-action items op. No code path
opens a second one: when the standing op cannot take more tasks the seal refuses and names --into,
and the standing op's own report files its items into itself.

Ways this still fails after it lands:
  * a full standing op makes the seal open another standing op beside it
  * the seal of the standing op's own report opens a new standing op
  * a refused seal leaves a seal row, a seal sidecar, an op or a task behind
  * a report with more items than one plan holds opens a standing op and then fails
  * open_standing is called from a place that does not first check for an open standing op"""
import inspect
import os
import re

import pytest

from alpaca import aar, aaritems, cli, db, taskplan
from alpaca.tests.aar_items_kit import (OP, OTHER, author, filed, ok, op_ids, reviewed, row, seal, setup,
                                        snapshot, standing_ops)


def _with_standing(project, monkeypatch):
    conn = setup(project, monkeypatch)
    before = op_ids(conn)
    author(conn, project, OP, [row("Alpha item")])
    code, out = seal(OP)
    assert code == cli.PASS, out
    return conn, before, standing_ops(conn, before)[0]


def _fill(conn, standing):
    for i in range(taskplan.MAX_GROUPS - len(taskplan.latest(conn, standing)["groups"])):
        taskplan.group(conn, standing, "filler-%02d" % i, "filler group", "build",
                       session="hand", actor="hand")


def test_a_full_standing_op_refuses_and_names_into(project, monkeypatch):
    conn, before, standing = _with_standing(project, monkeypatch)
    _fill(conn, standing)
    author(conn, project, OTHER, [row("Bravo item")])
    reviewed(OTHER)
    base = snapshot(conn)
    code, out = seal(OTHER)
    assert code == cli.FAIL and "--into" in out, out
    assert snapshot(conn) == base and aar.latest_seal(conn, OTHER) is None
    assert standing_ops(conn, before) == [standing]
    assert not os.path.exists(aar.report_path(project, OTHER) + ".seal.json")


def test_into_the_full_standing_op_does_not_offer_leaving_into_off(project, monkeypatch):
    conn, before, standing = _with_standing(project, monkeypatch)
    _fill(conn, standing)
    author(conn, project, OTHER, [row("Bravo item")])
    reviewed(OTHER)
    code, out = seal(OTHER, "--into", standing)
    assert code == cli.FAIL and "plan is full" in out, out
    assert "leave --into off" not in out, out
    code, out = seal(OTHER)
    assert code == cli.FAIL and "has no room" in out, out


def test_into_another_full_op_offers_leaving_into_off_when_the_standing_op_has_room(
        project, monkeypatch):
    conn, before, standing = _with_standing(project, monkeypatch)
    ok("op", "new", "a third op", "--done-when", "nothing to see")
    third = standing_ops(conn, set(before) | {standing})[0]
    _fill(conn, third)
    author(conn, project, OTHER, [row("Bravo item")])
    reviewed(OTHER)
    code, out = seal(OTHER, "--into", third)
    assert code == cli.FAIL and "plan is full" in out and "leave --into off" in out, out
    code, out = seal(OTHER)
    assert code == cli.PASS, out
    assert [o for _t, o in filed(conn, OTHER)] == [standing], filed(conn, OTHER)


def test_the_standing_ops_own_report_files_into_itself(project, monkeypatch):
    conn, before, standing = _with_standing(project, monkeypatch)
    author(conn, project, standing, [row("Standing item")])
    code, out = seal(standing)
    assert code == cli.PASS, out
    assert [o for _t, o in filed(conn, standing)] == [standing], filed(conn, standing)
    assert standing_ops(conn, before) == [standing]
    assert "opened" not in out.lower(), out


def test_the_standing_ops_own_report_when_full_refuses(project, monkeypatch):
    conn, before, standing = _with_standing(project, monkeypatch)
    _fill(conn, standing)
    author(conn, project, standing, [row("Standing item")])
    reviewed(standing)
    base = snapshot(conn)
    code, out = seal(standing)
    assert code == cli.FAIL and "--into" in out, out
    assert snapshot(conn) == base and aar.latest_seal(conn, standing) is None
    assert standing_ops(conn, before) == [standing]


def test_too_many_items_for_a_first_standing_op_open_nothing(project, monkeypatch):
    conn = setup(project, monkeypatch)
    monkeypatch.setattr(taskplan, "MAX_TASKS", 1)
    before = op_ids(conn)
    author(conn, project, OP, [row("Alpha item"), row("Beta item")])
    reviewed(OP)
    base = snapshot(conn)
    code, out = seal(OP)
    assert code == cli.FAIL and "--into" in out, out
    assert snapshot(conn) == base and aar.latest_seal(conn, OP) is None
    assert op_ids(conn) == before


def test_open_standing_refuses_while_one_is_open(project, monkeypatch):
    conn, before, standing = _with_standing(project, monkeypatch)
    with pytest.raises(ValueError):
        with db.transaction(conn):
            aaritems.open_standing(conn, "cli")
    assert standing_ops(conn, before) == [standing]


def test_open_standing_has_one_caller():
    """Only _target opens the After-action items op, and only when none is open."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(aaritems.__file__)))
    users = []
    for dirpath, _dirs, names in os.walk(os.path.join(root, "alpaca")):
        if os.path.basename(dirpath) == "tests":
            continue
        for name in names:
            if not name.endswith(".py"):
                continue
            with open(os.path.join(dirpath, name), encoding="utf-8") as fh:
                for line in fh:
                    if (re.search(r"(?<![\w.])open_standing\(", line) or "aaritems.open_standing(" in line) \
                            and not line.lstrip().startswith("def "):
                        users.append((name, line.strip()))
    assert users == [("aaritems.py", "target = open_standing(conn, session)")], users
    target = inspect.getsource(aaritems._target)
    assert re.search(r"if target is None:\n(?:.*\n)*?\s+target = open_standing\(", target), target
