"""(class 5, class 3, done_bar[1]): the op view's Not now panel. The roadmap renderer has several
hosts (Work & evidence ?op= in checklist and roadmap layouts, the finished op view, the pinned
operation map, the workspace roadmap, the mission page) and two filters (visibleIds from the
status/search filter, the plan fallback). A panel built from plan nodes loses tasks when the plan
is missing or filtered; a panel built from the workspace lists other ops' tasks; a panel put
unconditionally in renderOne shows twice on the Roadmap page; a panel without a data-key (or a
<details> without fold memory) churns on every 15 s live patch; long triggers overflow at 390 px."""
import re

import pytest

from alpaca.tests.hub_not_now_kit import (MET, UNREADABLE, UNRECORDED, WEB, all_rules, deferred, entry, node, one_panel, overview, panels,
                  plan_node, task, words)

RENDER = "const {renderRoadmap,renderPhaseMap}=await import('./roadmap.js');"
IDS = ("t-1", "t-2", "t-3", "t-4", "t-5")
PARKED = ("t-2", "t-3", "t-4", "t-5")  # entries are told apart by these; t-1 appears inside a trigger


def fixture(closed=False, plan=True):
    t2 = deferred("t-2", reason="vendor API not out", when="after:t-1", met=True, met_by="t-1 is done")
    t3 = deferred("t-3", reason=None, when=None)
    t4 = deferred("t-4", reason=None, when=None, error="ValueError")
    t5 = deferred("t-5", op="op-2", reason="OTHER OP REASON", when="date:2026-12-01")
    tasks = [task("t-1"), t2, t3, t4, t5]
    for t in tasks:
        t["phase"] = "build"
    node_ = lambda t: plan_node(t["id"], "deferred", reasons=words(t["deferral"]))
    o = overview(tasks, {"op-1": [plan_node("t-1", "ready"), node_(t2), node_(t3), node_(t4)], "op-2": [node_(t5)]})
    o["ops"][0]["phases"] = ["build"]
    if closed:
        o["ops"][0]["status"] = "closed"
        for t in (t2, t3, t4):
            t["deferral"]["op_closed"] = True
    if not plan:
        del o["roadmap"]
    return o


def render(tmp_path, o, call):
    return node(RENDER + "out(%s);" % call, o, tmp_path)


def op_view(tmp_path, o=None, opts="{op:'op-1'}"):
    return render(tmp_path, o or fixture(), "renderRoadmap(DATA,%s)" % opts)


def test_op_view_has_one_panel_with_exactly_this_ops_parked_tasks(tmp_path):
    p = one_panel(op_view(tmp_path))
    text = p.text()
    for tid in ("t-2", "t-3", "t-4"):
        assert re.search(r"(?<![\w-])%s(?![\w-])" % tid, text), "panel misses %s" % tid
    assert not re.search(r"(?<![\w-])t-5(?![\w-])", text) and "OTHER OP REASON" not in text, "panel lists op-2's task"
    linked = {e.attrs["data-task"] for e in p.walk() if e.attrs.get("data-task")}
    assert not linked or linked == {"t-2", "t-3", "t-4"}, "panel links %s (only the parked tasks of op-1 belong)" % linked


def test_entry_shows_reason_trigger_and_met(tmp_path):
    e = entry(one_panel(op_view(tmp_path)), "t-2", PARKED).text()
    assert "vendor API not out" in e and "after:t-1" in e, e
    assert MET in e.lower() and "t-1 is done" in e, "a met trigger is not marked with what met it: " + e


def test_entry_without_recorded_reason_says_so(tmp_path):
    e = entry(one_panel(op_view(tmp_path)), "t-3", PARKED).text()
    assert "null" not in e and "undefined" not in e and UNRECORDED in e.lower(), e
    assert MET not in e.lower(), e


def test_entry_with_unreadable_deferral_says_so(tmp_path):
    e = entry(one_panel(op_view(tmp_path)), "t-4", PARKED).text()
    assert UNREADABLE in e.lower(), e


