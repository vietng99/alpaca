"""(cause C8) Marks land on the right op and the right task. Ways this fails after it lands:

  * a trigger with no op (loose: a NO-GO or release FAIL nobody owns) is pinned to some op, or the
    fold crashes on it
  * a trigger row naming an op that is not in the ops table crashes the fold (an index into
    db.rows(...)[0], as aar owed once did) and takes the page down
  * the overdue mark is keyed by op or by group: the source op's own cards get marked, or the mark
    on the standing op's card does not match the workspace roadmap's
  * a closed op that owes is drawn as a blocking alarm; op close for a closed op blocks nothing
"""
from alpaca.tests.hub_aar_kit import (OP, OTHER, OWED, cards, ev, marked, op_view, overdue_record, overdue_source, owe, page, render,
                     setup, standing, text)
from alpaca import db


def test_a_loose_trigger_is_on_no_op(project, monkeypatch, tmp_path):
    conn = setup(project, monkeypatch)
    loose = db.append_event(conn, session="cli", actor="human", kind="task-move", op=None, ref="t-404",
                            data={"from": "done", "to": "open", "proof": None, "reason": "legacy row"})["id"]
    o = page(project)
    for r in o["ops"]:
        t = text(op_view(o, r["id"], tmp_path))
        assert not ev(t, loose), "the loose trigger event:%d is shown on %s" % (loose, r["id"])
        assert not OWED.search(t), "%s shows owed for a trigger that names no op" % r["id"]


def test_a_trigger_naming_an_unknown_op_does_not_break_the_page(project, monkeypatch, tmp_path):
    conn = setup(project, monkeypatch)
    n = owe(conn)
    db.append_event(conn, session="cli", actor="human", kind="task-move", op="op-404", ref="t-404",
                    data={"from": "done", "to": "open", "proof": None, "reason": "legacy row"})
    o = page(project)
    assert OWED.search(text(op_view(o, OP, tmp_path))) and ev(text(op_view(o, OP, tmp_path)), n)


def test_overdue_marks_follow_the_task_not_the_op(project, monkeypatch, tmp_path):
    _conn, tid = overdue_record(project, monkeypatch)
    late = {r["id"] for r in overdue_source(project)}
    o = page(project)
    st = standing(project)
    ws = {t for t, c in cards(render(o, "renderRoadmap(DATA)", tmp_path)).items() if marked(c)}
    so = {t for t, c in cards(op_view(o, st, tmp_path)).items() if marked(c)}
    src = {t for t, c in cards(op_view(o, OP, tmp_path)).items() if marked(c)}
    assert ws == so == late, (ws, so, late)
    assert not src, "cards of the source op %s are marked overdue: %s" % (OP, src)
    other = {t for t, c in cards(op_view(o, OTHER, tmp_path)).items() if marked(c)}
    assert not other, other
