"""(class: parked work counted as open): a deferred task is not open work. The research run card
keeps base's done and "N of M tasks done" count but leads with the live task; the mission page
does not list a parked, unlinked task under "Needs component mapping"."""
import importlib

from alpaca import db, taskplan
from alpaca.tests.hub_not_now_kit import node
from alpaca.tests.not_now_kit import raw_defer
from alpaca.tests.test_not_now_mission import _definition


def _node(tid, state, label):
    return {"id": tid, "state": state, "label": label, "depth": 0, "after": [], "reasons": []}


def _run_card(tmp_path, tasks, nodes):
    grp = lambda op, ns, cat: {"id": op + "-g", "title": "rr-7 " + op, "category": cat, "nodes": ns}
    data = {"tasks": tasks + [{"id": "t-9", "op": "op-x", "title": "c", "status": "open"}],
            "ops": [{"id": "op-r", "title": "R"}, {"id": "op-x", "title": "X"}],
            "roadmap": {"operations": [
                {"op": "op-r", "revision": 1, "edges": [], "groups": [grp("op-r", nodes, "research")]},
                {"op": "op-x", "revision": 1, "edges": [], "groups": [grp("op-x", [_node("t-9", "ready", "Ready")], "core")]}]}}
    html = node("const {renderRoadmap}=await import('./roadmap.js');out(renderRoadmap(DATA,{all:true,showAll:true}));", data, tmp_path)
    i = html.find('data-rm-node="run:op-r"')
    assert i >= 0, "no run card for op-r"
    return html[i:i + 1500]


def test_run_with_only_parked_work_left_is_not_complete(tmp_path):
    card = _run_card(tmp_path,
                     [{"id": "t-1", "op": "op-r", "title": "a", "status": "done"},
                      {"id": "t-2", "op": "op-r", "title": "b", "status": "deferred", "deferral": None}],
                     [_node("t-1", "done", "Done"), _node("t-2", "deferred", "Not now")])
    state = card[card.find('class="rm-state"'):][:200]
    assert "Complete" not in state and "rm-done" not in card[:card.find('class="rm-state"')], state
    assert "Not now" in state
    assert "1 of 2 tasks done" in card


def test_run_count_does_not_credit_a_parked_task(tmp_path):
    card = _run_card(tmp_path,
                     [{"id": "t-5", "op": "op-r", "title": "a", "status": "deferred", "deferral": None},
                      {"id": "t-6", "op": "op-r", "title": "b", "status": "open"}],
                     [_node("t-5", "deferred", "Not now"), _node("t-6", "waiting", "Waiting")])
    assert "0 of 2 tasks done" in card
    assert "now t-6" in card and "now t-5" not in card


def test_parked_unlinked_task_is_not_called_open(tmp_path):
    mission = importlib.import_module("alpaca.mission")
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    for name in ("parser", "usage"):
        (root / "src" / (name + ".txt")).write_text(name)
    mission.define(root, _definition(), session="owner", expected_revision=0)
    conn = db.connect(root)
    db.upsert(conn, "ops", "id", {"id": "op-dev", "intent": "Develop", "status": "open"})
    for ident in ("t-a", "t-b"):
        db.upsert(conn, "tasks", "id", {"id": ident, "op": "op-dev", "statement": ident, "title": ident, "status": "open"})
    plan = {"version": 1, "revision": 0, "groups": [{"id": "g", "title": "Work"}],
            "tasks": [{"id": i, "group": "g", "after": [], "resources": [], "requires_stages": [], "hold": None}
                      for i in ("t-a", "t-b")]}
    taskplan.record(conn, "op-dev", plan, session="owner", actor="owner")
    mission.link(root, "parser", "t-a", "work", session="owner")
    raw_defer(conn, "t-b")
    html = node("const {renderMission}=await import('./mission.js');out(renderMission(DATA));", mission.project(root), tmp_path)
    assert "Needs component mapping" not in html
