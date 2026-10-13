"""(done_bar[0]; cause C1, C4, C6) An op owing an AAR shows "AAR owed" and each trigger with its event
id, and exactly the ops aar.owed lists show it. Ways this fails after it lands:

  * the hub counts aar-owe / reopen events itself, so a trigger a seal already answered still shows
    (the cut lives in aar._seal_cut, not in the event list)
  * the fold is put on the full overview only and page_overview drops it, or it rides only one host
  * a not-owed op gets an empty `aar` value that page_overview keeps ({} and False are "present")
  * the served body (serve.Live.overview, cached on the record head) does not move when an aar-owe
    lands, so the page shows the old state until the 15 s bound
"""
import json
import re

from alpaca.tests.hub_aar_kit import (NOT_OWED, OP, OTHER, OWED, REASON, aar_keys, ev, op_row, op_view, owe, owed_source, page,
                     reopen, seal, setup, text, view_text)
from alpaca import aar, serve
from alpaca.tests import aar_items_kit as items


def _three_ops(project, monkeypatch):
    conn = setup(project, monkeypatch)
    items.ok("op", "new", "a quiet op", "--done-when", "nothing owed here")
    ev1 = owe(conn)
    ev2 = reopen(conn, project)
    return conn, ev1, ev2


def test_exactly_the_owed_ops_show_owed(project, monkeypatch, tmp_path):
    _conn, ev1, ev2 = _three_ops(project, monkeypatch)
    owed = owed_source(project)[0]
    o = page(project)
    shown = {r["id"] for r in o["ops"] if OWED.search(text(op_view(o, r["id"], tmp_path)))}
    assert shown == set(owed), "the hub shows AAR owed on %s, aar owed lists %s" % (sorted(shown), sorted(owed))


def test_each_trigger_is_listed_with_its_event_id_and_words(project, monkeypatch, tmp_path):
    _conn, ev1, ev2 = _three_ops(project, monkeypatch)
    t1 = view_text(project, OP, tmp_path)
    assert ev(t1, ev1), "op-001 view does not name its trigger event:%d:\n%s" % (ev1, t1)
    assert REASON in t1, "the aar owe reason is not shown"
    t2 = view_text(project, OTHER, tmp_path)
    assert ev(t2, ev2) and "t-002" in t2, "the reopen trigger of op-002 is not shown:\n" + t2
    assert not ev(t2, ev1) and not ev(t1, ev2), "a trigger shows on the wrong op"


def test_an_answered_trigger_is_not_listed_again(project, monkeypatch, tmp_path):
    """owe, seal (answers it), owe again: only the later trigger is owed (aar._seal_cut)."""
    conn = setup(project, monkeypatch)
    ev1 = owe(conn)
    seal(conn, project)
    ev3 = owe(conn, reason="a second gap found after the seal")
    assert [t["id"] for t in owed_source(project)[0][OP]] == [ev3]
    t = view_text(project, OP, tmp_path)
    assert OWED.search(t) and ev(t, ev3), t
    assert not ev(t, ev1), "the hub lists event:%d, which the seal answered" % ev1


def test_the_served_page_keeps_the_fact_and_quiet_ops_carry_none(project, monkeypatch):
    _three_ops(project, monkeypatch)
    o = page(project)
    for op in (OP, OTHER):
        assert aar_keys(op_row(o, op)), "page_overview dropped the AAR fact of %s" % op
    quiet = next(r for r in o["ops"] if r["id"] not in (OP, OTHER))
    assert not aar_keys(quiet), "an op with no AAR fact carries %s (page_overview keeps {} and False)" % aar_keys(quiet)


def test_no_op_says_not_owed_while_it_owes(project, monkeypatch, tmp_path):
    _three_ops(project, monkeypatch)
    for op in (OP, OTHER):
        assert not NOT_OWED.search(view_text(project, op, tmp_path)), op


def test_the_served_body_moves_when_an_aar_event_lands(project, monkeypatch):
    conn = setup(project, monkeypatch)
    live = serve.Live(str(project))
    before = json.loads(live.overview()[0])
    assert not aar_keys(op_row(before, OP))
    n = owe(conn)
    after = json.loads(live.overview()[0])
    got = json.dumps(aar_keys(op_row(after, OP)))
    assert re.search(r"(?<!\d)%d(?!\d)" % n, got), "the cached body still shows the old AAR state: %s" % got
    assert aar.KIND_OWE not in serve.OVERVIEW_QUIET_KINDS and aar.KIND_SEAL not in serve.OVERVIEW_QUIET_KINDS
