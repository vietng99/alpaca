"""(trace section 2; cause C8 siblings) The op view has several hosts: Work & evidence ?op= in the
checklist and roadmap layouts (renderRoadmap list/map, with the status filter as visibleIds), the
finished op view (hub.js:303, planned and unplanned branches), the pinned operation map
(renderPhaseMap) and the cards/timeline/table layouts that host notNowPanel in hub.js:357. Closed
ops are exactly the ones that owe an AAR, so the finished op view is the main reader. Ways this fails
after it lands:

  * the AAR block is added in renderOne only, so the unplanned finished op view and the
    cards/timeline/table layouts never show it (they host notNowPanel from hub.js)
  * the block is built from plan nodes or filtered by visibleIds, so a status filter or a missing
    plan hides it
  * the block sits inside .rm-canvas (moves the arrows) or has no data-key of its own (morph.js
    patches it by position on every 15 s refresh)
"""
import re

import pytest

from alpaca import cli

from alpaca.tests.hub_aar_kit import OP, OWED, block_of, hub_js, op_view, owe, page, render, setup, text, tree


@pytest.fixture
def owed_page(project, monkeypatch):
    conn = setup(project, monkeypatch)
    owe(conn)
    return page(project)


@pytest.mark.parametrize("opts", [
    "{op:'op-001'}",
    "{op:'op-001',all:false,routeScope:true,mode:'list',visibleIds:[]}",
    "{op:'op-001',all:false,routeScope:true,mode:'map',visibleIds:['t-404']}",
    "{op:'op-001',selector:false,showAll:true}",
])
def test_renderroadmap_hosts_show_it_whatever_the_filter(owed_page, tmp_path, opts):
    assert OWED.search(text(op_view(owed_page, OP, tmp_path, opts))), "no AAR owed in renderRoadmap(%s)" % opts


def test_without_a_plan_it_still_shows(owed_page, tmp_path):
    o = dict(owed_page)
    o.pop("roadmap", None)
    assert OWED.search(text(op_view(o, OP, tmp_path)))


@pytest.mark.parametrize("opts", [
    "{op:'op-003'}",
    "{op:'op-003',all:false,routeScope:true,mode:'list',visibleIds:[]}",
    "{op:'op-003',selector:false,showAll:true}",
])
def test_an_open_op_with_no_task_still_shows_it(project, monkeypatch, tmp_path, opts):
    conn = setup(project, monkeypatch)
    assert cli.main(["op", "new", "a fresh op with no tasks yet", "--done-when", "it has a report"]) == 0
    owe(conn, "op-003")
    html = op_view(page(project), "op-003", tmp_path, opts)
    assert OWED.search(text(html)), "no AAR owed for an op with no task in renderRoadmap(%s)" % opts
    assert "No tasks have been recorded yet" in text(html)


def test_pinned_operation_map_shows_it(owed_page, tmp_path):
    assert OWED.search(text(render(owed_page, "renderPhaseMap(DATA,'op-001')", tmp_path)))


def test_every_hub_js_host_of_the_not_now_panel_hosts_the_aar_block():
    """hub.js lines that call notNowPanel( are the op-view hosts outside renderOne (finished op view,
    Work & evidence cards/timeline/table). Each must also host the AAR block."""
    lines = [l for l in hub_js().splitlines() if "notNowPanel(" in l and not l.lstrip().startswith(("import", "//"))]
    assert lines, "hub.js no longer calls notNowPanel: re-read the hosts"
    bare = [l.strip()[:160] for l in lines if not re.search(r"aar", l.replace("notNowPanel", ""), re.I)]
    assert not bare, "these hub.js hosts show Not now but no AAR state:\n" + "\n".join(bare)


def test_the_block_is_keyed_and_outside_the_canvas(owed_page, tmp_path):
    html = op_view(owed_page, OP, tmp_path)
    holder, label = block_of(html, OWED)
    assert not any("rm-canvas" in a.classes for a in label.ancestors()), "the AAR block is inside .rm-canvas"
    assert holder is not None and re.search(r"aar", holder.attrs.get("data-key", ""), re.I), \
        "the AAR block has no data-key of its own (nearest key: %r); morph.js patches it by position" % (
            holder.attrs.get("data-key") if holder is not None else None)
    assert holder.tag != "details" or "open" in holder.attrs, "a closed <details> snaps shut on every live patch"


def test_one_block_per_op_view(owed_page, tmp_path):
    html = op_view(owed_page, OP, tmp_path)
    keys = {e.attrs["data-key"] for e in tree(html).walk()
            if re.search(r"aar", e.attrs.get("data-key", ""), re.I) and OWED.search(e.text())
            and not any(re.search(r"aar", a.attrs.get("data-key", "") if hasattr(a, "attrs") else "", re.I)
                        and OWED.search(a.text()) for a in e.ancestors() if hasattr(a, "attrs"))}
    assert len(keys) == 1, "the op view shows %d AAR blocks: %s" % (len(keys), sorted(keys))
