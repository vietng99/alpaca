"""(done_bar[1]; cause C1, C3, C8) Overdue action items are marked on the roadmap: exactly the task
ids aaritems.overdue lists, judged on the record clock. Ways this fails after it lands:

  * the JS compares the due text to Date.now() (wall clock, viewer time zone) or the hub re-parses
    the contract; then an item flips at the viewer's midnight, and Bravo (due 2026-10-20) is marked
    when the wall clock is later than the record clock
  * a done item stays marked, or a deferred (Not now) late item loses one of its two marks
  * the mark is keyed by op or group, so it shows on the wrong cards; or only one host (the
    workspace roadmap or the standing op's view) shows it
  * days late or the due day is not on the card, so "overdue" says nothing about how late
"""
import re

import pytest

from alpaca.tests.hub_aar_kit import UNREADABLE_DUE, cards, marked, op_view, overdue_record, overdue_source, page, render, standing
from alpaca.tests import aar_items_kit as items

LATER = "2027-06-01T10:00:00+00:00"
# node: any Date the page builds reads 2030-01-01, so a wall-clock comparison marks every item
CLOCK_TRAP = ("{const R=Date,T=R.UTC(2030,0,1);globalThis.Date=class extends R{constructor(...a){super(...(a.length?a:[T]))}"
              "static now(){return T}};}")


def _views(project, tmp_path, prelude=""):
    o = page(project)
    st = standing(project)
    return {"workspace": cards(render(o, "renderRoadmap(DATA)", tmp_path, prelude)),
            "standing op": cards(op_view(o, st, tmp_path, prelude=prelude))}


@pytest.fixture
def rec(project, monkeypatch):
    conn, tid = overdue_record(project, monkeypatch)
    items.freeze(monkeypatch, LATER)            # the wall clock moves on; the record clock does not
    return conn, tid


def test_marked_cards_are_exactly_the_overdue_ids(project, rec, tmp_path):
    _conn, tid = rec
    late = {r["id"] for r in overdue_source(project)}
    assert late == {tid["Alpha late by date"], tid["Kilo late and parked"]}
    for name, got in _views(project, tmp_path, CLOCK_TRAP).items():
        for t in late:
            assert t in got, "%s: no card for overdue item %s" % (name, t)
        marks = {t for t, card in got.items() if marked(card)}
        assert marks == late, "%s marks %s overdue; aaritems.overdue lists %s" % (name, sorted(marks), sorted(late))


def test_the_card_says_how_late(project, rec, tmp_path):
    _conn, tid = rec
    row = next(r for r in overdue_source(project) if r["id"] == tid["Alpha late by date"])
    card = _views(project, tmp_path)["standing op"][row["id"]]
    t = card.text()
    assert row["due_by"] in t or re.search(r"(?<![\d-])%d(?!\d)" % row["days_late"], t), \
        "the overdue card names neither the due day %s nor %d days late: %s" % (row["due_by"], row["days_late"], t)


def test_a_parked_late_item_carries_both_marks(project, rec, tmp_path):
    _conn, tid = rec
    card = _views(project, tmp_path)["standing op"][tid["Kilo late and parked"]]
    assert "rm-deferred" in card.classes, "the deferred state of the card changed: %s" % card.classes
    assert marked(card), "a deferred item past due lost its overdue mark (aaritems counts deferred as not done)"


def test_unreadable_due_is_said_and_never_counted_late(project, rec, tmp_path, monkeypatch):
    """aaritems.overdue lists a due it cannot read with due_by and days_late None (pad.py says so)."""
    _conn, tid = rec
    from alpaca import aaritems
    real = aaritems.overdue
    bravo = tid["Bravo due next week"]

    def with_unreadable(conn, today=None):
        rows = real(conn, today)
        return rows + [{"id": bravo, "op": standing(project), "source": items.OP, "line": None, "owner": "fixture-keeper",
                        "status": "open", "due": None, "due_by": None, "days_late": None}]
    monkeypatch.setattr(aaritems, "overdue", with_unreadable)
    card = _views(project, tmp_path)["standing op"][bravo]
    t = card.text()
    assert UNREADABLE_DUE.search(t), "a due the record cannot read is not said on the card (does the hub call aaritems.overdue?): " + t
    assert "None" not in t and "null" not in t and "undefined" not in t and "NaN" not in t, t
    assert "0 day" not in t, t