def test_status_filter_hides_cards_but_not_the_panel(tmp_path):
    """Work & evidence passes the status/search filter as visibleIds (hub.js:357)."""
    p = one_panel(op_view(tmp_path, opts="{op:'op-1',visibleIds:['t-1']}"))
    assert "vendor API not out" in p.text()


@pytest.mark.parametrize("mode", ("list", "map"))
def test_work_and_evidence_layouts_show_it(tmp_path, mode):
    html = op_view(tmp_path, opts="{op:'op-1',all:false,routeScope:true,mode:'%s',visibleIds:%s}" % (mode, list(IDS)))
    assert "vendor API not out" in one_panel(html).text()


def test_op_without_a_recorded_plan_still_lists_its_parked_work(tmp_path):
    p = one_panel(op_view(tmp_path, fixture(plan=False)))
    assert "vendor API not out" in p.text() and "OTHER OP REASON" not in p.text()


def test_finished_op_view_lists_parked_work(tmp_path):
    """hub.js:303: a task deferred until another op closes can sit in a CLOSED op."""
    html = op_view(tmp_path, fixture(closed=True), "{op:'op-1',selector:false,showAll:true}")
    assert "vendor API not out" in one_panel(html).text()


def test_pinned_operation_map_has_one_panel(tmp_path):
    html = render(tmp_path, fixture(), "renderPhaseMap(DATA,'op-1')")
    p = one_panel(html)
    assert "vendor API not out" in p.text() and "OTHER OP REASON" not in p.text()


def test_roadmap_page_lists_each_parked_task_once(tmp_path):
    """The Roadmap page is the pinned maps plus the workspace roadmap (hub.js:314-316)."""
    o = fixture()
    o["ops"][0]["pinned"] = True
    html = render(tmp_path, o, "renderPhaseMap(DATA,'op-1')+renderRoadmap(DATA)")
    found = {}
    for p in panels(html):
        for tid in ("t-2", "t-3", "t-4", "t-5"):
            if re.search(r"(?<![\w-])%s(?![\w-])" % tid, p.text()):
                found[tid] = found.get(tid, 0) + 1
    assert found.get("t-2") == 1, "t-2 is listed in %s Not now panels on one page" % found.get("t-2", 0)


def test_no_empty_panel_when_nothing_is_parked(tmp_path):
    o = overview([task("t-1"), task("t-9", status="done")], {"op-1": [plan_node("t-1", "ready"), plan_node("t-9", "done")]})
    assert not panels(op_view(tmp_path, o)), "an empty Not now panel on an op with no parked work"


def test_panel_is_keyed_open_and_outside_the_canvas(tmp_path):
    p = one_panel(op_view(tmp_path))
    assert p.attrs.get("data-key"), "the panel has no data-key: morph.js patches it by position on every refresh"
    assert not any("rm-canvas" in a.classes for a in p.ancestors()), "panel inside .rm-canvas moves the arrows"
    assert p.tag != "details" or "open" in p.attrs, "a closed <details> snaps shut on every live patch"


def test_free_text_is_escaped(tmp_path):
    o = fixture()
    o["tasks"][1]["deferral"]["reason"] = '<img src=x onerror="window.x=1">'
    o["tasks"][1]["deferral"]["reactivate_when"] = '"><b>x</b>'
    html = op_view(tmp_path, o)
    assert "<img" not in html and "<b>x</b>" not in html


def test_long_triggers_wrap_at_phone_width(tmp_path):
    p = one_panel(op_view(tmp_path))
    names = {c for e in [p, *p.walk()] for c in e.classes}
    wraps = [sels for _, _, sels, decls in all_rules()
             if any(("." + n) in s for s in sels for n in names)
             and (decls.get("overflow-wrap") in ("anywhere", "break-word") or decls.get("word-break") in ("break-word", "break-all"))]
    assert wraps, "no overflow-wrap rule on the panel's classes %s: a long trigger scrolls the page at 390 px" % sorted(names)
