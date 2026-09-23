"""Task contracts: four restated parts per task, appended as events, newest current."""
import json

import pytest

from alpaca import db, profile, taskcontract, work_record


class Stages(profile.Profile):
    """A domain profile that only declares stages."""

    def stages(self):
        return ("cva6_gds", "lint")


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(profile, "load", lambda root: Stages())
    root = tmp_path / "project"
    root.mkdir()
    conn = db.connect(str(root))
    db.append_event(conn, session="s", actor="human", kind="op-new", ref="op-1", data={"intent": "x"})
    db.upsert(conn, "ops", "id", {"id": "op-1", "intent": "x", "status": "open"})
    db.upsert(conn, "tasks", "id", {"id": "t-001", "op": "op-1", "statement": "cva6 first GDS", "title": "B.0",
                                     "status": "open"})
    yield conn
    conn.close()


def test_record_keeps_the_newest_and_lists_it_on_the_task(conn):
    taskcontract.record(conn, "t-001", {"done_bar": ["route DRC 0"], "stage": "cva6_gds"}, session="s", actor="a")
    taskcontract.record(conn, "t-001", {"input": ["CORE_UTILIZATION 40"], "done_bar": ["route DRC 0", "KLayout DRC 0"],
                                        "fail_cases": "at most 3 attempts", "stage": "cva6_gds"}, session="s", actor="b")
    current = taskcontract.latest(conn)["t-001"]
    assert current["done_bar"] == ["route DRC 0", "KLayout DRC 0"] and current["fail_cases"] == ["at most 3 attempts"]
    assert current["by"] == "b" and current["stage"] == "cva6_gds"
    task = next(t for t in work_record.tasks(conn) if t["id"] == "t-001")
    assert task["contract"]["input"] == ["CORE_UTILIZATION 40"]
    kinds = [e["kind"] for e in db.events(conn, kind=taskcontract.KIND)]
    assert kinds.count(taskcontract.KIND) == 2


@pytest.mark.parametrize("bad, message", [
    ({"input": ["x"]}, "done bar"),
    ({"done_bar": ["ok"], "stage": "not-a-stage"}, "profile stages: cva6_gds, lint"),
    ({"done_bar": [""]}, "1 to 600"),
    ({"done_bar": ["x" * 601]}, "1 to 600"),
    ({"done_bar": ["x"] * 21}, "at most 20"),
])
def test_bad_contracts_are_refused(conn, bad, message):
    with pytest.raises(ValueError, match=message):
        taskcontract.record(conn, "t-001", bad, session="s", actor="a")
    assert taskcontract.latest(conn) == {}


def test_unknown_task_is_refused(conn):
    with pytest.raises(ValueError, match="no task"):
        taskcontract.record(conn, "t-999", {"done_bar": ["x"]}, session="s", actor="a")


def test_a_task_without_contract_lists_none(conn):
    assert next(t for t in work_record.tasks(conn) if t["id"] == "t-001")["contract"] is None


def test_without_a_profile_a_stage_is_refused_and_none_is_fine(conn, monkeypatch):
    monkeypatch.setattr(profile, "load", lambda root: profile.EMPTY)
    with pytest.raises(ValueError, match="no profile declares stages"):
        taskcontract.record(conn, "t-001", {"done_bar": ["x"], "stage": "cva6_gds"}, session="s", actor="a")
    taskcontract.record(conn, "t-001", {"done_bar": ["x"]}, session="s", actor="a")
    assert taskcontract.latest(conn)["t-001"]["stage"] is None
