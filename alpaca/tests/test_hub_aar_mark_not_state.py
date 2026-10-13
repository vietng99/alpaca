"""(cause C3, history 1) Overdue is a mark beside the state, never a state: no count, label, focus or
open/done split may change because an item is late. t-618 round 1 failed twice in this class
(filtering `deferred` out of `open` flipped a run card to Complete). Ways this fails after it lands:

  * overdue becomes node.state / task status / a NOT_NOW value, so terminal(), the picker counts,
    rm-shell-count, rm-progress-line, rm-group-count or the cockpit focus change
  * the mark sits only on cards, so a user-collapsed aar-<op> group hides every overdue item
The same record is read twice: as built, and with aaritems.overdue returning nothing.
"""
import pytest

from alpaca.tests.hub_aar_kit import cards, marked, node, op_view, overdue_record, page, render, standing, tree
from alpaca import aaritems

COUNTS = ("rm-shell-count", "rm-progress-line", "rm-group-count", "rm-group-current", "rm-run-count")


@pytest.fixture
def pair(project, monkeypatch):
    _conn, tid = overdue_record(project, monkeypatch)
    marked_page = page(project)
    with monkeypatch.context() as m:
        m.setattr(aaritems, "overdue", lambda conn, today=None: [])
        plain_page = page(project)
    return marked_page, plain_page, tid, standing(project)


def _texts(html, cls):
    return [e.text() for e in tree(html).walk() if cls in e.classes]


def _picker_rows(html):
    return [e.text() for e in tree(html).walk() if "rm-choice" in e.classes]


def test_the_control_page_has_no_marks(pair, tmp_path):
    _marked, plain, _tid, st = pair
    got = cards(op_view(plain, st, tmp_path))
    assert not [t for t, c in got.items() if marked(c)], \
        "with aaritems.overdue empty the hub still marks cards: it does not read the one source"


def test_states_statuses_and_labels_do_not_move(pair):
    marked_page, plain, _tid, _st = pair
    def states(o):
        return {n["id"]: (n.get("state"), n.get("label")) for op in o["roadmap"]["operations"]
                for g in op["groups"] for n in g["nodes"]}
    assert states(marked_page) == states(plain)
    assert {t["id"]: t["status"] for t in marked_page["tasks"]} == {t["id"]: t["status"] for t in plain["tasks"]}


@pytest.mark.parametrize("view", ("standing", "workspace"))
def test_counts_and_picker_rows_do_not_move(pair, tmp_path, view):
    marked_page, plain, _tid, st = pair
    call = "renderRoadmap(DATA,{op:%r})" % st if view == "standing" else "renderRoadmap(DATA)"
    a = render(marked_page, call, tmp_path)
    b = render(plain, call, tmp_path)
    for cls in COUNTS:
        assert _texts(a, cls) == _texts(b, cls), "%s text changed because an item is overdue" % cls
    assert _picker_rows(a) == _picker_rows(b), "the operation picker counts changed because an item is overdue"


def test_cockpit_focus_does_not_move(pair, tmp_path):
    marked_page, plain, _tid, _st = pair
    body = "const c=await import('./cockpit.js');out(c.cockpitTaskIds(DATA));"
    assert node(body, marked_page, tmp_path) == node(body, plain, tmp_path)


def test_a_collapsed_group_still_says_it_holds_overdue_items(pair, tmp_path):
    marked_page, _plain, _tid, st = pair
    html = op_view(marked_page, st, tmp_path)
    groups = [e for e in tree(html).walk() if e.tag == "details" and e.attrs.get("data-rm-group") == "aar-op-001"]
    assert len(groups) == 1, "no aar-op-001 group on the standing op view"
    summary = next(c for c in groups[0].children if getattr(c, "tag", "") == "summary")
    assert "overdue" in summary.text().lower(), \
        "the group summary does not say it holds overdue items; folding the group hides every mark: " + summary.text()
